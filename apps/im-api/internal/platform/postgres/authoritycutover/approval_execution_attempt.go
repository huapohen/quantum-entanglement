package authoritycutover

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"strings"
	"time"
)

const (
	ApprovalExecutionAttemptRecordFormat      = "wanwork.im.postgres-authority-approval-execution-attempt/2"
	approvalExecutionAttemptDigestDomain      = "wanwork.im/postgres-authority-approval-execution-attempt/2\n"
	approvalExecutionAttemptIssuanceDomain    = "wanwork.im/postgres-authority-approval-execution-attempt-issuance/1\n"
	approvalExecutionAttemptRedacted          = "ApprovalExecutionAttempt{opaque}"
	approvalExecutionAttemptReconcileTimeout  = 5 * time.Second
	approvalExecutionAttemptReconcileMinDelay = 10 * time.Millisecond
	approvalExecutionAttemptReconcileMaxDelay = 250 * time.Millisecond
	maximumApprovalExecutionAttemptBytes      = 64 * 1024
)

var (
	ErrInvalidApprovalExecutionAttemptIssuer   = errors.New("invalid PostgreSQL authority approval execution attempt issuer")
	ErrInvalidApprovalExecutionAttempt         = errors.New("invalid PostgreSQL authority approval execution attempt")
	ErrApprovalExecutionAttemptNotFound        = errors.New("PostgreSQL authority approval execution attempt not found")
	ErrApprovalExecutionAttemptConflict        = errors.New("PostgreSQL authority approval execution attempt conflict")
	ErrApprovalExecutionAttemptExpired         = errors.New("PostgreSQL authority approval execution attempt admission expired")
	ErrApprovalExecutionAttemptCommitUncertain = errors.New(
		"PostgreSQL authority approval execution attempt commit is uncertain",
	)
	ErrApprovalExecutionAttemptUnavailable = errors.New("PostgreSQL authority approval execution attempt store is unavailable")
)

// ApprovalExecutionAttemptRecord is an immutable durable grant created only after a signed
// approval and a passing preflight report have been validated. The store allocates identity,
// generation, and CreatedAt. AttemptReceiptDigest covers every other field, including the exact
// policy head and complete approval/preflight/plan authorization vector.
type ApprovalExecutionAttemptRecord struct {
	ApprovalDigest                string             `json:"approvalDigest"`
	ApprovalExpiresAt             time.Time          `json:"approvalExpiresAt"`
	ApprovalKeyFingerprint        string             `json:"approvalKeyFingerprint"`
	ApprovalKeyGeneration         string             `json:"approvalKeyGeneration"`
	ApprovalKeyID                 string             `json:"approvalKeyId"`
	ApprovalPolicyRevision        string             `json:"approvalPolicyRevision"`
	ApprovalPolicyRootTrustDigest string             `json:"approvalPolicyRootTrustDigest"`
	ApprovalReference             string             `json:"approvalReference"`
	ApprovedAt                    time.Time          `json:"approvedAt"`
	ApproverIdentity              string             `json:"approverIdentity"`
	AttemptGeneration             uint64             `json:"attemptGeneration"`
	AttemptID                     string             `json:"attemptId"`
	AttemptIssuanceID             string             `json:"attemptIssuanceId"`
	AttemptReceiptDigest          string             `json:"attemptReceiptDigest"`
	CreatedAt                     time.Time          `json:"createdAt"`
	ExpectedPolicyHead            ApprovalPolicyHead `json:"expectedPolicyHead"`
	Format                        string             `json:"format"`
	MutationNotAfter              time.Time          `json:"mutationNotAfter"`
	PlanDigest                    string             `json:"planDigest"`
	PlanExpiresAt                 time.Time          `json:"planExpiresAt"`
	PlanID                        string             `json:"planId"`
	PreflightExpiresAt            time.Time          `json:"preflightExpiresAt"`
	PreflightObservedAt           time.Time          `json:"preflightObservedAt"`
	PreflightReportDigest         string             `json:"preflightReportDigest"`
	TargetDigest                  string             `json:"targetDigest"`
}

type approvalExecutionAttemptCandidate struct {
	record ApprovalExecutionAttemptRecord
}

// ApprovalExecutionAttemptStoredState must be one authoritative durable store snapshot.
type ApprovalExecutionAttemptStoredState struct {
	Record ApprovalExecutionAttemptRecord
}

