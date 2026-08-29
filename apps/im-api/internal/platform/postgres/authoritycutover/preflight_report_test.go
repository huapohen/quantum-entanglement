package authoritycutover

import (
	"bytes"
	"crypto/ed25519"
	"encoding/json"
	"errors"
	"reflect"
	"strings"
	"testing"
	"time"
)

func TestPreflightReportBindsPlanApprovalPoliciesAndShortTTL(t *testing.T) {
	fixture := newApprovalFixture(t, 0)
	approval, err := fixture.verifier.Verify(fixture.plan, fixture.raw, fixture.now)
	if err != nil {
		t.Fatalf("Verify: %v", err)
	}
	report, err := buildPreflightReport(
		fixture.plan,
		approval,
		fixture.now,
		passingPreflightObservations(),
	)
	if err != nil {
		t.Fatalf("buildPreflightReport: %v", err)
	}
	snapshot := report.Snapshot()
	planSnapshot := fixture.plan.Snapshot()
	if snapshot.Format != PreflightReportFormat || snapshot.MutationAuthorized ||
		snapshot.Outcome != PreflightCheckPass || snapshot.PlanID != planSnapshot.PlanID ||
		snapshot.PlanDigest != fixture.plan.Digest() || snapshot.ApprovalDigest != approval.ApprovalDigest() ||
		snapshot.ApprovalKeyFingerprint != approval.KeyFingerprint() ||
		snapshot.ApprovalKeyGeneration != approval.KeyGeneration() ||
		snapshot.ApprovalKeyID != approval.KeyID() ||
		snapshot.ApprovalPolicyActivationDigest != approval.ActivationRecordDigest() ||
		snapshot.ApprovalPolicyDigest != approval.PolicyDigest() ||
		snapshot.ApprovalPolicyID != approval.PolicyID() ||
		snapshot.ApprovalPolicyRevision != approval.PolicyRevision() ||
		snapshot.ApprovalPolicyRootTrustDigest != approval.RootTrustBundleDigest() ||
		snapshot.ApprovalPolicySequence != approval.PolicySequence() ||
		snapshot.CellID != planSnapshot.Target.CellID || snapshot.DeploymentID != planSnapshot.Target.DeploymentID ||
		snapshot.ObservedAt != fixture.now || snapshot.ExpiresAt != fixture.now.Add(time.Minute) ||
		snapshot.ExpectationDigest != planSnapshot.Steps[0].PreconditionDigest ||
		snapshot.PassPolicyDigest != planSnapshot.Steps[0].PostconditionDigest ||
		snapshot.AbortPolicyDigest != planSnapshot.Steps[0].AbortConditionDigest ||
		snapshot.ReportDigest != report.Digest() || !canonicalDigest.MatchString(report.Digest()) {
		t.Fatalf("report binding = %#v", snapshot)
	}
	if err := ValidatePreflightReport(report, fixture.plan, approval, fixture.now); err != nil {
		t.Fatalf("ValidatePreflightReport: %v", err)
	}
	if err := ValidatePreflightReport(
		report,
		fixture.plan,
		approval,
		report.ExpiresAt(),
	); !errors.Is(err, ErrExpiredPreflightReport) {
		t.Fatalf("expiry error = %v, want %v", err, ErrExpiredPreflightReport)
	}
}

func TestPreflightReportGoldenDigest(t *testing.T) {
	fixture := newApprovalFixture(t, 0)
	approval, err := fixture.verifier.Verify(fixture.plan, fixture.raw, fixture.now)
	if err != nil {
		t.Fatalf("Verify: %v", err)
	}
	report, err := buildPreflightReport(
		fixture.plan,
		approval,
		fixture.now,
		passingPreflightObservations(),
	)
	if err != nil {
		t.Fatalf("buildPreflightReport: %v", err)
	}
	const want = "sha256:a95ec3af08497f2181c729bc236b2034182c36a3d22b8df95177fb165c030d4e"
	if report.Digest() != want {
		t.Fatalf("golden digest = %q, want %q", report.Digest(), want)
	}
}

func TestPreflightReportUsesEarliestExpiry(t *testing.T) {
	fixture := newApprovalFixture(t, 0)
	approval, err := fixture.verifier.Verify(fixture.plan, fixture.raw, fixture.now)
	if err != nil {
		t.Fatalf("Verify: %v", err)
	}
	observedAt := approval.ExpiresAt().Add(-30 * time.Second)
	report, err := buildPreflightReport(fixture.plan, approval, observedAt, passingPreflightObservations())
	if err != nil {
		t.Fatalf("buildPreflightReport: %v", err)
	}
	if report.ExpiresAt() != approval.ExpiresAt() {
		t.Fatalf("report expiry = %v, want approval expiry %v", report.ExpiresAt(), approval.ExpiresAt())
	}
}

