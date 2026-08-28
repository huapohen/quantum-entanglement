package authoritycutover

import (
	"bytes"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"slices"
	"strings"
	"testing"
	"time"
)

type approvalFixture struct {
	plan       Plan
	publicKey  ed25519.PublicKey
	privateKey ed25519.PrivateKey
	trustedKey ApprovalVerificationKey
	verifier   ApprovalVerifier
	approvedAt time.Time
	expiresAt  time.Time
	now        time.Time
	raw        []byte
}

func TestDetachedApprovalDeterministicRoundTripAndEvidenceBoundary(t *testing.T) {
	fixture := newApprovalFixture(t, 0)
	toSign, err := NewApprovalToSign(fixture.plan, "release-key-2026-08", fixture.approvedAt, fixture.expiresAt)
	if err != nil {
		t.Fatalf("NewApprovalToSign: %v", err)
	}
	signingBytes := toSign.SigningBytes()
	if !bytes.HasPrefix(signingBytes, []byte(approvalSignatureDomain)) {
		t.Fatal("signing payload is missing its domain separator")
	}
	signingBytes[0] ^= 0xff
	if !bytes.HasPrefix(toSign.SigningBytes(), []byte(approvalSignatureDomain)) {
		t.Fatal("caller mutation escaped signing-payload boundary")
	}

	second, err := NewApprovalToSign(fixture.plan, "release-key-2026-08", fixture.approvedAt, fixture.expiresAt)
	if err != nil {
		t.Fatalf("NewApprovalToSign second: %v", err)
	}
	secondRaw, err := second.Encode(ed25519.Sign(fixture.privateKey, second.SigningBytes()))
	if err != nil {
		t.Fatalf("Encode second: %v", err)
	}
	if !bytes.Equal(fixture.raw, secondRaw) {
		t.Fatal("fixed plan, time, and key did not produce a deterministic approval")
	}

	verified, err := fixture.verifier.Verify(fixture.plan, fixture.raw, fixture.now)
	if err != nil {
		t.Fatalf("Verify: %v", err)
	}
	snapshot := fixture.plan.Snapshot()
	if verified.ApprovedAt() != fixture.approvedAt || verified.ExpiresAt() != fixture.expiresAt ||
		verified.ApproverIdentity() != snapshot.Approval.Identity ||
		verified.CellID() != snapshot.Target.CellID ||
		verified.DeploymentID() != snapshot.Target.DeploymentID ||
		verified.KeyFingerprint() != approvalKeyFingerprint(fixture.publicKey) ||
		verified.KeyGeneration() != fixture.trustedKey.Generation ||
		verified.KeyID() != "release-key-2026-08" || verified.PlanDigest() != fixture.plan.Digest() ||
		verified.PlanID() != snapshot.PlanID || verified.Reference() != snapshot.Approval.Reference ||
		verified.PolicyRevision() != fixture.trustedKey.PolicyRevision ||
		!canonicalDigest.MatchString(verified.ApprovalDigest()) {
		t.Fatalf("verified metadata is incomplete: %+v", verified)
	}
	if verified.ApprovalDigest() != approvalEvidenceDigest(fixture.raw) {
		t.Fatal("evidence digest is not bound to the canonical signed envelope")
	}

	encodedVerified, err := json.Marshal(verified)
	if err != nil || string(encodedVerified) != "{}" {
		t.Fatalf("VerifiedApproval exposed internal evidence: %s, %v", encodedVerified, err)
	}
	typeOfVerified := reflect.TypeOf(verified)
	for index := range typeOfVerified.NumField() {
		field := typeOfVerified.Field(index)
		if field.IsExported() || strings.Contains(strings.ToLower(field.Name), "signature") ||
			strings.Contains(strings.ToLower(field.Name), "envelope") ||
			strings.Contains(strings.ToLower(field.Name), "raw") ||
			strings.Contains(strings.ToLower(field.Name), "canonical") {
			t.Fatalf("VerifiedApproval exposes reusable signed material through field %q", field.Name)
		}
	}
	signature := approvalSignature(t, fixture.raw)
	if strings.Contains(fmt.Sprintf("%+v", verified), signature) {
		t.Fatal("VerifiedApproval representation disclosed the detached signature")
	}
}

