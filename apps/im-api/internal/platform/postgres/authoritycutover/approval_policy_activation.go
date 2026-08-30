package authoritycutover

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"reflect"
	"slices"
	"strings"
	"time"
	"unicode/utf8"

	"golang.org/x/text/unicode/norm"
)

const (
	ApprovalPolicyActivationRecordFormat       = "wanwork.im.postgres-authority-approval-policy-activation/1"
	approvalPolicyActivationDigestDomain       = "wanwork.im/postgres-authority-approval-policy-activation/1\n"
	approvalPolicyTargetDigestDomain           = "wanwork.im/postgres-authority-approval-policy-target/1\n"
	maximumApprovalPolicyActivationRecordBytes = 32 * 1024
	approvalPolicyReconciliationTimeout        = 5 * time.Second
)

var (
	ErrInvalidApprovalPolicyActivator   = errors.New("invalid PostgreSQL authority approval policy activator")
	ErrApprovalPolicyStoreEmpty         = errors.New("PostgreSQL authority approval policy store is empty")
	ErrApprovalPolicyStoreUnavailable   = errors.New("PostgreSQL authority approval policy store is unavailable")
	ErrInvalidApprovalPolicyStoreState  = errors.New("invalid PostgreSQL authority approval policy store state")
	ErrApprovalPolicyRollback           = errors.New("PostgreSQL authority approval policy rollback rejected")
	ErrApprovalPolicyFork               = errors.New("PostgreSQL authority approval policy fork rejected")
	ErrApprovalPolicyGap                = errors.New("PostgreSQL authority approval policy revision gap rejected")
	ErrApprovalPolicyBrokenChain        = errors.New("PostgreSQL authority approval policy digest chain rejected")
	ErrApprovalPolicyLineage            = errors.New("PostgreSQL authority approval key lineage rejected")
	ErrApprovalPolicyActivationConflict = errors.New("PostgreSQL authority approval policy activation conflict")
	ErrApprovalPolicyCommitUncertain    = errors.New("PostgreSQL authority approval policy activation commit is uncertain")
)

// ApprovalPolicyNamespace is a stable durable-store partition. TargetDigest binds the full
// physical-cell and executor-compatibility target, not only human-readable deployment labels.
type ApprovalPolicyNamespace struct {
	PolicyID     string
	TargetDigest string
}

// ApprovalPolicyHead is the compare-and-swap high-water mark. Revision is the monotonic ordering
// authority; PolicyDigest rejects same-revision forks; ActivationRecordDigest binds the immutable
// audit record that introduced the head.
type ApprovalPolicyHead struct {
	ActivationRecordDigest string
	PolicyDigest           string
	PolicyID               string
	Revision               uint64
	TargetDigest           string
}

// ApprovalPolicyActivationRecord is immutable public evidence. It activates approval
// verification only; MutationAuthorized is permanently false and cannot be used as a database
// mutation lease.
type ApprovalPolicyActivationRecord struct {
	ActivatedAt                 time.Time `json:"activatedAt"`
	ActivationRecordDigest      string    `json:"activationRecordDigest"`
	ApprovalVerificationEnabled bool      `json:"approvalVerificationEnabled"`
	Format                      string    `json:"format"`
	MutationAuthorized          bool      `json:"mutationAuthorized"`
	PolicyDigest                string    `json:"policyDigest"`
	PolicyEnvelopeDigest        string    `json:"policyEnvelopeDigest"`
	PolicyID                    string    `json:"policyId"`
	PolicyRevision              string    `json:"policyRevision"`
	PreviousPolicyDigest        string    `json:"previousPolicyDigest"`
	Revision                    uint64    `json:"revision"`
	RootSignerFingerprints      []string  `json:"rootSignerFingerprints"`
	RootTrustBundleDigest       string    `json:"rootTrustBundleDigest"`
	TargetDigest                string    `json:"targetDigest"`
}

func (record ApprovalPolicyActivationRecord) Head() ApprovalPolicyHead {
	return ApprovalPolicyHead{
		ActivationRecordDigest: record.ActivationRecordDigest,
		PolicyDigest:           record.PolicyDigest,
		PolicyID:               record.PolicyID,
		Revision:               record.Revision,
		TargetDigest:           record.TargetDigest,
	}
}

