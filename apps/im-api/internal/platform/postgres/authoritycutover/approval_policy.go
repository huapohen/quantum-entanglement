package authoritycutover

import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"slices"
	"sort"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/platform/postgres/migrations"
	"golang.org/x/text/unicode/norm"
)

const (
	ApprovalPolicyFormat                        = "wanwork.im.postgres-authority-approval-policy/1"
	ApprovalPolicyRootTrustDomain               = "wanwork.im/postgres-authority-approval-policy/root-trust/1"
	approvalPolicyAlgorithmEd25519              = "ed25519"
	approvalPolicySignatureDomain               = "wanwork.im/postgres-authority-approval-policy/signature/1\n"
	approvalPolicyDigestDomain                  = "wanwork.im/postgres-authority-approval-policy/content/1\n"
	approvalPolicyEnvelopeDigestDomain          = "wanwork.im/postgres-authority-approval-policy/envelope/1\n"
	approvalPolicyRootBundleDigestDomain        = "wanwork.im/postgres-authority-approval-policy/root-bundle/1\n"
	approvalPolicyRootFingerprintDomain         = "wanwork.im/postgres-authority-approval-policy/root-key/1\n"
	maximumApprovalPolicyBytes                  = 128 * 1024
	maximumApprovalPolicyLifetime               = 90 * 24 * time.Hour
	maximumApprovalPolicyRoots                  = 8
	minimumApprovalPolicyRootQuorum             = 2
	maximumApprovalPolicyRevision        uint64 = 1<<63 - 1
)

var (
	ErrInvalidApprovalPolicy         = errors.New("invalid PostgreSQL authority approval policy")
	ErrApprovalPolicyTooLarge        = errors.New("PostgreSQL authority approval policy exceeds size limit")
	ErrInvalidApprovalPolicyVerifier = errors.New("invalid PostgreSQL authority approval policy verifier")
	ErrUntrustedApprovalPolicy       = errors.New("untrusted PostgreSQL authority approval policy")
	ErrApprovalPolicyNotActive       = errors.New("PostgreSQL authority approval policy is not active")
	ErrExpiredApprovalPolicy         = errors.New("expired PostgreSQL authority approval policy")
)

type ApprovalPolicyKeyStatus string

const (
	ApprovalPolicyKeyActive  ApprovalPolicyKeyStatus = "active"
	ApprovalPolicyKeyRevoked ApprovalPolicyKeyStatus = "revoked"
)

// ApprovalPolicyTarget freezes the physical cell and the exact executor contract for which
// approval keys may authorize plans. A deployment/cell label alone is not a cluster identity.
type ApprovalPolicyTarget struct {
	CADigest                      string `json:"caDigest"`
	CellID                        string `json:"cellId"`
	CutoverTopology               string `json:"cutoverTopology"`
	Database                      string `json:"database"`
	DeploymentID                  string `json:"deploymentId"`
	ExecutorCompatibilityVersion  string `json:"executorCompatibilityVersion"`
	PlanFormat                    string `json:"planFormat"`
	PostgreSQLMajor               int    `json:"postgresqlMajor"`
	ServerIdentity                string `json:"serverIdentity"`
	SystemIdentifierDigest        string `json:"systemIdentifierDigest"`
	ValidatorCompatibilityVersion string `json:"validatorCompatibilityVersion"`
}

// ApprovalPolicyKey is semantic input for one online approval key. PublicKey is copied. Revoked
// tombstones remain in the inventory so a later activation store can reject key ID resurrection.
type ApprovalPolicyKey struct {
	ApproverIdentity string
	Generation       string
	KeyID            string
	NotAfter         time.Time
	NotBefore        time.Time
	PublicKey        ed25519.PublicKey
	ReferencePrefix  string
	RevocationReason string
	RevokedAt        time.Time
	Status           ApprovalPolicyKeyStatus
}

type ApprovalPolicyKeySnapshot struct {
	Algorithm            string                  `json:"algorithm"`
	ApproverIdentity     string                  `json:"approverIdentity"`
	Generation           string                  `json:"generation"`
	KeyID                string                  `json:"keyId"`
	NotAfter             time.Time               `json:"notAfter"`
	NotBefore            time.Time               `json:"notBefore"`
	PublicKey            string                  `json:"publicKey"`
	PublicKeyFingerprint string                  `json:"publicKeyFingerprint"`
	ReferencePrefix      string                  `json:"referencePrefix"`
	RevocationReason     string                  `json:"revocationReason"`
	RevokedAt            time.Time               `json:"revokedAt"`
	Status               ApprovalPolicyKeyStatus `json:"status"`
}

type ApprovalPolicyRootSignature struct {
	Algorithm string `json:"algorithm"`
	RootKeyID string `json:"rootKeyId"`
	Signature string `json:"signature"`
}

