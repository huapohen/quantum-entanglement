package authoritycutover

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestApprovalPolicyActivationRequiresDurableReadbackAndIsIdempotentByContent(t *testing.T) {
	fixture := newApprovalPolicyFixture(t)
	store := newFakeApprovalPolicyActivationStore()
	activator, err := NewApprovalPolicyActivator(fixture.verifier, store)
	if err != nil {
		t.Fatalf("NewApprovalPolicyActivator: %v", err)
	}
	activated, err := activator.Activate(t.Context(), fixture.raw, fixture.now)
	if err != nil {
		t.Fatalf("Activate: %v", err)
	}
	record := activated.ActivationRecord()
	if activated.Revision() != 1 || activated.PolicyDigest() != fixture.toSign.PolicyDigest() ||
		activated.PolicyRevision() != "policy/postgres-cell-a/revision-1" ||
		!activated.ApprovalVerificationEnabled() || record.MutationAuthorized ||
		record.PolicyEnvelopeDigest != digestApprovalPolicyEnvelope(fixture.raw) ||
		record.RootTrustBundleDigest != fixture.verifier.bundleDigest ||
		!validApprovalPolicyActivationRecord(record) {
		t.Fatalf("activation evidence is incomplete: %+v", record)
	}
	if record.ActivationRecordDigest != "sha256:badbcf911a373046560d69e6d9fcf1573c77e45f15fe11ecb8c9248db64a14bc" {
		t.Fatalf("activation record golden digest changed: %s", record.ActivationRecordDigest)
	}
	encoded, err := json.Marshal(activated)
	if err != nil || string(encoded) != "{}" {
		t.Fatalf("ActivatedApprovalPolicy exposed state: %s, %v", encoded, err)
	}
	typeOfActivated := reflect.TypeOf(activated)
	for index := range typeOfActivated.NumField() {
		if typeOfActivated.Field(index).IsExported() {
			t.Fatalf("ActivatedApprovalPolicy exposes field %q", typeOfActivated.Field(index).Name)
		}
	}

	canonicalCopy := activated.CanonicalPolicyBytes()
	canonicalCopy[0] ^= 0xff
	recordCopy := activated.ActivationRecord()
	recordCopy.RootSignerFingerprints[0] = "mutated"
	if !slices.Equal(activated.CanonicalPolicyBytes(), fixture.raw) ||
		activated.ActivationRecord().RootSignerFingerprints[0] == "mutated" {
		t.Fatal("caller mutation escaped activated policy boundary")
	}

	threeSignatureEnvelope := signApprovalPolicy(t, fixture.toSign, fixture.rootKeys, []int{0, 1, 2})
	idempotent, err := activator.Activate(t.Context(), threeSignatureEnvelope, fixture.now.Add(time.Minute))
	if err != nil {
		t.Fatalf("idempotent Activate: %v", err)
	}
	if !slices.Equal(idempotent.CanonicalPolicyBytes(), fixture.raw) || store.compareCalls != 1 ||
		idempotent.PolicyDigest() != activated.PolicyDigest() {
		t.Fatal("same revision/content did not return the exact archived activation")
	}
	stored := store.mustState(t, approvalPolicyNamespace(fixture.toSign.snapshot))
	if stored.Head != record.Head() || !slices.Equal(stored.CanonicalPolicy, fixture.raw) {
		t.Fatal("durable fake did not preserve exact canonical policy and head")
	}
}

