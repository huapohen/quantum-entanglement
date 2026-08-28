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

type approvalPolicyFixture struct {
	bundle     ApprovalPolicyRootTrustBundle
	input      ApprovalPolicyInput
	now        time.Time
	onlineKeys []ed25519.PrivateKey
	raw        []byte
	rootKeys   []ed25519.PrivateKey
	toSign     ApprovalPolicyToSign
	verifier   ApprovalPolicyVerifier
}

func TestApprovalPolicyDeterministicQuorumRoundTripAndEvidenceBoundary(t *testing.T) {
	fixture := newApprovalPolicyFixture(t)
	verified, err := fixture.verifier.Verify(fixture.raw, fixture.now)
	if err != nil {
		t.Fatalf("Verify: %v", err)
	}
	if !slices.Equal(verified.CanonicalBytes(), fixture.raw) ||
		verified.PolicyDigest() != fixture.toSign.PolicyDigest() ||
		verified.EnvelopeDigest() != digestApprovalPolicyEnvelope(fixture.raw) ||
		verified.PolicyID() != fixture.input.PolicyID || verified.Revision() != fixture.input.Revision ||
		verified.PreviousPolicyDigest() != "" ||
		verified.RootTrustBundleDigest() != fixture.verifier.bundleDigest ||
		len(verified.RootFingerprints()) != fixture.bundle.Quorum {
		t.Fatalf("verified policy evidence is incomplete: %+v", verified)
	}
	if verified.PolicyDigest() != "sha256:f8ee520f9ef7c14a5db09f883400ecc60f3e55eed2cf2b73c1e19edd996931dd" {
		t.Fatalf("policy golden digest changed: %s", verified.PolicyDigest())
	}
	if verified.RootTrustBundleDigest() != "sha256:12dd7b2d8f31dbdf14785677af81040348153784bf2e1d3785693e09a0d4bb58" {
		t.Fatalf("root bundle golden digest changed: %s", verified.RootTrustBundleDigest())
	}

	copyBytes := verified.CanonicalBytes()
	copyBytes[0] ^= 0xff
	copyRoots := verified.RootFingerprints()
	copyRoots[0] = strings.Repeat("x", len(copyRoots[0]))
	snapshot := verified.Snapshot()
	snapshot.Keys[0].KeyID = "mutated"
	snapshot.RootSignatures[0].Signature = "mutated"
	if !slices.Equal(verified.CanonicalBytes(), fixture.raw) ||
		verified.Snapshot().Keys[0].KeyID == "mutated" ||
		verified.Snapshot().RootSignatures[0].Signature == "mutated" ||
		verified.RootFingerprints()[0] == copyRoots[0] {
		t.Fatal("caller mutation escaped the verified policy boundary")
	}
	encoded, err := json.Marshal(verified)
	if err != nil || string(encoded) != "{}" {
		t.Fatalf("VerifiedApprovalPolicy leaked authenticated material: %s, %v", encoded, err)
	}
	typeOfVerified := reflect.TypeOf(verified)
	for index := range typeOfVerified.NumField() {
		if typeOfVerified.Field(index).IsExported() {
			t.Fatalf("VerifiedApprovalPolicy exposes field %q", typeOfVerified.Field(index).Name)
		}
	}
	for _, rootSignature := range verified.Snapshot().RootSignatures {
		if strings.Contains(fmt.Sprintf("%+v", verified), rootSignature.Signature) {
			t.Fatal("VerifiedApprovalPolicy representation disclosed a reusable root signature")
		}
	}

	reversedInput := fixture.input
	reversedInput.Keys = slices.Clone(fixture.input.Keys)
	slices.Reverse(reversedInput.Keys)
	second, err := NewApprovalPolicyToSign(reversedInput)
	if err != nil {
		t.Fatalf("NewApprovalPolicyToSign reversed: %v", err)
	}
	if !slices.Equal(second.SigningBytes(), fixture.toSign.SigningBytes()) {
		t.Fatal("semantic key-set order changed policy signing bytes")
	}
	reversedSignatures := signApprovalPolicy(t, second, fixture.rootKeys, []int{1, 0})
	if !slices.Equal(reversedSignatures, fixture.raw) {
		t.Fatal("semantic signature-set order changed canonical policy")
	}
}