// approvalExecutionAttemptStore is package-owned so an application caller cannot mint an
// identity factory. CompareAndIssue is the one transactional write boundary. Load must perform an
// authoritative read by the stable, platform-derived AttemptIssuanceID.
type approvalExecutionAttemptStore interface {
	Load(
		context.Context,
		ApprovalPolicyNamespace,
		string,
	) (ApprovalExecutionAttemptStoredState, error)
	CompareAndIssue(
		context.Context,
		ApprovalPolicyNamespace,
		approvalExecutionAttemptCandidate,
	) error
}

type approvalExecutionAttemptClock func() time.Time

type approvalExecutionAttemptReconcilePolicy struct {
	maximumDelay time.Duration
	minimumDelay time.Duration
	timeout      time.Duration
}

type ApprovalExecutionAttemptIssuer struct {
	clock     approvalExecutionAttemptClock
	reconcile approvalExecutionAttemptReconcilePolicy
	store     approvalExecutionAttemptStore
}

func NewApprovalExecutionAttemptIssuer(
	store approvalExecutionAttemptStore,
) (ApprovalExecutionAttemptIssuer, error) {
	return newApprovalExecutionAttemptIssuer(
		store,
		func() time.Time { return time.Now().UTC() },
	)
}

func newApprovalExecutionAttemptIssuer(
	store approvalExecutionAttemptStore,
	clock approvalExecutionAttemptClock,
) (ApprovalExecutionAttemptIssuer, error) {
	return newApprovalExecutionAttemptIssuerWithReconcilePolicy(
		store,
		clock,
		approvalExecutionAttemptReconcilePolicy{
			maximumDelay: approvalExecutionAttemptReconcileMaxDelay,
			minimumDelay: approvalExecutionAttemptReconcileMinDelay,
			timeout:      approvalExecutionAttemptReconcileTimeout,
		},
	)
}

func newApprovalExecutionAttemptIssuerWithReconcilePolicy(
	store approvalExecutionAttemptStore,
	clock approvalExecutionAttemptClock,
	reconcile approvalExecutionAttemptReconcilePolicy,
) (ApprovalExecutionAttemptIssuer, error) {
	if nilInterface(store) || clock == nil || reconcile.timeout <= 0 ||
		reconcile.minimumDelay <= 0 || reconcile.maximumDelay < reconcile.minimumDelay {
		return ApprovalExecutionAttemptIssuer{}, ErrInvalidApprovalExecutionAttemptIssuer
	}
	return ApprovalExecutionAttemptIssuer{
		clock:     clock,
		reconcile: reconcile,
		store:     store,
	}, nil
}

// ApprovalExecutionAttempt is an opaque, package-issued reference to a durable post-preflight
// grant. There is deliberately no constructor accepting an ID, digest, generation, or timestamp.
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
func (attempt ApprovalExecutionAttempt) IssuanceID() string {
	return attempt.record.AttemptIssuanceID
}
func (attempt ApprovalExecutionAttempt) ReceiptDigest() string {
	return attempt.record.AttemptReceiptDigest
}

