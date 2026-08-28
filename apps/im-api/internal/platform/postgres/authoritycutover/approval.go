package authoritycutover

import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"slices"
	"strings"
	"time"
	"unicode/utf8"

	"golang.org/x/text/unicode/norm"
)

const (
	DetachedApprovalFormat       = "wanwork.im.postgres-authority-cutover-approval/1"
	approvalAlgorithmEd25519     = "ed25519"
	approvalDecisionApproved     = "approved"
	approvalSignatureDomain      = "wanwork.im/postgres-authority-cutover-approval/signature/1\n"
	approvalEvidenceDigestDomain = "wanwork.im/postgres-authority-cutover-approval/evidence/1\n"
	approvalKeyFingerprintDomain = "wanwork.im/postgres-authority-cutover-approval/public-key/1\n"
	maximumApprovalBytes         = 32 * 1024
	maximumApprovalLifetime      = 15 * time.Minute
	maximumApprovalClockSkew     = 5 * time.Minute
	maximumApprovalKeys          = 64
)

var (
	ErrInvalidApproval         = errors.New("invalid PostgreSQL authority cutover approval")
	ErrApprovalTooLarge        = errors.New("PostgreSQL authority cutover approval exceeds size limit")
	ErrInvalidApprovalVerifier = errors.New("invalid PostgreSQL authority cutover approval verifier")
	ErrUntrustedApproval       = errors.New("untrusted PostgreSQL authority cutover approval")
	ErrExpiredApproval         = errors.New("expired PostgreSQL authority cutover approval")
)

type approvalEnvelope struct {
	Algorithm        string    `json:"algorithm"`
	ApprovedAt       time.Time `json:"approvedAt"`
	ApproverIdentity string    `json:"approverIdentity"`
	Decision         string    `json:"decision"`
	ExpiresAt        time.Time `json:"expiresAt"`
	Format           string    `json:"format"`
	KeyID            string    `json:"keyId"`
	PlanDigest       string    `json:"planDigest"`
	PlanID           string    `json:"planId"`
	Reference        string    `json:"reference"`
	Signature        string    `json:"signature"`
}

// ApprovalToSign is a detached, immutable controller payload. SigningBytes includes the domain
// separator and never contains a signature or private key. Encode performs structural validation,
// but the returned envelope becomes trusted only after ApprovalVerifier.Verify authenticates it.
type ApprovalToSign struct {
	envelope     approvalEnvelope
	signingBytes []byte
}

func NewApprovalToSign(
	plan Plan,
	keyID string,
	approvedAt time.Time,
	expiresAt time.Time,
) (ApprovalToSign, error) {
	if !validPlanSnapshot(plan.snapshot, true) {
		return ApprovalToSign{}, ErrInvalidApproval
	}
	snapshot := plan.Snapshot()
	envelope := approvalEnvelope{
		Algorithm:        approvalAlgorithmEd25519,
		ApprovedAt:       approvedAt,
		ApproverIdentity: snapshot.Approval.Identity,
		Decision:         approvalDecisionApproved,
		ExpiresAt:        expiresAt,
		Format:           DetachedApprovalFormat,
		KeyID:            keyID,
		PlanDigest:       plan.Digest(),
		PlanID:           snapshot.PlanID,
		Reference:        snapshot.Approval.Reference,
	}
	if !validUnsignedApproval(envelope, snapshot) {
		return ApprovalToSign{}, ErrInvalidApproval
	}
	canonical, err := marshalApprovalCanonical(envelope)
	if err != nil {
		return ApprovalToSign{}, ErrInvalidApproval
	}
	return ApprovalToSign{
		envelope:     envelope,
		signingBytes: approvalSigningMessage(canonical),
	}, nil
}

func (approval ApprovalToSign) SigningBytes() []byte { return slices.Clone(approval.signingBytes) }

func (approval ApprovalToSign) Encode(signature []byte) ([]byte, error) {
	if len(signature) != ed25519.SignatureSize || len(approval.signingBytes) == 0 ||
		!validUnsignedApprovalShape(approval.envelope) {
		return nil, ErrInvalidApproval
	}
	unsignedCanonical, err := marshalApprovalCanonical(approval.envelope)
	if err != nil ||
		!bytes.Equal(approval.signingBytes, approvalSigningMessage(unsignedCanonical)) {
		return nil, ErrInvalidApproval
	}
	envelope := approval.envelope
	envelope.Signature = base64.RawURLEncoding.EncodeToString(signature)
	canonical, err := marshalApprovalCanonical(envelope)
	if err != nil || len(canonical) > maximumApprovalBytes {
		return nil, ErrInvalidApproval
	}
	return canonical, nil
}

