package authoritycutover

import (
	"context"
	"crypto/ed25519"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func TestApprovalExecutionAttemptIssuerReturnsOpaqueDurableFullVectorBinding(t *testing.T) {
	fixture := newApprovalExecutionAttemptFixture(t)
	store := newFakeApprovalExecutionAttemptStore(fixture.observedAt)
	issuer := mustApprovalExecutionAttemptIssuer(t, store, fixture.observedAt)
	attempt, err := issuer.Issue(t.Context(), fixture.plan, fixture.approval, fixture.report)
	if err != nil {
		t.Fatalf("Issue: %v", err)
	}
	record := attempt.EvidenceRecord()
	if attempt.ID() != record.AttemptID || attempt.IssuanceID() != record.AttemptIssuanceID ||
		attempt.ReceiptDigest() != record.AttemptReceiptDigest || record.AttemptGeneration != 1 ||
		record.ApprovalDigest != fixture.approval.ApprovalDigest() ||
		record.ApprovalKeyFingerprint != fixture.approval.KeyFingerprint() ||
		record.ApprovalKeyGeneration != fixture.approval.KeyGeneration() ||
		record.ApprovalKeyID != fixture.approval.KeyID() ||
		record.ApprovalReference != fixture.approval.Reference() ||
		record.ApproverIdentity != fixture.approval.ApproverIdentity() ||
		record.ExpectedPolicyHead != (ApprovalPolicyHead{
			ActivationRecordDigest: fixture.approval.ActivationRecordDigest(),
			PolicyDigest:           fixture.approval.PolicyDigest(),
			PolicyID:               fixture.approval.PolicyID(),
			Revision:               fixture.approval.PolicySequence(),
			TargetDigest:           fixture.approval.PolicyTargetDigest(),
		}) || record.PlanID != fixture.plan.Snapshot().PlanID ||
		record.PlanDigest != fixture.plan.Digest() ||
		record.PreflightReportDigest != fixture.report.Digest() ||
		record.PreflightObservedAt != fixture.report.ObservedAt() ||
		record.PreflightExpiresAt != fixture.report.ExpiresAt() ||
		record.TargetDigest != fixture.approval.PolicyTargetDigest() ||
		record.MutationNotAfter != fixture.report.ExpiresAt() ||
		!validApprovalExecutionAttemptRecord(record, true) ||
		store.compareCalls != 1 || store.loadCalls != 1 {
		t.Fatalf("attempt binding is incomplete: %+v", record)
	}
	encoded, err := json.Marshal(attempt)
	if err != nil || string(encoded) != "{}" {
		t.Fatalf("opaque attempt JSON = %s, %v", encoded, err)
	}
	formatted := fmt.Sprintf("%v|%+v|%#v|%q", attempt, attempt, attempt, attempt)
	if strings.Contains(formatted, record.AttemptID) ||
		strings.Contains(formatted, record.AttemptReceiptDigest) ||
		formatted != "ApprovalExecutionAttempt{opaque}|ApprovalExecutionAttempt{opaque}|"+
			`ApprovalExecutionAttempt{opaque}|"ApprovalExecutionAttempt{opaque}"` {
		t.Fatalf("opaque attempt formatting = %q", formatted)
	}
}

func TestApprovalExecutionAttemptIssuerIsExactlyIdempotentUnderConcurrency(t *testing.T) {
	fixture := newApprovalExecutionAttemptFixture(t)
	store := newFakeApprovalExecutionAttemptStore(fixture.observedAt)
	issuer := mustApprovalExecutionAttemptIssuer(t, store, fixture.observedAt)

	const contenders = 64
	attempts := make(chan ApprovalExecutionAttempt, contenders)
	errorsByContender := make(chan error, contenders)
	var wait sync.WaitGroup
	for range contenders {
		wait.Add(1)
		go func() {
			defer wait.Done()
			attempt, err := issuer.Issue(t.Context(), fixture.plan, fixture.approval, fixture.report)
			attempts <- attempt
			errorsByContender <- err
		}()
	}
	wait.Wait()
	close(attempts)
	close(errorsByContender)
	for err := range errorsByContender {
		if err != nil {
			t.Fatalf("concurrent Issue: %v", err)
		}
	}
	var expected ApprovalExecutionAttemptRecord
	for attempt := range attempts {
		if expected.AttemptID == "" {
			expected = attempt.EvidenceRecord()
			continue
		}
		if attempt.EvidenceRecord() != expected {
			t.Fatalf("idempotent issuance diverged: got %+v want %+v", attempt.EvidenceRecord(), expected)
		}
	}
	if store.next != 1 || len(store.states) != 1 || store.compareCalls != contenders ||
		store.loadCalls != contenders {
		t.Fatalf("durable allocations=%d states=%d compare=%d load=%d", store.next,
			len(store.states), store.compareCalls, store.loadCalls)
	}
}

func TestApprovalExecutionAttemptIssuerReconcilesCommitUnknownAndDelayedVisibility(t *testing.T) {
	fixture := newApprovalExecutionAttemptFixture(t)
	store := newFakeApprovalExecutionAttemptStore(fixture.observedAt)
	store.commitThenErr = ErrApprovalExecutionAttemptCommitUncertain
	store.loadNotFoundCount = 2
	issuer := mustApprovalExecutionAttemptIssuer(t, store, fixture.observedAt)
	attempt, err := issuer.Issue(t.Context(), fixture.plan, fixture.approval, fixture.report)
	if err != nil {
		t.Fatalf("Issue after lost ACK: %v", err)
	}
	if attempt.ID() == "" || store.loadCalls != 3 || store.next != 1 {
		t.Fatalf("readback did not recover committed attempt: %+v", store)
	}

	missing := newFakeApprovalExecutionAttemptStore(fixture.observedAt)
	missing.compareErr = ErrApprovalExecutionAttemptCommitUncertain
	missingIssuer, err := newApprovalExecutionAttemptIssuerWithReconcilePolicy(
		missing,
		func() time.Time { return fixture.observedAt },
		approvalExecutionAttemptReconcilePolicy{
			maximumDelay: 2 * time.Millisecond,
			minimumDelay: time.Millisecond,
			timeout:      5 * time.Millisecond,
		},
	)
	if err != nil {
		t.Fatalf("new missing issuer: %v", err)
	}
	if _, err := missingIssuer.Issue(
		t.Context(), fixture.plan, fixture.approval, fixture.report,
	); !errors.Is(err, ErrApprovalExecutionAttemptCommitUncertain) {
		t.Fatalf("missing readback error = %v", err)
	}
}

func TestApprovalExecutionAttemptIssuerUsesFreshReadbackAfterCallerCancellation(t *testing.T) {
	fixture := newApprovalExecutionAttemptFixture(t)
	store := &cancellationApprovalExecutionAttemptStore{}
	issuer, err := newApprovalExecutionAttemptIssuerWithReconcilePolicy(
		store,
		func() time.Time { return fixture.observedAt },
		approvalExecutionAttemptReconcilePolicy{
			maximumDelay: 2 * time.Millisecond,
			minimumDelay: time.Millisecond,
			timeout:      5 * time.Millisecond,
		},
	)
	if err != nil {
		t.Fatalf("new cancellation issuer: %v", err)
	}
	ctx, cancel := context.WithCancel(context.WithValue(
		t.Context(), approvalExecutionAttemptContextKey{}, "retained",
	))
	cancel()
	if _, err := issuer.Issue(ctx, fixture.plan, fixture.approval, fixture.report); !errors.Is(
		err, ErrApprovalExecutionAttemptCommitUncertain,
	) {
		t.Fatalf("canceled issue error = %v", err)
	}
	if store.loadContextCanceled || store.loadContextValue != "retained" || store.loadCalls < 2 {
		t.Fatalf("fresh readback canceled=%t value=%q calls=%d", store.loadContextCanceled,
			store.loadContextValue, store.loadCalls)
	}
}

func TestApprovalExecutionAttemptIssuerRejectsInvalidAdmissionBeforeStore(t *testing.T) {
	fixture := newApprovalExecutionAttemptFixture(t)
	tests := map[string]struct {
		approval VerifiedApproval
		report   PreflightReport
		now      time.Time
		want     error
	}{
		"zero approval": {
			approval: VerifiedApproval{}, report: fixture.report, now: fixture.observedAt,
			want: ErrInvalidApprovalExecutionAttempt,
		},
		"zero report": {
			approval: fixture.approval, report: PreflightReport{}, now: fixture.observedAt,
			want: ErrInvalidApprovalExecutionAttempt,
		},
		"expired report": {
			approval: fixture.approval, report: fixture.report, now: fixture.report.ExpiresAt(),
			want: ErrApprovalExecutionAttemptExpired,
		},
		"approval drift": {
			approval: func() VerifiedApproval {
				changed := fixture.approval
				changed.reference += "-drift"
				return changed
			}(),
			report: fixture.report, now: fixture.observedAt,
			want: ErrInvalidApprovalExecutionAttempt,
		},
		"report digest drift": {
			approval: fixture.approval,
			report: func() PreflightReport {
				changed := fixture.report
				changed.digest = "sha256:" + strings.Repeat("e", 64)
				return changed
			}(),
			now:  fixture.observedAt,
			want: ErrInvalidApprovalExecutionAttempt,
		},
	}
	for name, test := range tests {
		t.Run(name, func(t *testing.T) {
			store := newFakeApprovalExecutionAttemptStore(fixture.observedAt)
			issuer := mustApprovalExecutionAttemptIssuer(t, store, test.now)
			if _, err := issuer.Issue(
				t.Context(), fixture.plan, test.approval, test.report,
			); !errors.Is(err, test.want) {
				t.Fatalf("Issue error = %v, want %v", err, test.want)
			}
			if store.compareCalls != 0 || store.loadCalls != 0 {
				t.Fatalf("invalid admission reached store: compare=%d load=%d",
					store.compareCalls, store.loadCalls)
			}
		})
	}
}

func TestApprovalExecutionAttemptRecordRejectsEveryDurableBoundaryDrift(t *testing.T) {
	fixture := newApprovalExecutionAttemptFixture(t)
	candidate, err := newApprovalExecutionAttemptCandidate(
		fixture.plan, fixture.approval, fixture.report,
	)
	if err != nil {
		t.Fatalf("new candidate: %v", err)
	}
	record, err := sealApprovalExecutionAttemptRecord(
		candidate, 7, "execution-attempt/0000000000000007", fixture.observedAt,
	)
	if err != nil {
		t.Fatalf("seal record: %v", err)
	}
	mutations := map[string]func(*ApprovalExecutionAttemptRecord){
		"approval digest": func(value *ApprovalExecutionAttemptRecord) {
			value.ApprovalDigest = "sha256:" + strings.Repeat("e", 64)
		},
		"approval expiry": func(value *ApprovalExecutionAttemptRecord) {
			value.ApprovalExpiresAt = value.ApprovalExpiresAt.Add(-time.Second)
		},
		"key fingerprint": func(value *ApprovalExecutionAttemptRecord) {
			value.ApprovalKeyFingerprint = "sha256:" + strings.Repeat("e", 64)
		},
		"policy head": func(value *ApprovalExecutionAttemptRecord) {
			value.ExpectedPolicyHead.Revision++
		},
		"issuance": func(value *ApprovalExecutionAttemptRecord) {
			value.AttemptIssuanceID += "-drift"
		},
		"generation": func(value *ApprovalExecutionAttemptRecord) { value.AttemptGeneration++ },
		"id":         func(value *ApprovalExecutionAttemptRecord) { value.AttemptID += "-drift" },
		"created": func(value *ApprovalExecutionAttemptRecord) {
			value.CreatedAt = value.CreatedAt.Add(time.Second)
		},
		"plan": func(value *ApprovalExecutionAttemptRecord) { value.PlanID += "-drift" },
		"preflight": func(value *ApprovalExecutionAttemptRecord) {
			value.PreflightReportDigest = "sha256:" + strings.Repeat("e", 64)
		},
		"target": func(value *ApprovalExecutionAttemptRecord) {
			value.TargetDigest = "sha256:" + strings.Repeat("e", 64)
		},
		"receipt": func(value *ApprovalExecutionAttemptRecord) {
			value.AttemptReceiptDigest = "sha256:" + strings.Repeat("e", 64)
		},
	}
	for name, mutate := range mutations {
		t.Run(name, func(t *testing.T) {
			changed := record
			mutate(&changed)
			if validApprovalExecutionAttemptRecord(changed, true) {
				t.Fatal("drifted durable record remained valid")
			}
		})
	}

	if _, err := sealApprovalExecutionAttemptRecord(
		candidate, 8, "execution-attempt/0000000000000008",
		fixture.observedAt.Add(-time.Nanosecond),
	); !errors.Is(err, ErrInvalidApprovalExecutionAttempt) {
		t.Fatalf("pre-observation CreatedAt error = %v", err)
	}
	if _, err := sealApprovalExecutionAttemptRecord(
		candidate, 8, "execution-attempt/0000000000000008", record.MutationNotAfter,
	); !errors.Is(err, ErrInvalidApprovalExecutionAttempt) {
		t.Fatalf("exclusive expiry CreatedAt error = %v", err)
	}
}

func TestApprovalExecutionAttemptRecordStrictCanonicalDecode(t *testing.T) {
	fixture := newApprovalExecutionAttemptFixture(t)
	candidate, err := newApprovalExecutionAttemptCandidate(
		fixture.plan, fixture.approval, fixture.report,
	)
	if err != nil {
		t.Fatalf("new candidate: %v", err)
	}
	record, err := sealApprovalExecutionAttemptRecord(
		candidate, 9, "execution-attempt/0000000000000009", fixture.observedAt,
	)
	if err != nil {
		t.Fatalf("seal record: %v", err)
	}
	canonical, err := marshalApprovalExecutionAttemptRecordCanonical(record)
	if err != nil {
		t.Fatalf("marshal canonical: %v", err)
	}
	decoded, err := decodeApprovalExecutionAttemptRecord(canonical)
	if err != nil || decoded != record {
		t.Fatalf("canonical decode mismatch: %v", err)
	}
	unknown := append([]byte(nil), canonical...)
	unknown = append(unknown[:len(unknown)-1], []byte(`,"unknown":true}`)...)
	for name, raw := range map[string][]byte{
		"empty":         nil,
		"trailing byte": append(append([]byte(nil), canonical...), '\n'),
		"unknown field": unknown,
		"oversized":     make([]byte, maximumApprovalExecutionAttemptBytes+1),
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := decodeApprovalExecutionAttemptRecord(raw); !errors.Is(
				err, ErrInvalidApprovalExecutionAttempt,
			) {
				t.Fatalf("decode error = %v", err)
			}
		})
	}
}