func TestApprovalPolicyActivationAdvancesExactChainAndPreservesKeyLineage(t *testing.T) {
	fixture := newApprovalPolicyFixture(t)
	store := newFakeApprovalPolicyActivationStore()
	activator := mustApprovalPolicyActivator(t, fixture.verifier, store)
	first, err := activator.Activate(t.Context(), fixture.raw, fixture.now)
	if err != nil {
		t.Fatalf("Activate first: %v", err)
	}
	secondToSign, secondRaw, secondInput := nextApprovalPolicy(t, fixture, nil)
	second, err := activator.Activate(t.Context(), secondRaw, secondInput.NotBefore.Add(time.Minute))
	if err != nil {
		t.Fatalf("Activate second: %v", err)
	}
	if second.Revision() != first.Revision()+1 ||
		second.ActivationRecord().PreviousPolicyDigest != first.PolicyDigest() ||
		second.PolicyDigest() != secondToSign.PolicyDigest() || store.compareCalls != 2 ||
		second.Snapshot().Keys[1].NotBefore != first.Snapshot().Keys[1].NotBefore {
		t.Fatalf("exact policy chain was not preserved: %+v", second.ActivationRecord())
	}

	denyToSign, denyRaw, denyInput := nextApprovalPolicy(t, fixture, func(input *ApprovalPolicyInput) {
		input.Keys[1].Status = ApprovalPolicyKeyRevoked
		input.Keys[1].RevokedAt = input.NotBefore
		input.Keys[1].RevocationReason = "revocation/emergency-freeze"
		input.DenyAll = true
	})
	denyStore := newFakeApprovalPolicyActivationStore()
	denyActivator := mustApprovalPolicyActivator(t, fixture.verifier, denyStore)
	if _, err := denyActivator.Activate(t.Context(), fixture.raw, fixture.now); err != nil {
		t.Fatalf("Activate deny base: %v", err)
	}
	denied, err := denyActivator.Activate(t.Context(), denyRaw, denyInput.NotBefore.Add(time.Minute))
	if err != nil {
		t.Fatalf("Activate deny-all: %v", err)
	}
	if denied.ApprovalVerificationEnabled() || denied.PolicyDigest() != denyToSign.PolicyDigest() {
		t.Fatal("explicit deny-all policy did not durably disable approval verification")
	}
}

func TestApprovalPolicyActivationRejectsRollbackForkGapBrokenChainAndLineageDrift(t *testing.T) {
	fixture := newApprovalPolicyFixture(t)

	t.Run("same revision fork", func(t *testing.T) {
		store, activator := activatedGenesisFixture(t, fixture)
		_, forkRaw := forkApprovalPolicy(t, fixture, func(input *ApprovalPolicyInput) {
			input.MaximumApprovalLifetime = 9 * time.Minute
		})
		if _, err := activator.Activate(t.Context(), forkRaw, fixture.now); !errors.Is(err, ErrApprovalPolicyFork) {
			t.Fatalf("Activate error = %v, want %v", err, ErrApprovalPolicyFork)
		}
		if store.compareCalls != 1 {
			t.Fatal("fork reached durable CAS")
		}
	})

	t.Run("revision gap", func(t *testing.T) {
		store, activator := activatedGenesisFixture(t, fixture)
		input := cloneApprovalPolicyInput(fixture.input)
		input.Revision = 3
		input.PreviousPolicyDigest = fixture.toSign.PolicyDigest()
		input.IssuedAt = input.IssuedAt.Add(time.Hour)
		input.NotBefore = input.NotBefore.Add(time.Hour)
		toSign, err := NewApprovalPolicyToSign(input)
		if err != nil {
			t.Fatalf("NewApprovalPolicyToSign: %v", err)
		}
		raw := signApprovalPolicy(t, toSign, fixture.rootKeys, []int{0, 1})
		if _, err := activator.Activate(t.Context(), raw, input.NotBefore.Add(time.Minute)); !errors.Is(err, ErrApprovalPolicyGap) {
			t.Fatalf("Activate error = %v, want %v", err, ErrApprovalPolicyGap)
		}
		if store.compareCalls != 1 {
			t.Fatal("gap reached durable CAS")
		}
	})

	t.Run("broken previous digest", func(t *testing.T) {
		store, activator := activatedGenesisFixture(t, fixture)
		_, raw, input := nextApprovalPolicy(t, fixture, func(input *ApprovalPolicyInput) {
			input.PreviousPolicyDigest = "sha256:" + strings.Repeat("d", 64)
		})
		if _, err := activator.Activate(t.Context(), raw, input.NotBefore.Add(time.Minute)); !errors.Is(err, ErrApprovalPolicyBrokenChain) {
			t.Fatalf("Activate error = %v, want %v", err, ErrApprovalPolicyBrokenChain)
		}
		if store.compareCalls != 1 {
			t.Fatal("broken chain reached durable CAS")
		}
	})

	lineageMutations := map[string]func(*ApprovalPolicyInput){
		"removed tombstone":   func(input *ApprovalPolicyInput) { input.Keys = input.Keys[1:] },
		"retargeted identity": func(input *ApprovalPolicyInput) { input.Keys[1].ApproverIdentity = "release-owner/other" },
		"changed generation":  func(input *ApprovalPolicyInput) { input.Keys[1].Generation = "generation-99" },
		"changed public key": func(input *ApprovalPolicyInput) {
			input.Keys[1].PublicKey = deterministicEd25519PrivateKey(0xc1).Public().(ed25519.PublicKey)
		},
		"changed reference scope": func(input *ApprovalPolicyInput) { input.Keys[1].ReferencePrefix = "approval/postgres-cell-a/other/" },
		"extended validity":       func(input *ApprovalPolicyInput) { input.Keys[1].NotAfter = input.Keys[1].NotAfter.Add(time.Hour) },
		"unrevoked tombstone": func(input *ApprovalPolicyInput) {
			input.NotAfter = time.Date(2026, 9, 30, 0, 0, 0, 0, time.UTC)
			input.Keys[0].Status = ApprovalPolicyKeyActive
			input.Keys[0].RevokedAt = time.Time{}
			input.Keys[0].RevocationReason = ""
		},
	}
	for name, mutate := range lineageMutations {
		t.Run(name, func(t *testing.T) {
			store, activator := activatedGenesisFixture(t, fixture)
			_, raw, input := nextApprovalPolicy(t, fixture, mutate)
			if _, err := activator.Activate(t.Context(), raw, input.NotBefore.Add(time.Minute)); !errors.Is(err, ErrApprovalPolicyLineage) {
				t.Fatalf("Activate error = %v, want %v", err, ErrApprovalPolicyLineage)
			}
			if store.compareCalls != 1 {
				t.Fatal("lineage drift reached durable CAS")
			}
		})
	}

	t.Run("rollback after revision two", func(t *testing.T) {
		store, activator := activatedGenesisFixture(t, fixture)
		_, raw, input := nextApprovalPolicy(t, fixture, nil)
		if _, err := activator.Activate(t.Context(), raw, input.NotBefore.Add(time.Minute)); err != nil {
			t.Fatalf("Activate second: %v", err)
		}
		if _, err := activator.Activate(t.Context(), fixture.raw, fixture.now); !errors.Is(err, ErrApprovalPolicyRollback) {
			t.Fatalf("rollback error = %v, want %v", err, ErrApprovalPolicyRollback)
		}
		if store.compareCalls != 2 {
			t.Fatal("rollback reached durable CAS")
		}
	})
}

