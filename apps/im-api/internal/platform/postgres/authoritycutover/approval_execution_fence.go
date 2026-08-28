package authoritycutover

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/json"
	"errors"
	"slices"
	"strings"
	"time"
)

const (
	ApprovalExecutionFenceRecordFormat = "wanwork.im.postgres-authority-approval-execution-fence/1"
	approvalConsumptionIDDomain        = "wanwork.im/postgres-authority-approval-consumption/1\n"
	approvalExecutionOperationIDDomain = "wanwork.im/postgres-authority-approval-operation/1\n"
	approvalExecutionFenceDigestDomain = "wanwork.im/postgres-authority-approval-execution-fence/1\n"
	approvalExecutionTokenDigestDomain = "wanwork.im/postgres-authority-approval-execution-token/1\n"
	approvalExecutionTokenBytes        = 32
	approvalExecutionReconcileTimeout  = 5 * time.Second
)

var (
	ErrInvalidApprovalExecutionFencer    = errors.New("invalid PostgreSQL authority approval execution fencer")
	ErrInvalidApprovalExecutionState     = errors.New("invalid PostgreSQL authority approval execution fence state")
	ErrApprovalExecutionFenceNotFound    = errors.New("PostgreSQL authority approval execution fence not found")
	ErrApprovalExecutionConflict         = errors.New("PostgreSQL authority approval execution fence conflict")
	ErrApprovalExecutionExpired          = errors.New("PostgreSQL authority approval execution admission expired")
	ErrApprovalExecutionCommitUncertain  = errors.New("PostgreSQL authority approval execution fence commit is uncertain")
	ErrApprovalExecutionStoreUnavailable = errors.New("PostgreSQL authority approval execution fence store is unavailable")
)

// ApprovalExecutionFenceRecord is immutable public admission evidence. TokenDigest is a one-way,
// domain-separated digest of the capability token; the token itself never enters durable state,
// JSON, logs, errors, receipts, or reports. Opening a fence does not prove that a target mutation
// started or completed.
type ApprovalExecutionFenceRecord struct {
	ApprovalDigest                 string             `json:"approvalDigest"`
	ApprovalExpiresAt              time.Time          `json:"approvalExpiresAt"`
	ApprovalKeyFingerprint         string             `json:"approvalKeyFingerprint"`
	ApprovalKeyGeneration          string             `json:"approvalKeyGeneration"`
	ApprovalKeyID                  string             `json:"approvalKeyId"`
	ApprovalPolicyActivationDigest string             `json:"approvalPolicyActivationDigest"`
	ApprovalPolicyDigest           string             `json:"approvalPolicyDigest"`
	ApprovalPolicyID               string             `json:"approvalPolicyId"`
	ApprovalPolicyRevision         string             `json:"approvalPolicyRevision"`
	ApprovalPolicyRootTrustDigest  string             `json:"approvalPolicyRootTrustDigest"`
	ApprovalPolicySequence         uint64             `json:"approvalPolicySequence"`
	ApprovalPolicyTargetDigest     string             `json:"approvalPolicyTargetDigest"`
	ApprovalReference              string             `json:"approvalReference"`
	ApprovedAt                     time.Time          `json:"approvedAt"`
	ApproverIdentity               string             `json:"approverIdentity"`
	ConsumptionID                  string             `json:"consumptionId"`
	ExecutionAttemptID             string             `json:"executionAttemptId"`
	ExpectedPolicyHead             ApprovalPolicyHead `json:"expectedPolicyHead"`
	FenceEpoch                     uint64             `json:"fenceEpoch"`
	Format                         string             `json:"format"`
	MutationNotAfter               time.Time          `json:"mutationNotAfter"`
	OpenedAt                       time.Time          `json:"openedAt"`
	OperationID                    string             `json:"operationId"`
	PlanDigest                     string             `json:"planDigest"`
	PlanExpiresAt                  time.Time          `json:"planExpiresAt"`
	PlanID                         string             `json:"planId"`
	PreflightExpiresAt             time.Time          `json:"preflightExpiresAt"`
	PreflightObservedAt            time.Time          `json:"preflightObservedAt"`
	PreflightReportDigest          string             `json:"preflightReportDigest"`
	RecordDigest                   string             `json:"recordDigest"`
	TargetBeforeStateDigest        string             `json:"targetBeforeStateDigest"`
	TokenDigest                    string             `json:"tokenDigest"`
}

type approvalExecutionFenceCandidate struct {
	record ApprovalExecutionFenceRecord
}

// ApprovalExecutionFenceStoredState must come from one authoritative durable snapshot.
type ApprovalExecutionFenceStoredState struct {
	Record ApprovalExecutionFenceRecord
}

