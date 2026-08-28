package authoritycutover

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"reflect"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func TestApprovalExecutionFencerConsumesApprovalAndReturnsOpaqueFence(t *testing.T) {
	fixture := newApprovalExecutionFenceFixture(t)
	store := newFakeApprovalExecutionFenceStore()
	fencer := mustApprovalExecutionFencer(t, store, 0x71)

	fence, err := fencer.ConsumeAndFence(
		t.Context(),
		fixture.plan,
		fixture.approval,
		fixture.report,
		fixture.executionAttemptID,
		fixture.now,
	)
	if err != nil {
		t.Fatalf("ConsumeAndFence: %v", err)
	}
	record := fence.EvidenceRecord()
	if fence.FenceEpoch() != 1 || record.FenceEpoch != 1 ||
		fence.ExecutionAttemptID() != fixture.executionAttemptID ||
		fence.PolicyHead() != record.ExpectedPolicyHead ||
		fence.MutationNotAfter() != fixture.report.ExpiresAt() ||
		record.TargetBeforeStateDigest != fixture.report.Digest() ||
		record.ApprovalPolicyTargetDigest != fixture.approval.PolicyTargetDigest() ||
		!validApprovalExecutionFenceRecord(record, true) ||
		store.compareCalls != 1 || store.loadCalls != 1 {
		t.Fatalf("fence evidence is incomplete: %+v", record)
	}
	if fence.ConsumptionID() != record.ConsumptionID || fence.OperationID() != record.OperationID ||
		!strings.HasPrefix(record.ConsumptionID, "approval-consumption/") ||
		!strings.HasPrefix(record.OperationID, "approval-operation/") {
		t.Fatalf("platform-derived identities are invalid: %+v", record)
	}
	encoded, err := json.Marshal(fence)
	if err != nil || string(encoded) != "{}" {
		t.Fatalf("opaque fence encoded as %s, %v", encoded, err)
	}
	typeOfFence := reflect.TypeOf(fence)
	for index := range typeOfFence.NumField() {
		if typeOfFence.Field(index).IsExported() {
			t.Fatalf("ApprovalMutationFence exposes field %q", typeOfFence.Field(index).Name)
		}
	}
	for _, forbidden := range []string{
		strings.Repeat("q", approvalExecutionTokenBytes),
		"postgresql://",
		"signature",
		"password",
	} {
		if strings.Contains(strings.ToLower(string(mustJSON(t, record))), strings.ToLower(forbidden)) {
			t.Fatalf("fence record exposed forbidden material %q", forbidden)
		}
	}
	tokenCopy := fence.tokenBytes()
	tokenCopy[0] ^= 0xff
	if fence.tokenBytes()[0] != 0x71 {
		t.Fatal("caller mutation escaped opaque token boundary")
	}
}

func TestApprovalMutationFenceRedactsFormattingAndStructuredLogs(t *testing.T) {
	fixture := newApprovalExecutionFenceFixture(t)
	store := newFakeApprovalExecutionFenceStore()
	fencer := mustApprovalExecutionFencer(t, store, 0x71)
	fence, err := fencer.ConsumeAndFence(
		t.Context(),
		fixture.plan,
		fixture.approval,
		fixture.report,
		fixture.executionAttemptID,
		fixture.now,
	)
	if err != nil {
		t.Fatalf("ConsumeAndFence: %v", err)
	}

	capabilityCanary := strings.Repeat("q", approvalExecutionTokenBytes)
	formatted := fmt.Sprintf("%v|%+v|%#v|%s|%q", fence, fence, fence, fence, fence)
	if strings.Contains(formatted, capabilityCanary) ||
		strings.Contains(formatted, fixture.executionAttemptID) ||
		formatted != "ApprovalMutationFence{redacted}|ApprovalMutationFence{redacted}|"+
			"ApprovalMutationFence{redacted}|ApprovalMutationFence{redacted}|"+
			`"ApprovalMutationFence{redacted}"` {
		t.Fatalf("opaque fence formatting was not redacted: %q", formatted)
	}

	var output bytes.Buffer
	logger := slog.New(slog.NewJSONHandler(&output, nil))
	logger.Info("fence canary", "fence", fence)
	logged := output.String()
	if strings.Contains(logged, capabilityCanary) ||
		strings.Contains(logged, fixture.executionAttemptID) ||
		!strings.Contains(logged, approvalMutationFenceRedacted) {
		t.Fatalf("opaque fence structured log was not redacted: %q", logged)
	}
}

