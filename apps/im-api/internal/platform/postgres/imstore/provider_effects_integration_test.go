package imstore

import (
	"context"
	"errors"
	"os"
	"testing"
	"time"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/agentstore"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
	store "github.com/huapohen/quantum-entanglement/apps/im-api/internal/imstore"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

func TestProviderEffectRepositoryAgainstPostgres(t *testing.T) {
	adminURL := os.Getenv(storeIntegrationURL)
	if adminURL == "" {
		t.Skip(storeIntegrationURL + " is not set")
	}
	unit, pool := newStoreIntegrationUnit(t, adminURL)
	tenantID := mustTenantID(t, "ten_alpha")
	installationID := seedProviderEffectInstallation(t, unit, tenantID)
	createdAt := time.Now().UTC()
	workspaceID := "wsp_alpha"
	intent := store.ProviderEffectIntent{
		TenantID: "ten_alpha", WorkspaceID: &workspaceID, InstallationID: installationID,
		EffectID: "eff_pg_worker_1", EffectKind: store.ProviderEffectUserProvision,
		Provider: "rongcloud", ProviderRealmID: "rlm_rong", ProviderSubjectID: "agt_pg_worker",
		OperationKey: "provider-effect/pg-worker-1", RequestRef: "request/provider-effect-1",
		RequestDigest: store.DigestBytes([]byte("provider-effect-request-1")), CreatedAt: createdAt,
	}

	transaction, repository := beginProviderEffectTransaction(t, pool, tenantID)
	inserted, replayed, err := repository.Enqueue(t.Context(), intent)
	if err != nil || replayed || inserted.State != store.ProviderEffectQueued {
		t.Fatalf("enqueue = (%#v, replayed=%v, err=%v)", inserted, replayed, err)
	}
	exact, replayed, err := repository.Enqueue(t.Context(), intent)
	if err != nil || !replayed || exact.Intent.EffectID != intent.EffectID {
		t.Fatalf("exact enqueue replay = (%#v, replayed=%v, err=%v)", exact, replayed, err)
	}
	if err := transaction.Commit(t.Context()); err != nil {
		t.Fatalf("commit enqueue: %v", err)
	}

	claimTransaction, claimRepository := beginProviderEffectTransaction(t, pool, tenantID)
	claims, err := claimRepository.ClaimDue(t.Context(), tenantID.String(), "worker-a", 5*time.Minute, 10)
	if err != nil || len(claims) != 1 || claims[0].Record.State != store.ProviderEffectSent ||
		claims[0].Record.AttemptCount != 1 || claims[0].LeaseToken == "" {
		t.Fatalf("claim = (%#v, %v)", claims, err)
	}
	claim := claims[0]
	if err := claimTransaction.Commit(t.Context()); err != nil {
		t.Fatalf("commit claim: %v", err)
	}

	wrongTransaction, wrongRepository := beginProviderEffectTransaction(t, pool, tenantID)
	receipt := im.ProviderEffectReceipt{
		OperationKey: intent.OperationKey, ExternalID: "agt_pg_worker",
		Status: im.ProviderEffectCommitted, ObservedAt: time.Now().UTC().Add(time.Minute),
	}
	if _, err := wrongRepository.RecordReceipt(
		t.Context(), intent.Key(), "wrong-lease", receipt,
	); !errors.Is(err, store.ErrProviderEffectLease) {
		t.Fatalf("wrong lease error = %v", err)
	}
	rollbackTransaction(wrongTransaction)

	receiptTransaction, receiptRepository := beginProviderEffectTransaction(t, pool, tenantID)
	committed, err := receiptRepository.RecordReceipt(t.Context(), intent.Key(), claim.LeaseToken, receipt)
	if err != nil || committed.State != store.ProviderEffectCommitted || committed.ProviderReceipt == nil ||
		committed.ProviderReceipt.ExternalID != receipt.ExternalID || committed.CommittedAt.IsZero() {
		t.Fatalf("record receipt = (%#v, %v)", committed, err)
	}
	if err := receiptTransaction.Commit(t.Context()); err != nil {
		t.Fatalf("commit receipt: %v", err)
	}

	unknownIntent := intent
	unknownIntent.EffectID = "eff_pg_worker_2"
	unknownIntent.OperationKey = "provider-effect/pg-worker-2"
	unknownIntent.RequestRef = "request/provider-effect-2"
	unknownIntent.RequestDigest = store.DigestBytes([]byte("provider-effect-request-2"))
	unknownIntent.CreatedAt = time.Now().UTC()
	transaction, repository = beginProviderEffectTransaction(t, pool, tenantID)
	if _, _, err := repository.Enqueue(t.Context(), unknownIntent); err != nil {
		t.Fatalf("enqueue unknown fixture: %v", err)
	}
	if err := transaction.Commit(t.Context()); err != nil {
		t.Fatalf("commit unknown fixture: %v", err)
	}

	claimTransaction, claimRepository = beginProviderEffectTransaction(t, pool, tenantID)
	claims, err = claimRepository.ClaimDue(t.Context(), tenantID.String(), "worker-b", 5*time.Minute, 1)
	if err != nil || len(claims) != 1 || claims[0].Record.Intent.EffectID != unknownIntent.EffectID {
		t.Fatalf("claim unknown fixture = (%#v, %v)", claims, err)
	}
	claim = claims[0]
	if err := claimTransaction.Commit(t.Context()); err != nil {
		t.Fatalf("commit unknown claim: %v", err)
	}

	unknownTransaction, unknownRepository := beginProviderEffectTransaction(t, pool, tenantID)
	unknown, err := unknownRepository.MarkUnknown(
		t.Context(), unknownIntent.Key(), claim.LeaseToken, "provider-timeout",
	)
	if err != nil || unknown.State != store.ProviderEffectUnknown || unknown.ProviderReceipt != nil {
		t.Fatalf("mark unknown = (%#v, %v)", unknown, err)
	}
	if err := unknownTransaction.Commit(t.Context()); err != nil {
		t.Fatalf("commit unknown: %v", err)
	}

	resolveTransaction, resolveRepository := beginProviderEffectTransaction(t, pool, tenantID)
	resolvedReceipt := im.ProviderEffectReceipt{
		OperationKey: unknownIntent.OperationKey, ExternalID: "agt_pg_worker_unknown",
		Status: im.ProviderEffectReplayed, ObservedAt: time.Now().UTC().Add(2 * time.Minute),
	}
	resolved, err := resolveRepository.ResolveUnknown(t.Context(), unknownIntent.Key(), resolvedReceipt)
	if err != nil || resolved.State != store.ProviderEffectReplayed || resolved.ProviderReceipt == nil ||
		resolved.ProviderReceipt.Status != im.ProviderEffectReplayed {
		t.Fatalf("resolve unknown = (%#v, %v)", resolved, err)
	}
	if err := resolveTransaction.Commit(t.Context()); err != nil {
		t.Fatalf("commit resolve: %v", err)
	}
}

func beginProviderEffectTransaction(
	t *testing.T,
	pool *pgxpool.Pool,
	tenantID im.TenantID,
) (pgx.Tx, *ProviderEffectRepository) {
	t.Helper()
	transaction, err := pool.BeginTx(t.Context(), pgx.TxOptions{IsoLevel: pgx.Serializable})
	if err != nil {
		t.Fatalf("begin provider effect transaction: %v", err)
	}
	if _, err := setStoreTenant(t.Context(), transaction, tenantID.String()); err != nil {
		rollbackTransaction(transaction)
		t.Fatalf("bind provider effect tenant: %v", err)
	}
	repository, err := NewProviderEffectRepository(transaction, tenantID)
	if err != nil {
		rollbackTransaction(transaction)
		t.Fatalf("create provider effect repository: %v", err)
	}
	return transaction, repository
}

func seedProviderEffectInstallation(t *testing.T, unit *UnitOfWork, tenantID im.TenantID) string {
	t.Helper()
	definitionID := mustAgentDefinitionID(t, "agd_provider_effect")
	publisherID := mustPublisherID(t, "pub_provider_effect")
	claimedBy := mustPrincipalID(t, "hpr_alice")
	definition := mustAgentDefinition(
		t, definitionID, tenantID, claimedBy, publisherID, 1, agentstore.DefinitionActive,
	)
	release := mustAgentRelease(t, definitionID, 1, agentstore.ReleasePublished)
	passport := mustAgentPassport(t, definition, release, 1)
	workspaceID := mustWorkspaceID(t, "wsp_alpha")
	actorID := mustActorID(t, "agt_repository")
	installation := mustAgentInstallation(
		t, tenantID, workspaceID, actorID, claimedBy, passport,
		time.Date(2026, 8, 29, 12, 0, 0, 0, time.UTC), agentstore.InstallationActive, 1,
	)
	command := mustCommand(t, "agent.provider-effect.seed", "provider-effect-seed", "provider-effect-seed-request")
	_, err := unit.ExecuteAgentStore(
		t.Context(), tenantID, command.Kind(), command.IdempotencyKey(), command.RequestDigest(),
		func(ctx context.Context, repositories store.TenantRepositories) (store.SHA256Digest, error) {
			if _, err := repositories.AgentStore().CompareAndSwapDefinition(ctx, 0, definition); err != nil {
				return store.SHA256Digest{}, err
			}
			if _, err := repositories.AgentStore().CompareAndSwapRelease(ctx, 0, release); err != nil {
				return store.SHA256Digest{}, err
			}
			if _, err := repositories.AgentStore().CompareAndSwapPassport(ctx, 0, passport); err != nil {
				return store.SHA256Digest{}, err
			}
			if _, err := repositories.AgentStore().CompareAndSwapInstallation(ctx, 0, installation); err != nil {
				return store.SHA256Digest{}, err
			}
			return store.DigestBytes([]byte("provider-effect-seed-result")), nil
		},
	)
	if err != nil {
		t.Fatalf("seed provider effect installation: %v", err)
	}
	return installation.ID().String()
}