func TestApprovalVerifierCopiesCallerOwnedTrustMaterial(t *testing.T) {
	fixture := newApprovalFixture(t, 0)
	key := fixture.trustedKey
	key.PublicKey = slices.Clone(fixture.publicKey)
	keys := []ApprovalVerificationKey{key}
	verifier, err := NewApprovalVerifier(keys, 0)
	if err != nil {
		t.Fatalf("NewApprovalVerifier: %v", err)
	}
	keys[0].KeyID = "mutated-key"
	keys[0].ApproverIdentity = "release-owner/mutated"
	keys[0].Generation = "generation-mutated"
	keys[0].PolicyRevision = "policy/mutated"
	keys[0].Scope.CellID = "postgres-cell-mutated"
	keys[0].PublicKey[0] ^= 0xff
	key.PublicKey[1] ^= 0xff
	if _, err := verifier.Verify(fixture.plan, fixture.raw, fixture.now); err != nil {
		t.Fatalf("caller mutation changed verifier trust: %v", err)
	}
}

func TestApprovalVerifierRejectsInvalidKeyrings(t *testing.T) {
	fixture := newApprovalFixture(t, 0)
	valid := fixture.trustedKey
	badIdentity := valid
	badIdentity.ApproverIdentity = "Release Owner"
	badGeneration := valid
	badGeneration.Generation = "rotation-1"
	badKeyID := valid
	badKeyID.KeyID = "Release Key"
	badPolicyRevision := valid
	badPolicyRevision.PolicyRevision = "revision-1"
	badScope := valid
	badScope.Scope.ReferencePrefix = "approval/postgres-cell-a"
	shortKey := valid
	shortKey.PublicKey = fixture.publicKey[:ed25519.PublicKeySize-1]
	revoked := valid
	revoked.Revoked = true
	badValidity := valid
	badValidity.NotAfter = badValidity.NotBefore
	tests := map[string]struct {
		keys []ApprovalVerificationKey
		skew time.Duration
	}{
		"empty":          {keys: nil},
		"duplicate":      {keys: []ApprovalVerificationKey{valid, valid}},
		"bad identity":   {keys: []ApprovalVerificationKey{badIdentity}},
		"bad generation": {keys: []ApprovalVerificationKey{badGeneration}},
		"bad key id":     {keys: []ApprovalVerificationKey{badKeyID}},
		"bad policy":     {keys: []ApprovalVerificationKey{badPolicyRevision}},
		"bad scope":      {keys: []ApprovalVerificationKey{badScope}},
		"short key":      {keys: []ApprovalVerificationKey{shortKey}},
		"revoked":        {keys: []ApprovalVerificationKey{revoked}},
		"bad validity":   {keys: []ApprovalVerificationKey{badValidity}},
		"negative skew":  {keys: []ApprovalVerificationKey{valid}, skew: -time.Nanosecond},
		"excessive skew": {keys: []ApprovalVerificationKey{valid}, skew: maximumApprovalClockSkew + time.Nanosecond},
	}
	tooMany := make([]ApprovalVerificationKey, maximumApprovalKeys+1)
	for index := range tooMany {
		tooMany[index] = ApprovalVerificationKey{
			ApproverIdentity: valid.ApproverIdentity,
			Generation:       valid.Generation,
			KeyID:            fmt.Sprintf("release-key-%03d", index),
			NotAfter:         valid.NotAfter,
			NotBefore:        valid.NotBefore,
			PolicyRevision:   valid.PolicyRevision,
			PublicKey:        fixture.publicKey,
			Scope:            valid.Scope,
		}
	}
	tests["too many"] = struct {
		keys []ApprovalVerificationKey
		skew time.Duration
	}{keys: tooMany}
	for name, test := range tests {
		t.Run(name, func(t *testing.T) {
			if _, err := NewApprovalVerifier(test.keys, test.skew); !errors.Is(err, ErrInvalidApprovalVerifier) {
				t.Fatalf("NewApprovalVerifier error = %v, want %v", err, ErrInvalidApprovalVerifier)
			}
		})
	}
	atLimit := make([]ApprovalVerificationKey, maximumApprovalKeys)
	copy(atLimit, tooMany[:maximumApprovalKeys])
	atLimit[maximumApprovalKeys-1] = valid
	atLimitVerifier, err := NewApprovalVerifier(atLimit, maximumApprovalClockSkew)
	if err != nil {
		t.Fatalf("NewApprovalVerifier at limit: %v", err)
	}
	if _, err := atLimitVerifier.Verify(fixture.plan, fixture.raw, fixture.now); err != nil {
		t.Fatalf("maximum accepted keyring failed verification: %v", err)
	}
	if _, err := (ApprovalVerifier{}).Verify(fixture.plan, fixture.raw, fixture.now); !errors.Is(err, ErrInvalidApprovalVerifier) {
		t.Fatalf("zero verifier error = %v, want %v", err, ErrInvalidApprovalVerifier)
	}
	if _, err := fixture.verifier.Verify(fixture.plan, fixture.raw, time.Time{}); !errors.Is(err, ErrInvalidApprovalVerifier) {
		t.Fatalf("zero verification time error = %v, want %v", err, ErrInvalidApprovalVerifier)
	}
	if _, err := fixture.verifier.Verify(Plan{}, fixture.raw, fixture.now); !errors.Is(err, ErrInvalidApproval) {
		t.Fatalf("zero plan error = %v, want %v", err, ErrInvalidApproval)
	}
}