func TestApprovalExecutionFencerReconcilesCommitUnknownWithExactToken(t *testing.T) {
	fixture := newApprovalExecutionFenceFixture(t)
	store := newFakeApprovalExecutionFenceStore()
	store.commitThenErr = ErrApprovalExecutionCommitUncertain
	fencer := mustApprovalExecutionFencer(t, store, 0x72)

	fence, err := fencer.ConsumeAndFence(
		t.Context(),
		fixture.plan,
		fixture.approval,
		fixture.report,
		fixture.executionAttemptID,
		fixture.now,
	)
	if err != nil {
		t.Fatalf("ConsumeAndFence lost ACK: %v", err)
	}
	if fence.FenceEpoch() != 1 || store.loadCalls != 1 {
		t.Fatal("authoritative readback did not recover exact committed fence")
	}

	missing := newFakeApprovalExecutionFenceStore()
	missing.compareErr = ErrApprovalExecutionCommitUncertain
	missingFencer := mustApprovalExecutionFencer(t, missing, 0x73)
	if _, err := missingFencer.ConsumeAndFence(
		t.Context(),
		fixture.plan,
		fixture.approval,
		fixture.report,
		fixture.executionAttemptID,
		fixture.now,
	); !errors.Is(err, ErrApprovalExecutionCommitUncertain) {
		t.Fatalf("missing readback error = %v, want %v", err, ErrApprovalExecutionCommitUncertain)
	}
}

func TestApprovalExecutionFencerRejectsReplayAndBindingDrift(t *testing.T) {
	fixture := newApprovalExecutionFenceFixture(t)
	store := newFakeApprovalExecutionFenceStore()
	fencer := mustApprovalExecutionFencer(t, store, 0x74)
	if _, err := fencer.ConsumeAndFence(
		t.Context(),
		fixture.plan,
		fixture.approval,
		fixture.report,
		fixture.executionAttemptID,
		fixture.now,
	); err != nil {
		t.Fatalf("first ConsumeAndFence: %v", err)
	}
	if _, err := fencer.ConsumeAndFence(
		t.Context(),
		fixture.plan,
		fixture.approval,
		fixture.report,
		"execution-attempt/second",
		fixture.now,
	); !errors.Is(err, ErrApprovalExecutionConflict) {
		t.Fatalf("approval replay error = %v, want %v", err, ErrApprovalExecutionConflict)
	}
	if len(store.states) != 1 {
		t.Fatalf("replay left %d durable records, want 1", len(store.states))
	}

	checksBefore := store.compareCalls
	tests := map[string]struct {
		approval VerifiedApproval
		report   PreflightReport
		attempt  string
		now      time.Time
		want     error
	}{
		"invalid attempt": {
			approval: fixture.approval,
			report:   fixture.report,
			attempt:  "caller-selected-retry",
			now:      fixture.now,
			want:     ErrInvalidApprovalExecutionState,
		},
		"expired report": {
			approval: fixture.approval,
			report:   fixture.report,
			attempt:  "execution-attempt/expired",
			now:      fixture.report.ExpiresAt(),
			want:     ErrApprovalExecutionExpired,
		},
		"target drift": {
			approval: func() VerifiedApproval {
				value := fixture.approval
				value.targetDigest = "sha256:" + strings.Repeat("e", 64)
				return value
			}(),
			report:  fixture.report,
			attempt: "execution-attempt/target-drift",
			now:     fixture.now,
			want:    ErrInvalidApprovalExecutionState,
		},
	}
	for name, test := range tests {
		t.Run(name, func(t *testing.T) {
			if _, err := fencer.ConsumeAndFence(
				t.Context(),
				fixture.plan,
				test.approval,
				test.report,
				test.attempt,
				test.now,
			); !errors.Is(err, test.want) {
				t.Fatalf("error = %v, want %v", err, test.want)
			}
		})
	}
	if store.compareCalls != checksBefore {
		t.Fatal("invalid local binding reached durable store")
	}
}