func TestApprovalPolicySignatureRequiresDistinctPinnedRootQuorumAndDomain(t *testing.T) {
	fixture := newApprovalPolicyFixture(t)
	wrongMessages := map[string][]byte{
		"bare canonical": bytes.TrimPrefix(fixture.toSign.SigningBytes(), []byte(approvalPolicySignatureDomain)),
		"approval domain": append(
			[]byte(approvalSignatureDomain),
			bytes.TrimPrefix(fixture.toSign.SigningBytes(), []byte(approvalPolicySignatureDomain))...,
		),
		"content digest domain": append(
			[]byte(approvalPolicyDigestDomain),
			bytes.TrimPrefix(fixture.toSign.SigningBytes(), []byte(approvalPolicySignatureDomain))...,
		),
	}
	for name, message := range wrongMessages {
		t.Run(name, func(t *testing.T) {
			raw, err := fixture.toSign.Encode([]ApprovalPolicyDetachedSignature{
				{RootKeyID: fixture.bundle.Roots[0].RootKeyID, Signature: ed25519.Sign(fixture.rootKeys[0], message)},
				{RootKeyID: fixture.bundle.Roots[1].RootKeyID, Signature: ed25519.Sign(fixture.rootKeys[1], message)},
			})
			if err != nil {
				t.Fatalf("Encode: %v", err)
			}
			if _, err := fixture.verifier.Verify(raw, fixture.now); !errors.Is(err, ErrUntrustedApprovalPolicy) {
				t.Fatalf("Verify error = %v, want %v", err, ErrUntrustedApprovalPolicy)
			}
		})
	}

	oneSignature := signApprovalPolicy(t, fixture.toSign, fixture.rootKeys, []int{0})
	if _, err := fixture.verifier.Verify(oneSignature, fixture.now); !errors.Is(err, ErrUntrustedApprovalPolicy) {
		t.Fatalf("sub-quorum policy error = %v, want %v", err, ErrUntrustedApprovalPolicy)
	}
	duplicate := []ApprovalPolicyDetachedSignature{
		{RootKeyID: fixture.bundle.Roots[0].RootKeyID, Signature: ed25519.Sign(fixture.rootKeys[0], fixture.toSign.SigningBytes())},
		{RootKeyID: fixture.bundle.Roots[0].RootKeyID, Signature: ed25519.Sign(fixture.rootKeys[0], fixture.toSign.SigningBytes())},
	}
	if _, err := fixture.toSign.Encode(duplicate); !errors.Is(err, ErrInvalidApprovalPolicy) {
		t.Fatalf("duplicate signer error = %v, want %v", err, ErrInvalidApprovalPolicy)
	}

	attackerPrivate := deterministicEd25519PrivateKey(0xe1)
	unknown, err := fixture.toSign.Encode([]ApprovalPolicyDetachedSignature{
		{RootKeyID: "root-key-attacker", Signature: ed25519.Sign(attackerPrivate, fixture.toSign.SigningBytes())},
		{RootKeyID: fixture.bundle.Roots[0].RootKeyID, Signature: ed25519.Sign(fixture.rootKeys[0], fixture.toSign.SigningBytes())},
	})
	if err != nil {
		t.Fatalf("Encode unknown root: %v", err)
	}
	if _, err := fixture.verifier.Verify(unknown, fixture.now); !errors.Is(err, ErrUntrustedApprovalPolicy) {
		t.Fatalf("unknown root error = %v, want %v", err, ErrUntrustedApprovalPolicy)
	}
}