func TestPreflightReportIsImmutableAndDoesNotExposeReusableEvidence(t *testing.T) {
	fixture := newApprovalFixture(t, 0)
	approval, err := fixture.verifier.Verify(fixture.plan, fixture.raw, fixture.now)
	if err != nil {
		t.Fatalf("Verify: %v", err)
	}
	report, err := buildPreflightReport(fixture.plan, approval, fixture.now, passingPreflightObservations())
	if err != nil {
		t.Fatalf("buildPreflightReport: %v", err)
	}
	canonical := report.CanonicalBytes()
	snapshot := report.Snapshot()
	canonical[0] ^= 0xff
	snapshot.Checks[0].CheckID = "mutated"
	if bytes.Equal(canonical, report.CanonicalBytes()) || report.Snapshot().Checks[0].CheckID == "mutated" {
		t.Fatal("caller mutation escaped immutable preflight report boundary")
	}
	encoded, err := json.Marshal(report)
	if err != nil || string(encoded) != "{}" {
		t.Fatalf("opaque report encoded as %s, %v", encoded, err)
	}
	typeOfReport := reflect.TypeOf(report)
	for index := range typeOfReport.NumField() {
		field := typeOfReport.Field(index)
		if field.IsExported() || strings.Contains(strings.ToLower(field.Name), "password") ||
			strings.Contains(strings.ToLower(field.Name), "signature") ||
			strings.Contains(strings.ToLower(field.Name), "systemidentifier") {
			t.Fatalf("PreflightReport exposes unsafe field %q", field.Name)
		}
	}
	for _, forbidden := range []string{"password", "postgresql://", "privatekey", "signature", "7678902413432981333"} {
		if strings.Contains(strings.ToLower(string(report.CanonicalBytes())), forbidden) {
			t.Fatalf("report contains forbidden material %q", forbidden)
		}
	}
}

func TestPreflightReportFailsClosedForMissingBlockAndUnknownChecks(t *testing.T) {
	fixture := newApprovalFixture(t, 0)
	approval, err := fixture.verifier.Verify(fixture.plan, fixture.raw, fixture.now)
	if err != nil {
		t.Fatalf("Verify: %v", err)
	}
	tests := map[string]struct {
		mutate func(map[string]preflightCheckObservation)
		want   PreflightCheckOutcome
	}{
		"missing": {
			mutate: func(values map[string]preflightCheckObservation) { delete(values, preflightCheckRegistry[0]) },
			want:   PreflightCheckUnknown,
		},
		"unknown": {
			mutate: func(values map[string]preflightCheckObservation) {
				values[preflightCheckRegistry[1]] = preflightCheckObservation{outcome: PreflightCheckUnknown, evidence: "timeout"}
			},
			want: PreflightCheckUnknown,
		},
		"block dominates unknown": {
			mutate: func(values map[string]preflightCheckObservation) {
				values[preflightCheckRegistry[1]] = preflightCheckObservation{outcome: PreflightCheckUnknown, evidence: "timeout"}
				values[preflightCheckRegistry[2]] = preflightCheckObservation{outcome: PreflightCheckBlock, evidence: "drift"}
			},
			want: PreflightCheckBlock,
		},
	}
	for name, test := range tests {
		t.Run(name, func(t *testing.T) {
			observations := passingPreflightObservations()
			test.mutate(observations)
			report, err := buildPreflightReport(fixture.plan, approval, fixture.now, observations)
			if err != nil {
				t.Fatalf("buildPreflightReport: %v", err)
			}
			if report.Outcome() != test.want {
				t.Fatalf("outcome = %q, want %q", report.Outcome(), test.want)
			}
			if err := ValidatePreflightReport(
				report,
				fixture.plan,
				approval,
				fixture.now,
			); !errors.Is(err, ErrPreflightBlocked) {
				t.Fatalf("ValidatePreflightReport error = %v, want %v", err, ErrPreflightBlocked)
			}
		})
	}
}