func TestApprovalExecutionFenceRecordRejectsFieldAndDigestDrift(t *testing.T) {
	fixture := newApprovalExecutionFenceFixture(t)
	candidate, err := newApprovalExecutionFenceCandidate(
		fixture.plan,
		fixture.approval,
		fixture.report,
		fixture.executionAttemptID,
	)
	if err != nil {
		t.Fatalf("newApprovalExecutionFenceCandidate: %v", err)
	}
	token := make([]byte, approvalExecutionTokenBytes)
	for index := range token {
		token[index] = 0x75
	}
	record, err := sealApprovalExecutionFenceRecord(
		candidate,
		7,
		fixture.now,
		domainSeparatedDigest(approvalExecutionTokenDigestDomain, token),
	)
	if err != nil {
		t.Fatalf("sealApprovalExecutionFenceRecord: %v", err)
	}
	mutations := map[string]func(*ApprovalExecutionFenceRecord){
		"approval": func(value *ApprovalExecutionFenceRecord) {
			value.ApprovalDigest = "sha256:" + strings.Repeat("e", 64)
		},
		"reference": func(value *ApprovalExecutionFenceRecord) { value.ApprovalReference += "-other" },
		"attempt":   func(value *ApprovalExecutionFenceRecord) { value.ExecutionAttemptID += "-other" },
		"head": func(value *ApprovalExecutionFenceRecord) {
			value.ExpectedPolicyHead.Revision++
		},
		"epoch": func(value *ApprovalExecutionFenceRecord) { value.FenceEpoch++ },
		"operation": func(value *ApprovalExecutionFenceRecord) {
			value.OperationID = "approval-operation/other"
		},
		"before state": func(value *ApprovalExecutionFenceRecord) {
			value.TargetBeforeStateDigest = "sha256:" + strings.Repeat("e", 64)
		},
		"token": func(value *ApprovalExecutionFenceRecord) {
			value.TokenDigest = "sha256:" + strings.Repeat("e", 64)
		},
		"record digest": func(value *ApprovalExecutionFenceRecord) {
			value.RecordDigest = "sha256:" + strings.Repeat("e", 64)
		},
	}
	for name, mutate := range mutations {
		t.Run(name, func(t *testing.T) {
			changed := record
			mutate(&changed)
			if validApprovalExecutionFenceRecord(changed, true) {
				t.Fatal("drifted record remained valid")
			}
		})
	}
}