func TestApprovalSignatureRequiresItsExactDomain(t *testing.T) {
	fixture := newApprovalFixture(t, 0)
	toSign, err := NewApprovalToSign(fixture.plan, "release-key-2026-08", fixture.approvedAt, fixture.expiresAt)
	if err != nil {
		t.Fatalf("NewApprovalToSign: %v", err)
	}
	unsigned := bytes.TrimPrefix(toSign.SigningBytes(), []byte(approvalSignatureDomain))
	wrongMessages := map[string][]byte{
		"bare canonical":  unsigned,
		"plan domain":     append([]byte(planDigestDomain), unsigned...),
		"evidence domain": append([]byte(approvalEvidenceDigestDomain), unsigned...),
	}
	for name, message := range wrongMessages {
		t.Run(name, func(t *testing.T) {
			raw, err := toSign.Encode(ed25519.Sign(fixture.privateKey, message))
			if err != nil {
				t.Fatalf("Encode: %v", err)
			}
			if _, err := fixture.verifier.Verify(fixture.plan, raw, fixture.now); !errors.Is(err, ErrUntrustedApproval) {
				t.Fatalf("Verify error = %v, want %v", err, ErrUntrustedApproval)
			}
		})
	}
}

func TestApprovalRejectsEveryPlanIdentityAndSignatureDrift(t *testing.T) {
	fixture := newApprovalFixture(t, 0)
	tests := map[string]func(*approvalEnvelope){
		"algorithm":         func(value *approvalEnvelope) { value.Algorithm = "ed448" },
		"decision":          func(value *approvalEnvelope) { value.Decision = "denied" },
		"format":            func(value *approvalEnvelope) { value.Format = DetachedApprovalFormat + "-future" },
		"approver identity": func(value *approvalEnvelope) { value.ApproverIdentity = "release-owner/secondary" },
		"plan id":           func(value *approvalEnvelope) { value.PlanID = "plan-20260829-0002" },
		"plan digest":       func(value *approvalEnvelope) { value.PlanDigest = "sha256:" + strings.Repeat("f", 64) },
		"reference":         func(value *approvalEnvelope) { value.Reference = "approval/postgres-cell-a/20260829-9999" },
		"unknown key":       func(value *approvalEnvelope) { value.KeyID = "release-key-unknown" },
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			raw := mutateAndResignApproval(t, fixture.raw, fixture.privateKey, mutate)
			if _, err := fixture.verifier.Verify(fixture.plan, raw, fixture.now); !errors.Is(err, ErrUntrustedApproval) {
				t.Fatalf("Verify error = %v, want %v", err, ErrUntrustedApproval)
			}
		})
	}

	tamperedSignature := mutateApproval(t, fixture.raw, func(value *approvalEnvelope) {
		signature, err := base64.RawURLEncoding.Strict().DecodeString(value.Signature)
		if err != nil {
			t.Fatalf("decode fixture signature: %v", err)
		}
		signature[0] ^= 0xff
		value.Signature = base64.RawURLEncoding.EncodeToString(signature)
	})
	if _, err := fixture.verifier.Verify(fixture.plan, tamperedSignature, fixture.now); !errors.Is(err, ErrUntrustedApproval) {
		t.Fatalf("signature tamper error = %v, want %v", err, ErrUntrustedApproval)
	}

	otherPlanInput := validPlanInput()
	otherPlanInput.PlanID = "plan-20260829-0002"
	otherPlan, err := BuildPlan(otherPlanInput)
	if err != nil {
		t.Fatalf("BuildPlan other: %v", err)
	}
	if _, err := fixture.verifier.Verify(otherPlan, fixture.raw, fixture.now); !errors.Is(err, ErrUntrustedApproval) {
		t.Fatalf("wrong plan error = %v, want %v", err, ErrUntrustedApproval)
	}
	wrongIdentityKey := fixture.trustedKey
	wrongIdentityKey.ApproverIdentity = "release-owner/secondary"
	wrongIdentityVerifier, err := NewApprovalVerifier([]ApprovalVerificationKey{wrongIdentityKey}, 0)
	if err != nil {
		t.Fatalf("NewApprovalVerifier wrong identity: %v", err)
	}
	if _, err := wrongIdentityVerifier.Verify(fixture.plan, fixture.raw, fixture.now); !errors.Is(err, ErrUntrustedApproval) {
		t.Fatalf("key-to-approver binding error = %v, want %v", err, ErrUntrustedApproval)
	}
}