// ApprovalVerificationScope limits a controller key to one deployment, PostgreSQL cell, and
// approval-reference namespace. ReferencePrefix must be an approval/ path ending in a slash and
// cannot contain empty, current-directory, or parent-directory segments.
type ApprovalVerificationScope struct {
	CellID          string
	DeploymentID    string
	ReferencePrefix string
}

// ApprovalVerificationKey is public verification material bound to one exact controller identity
// and scope. The constructor copies PublicKey so caller mutation cannot rotate trust behind the
// verifier's back. Revoked keys must be removed from the active policy; Revoked=true is rejected to
// fail closed when a policy loader accidentally passes a tombstone as active trust.
type ApprovalVerificationKey struct {
	ApproverIdentity string
	Generation       string
	KeyID            string
	NotAfter         time.Time
	NotBefore        time.Time
	PolicyRevision   string
	PublicKey        ed25519.PublicKey
	Revoked          bool
	Scope            ApprovalVerificationScope
}

type trustedApprovalKey struct {
	approverIdentity string
	fingerprint      string
	generation       string
	notAfter         time.Time
	notBefore        time.Time
	publicKey        ed25519.PublicKey
	scope            ApprovalVerificationScope
}

type ApprovalVerifier struct {
	activationRecordDigest string
	clockSkew              time.Duration
	keys                   map[string]trustedApprovalKey
	maximumLifetime        time.Duration
	policyDigest           string
	policyID               string
	policyNotAfter         time.Time
	policyNotBefore        time.Time
	policyRevision         string
	policySequence         uint64
	rootTrustBundleDigest  string
	target                 ApprovalPolicyTarget
	targetBound            bool
	verificationEnabled    bool
}

// newApprovalVerifierForTesting is intentionally package-private. Production composition must
// obtain a verifier from ActivatedApprovalPolicy after durable anti-rollback activation.
func newApprovalVerifierForTesting(
	keys []ApprovalVerificationKey,
	clockSkew time.Duration,
) (ApprovalVerifier, error) {
	if len(keys) == 0 {
		return ApprovalVerifier{}, ErrInvalidApprovalVerifier
	}
	policyRevision := keys[0].PolicyRevision
	policyNotBefore := time.Date(2000, 1, 1, 0, 0, 0, 0, time.UTC)
	policyNotAfter := time.Date(2100, 1, 1, 0, 0, 0, 0, time.UTC)
	for _, key := range keys[1:] {
		if key.PolicyRevision != policyRevision {
			return ApprovalVerifier{}, ErrInvalidApprovalVerifier
		}
	}
	return newApprovalVerifier(keys, approvalVerifierPolicy{
		activationRecordDigest: "sha256:" + strings.Repeat("1", 64),
		clockSkew:              clockSkew,
		maximumLifetime:        maximumApprovalLifetime,
		policyDigest:           "sha256:" + strings.Repeat("2", 64),
		policyID:               "approval-policy/testing",
		policyNotAfter:         policyNotAfter,
		policyNotBefore:        policyNotBefore,
		policyRevision:         policyRevision,
		policySequence:         1,
		rootTrustBundleDigest:  "sha256:" + strings.Repeat("3", 64),
		verificationEnabled:    true,
	})
}

type approvalVerifierPolicy struct {
	activationRecordDigest string
	clockSkew              time.Duration
	maximumLifetime        time.Duration
	policyDigest           string
	policyID               string
	policyNotAfter         time.Time
	policyNotBefore        time.Time
	policyRevision         string
	policySequence         uint64
	rootTrustBundleDigest  string
	target                 ApprovalPolicyTarget
	targetBound            bool
	verificationEnabled    bool
}

