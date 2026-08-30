package fake

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/isolation"
)

var fakeContractTime = time.Date(2026, 8, 28, 12, 0, 0, 0, time.UTC)

func TestFakeDeclaresVolatileNonIsolationAndNeverExecutesCode(t *testing.T) {
	t.Parallel()

	supervisor := newTestSupervisor(t)
	guarantees := supervisor.Guarantees()
	if guarantees.Durability != DurabilityVolatile || guarantees.Isolation != IsolationNone || guarantees.ExecutesCode {
		t.Fatalf("fake guarantees = %+v", guarantees)
	}
}

func TestLaunchIdempotencyReplaysExactReceiptAndRejectsDigestConflict(t *testing.T) {
	t.Parallel()

	supervisor := newTestSupervisor(t)
	command := testLaunchCommand(t, "operation-launch-1", "execution-1", 0)
	first, err := supervisor.Launch(context.Background(), command)
	if err != nil {
		t.Fatalf("first launch: %v", err)
	}
	second, err := supervisor.Launch(context.Background(), command)
	if err != nil {
		t.Fatalf("idempotent launch: %v", err)
	}
	if first != second || supervisor.LaunchCount() != 1 {
		t.Fatalf("idempotent launch drift: first=%+v second=%+v count=%d", first, second, supervisor.LaunchCount())
	}

	conflict := command
	conflict.AttemptID = "attempt-other"
	conflict.RequestDigest = ""
	conflict, err = isolation.SealLaunchCommand(conflict)
	if err != nil {
		t.Fatalf("seal conflicting launch: %v", err)
	}
	if _, err := supervisor.Launch(context.Background(), conflict); !errors.Is(err, isolation.ErrIdempotencyConflict) {
		t.Fatalf("conflict error = %v, want %v", err, isolation.ErrIdempotencyConflict)
	}
	if supervisor.LaunchCount() != 1 {
		t.Fatalf("conflict launched a second instance: %d", supervisor.LaunchCount())
	}
}

func TestConcurrentGenerationCASAllowsOneLaunch(t *testing.T) {
	t.Parallel()

	supervisor := newTestSupervisor(t)
	const competitors = 24
	var waitGroup sync.WaitGroup
	results := make(chan error, competitors)
	for index := 0; index < competitors; index++ {
		index := index
		waitGroup.Add(1)
		go func() {
			defer waitGroup.Done()
			command := testLaunchCommand(
				t,
				"operation-concurrent-"+testIndex(index),
				"execution-race",
				0,
			)
			_, err := supervisor.Launch(context.Background(), command)
			results <- err
		}()
	}
	waitGroup.Wait()
	close(results)

	successes := 0
	stale := 0
	for err := range results {
		switch {
		case err == nil:
			successes++
		case errors.Is(err, isolation.ErrStaleGeneration):
			stale++
		default:
			t.Fatalf("unexpected concurrent launch error: %v", err)
		}
	}
	if successes != 1 || stale != competitors-1 || supervisor.LaunchCount() != 1 {
		t.Fatalf("success=%d stale=%d launches=%d", successes, stale, supervisor.LaunchCount())
	}
}

func TestForcedTerminationFencesOldGenerationAndKeepsEffectUnknown(t *testing.T) {
	t.Parallel()

	supervisor := newTestSupervisor(t)
	launch := testLaunchCommand(t, "operation-launch-1", "execution-1", 0)
	launchReceipt, err := supervisor.Launch(context.Background(), launch)
	if err != nil {
		t.Fatalf("launch: %v", err)
	}
	if err := supervisor.SetTerminationMode(launch.ExecutionID, TerminateForced); err != nil {
		t.Fatalf("set termination mode: %v", err)
	}
	terminate := testTerminateCommand(t, "operation-terminate-1", launchReceipt.Instance, isolation.EffectExternal)
	observation, err := supervisor.TerminateAndReap(context.Background(), terminate)
	if err != nil {
		t.Fatalf("terminate: %v", err)
	}
	if err := isolation.ValidateTerminationObservation(terminate, observation); !errors.Is(err, isolation.ErrReconcileRequired) {
		t.Fatalf("termination validation = %v, want %v", err, isolation.ErrReconcileRequired)
	}
	if observation.Path != isolation.TerminationForced || observation.Kill == nil ||
		observation.Exit == nil || observation.Reap == nil || observation.Release == nil ||
		observation.EffectOutcome != isolation.EffectDispatchedUnknown {
		t.Fatalf("forced observation = %+v", observation)
	}
	if _, err := supervisor.GetExecution(context.Background(), terminate.Target); !errors.Is(err, isolation.ErrStaleGeneration) {
		t.Fatalf("old fence get error = %v, want %v", err, isolation.ErrStaleGeneration)
	}
	current, err := supervisor.GetExecution(context.Background(), observation.AdvancedFence)
	if err != nil {
		t.Fatalf("get advanced fence: %v", err)
	}
	if current.State != isolation.ProcessReleased {
		t.Fatalf("current state = %s, want %s", current.State, isolation.ProcessReleased)
	}

	staleTerminate := testTerminateCommand(t, "operation-terminate-stale", launchReceipt.Instance, isolation.EffectExternal)
	if _, err := supervisor.TerminateAndReap(context.Background(), staleTerminate); !errors.Is(err, isolation.ErrStaleGeneration) {
		t.Fatalf("stale terminate error = %v, want %v", err, isolation.ErrStaleGeneration)
	}
}

