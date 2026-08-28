package authoritycutover

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"slices"
	"time"
)

const (
	PreflightReportFormat       = "wanwork.im.postgres-authority-preflight-report/1"
	preflightReportDigestDomain = "wanwork.im/postgres-authority-preflight-report/1\n"
	preflightEvidenceDomain     = "wanwork.im/postgres-authority-preflight-evidence/1\n"
	maximumPreflightReportBytes = 64 * 1024
)

var (
	ErrInvalidPreflightReport   = errors.New("invalid PostgreSQL authority preflight report")
	ErrUntrustedPreflightReport = errors.New("untrusted PostgreSQL authority preflight report")
	ErrExpiredPreflightReport   = errors.New("expired PostgreSQL authority preflight report")
	ErrPreflightBlocked         = errors.New("PostgreSQL authority preflight blocked")
)

type PreflightCheckOutcome string

const (
	PreflightCheckPass    PreflightCheckOutcome = "pass"
	PreflightCheckBlock   PreflightCheckOutcome = "block"
	PreflightCheckUnknown PreflightCheckOutcome = "unknown"
)

type PreflightCheckResult struct {
	CheckID        string                `json:"checkId"`
	EvidenceDigest string                `json:"evidenceDigest"`
	Outcome        PreflightCheckOutcome `json:"outcome"`
}

// PreflightReportSnapshot is public deployment evidence only. It binds the exact verified
// approval and plan, but contains no DSN, password, raw cluster identifier, certificate, SQL row,
// or reusable signed envelope.
type PreflightReportSnapshot struct {
	AbortPolicyDigest              string                 `json:"abortPolicyDigest"`
	ApprovalDigest                 string                 `json:"approvalDigest"`
	ApprovalKeyFingerprint         string                 `json:"approvalKeyFingerprint"`
	ApprovalKeyGeneration          string                 `json:"approvalKeyGeneration"`
	ApprovalKeyID                  string                 `json:"approvalKeyId"`
	ApprovalPolicyActivationDigest string                 `json:"approvalPolicyActivationDigest"`
	ApprovalPolicyDigest           string                 `json:"approvalPolicyDigest"`
	ApprovalPolicyID               string                 `json:"approvalPolicyId"`
	ApprovalPolicyRevision         string                 `json:"approvalPolicyRevision"`
	ApprovalPolicyRootTrustDigest  string                 `json:"approvalPolicyRootTrustDigest"`
	ApprovalPolicySequence         uint64                 `json:"approvalPolicySequence"`
	CellID                         string                 `json:"cellId"`
	Checks                         []PreflightCheckResult `json:"checks"`
	DeploymentID                   string                 `json:"deploymentId"`
	ExpectationDigest              string                 `json:"expectationDigest"`
	ExpiresAt                      time.Time              `json:"expiresAt"`
	Format                         string                 `json:"format"`
	MutationAuthorized             bool                   `json:"mutationAuthorized"`
	ObservedAt                     time.Time              `json:"observedAt"`
	Outcome                        PreflightCheckOutcome  `json:"outcome"`
	PassPolicyDigest               string                 `json:"passPolicyDigest"`
	PlanDigest                     string                 `json:"planDigest"`
	PlanID                         string                 `json:"planId"`
	ReportDigest                   string                 `json:"reportDigest"`
}

// PreflightReport is immutable evidence of a short-lived observation. A passing report still does
// not authorize mutation; the executor must validate it inside its durable cutover fence.
type PreflightReport struct {
	snapshot  PreflightReportSnapshot
	canonical []byte
	digest    string
}

func (report PreflightReport) CanonicalBytes() []byte { return slices.Clone(report.canonical) }
func (report PreflightReport) Digest() string         { return report.digest }
func (report PreflightReport) ExpiresAt() time.Time   { return report.snapshot.ExpiresAt }
func (report PreflightReport) ObservedAt() time.Time  { return report.snapshot.ObservedAt }
func (report PreflightReport) Outcome() PreflightCheckOutcome {
	return report.snapshot.Outcome
}
func (report PreflightReport) Snapshot() PreflightReportSnapshot {
	return clonePreflightReportSnapshot(report.snapshot)
}

type preflightCheckObservation struct {
	outcome  PreflightCheckOutcome
	evidence any
}

type preflightEvidenceEnvelope struct {
	CheckID  string                `json:"checkId"`
	Evidence any                   `json:"evidence"`
	Outcome  PreflightCheckOutcome `json:"outcome"`
}