func TestApprovalExecutionAttemptIssuerMapsStoreErrorsAndRejectsDependencies(t *testing.T) {
	fixture := newApprovalExecutionAttemptFixture(t)
	const canary = "private-attempt-store-error"
	store := newFakeApprovalExecutionAttemptStore(fixture.observedAt)
	store.compareErr = errors.New(canary)
	issuer := mustApprovalExecutionAttemptIssuer(t, store, fixture.observedAt)
	_, issueErr := issuer.Issue(t.Context(), fixture.plan, fixture.approval, fixture.report)
	if !errors.Is(issueErr, ErrApprovalExecutionAttemptUnavailable) ||
		strings.Contains(issueErr.Error(), canary) {
		t.Fatalf("store error was not fixed and redacted: %v", issueErr)
	}

	var nilStore *fakeApprovalExecutionAttemptStore
	if _, err := NewApprovalExecutionAttemptIssuer(nilStore); !errors.Is(
		err, ErrInvalidApprovalExecutionAttemptIssuer,
	) {
		t.Fatalf("typed nil error = %v", err)
	}
	if _, err := (ApprovalExecutionAttemptIssuer{}).Issue(
		t.Context(), fixture.plan, fixture.approval, fixture.report,
	); !errors.Is(err, ErrInvalidApprovalExecutionAttemptIssuer) {
		t.Fatalf("zero issuer error = %v", err)
	}
	if (ApprovalExecutionAttempt{}).ID() != "" ||
		validApprovalExecutionAttemptRecord((ApprovalExecutionAttempt{}).record, true) {
		t.Fatal("zero attempt became valid")
	}
}