func TestApprovalExecutionFencerAllowsOneConcurrentApprovalConsumption(t *testing.T) {
	fixture := newApprovalExecutionFenceFixture(t)
	store := newFakeApprovalExecutionFenceStore()
	var tokenSequence atomic.Uint64
	fencer, err := newApprovalExecutionFencer(store, func(destination []byte) error {
		sequence := tokenSequence.Add(1)
		for index := range destination {
			destination[index] = byte(sequence + uint64(index))
		}
		return nil
	})
	if err != nil {
		t.Fatalf("newApprovalExecutionFencer: %v", err)
	}

	const contenders = 64
	var wait sync.WaitGroup
	errorsByContender := make(chan error, contenders)
	for index := range contenders {
		wait.Add(1)
		go func() {
			defer wait.Done()
			_, err := fencer.ConsumeAndFence(
				t.Context(),
				fixture.plan,
				fixture.approval,
				fixture.report,
				fmt.Sprintf("execution-attempt/concurrent-%02d", index),
				fixture.now,
			)
			errorsByContender <- err
		}()
	}
	wait.Wait()
	close(errorsByContender)
	successes := 0
	conflicts := 0
	for err := range errorsByContender {
		switch {
		case err == nil:
			successes++
		case errors.Is(err, ErrApprovalExecutionConflict):
			conflicts++
		default:
			t.Fatalf("unexpected concurrent error: %v", err)
		}
	}
	if successes != 1 || conflicts != contenders-1 || len(store.states) != 1 ||
		len(store.consumptions) != 1 || len(store.activeBySpace) != 1 || store.nextEpoch != 1 {
		t.Fatalf(
			"concurrent result success=%d conflict=%d states=%d consumptions=%d active=%d epoch=%d",
			successes,
			conflicts,
			len(store.states),
			len(store.consumptions),
			len(store.activeBySpace),
			store.nextEpoch,
		)
	}
}

func TestApprovalExecutionFencerUsesFreshReadbackAfterCallerCancellation(t *testing.T) {
	fixture := newApprovalExecutionFenceFixture(t)
	store := &cancellationApprovalExecutionFenceStore{}
	fencer := mustApprovalExecutionFencer(t, store, 0x76)
	ctx, cancel := context.WithCancel(context.WithValue(
		t.Context(),
		approvalExecutionFenceContextKey{},
		"retained",
	))
	cancel()

	_, err := fencer.ConsumeAndFence(
		ctx,
		fixture.plan,
		fixture.approval,
		fixture.report,
		fixture.executionAttemptID,
		fixture.now,
	)
	if !errors.Is(err, ErrApprovalExecutionCommitUncertain) {
		t.Fatalf("canceled fence error = %v, want %v", err, ErrApprovalExecutionCommitUncertain)
	}
	if store.loadContextCanceled || store.loadContextValue != "retained" || store.loadCalls != 1 {
		t.Fatalf(
			"fresh readback canceled=%t value=%q calls=%d",
			store.loadContextCanceled,
			store.loadContextValue,
			store.loadCalls,
		)
	}
}

func TestApprovalExecutionFencerRejectsInvalidDependenciesAndTokenSources(t *testing.T) {
	var nilStore *fakeApprovalExecutionFenceStore
	if _, err := NewApprovalExecutionFencer(nilStore); !errors.Is(
		err,
		ErrInvalidApprovalExecutionFencer,
	) {
		t.Fatalf("typed nil store error = %v, want %v", err, ErrInvalidApprovalExecutionFencer)
	}
	if _, err := newApprovalExecutionFencer(newFakeApprovalExecutionFenceStore(), nil); !errors.Is(
		err,
		ErrInvalidApprovalExecutionFencer,
	) {
		t.Fatalf("nil token source error = %v, want %v", err, ErrInvalidApprovalExecutionFencer)
	}

	fixture := newApprovalExecutionFenceFixture(t)
	zeroStore := newFakeApprovalExecutionFenceStore()
	zeroFencer, err := newApprovalExecutionFencer(zeroStore, func([]byte) error { return nil })
	if err != nil {
		t.Fatalf("new zero-token fencer: %v", err)
	}
	if _, err := zeroFencer.ConsumeAndFence(
		t.Context(),
		fixture.plan,
		fixture.approval,
		fixture.report,
		fixture.executionAttemptID,
		fixture.now,
	); !errors.Is(err, ErrApprovalExecutionStoreUnavailable) {
		t.Fatalf("zero token error = %v, want %v", err, ErrApprovalExecutionStoreUnavailable)
	}
	if zeroStore.compareCalls != 0 {
		t.Fatal("zero token reached durable store")
	}

	const canary = "token-source-private-canary"
	failingStore := newFakeApprovalExecutionFenceStore()
	failingFencer, err := newApprovalExecutionFencer(failingStore, func([]byte) error {
		return errors.New(canary)
	})
	if err != nil {
		t.Fatalf("new failing-token fencer: %v", err)
	}
	_, sourceErr := failingFencer.ConsumeAndFence(
		t.Context(),
		fixture.plan,
		fixture.approval,
		fixture.report,
		fixture.executionAttemptID,
		fixture.now,
	)
	if !errors.Is(sourceErr, ErrApprovalExecutionStoreUnavailable) ||
		strings.Contains(sourceErr.Error(), canary) || failingStore.compareCalls != 0 {
		t.Fatalf("token source error was not fixed and redacted: %v", sourceErr)
	}
}

