package authoritycutover

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestApprovalExecutionAttemptIssuerReturnsOpaqueDurableBinding(t *testing.T) {
	plan, err := BuildPlan(validPlanInput())
	if err != nil {
		t.Fatalf("BuildPlan: %v", err)
	}
	store := &fakeApprovalExecutionAttemptStore{createdAt: plan.Snapshot().ExpiresAt.Add(-time.Hour)}
	issuer, err := NewApprovalExecutionAttemptIssuer(store)
	if err != nil {
		t.Fatalf("NewApprovalExecutionAttemptIssuer: %v", err)
	}
	attempt, err := issuer.Issue(t.Context(), plan)
	if err != nil {
		t.Fatalf("Issue: %v", err)
	}
	record := attempt.EvidenceRecord()
	target, err := ApprovalPolicyTargetFromPlan(plan)
	if err != nil {
		t.Fatalf("ApprovalPolicyTargetFromPlan: %v", err)
	}
	if attempt.ID() != record.AttemptID ||
		attempt.ReceiptDigest() != record.AttemptReceiptDigest ||
		record.AttemptGeneration != 1 ||
		record.PlanID != plan.Snapshot().PlanID ||
		record.PlanDigest != plan.Digest() ||
		record.TargetDigest != digestApprovalPolicyTarget(target) ||
		!validApprovalExecutionAttemptRecord(record, true) {
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

func TestApprovalExecutionAttemptIssuerRejectsForgedAndCorruptState(t *testing.T) {
	plan, err := BuildPlan(validPlanInput())
	if err != nil {
		t.Fatalf("BuildPlan: %v", err)
	}
	if (ApprovalExecutionAttempt{}).ID() != "" ||
		validApprovalExecutionAttemptRecord((ApprovalExecutionAttempt{}).record, true) {
		t.Fatal("zero attempt became valid")
	}

	store := &fakeApprovalExecutionAttemptStore{
		createdAt: plan.Snapshot().ExpiresAt.Add(-time.Hour),
		mutate: func(record *ApprovalExecutionAttemptRecord) {
			record.PlanDigest = "sha256:" + strings.Repeat("e", 64)
		},
	}
	issuer, err := NewApprovalExecutionAttemptIssuer(store)
	if err != nil {
		t.Fatalf("NewApprovalExecutionAttemptIssuer: %v", err)
	}
	if _, err := issuer.Issue(t.Context(), plan); !errors.Is(
		err,
		ErrInvalidApprovalExecutionAttempt,
	) {
		t.Fatalf("corrupt state error = %v, want %v", err, ErrInvalidApprovalExecutionAttempt)
	}

	var nilStore *fakeApprovalExecutionAttemptStore
	if _, err := NewApprovalExecutionAttemptIssuer(nilStore); !errors.Is(
		err,
		ErrInvalidApprovalExecutionAttemptIssuer,
	) {
		t.Fatalf("typed nil error = %v", err)
	}
	if _, err := (ApprovalExecutionAttemptIssuer{}).Issue(t.Context(), plan); !errors.Is(
		err,
		ErrInvalidApprovalExecutionAttemptIssuer,
	) {
		t.Fatalf("zero issuer error = %v", err)
	}
}

func TestApprovalExecutionAttemptIssuerMapsStoreErrors(t *testing.T) {
	plan, err := BuildPlan(validPlanInput())
	if err != nil {
		t.Fatalf("BuildPlan: %v", err)
	}
	const canary = "private-attempt-store-error"
	store := &fakeApprovalExecutionAttemptStore{
		createdAt: plan.Snapshot().ExpiresAt.Add(-time.Hour),
		err:       errors.New(canary),
	}
	issuer, err := NewApprovalExecutionAttemptIssuer(store)
	if err != nil {
		t.Fatalf("NewApprovalExecutionAttemptIssuer: %v", err)
	}
	_, issueErr := issuer.Issue(t.Context(), plan)
	if !errors.Is(issueErr, ErrApprovalExecutionAttemptUnavailable) ||
		strings.Contains(issueErr.Error(), canary) {
		t.Fatalf("store error was not fixed and redacted: %v", issueErr)
	}
}

type fakeApprovalExecutionAttemptStore struct {
	mu        sync.Mutex
	createdAt time.Time
	err       error
	mutate    func(*ApprovalExecutionAttemptRecord)
	next      uint64
}

func (store *fakeApprovalExecutionAttemptStore) Issue(
	_ context.Context,
	candidate approvalExecutionAttemptCandidate,
) (ApprovalExecutionAttemptStoredState, error) {
	store.mu.Lock()
	defer store.mu.Unlock()
	if store.err != nil {
		return ApprovalExecutionAttemptStoredState{}, store.err
	}
	store.next++
	record, err := sealApprovalExecutionAttemptRecord(ApprovalExecutionAttemptRecord{
		AttemptGeneration: store.next,
		AttemptID:         fmt.Sprintf("execution-attempt/%016x", store.next),
		CreatedAt:         store.createdAt,
		Format:            ApprovalExecutionAttemptRecordFormat,
		PlanDigest:        candidate.PlanDigest,
		PlanID:            candidate.PlanID,
		TargetDigest:      candidate.TargetDigest,
	})
	if err != nil {
		return ApprovalExecutionAttemptStoredState{}, err
	}
	if store.mutate != nil {
		store.mutate(&record)
	}
	return ApprovalExecutionAttemptStoredState{Record: record}, nil
}