func TestApprovalExecutionAttemptIssuerUsesOwnedUTCClockOnce(t *testing.T) {
	fixture := newApprovalExecutionAttemptFixture(t)
	store := newFakeApprovalExecutionAttemptStore(fixture.observedAt)
	var calls atomic.Uint64
	issuer, err := newApprovalExecutionAttemptIssuer(store, func() time.Time {
		calls.Add(1)
		return fixture.observedAt.In(time.FixedZone("fixture", 8*60*60))
	})
	if err != nil {
		t.Fatalf("new issuer: %v", err)
	}
	attempt, err := issuer.Issue(t.Context(), fixture.plan, fixture.approval, fixture.report)
	if err != nil {
		t.Fatalf("Issue: %v", err)
	}
	if calls.Load() != 1 || attempt.EvidenceRecord().CreatedAt.Location() != time.UTC {
		t.Fatalf("clock calls=%d createdAt=%v", calls.Load(), attempt.EvidenceRecord().CreatedAt)
	}
}

type approvalExecutionAttemptFixture struct {
	approval   VerifiedApproval
	observedAt time.Time
	plan       Plan
	report     PreflightReport
}

func newApprovalExecutionAttemptFixture(t *testing.T) approvalExecutionAttemptFixture {
	t.Helper()
	policyFixture := newApprovalPolicyFixture(t)
	activationStore := newFakeApprovalPolicyActivationStore()
	activator := mustApprovalPolicyActivator(t, policyFixture.verifier, activationStore)
	activated, err := activator.Activate(
		t.Context(), policyFixture.raw, policyFixture.input.NotBefore.Add(30*time.Minute),
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
		plan, "release-key-2026-08", approvedAt, approvedAt.Add(10*time.Minute),
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
		plan, approval, observedAt, passingPreflightObservations(),
	)
	if err != nil {
		t.Fatalf("buildPreflightReport: %v", err)
	}
	return approvalExecutionAttemptFixture{
		approval: approval, observedAt: observedAt, plan: plan, report: report,
	}
}

