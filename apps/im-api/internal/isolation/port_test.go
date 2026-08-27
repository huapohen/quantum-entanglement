package isolation

import (
	"errors"
	"reflect"
	"testing"
	"time"
)

func TestLaunchSupervisorContractUsesRefsAndSupervisorOwnedGeneration(t *testing.T) {
	t.Parallel()

	command := validLaunchCommand(t)
	if err := ValidateLaunchCommandAt(command, executionContractTime); err != nil {
		t.Fatalf("validate launch command: %v", err)
	}
	receipt := validLaunchReceipt(t, command)
	if err := ValidateLaunchReceipt(command, receipt); err != nil {
		t.Fatalf("validate launch receipt: %v", err)
	}

	wrongGeneration := receipt
	wrongGeneration.Instance.Generation++
	wrongGeneration, err := SealLaunchReceipt(wrongGeneration)
	if err != nil {
		t.Fatalf("seal wrong-generation receipt: %v", err)
	}
	if err := ValidateLaunchReceipt(command, wrongGeneration); !errors.Is(err, ErrReceiptInvalid) {
		t.Fatalf("wrong generation error = %v, want %v", err, ErrReceiptInvalid)
	}

	wrongProfile := receipt
	wrongProfile.IsolationProfileRef.Digest = digestOf('9')
	wrongProfile, err = SealLaunchReceipt(wrongProfile)
	if err != nil {
		t.Fatalf("seal wrong-profile receipt: %v", err)
	}
	if err := ValidateLaunchReceipt(command, wrongProfile); !errors.Is(err, ErrReceiptInvalid) {
		t.Fatalf("wrong profile error = %v, want %v", err, ErrReceiptInvalid)
	}
}

func TestForcedTerminationRequiresKillWaitReapReleaseButKeepsExternalEffectUnknown(t *testing.T) {
	t.Parallel()

	command := validTerminateCommand(t, EffectExternal)
	observation := validTerminationObservation(t, command, TerminationForced)
	if err := ValidateTerminationObservation(command, observation); !errors.Is(err, ErrReconcileRequired) {
		t.Fatalf("forced termination error = %v, want %v", err, ErrReconcileRequired)
	}
	if observation.EffectOutcome != EffectDispatchedUnknown || !observation.ReconcileRequired {
		t.Fatal("released process incorrectly claimed external effect finality")
	}

	missingWait := observation
	missingWait.Exit = nil
	if _, err := SealTerminationObservation(missingWait); !errors.Is(err, ErrReceiptInvalid) {
		t.Fatalf("missing wait error = %v, want %v", err, ErrReceiptInvalid)
	}
	residualChild := observation
	residualChild.Reap = cloneReapReceipt(observation.Reap)
	residualChild.Reap.DescendantsEmpty = false
	if _, err := SealTerminationObservation(residualChild); !errors.Is(err, ErrReceiptInvalid) {
		t.Fatalf("residual child error = %v, want %v", err, ErrReceiptInvalid)
	}
	incompleteRelease := observation
	incompleteRelease.Release = cloneReleaseReceipt(observation.Release)
	incompleteRelease.Release.NetworkReleased = false
	if _, err := SealTerminationObservation(incompleteRelease); !errors.Is(err, ErrReceiptInvalid) {
		t.Fatalf("incomplete release error = %v, want %v", err, ErrReceiptInvalid)
	}
}

func TestGracefulTerminationSkipsKillButStillWaitsReapsAndReleases(t *testing.T) {
	t.Parallel()

	command := validTerminateCommand(t, EffectPure)
	observation := validTerminationObservation(t, command, TerminationGraceful)
	if observation.Kill != nil {
		t.Fatal("graceful path unexpectedly contains kill receipt")
	}
	if err := ValidateTerminationObservation(command, observation); err != nil {
		t.Fatalf("validate graceful pure termination: %v", err)
	}
}