func TestApprovalPolicyStrictCodecRejectsAmbiguousAndNonCanonicalBytes(t *testing.T) {
	fixture := newApprovalPolicyFixture(t)
	duplicate := bytes.Replace(
		fixture.raw,
		[]byte(`{"approvalClockSkewSeconds":30,`),
		[]byte(`{"approvalClockSkewSeconds":30,"approvalClockSkewSeconds":30,`),
		1,
	)
	escapedDuplicate := bytes.Replace(
		fixture.raw,
		[]byte(`{"approvalClockSkewSeconds":30,`),
		[]byte(`{"approvalClockSkewSeconds":30,"\u0061pprovalClockSkewSeconds":30,`),
		1,
	)
	unknown := bytes.Replace(
		fixture.raw,
		[]byte(`{"approvalClockSkewSeconds":30,`),
		[]byte(`{"approvalClockSkewSeconds":30,"unknown":false,`),
		1,
	)
	invalidUTF8 := slices.Clone(fixture.raw)
	invalidUTF8[len(invalidUTF8)/2] = 0xff
	tests := map[string][]byte{
		"empty":               nil,
		"scalar":              []byte(`1`),
		"array":               []byte(`[]`),
		"null":                []byte(`null`),
		"duplicate":           duplicate,
		"escaped duplicate":   escapedDuplicate,
		"unknown":             unknown,
		"trailing whitespace": append(slices.Clone(fixture.raw), '\n'),
		"trailing value":      append(slices.Clone(fixture.raw), []byte(`{}`)...),
		"invalid UTF-8":       invalidUTF8,
		"truncated":           fixture.raw[:len(fixture.raw)-1],
	}
	for name, raw := range tests {
		t.Run(name, func(t *testing.T) {
			if _, _, err := decodeApprovalPolicy(raw); !errors.Is(err, ErrInvalidApprovalPolicy) {
				t.Fatalf("decode error = %v, want %v", err, ErrInvalidApprovalPolicy)
			}
		})
	}
	oversized := bytes.Repeat([]byte{'x'}, maximumApprovalPolicyBytes+1)
	if _, _, err := decodeApprovalPolicy(oversized); !errors.Is(err, ErrApprovalPolicyTooLarge) {
		t.Fatalf("oversized error = %v, want %v", err, ErrApprovalPolicyTooLarge)
	}

	var reordered ApprovalPolicySnapshot
	if err := json.Unmarshal(fixture.raw, &reordered); err != nil {
		t.Fatalf("decode fixture: %v", err)
	}
	slices.Reverse(reordered.Keys)
	reorderedRaw, err := marshalApprovalPolicyCanonical(reordered)
	if err != nil {
		t.Fatalf("marshal reordered keys: %v", err)
	}
	if _, _, err := decodeApprovalPolicy(reorderedRaw); !errors.Is(err, ErrInvalidApprovalPolicy) {
		t.Fatalf("reordered key error = %v, want %v", err, ErrInvalidApprovalPolicy)
	}
	slices.Reverse(reordered.Keys)
	slices.Reverse(reordered.RootSignatures)
	reorderedRaw, err = marshalApprovalPolicyCanonical(reordered)
	if err != nil {
		t.Fatalf("marshal reordered signatures: %v", err)
	}
	if _, _, err := decodeApprovalPolicy(reorderedRaw); !errors.Is(err, ErrInvalidApprovalPolicy) {
		t.Fatalf("reordered signature error = %v, want %v", err, ErrInvalidApprovalPolicy)
	}
}

func TestApprovalPolicyRejectsSemanticScopeTimeKeyAndRootDrift(t *testing.T) {
	fixture := newApprovalPolicyFixture(t)
	mutations := map[string]func(*ApprovalPolicySnapshot){
		"policy id":  func(value *ApprovalPolicySnapshot) { value.PolicyID = "approval-policy/other-cell" },
		"deployment": func(value *ApprovalPolicySnapshot) { value.Target.DeploymentID = "wanwork-im-prod-b" },
		"cell":       func(value *ApprovalPolicySnapshot) { value.Target.CellID = "postgres-cell-b" },
		"database":   func(value *ApprovalPolicySnapshot) { value.Target.Database = "wanwork_im_other" },
		"cluster": func(value *ApprovalPolicySnapshot) {
			value.Target.SystemIdentifierDigest = "sha256:" + strings.Repeat("f", 64)
		},
		"server":      func(value *ApprovalPolicySnapshot) { value.Target.ServerIdentity = "postgres-b.internal.example" },
		"ca":          func(value *ApprovalPolicySnapshot) { value.Target.CADigest = "sha256:" + strings.Repeat("e", 64) },
		"plan format": func(value *ApprovalPolicySnapshot) { value.Target.PlanFormat = "future" },
	}
	for name, mutate := range mutations {
		t.Run(name, func(t *testing.T) {
			var snapshot ApprovalPolicySnapshot
			if err := json.Unmarshal(fixture.raw, &snapshot); err != nil {
				t.Fatalf("decode fixture: %v", err)
			}
			mutate(&snapshot)
			raw := resignApprovalPolicySnapshot(t, snapshot, fixture.rootKeys, []int{0, 1})
			if _, err := fixture.verifier.Verify(raw, fixture.now); !errors.Is(err, ErrUntrustedApprovalPolicy) &&
				!errors.Is(err, ErrInvalidApprovalPolicy) {
				t.Fatalf("Verify error = %v, want untrusted/invalid policy", err)
			}
		})
	}

	if _, err := fixture.verifier.Verify(fixture.raw, fixture.input.NotBefore.Add(-time.Minute)); !errors.Is(err, ErrApprovalPolicyNotActive) {
		t.Fatalf("not-yet-active error = %v, want %v", err, ErrApprovalPolicyNotActive)
	}
	if _, err := fixture.verifier.Verify(fixture.raw, fixture.input.NotAfter.Add(time.Minute)); !errors.Is(err, ErrExpiredApprovalPolicy) {
		t.Fatalf("expired error = %v, want %v", err, ErrExpiredApprovalPolicy)
	}
	if _, err := (ApprovalPolicyVerifier{}).Verify(fixture.raw, fixture.now); !errors.Is(err, ErrInvalidApprovalPolicyVerifier) {
		t.Fatalf("zero verifier error = %v, want %v", err, ErrInvalidApprovalPolicyVerifier)
	}
}