type approvalExecutionFenceFixture struct {
	approval           VerifiedApproval
	executionAttemptID string
	now                time.Time
	plan               Plan
	report             PreflightReport
}

func newApprovalExecutionFenceFixture(t *testing.T) approvalExecutionFenceFixture {
	t.Helper()
	policyFixture := newApprovalPolicyFixture(t)
	activationStore := newFakeApprovalPolicyActivationStore()
	activator := mustApprovalPolicyActivator(t, policyFixture.verifier, activationStore)
	activated, err := activator.Activate(
		t.Context(),
		policyFixture.raw,
		policyFixture.input.NotBefore.Add(30*time.Minute),
	)
	if err != nil {
		t.Fatalf("Activate: %v", err)
	}
	verifier, err := activated.NewApprovalVerifier()
	if err != nil {
		t.Fatalf("NewApprovalVerifier: %v", err)
	}
	plan, err := BuildPlan(validPlanInput())
	if err != nil {
		t.Fatalf("BuildPlan: %v", err)
	}
	approvedAt := policyFixture.input.NotBefore.Add(35 * time.Minute)
	toSign, err := NewApprovalToSign(
		plan,
		"release-key-2026-08",
		approvedAt,
		approvedAt.Add(10*time.Minute),
	)
	if err != nil {
		t.Fatalf("NewApprovalToSign: %v", err)
	}
	raw, err := toSign.Encode(ed25519.Sign(policyFixture.onlineKeys[0], toSign.SigningBytes()))
	if err != nil {
		t.Fatalf("Encode approval: %v", err)
	}
	observedAt := approvedAt.Add(time.Minute)
	approval, err := verifier.Verify(plan, raw, observedAt)
	if err != nil {
		t.Fatalf("Verify: %v", err)
	}
	report, err := buildPreflightReport(
		plan,
		approval,
		observedAt,
		passingPreflightObservations(),
	)
	if err != nil {
		t.Fatalf("buildPreflightReport: %v", err)
	}
	return approvalExecutionFenceFixture{
		approval:           approval,
		executionAttemptID: "execution-attempt/cutover-0001",
		now:                observedAt,
		plan:               plan,
		report:             report,
	}
}

func mustApprovalExecutionFencer(
	t *testing.T,
	store approvalExecutionFenceStore,
	tokenByte byte,
) ApprovalExecutionFencer {
	t.Helper()
	fencer, err := newApprovalExecutionFencer(store, func(destination []byte) error {
		for index := range destination {
			destination[index] = tokenByte
		}
		return nil
	})
	if err != nil {
		t.Fatalf("newApprovalExecutionFencer: %v", err)
	}
	return fencer
}

type fakeApprovalExecutionFenceStore struct {
	mu            sync.Mutex
	states        map[string]ApprovalExecutionFenceStoredState
	consumptions  map[string]string
	activeBySpace map[ApprovalPolicyNamespace]string
	compareCalls  int
	loadCalls     int
	compareErr    error
	commitThenErr error
	nextEpoch     uint64
}