func TestApprovalPolicyActivationReconcilesCommitUnknownAndNeverReturnsUnprovenState(t *testing.T) {
	fixture := newApprovalPolicyFixture(t)

	t.Run("commit then acknowledgement lost", func(t *testing.T) {
		store := newFakeApprovalPolicyActivationStore()
		store.afterCommitErr = ErrApprovalPolicyCommitUncertain
		activator := mustApprovalPolicyActivator(t, fixture.verifier, store)
		activated, err := activator.Activate(t.Context(), fixture.raw, fixture.now)
		if err != nil || activated.PolicyDigest() != fixture.toSign.PolicyDigest() {
			t.Fatalf("readback did not reconcile committed policy: %+v, %v", activated, err)
		}
	})

	t.Run("uncertain before commit", func(t *testing.T) {
		store := newFakeApprovalPolicyActivationStore()
		store.beforeCommitErr = ErrApprovalPolicyCommitUncertain
		activator := mustApprovalPolicyActivator(t, fixture.verifier, store)
		if _, err := activator.Activate(t.Context(), fixture.raw, fixture.now); !errors.Is(err, ErrApprovalPolicyCommitUncertain) {
			t.Fatalf("Activate error = %v, want %v", err, ErrApprovalPolicyCommitUncertain)
		}
	})

	t.Run("false success without commit", func(t *testing.T) {
		store := newFakeApprovalPolicyActivationStore()
		store.dropCommit = true
		activator := mustApprovalPolicyActivator(t, fixture.verifier, store)
		if _, err := activator.Activate(t.Context(), fixture.raw, fixture.now); !errors.Is(err, ErrApprovalPolicyCommitUncertain) {
			t.Fatalf("Activate error = %v, want %v", err, ErrApprovalPolicyCommitUncertain)
		}
	})

	t.Run("readback unavailable", func(t *testing.T) {
		store := newFakeApprovalPolicyActivationStore()
		store.failLoadAt[2] = errors.New("provider detail must not escape")
		activator := mustApprovalPolicyActivator(t, fixture.verifier, store)
		_, err := activator.Activate(t.Context(), fixture.raw, fixture.now)
		if !errors.Is(err, ErrApprovalPolicyCommitUncertain) || strings.Contains(err.Error(), "provider detail") {
			t.Fatalf("readback error = %v", err)
		}
	})

	t.Run("initial load unavailable", func(t *testing.T) {
		store := newFakeApprovalPolicyActivationStore()
		store.failLoadAt[1] = errors.New("private provider error")
		activator := mustApprovalPolicyActivator(t, fixture.verifier, store)
		_, err := activator.Activate(t.Context(), fixture.raw, fixture.now)
		if !errors.Is(err, ErrApprovalPolicyStoreUnavailable) || strings.Contains(err.Error(), "private provider error") {
			t.Fatalf("load error = %v", err)
		}
	})
}