func TestApprovalPolicyBuilderRejectsInvalidRevisionInventoryAndWindows(t *testing.T) {
	fixture := newApprovalPolicyFixture(t)
	tests := map[string]func(*ApprovalPolicyInput){
		"zero revision":                    func(value *ApprovalPolicyInput) { value.Revision = 0 },
		"genesis previous":                 func(value *ApprovalPolicyInput) { value.PreviousPolicyDigest = "sha256:" + strings.Repeat("a", 64) },
		"future revision missing previous": func(value *ApprovalPolicyInput) { value.Revision = 2 },
		"issued after activation":          func(value *ApprovalPolicyInput) { value.IssuedAt = value.NotBefore.Add(time.Second) },
		"policy window inverted":           func(value *ApprovalPolicyInput) { value.NotAfter = value.NotBefore },
		"policy too long": func(value *ApprovalPolicyInput) {
			value.NotAfter = value.NotBefore.Add(maximumApprovalPolicyLifetime + time.Second)
		},
		"fractional skew": func(value *ApprovalPolicyInput) { value.ApprovalClockSkew = time.Nanosecond },
		"excessive skew":  func(value *ApprovalPolicyInput) { value.ApprovalClockSkew = maximumApprovalClockSkew + time.Second },
		"approval lifetime": func(value *ApprovalPolicyInput) {
			value.MaximumApprovalLifetime = maximumApprovalLifetime + time.Second
		},
		"duplicate key id":       func(value *ApprovalPolicyInput) { value.Keys[1].KeyID = value.Keys[0].KeyID },
		"duplicate key":          func(value *ApprovalPolicyInput) { value.Keys[1].PublicKey = value.Keys[0].PublicKey },
		"active outside policy":  func(value *ApprovalPolicyInput) { value.Keys[1].NotAfter = value.NotAfter.Add(time.Second) },
		"revoked without reason": func(value *ApprovalPolicyInput) { value.Keys[0].RevocationReason = "" },
		"deny with active":       func(value *ApprovalPolicyInput) { value.DenyAll = true },
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			input := cloneApprovalPolicyInput(fixture.input)
			mutate(&input)
			if _, err := NewApprovalPolicyToSign(input); !errors.Is(err, ErrInvalidApprovalPolicy) {
				t.Fatalf("NewApprovalPolicyToSign error = %v, want %v", err, ErrInvalidApprovalPolicy)
			}
		})
	}

	denyAll := cloneApprovalPolicyInput(fixture.input)
	denyAll.DenyAll = true
	denyAll.Keys = denyAll.Keys[:1]
	if _, err := NewApprovalPolicyToSign(denyAll); err != nil {
		t.Fatalf("explicit deny-all tombstone policy: %v", err)
	}
}