func newFakeApprovalExecutionFenceStore() *fakeApprovalExecutionFenceStore {
	return &fakeApprovalExecutionFenceStore{
		states:        make(map[string]ApprovalExecutionFenceStoredState),
		consumptions:  make(map[string]string),
		activeBySpace: make(map[ApprovalPolicyNamespace]string),
	}
}

func (store *fakeApprovalExecutionFenceStore) Load(
	_ context.Context,
	namespace ApprovalPolicyNamespace,
	operationID string,
) (ApprovalExecutionFenceStoredState, error) {
	store.mu.Lock()
	defer store.mu.Unlock()
	store.loadCalls++
	state, exists := store.states[fakeApprovalExecutionFenceKey(namespace, operationID)]
	if !exists {
		return ApprovalExecutionFenceStoredState{}, ErrApprovalExecutionFenceNotFound
	}
	return state, nil
}

func (store *fakeApprovalExecutionFenceStore) CompareAndOpen(
	_ context.Context,
	namespace ApprovalPolicyNamespace,
	expected ApprovalPolicyHead,
	candidate approvalExecutionFenceCandidate,
	tokenDigest string,
) error {
	store.mu.Lock()
	defer store.mu.Unlock()
	store.compareCalls++
	if store.compareErr != nil {
		return store.compareErr
	}
	if !validApprovalExecutionFenceCandidate(candidate) ||
		expected != candidate.record.ExpectedPolicyHead ||
		namespace.PolicyID != expected.PolicyID || namespace.TargetDigest != expected.TargetDigest {
		return ErrInvalidApprovalExecutionState
	}
	if _, exists := store.activeBySpace[namespace]; exists {
		return ErrApprovalExecutionConflict
	}
	if _, exists := store.consumptions[candidate.record.ConsumptionID]; exists {
		return ErrApprovalExecutionConflict
	}
	store.nextEpoch++
	record, err := sealApprovalExecutionFenceRecord(
		candidate,
		store.nextEpoch,
		candidate.record.PreflightObservedAt,
		tokenDigest,
	)
	if err != nil {
		return err
	}
	key := fakeApprovalExecutionFenceKey(namespace, candidate.record.OperationID)
	store.states[key] = ApprovalExecutionFenceStoredState{Record: record}
	store.consumptions[candidate.record.ConsumptionID] = key
	store.activeBySpace[namespace] = key
	return store.commitThenErr
}

func fakeApprovalExecutionFenceKey(
	namespace ApprovalPolicyNamespace,
	operationID string,
) string {
	return namespace.PolicyID + "\x00" + namespace.TargetDigest + "\x00" + operationID
}

type cancellationApprovalExecutionFenceStore struct {
	loadCalls           int
	loadContextCanceled bool
	loadContextValue    string
}

type approvalExecutionFenceContextKey struct{}

func (store *cancellationApprovalExecutionFenceStore) Load(
	ctx context.Context,
	_ ApprovalPolicyNamespace,
	_ string,
) (ApprovalExecutionFenceStoredState, error) {
	store.loadCalls++
	store.loadContextCanceled = ctx.Err() != nil
	store.loadContextValue, _ = ctx.Value(approvalExecutionFenceContextKey{}).(string)
	return ApprovalExecutionFenceStoredState{}, ErrApprovalExecutionFenceNotFound
}

func (store *cancellationApprovalExecutionFenceStore) CompareAndOpen(
	ctx context.Context,
	_ ApprovalPolicyNamespace,
	_ ApprovalPolicyHead,
	_ approvalExecutionFenceCandidate,
	_ string,
) error {
	if ctx.Err() == nil {
		return ErrApprovalExecutionStoreUnavailable
	}
	return ErrApprovalExecutionCommitUncertain
}

func mustJSON(t *testing.T, value any) []byte {
	t.Helper()
	encoded, err := json.Marshal(value)
	if err != nil {
		t.Fatalf("json.Marshal: %v", err)
	}
	return encoded
}