func mustApprovalExecutionAttemptIssuer(
	t *testing.T,
	store approvalExecutionAttemptStore,
	now time.Time,
) ApprovalExecutionAttemptIssuer {
	t.Helper()
	issuer, err := newApprovalExecutionAttemptIssuer(store, func() time.Time { return now })
	if err != nil {
		t.Fatalf("newApprovalExecutionAttemptIssuer: %v", err)
	}
	return issuer
}

type fakeApprovalExecutionAttemptStore struct {
	mu                sync.Mutex
	states            map[string]ApprovalExecutionAttemptStoredState
	createdAt         time.Time
	compareErr        error
	commitThenErr     error
	loadErr           error
	loadNotFoundCount int
	mutate            func(*ApprovalExecutionAttemptRecord)
	next              uint64
	compareCalls      int
	loadCalls         int
}

func newFakeApprovalExecutionAttemptStore(createdAt time.Time) *fakeApprovalExecutionAttemptStore {
	return &fakeApprovalExecutionAttemptStore{
		createdAt: createdAt,
		states:    make(map[string]ApprovalExecutionAttemptStoredState),
	}
}

func (store *fakeApprovalExecutionAttemptStore) Load(
	_ context.Context,
	namespace ApprovalPolicyNamespace,
	issuanceID string,
) (ApprovalExecutionAttemptStoredState, error) {
	store.mu.Lock()
	defer store.mu.Unlock()
	store.loadCalls++
	if store.loadErr != nil {
		return ApprovalExecutionAttemptStoredState{}, store.loadErr
	}
	if store.loadNotFoundCount > 0 {
		store.loadNotFoundCount--
		return ApprovalExecutionAttemptStoredState{}, ErrApprovalExecutionAttemptNotFound
	}
	state, exists := store.states[fakeApprovalExecutionAttemptKey(namespace, issuanceID)]
	if !exists {
		return ApprovalExecutionAttemptStoredState{}, ErrApprovalExecutionAttemptNotFound
	}
	return state, nil
}

