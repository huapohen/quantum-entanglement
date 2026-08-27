package isolation

import (
	"context"
	"errors"
	"math"
	"time"
)

const (
	launchCommandDomain      = "wanwork.im/isolation-launch-command/1\n"
	launchReceiptDomain      = "wanwork.im/isolation-launch-receipt/1\n"
	terminateCommandDomain   = "wanwork.im/isolation-terminate-command/1\n"
	terminationReceiptDomain = "wanwork.im/isolation-termination-observation/1\n"
)

var (
	ErrInvalidCommand        = errors.New("invalid isolation supervisor command")
	ErrIdempotencyConflict   = errors.New("isolation supervisor idempotency conflict")
	ErrOperationPending      = errors.New("isolation supervisor operation pending")
	ErrControlOutcomeUnknown = errors.New("isolation supervisor control outcome unknown")
	ErrReceiptInvalid        = errors.New("invalid isolation supervisor receipt")
	ErrQuarantineRequired    = errors.New("isolation supervisor quarantine required")
	ErrReconcileRequired     = errors.New("external effect reconciliation required")
)

// VersionedRef points to a host-owned immutable record. Commands never carry raw package bytes,
// argv, environment values, host paths, mounts, sockets, secret material, or callbacks.
type VersionedRef struct {
	ID       string       `json:"id"`
	Revision uint64       `json:"revision"`
	Digest   SHA256Digest `json:"digest"`
}

type LaunchCommand struct {
	SchemaVersion              uint32       `json:"schemaVersion"`
	OperationID                string       `json:"operationId"`
	RequestDigest              SHA256Digest `json:"requestDigest"`
	ExecutionID                string       `json:"executionId"`
	ExpectedPreviousGeneration uint64       `json:"expectedPreviousGeneration"`
	PackageVersionRef          VersionedRef `json:"packageVersionRef"`
	IsolationProfileRef        VersionedRef `json:"isolationProfileRef"`
	RuntimeGrantRef            VersionedRef `json:"runtimeGrantRef"`
	AttemptID                  string       `json:"attemptId"`
	InputManifestDigest        SHA256Digest `json:"inputManifestDigest"`
	Deadline                   time.Time    `json:"deadline"`
}

type LaunchReceipt struct {
	SchemaVersion             uint32          `json:"schemaVersion"`
	OperationID               string          `json:"operationId"`
	RequestDigest             SHA256Digest    `json:"requestDigest"`
	PackageVersionRef         VersionedRef    `json:"packageVersionRef"`
	IsolationProfileRef       VersionedRef    `json:"isolationProfileRef"`
	RuntimeGrantRef           VersionedRef    `json:"runtimeGrantRef"`
	Instance                  ProcessInstance `json:"instance"`
	EnforcementEvidenceDigest SHA256Digest    `json:"enforcementEvidenceDigest"`
	StartedAt                 time.Time       `json:"startedAt"`
	Digest                    SHA256Digest    `json:"digest"`
}

type TerminationReason string

const (
	TerminationCancelled TerminationReason = "cancelled"
	TerminationDeadline  TerminationReason = "deadline"
	TerminationRevoked   TerminationReason = "revoked"
	TerminationTakeover  TerminationReason = "human_takeover"
	TerminationShutdown  TerminationReason = "supervisor_shutdown"
)

type TerminateCommand struct {
	SchemaVersion      uint32            `json:"schemaVersion"`
	OperationID        string            `json:"operationId"`
	RequestDigest      SHA256Digest      `json:"requestDigest"`
	Target             ProcessFence      `json:"target"`
	RuntimeGrantDigest SHA256Digest      `json:"runtimeGrantDigest"`
	EffectClass        EffectClass       `json:"effectClass"`
	Reason             TerminationReason `json:"reason"`
	RequestedAt        time.Time         `json:"requestedAt"`
}

type TerminationPath string

const (
	TerminationGraceful    TerminationPath = "graceful"
	TerminationForced      TerminationPath = "forced"
	TerminationAlreadyExit TerminationPath = "already_exited"
	TerminationUnverified  TerminationPath = "unverified"
)

type ProcessOutcome string

const (
	ProcessOutcomeReleased    ProcessOutcome = "released"
	ProcessOutcomeQuarantined ProcessOutcome = "quarantined"
)