// ApprovalPolicyStoredState must be returned from one durable snapshot. CanonicalPolicy is the
// exact create-only archived policy selected by Head; implementations must never synthesize it
// from a mutable current file.
type ApprovalPolicyStoredState struct {
	CanonicalPolicy []byte
	Head            ApprovalPolicyHead
	Record          ApprovalPolicyActivationRecord
}

// ApprovalPolicyActivationStore is the durability boundary. CompareAndActivate must atomically
// create an immutable policy archive entry, create its activation record, and advance the exact
// expected high-water head. A separate target PostgreSQL database must not be used for this state.
// Implementations return ErrApprovalPolicyActivationConflict for a failed CAS and
// ErrApprovalPolicyCommitUncertain when acknowledgement does not prove whether the transaction
// committed. The activator always performs authoritative readback before returning an active
// policy.
type ApprovalPolicyActivationStore interface {
	Load(context.Context, ApprovalPolicyNamespace) (ApprovalPolicyStoredState, error)
	CompareAndActivate(
		context.Context,
		ApprovalPolicyNamespace,
		ApprovalPolicyHead,
		ApprovalPolicyActivationRecord,
		[]byte,
	) error
}

type ApprovalPolicyActivator struct {
	store    ApprovalPolicyActivationStore
	verifier ApprovalPolicyVerifier
}

func NewApprovalPolicyActivator(
	verifier ApprovalPolicyVerifier,
	store ApprovalPolicyActivationStore,
) (ApprovalPolicyActivator, error) {
	if !validApprovalPolicyVerifier(verifier) || nilInterface(store) {
		return ApprovalPolicyActivator{}, ErrInvalidApprovalPolicyActivator
	}
	return ApprovalPolicyActivator{store: store, verifier: verifier}, nil
}

// ActivatedApprovalPolicy is the only policy form from which production approval verification may
// later be constructed. It is returned only after durable readback proves an exact activation.
type ActivatedApprovalPolicy struct {
	policy VerifiedApprovalPolicy
	record ApprovalPolicyActivationRecord
}

func (policy ActivatedApprovalPolicy) ActivationRecord() ApprovalPolicyActivationRecord {
	return cloneApprovalPolicyActivationRecord(policy.record)
}
func (policy ActivatedApprovalPolicy) ActivationRecordDigest() string {
	return policy.record.ActivationRecordDigest
}
func (policy ActivatedApprovalPolicy) ApprovalVerificationEnabled() bool {
	return policy.record.ApprovalVerificationEnabled
}
func (policy ActivatedApprovalPolicy) CanonicalPolicyBytes() []byte {
	return policy.policy.CanonicalBytes()
}
func (policy ActivatedApprovalPolicy) PolicyDigest() string { return policy.record.PolicyDigest }
func (policy ActivatedApprovalPolicy) PolicyID() string     { return policy.record.PolicyID }
func (policy ActivatedApprovalPolicy) PolicyRevision() string {
	return policy.record.PolicyRevision
}
func (policy ActivatedApprovalPolicy) Revision() uint64 { return policy.record.Revision }
func (policy ActivatedApprovalPolicy) Snapshot() ApprovalPolicySnapshot {
	return policy.policy.Snapshot()
}

