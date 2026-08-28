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
	store := newFakeApprovalExecutionFenceStore(fixture.now)
	fencer := mustApprovalExecutionFencer(t, store, 0x71, fixture.now)

	fence, err := fencer.ConsumeAndFence(
		t.Context(),
		fixture.plan,
		fixture.approval,
		fixture.report,
		fixture.attempt,
	)
	if err != nil {
		t.Fatalf("ConsumeAndFence: %v", err)
	}
	record := fence.EvidenceRecord()
	if fence.FenceEpoch() != 1 || record.FenceEpoch != 1 ||
		fence.ExecutionAttemptID() != fixture.attempt.ID() ||
		fence.PolicyHead() != record.ExpectedPolicyHead ||
		fence.MutationNotAfter() != fixture.report.ExpiresAt() ||
		!canonicalDigest.MatchString(record.AdmissionDigest) ||
		record.AdmissionDigest == record.RecordDigest ||
		record.ExecutionAttemptReceiptDigest != fixture.attempt.ReceiptDigest() ||
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
	store := newFakeApprovalExecutionFenceStore(fixture.now)
	fencer := mustApprovalExecutionFencer(t, store, 0x71, fixture.now)
	fence, err := fencer.ConsumeAndFence(
		t.Context(),
		fixture.plan,
		fixture.approval,
		fixture.report,
		fixture.attempt,
	)
	if err != nil {
		t.Fatalf("ConsumeAndFence: %v", err)
	}

	capabilityCanary := strings.Repeat("q", approvalExecutionTokenBytes)
	formatted := fmt.Sprintf("%v|%+v|%#v|%s|%q", fence, fence, fence, fence, fence)
	if strings.Contains(formatted, capabilityCanary) ||
		strings.Contains(formatted, fixture.attempt.ID()) ||
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
		strings.Contains(logged, fixture.attempt.ID()) ||
		!strings.Contains(logged, approvalMutationFenceRedacted) {
		t.Fatalf("opaque fence structured log was not redacted: %q", logged)
	}
}

func TestApprovalExecutionFencerReconcilesCommitUnknownWithExactToken(t *testing.T) {
	fixture := newApprovalExecutionFenceFixture(t)
	store := newFakeApprovalExecutionFenceStore(fixture.now)
	store.commitThenErr = ErrApprovalExecutionCommitUncertain
	fencer := mustApprovalExecutionFencer(t, store, 0x72, fixture.now)

	fence, err := fencer.ConsumeAndFence(
		t.Context(),
		fixture.plan,
		fixture.approval,
		fixture.report,
		fixture.attempt,
	)
	if err != nil {
		t.Fatalf("ConsumeAndFence lost ACK: %v", err)
	}
	if fence.FenceEpoch() != 1 || store.loadCalls != 1 {
		t.Fatal("authoritative readback did not recover exact committed fence")
	}

	missing := newFakeApprovalExecutionFenceStore(fixture.now)
	missing.compareErr = ErrApprovalExecutionCommitUncertain
	missingFencer := mustApprovalExecutionFencer(t, missing, 0x73, fixture.now)
	if _, err := missingFencer.ConsumeAndFence(
		t.Context(),
		fixture.plan,
		fixture.approval,
		fixture.report,
		fixture.attempt,
	); !errors.Is(err, ErrApprovalExecutionCommitUncertain) {
		t.Fatalf("missing readback error = %v, want %v", err, ErrApprovalExecutionCommitUncertain)
	}
}

