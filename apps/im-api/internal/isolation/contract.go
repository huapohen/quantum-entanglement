package isolation

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"regexp"
	"slices"
	"time"
)

const (
	contractSchemaV1    = 1
	maxIdentifierBytes  = 256
	profileDigestDomain = "wanwork.im/execution-isolation-profile/1\n"
	runtimeGrantDomain  = "wanwork.im/runtime-grant/1\n"
)

var (
	ErrInvalidPackage          = errors.New("invalid executable package version")
	ErrInvalidIsolationProfile = errors.New("invalid execution isolation profile")
	ErrInvalidRuntimeGrant     = errors.New("invalid runtime grant")
	ErrInvalidLaunchRequest    = errors.New("invalid isolated execution launch request")
	ErrStaleGeneration         = errors.New("stale execution generation")

	identifierPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:/-]*$`)
	digestPattern     = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)
)

type SHA256Digest string

type ExecutablePackageVersion struct {
	SchemaVersion     uint32       `json:"schemaVersion"`
	PackageID         string       `json:"packageId"`
	Version           string       `json:"version"`
	ArtifactDigest    SHA256Digest `json:"artifactDigest"`
	ManifestDigest    SHA256Digest `json:"manifestDigest"`
	AdmissionRevision uint64       `json:"admissionRevision"`
}

type IsolationKind string

const (
	IsolationSeparateUID IsolationKind = "separate_uid_process"
	IsolationContainer   IsolationKind = "container"
	IsolationMicroVM     IsolationKind = "microvm"
)

type WorkspaceMode string

const (
	WorkspaceReadOnly    WorkspaceMode = "read_only"
	WorkspaceEphemeralRW WorkspaceMode = "ephemeral_read_write"
)

type NetworkMode string

const (
	NetworkDefaultDeny NetworkMode = "default_deny"
	NetworkBrokerOnly  NetworkMode = "broker_only"
)

type FilesystemPolicy struct {
	ReadOnlyRoot       bool          `json:"readOnlyRoot"`
	Workspace          WorkspaceMode `json:"workspace"`
	MountHostHome      bool          `json:"mountHostHome"`
	MountRuntimeSocket bool          `json:"mountRuntimeSocket"`
}

type ProcessPolicy struct {
	SeparateUID  bool   `json:"separateUid"`
	Privileged   bool   `json:"privileged"`
	HostPID      bool   `json:"hostPid"`
	MaxProcesses uint32 `json:"maxProcesses"`
}

type NetworkPolicy struct {
	Mode        NetworkMode `json:"mode"`
	HostNetwork bool        `json:"hostNetwork"`
}

type ResourcePolicy struct {
	MemoryBytes uint64        `json:"memoryBytes"`
	DiskBytes   uint64        `json:"diskBytes"`
	CPUTime     time.Duration `json:"cpuTime"`
	WallTime    time.Duration `json:"wallTime"`
}

// ExecutionIsolationProfile is a declarative, immutable claim. A profile digest is not proof
// that an adapter enforced it; production supervisors must return measured launch evidence.
type ExecutionIsolationProfile struct {
	SchemaVersion uint32           `json:"schemaVersion"`
	ProfileID     string           `json:"profileId"`
	Revision      uint64           `json:"revision"`
	Kind          IsolationKind    `json:"kind"`
	Filesystem    FilesystemPolicy `json:"filesystem"`
	Process       ProcessPolicy    `json:"process"`
	Network       NetworkPolicy    `json:"network"`
	Resources     ResourcePolicy   `json:"resources"`
	Digest        SHA256Digest     `json:"digest"`
}

type EffectClass string

const (
	EffectPure     EffectClass = "pure"
	EffectExternal EffectClass = "external_receipt_reconciled"
)

// RuntimeGrant is host-issued scope metadata, not a bearer secret. Binding digests identify
// policy-approved leases; secret material and privileged runtime handles are resolved only by
// the isolated supervisor after an action-time fence check.
type RuntimeGrant struct {
	SchemaVersion              uint32         `json:"schemaVersion"`
	GrantID                    string         `json:"grantId"`
	TenantID                   string         `json:"tenantId"`
	WorkspaceID                string         `json:"workspaceId"`
	TaskID                     string         `json:"taskId"`
	AttemptID                  string         `json:"attemptId"`
	ActionID                   string         `json:"actionId"`
	ExecutionID                string         `json:"executionId"`
	PackageArtifactDigest      SHA256Digest   `json:"packageArtifactDigest"`
	PackageManifestDigest      SHA256Digest   `json:"packageManifestDigest"`
	PackageAdmissionRevision   uint64         `json:"packageAdmissionRevision"`
	IsolationProfileDigest     SHA256Digest   `json:"isolationProfileDigest"`
	ExpectedPreviousGeneration uint64         `json:"expectedPreviousGeneration"`
	PolicyRevision             uint64         `json:"policyRevision"`
	ApprovalRevision           uint64         `json:"approvalRevision"`
	RevocationEpoch            uint64         `json:"revocationEpoch"`
	IssuedAt                   time.Time      `json:"issuedAt"`
	ExpiresAt                  time.Time      `json:"expiresAt"`
	MaxUses                    uint32         `json:"maxUses"`
	EffectClass                EffectClass    `json:"effectClass"`
	CapabilityBindings         []SHA256Digest `json:"capabilityBindings"`
	SecretBindings             []SHA256Digest `json:"secretBindings"`
	EgressBindings             []SHA256Digest `json:"egressBindings"`
	Digest                     SHA256Digest   `json:"digest"`
}

// ResolvedLaunchAdmission is a supervisor-side validation view, not the IPC launch command.
// Its digest functions prove deterministic content identity only; they do not confer admission.
// A supervisor must resolve package, profile, and grant from host-owned immutable references.
type ResolvedLaunchAdmission struct {
	Package ExecutablePackageVersion  `json:"package"`
	Profile ExecutionIsolationProfile `json:"profile"`
	Grant   RuntimeGrant              `json:"grant"`
}

type ProcessState string

const (
	ProcessStarting        ProcessState = "starting"
	ProcessRunning         ProcessState = "running"
	ProcessCancelRequested ProcessState = "cancel_requested"
	ProcessKillRequested   ProcessState = "kill_requested"
	ProcessExited          ProcessState = "exited"
	ProcessReaped          ProcessState = "reaped"
	ProcessQuarantined     ProcessState = "quarantined"
)

// ProcessInstance contains safe audit identity only. It is not a process handle and grants no
// authority by possession; only the supervisor service owns privileged runtime capabilities.
type ProcessInstance struct {
	InstanceID             string       `json:"instanceId"`
	ExecutionID            string       `json:"executionId"`
	GrantID                string       `json:"grantId"`
	TenantID               string       `json:"tenantId"`
	TaskID                 string       `json:"taskId"`
	AttemptID              string       `json:"attemptId"`
	PackageArtifactDigest  SHA256Digest `json:"packageArtifactDigest"`
	PackageManifestDigest  SHA256Digest `json:"packageManifestDigest"`
	IsolationProfileDigest SHA256Digest `json:"isolationProfileDigest"`
	RuntimeGrantDigest     SHA256Digest `json:"runtimeGrantDigest"`
	Generation             uint64       `json:"generation"`
	FenceRevision          uint64       `json:"fenceRevision"`
	FenceDigest            SHA256Digest `json:"fenceDigest"`
	State                  ProcessState `json:"state"`
}

type ProcessFence struct {
	InstanceID    string       `json:"instanceId"`
	ExecutionID   string       `json:"executionId"`
	TenantID      string       `json:"tenantId"`
	TaskID        string       `json:"taskId"`
	AttemptID     string       `json:"attemptId"`
	Generation    uint64       `json:"generation"`
	FenceRevision uint64       `json:"fenceRevision"`
	FenceDigest   SHA256Digest `json:"fenceDigest"`
}

func (instance ProcessInstance) Fence() ProcessFence {
	return ProcessFence{
		InstanceID:    instance.InstanceID,
		ExecutionID:   instance.ExecutionID,
		TenantID:      instance.TenantID,
		TaskID:        instance.TaskID,
		AttemptID:     instance.AttemptID,
		Generation:    instance.Generation,
		FenceRevision: instance.FenceRevision,
		FenceDigest:   instance.FenceDigest,
	}
}

func ValidateExecutablePackageVersion(version ExecutablePackageVersion) error {
	if version.SchemaVersion != contractSchemaV1 ||
		!validIdentifier(version.PackageID) || !validIdentifier(version.Version) ||
		!validDigest(version.ArtifactDigest) || !validDigest(version.ManifestDigest) ||
		version.AdmissionRevision == 0 {
		return ErrInvalidPackage
	}
	return nil
}

func SealExecutionIsolationProfile(profile ExecutionIsolationProfile) (ExecutionIsolationProfile, error) {
	profile.Digest = ""
	if !validIsolationProfileFields(profile) {
		return ExecutionIsolationProfile{}, ErrInvalidIsolationProfile
	}
	encoded, err := canonicalJSON(profile)
	if err != nil {
		return ExecutionIsolationProfile{}, ErrInvalidIsolationProfile
	}
	profile.Digest = digestBytes(profileDigestDomain, encoded)
	return profile, nil
}

func ValidateExecutionIsolationProfile(profile ExecutionIsolationProfile) error {
	sealed, err := SealExecutionIsolationProfile(profile)
	if err != nil || profile.Digest == "" || sealed.Digest != profile.Digest {
		return ErrInvalidIsolationProfile
	}
	return nil
}

func SealRuntimeGrant(grant RuntimeGrant) (RuntimeGrant, error) {
	grant.Digest = ""
	grant.IssuedAt = grant.IssuedAt.Round(0).UTC()
	grant.ExpiresAt = grant.ExpiresAt.Round(0).UTC()
	grant.CapabilityBindings = sortedDigestSnapshot(grant.CapabilityBindings)
	grant.SecretBindings = sortedDigestSnapshot(grant.SecretBindings)
	grant.EgressBindings = sortedDigestSnapshot(grant.EgressBindings)
	if !validRuntimeGrantFields(grant) {
		return RuntimeGrant{}, ErrInvalidRuntimeGrant
	}
	encoded, err := canonicalJSON(grant)
	if err != nil {
		return RuntimeGrant{}, ErrInvalidRuntimeGrant
	}
	grant.Digest = digestBytes(runtimeGrantDomain, encoded)
	return grant, nil
}

func ValidateRuntimeGrantAt(grant RuntimeGrant, now time.Time) error {
	sealed, err := SealRuntimeGrant(grant)
	if err != nil || grant.Digest == "" || sealed.Digest != grant.Digest ||
		now.Before(grant.IssuedAt) || !now.Before(grant.ExpiresAt) {
		return ErrInvalidRuntimeGrant
	}
	return nil
}

func ValidateResolvedLaunchAdmissionAt(request ResolvedLaunchAdmission, now time.Time) error {
	if ValidateExecutablePackageVersion(request.Package) != nil ||
		ValidateExecutionIsolationProfile(request.Profile) != nil ||
		ValidateRuntimeGrantAt(request.Grant, now) != nil ||
		request.Grant.PackageArtifactDigest != request.Package.ArtifactDigest ||
		request.Grant.PackageManifestDigest != request.Package.ManifestDigest ||
		request.Grant.PackageAdmissionRevision != request.Package.AdmissionRevision ||
		request.Grant.IsolationProfileDigest != request.Profile.Digest {
		return ErrInvalidLaunchRequest
	}
	return nil
}

func ValidateProcessFence(instance ProcessInstance, fence ProcessFence) error {
	if !validProcessInstance(instance) || !validProcessFence(fence) {
		return ErrStaleGeneration
	}
	if instance.Fence() != fence {
		return ErrStaleGeneration
	}
	return nil
}

func validIsolationProfileFields(profile ExecutionIsolationProfile) bool {
	return profile.SchemaVersion == contractSchemaV1 && validIdentifier(profile.ProfileID) &&
		profile.Revision > 0 && validIsolationKind(profile.Kind) &&
		profile.Filesystem.ReadOnlyRoot && validWorkspaceMode(profile.Filesystem.Workspace) &&
		!profile.Filesystem.MountHostHome && !profile.Filesystem.MountRuntimeSocket &&
		profile.Process.SeparateUID && !profile.Process.Privileged && !profile.Process.HostPID &&
		profile.Process.MaxProcesses > 0 && validNetworkMode(profile.Network.Mode) &&
		!profile.Network.HostNetwork && profile.Resources.MemoryBytes > 0 &&
		profile.Resources.DiskBytes > 0 && profile.Resources.CPUTime > 0 &&
		profile.Resources.WallTime > 0
}

func validRuntimeGrantFields(grant RuntimeGrant) bool {
	return grant.SchemaVersion == contractSchemaV1 && validIdentifier(grant.GrantID) &&
		validIdentifier(grant.TenantID) && validIdentifier(grant.WorkspaceID) &&
		validIdentifier(grant.TaskID) && validIdentifier(grant.AttemptID) && validIdentifier(grant.ActionID) &&
		validIdentifier(grant.ExecutionID) &&
		validDigest(grant.PackageArtifactDigest) && validDigest(grant.PackageManifestDigest) &&
		grant.PackageAdmissionRevision > 0 && validDigest(grant.IsolationProfileDigest) &&
		grant.PolicyRevision > 0 &&
		grant.ApprovalRevision > 0 && grant.RevocationEpoch > 0 && !grant.IssuedAt.IsZero() &&
		!grant.ExpiresAt.IsZero() && grant.IssuedAt.Before(grant.ExpiresAt) && grant.MaxUses == 1 &&
		(grant.EffectClass == EffectPure || grant.EffectClass == EffectExternal) &&
		validDigestSet(grant.CapabilityBindings) && validDigestSet(grant.SecretBindings) &&
		validDigestSet(grant.EgressBindings) &&
		(grant.EffectClass != EffectPure ||
			(len(grant.CapabilityBindings) == 0 && len(grant.SecretBindings) == 0 && len(grant.EgressBindings) == 0))
}

func validProcessInstance(instance ProcessInstance) bool {
	if !validIdentifier(instance.InstanceID) || !validIdentifier(instance.ExecutionID) ||
		!validIdentifier(instance.GrantID) ||
		!validIdentifier(instance.TenantID) || !validIdentifier(instance.TaskID) ||
		!validIdentifier(instance.AttemptID) || !validDigest(instance.PackageArtifactDigest) ||
		!validDigest(instance.PackageManifestDigest) || !validDigest(instance.IsolationProfileDigest) ||
		!validDigest(instance.RuntimeGrantDigest) || instance.Generation == 0 ||
		instance.FenceRevision == 0 ||
		!validDigest(instance.FenceDigest) {
		return false
	}
	switch instance.State {
	case ProcessStarting, ProcessRunning, ProcessCancelRequested, ProcessKillRequested,
		ProcessExited, ProcessReaped, ProcessQuarantined:
		return true
	default:
		return false
	}
}

func validProcessFence(fence ProcessFence) bool {
	return validIdentifier(fence.InstanceID) && validIdentifier(fence.ExecutionID) &&
		validIdentifier(fence.TenantID) &&
		validIdentifier(fence.TaskID) && validIdentifier(fence.AttemptID) &&
		fence.Generation > 0 && fence.FenceRevision > 0 && validDigest(fence.FenceDigest)
}

func validIsolationKind(kind IsolationKind) bool {
	return kind == IsolationSeparateUID || kind == IsolationContainer || kind == IsolationMicroVM
}

func validWorkspaceMode(mode WorkspaceMode) bool {
	return mode == WorkspaceReadOnly || mode == WorkspaceEphemeralRW
}

func validNetworkMode(mode NetworkMode) bool {
	return mode == NetworkDefaultDeny || mode == NetworkBrokerOnly
}

func validIdentifier(value string) bool {
	return value != "" && len(value) <= maxIdentifierBytes && identifierPattern.MatchString(value)
}

func validDigest(value SHA256Digest) bool {
	return digestPattern.MatchString(string(value))
}

func validDigestSet(values []SHA256Digest) bool {
	for index, value := range values {
		if !validDigest(value) || (index > 0 && values[index-1] >= value) {
			return false
		}
	}
	return true
}

func sortedDigestSnapshot(values []SHA256Digest) []SHA256Digest {
	cloned := slices.Clone(values)
	slices.Sort(cloned)
	return cloned
}

func canonicalJSON(value any) ([]byte, error) {
	var output bytes.Buffer
	encoder := json.NewEncoder(&output)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(value); err != nil {
		return nil, err
	}
	return bytes.TrimSuffix(output.Bytes(), []byte("\n")), nil
}

func digestBytes(domain string, payload []byte) SHA256Digest {
	digest := sha256.New()
	_, _ = digest.Write([]byte(domain))
	_, _ = digest.Write(payload)
	return SHA256Digest("sha256:" + hex.EncodeToString(digest.Sum(nil)))
}