// NewApprovalVerifier is the only production constructor for ApprovalVerifier. Every active key
// inherits one exact activated policy revision/digest and the verifier rechecks the policy window
// on every approval verification. A deny-all activation returns a valid verifier that rejects all
// approvals without an alternate keyring path.
func (policy ActivatedApprovalPolicy) NewApprovalVerifier() (ApprovalVerifier, error) {
	if !validApprovalPolicyActivationRecord(policy.record) ||
		policy.policy.PolicyID() != policy.record.PolicyID ||
		policy.policy.PolicyDigest() != policy.record.PolicyDigest ||
		policy.policy.EnvelopeDigest() != policy.record.PolicyEnvelopeDigest ||
		policy.policy.RootTrustBundleDigest() != policy.record.RootTrustBundleDigest ||
		policy.policy.Revision() != policy.record.Revision {
		return ApprovalVerifier{}, ErrInvalidApprovalVerifier
	}
	snapshot := policy.policy.Snapshot()
	if !validApprovalPolicySnapshot(snapshot, true, true) ||
		digestApprovalPolicyTarget(snapshot.Target) != policy.record.TargetDigest ||
		policy.record.ApprovalVerificationEnabled == snapshot.DenyAll {
		return ApprovalVerifier{}, ErrInvalidApprovalVerifier
	}
	keys := make([]ApprovalVerificationKey, 0, len(snapshot.Keys))
	for _, key := range snapshot.Keys {
		if key.Status == ApprovalPolicyKeyRevoked {
			continue
		}
		publicKey, err := base64.RawURLEncoding.Strict().DecodeString(key.PublicKey)
		if err != nil || len(publicKey) != ed25519.PublicKeySize ||
			base64.RawURLEncoding.EncodeToString(publicKey) != key.PublicKey {
			return ApprovalVerifier{}, ErrInvalidApprovalVerifier
		}
		keys = append(keys, ApprovalVerificationKey{
			ApproverIdentity: key.ApproverIdentity,
			Generation:       key.Generation,
			KeyID:            key.KeyID,
			NotAfter:         key.NotAfter,
			NotBefore:        key.NotBefore,
			PolicyRevision:   policy.record.PolicyRevision,
			PublicKey:        ed25519.PublicKey(slices.Clone(publicKey)),
			Scope: ApprovalVerificationScope{
				CellID:          snapshot.Target.CellID,
				DeploymentID:    snapshot.Target.DeploymentID,
				ReferencePrefix: key.ReferencePrefix,
			},
		})
	}
	return newApprovalVerifier(keys, approvalVerifierPolicy{
		activationRecordDigest: policy.record.ActivationRecordDigest,
		clockSkew:              time.Duration(snapshot.ApprovalClockSkewSeconds) * time.Second,
		maximumLifetime:        time.Duration(snapshot.MaximumApprovalLifetimeSeconds) * time.Second,
		policyDigest:           policy.record.PolicyDigest,
		policyID:               policy.record.PolicyID,
		policyNotAfter:         snapshot.NotAfter,
		policyNotBefore:        snapshot.NotBefore,
		policyRevision:         policy.record.PolicyRevision,
		policySequence:         policy.record.Revision,
		rootTrustBundleDigest:  policy.record.RootTrustBundleDigest,
		target:                 snapshot.Target,
		targetBound:            true,
		verificationEnabled:    policy.record.ApprovalVerificationEnabled,
	})
}