func TestApprovalPolicyActivationConcurrentForkHasOneDurableWinner(t *testing.T) {
	fixture := newApprovalPolicyFixture(t)
	store, activator := activatedGenesisFixture(t, fixture)
	_, firstRaw, firstInput := nextApprovalPolicy(t, fixture, nil)
	_, secondRaw, _ := nextApprovalPolicy(t, fixture, func(input *ApprovalPolicyInput) {
		input.MaximumApprovalLifetime = 9 * time.Minute
	})
	start := make(chan struct{})
	type result struct {
		policy ActivatedApprovalPolicy
		err    error
	}
	results := make(chan result, 2)
	for _, raw := range [][]byte{firstRaw, secondRaw} {
		raw := slices.Clone(raw)
		go func() {
			<-start
			policy, err := activator.Activate(t.Context(), raw, firstInput.NotBefore.Add(time.Minute))
			results <- result{policy: policy, err: err}
		}()
	}
	close(start)
	firstResult := <-results
	secondResult := <-results
	successes := 0
	failures := 0
	for _, value := range []result{firstResult, secondResult} {
		if value.err == nil {
			successes++
			if value.policy.Revision() != 2 {
				t.Fatalf("winner revision = %d", value.policy.Revision())
			}
		} else if errors.Is(value.err, ErrApprovalPolicyFork) ||
			errors.Is(value.err, ErrApprovalPolicyActivationConflict) {
			failures++
		} else {
			t.Fatalf("unexpected concurrent error: %v", value.err)
		}
	}
	if successes != 1 || failures != 1 || store.mustOnlyState(t).Head.Revision != 2 {
		t.Fatalf("concurrent activation results: successes=%d failures=%d", successes, failures)
	}
}

func TestApprovalPolicyActivationRejectsCorruptDurableStateAndTypedNilStore(t *testing.T) {
	fixture := newApprovalPolicyFixture(t)
	store, activator := activatedGenesisFixture(t, fixture)
	namespace := approvalPolicyNamespace(fixture.toSign.snapshot)
	store.mu.Lock()
	state := store.states[namespace]
	state.Head.PolicyDigest = "sha256:" + strings.Repeat("0", 64)
	store.states[namespace] = state
	store.mu.Unlock()
	_, raw, input := nextApprovalPolicy(t, fixture, nil)
	if _, err := activator.Activate(t.Context(), raw, input.NotBefore.Add(time.Minute)); !errors.Is(err, ErrInvalidApprovalPolicyStoreState) {
		t.Fatalf("corrupt store error = %v, want %v", err, ErrInvalidApprovalPolicyStoreState)
	}

	var typedNil *fakeApprovalPolicyActivationStore
	if _, err := NewApprovalPolicyActivator(fixture.verifier, typedNil); !errors.Is(err, ErrInvalidApprovalPolicyActivator) {
		t.Fatalf("typed nil error = %v, want %v", err, ErrInvalidApprovalPolicyActivator)
	}
	if _, err := (ApprovalPolicyActivator{}).Activate(context.Background(), fixture.raw, fixture.now); !errors.Is(err, ErrInvalidApprovalPolicyActivator) {
		t.Fatalf("zero activator error = %v, want %v", err, ErrInvalidApprovalPolicyActivator)
	}
	if _, err := mustApprovalPolicyActivator(t, fixture.verifier, newFakeApprovalPolicyActivationStore()).Activate(
		nil, fixture.raw, fixture.now,
	); !errors.Is(err, ErrInvalidApprovalPolicyActivator) {
		t.Fatalf("nil context error = %v, want %v", err, ErrInvalidApprovalPolicyActivator)
	}
}