// ApprovalPolicySnapshot is the canonical public distribution and archive shape. PolicyDigest
// identifies the content with both digest/signatures blank. Every root signs the canonical shape
// with PolicyDigest set and RootSignatures empty, so adding a co-signature does not change content
// identity or another root's signing message.
type ApprovalPolicySnapshot struct {
	ApprovalClockSkewSeconds       int64                         `json:"approvalClockSkewSeconds"`
	DenyAll                        bool                          `json:"denyAll"`
	Format                         string                        `json:"format"`
	IssuedAt                       time.Time                     `json:"issuedAt"`
	Keys                           []ApprovalPolicyKeySnapshot   `json:"keys"`
	MaximumApprovalLifetimeSeconds int64                         `json:"maximumApprovalLifetimeSeconds"`
	NotAfter                       time.Time                     `json:"notAfter"`
	NotBefore                      time.Time                     `json:"notBefore"`
	PolicyDigest                   string                        `json:"policyDigest"`
	PolicyID                       string                        `json:"policyId"`
	PreviousPolicyDigest           string                        `json:"previousPolicyDigest"`
	Revision                       uint64                        `json:"revision"`
	RootSignatures                 []ApprovalPolicyRootSignature `json:"rootSignatures"`
	Target                         ApprovalPolicyTarget          `json:"target"`
}

type ApprovalPolicyInput struct {
	ApprovalClockSkew       time.Duration
	DenyAll                 bool
	IssuedAt                time.Time
	Keys                    []ApprovalPolicyKey
	MaximumApprovalLifetime time.Duration
	NotAfter                time.Time
	NotBefore               time.Time
	PolicyID                string
	PreviousPolicyDigest    string
	Revision                uint64
	Target                  ApprovalPolicyTarget
}

// ApprovalPolicyToSign is detached policy content. It contains no root private key and returns
// cloned signing bytes. Each quorum root signs the same domain-separated message independently.
type ApprovalPolicyToSign struct {
	snapshot     ApprovalPolicySnapshot
	signingBytes []byte
}

func NewApprovalPolicyToSign(input ApprovalPolicyInput) (ApprovalPolicyToSign, error) {
	keys := make([]ApprovalPolicyKeySnapshot, len(input.Keys))
	for index, key := range input.Keys {
		publicKey := slices.Clone(key.PublicKey)
		keys[index] = ApprovalPolicyKeySnapshot{
			Algorithm:            approvalPolicyAlgorithmEd25519,
			ApproverIdentity:     key.ApproverIdentity,
			Generation:           key.Generation,
			KeyID:                key.KeyID,
			NotAfter:             key.NotAfter,
			NotBefore:            key.NotBefore,
			PublicKey:            base64.RawURLEncoding.EncodeToString(publicKey),
			PublicKeyFingerprint: approvalKeyFingerprint(publicKey),
			ReferencePrefix:      key.ReferencePrefix,
			RevocationReason:     key.RevocationReason,
			RevokedAt:            key.RevokedAt,
			Status:               key.Status,
		}
	}
	sort.Slice(keys, func(left, right int) bool { return keys[left].KeyID < keys[right].KeyID })
	snapshot := ApprovalPolicySnapshot{
		ApprovalClockSkewSeconds:       int64(input.ApprovalClockSkew / time.Second),
		DenyAll:                        input.DenyAll,
		Format:                         ApprovalPolicyFormat,
		IssuedAt:                       input.IssuedAt,
		Keys:                           keys,
		MaximumApprovalLifetimeSeconds: int64(input.MaximumApprovalLifetime / time.Second),
		NotAfter:                       input.NotAfter,
		NotBefore:                      input.NotBefore,
		PolicyID:                       input.PolicyID,
		PreviousPolicyDigest:           input.PreviousPolicyDigest,
		Revision:                       input.Revision,
		RootSignatures:                 []ApprovalPolicyRootSignature{},
		Target:                         input.Target,
	}
	if input.ApprovalClockSkew != time.Duration(snapshot.ApprovalClockSkewSeconds)*time.Second ||
		input.MaximumApprovalLifetime != time.Duration(snapshot.MaximumApprovalLifetimeSeconds)*time.Second ||
		!validApprovalPolicySnapshot(snapshot, false, false) {
		return ApprovalPolicyToSign{}, ErrInvalidApprovalPolicy
	}
	digestPayload := cloneApprovalPolicySnapshot(snapshot)
	digestCanonical, err := marshalApprovalPolicyCanonical(digestPayload)
	if err != nil {
		return ApprovalPolicyToSign{}, ErrInvalidApprovalPolicy
	}
	snapshot.PolicyDigest = digestApprovalPolicyContent(digestCanonical)
	if !validApprovalPolicySnapshot(snapshot, true, false) {
		return ApprovalPolicyToSign{}, ErrInvalidApprovalPolicy
	}
	signingCanonical, err := marshalApprovalPolicyCanonical(snapshot)
	if err != nil {
		return ApprovalPolicyToSign{}, ErrInvalidApprovalPolicy
	}
	return ApprovalPolicyToSign{
		snapshot:     cloneApprovalPolicySnapshot(snapshot),
		signingBytes: approvalPolicySigningMessage(signingCanonical),
	}, nil
}