type approvalExecutionFenceStore interface {
	Load(
		context.Context,
		ApprovalPolicyNamespace,
		string,
	) (ApprovalExecutionFenceStoredState, error)
	CompareAndOpen(
		context.Context,
		ApprovalPolicyNamespace,
		ApprovalPolicyHead,
		approvalExecutionFenceCandidate,
		string,
	) error
}

type approvalExecutionTokenSource func([]byte) error

// ApprovalExecutionFencer is the only constructor of an opaque ApprovalMutationFence. The store
// must atomically verify the exact current policy head, consume the approval, allocate a monotonic
// epoch, and create one unresolved namespace head before returning from CompareAndOpen.
type ApprovalExecutionFencer struct {
	store       approvalExecutionFenceStore
	tokenSource approvalExecutionTokenSource
}

func NewApprovalExecutionFencer(store approvalExecutionFenceStore) (ApprovalExecutionFencer, error) {
	return newApprovalExecutionFencer(store, func(destination []byte) error {
		_, err := rand.Read(destination)
		return err
	})
}

func newApprovalExecutionFencer(
	store approvalExecutionFenceStore,
	tokenSource approvalExecutionTokenSource,
) (ApprovalExecutionFencer, error) {
	if nilInterface(store) || tokenSource == nil {
		return ApprovalExecutionFencer{}, ErrInvalidApprovalExecutionFencer
	}
	return ApprovalExecutionFencer{store: store, tokenSource: tokenSource}, nil
}

// ApprovalMutationFence is an in-process capability. Its token is intentionally unexported; later
// target-step admission must present it through package-owned executor code and must still create a
// durable target receipt. A fence never auto-closes merely because MutationNotAfter has elapsed.
type ApprovalMutationFence struct {
	record ApprovalExecutionFenceRecord
	token  [approvalExecutionTokenBytes]byte
}

func (fence ApprovalMutationFence) ConsumptionID() string { return fence.record.ConsumptionID }
func (fence ApprovalMutationFence) EvidenceRecord() ApprovalExecutionFenceRecord {
	return fence.record
}
func (fence ApprovalMutationFence) ExecutionAttemptID() string {
	return fence.record.ExecutionAttemptID
}
func (fence ApprovalMutationFence) FenceEpoch() uint64          { return fence.record.FenceEpoch }
func (fence ApprovalMutationFence) MutationNotAfter() time.Time { return fence.record.MutationNotAfter }
func (fence ApprovalMutationFence) OperationID() string         { return fence.record.OperationID }
func (fence ApprovalMutationFence) PolicyHead() ApprovalPolicyHead {
	return fence.record.ExpectedPolicyHead
}
func (fence ApprovalMutationFence) tokenBytes() []byte { return slices.Clone(fence.token[:]) }