type ExternalEffectOutcome string

const (
	EffectNotApplicable     ExternalEffectOutcome = "not_applicable"
	EffectDispatchedUnknown ExternalEffectOutcome = "dispatched_unknown"
)

type CancelReceipt struct {
	Target         ProcessFence `json:"target"`
	RequestedAt    time.Time    `json:"requestedAt"`
	EvidenceDigest SHA256Digest `json:"evidenceDigest"`
}

// KillReceipt proves only that the exact fenced kill-tree operation was issued. It is neither
// exit proof nor evidence that an external effect did not happen.
type KillReceipt struct {
	Target         ProcessFence `json:"target"`
	Method         string       `json:"method"`
	IssuedAt       time.Time    `json:"issuedAt"`
	EvidenceDigest SHA256Digest `json:"evidenceDigest"`
}

type ExitReceipt struct {
	Target              ProcessFence `json:"target"`
	ObservedAt          time.Time    `json:"observedAt"`
	ExactInstanceExited bool         `json:"exactInstanceExited"`
	WaitStatusDigest    SHA256Digest `json:"waitStatusDigest"`
}

type ReapReceipt struct {
	Target           ProcessFence `json:"target"`
	ReapedAt         time.Time    `json:"reapedAt"`
	DescendantsEmpty bool         `json:"descendantsEmpty"`
	EvidenceDigest   SHA256Digest `json:"evidenceDigest"`
}

type ReleaseReceipt struct {
	Target              ProcessFence `json:"target"`
	ReleasedAt          time.Time    `json:"releasedAt"`
	RuntimeGrantRevoked bool         `json:"runtimeGrantRevoked"`
	NetworkReleased     bool         `json:"networkReleased"`
	MountsReleased      bool         `json:"mountsReleased"`
	WorkspaceReleased   bool         `json:"workspaceReleased"`
	EvidenceDigest      SHA256Digest `json:"evidenceDigest"`
}

type QuarantineReceipt struct {
	Target          ProcessFence `json:"target"`
	Reason          string       `json:"reason"`
	RecordedAt      time.Time    `json:"recordedAt"`
	OperatorVisible bool         `json:"operatorVisible"`
	EvidenceDigest  SHA256Digest `json:"evidenceDigest"`
}

type TerminationObservation struct {
	SchemaVersion     uint32                `json:"schemaVersion"`
	OperationID       string                `json:"operationId"`
	RequestDigest     SHA256Digest          `json:"requestDigest"`
	Target            ProcessFence          `json:"target"`
	AdvancedFence     ProcessFence          `json:"advancedFence"`
	Path              TerminationPath       `json:"path"`
	ProcessOutcome    ProcessOutcome        `json:"processOutcome"`
	EffectOutcome     ExternalEffectOutcome `json:"effectOutcome"`
	ReconcileRequired bool                  `json:"reconcileRequired"`
	Cancel            *CancelReceipt        `json:"cancel,omitempty"`
	Kill              *KillReceipt          `json:"kill,omitempty"`
	Exit              *ExitReceipt          `json:"exit,omitempty"`
	Reap              *ReapReceipt          `json:"reap,omitempty"`
	Release           *ReleaseReceipt       `json:"release,omitempty"`
	Quarantine        *QuarantineReceipt    `json:"quarantine,omitempty"`
	Digest            SHA256Digest          `json:"digest"`
}

type OperationState string

const (
	OperationPending   OperationState = "pending"
	OperationCompleted OperationState = "completed"
	OperationUnknown   OperationState = "unknown"
)

type OperationObservation struct {
	OperationID   string                  `json:"operationId"`
	RequestDigest SHA256Digest            `json:"requestDigest"`
	State         OperationState          `json:"state"`
	Launch        *LaunchReceipt          `json:"launch,omitempty"`
	Termination   *TerminationObservation `json:"termination,omitempty"`
}

// SupervisorClient is an IPC port to a separately deployed privileged supervisor. API/Gateway
// composition must never provide an implementation backed by os/exec, Go plugins, dlopen/cgo,
// a container runtime socket, or third-party callbacks in the API process.
type SupervisorClient interface {
	Launch(context.Context, LaunchCommand) (LaunchReceipt, error)
	TerminateAndReap(context.Context, TerminateCommand) (TerminationObservation, error)
	GetExecution(context.Context, ProcessFence) (ProcessInstance, error)
	GetOperation(context.Context, string) (OperationObservation, error)
}