func newApprovalVerifier(
	keys []ApprovalVerificationKey,
	policy approvalVerifierPolicy,
) (ApprovalVerifier, error) {
	if len(keys) > maximumApprovalKeys || !validApprovalVerifierPolicy(policy) ||
		(policy.verificationEnabled && len(keys) == 0) || (!policy.verificationEnabled && len(keys) != 0) {
		return ApprovalVerifier{}, ErrInvalidApprovalVerifier
	}
	trusted := make(map[string]trustedApprovalKey, len(keys))
	for _, key := range keys {
		if !validApprovalVerificationKey(key) || key.PolicyRevision != policy.policyRevision {
			return ApprovalVerifier{}, ErrInvalidApprovalVerifier
		}
		if _, duplicate := trusted[key.KeyID]; duplicate {
			return ApprovalVerifier{}, ErrInvalidApprovalVerifier
		}
		trusted[key.KeyID] = trustedApprovalKey{
			approverIdentity: key.ApproverIdentity,
			fingerprint:      approvalKeyFingerprint(key.PublicKey),
			generation:       key.Generation,
			notAfter:         key.NotAfter,
			notBefore:        key.NotBefore,
			publicKey:        slices.Clone(key.PublicKey),
			scope:            key.Scope,
		}
	}
	return ApprovalVerifier{
		activationRecordDigest: policy.activationRecordDigest,
		clockSkew:              policy.clockSkew,
		keys:                   trusted,
		maximumLifetime:        policy.maximumLifetime,
		policyDigest:           policy.policyDigest,
		policyID:               policy.policyID,
		policyNotAfter:         policy.policyNotAfter,
		policyNotBefore:        policy.policyNotBefore,
		policyRevision:         policy.policyRevision,
		policySequence:         policy.policySequence,
		rootTrustBundleDigest:  policy.rootTrustBundleDigest,
		target:                 policy.target,
		targetBound:            policy.targetBound,
		verificationEnabled:    policy.verificationEnabled,
	}, nil
}

// VerifiedApproval is the only approval form an executor may consume. It exposes a
// non-authenticating evidence digest and bounded policy metadata, never the signature or canonical
// envelope. Verification is intentionally not a single-use operation: the executor must durably
// consume ApprovalDigest with a plan-bound execution attempt before it performs any mutation.
type VerifiedApproval struct {
	activationRecordDigest string
	approvalDigest         string
	approvedAt             time.Time
	approverIdentity       string
	cellID                 string
	deploymentID           string
	expiresAt              time.Time
	keyFingerprint         string
	keyGeneration          string
	keyID                  string
	planDigest             string
	planID                 string
	policyDigest           string
	policyID               string
	policyRevision         string
	policySequence         uint64
	reference              string
	rootTrustBundleDigest  string
}

func (approval VerifiedApproval) ActivationRecordDigest() string {
	return approval.activationRecordDigest
}
func (approval VerifiedApproval) ApprovalDigest() string   { return approval.approvalDigest }
func (approval VerifiedApproval) ApprovedAt() time.Time    { return approval.approvedAt }
func (approval VerifiedApproval) ApproverIdentity() string { return approval.approverIdentity }
func (approval VerifiedApproval) CellID() string           { return approval.cellID }
func (approval VerifiedApproval) DeploymentID() string     { return approval.deploymentID }
func (approval VerifiedApproval) ExpiresAt() time.Time     { return approval.expiresAt }
func (approval VerifiedApproval) KeyFingerprint() string   { return approval.keyFingerprint }
func (approval VerifiedApproval) KeyGeneration() string    { return approval.keyGeneration }
func (approval VerifiedApproval) KeyID() string            { return approval.keyID }
func (approval VerifiedApproval) PlanDigest() string       { return approval.planDigest }
func (approval VerifiedApproval) PlanID() string           { return approval.planID }
func (approval VerifiedApproval) PolicyDigest() string     { return approval.policyDigest }
func (approval VerifiedApproval) PolicyID() string         { return approval.policyID }
func (approval VerifiedApproval) PolicyRevision() string   { return approval.policyRevision }
func (approval VerifiedApproval) PolicySequence() uint64   { return approval.policySequence }
func (approval VerifiedApproval) Reference() string        { return approval.reference }
func (approval VerifiedApproval) RootTrustBundleDigest() string {
	return approval.rootTrustBundleDigest
}