func TestApprovalExecutionFencerRejectsReplayAndBindingDrift(t *testing.T) {
	fixture := newApprovalExecutionFenceFixture(t)
	store := newFakeApprovalExecutionFenceStore(fixture.now)
	fencer := mustApprovalExecutionFencer(t, store, 0x74, fixture.now)
	if _, err := fencer.ConsumeAndFence(
		t.Context(),
		fixture.plan,
		fixture.approval,
		fixture.report,
		fixture.attempt,
	); err != nil {
		t.Fatalf("first ConsumeAndFence: %v", err)
	}
	if _, err := fencer.ConsumeAndFence(
		t.Context(),
		fixture.plan,
		fixture.approval,
		fixture.report,
		mustIssueApprovalExecutionAttempt(t, fixture.attemptIssuer, fixture.plan),
	); !errors.Is(err, ErrApprovalExecutionConflict) {
		t.Fatalf("approval replay error = %v, want %v", err, ErrApprovalExecutionConflict)
	}
	if len(store.states) != 1 {
		t.Fatalf("replay left %d durable records, want 1", len(store.states))
	}

	checksBefore := store.compareCalls
	otherInput := validPlanInput()
	otherInput.PlanID = "plan-20260829-0002"
	otherPlan, err := BuildPlan(otherInput)
	if err != nil {
		t.Fatalf("BuildPlan other attempt: %v", err)
	}
	otherAttemptStore := &fakeApprovalExecutionAttemptStore{createdAt: fixture.now}
	otherAttemptIssuer, err := NewApprovalExecutionAttemptIssuer(otherAttemptStore)
	if err != nil {
		t.Fatalf("NewApprovalExecutionAttemptIssuer other attempt: %v", err)
	}
	otherAttempt := mustIssueApprovalExecutionAttempt(t, otherAttemptIssuer, otherPlan)
	tests := map[string]struct {
		approval VerifiedApproval
		report   PreflightReport
		attempt  ApprovalExecutionAttempt
		now      time.Time
		want     error
	}{
		"invalid attempt": {
			approval: fixture.approval,
			report:   fixture.report,
			attempt:  ApprovalExecutionAttempt{},
			now:      fixture.now,
			want:     ErrInvalidApprovalExecutionState,
		},
		"expired report": {
			approval: fixture.approval,
			report:   fixture.report,
			attempt:  fixture.attempt,
			now:      fixture.report.ExpiresAt(),
			want:     ErrApprovalExecutionExpired,
		},
		"attempt plan drift": {
			approval: fixture.approval,
			report:   fixture.report,
			attempt:  otherAttempt,
			now:      fixture.now,
			want:     ErrInvalidApprovalExecutionState,
		},
		"target drift": {
			approval: func() VerifiedApproval {
				value := fixture.approval
				value.targetDigest = "sha256:" + strings.Repeat("e", 64)
				return value
			}(),
			report:  fixture.report,
			attempt: fixture.attempt,
			now:     fixture.now,
			want:    ErrInvalidApprovalExecutionState,
		},
	}
	for name, test := range tests {
		t.Run(name, func(t *testing.T) {
			testFencer := mustApprovalExecutionFencer(t, store, 0x78, test.now)
			if _, err := testFencer.ConsumeAndFence(
				t.Context(),
				fixture.plan,
				test.approval,
				test.report,
				test.attempt,
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
		fixture.attempt,
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
		"admission digest": func(value *ApprovalExecutionFenceRecord) {
			value.AdmissionDigest = "sha256:" + strings.Repeat("e", 64)
		},
		"approval": func(value *ApprovalExecutionFenceRecord) {
			value.ApprovalDigest = "sha256:" + strings.Repeat("e", 64)
		},
		"reference": func(value *ApprovalExecutionFenceRecord) { value.ApprovalReference += "-other" },
		"attempt":   func(value *ApprovalExecutionFenceRecord) { value.ExecutionAttemptID += "-other" },
		"attempt generation": func(value *ApprovalExecutionFenceRecord) {
			value.ExecutionAttemptGeneration++
		},
		"attempt created at": func(value *ApprovalExecutionFenceRecord) {
			value.ExecutionAttemptCreatedAt = value.ExecutionAttemptCreatedAt.Add(time.Second)
		},
		"head": func(value *ApprovalExecutionFenceRecord) {
			value.ExpectedPolicyHead.Revision++
		},
		"epoch": func(value *ApprovalExecutionFenceRecord) { value.FenceEpoch++ },
		"operation": func(value *ApprovalExecutionFenceRecord) {
			value.OperationID = "approval-operation/other"
		},
		"attempt receipt": func(value *ApprovalExecutionFenceRecord) {
			value.ExecutionAttemptReceiptDigest = "sha256:" + strings.Repeat("e", 64)
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

func TestApprovalExecutionFenceAdmissionDigestRejectsSemanticRewrite(t *testing.T) {
	fixture := newApprovalExecutionFenceFixture(t)
	candidate, err := newApprovalExecutionFenceCandidate(
		fixture.plan,
		fixture.approval,
		fixture.report,
		fixture.attempt,
	)
	if err != nil {
		t.Fatalf("newApprovalExecutionFenceCandidate: %v", err)
	}
	tokenDigest := domainSeparatedDigest(
		approvalExecutionTokenDigestDomain,
		bytes.Repeat([]byte{0x7a}, approvalExecutionTokenBytes),
	)
	record, err := sealApprovalExecutionFenceRecord(candidate, 11, fixture.now, tokenDigest)
	if err != nil {
		t.Fatalf("sealApprovalExecutionFenceRecord: %v", err)
	}

	changed := record
	changed.PlanID = "plan-20260829-rewritten"
	changed.AdmissionDigest = approvalExecutionAdmissionDigest(changed)
	changed.RecordDigest = ""
	canonical, err := marshalApprovalExecutionFenceRecordCanonical(changed)
	if err != nil {
		t.Fatalf("marshal rewritten record: %v", err)
	}
	changed.RecordDigest = domainSeparatedDigest(approvalExecutionFenceDigestDomain, canonical)
	if validApprovalExecutionFenceRecord(changed, true) {
		t.Fatal("semantic rewrite with recomputed admission and record digests became valid")
	}
}

func TestApprovalExecutionFencerUsesOwnedClockOnce(t *testing.T) {
	fixture := newApprovalExecutionFenceFixture(t)
	store := newFakeApprovalExecutionFenceStore(fixture.report.ExpiresAt())
	var clockCalls atomic.Uint64
	fencer, err := newApprovalExecutionFencer(
		store,
		func(destination []byte) error {
			for index := range destination {
				destination[index] = 0x79
			}
			return nil
		},
		func() time.Time {
			clockCalls.Add(1)
			return fixture.report.ExpiresAt()
		},
	)
	if err != nil {
		t.Fatalf("newApprovalExecutionFencer: %v", err)
	}
	if _, err := fencer.ConsumeAndFence(
		t.Context(),
		fixture.plan,
		fixture.approval,
		fixture.report,
		fixture.attempt,
	); !errors.Is(err, ErrApprovalExecutionExpired) {
		t.Fatalf("expired owned clock error = %v, want %v", err, ErrApprovalExecutionExpired)
	}
	if clockCalls.Load() != 1 || store.compareCalls != 0 {
		t.Fatalf("owned clock calls=%d store calls=%d", clockCalls.Load(), store.compareCalls)
	}
}

func TestApprovalExecutionFencerAllowsOneConcurrentApprovalConsumption(t *testing.T) {
	fixture := newApprovalExecutionFenceFixture(t)
	store := newFakeApprovalExecutionFenceStore(fixture.now)
	var tokenSequence atomic.Uint64
	fencer, err := newApprovalExecutionFencer(store, func(destination []byte) error {
		sequence := tokenSequence.Add(1)
		for index := range destination {
			destination[index] = byte(sequence + uint64(index))
		}
		return nil
	}, func() time.Time { return fixture.now })
	if err != nil {
		t.Fatalf("newApprovalExecutionFencer: %v", err)
	}

	const contenders = 64
	attempts := make([]ApprovalExecutionAttempt, contenders)
	for index := range attempts {
		attempts[index] = mustIssueApprovalExecutionAttempt(t, fixture.attemptIssuer, fixture.plan)
	}
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
				attempts[index],
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
	fencer := mustApprovalExecutionFencer(t, store, 0x76, fixture.now)
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
		fixture.attempt,
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
	if _, err := newApprovalExecutionFencer(
		newFakeApprovalExecutionFenceStore(time.Now().UTC().Truncate(time.Second)),
		nil,
		func() time.Time { return time.Now().UTC() },
	); !errors.Is(
		err,
		ErrInvalidApprovalExecutionFencer,
	) {
		t.Fatalf("nil token source error = %v, want %v", err, ErrInvalidApprovalExecutionFencer)
	}
	if _, err := newApprovalExecutionFencer(
		newFakeApprovalExecutionFenceStore(time.Now().UTC().Truncate(time.Second)),
		func([]byte) error { return nil },
		nil,
	); !errors.Is(err, ErrInvalidApprovalExecutionFencer) {
		t.Fatalf("nil clock error = %v, want %v", err, ErrInvalidApprovalExecutionFencer)
	}

	fixture := newApprovalExecutionFenceFixture(t)
	zeroStore := newFakeApprovalExecutionFenceStore(fixture.now)
	zeroFencer, err := newApprovalExecutionFencer(
		zeroStore,
		func([]byte) error { return nil },
		func() time.Time { return fixture.now },
	)
	if err != nil {
		t.Fatalf("new zero-token fencer: %v", err)
	}
	if _, err := zeroFencer.ConsumeAndFence(
		t.Context(),
		fixture.plan,
		fixture.approval,
		fixture.report,
		fixture.attempt,
	); !errors.Is(err, ErrApprovalExecutionStoreUnavailable) {
		t.Fatalf("zero token error = %v, want %v", err, ErrApprovalExecutionStoreUnavailable)
	}
	if zeroStore.compareCalls != 0 {
		t.Fatal("zero token reached durable store")
	}

	const canary = "token-source-private-canary"
	failingStore := newFakeApprovalExecutionFenceStore(fixture.now)
	failingFencer, err := newApprovalExecutionFencer(
		failingStore,
		func([]byte) error { return errors.New(canary) },
		func() time.Time { return fixture.now },
	)
	if err != nil {
		t.Fatalf("new failing-token fencer: %v", err)
	}
	_, sourceErr := failingFencer.ConsumeAndFence(
		t.Context(),
		fixture.plan,
		fixture.approval,
		fixture.report,
		fixture.attempt,
	)
	if !errors.Is(sourceErr, ErrApprovalExecutionStoreUnavailable) ||
		strings.Contains(sourceErr.Error(), canary) || failingStore.compareCalls != 0 {
		t.Fatalf("token source error was not fixed and redacted: %v", sourceErr)
	}
}

type approvalExecutionFenceFixture struct {
	approval      VerifiedApproval
	attempt       ApprovalExecutionAttempt
	attemptIssuer ApprovalExecutionAttemptIssuer
	now           time.Time
	plan          Plan
	report        PreflightReport
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
	attemptStore := &fakeApprovalExecutionAttemptStore{createdAt: observedAt}
	attemptIssuer, err := NewApprovalExecutionAttemptIssuer(attemptStore)
	if err != nil {
		t.Fatalf("NewApprovalExecutionAttemptIssuer: %v", err)
	}
	attempt := mustIssueApprovalExecutionAttempt(t, attemptIssuer, plan)
	return approvalExecutionFenceFixture{
		approval:      approval,
		attempt:       attempt,
		attemptIssuer: attemptIssuer,
		now:           observedAt,
		plan:          plan,
		report:        report,
	}
}

func mustIssueApprovalExecutionAttempt(
	t *testing.T,
	issuer ApprovalExecutionAttemptIssuer,
	plan Plan,
) ApprovalExecutionAttempt {
	t.Helper()
	attempt, err := issuer.Issue(t.Context(), plan)
	if err != nil {
		t.Fatalf("Issue approval execution attempt: %v", err)
	}
	return attempt
}

func mustApprovalExecutionFencer(
	t *testing.T,
	store approvalExecutionFenceStore,
	tokenByte byte,
	now time.Time,
) ApprovalExecutionFencer {
	t.Helper()
	fencer, err := newApprovalExecutionFencer(store, func(destination []byte) error {
		for index := range destination {
			destination[index] = tokenByte
		}
		return nil
	}, func() time.Time { return now })
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
	clock         approvalExecutionClock
	nextEpoch     uint64
}

func newFakeApprovalExecutionFenceStore(now time.Time) *fakeApprovalExecutionFenceStore {
	return &fakeApprovalExecutionFenceStore{
		states:        make(map[string]ApprovalExecutionFenceStoredState),
		consumptions:  make(map[string]string),
		activeBySpace: make(map[ApprovalPolicyNamespace]string),
		clock:         func() time.Time { return now },
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
		store.clock().UTC(),
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
