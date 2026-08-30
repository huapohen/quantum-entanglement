package localdemo

import (
	"context"
	"testing"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/agentstore"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/modelruntime"
)

func TestAgentStoreBackendSeamSeedsAndCommitsLifecycle(t *testing.T) {
	t.Parallel()
	backend := &recordingAgentStoreBackend{}
	service, err := NewWithRuntimeAndAgentStore(modelruntime.NewDeterministic(), backend)
	if err != nil {
		t.Fatal(err)
	}
	if len(backend.synced) != 2 || backend.synced[0].Passport.IsZero() {
		t.Fatalf("seeded catalog = %#v", backend.synced)
	}
	if _, err := service.InstallAgent(context.Background(), LocalBearerToken, "agd_local_planner", AgentStoreInstallInput{
		IdempotencyKey: "test/backend/install",
	}); err != nil {
		t.Fatal(err)
	}
	if backend.installCalls != 1 || backend.lastInstallTarget.IsZero() || len(backend.lastRetired) != 1 {
		t.Fatalf("install backend calls=%d target=%#v retired=%#v", backend.installCalls, backend.lastInstallTarget, backend.lastRetired)
	}
	if _, err := service.OffboardAgent(context.Background(), LocalBearerToken, "agd_local_planner", AgentStoreOffboardInput{
		IdempotencyKey: "test/backend/offboard", DataDisposition: string(agentstore.DataDispositionArchive),
	}); err != nil {
		t.Fatal(err)
	}
	if backend.offboardCalls != 1 || backend.lastOffboardCurrent.IsZero() || backend.lastOffboardNext.Status() != agentstore.InstallationOffboarded {
		t.Fatalf("offboard backend calls=%d current=%#v next=%#v", backend.offboardCalls, backend.lastOffboardCurrent, backend.lastOffboardNext)
	}
}

type recordingAgentStoreBackend struct {
	synced              []AgentStoreRecord
	installCalls        int
	lastInstallTarget   agentstore.InstallationSnapshot
	lastRetired         []agentstore.InstallationSnapshot
	offboardCalls       int
	lastOffboardCurrent agentstore.InstallationSnapshot
	lastOffboardNext    agentstore.InstallationSnapshot
}

func (backend *recordingAgentStoreBackend) SyncCatalog(_ context.Context, _ im.TenantID, records []AgentStoreRecord) error {
	backend.synced = append([]AgentStoreRecord(nil), records...)
	return nil
}

func (backend *recordingAgentStoreBackend) CommitInstall(_ context.Context, _ im.TenantID, _ string, _ agentstore.SHA256Digest, target agentstore.InstallationSnapshot, retired []agentstore.InstallationSnapshot) error {
	backend.installCalls++
	backend.lastInstallTarget = target
	backend.lastRetired = append([]agentstore.InstallationSnapshot(nil), retired...)
	return nil
}

func (backend *recordingAgentStoreBackend) CommitOffboard(_ context.Context, _ im.TenantID, _ string, _ agentstore.SHA256Digest, current agentstore.InstallationSnapshot, next agentstore.InstallationSnapshot) error {
	backend.offboardCalls++
	backend.lastOffboardCurrent, backend.lastOffboardNext = current, next
	return nil
}