type fakeApprovalPolicyActivationStore struct {
	mu              sync.Mutex
	afterCommitErr  error
	beforeCommitErr error
	compareCalls    int
	dropCommit      bool
	failLoadAt      map[int]error
	loadCalls       int
	states          map[ApprovalPolicyNamespace]ApprovalPolicyStoredState
}

func newFakeApprovalPolicyActivationStore() *fakeApprovalPolicyActivationStore {
	return &fakeApprovalPolicyActivationStore{
		failLoadAt: make(map[int]error),
		states:     make(map[ApprovalPolicyNamespace]ApprovalPolicyStoredState),
	}
}

func (store *fakeApprovalPolicyActivationStore) Load(
	_ context.Context,
	namespace ApprovalPolicyNamespace,
) (ApprovalPolicyStoredState, error) {
	store.mu.Lock()
	defer store.mu.Unlock()
	store.loadCalls++
	if err := store.failLoadAt[store.loadCalls]; err != nil {
		return ApprovalPolicyStoredState{}, err
	}
	state, exists := store.states[namespace]
	if !exists {
		return ApprovalPolicyStoredState{}, ErrApprovalPolicyStoreEmpty
	}
	return cloneApprovalPolicyStoredState(state), nil
}

func (store *fakeApprovalPolicyActivationStore) CompareAndActivate(
	_ context.Context,
	namespace ApprovalPolicyNamespace,
	expected ApprovalPolicyHead,
	record ApprovalPolicyActivationRecord,
	canonical []byte,
) error {
	store.mu.Lock()
	defer store.mu.Unlock()
	store.compareCalls++
	state, exists := store.states[namespace]
	current := approvalPolicyGenesisHead(namespace)
	if exists {
		current = state.Head
	}
	if current != expected {
		return ErrApprovalPolicyActivationConflict
	}
	if store.beforeCommitErr != nil {
		return store.beforeCommitErr
	}
	if !store.dropCommit {
		store.states[namespace] = ApprovalPolicyStoredState{
			CanonicalPolicy: slices.Clone(canonical),
			Head:            record.Head(),
			Record:          cloneApprovalPolicyActivationRecord(record),
		}
	}
	return store.afterCommitErr
}

func (store *fakeApprovalPolicyActivationStore) mustState(
	t *testing.T,
	namespace ApprovalPolicyNamespace,
) ApprovalPolicyStoredState {
	t.Helper()
	store.mu.Lock()
	defer store.mu.Unlock()
	state, exists := store.states[namespace]
	if !exists {
		t.Fatal("missing stored state")
	}
	return cloneApprovalPolicyStoredState(state)
}

func (store *fakeApprovalPolicyActivationStore) mustOnlyState(t *testing.T) ApprovalPolicyStoredState {
	t.Helper()
	store.mu.Lock()
	defer store.mu.Unlock()
	if len(store.states) != 1 {
		t.Fatalf("stored namespace count = %d", len(store.states))
	}
	for _, state := range store.states {
		return cloneApprovalPolicyStoredState(state)
	}
	panic("unreachable")
}

func cloneApprovalPolicyStoredState(state ApprovalPolicyStoredState) ApprovalPolicyStoredState {
	clone := state
	clone.CanonicalPolicy = slices.Clone(state.CanonicalPolicy)
	clone.Record = cloneApprovalPolicyActivationRecord(state.Record)
	return clone
}

func mustApprovalPolicyActivator(
	t *testing.T,
	verifier ApprovalPolicyVerifier,
	store ApprovalPolicyActivationStore,
) ApprovalPolicyActivator {
	t.Helper()
	activator, err := NewApprovalPolicyActivator(verifier, store)
	if err != nil {
		t.Fatalf("NewApprovalPolicyActivator: %v", err)
	}
	return activator
}