func TestApprovalPolicyRootBundleIsImmutableScopedAndCannotReuseOnlineKey(t *testing.T) {
	fixture := newApprovalPolicyFixture(t)
	bundle := fixture.bundle
	bundle.Roots = slices.Clone(bundle.Roots)
	bundle.Roots[0].PublicKey = slices.Clone(bundle.Roots[0].PublicKey)
	verifier, err := NewApprovalPolicyVerifier(bundle, 0)
	if err != nil {
		t.Fatalf("NewApprovalPolicyVerifier: %v", err)
	}
	bundle.PolicyID = "approval-policy/attacker"
	bundle.Target.CellID = "postgres-cell-b"
	bundle.Roots[0].RootKeyID = "root-key-attacker"
	bundle.Roots[0].PublicKey[0] ^= 0xff
	if _, err := verifier.Verify(fixture.raw, fixture.now); err != nil {
		t.Fatalf("caller mutation changed pinned root trust: %v", err)
	}

	invalidBundles := map[string]func(*ApprovalPolicyRootTrustBundle){
		"one root quorum":    func(value *ApprovalPolicyRootTrustBundle) { value.Quorum = 1 },
		"quorum too high":    func(value *ApprovalPolicyRootTrustBundle) { value.Quorum = 4 },
		"wrong domain":       func(value *ApprovalPolicyRootTrustBundle) { value.TrustDomain = approvalPolicySignatureDomain },
		"duplicate root id":  func(value *ApprovalPolicyRootTrustBundle) { value.Roots[1].RootKeyID = value.Roots[0].RootKeyID },
		"duplicate root key": func(value *ApprovalPolicyRootTrustBundle) { value.Roots[1].PublicKey = value.Roots[0].PublicKey },
		"revoked quorum": func(value *ApprovalPolicyRootTrustBundle) {
			value.Roots[0].Revoked = true
			value.Roots[1].Revoked = true
		},
		"wrong policy": func(value *ApprovalPolicyRootTrustBundle) { value.PolicyID = "other" },
		"wrong target": func(value *ApprovalPolicyRootTrustBundle) { value.Target.PlanFormat = "future" },
	}
	for name, mutate := range invalidBundles {
		t.Run(name, func(t *testing.T) {
			candidate := cloneApprovalPolicyBundle(fixture.bundle)
			mutate(&candidate)
			if _, err := NewApprovalPolicyVerifier(candidate, 0); !errors.Is(err, ErrInvalidApprovalPolicyVerifier) {
				t.Fatalf("NewApprovalPolicyVerifier error = %v, want %v", err, ErrInvalidApprovalPolicyVerifier)
			}
		})
	}

	overlap := cloneApprovalPolicyInput(fixture.input)
	overlap.Keys[0].PublicKey = slices.Clone(fixture.bundle.Roots[0].PublicKey)
	toSign, err := NewApprovalPolicyToSign(overlap)
	if err != nil {
		t.Fatalf("NewApprovalPolicyToSign overlap: %v", err)
	}
	raw := signApprovalPolicy(t, toSign, fixture.rootKeys, []int{0, 1})
	if _, err := fixture.verifier.Verify(raw, fixture.now); !errors.Is(err, ErrUntrustedApprovalPolicy) {
		t.Fatalf("root/online key reuse error = %v, want %v", err, ErrUntrustedApprovalPolicy)
	}
}

func TestApprovalPolicyContentChainIdentityIsIndependentOfRootCosignatures(t *testing.T) {
	fixture := newApprovalPolicyFixture(t)
	revisionTwo := cloneApprovalPolicyInput(fixture.input)
	revisionTwo.Revision = 2
	revisionTwo.PreviousPolicyDigest = fixture.toSign.PolicyDigest()
	revisionTwo.IssuedAt = revisionTwo.IssuedAt.Add(time.Hour)
	revisionTwo.NotBefore = revisionTwo.NotBefore.Add(time.Hour)
	revisionTwo.Keys[0].RevokedAt = revisionTwo.NotBefore
	toSign, err := NewApprovalPolicyToSign(revisionTwo)
	if err != nil {
		t.Fatalf("NewApprovalPolicyToSign revision two: %v", err)
	}
	twoRoots := signApprovalPolicy(t, toSign, fixture.rootKeys, []int{0, 1})
	threeRoots := signApprovalPolicy(t, toSign, fixture.rootKeys, []int{0, 1, 2})
	verifiedTwo, err := fixture.verifier.Verify(twoRoots, revisionTwo.NotBefore.Add(time.Minute))
	if err != nil {
		t.Fatalf("Verify two roots: %v", err)
	}
	verifiedThree, err := fixture.verifier.Verify(threeRoots, revisionTwo.NotBefore.Add(time.Minute))
	if err != nil {
		t.Fatalf("Verify three roots: %v", err)
	}
	if verifiedTwo.PolicyDigest() != verifiedThree.PolicyDigest() ||
		verifiedTwo.EnvelopeDigest() == verifiedThree.EnvelopeDigest() ||
		verifiedTwo.PreviousPolicyDigest() != fixture.toSign.PolicyDigest() ||
		verifiedTwo.Snapshot().Keys[1].NotBefore != fixture.input.Keys[1].NotBefore {
		t.Fatal("policy content identity or envelope evidence semantics are incorrect")
	}
}

