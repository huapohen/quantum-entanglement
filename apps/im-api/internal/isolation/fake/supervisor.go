// Package fake provides a deterministic control-plane fake. It never executes package bytes.
// Its guarantees are deliberately volatile and non-isolating; passing these tests does not prove
// process, container, UID, network, filesystem, or microVM enforcement.
package fake

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"strconv"
	"sync"
	"time"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/isolation"
)

type Durability string
type Isolation string

const (
	DurabilityVolatile Durability = "volatile"
	IsolationNone      Isolation  = "none"
)

type Guarantees struct {
	Durability   Durability
	Isolation    Isolation
	ExecutesCode bool
}

type TerminationMode string

const (
	TerminateGracefully  TerminationMode = "graceful"
	TerminateForced      TerminationMode = "forced"
	TerminateWaitUnknown TerminationMode = "wait_unknown"
	TerminateResiduals   TerminationMode = "residuals"
)

type Config struct {
	Now      time.Time
	TenantID string
	TaskID   string
}

type Supervisor struct {
	mu sync.Mutex

	now        time.Time
	tenantID   string
	taskID     string
	generation map[string]uint64
	instances  map[string]isolation.ProcessInstance
	operations map[string]operationRecord
	modes      map[string]TerminationMode
	launches   uint64
}

type operationRecord struct {
	requestDigest isolation.SHA256Digest
	observation   isolation.OperationObservation
}

func New(config Config) (*Supervisor, error) {
	if config.Now.IsZero() || config.TenantID == "" || config.TaskID == "" {
		return nil, isolation.ErrInvalidCommand
	}
	return &Supervisor{
		now:        config.Now.Round(0).UTC(),
		tenantID:   config.TenantID,
		taskID:     config.TaskID,
		generation: make(map[string]uint64),
		instances:  make(map[string]isolation.ProcessInstance),
		operations: make(map[string]operationRecord),
		modes:      make(map[string]TerminationMode),
	}, nil
}

func (supervisor *Supervisor) Guarantees() Guarantees {
	return Guarantees{Durability: DurabilityVolatile, Isolation: IsolationNone, ExecutesCode: false}
}

func (supervisor *Supervisor) SetTerminationMode(executionID string, mode TerminationMode) error {
	if executionID == "" || !validTerminationMode(mode) {
		return isolation.ErrInvalidCommand
	}
	supervisor.mu.Lock()
	defer supervisor.mu.Unlock()
	supervisor.modes[executionID] = mode
	return nil
}

func (supervisor *Supervisor) Launch(
	ctx context.Context,
	command isolation.LaunchCommand,
) (isolation.LaunchReceipt, error) {
	if err := ctx.Err(); err != nil {
		return isolation.LaunchReceipt{}, err
	}
	supervisor.mu.Lock()
	defer supervisor.mu.Unlock()
	if err := isolation.ValidateLaunchCommandAt(command, supervisor.now); err != nil {
		return isolation.LaunchReceipt{}, err
	}
	if record, exists := supervisor.operations[command.OperationID]; exists {
		if record.requestDigest != command.RequestDigest {
			return isolation.LaunchReceipt{}, isolation.ErrIdempotencyConflict
		}
		if record.observation.Launch == nil {
			return isolation.LaunchReceipt{}, isolation.ErrIdempotencyConflict
		}
		return *record.observation.Launch, nil
	}
	if supervisor.generation[command.ExecutionID] != command.ExpectedPreviousGeneration {
		return isolation.LaunchReceipt{}, isolation.ErrStaleGeneration
	}

	generation := command.ExpectedPreviousGeneration + 1
	instance := isolation.ProcessInstance{
		InstanceID:             "instance-" + command.ExecutionID + "-" + strconv.FormatUint(generation, 10),
		ExecutionID:            command.ExecutionID,
		GrantID:                command.RuntimeGrantRef.ID,
		TenantID:               supervisor.tenantID,
		TaskID:                 supervisor.taskID,
		AttemptID:              command.AttemptID,
		PackageArtifactDigest:  command.PackageVersionRef.Digest,
		PackageManifestDigest:  command.PackageVersionRef.Digest,
		IsolationProfileDigest: command.IsolationProfileRef.Digest,
		RuntimeGrantDigest:     command.RuntimeGrantRef.Digest,
		Generation:             generation,
		FenceRevision:          1,
		FenceDigest:            fakeDigest("fence", command.ExecutionID, strconv.FormatUint(generation, 10), "1"),
		State:                  isolation.ProcessRunning,
	}
	receipt, err := isolation.SealLaunchReceipt(isolation.LaunchReceipt{
		SchemaVersion:             1,
		OperationID:               command.OperationID,
		RequestDigest:             command.RequestDigest,
		PackageVersionRef:         command.PackageVersionRef,
		IsolationProfileRef:       command.IsolationProfileRef,
		RuntimeGrantRef:           command.RuntimeGrantRef,
		Instance:                  instance,
		EnforcementEvidenceDigest: fakeDigest("fake-no-isolation", string(command.RequestDigest)),
		StartedAt:                 supervisor.now,
	})
	if err != nil {
		return isolation.LaunchReceipt{}, err
	}
	supervisor.generation[command.ExecutionID] = generation
	supervisor.instances[command.ExecutionID] = instance
	supervisor.launches++
	supervisor.operations[command.OperationID] = operationRecord{
		requestDigest: command.RequestDigest,
		observation: isolation.OperationObservation{
			OperationID: command.OperationID, RequestDigest: command.RequestDigest,
			State: isolation.OperationCompleted, Launch: &receipt,
		},
	}
	return receipt, nil
}