func TestApprovalTrustPolicyRejectsOutOfScopeAndOutOfWindow(t *testing.T) {
	fixture := newApprovalFixture(t, 0)
	tests := map[string]func(*ApprovalVerificationKey){
		"deployment": func(key *ApprovalVerificationKey) {
			key.Scope.DeploymentID = "wanwork-im-prod-b"
		},
		"cell": func(key *ApprovalVerificationKey) {
			key.Scope.CellID = "postgres-cell-b"
		},
		"reference namespace": func(key *ApprovalVerificationKey) {
			key.Scope.ReferencePrefix = "approval/postgres-cell-b/"
		},
		"approval before key validity": func(key *ApprovalVerificationKey) {
			key.NotBefore = fixture.approvedAt.Add(time.Second)
		},
		"approval at key expiry": func(key *ApprovalVerificationKey) {
			key.NotAfter = fixture.approvedAt
		},
		"approval expires after key": func(key *ApprovalVerificationKey) {
			key.NotAfter = fixture.expiresAt.Add(-time.Second)
		},
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			key := fixture.trustedKey
			mutate(&key)
			verifier, err := NewApprovalVerifier([]ApprovalVerificationKey{key}, 0)
			if err != nil {
				t.Fatalf("NewApprovalVerifier: %v", err)
			}
			if _, err := verifier.Verify(fixture.plan, fixture.raw, fixture.now); !errors.Is(err, ErrUntrustedApproval) {
				t.Fatalf("Verify error = %v, want %v", err, ErrUntrustedApproval)
			}
		})
	}
}