func newApprovalPolicyFixture(t *testing.T) approvalPolicyFixture {
	t.Helper()
	plan, err := BuildPlan(validPlanInput())
	if err != nil {
		t.Fatalf("BuildPlan: %v", err)
	}
	target, err := ApprovalPolicyTargetFromPlan(plan)
	if err != nil {
		t.Fatalf("ApprovalPolicyTargetFromPlan: %v", err)
	}
	rootKeys := []ed25519.PrivateKey{
		deterministicEd25519PrivateKey(0xa1),
		deterministicEd25519PrivateKey(0xa2),
		deterministicEd25519PrivateKey(0xa3),
	}
	onlineKeys := []ed25519.PrivateKey{
		deterministicEd25519PrivateKey(0xb1),
		deterministicEd25519PrivateKey(0xb2),
	}
	issuedAt := time.Date(2026, 8, 29, 22, 0, 0, 0, time.UTC)
	notBefore := time.Date(2026, 8, 29, 23, 0, 0, 0, time.UTC)
	notAfter := time.Date(2026, 9, 15, 23, 0, 0, 0, time.UTC)
	input := ApprovalPolicyInput{
		ApprovalClockSkew:       30 * time.Second,
		IssuedAt:                issuedAt,
		MaximumApprovalLifetime: 10 * time.Minute,
		NotAfter:                notAfter,
		NotBefore:               notBefore,
		PolicyID:                "approval-policy/postgres-cell-a",
		Revision:                1,
		Target:                  target,
		Keys: []ApprovalPolicyKey{
			{
				ApproverIdentity: "release-owner/retired",
				Generation:       "generation-1",
				KeyID:            "release-key-2026-07",
				NotAfter:         time.Date(2026, 9, 30, 0, 0, 0, 0, time.UTC),
				NotBefore:        time.Date(2026, 8, 1, 0, 0, 0, 0, time.UTC),
				PublicKey:        onlineKeys[1].Public().(ed25519.PublicKey),
				ReferencePrefix:  "approval/postgres-cell-a/",
				RevocationReason: "revocation/routine-rotation",
				RevokedAt:        notBefore,
				Status:           ApprovalPolicyKeyRevoked,
			},
			{
				ApproverIdentity: "release-owner/primary",
				Generation:       "generation-2",
				KeyID:            "release-key-2026-08",
				NotAfter:         time.Date(2026, 9, 10, 0, 0, 0, 0, time.UTC),
				NotBefore:        notBefore,
				PublicKey:        onlineKeys[0].Public().(ed25519.PublicKey),
				ReferencePrefix:  "approval/postgres-cell-a/",
				Status:           ApprovalPolicyKeyActive,
			},
		},
	}
	toSign, err := NewApprovalPolicyToSign(input)
	if err != nil {
		t.Fatalf("NewApprovalPolicyToSign: %v", err)
	}
	bundle := ApprovalPolicyRootTrustBundle{
		BundleID:    "approval-policy-root-bundle/postgres-cell-a",
		PolicyID:    input.PolicyID,
		Quorum:      2,
		Revision:    1,
		Target:      target,
		TrustDomain: ApprovalPolicyRootTrustDomain,
		Roots: []ApprovalPolicyTrustRoot{
			{
				Generation: "generation-3",
				NotAfter:   time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC),
				NotBefore:  time.Date(2026, 8, 1, 0, 0, 0, 0, time.UTC),
				PublicKey:  rootKeys[2].Public().(ed25519.PublicKey),
				RootKeyID:  "root-key-2026-c",
			},
			{
				Generation: "generation-1",
				NotAfter:   time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC),
				NotBefore:  time.Date(2026, 8, 1, 0, 0, 0, 0, time.UTC),
				PublicKey:  rootKeys[0].Public().(ed25519.PublicKey),
				RootKeyID:  "root-key-2026-a",
			},
			{
				Generation: "generation-2",
				NotAfter:   time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC),
				NotBefore:  time.Date(2026, 8, 1, 0, 0, 0, 0, time.UTC),
				PublicKey:  rootKeys[1].Public().(ed25519.PublicKey),
				RootKeyID:  "root-key-2026-b",
			},
		},
	}
	verifier, err := NewApprovalPolicyVerifier(bundle, 0)
	if err != nil {
		t.Fatalf("NewApprovalPolicyVerifier: %v", err)
	}
	raw := signApprovalPolicy(t, toSign, rootKeys, []int{0, 1})
	return approvalPolicyFixture{
		bundle:     bundle,
		input:      input,
		now:        notBefore.Add(time.Hour),
		onlineKeys: onlineKeys,
		raw:        raw,
		rootKeys:   rootKeys,
		toSign:     toSign,
		verifier:   verifier,
	}
}