func (activator ApprovalPolicyActivator) Activate(
	ctx context.Context,
	raw []byte,
	now time.Time,
) (ActivatedApprovalPolicy, error) {
	if ctx == nil || !validApprovalPolicyVerifier(activator.verifier) || nilInterface(activator.store) {
		return ActivatedApprovalPolicy{}, ErrInvalidApprovalPolicyActivator
	}
	candidate, err := activator.verifier.Verify(raw, now)
	if err != nil {
		return ActivatedApprovalPolicy{}, err
	}
	activationInstant := now.UTC().Truncate(time.Second)
	candidateSnapshot := candidate.Snapshot()
	if activationInstant.Before(candidateSnapshot.NotBefore) ||
		!activationInstant.Before(candidateSnapshot.NotAfter) {
		return ActivatedApprovalPolicy{}, ErrApprovalPolicyNotActive
	}
	namespace := approvalPolicyNamespace(candidateSnapshot)
	currentState, loadErr := activator.store.Load(ctx, namespace)
	var current ActivatedApprovalPolicy
	var expected ApprovalPolicyHead
	switch {
	case loadErr == nil:
		current, err = activator.validateStoredState(currentState, namespace)
		if err != nil {
			return ActivatedApprovalPolicy{}, err
		}
		expected = current.record.Head()
		if candidate.Revision() < current.Revision() {
			return ActivatedApprovalPolicy{}, ErrApprovalPolicyRollback
		}
		if candidate.Revision() == current.Revision() {
			if candidate.PolicyDigest() != current.PolicyDigest() {
				return ActivatedApprovalPolicy{}, ErrApprovalPolicyFork
			}
			return current, nil
		}
		if current.Revision() == maximumApprovalPolicyRevision ||
			candidate.Revision() != current.Revision()+1 {
			return ActivatedApprovalPolicy{}, ErrApprovalPolicyGap
		}
		if candidate.PreviousPolicyDigest() != current.PolicyDigest() {
			return ActivatedApprovalPolicy{}, ErrApprovalPolicyBrokenChain
		}
		if !validApprovalPolicyKeyTransition(current.Snapshot(), candidateSnapshot) {
			return ActivatedApprovalPolicy{}, ErrApprovalPolicyLineage
		}
	case errors.Is(loadErr, ErrApprovalPolicyStoreEmpty):
		expected = approvalPolicyGenesisHead(namespace)
		if candidate.Revision() != 1 || candidate.PreviousPolicyDigest() != "" {
			return ActivatedApprovalPolicy{}, ErrApprovalPolicyGap
		}
	default:
		return ActivatedApprovalPolicy{}, ErrApprovalPolicyStoreUnavailable
	}
	record, err := newApprovalPolicyActivationRecord(candidate, activationInstant)
	if err != nil {
		return ActivatedApprovalPolicy{}, err
	}
	commitErr := activator.store.CompareAndActivate(
		ctx,
		namespace,
		expected,
		record,
		candidate.CanonicalBytes(),
	)
	// Commit acknowledgement and caller cancellation cannot determine durable outcome. Reconcile
	// through a bounded fresh context that retains request values but not its cancellation signal;
	// no ActivatedApprovalPolicy is returned unless this authoritative readback succeeds.
	reconciliationContext, cancelReconciliation := context.WithTimeout(
		context.WithoutCancel(ctx),
		approvalPolicyReconciliationTimeout,
	)
	defer cancelReconciliation()
	readback, readbackErr := activator.store.Load(reconciliationContext, namespace)
	if readbackErr != nil {
		if errors.Is(readbackErr, ErrInvalidApprovalPolicyStoreState) ||
			errors.Is(commitErr, ErrInvalidApprovalPolicyStoreState) {
			return ActivatedApprovalPolicy{}, ErrInvalidApprovalPolicyStoreState
		}
		if commitErr == nil || errors.Is(commitErr, ErrApprovalPolicyCommitUncertain) {
			return ActivatedApprovalPolicy{}, ErrApprovalPolicyCommitUncertain
		}
		return ActivatedApprovalPolicy{}, ErrApprovalPolicyStoreUnavailable
	}
	activated, validationErr := activator.validateStoredState(readback, namespace)
	if validationErr != nil {
		return ActivatedApprovalPolicy{}, validationErr
	}
	if activated.Revision() == candidate.Revision() &&
		activated.PolicyDigest() == candidate.PolicyDigest() {
		return activated, nil
	}
	if commitErr == nil || errors.Is(commitErr, ErrApprovalPolicyCommitUncertain) {
		return ActivatedApprovalPolicy{}, ErrApprovalPolicyCommitUncertain
	}
	if errors.Is(commitErr, ErrApprovalPolicyActivationConflict) {
		return ActivatedApprovalPolicy{}, ErrApprovalPolicyActivationConflict
	}
	return ActivatedApprovalPolicy{}, ErrApprovalPolicyStoreUnavailable
}

func (activator ApprovalPolicyActivator) validateStoredState(
	state ApprovalPolicyStoredState,
	namespace ApprovalPolicyNamespace,
) (ActivatedApprovalPolicy, error) {
	if !validApprovalPolicyActivationRecord(state.Record) || state.Head != state.Record.Head() ||
		state.Head.PolicyID != namespace.PolicyID || state.Head.TargetDigest != namespace.TargetDigest ||
		len(state.CanonicalPolicy) == 0 {
		return ActivatedApprovalPolicy{}, ErrInvalidApprovalPolicyStoreState
	}
	policy, err := activator.verifier.Verify(state.CanonicalPolicy, state.Record.ActivatedAt)
	if err != nil || policy.PolicyID() != state.Record.PolicyID ||
		policy.Revision() != state.Record.Revision || policy.PolicyDigest() != state.Record.PolicyDigest ||
		policy.PreviousPolicyDigest() != state.Record.PreviousPolicyDigest ||
		policy.EnvelopeDigest() != state.Record.PolicyEnvelopeDigest ||
		policy.RootTrustBundleDigest() != state.Record.RootTrustBundleDigest ||
		digestApprovalPolicyTarget(policy.Snapshot().Target) != state.Record.TargetDigest ||
		approvalPolicyRevision(policy.PolicyID(), policy.Revision()) != state.Record.PolicyRevision ||
		state.Record.ApprovalVerificationEnabled == policy.Snapshot().DenyAll {
		return ActivatedApprovalPolicy{}, ErrInvalidApprovalPolicyStoreState
	}
	fingerprints := policy.RootFingerprints()
	slices.Sort(fingerprints)
	if !slices.Equal(fingerprints, state.Record.RootSignerFingerprints) {
		return ActivatedApprovalPolicy{}, ErrInvalidApprovalPolicyStoreState
	}
	return ActivatedApprovalPolicy{
		policy: policy,
		record: cloneApprovalPolicyActivationRecord(state.Record),
	}, nil
}