// Issue validates the full admission vector with an issuer-owned clock before asking the durable
// store to allocate an attempt. It never returns an attempt solely from a write acknowledgement;
// every outcome is reconciled through a bounded fresh-context readback.
func (issuer ApprovalExecutionAttemptIssuer) Issue(
	ctx context.Context,
	plan Plan,
	approval VerifiedApproval,
	report PreflightReport,
) (ApprovalExecutionAttempt, error) {
	if ctx == nil || nilInterface(issuer.store) || issuer.clock == nil ||
		!validPlanSnapshot(plan.snapshot, true) {
		return ApprovalExecutionAttempt{}, ErrInvalidApprovalExecutionAttemptIssuer
	}
	now := issuer.clock().UTC()
	if err := ValidatePreflightReport(report, plan, approval, now); err != nil {
		if errors.Is(err, ErrExpiredPreflightReport) || errors.Is(err, ErrExpiredApproval) {
			return ApprovalExecutionAttempt{}, ErrApprovalExecutionAttemptExpired
		}
		return ApprovalExecutionAttempt{}, ErrInvalidApprovalExecutionAttempt
	}
	candidate, err := newApprovalExecutionAttemptCandidate(plan, approval, report)
	if err != nil {
		return ApprovalExecutionAttempt{}, err
	}
	policyNamespace := ApprovalPolicyNamespace{
		PolicyID:     candidate.record.ExpectedPolicyHead.PolicyID,
		TargetDigest: candidate.record.ExpectedPolicyHead.TargetDigest,
	}
	commitErr := issuer.store.CompareAndIssue(ctx, policyNamespace, candidate)
	reconciliationContext, cancelReconciliation := context.WithTimeout(
		context.WithoutCancel(ctx),
		issuer.reconcile.timeout,
	)
	defer cancelReconciliation()
	readback, readbackErr := issuer.loadForReconciliation(
		reconciliationContext,
		policyNamespace,
		candidate.record.AttemptIssuanceID,
		commitErr == nil || errors.Is(commitErr, ErrApprovalExecutionAttemptCommitUncertain),
	)
	if readbackErr != nil {
		if errors.Is(readbackErr, ErrInvalidApprovalExecutionAttempt) ||
			errors.Is(commitErr, ErrInvalidApprovalExecutionAttempt) {
			return ApprovalExecutionAttempt{}, ErrInvalidApprovalExecutionAttempt
		}
		if errors.Is(readbackErr, ErrApprovalExecutionAttemptNotFound) &&
			errors.Is(commitErr, ErrApprovalExecutionAttemptExpired) {
			return ApprovalExecutionAttempt{}, ErrApprovalExecutionAttemptExpired
		}
		if commitErr == nil || errors.Is(commitErr, ErrApprovalExecutionAttemptCommitUncertain) {
			return ApprovalExecutionAttempt{}, ErrApprovalExecutionAttemptCommitUncertain
		}
		if errors.Is(commitErr, ErrApprovalExecutionAttemptConflict) {
			return ApprovalExecutionAttempt{}, ErrApprovalExecutionAttemptConflict
		}
		return ApprovalExecutionAttempt{}, ErrApprovalExecutionAttemptUnavailable
	}
	if !approvalExecutionAttemptReadbackMatches(readback.Record, candidate) {
		if commitErr == nil || errors.Is(commitErr, ErrApprovalExecutionAttemptCommitUncertain) {
			return ApprovalExecutionAttempt{}, ErrApprovalExecutionAttemptCommitUncertain
		}
		if errors.Is(commitErr, ErrApprovalExecutionAttemptConflict) {
			return ApprovalExecutionAttempt{}, ErrApprovalExecutionAttemptConflict
		}
		return ApprovalExecutionAttempt{}, ErrInvalidApprovalExecutionAttempt
	}
	return ApprovalExecutionAttempt{record: readback.Record}, nil
}

func (issuer ApprovalExecutionAttemptIssuer) loadForReconciliation(
	ctx context.Context,
	namespace ApprovalPolicyNamespace,
	issuanceID string,
	retryNotFound bool,
) (ApprovalExecutionAttemptStoredState, error) {
	delay := issuer.reconcile.minimumDelay
	for {
		state, err := issuer.store.Load(ctx, namespace, issuanceID)
		if err == nil || !retryNotFound || !errors.Is(err, ErrApprovalExecutionAttemptNotFound) {
			return state, err
		}
		timer := time.NewTimer(delay)
		select {
		case <-ctx.Done():
			if !timer.Stop() {
				select {
				case <-timer.C:
				default:
				}
			}
			return ApprovalExecutionAttemptStoredState{}, ErrApprovalExecutionAttemptNotFound
		case <-timer.C:
		}
		if delay < issuer.reconcile.maximumDelay {
			delay *= 2
			if delay > issuer.reconcile.maximumDelay {
				delay = issuer.reconcile.maximumDelay
			}
		}
	}
}