func SealLaunchCommand(command LaunchCommand) (LaunchCommand, error) {
	command.RequestDigest = ""
	command.Deadline = normalizeControlTime(command.Deadline)
	if !validLaunchCommandFields(command) {
		return LaunchCommand{}, ErrInvalidCommand
	}
	encoded, err := canonicalJSON(command)
	if err != nil {
		return LaunchCommand{}, ErrInvalidCommand
	}
	command.RequestDigest = digestBytes(launchCommandDomain, encoded)
	return command, nil
}

func ValidateLaunchCommandAt(command LaunchCommand, now time.Time) error {
	sealed, err := SealLaunchCommand(command)
	if err != nil || command.RequestDigest == "" || sealed.RequestDigest != command.RequestDigest ||
		!now.Before(command.Deadline) {
		return ErrInvalidCommand
	}
	return nil
}

func SealLaunchReceipt(receipt LaunchReceipt) (LaunchReceipt, error) {
	receipt.Digest = ""
	receipt.StartedAt = normalizeControlTime(receipt.StartedAt)
	if !validLaunchReceiptFields(receipt) {
		return LaunchReceipt{}, ErrReceiptInvalid
	}
	encoded, err := canonicalJSON(receipt)
	if err != nil {
		return LaunchReceipt{}, ErrReceiptInvalid
	}
	receipt.Digest = digestBytes(launchReceiptDomain, encoded)
	return receipt, nil
}

func ValidateLaunchReceipt(command LaunchCommand, receipt LaunchReceipt) error {
	sealed, err := SealLaunchReceipt(receipt)
	if err != nil || sealed.Digest != receipt.Digest || receipt.Digest == "" ||
		receipt.OperationID != command.OperationID || receipt.RequestDigest != command.RequestDigest ||
		receipt.PackageVersionRef != command.PackageVersionRef ||
		receipt.IsolationProfileRef != command.IsolationProfileRef ||
		receipt.RuntimeGrantRef != command.RuntimeGrantRef ||
		receipt.Instance.ExecutionID != command.ExecutionID ||
		receipt.Instance.AttemptID != command.AttemptID ||
		command.ExpectedPreviousGeneration == math.MaxUint64 ||
		receipt.Instance.Generation != command.ExpectedPreviousGeneration+1 ||
		receipt.Instance.State != ProcessRunning {
		return ErrReceiptInvalid
	}
	return nil
}

func SealTerminateCommand(command TerminateCommand) (TerminateCommand, error) {
	command.RequestDigest = ""
	command.RequestedAt = normalizeControlTime(command.RequestedAt)
	if !validTerminateCommandFields(command) {
		return TerminateCommand{}, ErrInvalidCommand
	}
	encoded, err := canonicalJSON(command)
	if err != nil {
		return TerminateCommand{}, ErrInvalidCommand
	}
	command.RequestDigest = digestBytes(terminateCommandDomain, encoded)
	return command, nil
}

func ValidateTerminateCommand(command TerminateCommand) error {
	sealed, err := SealTerminateCommand(command)
	if err != nil || command.RequestDigest == "" || sealed.RequestDigest != command.RequestDigest {
		return ErrInvalidCommand
	}
	return nil
}

func SealTerminationObservation(observation TerminationObservation) (TerminationObservation, error) {
	observation.Digest = ""
	normalizeTerminationTimes(&observation)
	if !validTerminationObservationFields(observation) {
		return TerminationObservation{}, ErrReceiptInvalid
	}
	encoded, err := canonicalJSON(observation)
	if err != nil {
		return TerminationObservation{}, ErrReceiptInvalid
	}
	observation.Digest = digestBytes(terminationReceiptDomain, encoded)
	return observation, nil
}