func TestWaitUnknownProducesStableOperatorVisibleQuarantine(t *testing.T) {
	t.Parallel()

	supervisor := newTestSupervisor(t)
	launch := testLaunchCommand(t, "operation-launch-1", "execution-1", 0)
	launchReceipt, err := supervisor.Launch(context.Background(), launch)
	if err != nil {
		t.Fatalf("launch: %v", err)
	}
	if err := supervisor.SetTerminationMode(launch.ExecutionID, TerminateWaitUnknown); err != nil {
		t.Fatalf("set termination mode: %v", err)
	}
	terminate := testTerminateCommand(t, "operation-terminate-1", launchReceipt.Instance, isolation.EffectExternal)
	first, err := supervisor.TerminateAndReap(context.Background(), terminate)
	if err != nil {
		t.Fatalf("terminate: %v", err)
	}
	validationErr := isolation.ValidateTerminationObservation(terminate, first)
	if !errors.Is(validationErr, isolation.ErrQuarantineRequired) ||
		!errors.Is(validationErr, isolation.ErrReconcileRequired) {
		t.Fatalf("quarantine validation = %v", validationErr)
	}
	if first.Quarantine == nil || !first.Quarantine.OperatorVisible || first.Release != nil {
		t.Fatalf("quarantine observation = %+v", first)
	}

	first.Quarantine.Reason = "caller-mutated"
	second, err := supervisor.TerminateAndReap(context.Background(), terminate)
	if err != nil {
		t.Fatalf("idempotent terminate: %v", err)
	}
	if second.Quarantine == nil || second.Quarantine.Reason != "wait_unverified" {
		t.Fatalf("stored quarantine mutated through caller: %+v", second.Quarantine)
	}
	operation, err := supervisor.GetOperation(context.Background(), terminate.OperationID)
	if err != nil {
		t.Fatalf("get operation: %v", err)
	}
	if operation.Termination == nil || operation.Termination.Digest != second.Digest {
		t.Fatalf("operation observation = %+v", operation)
	}
}

func TestFakeIsDeterministicAcrossFreshInstances(t *testing.T) {
	t.Parallel()

	firstSupervisor := newTestSupervisor(t)
	secondSupervisor := newTestSupervisor(t)
	command := testLaunchCommand(t, "operation-launch-1", "execution-1", 0)
	first, firstErr := firstSupervisor.Launch(context.Background(), command)
	second, secondErr := secondSupervisor.Launch(context.Background(), command)
	if firstErr != nil || secondErr != nil {
		t.Fatalf("launch errors: first=%v second=%v", firstErr, secondErr)
	}
	if first != second {
		t.Fatalf("deterministic receipts differ: first=%+v second=%+v", first, second)
	}
}

func newTestSupervisor(t *testing.T) *Supervisor {
	t.Helper()
	supervisor, err := New(Config{Now: fakeContractTime, TenantID: "tenant-acme", TaskID: "task-1"})
	if err != nil {
		t.Fatalf("new fake supervisor: %v", err)
	}
	return supervisor
}

func testLaunchCommand(
	t *testing.T,
	operationID string,
	executionID string,
	expectedPreviousGeneration uint64,
) isolation.LaunchCommand {
	t.Helper()
	command, err := isolation.SealLaunchCommand(isolation.LaunchCommand{
		SchemaVersion:              1,
		OperationID:                operationID,
		ExecutionID:                executionID,
		ExpectedPreviousGeneration: expectedPreviousGeneration,
		PackageVersionRef: isolation.VersionedRef{
			ID: "package-version-1", Revision: 7, Digest: fakeDigest("package"),
		},
		IsolationProfileRef: isolation.VersionedRef{
			ID: "isolation-profile-1", Revision: 3, Digest: fakeDigest("profile"),
		},
		RuntimeGrantRef: isolation.VersionedRef{
			ID: "runtime-grant-1", Revision: 11, Digest: fakeDigest("grant"),
		},
		AttemptID:           "attempt-1",
		InputManifestDigest: fakeDigest("input"),
		Deadline:            fakeContractTime.Add(time.Hour),
	})
	if err != nil {
		t.Fatalf("seal launch command: %v", err)
	}
	return command
}

func testTerminateCommand(
	t *testing.T,
	operationID string,
	instance isolation.ProcessInstance,
	effectClass isolation.EffectClass,
) isolation.TerminateCommand {
	t.Helper()
	command, err := isolation.SealTerminateCommand(isolation.TerminateCommand{
		SchemaVersion:      1,
		OperationID:        operationID,
		Target:             instance.Fence(),
		RuntimeGrantDigest: instance.RuntimeGrantDigest,
		EffectClass:        effectClass,
		Reason:             isolation.TerminationCancelled,
		RequestedAt:        fakeContractTime,
	})
	if err != nil {
		t.Fatalf("seal terminate command: %v", err)
	}
	return command
}

func testIndex(index int) string {
	const digits = "0123456789"
	if index < 10 {
		return string(digits[index])
	}
	return string([]byte{digits[index/10], digits[index%10]})
}
