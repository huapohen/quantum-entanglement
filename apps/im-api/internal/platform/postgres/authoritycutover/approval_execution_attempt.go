package authoritycutover

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"strings"
	"time"
)

const (
	ApprovalExecutionAttemptRecordFormat = "wanwork.im.postgres-authority-approval-execution-attempt/1"
	approvalExecutionAttemptDigestDomain = "wanwork.im/postgres-authority-approval-execution-attempt/1\n"
	approvalExecutionAttemptRedacted     = "ApprovalExecutionAttempt{opaque}"
)

var (
	ErrInvalidApprovalExecutionAttemptIssuer = errors.New("invalid PostgreSQL authority approval execution attempt issuer")
	ErrInvalidApprovalExecutionAttempt       = errors.New("invalid PostgreSQL authority approval execution attempt")
	ErrApprovalExecutionAttemptUnavailable   = errors.New("PostgreSQL authority approval execution attempt store is unavailable")
)

// ApprovalExecutionAttemptRecord is the immutable durable receipt returned by the package-owned
// attempt store. It binds a store-allocated identity and monotonic generation to one exact plan
// and physical approval-policy target. AttemptReceiptDigest covers every other field.
type ApprovalExecutionAttemptRecord struct {
	AttemptGeneration    uint64    `json:"attemptGeneration"`
	AttemptID            string    `json:"attemptId"`
	AttemptReceiptDigest string    `json:"attemptReceiptDigest"`
	CreatedAt            time.Time `json:"createdAt"`
	Format               string    `json:"format"`
	PlanDigest           string    `json:"planDigest"`
	PlanID               string    `json:"planId"`
	TargetDigest         string    `json:"targetDigest"`
}

type approvalExecutionAttemptCandidate struct {
	PlanDigest   string
	PlanID       string
	TargetDigest string
}

// ApprovalExecutionAttemptStoredState must be one authoritative durable store snapshot.
type ApprovalExecutionAttemptStoredState struct {
	Record ApprovalExecutionAttemptRecord
}

// approvalExecutionAttemptStore is intentionally package-owned: application callers cannot
// implement an identity factory or construct attempts from arbitrary execution-attempt strings.
type approvalExecutionAttemptStore interface {
	Issue(context.Context, approvalExecutionAttemptCandidate) (ApprovalExecutionAttemptStoredState, error)
}

type ApprovalExecutionAttemptIssuer struct {
	store approvalExecutionAttemptStore
}

func NewApprovalExecutionAttemptIssuer(
	store approvalExecutionAttemptStore,
) (ApprovalExecutionAttemptIssuer, error) {
	if nilInterface(store) {
		return ApprovalExecutionAttemptIssuer{}, ErrInvalidApprovalExecutionAttemptIssuer
	}
	return ApprovalExecutionAttemptIssuer{store: store}, nil
}

// ApprovalExecutionAttempt is an opaque, package-issued reference to a durable attempt receipt.
// There is deliberately no constructor accepting an ID, digest, generation, or timestamp.
type ApprovalExecutionAttempt struct {
	record ApprovalExecutionAttemptRecord
}

func (ApprovalExecutionAttempt) String() string   { return approvalExecutionAttemptRedacted }
func (ApprovalExecutionAttempt) GoString() string { return approvalExecutionAttemptRedacted }
func (ApprovalExecutionAttempt) LogValue() slog.Value {
	return slog.StringValue(approvalExecutionAttemptRedacted)
}
func (ApprovalExecutionAttempt) MarshalJSON() ([]byte, error) { return []byte("{}"), nil }

func (attempt ApprovalExecutionAttempt) EvidenceRecord() ApprovalExecutionAttemptRecord {
	return attempt.record
}
func (attempt ApprovalExecutionAttempt) ID() string { return attempt.record.AttemptID }
func (attempt ApprovalExecutionAttempt) ReceiptDigest() string {
	return attempt.record.AttemptReceiptDigest
}