func TestKillWithoutExitProofIsOperatorVisibleQuarantineAndEffectUnknown(t *testing.T) {
	t.Parallel()

	command := validTerminateCommand(t, EffectExternal)
	advanced := advancedFence(command.Target)
	observation, err := SealTerminationObservation(TerminationObservation{
		SchemaVersion:     1,
		OperationID:       command.OperationID,
		RequestDigest:     command.RequestDigest,
		Target:            command.Target,
		AdvancedFence:     advanced,
		Path:              TerminationUnverified,
		ProcessOutcome:    ProcessOutcomeQuarantined,
		EffectOutcome:     EffectDispatchedUnknown,
		ReconcileRequired: true,
		Cancel: &CancelReceipt{
			Target: advanced, RequestedAt: executionContractTime, EvidenceDigest: digestOf('1'),
		},
		Kill: &KillReceipt{
			Target: advanced, Method: "kill_tree", IssuedAt: executionContractTime.Add(time.Second),
			EvidenceDigest: digestOf('2'),
		},
		Quarantine: &QuarantineReceipt{
			Target: advanced, Reason: "wait_unverified", RecordedAt: executionContractTime.Add(2 * time.Second),
			OperatorVisible: true, EvidenceDigest: digestOf('3'),
		},
	})
	if err != nil {
		t.Fatalf("seal quarantine observation: %v", err)
	}
	validationErr := ValidateTerminationObservation(command, observation)
	if !errors.Is(validationErr, ErrQuarantineRequired) || !errors.Is(validationErr, ErrReconcileRequired) {
		t.Fatalf("quarantine error = %v, want quarantine and reconcile", validationErr)
	}
}

func TestTerminationReceiptCannotTurnKillIntoNegativeEffectProof(t *testing.T) {
	t.Parallel()

	command := validTerminateCommand(t, EffectExternal)
	observation := validTerminationObservation(t, command, TerminationForced)
	observation.EffectOutcome = EffectNotApplicable
	observation.ReconcileRequired = false
	observation, err := SealTerminationObservation(observation)
	if err != nil {
		t.Fatalf("seal forged effect observation: %v", err)
	}
	if err := ValidateTerminationObservation(command, observation); !errors.Is(err, ErrReceiptInvalid) {
		t.Fatalf("forged effect error = %v, want %v", err, ErrReceiptInvalid)
	}
}

func TestSupervisorCommandsContainNoInProcessAuthority(t *testing.T) {
	t.Parallel()

	assertDataOnlyType(t, reflect.TypeOf(LaunchCommand{}), map[reflect.Type]bool{})
	assertDataOnlyType(t, reflect.TypeOf(TerminateCommand{}), map[reflect.Type]bool{})
	for _, field := range []string{"Argv", "Command", "Environment", "HostPath", "Mount", "Socket", "Secret"} {
		if _, exists := reflect.TypeOf(LaunchCommand{}).FieldByName(field); exists {
			t.Fatalf("launch command exposes forbidden field %s", field)
		}
	}
}

func validLaunchCommand(t *testing.T) LaunchCommand {
	t.Helper()
	command, err := SealLaunchCommand(LaunchCommand{
		SchemaVersion:              1,
		OperationID:                "operation-launch-1",
		ExecutionID:                "execution-1",
		ExpectedPreviousGeneration: 3,
		PackageVersionRef:          VersionedRef{ID: "package-version-1", Revision: 7, Digest: digestOf('a')},
		IsolationProfileRef:        VersionedRef{ID: "isolation-profile-1", Revision: 3, Digest: digestOf('b')},
		RuntimeGrantRef:            VersionedRef{ID: "runtime-grant-1", Revision: 11, Digest: digestOf('c')},
		AttemptID:                  "attempt-1",
		InputManifestDigest:        digestOf('d'),
		Deadline:                   executionContractTime.Add(time.Hour),
	})
	if err != nil {
		t.Fatalf("seal launch command: %v", err)
	}
	return command
}