func TestApprovalRejectsAmbiguousReferenceNamespaces(t *testing.T) {
	fixture := newApprovalFixture(t, 0)
	invalidReferences := []string{
		"approval/postgres-cell-a/../release-1",
		"approval/postgres-cell-a/./release-1",
		"approval/postgres-cell-a//release-1",
		"approval/postgres-cell-a/",
	}
	for _, reference := range invalidReferences {
		t.Run("plan-"+strings.ReplaceAll(reference, "/", "-"), func(t *testing.T) {
			input := validPlanInput()
			input.ApprovalReference = reference
			if _, err := BuildPlan(input); !errors.Is(err, ErrInvalidPlan) {
				t.Fatalf("BuildPlan error = %v, want %v", err, ErrInvalidPlan)
			}
		})
	}
	invalidPrefixes := []string{
		"approval/postgres-cell-a/../",
		"approval/postgres-cell-a/./",
		"approval/postgres-cell-a//",
		"approval/postgres-cell-a",
	}
	for _, prefix := range invalidPrefixes {
		t.Run("policy-"+strings.ReplaceAll(prefix, "/", "-"), func(t *testing.T) {
			key := fixture.trustedKey
			key.Scope.ReferencePrefix = prefix
			if _, err := NewApprovalVerifier([]ApprovalVerificationKey{key}, 0); !errors.Is(err, ErrInvalidApprovalVerifier) {
				t.Fatalf("NewApprovalVerifier error = %v, want %v", err, ErrInvalidApprovalVerifier)
			}
		})
	}
}

func TestApprovalEnforcesShortLivedUTCWindowAndClockSkew(t *testing.T) {
	plan, err := BuildPlan(validPlanInput())
	if err != nil {
		t.Fatalf("BuildPlan: %v", err)
	}
	base := time.Date(2026, 8, 29, 23, 30, 0, 0, time.UTC)
	tests := map[string]struct {
		approvedAt time.Time
		expiresAt  time.Time
	}{
		"zero approved":        {expiresAt: base.Add(time.Minute)},
		"zero expiry":          {approvedAt: base},
		"equal times":          {approvedAt: base, expiresAt: base},
		"negative window":      {approvedAt: base, expiresAt: base.Add(-time.Second)},
		"long window":          {approvedAt: base, expiresAt: base.Add(maximumApprovalLifetime + time.Second)},
		"expires after plan":   {approvedAt: base, expiresAt: plan.Snapshot().ExpiresAt.Add(time.Second)},
		"approved nanoseconds": {approvedAt: base.Add(time.Nanosecond), expiresAt: base.Add(time.Minute)},
		"expiry nanoseconds":   {approvedAt: base, expiresAt: base.Add(time.Minute + time.Nanosecond)},
		"non UTC": {
			approvedAt: base.In(time.FixedZone("controller", 8*60*60)),
			expiresAt:  base.Add(time.Minute).In(time.FixedZone("controller", 8*60*60)),
		},
	}
	for name, test := range tests {
		t.Run(name, func(t *testing.T) {
			if _, err := NewApprovalToSign(plan, "release-key-2026-08", test.approvedAt, test.expiresAt); !errors.Is(err, ErrInvalidApproval) {
				t.Fatalf("NewApprovalToSign error = %v, want %v", err, ErrInvalidApproval)
			}
		})
	}
	planExpiry := plan.Snapshot().ExpiresAt
	if _, err := NewApprovalToSign(
		plan,
		"release-key-2026-08",
		planExpiry.Add(-maximumApprovalLifetime),
		planExpiry,
	); err != nil {
		t.Fatalf("exact maximum lifetime ending at plan expiry should be accepted: %v", err)
	}
	if _, err := NewApprovalToSign(
		plan,
		"Release Key",
		planExpiry.Add(-maximumApprovalLifetime),
		planExpiry,
	); !errors.Is(err, ErrInvalidApproval) {
		t.Fatalf("plan-expiry equality bypassed approval shape: %v", err)
	}

	fixture := newApprovalFixture(t, 2*time.Minute)
	if _, err := fixture.verifier.Verify(fixture.plan, fixture.raw, fixture.approvedAt.Add(-2*time.Minute)); err != nil {
		t.Fatalf("bounded future clock skew should be accepted: %v", err)
	}
	if _, err := fixture.verifier.Verify(fixture.plan, fixture.raw, fixture.approvedAt.Add(-2*time.Minute-time.Nanosecond)); !errors.Is(err, ErrUntrustedApproval) {
		t.Fatalf("excess future skew error = %v, want %v", err, ErrUntrustedApproval)
	}
	if _, err := fixture.verifier.Verify(fixture.plan, fixture.raw, fixture.expiresAt.Add(2*time.Minute-time.Nanosecond)); err != nil {
		t.Fatalf("bounded expiry skew should be accepted: %v", err)
	}
	if _, err := fixture.verifier.Verify(fixture.plan, fixture.raw, fixture.expiresAt.Add(2*time.Minute)); !errors.Is(err, ErrExpiredApproval) {
		t.Fatalf("expiry boundary error = %v, want %v", err, ErrExpiredApproval)
	}
}