func ValidateTerminationObservation(command TerminateCommand, observation TerminationObservation) error {
	sealed, err := SealTerminationObservation(observation)
	if err != nil || observation.Digest == "" || sealed.Digest != observation.Digest ||
		observation.OperationID != command.OperationID || observation.RequestDigest != command.RequestDigest ||
		observation.Target != command.Target || !isAdvancedFence(command.Target, observation.AdvancedFence) ||
		!effectOutcomeMatches(command.EffectClass, observation) {
		return ErrReceiptInvalid
	}
	if observation.ProcessOutcome == ProcessOutcomeQuarantined {
		if observation.ReconcileRequired {
			return errors.Join(ErrQuarantineRequired, ErrReconcileRequired)
		}
		return ErrQuarantineRequired
	}
	if observation.ReconcileRequired {
		return ErrReconcileRequired
	}
	return nil
}

func validLaunchCommandFields(command LaunchCommand) bool {
	return command.SchemaVersion == contractSchemaV1 && validIdentifier(command.OperationID) &&
		validIdentifier(command.ExecutionID) && command.ExpectedPreviousGeneration < math.MaxUint64 &&
		validVersionedRef(command.PackageVersionRef) && validVersionedRef(command.IsolationProfileRef) &&
		validVersionedRef(command.RuntimeGrantRef) && validIdentifier(command.AttemptID) &&
		validDigest(command.InputManifestDigest) && !command.Deadline.IsZero()
}

func validLaunchReceiptFields(receipt LaunchReceipt) bool {
	return receipt.SchemaVersion == contractSchemaV1 && validIdentifier(receipt.OperationID) &&
		validDigest(receipt.RequestDigest) && validVersionedRef(receipt.PackageVersionRef) &&
		validVersionedRef(receipt.IsolationProfileRef) && validVersionedRef(receipt.RuntimeGrantRef) &&
		validProcessInstance(receipt.Instance) && validDigest(receipt.EnforcementEvidenceDigest) &&
		!receipt.StartedAt.IsZero()
}

func validTerminateCommandFields(command TerminateCommand) bool {
	return command.SchemaVersion == contractSchemaV1 && validIdentifier(command.OperationID) &&
		validProcessFence(command.Target) && validDigest(command.RuntimeGrantDigest) &&
		(command.EffectClass == EffectPure || command.EffectClass == EffectExternal) &&
		validTerminationReason(command.Reason) && !command.RequestedAt.IsZero()
}

func validTerminationObservationFields(observation TerminationObservation) bool {
	if observation.SchemaVersion != contractSchemaV1 || !validIdentifier(observation.OperationID) ||
		!validDigest(observation.RequestDigest) || !validProcessFence(observation.Target) ||
		!isAdvancedFence(observation.Target, observation.AdvancedFence) ||
		!validOptionalTerminationReceipts(observation) {
		return false
	}
	if observation.ProcessOutcome == ProcessOutcomeReleased {
		return validReleasedObservation(observation)
	}
	return observation.ProcessOutcome == ProcessOutcomeQuarantined &&
		observation.Path == TerminationUnverified && validQuarantineReceipt(observation.Quarantine, observation.AdvancedFence) &&
		observation.Release == nil
}

func validReleasedObservation(observation TerminationObservation) bool {
	if observation.Quarantine != nil || observation.Exit == nil || observation.Reap == nil || observation.Release == nil ||
		!validExitReceipt(observation.Exit, observation.AdvancedFence) ||
		!validReapReceipt(observation.Reap, observation.AdvancedFence) ||
		!validReleaseReceipt(observation.Release, observation.AdvancedFence) {
		return false
	}
	switch observation.Path {
	case TerminationGraceful:
		return validCancelReceipt(observation.Cancel, observation.AdvancedFence) && observation.Kill == nil
	case TerminationForced:
		return validCancelReceipt(observation.Cancel, observation.AdvancedFence) &&
			validKillReceipt(observation.Kill, observation.AdvancedFence)
	case TerminationAlreadyExit:
		return observation.Kill == nil && (observation.Cancel == nil || validCancelReceipt(observation.Cancel, observation.AdvancedFence))
	default:
		return false
	}
}

func validOptionalTerminationReceipts(observation TerminationObservation) bool {
	return (observation.Cancel == nil || validCancelReceipt(observation.Cancel, observation.AdvancedFence)) &&
		(observation.Kill == nil || validKillReceipt(observation.Kill, observation.AdvancedFence)) &&
		(observation.Exit == nil || validExitReceipt(observation.Exit, observation.AdvancedFence)) &&
		(observation.Reap == nil || validReapReceipt(observation.Reap, observation.AdvancedFence)) &&
		(observation.Release == nil || validReleaseReceipt(observation.Release, observation.AdvancedFence)) &&
		(observation.Quarantine == nil || validQuarantineReceipt(observation.Quarantine, observation.AdvancedFence))
}