func TestPreflightReportRejectsPlanApprovalAndSnapshotDrift(t *testing.T) {
	fixture := newApprovalFixture(t, 0)
	approval, err := fixture.verifier.Verify(fixture.plan, fixture.raw, fixture.now)
	if err != nil {
		t.Fatalf("Verify: %v", err)
	}
	report, err := buildPreflightReport(fixture.plan, approval, fixture.now, passingPreflightObservations())
	if err != nil {
		t.Fatalf("buildPreflightReport: %v", err)
	}
	otherFixture := newApprovalFixtureWithPlanMutation(t, func(input *PlanInput) {
		input.PlanID = "plan-20260829-0002"
	})
	otherApproval, err := otherFixture.verifier.Verify(otherFixture.plan, otherFixture.raw, otherFixture.now)
	if err != nil {
		t.Fatalf("Verify other: %v", err)
	}
	if err := ValidatePreflightReport(
		report,
		otherFixture.plan,
		otherApproval,
		fixture.now,
	); !errors.Is(err, ErrUntrustedPreflightReport) {
		t.Fatalf("cross-plan error = %v, want %v", err, ErrUntrustedPreflightReport)
	}
	targetDriftedApproval := approval
	targetDriftedApproval.targetDigest = "sha256:" + strings.Repeat("e", 64)
	if err := ValidatePreflightReport(
		report,
		fixture.plan,
		targetDriftedApproval,
		fixture.now,
	); !errors.Is(err, ErrUntrustedPreflightReport) {
		t.Fatalf("policy target drift error = %v, want %v", err, ErrUntrustedPreflightReport)
	}

	mutations := map[string]func(*PreflightReportSnapshot){
		"mutation authorized": func(value *PreflightReportSnapshot) { value.MutationAuthorized = true },
		"changed approval": func(value *PreflightReportSnapshot) {
			value.ApprovalDigest = "sha256:" + strings.Repeat("e", 64)
		},
		"changed policy activation": func(value *PreflightReportSnapshot) {
			value.ApprovalPolicyActivationDigest = "sha256:" + strings.Repeat("e", 64)
		},
		"changed policy digest": func(value *PreflightReportSnapshot) {
			value.ApprovalPolicyDigest = "sha256:" + strings.Repeat("e", 64)
		},
		"changed policy id": func(value *PreflightReportSnapshot) {
			value.ApprovalPolicyID = "approval-policy/other"
		},
		"changed policy sequence": func(value *PreflightReportSnapshot) {
			value.ApprovalPolicySequence++
		},
		"changed root trust": func(value *PreflightReportSnapshot) {
			value.ApprovalPolicyRootTrustDigest = "sha256:" + strings.Repeat("e", 64)
		},
		"changed expectation": func(value *PreflightReportSnapshot) {
			value.ExpectationDigest = "sha256:" + strings.Repeat("e", 64)
		},
		"reordered checks": func(value *PreflightReportSnapshot) {
			value.Checks[0], value.Checks[1] = value.Checks[1], value.Checks[0]
		},
		"changed outcome": func(value *PreflightReportSnapshot) { value.Checks[0].Outcome = PreflightCheckBlock },
		"extended TTL":    func(value *PreflightReportSnapshot) { value.ExpiresAt = value.ExpiresAt.Add(time.Second) },
	}
	for name, mutate := range mutations {
		t.Run(name, func(t *testing.T) {
			snapshot := report.Snapshot()
			mutate(&snapshot)
			if _, err := sealPreflightReport(snapshot, fixture.plan, approval); !errors.Is(
				err,
				ErrInvalidPreflightReport,
			) {
				t.Fatalf("seal error = %v, want %v", err, ErrInvalidPreflightReport)
			}
		})
	}
}

func TestPreflightErrorsDoNotExposeObservationEvidence(t *testing.T) {
	fixture := newApprovalFixture(t, 0)
	approval, err := fixture.verifier.Verify(fixture.plan, fixture.raw, fixture.now)
	if err != nil {
		t.Fatalf("Verify: %v", err)
	}
	observations := passingPreflightObservations()
	observations[preflightCheckRegistry[0]] = preflightCheckObservation{
		outcome:  "password=preflight-canary",
		evidence: "postgresql://user:preflight-canary@db.invalid/postgres",
	}
	_, reportErr := buildPreflightReport(fixture.plan, approval, fixture.now, observations)
	if !errors.Is(reportErr, ErrInvalidPreflightReport) {
		t.Fatalf("error = %v, want %v", reportErr, ErrInvalidPreflightReport)
	}
	for _, forbidden := range []string{"password", "preflight-canary", "postgresql://"} {
		if strings.Contains(strings.ToLower(reportErr.Error()), forbidden) {
			t.Fatalf("preflight error exposed %q: %v", forbidden, reportErr)
		}
	}
}

func passingPreflightObservations() map[string]preflightCheckObservation {
	values := make(map[string]preflightCheckObservation, len(preflightCheckRegistry))
	for _, checkID := range preflightCheckRegistry {
		values[checkID] = preflightCheckObservation{
			outcome:  PreflightCheckPass,
			evidence: "fixture/" + checkID,
		}
	}
	return values
}

func newApprovalFixtureWithPlanMutation(
	t *testing.T,
	mutate func(*PlanInput),
) approvalFixture {
	t.Helper()
	input := validPlanInput()
	mutate(&input)
	plan, err := BuildPlan(input)
	if err != nil {
		t.Fatalf("BuildPlan mutated fixture: %v", err)
	}
	fixture := newApprovalFixture(t, 0)
	approvedAt := fixture.approvedAt
	expiresAt := fixture.expiresAt
	toSign, err := NewApprovalToSign(plan, fixture.trustedKey.KeyID, approvedAt, expiresAt)
	if err != nil {
		t.Fatalf("NewApprovalToSign mutated fixture: %v", err)
	}
	raw, err := toSign.Encode(ed25519.Sign(fixture.privateKey, toSign.SigningBytes()))
	if err != nil {
		t.Fatalf("Encode mutated fixture: %v", err)
	}
	fixture.plan = plan
	fixture.raw = raw
	return fixture
}