func (policy ApprovalPolicyToSign) SigningBytes() []byte { return slices.Clone(policy.signingBytes) }
func (policy ApprovalPolicyToSign) PolicyDigest() string { return policy.snapshot.PolicyDigest }

type ApprovalPolicyDetachedSignature struct {
	RootKeyID string
	Signature []byte
}

func (policy ApprovalPolicyToSign) Encode(signatures []ApprovalPolicyDetachedSignature) ([]byte, error) {
	if len(policy.signingBytes) == 0 || len(signatures) == 0 || len(signatures) > maximumApprovalPolicyRoots ||
		!validApprovalPolicySnapshot(policy.snapshot, true, false) {
		return nil, ErrInvalidApprovalPolicy
	}
	snapshot := cloneApprovalPolicySnapshot(policy.snapshot)
	snapshot.RootSignatures = make([]ApprovalPolicyRootSignature, len(signatures))
	for index, signature := range signatures {
		if len(signature.Signature) != ed25519.SignatureSize {
			return nil, ErrInvalidApprovalPolicy
		}
		snapshot.RootSignatures[index] = ApprovalPolicyRootSignature{
			Algorithm: approvalPolicyAlgorithmEd25519,
			RootKeyID: signature.RootKeyID,
			Signature: base64.RawURLEncoding.EncodeToString(signature.Signature),
		}
	}
	sort.Slice(snapshot.RootSignatures, func(left, right int) bool {
		return snapshot.RootSignatures[left].RootKeyID < snapshot.RootSignatures[right].RootKeyID
	})
	if !validApprovalPolicySnapshot(snapshot, true, true) {
		return nil, ErrInvalidApprovalPolicy
	}
	unsigned := cloneApprovalPolicySnapshot(snapshot)
	unsigned.RootSignatures = []ApprovalPolicyRootSignature{}
	canonical, err := marshalApprovalPolicyCanonical(unsigned)
	if err != nil || !bytes.Equal(policy.signingBytes, approvalPolicySigningMessage(canonical)) {
		return nil, ErrInvalidApprovalPolicy
	}
	canonical, err = marshalApprovalPolicyCanonical(snapshot)
	if err != nil || len(canonical) > maximumApprovalPolicyBytes {
		return nil, ErrInvalidApprovalPolicy
	}
	return canonical, nil
}

// ApprovalPolicyTrustRoot is offline public trust material. It is never loaded from the signed
// policy itself. The verifier copies PublicKey and computes its own fingerprint.
type ApprovalPolicyTrustRoot struct {
	Generation string
	NotAfter   time.Time
	NotBefore  time.Time
	PublicKey  ed25519.PublicKey
	Revoked    bool
	RootKeyID  string
}

// ApprovalPolicyRootTrustBundle is supplied by pinned release/IaC configuration. High-impact
// cutover policy requires at least two distinct roots; a policy cannot lower this quorum.
type ApprovalPolicyRootTrustBundle struct {
	BundleID    string
	PolicyID    string
	Quorum      int
	Revision    uint64
	Roots       []ApprovalPolicyTrustRoot
	Target      ApprovalPolicyTarget
	TrustDomain string
}

type trustedApprovalPolicyRoot struct {
	approvalFingerprint string
	fingerprint         string
	generation          string
	notAfter            time.Time
	notBefore           time.Time
	publicKey           ed25519.PublicKey
	rootKeyID           string
}

type ApprovalPolicyVerifier struct {
	bundleDigest   string
	bundleID       string
	bundleRevision uint64
	clockSkew      time.Duration
	policyID       string
	quorum         int
	roots          map[string]trustedApprovalPolicyRoot
	target         ApprovalPolicyTarget
}