func TestApprovalDecoderRejectsAmbiguousMalformedAndOversizedInput(t *testing.T) {
	fixture := newApprovalFixture(t, 0)
	keyIDFragment := []byte(`"keyId":"release-key-2026-08"`)
	tests := map[string][]byte{
		"empty":         nil,
		"null":          []byte(`null`),
		"array":         []byte(`[]`),
		"scalar":        []byte(`true`),
		"unknown field": bytes.Replace(fixture.raw, []byte(`{"algorithm"`), []byte(`{"unknown":true,"algorithm"`), 1),
		"duplicate key": bytes.Replace(fixture.raw, keyIDFragment, append(slices.Clone(keyIDFragment), []byte(`,"keyId":"release-key-2026-08"`)...), 1),
		"escaped duplicate key": bytes.Replace(
			fixture.raw,
			keyIDFragment,
			append(slices.Clone(keyIDFragment), []byte(`,"key\u0049d":"release-key-2026-08"`)...),
			1,
		),
		"trailing value":     append(slices.Clone(fixture.raw), []byte(` {}`)...),
		"trailing newline":   append(slices.Clone(fixture.raw), '\n'),
		"leading whitespace": append([]byte{' '}, fixture.raw...),
		"null signature":     bytes.Replace(fixture.raw, []byte(`"signature":"`+approvalSignature(t, fixture.raw)+`"`), []byte(`"signature":null`), 1),
		"non utf8":           append(slices.Clone(fixture.raw), 0xff),
		"non canonical signature": mutateApproval(t, fixture.raw, func(value *approvalEnvelope) {
			value.Signature += "="
		}),
	}
	for name, raw := range tests {
		t.Run(name, func(t *testing.T) {
			if _, err := fixture.verifier.Verify(fixture.plan, raw, fixture.now); !errors.Is(err, ErrInvalidApproval) {
				t.Fatalf("Verify error = %v, want %v", err, ErrInvalidApproval)
			}
		})
	}
	oversized := bytes.Repeat([]byte{'x'}, maximumApprovalBytes+1)
	if _, err := fixture.verifier.Verify(fixture.plan, oversized, fixture.now); !errors.Is(err, ErrApprovalTooLarge) {
		t.Fatalf("oversized error = %v, want %v", err, ErrApprovalTooLarge)
	}
}

func TestApprovalConstructionAndErrorsDoNotExposeSignedMaterial(t *testing.T) {
	fixture := newApprovalFixture(t, 0)
	toSign, err := NewApprovalToSign(fixture.plan, "release-key-2026-08", fixture.approvedAt, fixture.expiresAt)
	if err != nil {
		t.Fatalf("NewApprovalToSign: %v", err)
	}
	for _, signature := range [][]byte{nil, make([]byte, ed25519.SignatureSize-1), make([]byte, ed25519.SignatureSize+1)} {
		if _, err := toSign.Encode(signature); !errors.Is(err, ErrInvalidApproval) {
			t.Fatalf("Encode error = %v, want %v", err, ErrInvalidApproval)
		}
	}

	signatureCanary := approvalSignature(t, fixture.raw)
	privateCanary := base64.RawURLEncoding.EncodeToString(fixture.privateKey)
	invalid := mutateApproval(t, fixture.raw, func(value *approvalEnvelope) { value.Signature = "signature-canary" })
	_, verifyErr := fixture.verifier.Verify(fixture.plan, invalid, fixture.now)
	for _, rendered := range []string{fmt.Sprint(verifyErr), fmt.Sprintf("%+v", verifyErr)} {
		if strings.Contains(rendered, signatureCanary) || strings.Contains(rendered, privateCanary) ||
			strings.Contains(rendered, "signature-canary") || strings.Contains(rendered, string(fixture.raw)) {
			t.Fatalf("approval error disclosed signed or private material: %q", rendered)
		}
	}
}