func (store *fakeApprovalExecutionAttemptStore) CompareAndIssue(
	_ context.Context,
	namespace ApprovalPolicyNamespace,
	candidate approvalExecutionAttemptCandidate,
) error {
	store.mu.Lock()
	defer store.mu.Unlock()
	store.compareCalls++
	if store.compareErr != nil {
		return store.compareErr
	}
	if !validApprovalExecutionAttemptCandidate(candidate) ||
		namespace.PolicyID != candidate.record.ExpectedPolicyHead.PolicyID ||
		namespace.TargetDigest != candidate.record.ExpectedPolicyHead.TargetDigest {
		return ErrInvalidApprovalExecutionAttempt
	}
	key := fakeApprovalExecutionAttemptKey(namespace, candidate.record.AttemptIssuanceID)
	if _, exists := store.states[key]; exists {
		return nil
	}
	if !store.createdAt.Before(candidate.record.MutationNotAfter) {
		return ErrApprovalExecutionAttemptExpired
	}
	store.next++
	record, err := sealApprovalExecutionAttemptRecord(
		candidate,
		store.next,
		fmt.Sprintf("execution-attempt/%016x", store.next),
		store.createdAt.UTC(),
	)
	if err != nil {
		return err
	}
	if store.mutate != nil {
		store.mutate(&record)
	}
	store.states[key] = ApprovalExecutionAttemptStoredState{Record: record}
	return store.commitThenErr
}