func buildPreflightReport(
	plan Plan,
	approval VerifiedApproval,
	observedAt time.Time,
	observations map[string]preflightCheckObservation,
) (PreflightReport, error) {
	if !validPlanSnapshot(plan.snapshot, true) || !verifiedApprovalBindsPlanAt(approval, plan, observedAt) {
		return PreflightReport{}, ErrUntrustedPreflightReport
	}
	planSnapshot := plan.Snapshot()
	preflightStep := planSnapshot.Steps[0]
	for checkID := range observations {
		if !slices.Contains(preflightCheckRegistry[:], checkID) {
			return PreflightReport{}, ErrInvalidPreflightReport
		}
	}
	checks := make([]PreflightCheckResult, 0, len(preflightCheckRegistry))
	for _, checkID := range preflightCheckRegistry {
		observation, exists := observations[checkID]
		if !exists {
			observation = preflightCheckObservation{
				outcome:  PreflightCheckUnknown,
				evidence: "observation-unavailable",
			}
		} else if !validPreflightCheckOutcome(observation.outcome) {
			return PreflightReport{}, ErrInvalidPreflightReport
		}
		evidenceDigest, err := digestPreflightEvidence(checkID, observation)
		if err != nil {
			return PreflightReport{}, ErrInvalidPreflightReport
		}
		checks = append(checks, PreflightCheckResult{
			CheckID:        checkID,
			EvidenceDigest: evidenceDigest,
			Outcome:        observation.outcome,
		})
	}
	snapshot := PreflightReportSnapshot{
		AbortPolicyDigest:              preflightStep.AbortConditionDigest,
		ApprovalDigest:                 approval.ApprovalDigest(),
		ApprovalKeyFingerprint:         approval.KeyFingerprint(),
		ApprovalKeyGeneration:          approval.KeyGeneration(),
		ApprovalKeyID:                  approval.KeyID(),
		ApprovalPolicyActivationDigest: approval.ActivationRecordDigest(),
		ApprovalPolicyDigest:           approval.PolicyDigest(),
		ApprovalPolicyID:               approval.PolicyID(),
		ApprovalPolicyRevision:         approval.PolicyRevision(),
		ApprovalPolicyRootTrustDigest:  approval.RootTrustBundleDigest(),
		ApprovalPolicySequence:         approval.PolicySequence(),
		CellID:                         planSnapshot.Target.CellID,
		Checks:                         checks,
		DeploymentID:                   planSnapshot.Target.DeploymentID,
		ExpectationDigest:              preflightStep.PreconditionDigest,
		ExpiresAt:                      preflightReportExpiry(planSnapshot.ExpiresAt, approval.ExpiresAt(), observedAt),
		Format:                         PreflightReportFormat,
		MutationAuthorized:             false,
		ObservedAt:                     observedAt,
		Outcome:                        aggregatePreflightOutcome(checks),
		PassPolicyDigest:               preflightStep.PostconditionDigest,
		PlanDigest:                     plan.Digest(),
		PlanID:                         planSnapshot.PlanID,
	}
	return sealPreflightReport(snapshot, plan, approval)
}

// ValidatePreflightReport proves structural integrity, exact plan/approval binding, freshness and
// a passing outcome. It is intentionally still not a mutation lease or replay fence.
func ValidatePreflightReport(
	report PreflightReport,
	plan Plan,
	approval VerifiedApproval,
	now time.Time,
) error {
	if now.IsZero() || !validPlanSnapshot(plan.snapshot, true) ||
		!verifiedApprovalBindsPlanAt(approval, plan, report.snapshot.ObservedAt) ||
		!validPreflightReportSnapshot(report.snapshot, plan, approval, true) ||
		report.digest != report.snapshot.ReportDigest {
		return ErrUntrustedPreflightReport
	}
	sealed, err := sealPreflightReport(report.Snapshot(), plan, approval)
	if err != nil || sealed.Digest() != report.Digest() ||
		!bytes.Equal(sealed.CanonicalBytes(), report.CanonicalBytes()) {
		return ErrUntrustedPreflightReport
	}
	instant := now.UTC()
	if !instant.Before(report.ExpiresAt()) || !instant.Before(plan.Snapshot().ExpiresAt) ||
		!instant.Before(approval.ExpiresAt()) {
		return ErrExpiredPreflightReport
	}
	if report.Outcome() != PreflightCheckPass {
		return ErrPreflightBlocked
	}
	return nil
}

func sealPreflightReport(
	snapshot PreflightReportSnapshot,
	plan Plan,
	approval VerifiedApproval,
) (PreflightReport, error) {
	if !validPreflightReportSnapshot(snapshot, plan, approval, false) {
		return PreflightReport{}, ErrInvalidPreflightReport
	}
	unsigned := clonePreflightReportSnapshot(snapshot)
	unsigned.ReportDigest = ""
	canonicalUnsigned, err := marshalPreflightReportCanonical(unsigned)
	if err != nil {
		return PreflightReport{}, ErrInvalidPreflightReport
	}
	digest := digestPreflightReport(canonicalUnsigned)
	if snapshot.ReportDigest != "" && snapshot.ReportDigest != digest {
		return PreflightReport{}, ErrInvalidPreflightReport
	}
	snapshot.ReportDigest = digest
	if !validPreflightReportSnapshot(snapshot, plan, approval, true) {
		return PreflightReport{}, ErrInvalidPreflightReport
	}
	canonical, err := marshalPreflightReportCanonical(snapshot)
	if err != nil || len(canonical) > maximumPreflightReportBytes {
		return PreflightReport{}, ErrInvalidPreflightReport
	}
	return PreflightReport{
		snapshot:  clonePreflightReportSnapshot(snapshot),
		canonical: slices.Clone(canonical),
		digest:    digest,
	}, nil
}

