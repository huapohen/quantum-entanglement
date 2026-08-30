package localdemo

import (
	"context"
	"testing"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/agentstore"
)

func TestAgentStoreActionsExposeCommittedAndReplayedCommandStatus(t *testing.T) {
	t.Parallel()
	service, err := New()
	if err != nil {
		t.Fatal(err)
	}
	installInput := AgentStoreInstallInput{IdempotencyKey: "test/store/status/install"}
	firstInstall, err := service.InstallAgent(context.Background(), LocalBearerToken, "agd_local_planner", installInput)
	if err != nil {
		t.Fatal(err)
	}
	if firstInstall.Replayed || firstInstall.CommandStatus != agentStoreCommandCommitted || firstInstall.Agent.InstallationStatus != string(agentstore.InstallationActive) {
		t.Fatalf("first install status = %#v", firstInstall)
	}
	replayedInstall, err := service.InstallAgent(context.Background(), LocalBearerToken, "agd_local_planner", installInput)
	if err != nil {
		t.Fatal(err)
	}
	if !replayedInstall.Replayed || replayedInstall.CommandStatus != agentStoreCommandReplayed || replayedInstall.Agent.InstallationID != firstInstall.Agent.InstallationID {
		t.Fatalf("replayed install status = %#v", replayedInstall)
	}

	offboardInput := AgentStoreOffboardInput{
		IdempotencyKey: "test/store/status/offboard", DataDisposition: string(agentstore.DataDispositionArchive),
	}
	firstOffboard, err := service.OffboardAgent(context.Background(), LocalBearerToken, "agd_local_planner", offboardInput)
	if err != nil {
		t.Fatal(err)
	}
	if firstOffboard.Replayed || firstOffboard.CommandStatus != agentStoreCommandCommitted || firstOffboard.Agent.InstallationStatus != string(agentstore.InstallationOffboarded) {
		t.Fatalf("first offboard status = %#v", firstOffboard)
	}
	replayedOffboard, err := service.OffboardAgent(context.Background(), LocalBearerToken, "agd_local_planner", offboardInput)
	if err != nil {
		t.Fatal(err)
	}
	if !replayedOffboard.Replayed || replayedOffboard.CommandStatus != agentStoreCommandReplayed || replayedOffboard.DataDisposition != offboardInput.DataDisposition {
		t.Fatalf("replayed offboard status = %#v", replayedOffboard)
	}
}