func newApprovalPolicyActivationRecord(
	policy VerifiedApprovalPolicy,
	activatedAt time.Time,
) (ApprovalPolicyActivationRecord, error) {
	snapshot := policy.Snapshot()
	fingerprints := policy.RootFingerprints()
	slices.Sort(fingerprints)
	record := ApprovalPolicyActivationRecord{
		ActivatedAt:                 activatedAt,
		ApprovalVerificationEnabled: !snapshot.DenyAll,
		Format:                      ApprovalPolicyActivationRecordFormat,
		MutationAuthorized:          false,
		PolicyDigest:                policy.PolicyDigest(),
		PolicyEnvelopeDigest:        policy.EnvelopeDigest(),
		PolicyID:                    policy.PolicyID(),
		PolicyRevision:              approvalPolicyRevision(policy.PolicyID(), policy.Revision()),
		PreviousPolicyDigest:        policy.PreviousPolicyDigest(),
		Revision:                    policy.Revision(),
		RootSignerFingerprints:      fingerprints,
		RootTrustBundleDigest:       policy.RootTrustBundleDigest(),
		TargetDigest:                digestApprovalPolicyTarget(snapshot.Target),
	}
	if !validApprovalPolicyActivationRecord(record) {
		return ApprovalPolicyActivationRecord{}, ErrInvalidApprovalPolicyStoreState
	}
	unsigned, err := marshalApprovalPolicyActivationRecordCanonical(record)
	if err != nil {
		return ApprovalPolicyActivationRecord{}, ErrInvalidApprovalPolicyStoreState
	}
	record.ActivationRecordDigest = digestApprovalPolicyActivationRecord(unsigned)
	if !validApprovalPolicyActivationRecord(record) {
		return ApprovalPolicyActivationRecord{}, ErrInvalidApprovalPolicyStoreState
	}
	return record, nil
}

func validApprovalPolicyActivationRecord(record ApprovalPolicyActivationRecord) bool {
	if record.Format != ApprovalPolicyActivationRecordFormat || !canonicalPolicyTime(record.ActivatedAt) ||
		record.MutationAuthorized || !canonicalDigest.MatchString(record.PolicyDigest) ||
		!canonicalDigest.MatchString(record.PolicyEnvelopeDigest) || !canonicalIdentity(record.PolicyID) ||
		!strings.HasPrefix(record.PolicyID, "approval-policy/") || record.Revision == 0 ||
		record.Revision > maximumApprovalPolicyRevision ||
		record.PolicyRevision != approvalPolicyRevision(record.PolicyID, record.Revision) ||
		!canonicalDigest.MatchString(record.RootTrustBundleDigest) ||
		!canonicalDigest.MatchString(record.TargetDigest) ||
		len(record.RootSignerFingerprints) < minimumApprovalPolicyRootQuorum ||
		len(record.RootSignerFingerprints) > maximumApprovalPolicyRoots ||
		!slices.IsSorted(record.RootSignerFingerprints) {
		return false
	}
	if (record.Revision == 1 && record.PreviousPolicyDigest != "") ||
		(record.Revision > 1 && !canonicalDigest.MatchString(record.PreviousPolicyDigest)) {
		return false
	}
	seen := make(map[string]struct{}, len(record.RootSignerFingerprints))
	for _, fingerprint := range record.RootSignerFingerprints {
		if !canonicalDigest.MatchString(fingerprint) {
			return false
		}
		if _, duplicate := seen[fingerprint]; duplicate {
			return false
		}
		seen[fingerprint] = struct{}{}
	}
	if record.ActivationRecordDigest == "" {
		return true
	}
	if !canonicalDigest.MatchString(record.ActivationRecordDigest) {
		return false
	}
	unsigned := cloneApprovalPolicyActivationRecord(record)
	unsigned.ActivationRecordDigest = ""
	canonical, err := marshalApprovalPolicyActivationRecordCanonical(unsigned)
	return err == nil && digestApprovalPolicyActivationRecord(canonical) == record.ActivationRecordDigest
}