func validLaunchReceipt(t *testing.T, command LaunchCommand) LaunchReceipt {
	t.Helper()
	instance := exampleProcessInstance()
	instance.ExecutionID = command.ExecutionID
	instance.AttemptID = command.AttemptID
	instance.Generation = command.ExpectedPreviousGeneration + 1
	instance.PackageArtifactDigest = command.PackageVersionRef.Digest
	instance.IsolationProfileDigest = command.IsolationProfileRef.Digest
	instance.RuntimeGrantDigest = command.RuntimeGrantRef.Digest
	receipt, err := SealLaunchReceipt(LaunchReceipt{
		SchemaVersion:             1,
		OperationID:               command.OperationID,
		RequestDigest:             command.RequestDigest,
		PackageVersionRef:         command.PackageVersionRef,
		IsolationProfileRef:       command.IsolationProfileRef,
		RuntimeGrantRef:           command.RuntimeGrantRef,
		Instance:                  instance,
		EnforcementEvidenceDigest: digestOf('e'),
		StartedAt:                 executionContractTime,
	})
	if err != nil {
		t.Fatalf("seal launch receipt: %v", err)
	}
	return receipt
}

func validTerminateCommand(t *testing.T, effectClass EffectClass) TerminateCommand {
	t.Helper()
	command, err := SealTerminateCommand(TerminateCommand{
		SchemaVersion:      1,
		OperationID:        "operation-terminate-1",
		Target:             exampleProcessInstance().Fence(),
		RuntimeGrantDigest: exampleProcessInstance().RuntimeGrantDigest,
		EffectClass:        effectClass,
		Reason:             TerminationCancelled,
		RequestedAt:        executionContractTime,
	})
	if err != nil {
		t.Fatalf("seal terminate command: %v", err)
	}
	return command
}

func validTerminationObservation(
	t *testing.T,
	command TerminateCommand,
	path TerminationPath,
) TerminationObservation {
	t.Helper()
	advanced := advancedFence(command.Target)
	effectOutcome := EffectNotApplicable
	reconcileRequired := false
	if command.EffectClass == EffectExternal {
		effectOutcome = EffectDispatchedUnknown
		reconcileRequired = true
	}
	observation := TerminationObservation{
		SchemaVersion:     1,
		OperationID:       command.OperationID,
		RequestDigest:     command.RequestDigest,
		Target:            command.Target,
		AdvancedFence:     advanced,
		Path:              path,
		ProcessOutcome:    ProcessOutcomeReleased,
		EffectOutcome:     effectOutcome,
		ReconcileRequired: reconcileRequired,
		Exit: &ExitReceipt{
			Target: advanced, ObservedAt: executionContractTime.Add(2 * time.Second),
			ExactInstanceExited: true, WaitStatusDigest: digestOf('3'),
		},
		Reap: &ReapReceipt{
			Target: advanced, ReapedAt: executionContractTime.Add(3 * time.Second),
			DescendantsEmpty: true, EvidenceDigest: digestOf('4'),
		},
		Release: &ReleaseReceipt{
			Target: advanced, ReleasedAt: executionContractTime.Add(4 * time.Second),
			RuntimeGrantRevoked: true, NetworkReleased: true, MountsReleased: true,
			WorkspaceReleased: true, EvidenceDigest: digestOf('5'),
		},
	}
	if path != TerminationAlreadyExit {
		observation.Cancel = &CancelReceipt{
			Target: advanced, RequestedAt: executionContractTime, EvidenceDigest: digestOf('1'),
		}
	}
	if path == TerminationForced {
		observation.Kill = &KillReceipt{
			Target: advanced, Method: "kill_tree", IssuedAt: executionContractTime.Add(time.Second),
			EvidenceDigest: digestOf('2'),
		}
	}
	sealed, err := SealTerminationObservation(observation)
	if err != nil {
		t.Fatalf("seal termination observation: %v", err)
	}
	return sealed
}

func advancedFence(previous ProcessFence) ProcessFence {
	advanced := previous
	advanced.FenceRevision++
	advanced.FenceDigest = digestOf('f')
	return advanced
}

func cloneReapReceipt(receipt *ReapReceipt) *ReapReceipt {
	if receipt == nil {
		return nil
	}
	cloned := *receipt
	return &cloned
}

func cloneReleaseReceipt(receipt *ReleaseReceipt) *ReleaseReceipt {
	if receipt == nil {
		return nil
	}
	cloned := *receipt
	return &cloned
}