func (verifier ApprovalVerifier) Verify(
	plan Plan,
	raw []byte,
	now time.Time,
) (VerifiedApproval, error) {
	if !validPlanSnapshot(plan.snapshot, true) {
		return VerifiedApproval{}, ErrInvalidApproval
	}
	if now.IsZero() || !validApprovalVerifier(verifier) {
		return VerifiedApproval{}, ErrInvalidApprovalVerifier
	}
	if !verifier.verificationEnabled || len(verifier.keys) == 0 {
		return VerifiedApproval{}, ErrUntrustedApproval
	}
	instant := now.UTC()
	if instant.Before(verifier.policyNotBefore) {
		return VerifiedApproval{}, ErrUntrustedApproval
	}
	if !instant.Before(verifier.policyNotAfter) {
		return VerifiedApproval{}, ErrExpiredApproval
	}
	envelope, canonical, err := decodeApproval(raw)
	if err != nil {
		return VerifiedApproval{}, err
	}
	planSnapshot := plan.Snapshot()
	if !validSignedApproval(envelope, planSnapshot) || envelope.PlanDigest != plan.Digest() {
		return VerifiedApproval{}, ErrUntrustedApproval
	}
	verificationKey, trusted := verifier.keys[envelope.KeyID]
	if !trusted || verificationKey.approverIdentity != envelope.ApproverIdentity ||
		len(verificationKey.publicKey) != ed25519.PublicKeySize {
		return VerifiedApproval{}, ErrUntrustedApproval
	}
	signature, err := base64.RawURLEncoding.Strict().DecodeString(envelope.Signature)
	if err != nil || len(signature) != ed25519.SignatureSize ||
		base64.RawURLEncoding.EncodeToString(signature) != envelope.Signature {
		return VerifiedApproval{}, ErrInvalidApproval
	}
	unsigned := envelope
	unsigned.Signature = ""
	unsignedCanonical, err := marshalApprovalCanonical(unsigned)
	if err != nil ||
		!ed25519.Verify(verificationKey.publicKey, approvalSigningMessage(unsignedCanonical), signature) {
		return VerifiedApproval{}, ErrUntrustedApproval
	}
	if !validApprovalPolicyForPlan(verifier, verificationKey, envelope, planSnapshot) {
		return VerifiedApproval{}, ErrUntrustedApproval
	}
	if envelope.ApprovedAt.After(instant.Add(verifier.clockSkew)) {
		return VerifiedApproval{}, ErrUntrustedApproval
	}
	if !instant.Before(envelope.ExpiresAt.Add(verifier.clockSkew)) ||
		!instant.Before(planSnapshot.ExpiresAt.Add(verifier.clockSkew)) {
		return VerifiedApproval{}, ErrExpiredApproval
	}
	return VerifiedApproval{
		activationRecordDigest: verifier.activationRecordDigest,
		approvalDigest:         approvalEvidenceDigest(canonical),
		approvedAt:             envelope.ApprovedAt,
		approverIdentity:       envelope.ApproverIdentity,
		cellID:                 planSnapshot.Target.CellID,
		deploymentID:           planSnapshot.Target.DeploymentID,
		expiresAt:              envelope.ExpiresAt,
		keyFingerprint:         verificationKey.fingerprint,
		keyGeneration:          verificationKey.generation,
		keyID:                  envelope.KeyID,
		planDigest:             envelope.PlanDigest,
		planID:                 envelope.PlanID,
		policyDigest:           verifier.policyDigest,
		policyID:               verifier.policyID,
		policyRevision:         verifier.policyRevision,
		policySequence:         verifier.policySequence,
		reference:              envelope.Reference,
		rootTrustBundleDigest:  verifier.rootTrustBundleDigest,
	}, nil
}

func validApprovalVerificationKey(key ApprovalVerificationKey) bool {
	return canonicalIdentity(key.ApproverIdentity) && canonicalIdentity(key.Generation) &&
		strings.HasPrefix(key.Generation, "generation-") && canonicalIdentity(key.KeyID) &&
		canonicalIdentity(key.PolicyRevision) && strings.HasPrefix(key.PolicyRevision, "policy/") &&
		len(key.PublicKey) == ed25519.PublicKeySize && !key.Revoked &&
		!key.NotBefore.IsZero() && !key.NotAfter.IsZero() && key.NotBefore.Location() == time.UTC &&
		key.NotAfter.Location() == time.UTC && key.NotBefore.Nanosecond() == 0 &&
		key.NotAfter.Nanosecond() == 0 && key.NotAfter.After(key.NotBefore) &&
		canonicalIdentity(key.Scope.CellID) && canonicalIdentity(key.Scope.DeploymentID) &&
		validApprovalReferencePrefix(key.Scope.ReferencePrefix)
}

