package authoritycutover

import (
	"context"
	"crypto/ed25519"
	"encoding/json"
	"errors"
	"reflect"
	"strings"
	"sync"
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

func mustJSON(t *testing.T, value any) []byte {
	t.Helper()
	encoded, err := json.Marshal(value)
	if err != nil {
		t.Fatalf("json.Marshal: %v", err)
	}
	return encoded
}