// ConsumeAndFence performs local immutable binding first, then delegates the atomic consumption
// and exact-head fence to the control store. It always performs a bounded authoritative readback;
// no capability is returned solely from a successful transport acknowledgement.
func (fencer ApprovalExecutionFencer) ConsumeAndFence(
	ctx context.Context,
	plan Plan,
	approval VerifiedApproval,
	report PreflightReport,
	executionAttemptID string,
	now time.Time,
) (ApprovalMutationFence, error) {
	if ctx == nil || nilInterface(fencer.store) || fencer.tokenSource == nil {
		return ApprovalMutationFence{}, ErrInvalidApprovalExecutionFencer
	}
	if !canonicalIdentity(executionAttemptID) ||
		!strings.HasPrefix(executionAttemptID, "execution-attempt/") {
		return ApprovalMutationFence{}, ErrInvalidApprovalExecutionState
	}
	if err := ValidatePreflightReport(report, plan, approval, now); err != nil {
		if errors.Is(err, ErrExpiredPreflightReport) || errors.Is(err, ErrExpiredApproval) {
			return ApprovalMutationFence{}, ErrApprovalExecutionExpired
		}
		return ApprovalMutationFence{}, ErrInvalidApprovalExecutionState
	}
	candidate, err := newApprovalExecutionFenceCandidate(plan, approval, report, executionAttemptID)
	if err != nil {
		return ApprovalMutationFence{}, err
	}
	var token [approvalExecutionTokenBytes]byte
	if err := fencer.tokenSource(token[:]); err != nil || zeroBytes(token[:]) {
		return ApprovalMutationFence{}, ErrApprovalExecutionStoreUnavailable
	}
	tokenDigest := domainSeparatedDigest(approvalExecutionTokenDigestDomain, token[:])
	namespace := ApprovalPolicyNamespace{
		PolicyID:     candidate.record.ApprovalPolicyID,
		TargetDigest: candidate.record.ApprovalPolicyTargetDigest,
	}
	commitErr := fencer.store.CompareAndOpen(
		ctx,
		namespace,
		candidate.record.ExpectedPolicyHead,
		candidate,
		tokenDigest,
	)
	reconciliationContext, cancelReconciliation := context.WithTimeout(
		context.WithoutCancel(ctx),
		approvalExecutionReconcileTimeout,
	)
	defer cancelReconciliation()
	readback, readbackErr := fencer.store.Load(
		reconciliationContext,
		namespace,
		candidate.record.OperationID,
	)
	if readbackErr != nil {
		if errors.Is(readbackErr, ErrInvalidApprovalExecutionState) ||
			errors.Is(commitErr, ErrInvalidApprovalExecutionState) {
			return ApprovalMutationFence{}, ErrInvalidApprovalExecutionState
		}
		if commitErr == nil || errors.Is(commitErr, ErrApprovalExecutionCommitUncertain) {
			return ApprovalMutationFence{}, ErrApprovalExecutionCommitUncertain
		}
		if errors.Is(commitErr, ErrApprovalExecutionConflict) {
			return ApprovalMutationFence{}, ErrApprovalExecutionConflict
		}
		return ApprovalMutationFence{}, ErrApprovalExecutionStoreUnavailable
	}
	if !approvalExecutionReadbackMatches(readback.Record, candidate, tokenDigest) {
		if commitErr == nil || errors.Is(commitErr, ErrApprovalExecutionCommitUncertain) {
			return ApprovalMutationFence{}, ErrApprovalExecutionCommitUncertain
		}
		if errors.Is(commitErr, ErrApprovalExecutionConflict) {
			return ApprovalMutationFence{}, ErrApprovalExecutionConflict
		}
		return ApprovalMutationFence{}, ErrInvalidApprovalExecutionState
	}
	return ApprovalMutationFence{record: readback.Record, token: token}, nil
}

func newApprovalExecutionFenceCandidate(
	plan Plan,
	approval VerifiedApproval,
	report PreflightReport,
	executionAttemptID string,
) (approvalExecutionFenceCandidate, error) {
	planSnapshot := plan.Snapshot()
	reportSnapshot := report.Snapshot()
	head := ApprovalPolicyHead{
		ActivationRecordDigest: approval.ActivationRecordDigest(),
		PolicyDigest:           approval.PolicyDigest(),
		PolicyID:               approval.PolicyID(),
		Revision:               approval.PolicySequence(),
		TargetDigest:           approval.PolicyTargetDigest(),
	}
	record := ApprovalExecutionFenceRecord{
		ApprovalDigest:                 approval.ApprovalDigest(),
		ApprovalExpiresAt:              approval.ExpiresAt(),
		ApprovalKeyFingerprint:         approval.KeyFingerprint(),
		ApprovalKeyGeneration:          approval.KeyGeneration(),
		ApprovalKeyID:                  approval.KeyID(),
		ApprovalPolicyActivationDigest: approval.ActivationRecordDigest(),
		ApprovalPolicyDigest:           approval.PolicyDigest(),
		ApprovalPolicyID:               approval.PolicyID(),
		ApprovalPolicyRevision:         approval.PolicyRevision(),
		ApprovalPolicyRootTrustDigest:  approval.RootTrustBundleDigest(),
		ApprovalPolicySequence:         approval.PolicySequence(),
		ApprovalPolicyTargetDigest:     approval.PolicyTargetDigest(),
		ApprovalReference:              approval.Reference(),
		ApprovedAt:                     approval.ApprovedAt(),
		ApproverIdentity:               approval.ApproverIdentity(),
		ExecutionAttemptID:             executionAttemptID,
		ExpectedPolicyHead:             head,
		Format:                         ApprovalExecutionFenceRecordFormat,
		MutationNotAfter: earliestApprovalExecutionExpiry(
			approval.ExpiresAt(),
			planSnapshot.ExpiresAt,
			report.ExpiresAt(),
		),
		PlanDigest:              plan.Digest(),
		PlanExpiresAt:           planSnapshot.ExpiresAt,
		PlanID:                  planSnapshot.PlanID,
		PreflightExpiresAt:      report.ExpiresAt(),
		PreflightObservedAt:     report.ObservedAt(),
		PreflightReportDigest:   report.Digest(),
		TargetBeforeStateDigest: report.Digest(),
	}
	record.ConsumptionID = approvalConsumptionID(record)
	record.OperationID = approvalExecutionOperationID(record.ConsumptionID, executionAttemptID)
	candidate := approvalExecutionFenceCandidate{record: record}
	if !validApprovalExecutionFenceCandidate(candidate) ||
		reportSnapshot.ReportDigest != record.PreflightReportDigest {
		return approvalExecutionFenceCandidate{}, ErrInvalidApprovalExecutionState
	}
	return candidate, nil
}