func (issuer ApprovalExecutionAttemptIssuer) Issue(
	ctx context.Context,
	plan Plan,
) (ApprovalExecutionAttempt, error) {
	if ctx == nil || nilInterface(issuer.store) || !validPlanSnapshot(plan.snapshot, true) {
		return ApprovalExecutionAttempt{}, ErrInvalidApprovalExecutionAttemptIssuer
	}
	target, err := ApprovalPolicyTargetFromPlan(plan)
	if err != nil {
		return ApprovalExecutionAttempt{}, ErrInvalidApprovalExecutionAttempt
	}
	candidate := approvalExecutionAttemptCandidate{
		PlanDigest:   plan.Digest(),
		PlanID:       plan.Snapshot().PlanID,
		TargetDigest: digestApprovalPolicyTarget(target),
	}
	if !validApprovalExecutionAttemptCandidate(candidate) {
		return ApprovalExecutionAttempt{}, ErrInvalidApprovalExecutionAttempt
	}
	state, err := issuer.store.Issue(ctx, candidate)
	if err != nil {
		return ApprovalExecutionAttempt{}, ErrApprovalExecutionAttemptUnavailable
	}
	if !validApprovalExecutionAttemptRecord(state.Record, true) ||
		state.Record.PlanID != candidate.PlanID ||
		state.Record.PlanDigest != candidate.PlanDigest ||
		state.Record.TargetDigest != candidate.TargetDigest ||
		!state.Record.CreatedAt.Before(plan.Snapshot().ExpiresAt) {
		return ApprovalExecutionAttempt{}, ErrInvalidApprovalExecutionAttempt
	}
	return ApprovalExecutionAttempt{record: state.Record}, nil
}

func sealApprovalExecutionAttemptRecord(
	record ApprovalExecutionAttemptRecord,
) (ApprovalExecutionAttemptRecord, error) {
	if !validApprovalExecutionAttemptRecord(record, false) {
		return ApprovalExecutionAttemptRecord{}, ErrInvalidApprovalExecutionAttempt
	}
	canonical, err := marshalApprovalExecutionAttemptRecordCanonical(record)
	if err != nil {
		return ApprovalExecutionAttemptRecord{}, ErrInvalidApprovalExecutionAttempt
	}
	record.AttemptReceiptDigest = domainSeparatedDigest(
		approvalExecutionAttemptDigestDomain,
		canonical,
	)
	if !validApprovalExecutionAttemptRecord(record, true) {
		return ApprovalExecutionAttemptRecord{}, ErrInvalidApprovalExecutionAttempt
	}
	return record, nil
}

func validApprovalExecutionAttemptCandidate(candidate approvalExecutionAttemptCandidate) bool {
	return canonicalIdentity(candidate.PlanID) &&
		canonicalDigest.MatchString(candidate.PlanDigest) &&
		canonicalDigest.MatchString(candidate.TargetDigest)
}

func validApprovalExecutionAttemptRecord(
	record ApprovalExecutionAttemptRecord,
	requireDigest bool,
) bool {
	if record.Format != ApprovalExecutionAttemptRecordFormat ||
		record.AttemptGeneration == 0 ||
		!canonicalIdentity(record.AttemptID) ||
		!strings.HasPrefix(record.AttemptID, "execution-attempt/") ||
		!canonicalPreflightTime(record.CreatedAt) ||
		!canonicalIdentity(record.PlanID) ||
		!canonicalDigest.MatchString(record.PlanDigest) ||
		!canonicalDigest.MatchString(record.TargetDigest) {
		return false
	}
	if !requireDigest {
		return record.AttemptReceiptDigest == ""
	}
	if !canonicalDigest.MatchString(record.AttemptReceiptDigest) {
		return false
	}
	unsigned := record
	unsigned.AttemptReceiptDigest = ""
	canonical, err := marshalApprovalExecutionAttemptRecordCanonical(unsigned)
	return err == nil && domainSeparatedDigest(
		approvalExecutionAttemptDigestDomain,
		canonical,
	) == record.AttemptReceiptDigest
}

func marshalApprovalExecutionAttemptRecordCanonical(
	record ApprovalExecutionAttemptRecord,
) ([]byte, error) {
	var output bytes.Buffer
	encoder := json.NewEncoder(&output)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(record); err != nil {
		return nil, err
	}
	return bytes.TrimSuffix(output.Bytes(), []byte("\n")), nil
}