func activatedGenesisFixture(
	t *testing.T,
	fixture approvalPolicyFixture,
) (*fakeApprovalPolicyActivationStore, ApprovalPolicyActivator) {
	t.Helper()
	store := newFakeApprovalPolicyActivationStore()
	activator := mustApprovalPolicyActivator(t, fixture.verifier, store)
	if _, err := activator.Activate(t.Context(), fixture.raw, fixture.now); err != nil {
		t.Fatalf("Activate genesis: %v", err)
	}
	return store, activator
}

func nextApprovalPolicy(
	t *testing.T,
	fixture approvalPolicyFixture,
	mutate func(*ApprovalPolicyInput),
) (ApprovalPolicyToSign, []byte, ApprovalPolicyInput) {
	t.Helper()
	input := cloneApprovalPolicyInput(fixture.input)
	input.Revision = 2
	input.PreviousPolicyDigest = fixture.toSign.PolicyDigest()
	input.IssuedAt = input.IssuedAt.Add(time.Hour)
	input.NotBefore = input.NotBefore.Add(time.Hour)
	if mutate != nil {
		mutate(&input)
	}
	toSign, err := NewApprovalPolicyToSign(input)
	if err != nil {
		t.Fatalf("NewApprovalPolicyToSign next: %v", err)
	}
	raw := signApprovalPolicy(t, toSign, fixture.rootKeys, []int{0, 1})
	return toSign, raw, input
}

func forkApprovalPolicy(
	t *testing.T,
	fixture approvalPolicyFixture,
	mutate func(*ApprovalPolicyInput),
) (ApprovalPolicyToSign, []byte) {
	t.Helper()
	input := cloneApprovalPolicyInput(fixture.input)
	mutate(&input)
	toSign, err := NewApprovalPolicyToSign(input)
	if err != nil {
		t.Fatalf("NewApprovalPolicyToSign fork: %v", err)
	}
	return toSign, signApprovalPolicy(t, toSign, fixture.rootKeys, []int{0, 1})
}

func TestApprovalPolicyActivationRecordCanonicalDigestRejectsMutation(t *testing.T) {
	fixture := newApprovalPolicyFixture(t)
	store, _ := activatedGenesisFixture(t, fixture)
	record := store.mustOnlyState(t).Record
	unsigned := cloneApprovalPolicyActivationRecord(record)
	unsigned.ActivationRecordDigest = ""
	canonical, err := marshalApprovalPolicyActivationRecordCanonical(unsigned)
	if err != nil {
		t.Fatalf("marshal record: %v", err)
	}
	if digestApprovalPolicyActivationRecord(canonical) != record.ActivationRecordDigest {
		t.Fatal("record digest cannot be recomputed")
	}
	mutations := map[string]func(*ApprovalPolicyActivationRecord){
		"time":   func(value *ApprovalPolicyActivationRecord) { value.ActivatedAt = value.ActivatedAt.Add(time.Second) },
		"policy": func(value *ApprovalPolicyActivationRecord) { value.PolicyDigest = "sha256:" + strings.Repeat("f", 64) },
		"envelope": func(value *ApprovalPolicyActivationRecord) {
			value.PolicyEnvelopeDigest = "sha256:" + strings.Repeat("e", 64)
		},
		"root": func(value *ApprovalPolicyActivationRecord) {
			value.RootSignerFingerprints[0] = "sha256:" + strings.Repeat("d", 64)
		},
		"enable":   func(value *ApprovalPolicyActivationRecord) { value.ApprovalVerificationEnabled = false },
		"mutation": func(value *ApprovalPolicyActivationRecord) { value.MutationAuthorized = true },
	}
	for name, mutate := range mutations {
		t.Run(name, func(t *testing.T) {
			candidate := cloneApprovalPolicyActivationRecord(record)
			mutate(&candidate)
			if validApprovalPolicyActivationRecord(candidate) {
				t.Fatal("mutated activation record remained valid")
			}
		})
	}
	if bytes.Contains(canonical, []byte("signature")) {
		t.Fatal("activation record contains a reusable signature")
	}
	if strings.Contains(fmt.Sprintf("%+v", ActivatedApprovalPolicy{}), "private") {
		t.Fatal("zero activation representation contains secret material")
	}
}