func (supervisor *Supervisor) TerminateAndReap(
	ctx context.Context,
	command isolation.TerminateCommand,
) (isolation.TerminationObservation, error) {
	if err := ctx.Err(); err != nil {
		return isolation.TerminationObservation{}, err
	}
	supervisor.mu.Lock()
	defer supervisor.mu.Unlock()
	if err := isolation.ValidateTerminateCommand(command); err != nil {
		return isolation.TerminationObservation{}, err
	}
	if record, exists := supervisor.operations[command.OperationID]; exists {
		if record.requestDigest != command.RequestDigest {
			return isolation.TerminationObservation{}, isolation.ErrIdempotencyConflict
		}
		if record.observation.Termination == nil {
			return isolation.TerminationObservation{}, isolation.ErrIdempotencyConflict
		}
		return cloneTerminationObservation(*record.observation.Termination), nil
	}
	instance, exists := supervisor.instances[command.Target.ExecutionID]
	if !exists || isolation.ValidateProcessFence(instance, command.Target) != nil ||
		instance.RuntimeGrantDigest != command.RuntimeGrantDigest {
		return isolation.TerminationObservation{}, isolation.ErrStaleGeneration
	}

	advanced := command.Target
	advanced.FenceRevision++
	advanced.FenceDigest = fakeDigest(
		"fence", advanced.ExecutionID, strconv.FormatUint(advanced.Generation, 10),
		strconv.FormatUint(advanced.FenceRevision, 10),
	)
	mode := supervisor.modes[command.Target.ExecutionID]
	if mode == "" {
		mode = TerminateGracefully
	}
	observation, err := supervisor.terminationObservation(command, advanced, mode)
	if err != nil {
		return isolation.TerminationObservation{}, err
	}
	instance.FenceRevision = advanced.FenceRevision
	instance.FenceDigest = advanced.FenceDigest
	if observation.ProcessOutcome == isolation.ProcessOutcomeReleased {
		instance.State = isolation.ProcessReleased
	} else {
		instance.State = isolation.ProcessQuarantined
	}
	supervisor.instances[command.Target.ExecutionID] = instance
	storedObservation := cloneTerminationObservation(observation)
	supervisor.operations[command.OperationID] = operationRecord{
		requestDigest: command.RequestDigest,
		observation: isolation.OperationObservation{
			OperationID: command.OperationID, RequestDigest: command.RequestDigest,
			State: isolation.OperationCompleted, Termination: &storedObservation,
		},
	}
	return cloneTerminationObservation(observation), nil
}

func (supervisor *Supervisor) GetExecution(
	ctx context.Context,
	fence isolation.ProcessFence,
) (isolation.ProcessInstance, error) {
	if err := ctx.Err(); err != nil {
		return isolation.ProcessInstance{}, err
	}
	supervisor.mu.Lock()
	defer supervisor.mu.Unlock()
	instance, exists := supervisor.instances[fence.ExecutionID]
	if !exists || isolation.ValidateProcessFence(instance, fence) != nil {
		return isolation.ProcessInstance{}, isolation.ErrStaleGeneration
	}
	return instance, nil
}

func (supervisor *Supervisor) GetOperation(
	ctx context.Context,
	operationID string,
) (isolation.OperationObservation, error) {
	if err := ctx.Err(); err != nil {
		return isolation.OperationObservation{}, err
	}
	supervisor.mu.Lock()
	defer supervisor.mu.Unlock()
	record, exists := supervisor.operations[operationID]
	if !exists {
		return isolation.OperationObservation{}, isolation.ErrControlOutcomeUnknown
	}
	return cloneOperationObservation(record.observation), nil
}

func (supervisor *Supervisor) LaunchCount() uint64 {
	supervisor.mu.Lock()
	defer supervisor.mu.Unlock()
	return supervisor.launches
}