func validApprovalPolicyForPlan(
	verifier ApprovalVerifier,
	key trustedApprovalKey,
	envelope approvalEnvelope,
	plan PlanSnapshot,
) bool {
	return key.scope.CellID == plan.Target.CellID && key.scope.DeploymentID == plan.Target.DeploymentID &&
		strings.HasPrefix(envelope.Reference, key.scope.ReferencePrefix) &&
		!envelope.ApprovedAt.Before(key.notBefore) && envelope.ApprovedAt.Before(key.notAfter) &&
		(envelope.ExpiresAt.Before(key.notAfter) || envelope.ExpiresAt.Equal(key.notAfter)) &&
		!envelope.ApprovedAt.Before(verifier.policyNotBefore) &&
		!envelope.ExpiresAt.After(verifier.policyNotAfter) &&
		envelope.ExpiresAt.Sub(envelope.ApprovedAt) <= verifier.maximumLifetime &&
		(!verifier.targetBound || approvalPolicyTargetMatchesPlan(verifier.target, plan))
}

func validApprovalVerifierPolicy(policy approvalVerifierPolicy) bool {
	return canonicalDigest.MatchString(policy.activationRecordDigest) &&
		policy.clockSkew >= 0 && policy.clockSkew <= maximumApprovalClockSkew &&
		policy.maximumLifetime > 0 && policy.maximumLifetime <= maximumApprovalLifetime &&
		canonicalDigest.MatchString(policy.policyDigest) && canonicalIdentity(policy.policyID) &&
		strings.HasPrefix(policy.policyID, "approval-policy/") &&
		canonicalPolicyTime(policy.policyNotBefore) && canonicalPolicyTime(policy.policyNotAfter) &&
		policy.policyNotAfter.After(policy.policyNotBefore) && canonicalIdentity(policy.policyRevision) &&
		strings.HasPrefix(policy.policyRevision, "policy/") &&
		policy.policySequence > 0 && policy.policySequence <= maximumApprovalPolicyRevision &&
		canonicalDigest.MatchString(policy.rootTrustBundleDigest) &&
		(!policy.targetBound || (validApprovalPolicyTarget(policy.target) &&
			policy.policyRevision == approvalPolicyRevision(policy.policyID, policy.policySequence)))
}

func approvalPolicyTargetMatchesPlan(target ApprovalPolicyTarget, plan PlanSnapshot) bool {
	return target.CADigest == plan.Target.TLS.CADigest && target.CellID == plan.Target.CellID &&
		target.CutoverTopology == plan.Authority.CutoverTopology && target.Database == plan.Target.Database &&
		target.DeploymentID == plan.Target.DeploymentID &&
		target.ExecutorCompatibilityVersion == plan.Authority.ExecutorCompatibilityVersion &&
		target.PlanFormat == plan.Format && target.PostgreSQLMajor == plan.Target.PostgreSQLMajor &&
		target.ServerIdentity == plan.Target.ServerIdentity &&
		target.SystemIdentifierDigest == plan.Target.SystemIdentifierDigest &&
		target.ValidatorCompatibilityVersion == plan.Authority.ValidatorCompatibilityVersion
}

// decodeApproval accepts only the exact canonical envelope emitted by Encode. This prevents
// multiple byte representations of one signed approval from acquiring the same evidence identity.
func decodeApproval(raw []byte) (approvalEnvelope, []byte, error) {
	if len(raw) == 0 {
		return approvalEnvelope{}, nil, ErrInvalidApproval
	}
	if len(raw) > maximumApprovalBytes {
		return approvalEnvelope{}, nil, ErrApprovalTooLarge
	}
	if !utf8.Valid(raw) || !norm.NFC.IsNormal(raw) {
		return approvalEnvelope{}, nil, ErrInvalidApproval
	}
	structural := json.NewDecoder(bytes.NewReader(raw))
	structural.UseNumber()
	value, err := decodeStrictJSONValue(structural, 0)
	if err != nil {
		return approvalEnvelope{}, nil, ErrInvalidApproval
	}
	if _, object := value.(map[string]any); !object {
		return approvalEnvelope{}, nil, ErrInvalidApproval
	}
	if _, err := structural.Token(); !errors.Is(err, io.EOF) {
		return approvalEnvelope{}, nil, ErrInvalidApproval
	}
	var envelope approvalEnvelope
	typed := json.NewDecoder(bytes.NewReader(raw))
	typed.DisallowUnknownFields()
	if err := typed.Decode(&envelope); err != nil {
		return approvalEnvelope{}, nil, ErrInvalidApproval
	}
	if err := typed.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return approvalEnvelope{}, nil, ErrInvalidApproval
	}
	canonical, err := marshalApprovalCanonical(envelope)
	if err != nil || !bytes.Equal(raw, canonical) {
		return approvalEnvelope{}, nil, ErrInvalidApproval
	}
	return envelope, canonical, nil
}