func validPreflightReportSnapshot(
	snapshot PreflightReportSnapshot,
	plan Plan,
	approval VerifiedApproval,
	requireDigest bool,
) bool {
	if !validPlanSnapshot(plan.snapshot, true) || snapshot.Format != PreflightReportFormat ||
		snapshot.MutationAuthorized || snapshot.PlanID != plan.snapshot.PlanID ||
		snapshot.PlanDigest != plan.Digest() || snapshot.ApprovalDigest != approval.ApprovalDigest() ||
		snapshot.ApprovalKeyFingerprint != approval.KeyFingerprint() ||
		snapshot.ApprovalKeyGeneration != approval.KeyGeneration() ||
		snapshot.ApprovalKeyID != approval.KeyID() ||
		snapshot.ApprovalPolicyActivationDigest != approval.ActivationRecordDigest() ||
		snapshot.ApprovalPolicyDigest != approval.PolicyDigest() ||
		snapshot.ApprovalPolicyID != approval.PolicyID() ||
		snapshot.ApprovalPolicyRevision != approval.PolicyRevision() ||
		snapshot.ApprovalPolicyRootTrustDigest != approval.RootTrustBundleDigest() ||
		snapshot.ApprovalPolicySequence != approval.PolicySequence() ||
		snapshot.CellID != plan.snapshot.Target.CellID ||
		snapshot.DeploymentID != plan.snapshot.Target.DeploymentID ||
		!canonicalDigest.MatchString(snapshot.ApprovalDigest) ||
		!canonicalDigest.MatchString(snapshot.ApprovalKeyFingerprint) ||
		!canonicalIdentity(snapshot.ApprovalKeyGeneration) ||
		!canonicalIdentity(snapshot.ApprovalKeyID) ||
		!canonicalDigest.MatchString(snapshot.ApprovalPolicyActivationDigest) ||
		!canonicalDigest.MatchString(snapshot.ApprovalPolicyDigest) ||
		!canonicalIdentity(snapshot.ApprovalPolicyID) ||
		!canonicalIdentity(snapshot.ApprovalPolicyRevision) ||
		!canonicalDigest.MatchString(snapshot.ApprovalPolicyRootTrustDigest) ||
		snapshot.ApprovalPolicySequence == 0 ||
		snapshot.ApprovalPolicySequence > maximumApprovalPolicyRevision ||
		!canonicalIdentity(snapshot.CellID) || !canonicalIdentity(snapshot.DeploymentID) ||
		!canonicalIdentity(snapshot.PlanID) || !canonicalDigest.MatchString(snapshot.PlanDigest) ||
		!canonicalPreflightTime(snapshot.ObservedAt) || !canonicalPreflightTime(snapshot.ExpiresAt) ||
		!snapshot.ExpiresAt.After(snapshot.ObservedAt) ||
		snapshot.ExpiresAt != preflightReportExpiry(plan.snapshot.ExpiresAt, approval.ExpiresAt(), snapshot.ObservedAt) ||
		len(plan.snapshot.Steps) != 5 || snapshot.ExpectationDigest != plan.snapshot.Steps[0].PreconditionDigest ||
		snapshot.PassPolicyDigest != plan.snapshot.Steps[0].PostconditionDigest ||
		snapshot.AbortPolicyDigest != plan.snapshot.Steps[0].AbortConditionDigest ||
		!canonicalDigest.MatchString(snapshot.ExpectationDigest) ||
		!canonicalDigest.MatchString(snapshot.PassPolicyDigest) ||
		!canonicalDigest.MatchString(snapshot.AbortPolicyDigest) ||
		!validPreflightCheckResults(snapshot.Checks) ||
		snapshot.Outcome != aggregatePreflightOutcome(snapshot.Checks) {
		return false
	}
	if requireDigest {
		return canonicalDigest.MatchString(snapshot.ReportDigest)
	}
	return snapshot.ReportDigest == "" || canonicalDigest.MatchString(snapshot.ReportDigest)
}