func validCancelReceipt(receipt *CancelReceipt, fence ProcessFence) bool {
	return receipt != nil && receipt.Target == fence && !receipt.RequestedAt.IsZero() && validDigest(receipt.EvidenceDigest)
}

func validKillReceipt(receipt *KillReceipt, fence ProcessFence) bool {
	return receipt != nil && receipt.Target == fence && receipt.Method == "kill_tree" &&
		!receipt.IssuedAt.IsZero() && validDigest(receipt.EvidenceDigest)
}

func validExitReceipt(receipt *ExitReceipt, fence ProcessFence) bool {
	return receipt != nil && receipt.Target == fence && !receipt.ObservedAt.IsZero() &&
		receipt.ExactInstanceExited && validDigest(receipt.WaitStatusDigest)
}

func validReapReceipt(receipt *ReapReceipt, fence ProcessFence) bool {
	return receipt != nil && receipt.Target == fence && !receipt.ReapedAt.IsZero() &&
		receipt.DescendantsEmpty && validDigest(receipt.EvidenceDigest)
}

func validReleaseReceipt(receipt *ReleaseReceipt, fence ProcessFence) bool {
	return receipt != nil && receipt.Target == fence && !receipt.ReleasedAt.IsZero() &&
		receipt.RuntimeGrantRevoked && receipt.NetworkReleased && receipt.MountsReleased &&
		receipt.WorkspaceReleased && validDigest(receipt.EvidenceDigest)
}

func validQuarantineReceipt(receipt *QuarantineReceipt, fence ProcessFence) bool {
	return receipt != nil && receipt.Target == fence && validIdentifier(receipt.Reason) &&
		!receipt.RecordedAt.IsZero() && receipt.OperatorVisible && validDigest(receipt.EvidenceDigest)
}

func isAdvancedFence(previous, advanced ProcessFence) bool {
	return previous.InstanceID == advanced.InstanceID && previous.ExecutionID == advanced.ExecutionID &&
		previous.TenantID == advanced.TenantID && previous.TaskID == advanced.TaskID &&
		previous.AttemptID == advanced.AttemptID && previous.Generation == advanced.Generation &&
		advanced.FenceRevision > previous.FenceRevision && validDigest(advanced.FenceDigest) &&
		advanced.FenceDigest != previous.FenceDigest
}

func effectOutcomeMatches(effectClass EffectClass, observation TerminationObservation) bool {
	if effectClass == EffectPure {
		return observation.EffectOutcome == EffectNotApplicable && !observation.ReconcileRequired
	}
	return effectClass == EffectExternal && observation.EffectOutcome == EffectDispatchedUnknown &&
		observation.ReconcileRequired
}

func validVersionedRef(reference VersionedRef) bool {
	return validIdentifier(reference.ID) && reference.Revision > 0 && validDigest(reference.Digest)
}

func validTerminationReason(reason TerminationReason) bool {
	switch reason {
	case TerminationCancelled, TerminationDeadline, TerminationRevoked, TerminationTakeover, TerminationShutdown:
		return true
	default:
		return false
	}
}

func normalizeControlTime(value time.Time) time.Time {
	return value.Round(0).UTC()
}

func normalizeTerminationTimes(observation *TerminationObservation) {
	if observation.Cancel != nil {
		observation.Cancel.RequestedAt = normalizeControlTime(observation.Cancel.RequestedAt)
	}
	if observation.Kill != nil {
		observation.Kill.IssuedAt = normalizeControlTime(observation.Kill.IssuedAt)
	}
	if observation.Exit != nil {
		observation.Exit.ObservedAt = normalizeControlTime(observation.Exit.ObservedAt)
	}
	if observation.Reap != nil {
		observation.Reap.ReapedAt = normalizeControlTime(observation.Reap.ReapedAt)
	}
	if observation.Release != nil {
		observation.Release.ReleasedAt = normalizeControlTime(observation.Release.ReleasedAt)
	}
	if observation.Quarantine != nil {
		observation.Quarantine.RecordedAt = normalizeControlTime(observation.Quarantine.RecordedAt)
	}
}