func validUnsignedApproval(envelope approvalEnvelope, plan PlanSnapshot) bool {
	return validUnsignedApprovalShape(envelope) && envelope.ApproverIdentity == plan.Approval.Identity &&
		envelope.PlanID == plan.PlanID && envelope.PlanDigest == plan.PlanDigest &&
		envelope.Reference == plan.Approval.Reference &&
		(envelope.ExpiresAt.Before(plan.ExpiresAt) || envelope.ExpiresAt.Equal(plan.ExpiresAt))
}

func validUnsignedApprovalShape(envelope approvalEnvelope) bool {
	return envelope.Algorithm == approvalAlgorithmEd25519 &&
		envelope.Format == DetachedApprovalFormat && envelope.Decision == approvalDecisionApproved &&
		canonicalIdentity(envelope.ApproverIdentity) && canonicalIdentity(envelope.KeyID) &&
		canonicalDigest.MatchString(envelope.PlanDigest) && canonicalIdentity(envelope.PlanID) &&
		validApprovalReference(envelope.Reference) && envelope.Signature == "" &&
		canonicalApprovalTimeRange(envelope.ApprovedAt, envelope.ExpiresAt)
}

func validApprovalReference(value string) bool {
	return canonicalIdentity(value) && strings.HasPrefix(value, "approval/") &&
		!strings.HasSuffix(value, "/") && validApprovalReferenceSegments(value)
}

func validApprovalReferencePrefix(value string) bool {
	return canonicalIdentity(value) && strings.HasPrefix(value, "approval/") &&
		strings.HasSuffix(value, "/") && validApprovalReferenceSegments(strings.TrimSuffix(value, "/"))
}

func validApprovalReferenceSegments(value string) bool {
	for _, segment := range strings.Split(value, "/") {
		if segment == "" || segment == "." || segment == ".." {
			return false
		}
	}
	return true
}

func validSignedApproval(envelope approvalEnvelope, plan PlanSnapshot) bool {
	unsigned := envelope
	unsigned.Signature = ""
	return envelope.Signature != "" && validUnsignedApproval(unsigned, plan) &&
		envelope.PlanID == plan.PlanID && envelope.PlanDigest == plan.PlanDigest &&
		envelope.Reference == plan.Approval.Reference
}

func canonicalApprovalTimeRange(approvedAt time.Time, expiresAt time.Time) bool {
	return !approvedAt.IsZero() && !expiresAt.IsZero() && approvedAt.Location() == time.UTC &&
		expiresAt.Location() == time.UTC && approvedAt.Nanosecond() == 0 && expiresAt.Nanosecond() == 0 &&
		expiresAt.After(approvedAt) && expiresAt.Sub(approvedAt) <= maximumApprovalLifetime
}

func marshalApprovalCanonical(envelope approvalEnvelope) ([]byte, error) {
	var output bytes.Buffer
	encoder := json.NewEncoder(&output)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(envelope); err != nil {
		return nil, err
	}
	return bytes.TrimSuffix(output.Bytes(), []byte("\n")), nil
}

func approvalSigningMessage(canonical []byte) []byte {
	message := make([]byte, 0, len(approvalSignatureDomain)+len(canonical))
	message = append(message, approvalSignatureDomain...)
	return append(message, canonical...)
}

func approvalEvidenceDigest(canonical []byte) string {
	hash := sha256.New()
	_, _ = hash.Write([]byte(approvalEvidenceDigestDomain))
	_, _ = hash.Write(canonical)
	return "sha256:" + hex.EncodeToString(hash.Sum(nil))
}

func approvalKeyFingerprint(publicKey ed25519.PublicKey) string {
	hash := sha256.New()
	_, _ = hash.Write([]byte(approvalKeyFingerprintDomain))
	_, _ = hash.Write(publicKey)
	return "sha256:" + hex.EncodeToString(hash.Sum(nil))
}