func NewApprovalPolicyVerifier(
	bundle ApprovalPolicyRootTrustBundle,
	clockSkew time.Duration,
) (ApprovalPolicyVerifier, error) {
	if bundle.TrustDomain != ApprovalPolicyRootTrustDomain || !canonicalIdentity(bundle.BundleID) ||
		!strings.HasPrefix(bundle.BundleID, "approval-policy-root-bundle/") ||
		!canonicalIdentity(bundle.PolicyID) || !strings.HasPrefix(bundle.PolicyID, "approval-policy/") ||
		bundle.Revision == 0 || bundle.Revision > maximumApprovalPolicyRevision ||
		bundle.Quorum < minimumApprovalPolicyRootQuorum || bundle.Quorum > maximumApprovalPolicyRoots ||
		len(bundle.Roots) < bundle.Quorum || len(bundle.Roots) > maximumApprovalPolicyRoots ||
		clockSkew < 0 || clockSkew > maximumApprovalClockSkew || !validApprovalPolicyTarget(bundle.Target) {
		return ApprovalPolicyVerifier{}, ErrInvalidApprovalPolicyVerifier
	}
	roots := make([]ApprovalPolicyTrustRoot, len(bundle.Roots))
	copy(roots, bundle.Roots)
	sort.Slice(roots, func(left, right int) bool { return roots[left].RootKeyID < roots[right].RootKeyID })
	trusted := make(map[string]trustedApprovalPolicyRoot, len(roots))
	fingerprints := make(map[string]struct{}, len(roots))
	activeRoots := 0
	for index := range roots {
		root := roots[index]
		if !validApprovalPolicyTrustRoot(root) {
			return ApprovalPolicyVerifier{}, ErrInvalidApprovalPolicyVerifier
		}
		if _, duplicate := trusted[root.RootKeyID]; duplicate {
			return ApprovalPolicyVerifier{}, ErrInvalidApprovalPolicyVerifier
		}
		fingerprint := approvalPolicyRootFingerprint(root.PublicKey)
		if _, duplicate := fingerprints[fingerprint]; duplicate {
			return ApprovalPolicyVerifier{}, ErrInvalidApprovalPolicyVerifier
		}
		fingerprints[fingerprint] = struct{}{}
		if !root.Revoked {
			activeRoots++
			trusted[root.RootKeyID] = trustedApprovalPolicyRoot{
				approvalFingerprint: approvalKeyFingerprint(root.PublicKey),
				fingerprint:         fingerprint,
				generation:          root.Generation,
				notAfter:            root.NotAfter,
				notBefore:           root.NotBefore,
				publicKey:           slices.Clone(root.PublicKey),
				rootKeyID:           root.RootKeyID,
			}
		}
	}
	if activeRoots < bundle.Quorum {
		return ApprovalPolicyVerifier{}, ErrInvalidApprovalPolicyVerifier
	}
	bundleCanonical, err := marshalApprovalPolicyRootBundleCanonical(bundle, roots)
	if err != nil {
		return ApprovalPolicyVerifier{}, ErrInvalidApprovalPolicyVerifier
	}
	return ApprovalPolicyVerifier{
		bundleDigest:   digestApprovalPolicyRootBundle(bundleCanonical),
		bundleID:       bundle.BundleID,
		bundleRevision: bundle.Revision,
		clockSkew:      clockSkew,
		policyID:       bundle.PolicyID,
		quorum:         bundle.Quorum,
		roots:          trusted,
		target:         bundle.Target,
	}, nil
}

func validApprovalPolicyTrustRoot(root ApprovalPolicyTrustRoot) bool {
	return canonicalIdentity(root.Generation) && strings.HasPrefix(root.Generation, "generation-") &&
		canonicalIdentity(root.RootKeyID) && strings.HasPrefix(root.RootKeyID, "root-key-") &&
		len(root.PublicKey) == ed25519.PublicKeySize && canonicalPolicyTime(root.NotBefore) &&
		canonicalPolicyTime(root.NotAfter) && root.NotAfter.After(root.NotBefore)
}

// VerifiedApprovalPolicy is authenticated but not yet durably activated. It cannot create a
// production ApprovalVerifier until the anti-rollback store turns it into an activated policy.
type VerifiedApprovalPolicy struct {
	canonical             []byte
	envelopeDigest        string
	policyDigest          string
	rootFingerprints      []string
	rootTrustBundleDigest string
	snapshot              ApprovalPolicySnapshot
}

func (policy VerifiedApprovalPolicy) CanonicalBytes() []byte { return slices.Clone(policy.canonical) }
func (policy VerifiedApprovalPolicy) EnvelopeDigest() string { return policy.envelopeDigest }
func (policy VerifiedApprovalPolicy) PolicyDigest() string   { return policy.policyDigest }
func (policy VerifiedApprovalPolicy) PolicyID() string       { return policy.snapshot.PolicyID }
func (policy VerifiedApprovalPolicy) PreviousPolicyDigest() string {
	return policy.snapshot.PreviousPolicyDigest
}
func (policy VerifiedApprovalPolicy) Revision() uint64 { return policy.snapshot.Revision }
func (policy VerifiedApprovalPolicy) RootTrustBundleDigest() string {
	return policy.rootTrustBundleDigest
}
func (policy VerifiedApprovalPolicy) RootFingerprints() []string {
	return slices.Clone(policy.rootFingerprints)
}
func (policy VerifiedApprovalPolicy) Snapshot() ApprovalPolicySnapshot {
	return cloneApprovalPolicySnapshot(policy.snapshot)
}