func newApprovalFixture(t *testing.T, clockSkew time.Duration) approvalFixture {
	t.Helper()
	plan, err := BuildPlan(validPlanInput())
	if err != nil {
		t.Fatalf("BuildPlan: %v", err)
	}
	seed := bytes.Repeat([]byte{0x42}, ed25519.SeedSize)
	privateKey := ed25519.NewKeyFromSeed(seed)
	publicKey := slices.Clone(privateKey.Public().(ed25519.PublicKey))
	approvedAt := time.Date(2026, 8, 29, 23, 40, 0, 0, time.UTC)
	expiresAt := approvedAt.Add(10 * time.Minute)
	toSign, err := NewApprovalToSign(plan, "release-key-2026-08", approvedAt, expiresAt)
	if err != nil {
		t.Fatalf("NewApprovalToSign: %v", err)
	}
	raw, err := toSign.Encode(ed25519.Sign(privateKey, toSign.SigningBytes()))
	if err != nil {
		t.Fatalf("Encode: %v", err)
	}
	trustedKey := ApprovalVerificationKey{
		ApproverIdentity: "release-owner/primary",
		Generation:       "generation-1",
		KeyID:            "release-key-2026-08",
		NotAfter:         time.Date(2026, 9, 1, 0, 0, 0, 0, time.UTC),
		NotBefore:        time.Date(2026, 8, 1, 0, 0, 0, 0, time.UTC),
		PolicyRevision:   "policy/release-approvers/revision-1",
		PublicKey:        publicKey,
		Scope: ApprovalVerificationScope{
			CellID:          "postgres-cell-a",
			DeploymentID:    "wanwork-im-prod-a",
			ReferencePrefix: "approval/postgres-cell-a/",
		},
	}
	verifier, err := NewApprovalVerifier([]ApprovalVerificationKey{trustedKey}, clockSkew)
	if err != nil {
		t.Fatalf("NewApprovalVerifier: %v", err)
	}
	return approvalFixture{
		plan:       plan,
		publicKey:  publicKey,
		privateKey: privateKey,
		trustedKey: trustedKey,
		verifier:   verifier,
		approvedAt: approvedAt,
		expiresAt:  expiresAt,
		now:        approvedAt.Add(5 * time.Minute),
		raw:        raw,
	}
}

func mutateAndResignApproval(
	t *testing.T,
	raw []byte,
	privateKey ed25519.PrivateKey,
	mutate func(*approvalEnvelope),
) []byte {
	t.Helper()
	var envelope approvalEnvelope
	if err := json.Unmarshal(raw, &envelope); err != nil {
		t.Fatalf("decode approval fixture: %v", err)
	}
	mutate(&envelope)
	envelope.Signature = ""
	unsigned, err := marshalApprovalCanonical(envelope)
	if err != nil {
		t.Fatalf("marshal unsigned approval: %v", err)
	}
	envelope.Signature = base64.RawURLEncoding.EncodeToString(
		ed25519.Sign(privateKey, approvalSigningMessage(unsigned)),
	)
	canonical, err := marshalApprovalCanonical(envelope)
	if err != nil {
		t.Fatalf("marshal signed approval: %v", err)
	}
	return canonical
}

func mutateApproval(t *testing.T, raw []byte, mutate func(*approvalEnvelope)) []byte {
	t.Helper()
	var envelope approvalEnvelope
	if err := json.Unmarshal(raw, &envelope); err != nil {
		t.Fatalf("decode approval fixture: %v", err)
	}
	mutate(&envelope)
	canonical, err := marshalApprovalCanonical(envelope)
	if err != nil {
		t.Fatalf("marshal approval: %v", err)
	}
	return canonical
}

func approvalSignature(t *testing.T, raw []byte) string {
	t.Helper()
	var envelope approvalEnvelope
	if err := json.Unmarshal(raw, &envelope); err != nil {
		t.Fatalf("decode approval fixture: %v", err)
	}
	return envelope.Signature
}
