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

// ApprovalVerificationKey is public verification material bound to one exact controller identity.
// The constructor copies PublicKey so caller mutation cannot rotate trust behind the verifier's
// back. Key rotation is represented by multiple distinct KeyIDs for the same identity.
type ApprovalVerificationKey struct {
	ApproverIdentity string
	KeyID            string
	PublicKey        ed25519.PublicKey
}

type trustedApprovalKey struct {
	approverIdentity string
	publicKey        ed25519.PublicKey
}

type ApprovalVerifier struct {
	keys      map[string]trustedApprovalKey
	clockSkew time.Duration
}

func NewApprovalVerifier(
	keys []ApprovalVerificationKey,
	clockSkew time.Duration,
) (ApprovalVerifier, error) {
	if len(keys) == 0 || len(keys) > maximumApprovalKeys || clockSkew < 0 ||
		clockSkew > maximumApprovalClockSkew {
		return ApprovalVerifier{}, ErrInvalidApprovalVerifier
	}
	trusted := make(map[string]trustedApprovalKey, len(keys))
	for _, key := range keys {
		if !canonicalIdentity(key.ApproverIdentity) || !canonicalIdentity(key.KeyID) ||
			len(key.PublicKey) != ed25519.PublicKeySize {
			return ApprovalVerifier{}, ErrInvalidApprovalVerifier
		}
		if _, duplicate := trusted[key.KeyID]; duplicate {
			return ApprovalVerifier{}, ErrInvalidApprovalVerifier
		}
		trusted[key.KeyID] = trustedApprovalKey{
			approverIdentity: key.ApproverIdentity,
			publicKey:        slices.Clone(key.PublicKey),
		}
	}
	return ApprovalVerifier{keys: trusted, clockSkew: clockSkew}, nil
}

// VerifiedApproval is the only approval form an executor may consume. It exposes a non-reusable
// evidence digest and bounded metadata, never the signature or canonical envelope.
type VerifiedApproval struct {
	approvalDigest   string
	approvedAt       time.Time
	approverIdentity string
	expiresAt        time.Time
	keyID            string
	planDigest       string
	planID           string
	reference        string
}

func (approval VerifiedApproval) ApprovalDigest() string   { return approval.approvalDigest }
func (approval VerifiedApproval) ApprovedAt() time.Time    { return approval.approvedAt }
func (approval VerifiedApproval) ApproverIdentity() string { return approval.approverIdentity }
func (approval VerifiedApproval) ExpiresAt() time.Time     { return approval.expiresAt }
func (approval VerifiedApproval) KeyID() string            { return approval.keyID }
func (approval VerifiedApproval) PlanDigest() string       { return approval.planDigest }
func (approval VerifiedApproval) PlanID() string           { return approval.planID }
func (approval VerifiedApproval) Reference() string        { return approval.reference }

func (verifier ApprovalVerifier) Verify(
	plan Plan,
	raw []byte,
	now time.Time,
) (VerifiedApproval, error) {
	if !validPlanSnapshot(plan.snapshot, true) {
		return VerifiedApproval{}, ErrInvalidApproval
	}
	if now.IsZero() || len(verifier.keys) == 0 || verifier.clockSkew < 0 ||
		verifier.clockSkew > maximumApprovalClockSkew {
		return VerifiedApproval{}, ErrInvalidApprovalVerifier
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
	instant := now.UTC()
	if envelope.ApprovedAt.After(instant.Add(verifier.clockSkew)) {
		return VerifiedApproval{}, ErrUntrustedApproval
	}
	if !instant.Before(envelope.ExpiresAt.Add(verifier.clockSkew)) ||
		!instant.Before(planSnapshot.ExpiresAt.Add(verifier.clockSkew)) {
		return VerifiedApproval{}, ErrExpiredApproval
	}
	return VerifiedApproval{
		approvalDigest:   approvalEvidenceDigest(canonical),
		approvedAt:       envelope.ApprovedAt,
		approverIdentity: envelope.ApproverIdentity,
		expiresAt:        envelope.ExpiresAt,
		keyID:            envelope.KeyID,
		planDigest:       envelope.PlanDigest,
		planID:           envelope.PlanID,
		reference:        envelope.Reference,
	}, nil
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
		canonicalIdentity(envelope.Reference) && envelope.Signature == "" &&
		canonicalApprovalTimeRange(envelope.ApprovedAt, envelope.ExpiresAt)
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