func (verifier ApprovalPolicyVerifier) Verify(raw []byte, now time.Time) (VerifiedApprovalPolicy, error) {
	if now.IsZero() || len(verifier.roots) < verifier.quorum || verifier.quorum < minimumApprovalPolicyRootQuorum ||
		verifier.clockSkew < 0 || verifier.clockSkew > maximumApprovalClockSkew ||
		!canonicalDigest.MatchString(verifier.bundleDigest) || !validApprovalPolicyTarget(verifier.target) {
		return VerifiedApprovalPolicy{}, ErrInvalidApprovalPolicyVerifier
	}
	snapshot, canonical, err := decodeApprovalPolicy(raw)
	if err != nil {
		return VerifiedApprovalPolicy{}, err
	}
	if snapshot.PolicyID != verifier.policyID || snapshot.Target != verifier.target {
		return VerifiedApprovalPolicy{}, ErrUntrustedApprovalPolicy
	}
	digestPayload := cloneApprovalPolicySnapshot(snapshot)
	digestPayload.PolicyDigest = ""
	digestPayload.RootSignatures = []ApprovalPolicyRootSignature{}
	digestCanonical, err := marshalApprovalPolicyCanonical(digestPayload)
	if err != nil || digestApprovalPolicyContent(digestCanonical) != snapshot.PolicyDigest {
		return VerifiedApprovalPolicy{}, ErrUntrustedApprovalPolicy
	}
	unsigned := cloneApprovalPolicySnapshot(snapshot)
	unsigned.RootSignatures = []ApprovalPolicyRootSignature{}
	unsignedCanonical, err := marshalApprovalPolicyCanonical(unsigned)
	if err != nil {
		return VerifiedApprovalPolicy{}, ErrInvalidApprovalPolicy
	}
	message := approvalPolicySigningMessage(unsignedCanonical)
	rootFingerprints := make([]string, 0, len(snapshot.RootSignatures))
	approvalFingerprints := make(map[string]struct{}, len(snapshot.Keys))
	for _, key := range snapshot.Keys {
		approvalFingerprints[key.PublicKeyFingerprint] = struct{}{}
	}
	for _, root := range verifier.roots {
		if _, overlap := approvalFingerprints[root.approvalFingerprint]; overlap {
			return VerifiedApprovalPolicy{}, ErrUntrustedApprovalPolicy
		}
	}
	for _, signatureSnapshot := range snapshot.RootSignatures {
		root, trusted := verifier.roots[signatureSnapshot.RootKeyID]
		if !trusted || snapshot.NotBefore.Before(root.notBefore) || snapshot.NotAfter.After(root.notAfter) ||
			snapshot.IssuedAt.Before(root.notBefore) || !snapshot.IssuedAt.Before(root.notAfter) {
			return VerifiedApprovalPolicy{}, ErrUntrustedApprovalPolicy
		}
		signature, decodeErr := base64.RawURLEncoding.Strict().DecodeString(signatureSnapshot.Signature)
		if decodeErr != nil || len(signature) != ed25519.SignatureSize ||
			base64.RawURLEncoding.EncodeToString(signature) != signatureSnapshot.Signature ||
			!ed25519.Verify(root.publicKey, message, signature) {
			return VerifiedApprovalPolicy{}, ErrUntrustedApprovalPolicy
		}
		rootFingerprints = append(rootFingerprints, root.fingerprint)
	}
	if len(rootFingerprints) < verifier.quorum {
		return VerifiedApprovalPolicy{}, ErrUntrustedApprovalPolicy
	}
	instant := now.UTC()
	if snapshot.IssuedAt.After(instant.Add(verifier.clockSkew)) {
		return VerifiedApprovalPolicy{}, ErrUntrustedApprovalPolicy
	}
	if instant.Before(snapshot.NotBefore.Add(-verifier.clockSkew)) {
		return VerifiedApprovalPolicy{}, ErrApprovalPolicyNotActive
	}
	if !instant.Before(snapshot.NotAfter.Add(verifier.clockSkew)) {
		return VerifiedApprovalPolicy{}, ErrExpiredApprovalPolicy
	}
	return VerifiedApprovalPolicy{
		canonical:             slices.Clone(canonical),
		envelopeDigest:        digestApprovalPolicyEnvelope(canonical),
		policyDigest:          snapshot.PolicyDigest,
		rootFingerprints:      slices.Clone(rootFingerprints),
		rootTrustBundleDigest: verifier.bundleDigest,
		snapshot:              cloneApprovalPolicySnapshot(snapshot),
	}, nil
}