func validApprovalPolicyKeyTransition(previous, candidate ApprovalPolicySnapshot) bool {
	if previous.PolicyID != candidate.PolicyID || previous.Target != candidate.Target ||
		candidate.Revision != previous.Revision+1 ||
		candidate.PreviousPolicyDigest != previous.PolicyDigest {
		return false
	}
	candidateKeys := make(map[string]ApprovalPolicyKeySnapshot, len(candidate.Keys))
	for _, key := range candidate.Keys {
		candidateKeys[key.KeyID] = key
	}
	for _, previousKey := range previous.Keys {
		candidateKey, exists := candidateKeys[previousKey.KeyID]
		if !exists || !sameApprovalPolicyKeyIdentity(previousKey, candidateKey) {
			return false
		}
		switch previousKey.Status {
		case ApprovalPolicyKeyActive:
			if candidateKey.Status != ApprovalPolicyKeyActive &&
				candidateKey.Status != ApprovalPolicyKeyRevoked {
				return false
			}
		case ApprovalPolicyKeyRevoked:
			if candidateKey != previousKey {
				return false
			}
		default:
			return false
		}
	}
	return true
}

func sameApprovalPolicyKeyIdentity(left, right ApprovalPolicyKeySnapshot) bool {
	return left.Algorithm == right.Algorithm && left.ApproverIdentity == right.ApproverIdentity &&
		left.Generation == right.Generation && left.KeyID == right.KeyID &&
		left.NotAfter == right.NotAfter && left.NotBefore == right.NotBefore &&
		left.PublicKey == right.PublicKey && left.PublicKeyFingerprint == right.PublicKeyFingerprint &&
		left.ReferencePrefix == right.ReferencePrefix
}

func approvalPolicyNamespace(snapshot ApprovalPolicySnapshot) ApprovalPolicyNamespace {
	return ApprovalPolicyNamespace{
		PolicyID:     snapshot.PolicyID,
		TargetDigest: digestApprovalPolicyTarget(snapshot.Target),
	}
}

func approvalPolicyGenesisHead(namespace ApprovalPolicyNamespace) ApprovalPolicyHead {
	return ApprovalPolicyHead{
		PolicyID:     namespace.PolicyID,
		Revision:     0,
		TargetDigest: namespace.TargetDigest,
	}
}

func validApprovalPolicyVerifier(verifier ApprovalPolicyVerifier) bool {
	return len(verifier.roots) >= verifier.quorum && verifier.quorum >= minimumApprovalPolicyRootQuorum &&
		verifier.clockSkew >= 0 && verifier.clockSkew <= maximumApprovalClockSkew &&
		canonicalDigest.MatchString(verifier.bundleDigest) && canonicalIdentity(verifier.policyID) &&
		validApprovalPolicyTarget(verifier.target)
}

func validApprovalVerifier(verifier ApprovalVerifier) bool {
	if !validApprovalVerifierPolicy(approvalVerifierPolicy{
		activationRecordDigest: verifier.activationRecordDigest,
		clockSkew:              verifier.clockSkew,
		maximumLifetime:        verifier.maximumLifetime,
		policyDigest:           verifier.policyDigest,
		policyID:               verifier.policyID,
		policyNotAfter:         verifier.policyNotAfter,
		policyNotBefore:        verifier.policyNotBefore,
		policyRevision:         verifier.policyRevision,
		policySequence:         verifier.policySequence,
		rootTrustBundleDigest:  verifier.rootTrustBundleDigest,
		target:                 verifier.target,
		targetBound:            verifier.targetBound,
		verificationEnabled:    verifier.verificationEnabled,
	}) || len(verifier.keys) > maximumApprovalKeys ||
		(verifier.verificationEnabled && len(verifier.keys) == 0) ||
		(!verifier.verificationEnabled && len(verifier.keys) != 0) {
		return false
	}
	for keyID, key := range verifier.keys {
		if keyID == "" || !canonicalIdentity(keyID) ||
			!canonicalIdentity(key.approverIdentity) || !canonicalIdentity(key.generation) ||
			!canonicalDigest.MatchString(key.fingerprint) || len(key.publicKey) != ed25519.PublicKeySize ||
			approvalKeyFingerprint(key.publicKey) != key.fingerprint ||
			!canonicalPolicyTime(key.notBefore) || !canonicalPolicyTime(key.notAfter) ||
			!key.notAfter.After(key.notBefore) || !canonicalIdentity(key.scope.CellID) ||
			!canonicalIdentity(key.scope.DeploymentID) || !validApprovalReferencePrefix(key.scope.ReferencePrefix) {
			return false
		}
	}
	return true
}