func sealApprovalExecutionFenceRecord(
	candidate approvalExecutionFenceCandidate,
	fenceEpoch uint64,
	openedAt time.Time,
	tokenDigest string,
) (ApprovalExecutionFenceRecord, error) {
	if !validApprovalExecutionFenceCandidate(candidate) || fenceEpoch == 0 ||
		!canonicalPreflightTime(openedAt) || !canonicalDigest.MatchString(tokenDigest) {
		return ApprovalExecutionFenceRecord{}, ErrInvalidApprovalExecutionState
	}
	record := candidate.record
	record.FenceEpoch = fenceEpoch
	record.OpenedAt = openedAt
	record.TokenDigest = tokenDigest
	if !validApprovalExecutionFenceRecord(record, false) {
		return ApprovalExecutionFenceRecord{}, ErrInvalidApprovalExecutionState
	}
	canonical, err := marshalApprovalExecutionFenceRecordCanonical(record)
	if err != nil {
		return ApprovalExecutionFenceRecord{}, ErrInvalidApprovalExecutionState
	}
	record.RecordDigest = domainSeparatedDigest(approvalExecutionFenceDigestDomain, canonical)
	if !validApprovalExecutionFenceRecord(record, true) {
		return ApprovalExecutionFenceRecord{}, ErrInvalidApprovalExecutionState
	}
	return record, nil
}

func validApprovalExecutionFenceCandidate(candidate approvalExecutionFenceCandidate) bool {
	record := candidate.record
	return record.FenceEpoch == 0 && record.OpenedAt.IsZero() && record.RecordDigest == "" &&
		record.TokenDigest == "" && validApprovalExecutionFenceBinding(record)
}

func validApprovalExecutionFenceRecord(
	record ApprovalExecutionFenceRecord,
	requireDigest bool,
) bool {
	if record.FenceEpoch == 0 || !canonicalPreflightTime(record.OpenedAt) ||
		!canonicalDigest.MatchString(record.TokenDigest) ||
		record.OpenedAt.Before(record.ApprovedAt) ||
		record.OpenedAt.Before(record.PreflightObservedAt) ||
		!record.OpenedAt.Before(record.MutationNotAfter) ||
		!validApprovalExecutionFenceBinding(record) {
		return false
	}
	if !requireDigest {
		return record.RecordDigest == ""
	}
	if !canonicalDigest.MatchString(record.RecordDigest) {
		return false
	}
	unsigned := record
	unsigned.RecordDigest = ""
	canonical, err := marshalApprovalExecutionFenceRecordCanonical(unsigned)
	return err == nil &&
		domainSeparatedDigest(approvalExecutionFenceDigestDomain, canonical) == record.RecordDigest
}