func deterministicEd25519PrivateKey(value byte) ed25519.PrivateKey {
	return ed25519.NewKeyFromSeed(bytes.Repeat([]byte{value}, ed25519.SeedSize))
}

func signApprovalPolicy(
	t *testing.T,
	toSign ApprovalPolicyToSign,
	rootKeys []ed25519.PrivateKey,
	rootIndexes []int,
) []byte {
	t.Helper()
	signatures := make([]ApprovalPolicyDetachedSignature, len(rootIndexes))
	for index, rootIndex := range rootIndexes {
		signatures[index] = ApprovalPolicyDetachedSignature{
			RootKeyID: fmt.Sprintf("root-key-2026-%c", 'a'+rune(rootIndex)),
			Signature: ed25519.Sign(rootKeys[rootIndex], toSign.SigningBytes()),
		}
	}
	raw, err := toSign.Encode(signatures)
	if err != nil {
		t.Fatalf("Encode policy: %v", err)
	}
	return raw
}

func resignApprovalPolicySnapshot(
	t *testing.T,
	snapshot ApprovalPolicySnapshot,
	rootKeys []ed25519.PrivateKey,
	rootIndexes []int,
) []byte {
	t.Helper()
	snapshot.PolicyDigest = ""
	snapshot.RootSignatures = []ApprovalPolicyRootSignature{}
	digestCanonical, err := marshalApprovalPolicyCanonical(snapshot)
	if err != nil {
		t.Fatalf("marshal content: %v", err)
	}
	snapshot.PolicyDigest = digestApprovalPolicyContent(digestCanonical)
	unsigned, err := marshalApprovalPolicyCanonical(snapshot)
	if err != nil {
		t.Fatalf("marshal unsigned: %v", err)
	}
	message := approvalPolicySigningMessage(unsigned)
	for _, rootIndex := range rootIndexes {
		snapshot.RootSignatures = append(snapshot.RootSignatures, ApprovalPolicyRootSignature{
			Algorithm: approvalPolicyAlgorithmEd25519,
			RootKeyID: fmt.Sprintf("root-key-2026-%c", 'a'+rune(rootIndex)),
			Signature: base64.RawURLEncoding.EncodeToString(ed25519.Sign(rootKeys[rootIndex], message)),
		})
	}
	slices.SortFunc(snapshot.RootSignatures, func(left, right ApprovalPolicyRootSignature) int {
		return strings.Compare(left.RootKeyID, right.RootKeyID)
	})
	raw, err := marshalApprovalPolicyCanonical(snapshot)
	if err != nil {
		t.Fatalf("marshal signed: %v", err)
	}
	return raw
}

func cloneApprovalPolicyInput(input ApprovalPolicyInput) ApprovalPolicyInput {
	clone := input
	clone.Keys = slices.Clone(input.Keys)
	for index := range clone.Keys {
		clone.Keys[index].PublicKey = slices.Clone(input.Keys[index].PublicKey)
	}
	return clone
}

func cloneApprovalPolicyBundle(bundle ApprovalPolicyRootTrustBundle) ApprovalPolicyRootTrustBundle {
	clone := bundle
	clone.Roots = slices.Clone(bundle.Roots)
	for index := range clone.Roots {
		clone.Roots[index].PublicKey = slices.Clone(bundle.Roots[index].PublicKey)
	}
	return clone
}