func decodeApprovalPolicy(raw []byte) (ApprovalPolicySnapshot, []byte, error) {
	if len(raw) == 0 {
		return ApprovalPolicySnapshot{}, nil, ErrInvalidApprovalPolicy
	}
	if len(raw) > maximumApprovalPolicyBytes {
		return ApprovalPolicySnapshot{}, nil, ErrApprovalPolicyTooLarge
	}
	if !utf8.Valid(raw) || !norm.NFC.IsNormal(raw) {
		return ApprovalPolicySnapshot{}, nil, ErrInvalidApprovalPolicy
	}
	structural := json.NewDecoder(bytes.NewReader(raw))
	structural.UseNumber()
	value, err := decodeStrictJSONValue(structural, 0)
	if err != nil {
		return ApprovalPolicySnapshot{}, nil, ErrInvalidApprovalPolicy
	}
	if _, object := value.(map[string]any); !object {
		return ApprovalPolicySnapshot{}, nil, ErrInvalidApprovalPolicy
	}
	if _, err := structural.Token(); !errors.Is(err, io.EOF) {
		return ApprovalPolicySnapshot{}, nil, ErrInvalidApprovalPolicy
	}
	var snapshot ApprovalPolicySnapshot
	typed := json.NewDecoder(bytes.NewReader(raw))
	typed.DisallowUnknownFields()
	if err := typed.Decode(&snapshot); err != nil {
		return ApprovalPolicySnapshot{}, nil, ErrInvalidApprovalPolicy
	}
	if err := typed.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return ApprovalPolicySnapshot{}, nil, ErrInvalidApprovalPolicy
	}
	if !validApprovalPolicySnapshot(snapshot, true, true) {
		return ApprovalPolicySnapshot{}, nil, ErrInvalidApprovalPolicy
	}
	canonical, err := marshalApprovalPolicyCanonical(snapshot)
	if err != nil || !bytes.Equal(raw, canonical) {
		return ApprovalPolicySnapshot{}, nil, ErrInvalidApprovalPolicy
	}
	return snapshot, canonical, nil
}

func validApprovalPolicySnapshot(
	snapshot ApprovalPolicySnapshot,
	requireDigest bool,
	requireSignatures bool,
) bool {
	if snapshot.Format != ApprovalPolicyFormat || !canonicalIdentity(snapshot.PolicyID) ||
		!strings.HasPrefix(snapshot.PolicyID, "approval-policy/") || snapshot.Revision == 0 ||
		snapshot.Revision > maximumApprovalPolicyRevision || !canonicalPolicyTime(snapshot.IssuedAt) ||
		!canonicalPolicyTime(snapshot.NotBefore) || !canonicalPolicyTime(snapshot.NotAfter) ||
		snapshot.IssuedAt.After(snapshot.NotBefore) || !snapshot.NotAfter.After(snapshot.NotBefore) ||
		snapshot.NotAfter.Sub(snapshot.NotBefore) > maximumApprovalPolicyLifetime ||
		snapshot.ApprovalClockSkewSeconds < 0 ||
		snapshot.ApprovalClockSkewSeconds > int64(maximumApprovalClockSkew/time.Second) ||
		snapshot.MaximumApprovalLifetimeSeconds <= 0 ||
		snapshot.MaximumApprovalLifetimeSeconds > int64(maximumApprovalLifetime/time.Second) ||
		!validApprovalPolicyTarget(snapshot.Target) || len(snapshot.Keys) > maximumApprovalKeys ||
		!slices.IsSortedFunc(snapshot.Keys, func(left, right ApprovalPolicyKeySnapshot) int {
			return strings.Compare(left.KeyID, right.KeyID)
		}) {
		return false
	}
	if (snapshot.Revision == 1 && snapshot.PreviousPolicyDigest != "") ||
		(snapshot.Revision > 1 && !canonicalDigest.MatchString(snapshot.PreviousPolicyDigest)) {
		return false
	}
	if requireDigest {
		if !canonicalDigest.MatchString(snapshot.PolicyDigest) {
			return false
		}
	} else if snapshot.PolicyDigest != "" {
		return false
	}
	seenKeyIDs := make(map[string]struct{}, len(snapshot.Keys))
	seenFingerprints := make(map[string]struct{}, len(snapshot.Keys))
	activeKeys := 0
	for _, key := range snapshot.Keys {
		if !validApprovalPolicyKeySnapshot(key, snapshot) {
			return false
		}
		if _, duplicate := seenKeyIDs[key.KeyID]; duplicate {
			return false
		}
		if _, duplicate := seenFingerprints[key.PublicKeyFingerprint]; duplicate {
			return false
		}
		seenKeyIDs[key.KeyID] = struct{}{}
		seenFingerprints[key.PublicKeyFingerprint] = struct{}{}
		if key.Status == ApprovalPolicyKeyActive {
			activeKeys++
		}
	}
	if snapshot.DenyAll != (activeKeys == 0) {
		return false
	}
	if !requireSignatures {
		return len(snapshot.RootSignatures) == 0
	}
	if len(snapshot.RootSignatures) == 0 || len(snapshot.RootSignatures) > maximumApprovalPolicyRoots ||
		!slices.IsSortedFunc(snapshot.RootSignatures, func(left, right ApprovalPolicyRootSignature) int {
			return strings.Compare(left.RootKeyID, right.RootKeyID)
		}) {
		return false
	}
	seenRoots := make(map[string]struct{}, len(snapshot.RootSignatures))
	for _, signature := range snapshot.RootSignatures {
		if signature.Algorithm != approvalPolicyAlgorithmEd25519 ||
			!canonicalIdentity(signature.RootKeyID) || !strings.HasPrefix(signature.RootKeyID, "root-key-") {
			return false
		}
		if _, duplicate := seenRoots[signature.RootKeyID]; duplicate {
			return false
		}
		decoded, err := base64.RawURLEncoding.Strict().DecodeString(signature.Signature)
		if err != nil || len(decoded) != ed25519.SignatureSize ||
			base64.RawURLEncoding.EncodeToString(decoded) != signature.Signature {
			return false
		}
		seenRoots[signature.RootKeyID] = struct{}{}
	}
	return true
}