func nilInterface(value any) bool {
	if value == nil {
		return true
	}
	reflected := reflect.ValueOf(value)
	switch reflected.Kind() {
	case reflect.Chan, reflect.Func, reflect.Interface, reflect.Map, reflect.Pointer, reflect.Slice:
		return reflected.IsNil()
	default:
		return false
	}
}

func marshalApprovalPolicyActivationRecordCanonical(record ApprovalPolicyActivationRecord) ([]byte, error) {
	var output bytes.Buffer
	encoder := json.NewEncoder(&output)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(record); err != nil {
		return nil, err
	}
	return bytes.TrimSuffix(output.Bytes(), []byte("\n")), nil
}

func decodeApprovalPolicyActivationRecord(raw []byte) (ApprovalPolicyActivationRecord, error) {
	if len(raw) == 0 || len(raw) > maximumApprovalPolicyActivationRecordBytes ||
		!utf8.Valid(raw) || !norm.NFC.IsNormal(raw) {
		return ApprovalPolicyActivationRecord{}, ErrInvalidApprovalPolicyStoreState
	}
	structural := json.NewDecoder(bytes.NewReader(raw))
	structural.UseNumber()
	value, err := decodeStrictJSONValue(structural, 0)
	if err != nil {
		return ApprovalPolicyActivationRecord{}, ErrInvalidApprovalPolicyStoreState
	}
	if _, object := value.(map[string]any); !object {
		return ApprovalPolicyActivationRecord{}, ErrInvalidApprovalPolicyStoreState
	}
	if _, err := structural.Token(); !errors.Is(err, io.EOF) {
		return ApprovalPolicyActivationRecord{}, ErrInvalidApprovalPolicyStoreState
	}
	var record ApprovalPolicyActivationRecord
	typed := json.NewDecoder(bytes.NewReader(raw))
	typed.DisallowUnknownFields()
	if err := typed.Decode(&record); err != nil {
		return ApprovalPolicyActivationRecord{}, ErrInvalidApprovalPolicyStoreState
	}
	if err := typed.Decode(&struct{}{}); !errors.Is(err, io.EOF) ||
		!validApprovalPolicyActivationRecord(record) {
		return ApprovalPolicyActivationRecord{}, ErrInvalidApprovalPolicyStoreState
	}
	canonical, err := marshalApprovalPolicyActivationRecordCanonical(record)
	if err != nil || !bytes.Equal(raw, canonical) {
		return ApprovalPolicyActivationRecord{}, ErrInvalidApprovalPolicyStoreState
	}
	return record, nil
}

func digestApprovalPolicyActivationRecord(canonical []byte) string {
	return domainSeparatedDigest(approvalPolicyActivationDigestDomain, canonical)
}

func digestApprovalPolicyTarget(target ApprovalPolicyTarget) string {
	var output bytes.Buffer
	encoder := json.NewEncoder(&output)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(target); err != nil {
		return ""
	}
	return domainSeparatedDigest(
		approvalPolicyTargetDigestDomain,
		bytes.TrimSuffix(output.Bytes(), []byte("\n")),
	)
}

func cloneApprovalPolicyActivationRecord(record ApprovalPolicyActivationRecord) ApprovalPolicyActivationRecord {
	clone := record
	clone.RootSignerFingerprints = slices.Clone(record.RootSignerFingerprints)
	return clone
}