func newApprovalExecutionAttemptCandidate(
	plan Plan,
	approval VerifiedApproval,
	report PreflightReport,
) (approvalExecutionAttemptCandidate, error) {
	if !validPlanSnapshot(plan.snapshot, true) {
		return approvalExecutionAttemptCandidate{}, ErrInvalidApprovalExecutionAttempt
	}
	planSnapshot := plan.Snapshot()
	target, err := ApprovalPolicyTargetFromPlan(plan)
	if err != nil {
		return approvalExecutionAttemptCandidate{}, ErrInvalidApprovalExecutionAttempt
	}
	record := ApprovalExecutionAttemptRecord{
		ApprovalDigest:                approval.ApprovalDigest(),
		ApprovalExpiresAt:             approval.ExpiresAt(),
		ApprovalKeyFingerprint:        approval.KeyFingerprint(),
		ApprovalKeyGeneration:         approval.KeyGeneration(),
		ApprovalKeyID:                 approval.KeyID(),
		ApprovalPolicyRevision:        approval.PolicyRevision(),
		ApprovalPolicyRootTrustDigest: approval.RootTrustBundleDigest(),
		ApprovalReference:             approval.Reference(),
		ApprovedAt:                    approval.ApprovedAt(),
		ApproverIdentity:              approval.ApproverIdentity(),
		ExpectedPolicyHead: ApprovalPolicyHead{
			ActivationRecordDigest: approval.ActivationRecordDigest(),
			PolicyDigest:           approval.PolicyDigest(),
			PolicyID:               approval.PolicyID(),
			Revision:               approval.PolicySequence(),
			TargetDigest:           approval.PolicyTargetDigest(),
		},
		Format: ApprovalExecutionAttemptRecordFormat,
		MutationNotAfter: earliestApprovalExecutionExpiry(
			approval.ExpiresAt(),
			planSnapshot.ExpiresAt,
			report.ExpiresAt(),
		),
		PlanDigest:            plan.Digest(),
		PlanExpiresAt:         planSnapshot.ExpiresAt,
		PlanID:                planSnapshot.PlanID,
		PreflightExpiresAt:    report.ExpiresAt(),
		PreflightObservedAt:   report.ObservedAt(),
		PreflightReportDigest: report.Digest(),
		TargetDigest:          digestApprovalPolicyTarget(target),
	}
	record.AttemptIssuanceID = approvalExecutionAttemptIssuanceID(record)
	candidate := approvalExecutionAttemptCandidate{record: record}
	if !validApprovalExecutionAttemptCandidate(candidate) ||
		report.Snapshot().ReportDigest != record.PreflightReportDigest {
		return approvalExecutionAttemptCandidate{}, ErrInvalidApprovalExecutionAttempt
	}
	return candidate, nil
}

func sealApprovalExecutionAttemptRecord(
	candidate approvalExecutionAttemptCandidate,
	attemptGeneration uint64,
	attemptID string,
	createdAt time.Time,
) (ApprovalExecutionAttemptRecord, error) {
	if !validApprovalExecutionAttemptCandidate(candidate) || attemptGeneration == 0 ||
		!canonicalIdentity(attemptID) || !strings.HasPrefix(attemptID, "execution-attempt/") ||
		!canonicalPreflightTime(createdAt) {
		return ApprovalExecutionAttemptRecord{}, ErrInvalidApprovalExecutionAttempt
	}
	record := candidate.record
	record.AttemptGeneration = attemptGeneration
	record.AttemptID = attemptID
	record.CreatedAt = createdAt
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
	record := candidate.record
	return record.AttemptGeneration == 0 && record.AttemptID == "" &&
		record.AttemptReceiptDigest == "" && record.CreatedAt.IsZero() &&
		validApprovalExecutionAttemptBinding(record) &&
		record.AttemptIssuanceID == approvalExecutionAttemptIssuanceID(record)
}