func validApprovalPolicyKeySnapshot(key ApprovalPolicyKeySnapshot, policy ApprovalPolicySnapshot) bool {
	if key.Algorithm != approvalPolicyAlgorithmEd25519 || !canonicalIdentity(key.ApproverIdentity) ||
		!canonicalIdentity(key.Generation) || !strings.HasPrefix(key.Generation, "generation-") ||
		!canonicalIdentity(key.KeyID) || !canonicalPolicyTime(key.NotBefore) ||
		!canonicalPolicyTime(key.NotAfter) || !key.NotAfter.After(key.NotBefore) ||
		!validApprovalReferencePrefix(key.ReferencePrefix) || !canonicalDigest.MatchString(key.PublicKeyFingerprint) {
		return false
	}
	publicKey, err := base64.RawURLEncoding.Strict().DecodeString(key.PublicKey)
	if err != nil || len(publicKey) != ed25519.PublicKeySize ||
		base64.RawURLEncoding.EncodeToString(publicKey) != key.PublicKey ||
		approvalKeyFingerprint(ed25519.PublicKey(publicKey)) != key.PublicKeyFingerprint {
		return false
	}
	switch key.Status {
	case ApprovalPolicyKeyActive:
		return key.RevokedAt.IsZero() && key.RevocationReason == "" &&
			key.NotAfter.After(policy.NotBefore) && !key.NotAfter.After(policy.NotAfter)
	case ApprovalPolicyKeyRevoked:
		return canonicalPolicyTime(key.RevokedAt) && !key.RevokedAt.Before(key.NotBefore) &&
			!key.RevokedAt.After(policy.NotBefore) && canonicalIdentity(key.RevocationReason) &&
			strings.HasPrefix(key.RevocationReason, "revocation/")
	default:
		return false
	}
}

func validApprovalPolicyTarget(target ApprovalPolicyTarget) bool {
	return canonicalDigest.MatchString(target.CADigest) && canonicalIdentity(target.CellID) &&
		target.CutoverTopology == migrations.AuthorityCutoverTopology && canonicalIdentity(target.Database) &&
		canonicalIdentity(target.DeploymentID) &&
		target.ExecutorCompatibilityVersion == migrations.AuthorityAccessExecutorCompatibility &&
		target.PlanFormat == PlanFormat && target.PostgreSQLMajor == migrations.AuthorityAccessPostgreSQLMajor &&
		canonicalIdentity(target.ServerIdentity) && canonicalDigest.MatchString(target.SystemIdentifierDigest) &&
		target.ValidatorCompatibilityVersion == migrations.AuthorityAccessValidatorCompatibility
}

func canonicalPolicyTime(value time.Time) bool {
	return !value.IsZero() && value.Location() == time.UTC && value.Nanosecond() == 0
}

func ApprovalPolicyTargetFromPlan(plan Plan) (ApprovalPolicyTarget, error) {
	if !validPlanSnapshot(plan.snapshot, true) {
		return ApprovalPolicyTarget{}, ErrInvalidApprovalPolicy
	}
	snapshot := plan.Snapshot()
	return ApprovalPolicyTarget{
		CADigest:                      snapshot.Target.TLS.CADigest,
		CellID:                        snapshot.Target.CellID,
		CutoverTopology:               snapshot.Authority.CutoverTopology,
		Database:                      snapshot.Target.Database,
		DeploymentID:                  snapshot.Target.DeploymentID,
		ExecutorCompatibilityVersion:  snapshot.Authority.ExecutorCompatibilityVersion,
		PlanFormat:                    snapshot.Format,
		PostgreSQLMajor:               snapshot.Target.PostgreSQLMajor,
		ServerIdentity:                snapshot.Target.ServerIdentity,
		SystemIdentifierDigest:        snapshot.Target.SystemIdentifierDigest,
		ValidatorCompatibilityVersion: snapshot.Authority.ValidatorCompatibilityVersion,
	}, nil
}