func fakeApprovalExecutionAttemptKey(
	namespace ApprovalPolicyNamespace,
	issuanceID string,
) string {
	return namespace.PolicyID + "\x00" + namespace.TargetDigest + "\x00" + issuanceID
}

type approvalExecutionAttemptContextKey struct{}

type cancellationApprovalExecutionAttemptStore struct {
	loadCalls           int
	loadContextCanceled bool
	loadContextValue    string
}

func (store *cancellationApprovalExecutionAttemptStore) Load(
	ctx context.Context,
	_ ApprovalPolicyNamespace,
	_ string,
) (ApprovalExecutionAttemptStoredState, error) {
	store.loadCalls++
	store.loadContextCanceled = ctx.Err() != nil
	store.loadContextValue, _ = ctx.Value(approvalExecutionAttemptContextKey{}).(string)
	return ApprovalExecutionAttemptStoredState{}, ErrApprovalExecutionAttemptNotFound
}

func (*cancellationApprovalExecutionAttemptStore) CompareAndIssue(
	ctx context.Context,
	_ ApprovalPolicyNamespace,
	_ approvalExecutionAttemptCandidate,
) error {
	if ctx.Err() == nil {
		return ErrApprovalExecutionAttemptUnavailable
	}
	return ErrApprovalExecutionAttemptCommitUncertain
}