func validPreflightCheckResults(checks []PreflightCheckResult) bool {
	if len(checks) != len(preflightCheckRegistry) {
		return false
	}
	for index, result := range checks {
		if result.CheckID != preflightCheckRegistry[index] ||
			!validPreflightCheckOutcome(result.Outcome) ||
			!canonicalDigest.MatchString(result.EvidenceDigest) {
			return false
		}
	}
	return true
}

func validPreflightCheckOutcome(outcome PreflightCheckOutcome) bool {
	return outcome == PreflightCheckPass || outcome == PreflightCheckBlock ||
		outcome == PreflightCheckUnknown
}

func aggregatePreflightOutcome(checks []PreflightCheckResult) PreflightCheckOutcome {
	outcome := PreflightCheckPass
	for _, check := range checks {
		if check.Outcome == PreflightCheckBlock {
			return PreflightCheckBlock
		}
		if check.Outcome == PreflightCheckUnknown {
			outcome = PreflightCheckUnknown
		}
	}
	return outcome
}

func verifiedApprovalBindsPlanAt(
	approval VerifiedApproval,
	plan Plan,
	observedAt time.Time,
) bool {
	if !canonicalPreflightTime(observedAt) || !canonicalDigest.MatchString(approval.ApprovalDigest()) ||
		!canonicalDigest.MatchString(approval.KeyFingerprint()) ||
		!canonicalIdentity(approval.KeyGeneration()) || !canonicalIdentity(approval.KeyID()) ||
		!canonicalDigest.MatchString(approval.ActivationRecordDigest()) ||
		!canonicalDigest.MatchString(approval.PolicyDigest()) ||
		!canonicalIdentity(approval.PolicyID()) || !canonicalIdentity(approval.PolicyRevision()) ||
		!canonicalDigest.MatchString(approval.RootTrustBundleDigest()) ||
		!canonicalDigest.MatchString(approval.PolicyTargetDigest()) ||
		approval.PolicySequence() == 0 || approval.PolicySequence() > maximumApprovalPolicyRevision ||
		!canonicalPreflightTime(approval.ApprovedAt()) ||
		!canonicalPreflightTime(approval.ExpiresAt()) {
		return false
	}
	target, err := ApprovalPolicyTargetFromPlan(plan)
	if err != nil {
		return false
	}
	snapshot := plan.Snapshot()
	return approval.PlanID() == snapshot.PlanID && approval.PlanDigest() == plan.Digest() &&
		approval.ApproverIdentity() == snapshot.Approval.Identity &&
		approval.Reference() == snapshot.Approval.Reference &&
		approval.CellID() == snapshot.Target.CellID &&
		approval.DeploymentID() == snapshot.Target.DeploymentID &&
		approval.PolicyTargetDigest() == digestApprovalPolicyTarget(target) &&
		!observedAt.Before(approval.ApprovedAt()) && observedAt.Before(approval.ExpiresAt()) &&
		observedAt.Before(snapshot.ExpiresAt)
}

func preflightReportExpiry(planExpiry time.Time, approvalExpiry time.Time, observedAt time.Time) time.Time {
	expiresAt := observedAt.Add(preflightMaximumObservationAgeSeconds * time.Second)
	if planExpiry.Before(expiresAt) {
		expiresAt = planExpiry
	}
	if approvalExpiry.Before(expiresAt) {
		expiresAt = approvalExpiry
	}
	return expiresAt
}

func canonicalPreflightTime(value time.Time) bool {
	return !value.IsZero() && value.Location() == time.UTC && value.Nanosecond() == 0
}

func digestPreflightEvidence(
	checkID string,
	observation preflightCheckObservation,
) (string, error) {
	canonical, err := json.Marshal(preflightEvidenceEnvelope{
		CheckID:  checkID,
		Evidence: observation.evidence,
		Outcome:  observation.outcome,
	})
	if err != nil {
		return "", ErrInvalidPreflightReport
	}
	hash := sha256.New()
	_, _ = hash.Write([]byte(preflightEvidenceDomain))
	_, _ = hash.Write(canonical)
	return "sha256:" + hex.EncodeToString(hash.Sum(nil)), nil
}

func marshalPreflightReportCanonical(snapshot PreflightReportSnapshot) ([]byte, error) {
	var output bytes.Buffer
	encoder := json.NewEncoder(&output)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(snapshot); err != nil {
		return nil, err
	}
	return bytes.TrimSuffix(output.Bytes(), []byte("\n")), nil
}

func digestPreflightReport(canonical []byte) string {
	hash := sha256.New()
	_, _ = hash.Write([]byte(preflightReportDigestDomain))
	_, _ = hash.Write(canonical)
	return "sha256:" + hex.EncodeToString(hash.Sum(nil))
}

func clonePreflightReportSnapshot(snapshot PreflightReportSnapshot) PreflightReportSnapshot {
	cloned := snapshot
	cloned.Checks = slices.Clone(snapshot.Checks)
	return cloned
}