func approvalPolicyRevision(policyID string, revision uint64) string {
	return "policy/" + strings.TrimPrefix(policyID, "approval-policy/") + "/revision-" +
		strconv.FormatUint(revision, 10)
}

func marshalApprovalPolicyCanonical(snapshot ApprovalPolicySnapshot) ([]byte, error) {
	var output bytes.Buffer
	encoder := json.NewEncoder(&output)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(snapshot); err != nil {
		return nil, err
	}
	return bytes.TrimSuffix(output.Bytes(), []byte("\n")), nil
}

type approvalPolicyRootBundleSnapshot struct {
	BundleID    string                            `json:"bundleId"`
	PolicyID    string                            `json:"policyId"`
	Quorum      int                               `json:"quorum"`
	Revision    uint64                            `json:"revision"`
	Roots       []approvalPolicyTrustRootSnapshot `json:"roots"`
	Target      ApprovalPolicyTarget              `json:"target"`
	TrustDomain string                            `json:"trustDomain"`
}

type approvalPolicyTrustRootSnapshot struct {
	Generation           string    `json:"generation"`
	NotAfter             time.Time `json:"notAfter"`
	NotBefore            time.Time `json:"notBefore"`
	PublicKey            string    `json:"publicKey"`
	PublicKeyFingerprint string    `json:"publicKeyFingerprint"`
	Revoked              bool      `json:"revoked"`
	RootKeyID            string    `json:"rootKeyId"`
}

func marshalApprovalPolicyRootBundleCanonical(
	bundle ApprovalPolicyRootTrustBundle,
	roots []ApprovalPolicyTrustRoot,
) ([]byte, error) {
	snapshots := make([]approvalPolicyTrustRootSnapshot, len(roots))
	for index, root := range roots {
		snapshots[index] = approvalPolicyTrustRootSnapshot{
			Generation:           root.Generation,
			NotAfter:             root.NotAfter,
			NotBefore:            root.NotBefore,
			PublicKey:            base64.RawURLEncoding.EncodeToString(root.PublicKey),
			PublicKeyFingerprint: approvalPolicyRootFingerprint(root.PublicKey),
			Revoked:              root.Revoked,
			RootKeyID:            root.RootKeyID,
		}
	}
	value := approvalPolicyRootBundleSnapshot{
		BundleID:    bundle.BundleID,
		PolicyID:    bundle.PolicyID,
		Quorum:      bundle.Quorum,
		Revision:    bundle.Revision,
		Roots:       snapshots,
		Target:      bundle.Target,
		TrustDomain: bundle.TrustDomain,
	}
	var output bytes.Buffer
	encoder := json.NewEncoder(&output)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(value); err != nil {
		return nil, err
	}
	return bytes.TrimSuffix(output.Bytes(), []byte("\n")), nil
}

func approvalPolicySigningMessage(canonical []byte) []byte {
	message := make([]byte, 0, len(approvalPolicySignatureDomain)+len(canonical))
	message = append(message, approvalPolicySignatureDomain...)
	return append(message, canonical...)
}

func digestApprovalPolicyContent(canonical []byte) string {
	return domainSeparatedDigest(approvalPolicyDigestDomain, canonical)
}

func digestApprovalPolicyEnvelope(canonical []byte) string {
	return domainSeparatedDigest(approvalPolicyEnvelopeDigestDomain, canonical)
}

func digestApprovalPolicyRootBundle(canonical []byte) string {
	return domainSeparatedDigest(approvalPolicyRootBundleDigestDomain, canonical)
}

func approvalPolicyRootFingerprint(publicKey ed25519.PublicKey) string {
	return domainSeparatedDigest(approvalPolicyRootFingerprintDomain, publicKey)
}

func domainSeparatedDigest(domain string, value []byte) string {
	hash := sha256.New()
	_, _ = hash.Write([]byte(domain))
	_, _ = hash.Write(value)
	return "sha256:" + hex.EncodeToString(hash.Sum(nil))
}

func cloneApprovalPolicySnapshot(snapshot ApprovalPolicySnapshot) ApprovalPolicySnapshot {
	clone := snapshot
	clone.Keys = slices.Clone(snapshot.Keys)
	clone.RootSignatures = slices.Clone(snapshot.RootSignatures)
	return clone
}

func (policy VerifiedApprovalPolicy) String() string {
	return fmt.Sprintf("ApprovalPolicy{%s revision=%d digest=%s}",
		policy.PolicyID(), policy.Revision(), policy.PolicyDigest())
}