func (supervisor *Supervisor) terminationObservation(
	command isolation.TerminateCommand,
	advanced isolation.ProcessFence,
	mode TerminationMode,
) (isolation.TerminationObservation, error) {
	effectOutcome := isolation.EffectNotApplicable
	reconcileRequired := false
	if command.EffectClass == isolation.EffectExternal {
		effectOutcome = isolation.EffectDispatchedUnknown
		reconcileRequired = true
	}
	observation := isolation.TerminationObservation{
		SchemaVersion:     1,
		OperationID:       command.OperationID,
		RequestDigest:     command.RequestDigest,
		Target:            command.Target,
		AdvancedFence:     advanced,
		EffectOutcome:     effectOutcome,
		ReconcileRequired: reconcileRequired,
		Cancel: &isolation.CancelReceipt{
			Target: advanced, RequestedAt: supervisor.now,
			EvidenceDigest: fakeDigest("cancel", string(command.RequestDigest)),
		},
	}
	switch mode {
	case TerminateGracefully:
		observation.Path = isolation.TerminationGraceful
	case TerminateForced:
		observation.Path = isolation.TerminationForced
		observation.Kill = supervisor.killReceipt(command, advanced)
	case TerminateWaitUnknown, TerminateResiduals:
		observation.Path = isolation.TerminationUnverified
		observation.ProcessOutcome = isolation.ProcessOutcomeQuarantined
		observation.Kill = supervisor.killReceipt(command, advanced)
		reason := "wait_unverified"
		if mode == TerminateResiduals {
			reason = "residuals_detected"
		}
		observation.Quarantine = &isolation.QuarantineReceipt{
			Target: advanced, Reason: reason, RecordedAt: supervisor.now.Add(4 * time.Second),
			OperatorVisible: true, EvidenceDigest: fakeDigest("quarantine", string(command.RequestDigest), reason),
		}
		return isolation.SealTerminationObservation(observation)
	default:
		return isolation.TerminationObservation{}, isolation.ErrInvalidCommand
	}
	observation.ProcessOutcome = isolation.ProcessOutcomeReleased
	observation.Exit = &isolation.ExitReceipt{
		Target: advanced, ObservedAt: supervisor.now.Add(2 * time.Second), ExactInstanceExited: true,
		WaitStatusDigest: fakeDigest("wait", string(command.RequestDigest)),
	}
	observation.Reap = &isolation.ReapReceipt{
		Target: advanced, ReapedAt: supervisor.now.Add(3 * time.Second), DescendantsEmpty: true,
		EvidenceDigest: fakeDigest("reap", string(command.RequestDigest)),
	}
	observation.Release = &isolation.ReleaseReceipt{
		Target: advanced, ReleasedAt: supervisor.now.Add(4 * time.Second), RuntimeGrantRevoked: true,
		NetworkReleased: true, MountsReleased: true, WorkspaceReleased: true,
		EvidenceDigest: fakeDigest("release", string(command.RequestDigest)),
	}
	return isolation.SealTerminationObservation(observation)
}

func (supervisor *Supervisor) killReceipt(
	command isolation.TerminateCommand,
	advanced isolation.ProcessFence,
) *isolation.KillReceipt {
	return &isolation.KillReceipt{
		Target: advanced, Method: "kill_tree", IssuedAt: supervisor.now.Add(time.Second),
		EvidenceDigest: fakeDigest("kill", string(command.RequestDigest)),
	}
}

func validTerminationMode(mode TerminationMode) bool {
	return mode == TerminateGracefully || mode == TerminateForced ||
		mode == TerminateWaitUnknown || mode == TerminateResiduals
}

func cloneOperationObservation(observation isolation.OperationObservation) isolation.OperationObservation {
	cloned := observation
	if observation.Launch != nil {
		launch := *observation.Launch
		cloned.Launch = &launch
	}
	if observation.Termination != nil {
		termination := cloneTerminationObservation(*observation.Termination)
		cloned.Termination = &termination
	}
	return cloned
}

func cloneTerminationObservation(observation isolation.TerminationObservation) isolation.TerminationObservation {
	cloned := observation
	if observation.Cancel != nil {
		receipt := *observation.Cancel
		cloned.Cancel = &receipt
	}
	if observation.Kill != nil {
		receipt := *observation.Kill
		cloned.Kill = &receipt
	}
	if observation.Exit != nil {
		receipt := *observation.Exit
		cloned.Exit = &receipt
	}
	if observation.Reap != nil {
		receipt := *observation.Reap
		cloned.Reap = &receipt
	}
	if observation.Release != nil {
		receipt := *observation.Release
		cloned.Release = &receipt
	}
	if observation.Quarantine != nil {
		receipt := *observation.Quarantine
		cloned.Quarantine = &receipt
	}
	return cloned
}

func fakeDigest(parts ...string) isolation.SHA256Digest {
	digest := sha256.New()
	for _, part := range parts {
		_, _ = digest.Write([]byte(part))
		_, _ = digest.Write([]byte{0})
	}
	return isolation.SHA256Digest("sha256:" + hex.EncodeToString(digest.Sum(nil)))
}

var _ isolation.SupervisorClient = (*Supervisor)(nil)