func validApprovalExecutionAttemptRecord(
	record ApprovalExecutionAttemptRecord,
	requireDigest bool,
) bool {
	if record.AttemptGeneration == 0 || !canonicalIdentity(record.AttemptID) ||
		!strings.HasPrefix(record.AttemptID, "execution-attempt/") ||
		!canonicalPreflightTime(record.CreatedAt) ||
		record.CreatedAt.Before(record.PreflightObservedAt) ||
		!record.CreatedAt.Before(record.MutationNotAfter) ||
		!validApprovalExecutionAttemptBinding(record) ||
		record.AttemptIssuanceID != approvalExecutionAttemptIssuanceID(record) {
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

func validApprovalExecutionAttemptBinding(record ApprovalExecutionAttemptRecord) bool {
	namespace := ApprovalPolicyNamespace{
		PolicyID:     record.ExpectedPolicyHead.PolicyID,
		TargetDigest: record.ExpectedPolicyHead.TargetDigest,
	}
	return record.Format == ApprovalExecutionAttemptRecordFormat &&
		canonicalDigest.MatchString(record.ApprovalDigest) &&
		canonicalPreflightTime(record.ApprovalExpiresAt) &&
		canonicalDigest.MatchString(record.ApprovalKeyFingerprint) &&
		canonicalIdentity(record.ApprovalKeyGeneration) &&
		canonicalIdentity(record.ApprovalKeyID) &&
		canonicalIdentity(record.ApprovalPolicyRevision) &&
		canonicalDigest.MatchString(record.ApprovalPolicyRootTrustDigest) &&
		validApprovalReference(record.ApprovalReference) &&
		canonicalPreflightTime(record.ApprovedAt) &&
		record.ApprovalExpiresAt.After(record.ApprovedAt) &&
		canonicalIdentity(record.ApproverIdentity) &&
		canonicalIdentity(record.AttemptIssuanceID) &&
		strings.HasPrefix(record.AttemptIssuanceID, "execution-attempt-issuance/") &&
		record.ExpectedPolicyHead.Revision > 0 &&
		validApprovalPolicyExpectedHead(record.ExpectedPolicyHead, namespace) &&
		record.ApprovalPolicyRevision == approvalPolicyRevision(
			record.ExpectedPolicyHead.PolicyID,
			record.ExpectedPolicyHead.Revision,
		) &&
		canonicalPreflightTime(record.MutationNotAfter) &&
		canonicalDigest.MatchString(record.PlanDigest) &&
		canonicalPreflightTime(record.PlanExpiresAt) &&
		canonicalIdentity(record.PlanID) &&
		canonicalPreflightTime(record.PreflightExpiresAt) &&
		canonicalPreflightTime(record.PreflightObservedAt) &&
		!record.PreflightObservedAt.Before(record.ApprovedAt) &&
		record.PreflightExpiresAt.After(record.PreflightObservedAt) &&
		canonicalDigest.MatchString(record.PreflightReportDigest) &&
		canonicalDigest.MatchString(record.TargetDigest) &&
		record.TargetDigest == record.ExpectedPolicyHead.TargetDigest &&
		record.MutationNotAfter == earliestApprovalExecutionExpiry(
			record.ApprovalExpiresAt,
			record.PlanExpiresAt,
			record.PreflightExpiresAt,
		)
}

func approvalExecutionAttemptIssuanceID(record ApprovalExecutionAttemptRecord) string {
	issuance := record
	issuance.AttemptGeneration = 0
	issuance.AttemptID = ""
	issuance.AttemptIssuanceID = ""
	issuance.AttemptReceiptDigest = ""
	issuance.CreatedAt = time.Time{}
	canonical, err := marshalApprovalExecutionAttemptRecordCanonical(issuance)
	if err != nil {
		return ""
	}
	return "execution-attempt-issuance/" + strings.TrimPrefix(
		domainSeparatedDigest(approvalExecutionAttemptIssuanceDomain, canonical),
		"sha256:",
	)
}

func approvalExecutionAttemptReadbackMatches(
	record ApprovalExecutionAttemptRecord,
	candidate approvalExecutionAttemptCandidate,
) bool {
	if !validApprovalExecutionAttemptRecord(record, true) ||
		!validApprovalExecutionAttemptCandidate(candidate) {
		return false
	}
	expected := record
	expected.AttemptGeneration = 0
	expected.AttemptID = ""
	expected.AttemptReceiptDigest = ""
	expected.CreatedAt = time.Time{}
	return expected == candidate.record
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

func decodeApprovalExecutionAttemptRecord(raw []byte) (ApprovalExecutionAttemptRecord, error) {
	if len(raw) == 0 || len(raw) > maximumApprovalExecutionAttemptBytes {
		return ApprovalExecutionAttemptRecord{}, ErrInvalidApprovalExecutionAttempt
	}
	var record ApprovalExecutionAttemptRecord
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&record); err != nil {
		return ApprovalExecutionAttemptRecord{}, ErrInvalidApprovalExecutionAttempt
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return ApprovalExecutionAttemptRecord{}, ErrInvalidApprovalExecutionAttempt
	}
	canonical, err := marshalApprovalExecutionAttemptRecordCanonical(record)
	if err != nil || !bytes.Equal(raw, canonical) ||
		!validApprovalExecutionAttemptRecord(record, true) {
		return ApprovalExecutionAttemptRecord{}, ErrInvalidApprovalExecutionAttempt
	}
	return record, nil
}