func validApprovalExecutionFenceBinding(record ApprovalExecutionFenceRecord) bool {
	return record.Format == ApprovalExecutionFenceRecordFormat &&
		canonicalDigest.MatchString(record.ApprovalDigest) &&
		canonicalPreflightTime(record.ApprovalExpiresAt) &&
		canonicalDigest.MatchString(record.ApprovalKeyFingerprint) &&
		canonicalIdentity(record.ApprovalKeyGeneration) &&
		canonicalIdentity(record.ApprovalKeyID) &&
		canonicalDigest.MatchString(record.ApprovalPolicyActivationDigest) &&
		canonicalDigest.MatchString(record.ApprovalPolicyDigest) &&
		canonicalIdentity(record.ApprovalPolicyID) &&
		canonicalIdentity(record.ApprovalPolicyRevision) &&
		canonicalDigest.MatchString(record.ApprovalPolicyRootTrustDigest) &&
		record.ApprovalPolicySequence > 0 &&
		record.ApprovalPolicySequence <= maximumApprovalPolicyRevision &&
		canonicalDigest.MatchString(record.ApprovalPolicyTargetDigest) &&
		validApprovalReference(record.ApprovalReference) &&
		canonicalPreflightTime(record.ApprovedAt) &&
		record.ApprovalExpiresAt.After(record.ApprovedAt) &&
		canonicalIdentity(record.ApproverIdentity) &&
		canonicalIdentity(record.ConsumptionID) &&
		canonicalIdentity(record.ExecutionAttemptID) &&
		strings.HasPrefix(record.ExecutionAttemptID, "execution-attempt/") &&
		record.ExpectedPolicyHead == (ApprovalPolicyHead{
			ActivationRecordDigest: record.ApprovalPolicyActivationDigest,
			PolicyDigest:           record.ApprovalPolicyDigest,
			PolicyID:               record.ApprovalPolicyID,
			Revision:               record.ApprovalPolicySequence,
			TargetDigest:           record.ApprovalPolicyTargetDigest,
		}) &&
		canonicalPreflightTime(record.MutationNotAfter) &&
		canonicalIdentity(record.OperationID) &&
		canonicalDigest.MatchString(record.PlanDigest) &&
		canonicalPreflightTime(record.PlanExpiresAt) &&
		canonicalIdentity(record.PlanID) &&
		canonicalPreflightTime(record.PreflightExpiresAt) &&
		canonicalPreflightTime(record.PreflightObservedAt) &&
		record.PreflightExpiresAt.After(record.PreflightObservedAt) &&
		canonicalDigest.MatchString(record.PreflightReportDigest) &&
		canonicalDigest.MatchString(record.TargetBeforeStateDigest) &&
		record.TargetBeforeStateDigest == record.PreflightReportDigest &&
		record.MutationNotAfter == earliestApprovalExecutionExpiry(
			record.ApprovalExpiresAt,
			record.PlanExpiresAt,
			record.PreflightExpiresAt,
		) &&
		record.ConsumptionID == approvalConsumptionID(record) &&
		record.OperationID == approvalExecutionOperationID(
			record.ConsumptionID,
			record.ExecutionAttemptID,
		) &&
		record.ApprovalPolicyRevision == approvalPolicyRevision(
			record.ApprovalPolicyID,
			record.ApprovalPolicySequence,
		)
}

func approvalExecutionReadbackMatches(
	record ApprovalExecutionFenceRecord,
	candidate approvalExecutionFenceCandidate,
	tokenDigest string,
) bool {
	if !validApprovalExecutionFenceRecord(record, true) ||
		!validApprovalExecutionFenceCandidate(candidate) || record.TokenDigest != tokenDigest {
		return false
	}
	expected := record
	expected.FenceEpoch = 0
	expected.OpenedAt = time.Time{}
	expected.RecordDigest = ""
	expected.TokenDigest = ""
	return expected == candidate.record
}

func approvalConsumptionID(record ApprovalExecutionFenceRecord) string {
	canonical, err := json.Marshal(struct {
		ApprovalDigest             string `json:"approvalDigest"`
		ApprovalPolicyID           string `json:"approvalPolicyId"`
		ApprovalPolicyTargetDigest string `json:"approvalPolicyTargetDigest"`
		ApprovalReference          string `json:"approvalReference"`
		PlanDigest                 string `json:"planDigest"`
	}{
		ApprovalDigest:             record.ApprovalDigest,
		ApprovalPolicyID:           record.ApprovalPolicyID,
		ApprovalPolicyTargetDigest: record.ApprovalPolicyTargetDigest,
		ApprovalReference:          record.ApprovalReference,
		PlanDigest:                 record.PlanDigest,
	})
	if err != nil {
		return ""
	}
	return "approval-consumption/" + strings.TrimPrefix(
		domainSeparatedDigest(approvalConsumptionIDDomain, canonical),
		"sha256:",
	)
}

func approvalExecutionOperationID(consumptionID string, executionAttemptID string) string {
	canonical, err := json.Marshal(struct {
		ConsumptionID      string `json:"consumptionId"`
		ExecutionAttemptID string `json:"executionAttemptId"`
	}{ConsumptionID: consumptionID, ExecutionAttemptID: executionAttemptID})
	if err != nil {
		return ""
	}
	return "approval-operation/" + strings.TrimPrefix(
		domainSeparatedDigest(approvalExecutionOperationIDDomain, canonical),
		"sha256:",
	)
}

func marshalApprovalExecutionFenceRecordCanonical(
	record ApprovalExecutionFenceRecord,
) ([]byte, error) {
	var output bytes.Buffer
	encoder := json.NewEncoder(&output)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(record); err != nil {
		return nil, err
	}
	return bytes.TrimSuffix(output.Bytes(), []byte("\n")), nil
}

func earliestApprovalExecutionExpiry(values ...time.Time) time.Time {
	if len(values) == 0 {
		return time.Time{}
	}
	earliest := values[0]
	for _, value := range values[1:] {
		if value.Before(earliest) {
			earliest = value
		}
	}
	return earliest
}

func zeroBytes(value []byte) bool {
	for _, item := range value {
		if item != 0 {
			return false
		}
	}
	return true
}
