package imstore

import (
	"context"
	"errors"
	"fmt"
	"os"
	"reflect"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/agentstore"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/auth"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
	store "github.com/huapohen/quantum-entanglement/apps/im-api/internal/imstore"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/platform/postgres/migrations"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

const storeIntegrationURL = "WANWORK_TEST_POSTGRES_ADMIN_URL"

var storeDatabaseSequence atomic.Uint64

func TestUnitOfWorkAgainstPostgres(t *testing.T) {
	adminURL := os.Getenv(storeIntegrationURL)
	if adminURL == "" {
		t.Skip(storeIntegrationURL + " is not set")
	}

	t.Run("exact replay and authority round trip", func(t *testing.T) {
		unit, _ := newStoreIntegrationUnit(t, adminURL)
		tenantID := mustTenantID(t, "ten_alpha")
		conversation := mustConversationSnapshot(t, tenantID, "cnv_repository", 1, im.ConversationActive)
		actorReference := mustActorRef(t, tenantID, "usr_alice")
		externalReference := mustProviderConversationRef(t, "cnv_repository")
		providerBinding := mustProviderConversationBinding(
			t,
			externalReference,
			conversation.Ref(),
			1,
		)
		membership := mustMembership(t, conversation.Ref(), actorReference, 1)
		permissions := []im.ConversationPermission{
			im.ConversationPermissionRead,
			im.ConversationPermissionSendMessage,
			im.ConversationPermissionManageMembers,
		}
		access := mustAccess(t, conversation.Ref(), actorReference, permissions, 1)
		command := mustCommand(t, "conversation.authority.create", "create-repository", "request-a")
		resultDigest := store.DigestBytes([]byte("result-a"))
		var calls atomic.Int64
		operation := func(
			ctx context.Context,
			repositories store.TenantRepositories,
		) (store.SHA256Digest, error) {
			calls.Add(1)
			if _, err := repositories.Conversations().CompareAndSwapConversation(
				ctx,
				0,
				conversation,
			); err != nil {
				return store.SHA256Digest{}, err
			}
			if _, err := repositories.Authority().CompareAndSwapProviderBinding(
				ctx,
				0,
				providerBinding,
			); err != nil {
				return store.SHA256Digest{}, err
			}
			if _, err := repositories.Authority().CompareAndSwapMembership(
				ctx,
				0,
				membership,
			); err != nil {
				return store.SHA256Digest{}, err
			}
			if _, err := repositories.Authority().CompareAndSwapAccess(
				ctx,
				0,
				access,
			); err != nil {
				return store.SHA256Digest{}, err
			}
			return resultDigest, nil
		}

		fresh, err := unit.Execute(t.Context(), tenantID, command, operation)
		if err != nil || fresh.Replayed() || fresh.ResolvedAfterUnknown() ||
			fresh.ResultDigest() != resultDigest || fresh.CommittedAt().IsZero() {
			t.Fatalf("fresh Execute = (%#v, %v)", fresh, err)
		}
		replay, err := unit.Execute(t.Context(), tenantID, command, operation)
		if err != nil || !replay.Replayed() || replay.ResolvedAfterUnknown() ||
			replay.ResultDigest() != resultDigest || replay.CommittedAt() != fresh.CommittedAt() {
			t.Fatalf("replay Execute = (%#v, %v)", replay, err)
		}
		if calls.Load() != 1 {
			t.Fatalf("operation calls = %d, want 1", calls.Load())
		}

		conflict := mustCommand(t, command.Kind(), command.IdempotencyKey(), "request-drift")
		if _, err := unit.Execute(t.Context(), tenantID, conflict, operation); !errors.Is(
			err,
			store.ErrIdempotencyConflict,
		) {
			t.Fatalf("digest drift Execute error = %v", err)
		}
		resolved, err := unit.Resolve(t.Context(), tenantID, command)
		if err != nil || !resolved.Replayed() || resolved.ResultDigest() != resultDigest {
			t.Fatalf("Resolve = (%#v, %v)", resolved, err)
		}

		err = unit.Read(t.Context(), tenantID, func(
			ctx context.Context,
			repositories store.TenantRepositories,
		) error {
			gotConversation, err := repositories.Conversations().CurrentConversation(
				ctx,
				conversation.Ref(),
			)
			if err != nil || gotConversation.Ref() != conversation.Ref() ||
				gotConversation.Revision() != 1 || gotConversation.Status() != im.ConversationActive {
				return fmt.Errorf("conversation round trip: %#v: %w", gotConversation, err)
			}
			gotBinding, err := repositories.Authority().CurrentProviderBinding(ctx, externalReference)
			if err != nil || gotBinding.ConversationRef() != conversation.Ref() ||
				gotBinding.Revision() != 1 {
				return fmt.Errorf("provider binding round trip: %#v: %w", gotBinding, err)
			}
			gotMembership, err := repositories.Authority().CurrentMembership(
				ctx,
				conversation.Ref(),
				actorReference,
			)
			if err != nil || gotMembership.Revision() != 1 ||
				gotMembership.Status() != im.ConversationMembershipActive {
				return fmt.Errorf("membership round trip: %#v: %w", gotMembership, err)
			}
			gotAccess, err := repositories.Authority().CurrentAccess(
				ctx,
				conversation.Ref(),
				actorReference,
			)
			if err != nil || gotAccess.Revision() != 1 ||
				!reflect.DeepEqual(gotAccess.Permissions(), permissions) {
				return fmt.Errorf("access round trip: %#v: %w", gotAccess, err)
			}
			return nil
		})
		if err != nil {
			t.Fatalf("Read authority: %v", err)
		}
	})

	t.Run("agent store catalog passport and installation CAS use function-only writes", func(t *testing.T) {
		unit, pool := newStoreIntegrationUnit(t, adminURL)
		tenantID := mustTenantID(t, "ten_alpha")
		definitionID := mustAgentDefinitionID(t, "agd_repository")
		publisherID := mustPublisherID(t, "pub_repository")
		claimedBy := mustPrincipalID(t, "hpr_alice")
		definition := mustAgentDefinition(t, definitionID, tenantID, claimedBy, publisherID, 1, agentstore.DefinitionActive)
		release := mustAgentRelease(t, definitionID, 1, agentstore.ReleasePublished)
		passport := mustAgentPassport(t, definition, release, 1)
		workspaceID := mustWorkspaceID(t, "wsp_alpha")
		actorID := mustActorID(t, "agt_repository")
		createdAt := time.Date(2026, 8, 29, 12, 0, 0, 0, time.UTC)
		installation := mustAgentInstallation(t, tenantID, workspaceID, actorID, claimedBy, passport, createdAt, agentstore.InstallationActive, 1)
		createCommand := mustCommand(t, "agent.store.create", "agent-store-create", "agent-store-create-request")
		createReceipt, err := unit.ExecuteAgentStore(t.Context(), tenantID, createCommand.Kind(), createCommand.IdempotencyKey(), createCommand.RequestDigest(), func(ctx context.Context, repositories store.TenantRepositories) (store.SHA256Digest, error) {
			if _, err := repositories.AgentStore().CompareAndSwapDefinition(ctx, 0, definition); err != nil {
				return store.SHA256Digest{}, fmt.Errorf("definition create: %w", err)
			}
			if _, err := repositories.AgentStore().CompareAndSwapRelease(ctx, 0, release); err != nil {
				return store.SHA256Digest{}, fmt.Errorf("release create: %w", err)
			}
			if _, err := repositories.AgentStore().CompareAndSwapPassport(ctx, 0, passport); err != nil {
				return store.SHA256Digest{}, fmt.Errorf("passport create: %w", err)
			}
			if _, err := repositories.AgentStore().CompareAndSwapInstallation(ctx, 0, installation); err != nil {
				return store.SHA256Digest{}, fmt.Errorf("installation create: %w", err)
			}
			return store.DigestBytes([]byte("agent-store-create-result")), nil
		})
		if err != nil {
			t.Fatalf("Agent Store create: %v", err)
		}
		if createReceipt.Replayed() || createReceipt.ResolvedAfterUnknown() {
			t.Fatalf("initial Agent Store receipt = %#v, want fresh committed receipt", createReceipt)
		}
		replayCalls := 0
		replayedReceipt, err := unit.ExecuteAgentStore(
			t.Context(), tenantID, createCommand.Kind(), createCommand.IdempotencyKey(), createCommand.RequestDigest(),
			func(context.Context, store.TenantRepositories) (store.SHA256Digest, error) {
				replayCalls++
				return store.SHA256Digest{}, errors.New("replayed Agent Store command was executed")
			},
		)
		if err != nil || !replayedReceipt.Replayed() || replayCalls != 0 || replayedReceipt.ResultDigest() != store.DigestBytes([]byte("agent-store-create-result")) {
			t.Fatalf("Agent Store durable replay receipt = %#v, err=%v, calls=%d", replayedReceipt, err, replayCalls)
		}
		if err := unit.Read(t.Context(), tenantID, func(ctx context.Context, repositories store.TenantRepositories) error {
			gotDefinition, err := repositories.AgentStore().CurrentDefinition(ctx, definitionID)
			if err != nil || gotDefinition.Revision() != 1 {
				return fmt.Errorf("definition read: %#v: %w", gotDefinition, err)
			}
			gotRelease, err := repositories.AgentStore().CurrentRelease(ctx, release.ID())
			if err != nil || gotRelease.Revision() != 1 {
				return fmt.Errorf("release read: %#v: %w", gotRelease, err)
			}
			gotPassport, err := repositories.AgentStore().CurrentPassport(ctx, release.ID())
			if err != nil || gotPassport.Revision() != 1 {
				return fmt.Errorf("passport read: %#v: %w", gotPassport, err)
			}
			gotInstallation, err := repositories.AgentStore().CurrentInstallation(ctx, installation.ID())
			if err != nil || gotInstallation.Revision() != 1 || gotInstallation.Status() != agentstore.InstallationActive {
				return fmt.Errorf("installation read: %#v: %w", gotInstallation, err)
			}
			return nil
		}); err != nil {
			t.Fatalf("Agent Store read: %v", err)
		}
		updatedInstallation := mustAgentInstallation(t, tenantID, workspaceID, actorID, claimedBy, passport, createdAt, agentstore.InstallationSuspended, 2)
		updateCommand := mustCommand(t, "agent.store.update", "agent-store-update", "agent-store-update-request")
		if _, err := unit.Execute(t.Context(), tenantID, updateCommand, func(ctx context.Context, repositories store.TenantRepositories) (store.SHA256Digest, error) {
			if _, err := repositories.AgentStore().CompareAndSwapInstallation(ctx, 1, updatedInstallation); err != nil {
				return store.SHA256Digest{}, fmt.Errorf("installation update: %w", err)
			}
			return store.DigestBytes([]byte("agent-store-update-result")), nil
		}); err != nil {
			t.Fatalf("Agent Store CAS: %v", err)
		}
		staleCommand := mustCommand(t, "agent.store.update", "agent-store-stale", "agent-store-stale-request")
		if _, err := unit.Execute(t.Context(), tenantID, staleCommand, func(ctx context.Context, repositories store.TenantRepositories) (store.SHA256Digest, error) {
			_, err := repositories.AgentStore().CompareAndSwapInstallation(ctx, 1, updatedInstallation)
			return store.SHA256Digest{}, err
		}); !errors.Is(err, agentstore.ErrInstallationConflict) {
			t.Fatalf("Agent Store stale CAS error = %v", err)
		}

		t.Run("database rejects non-canonical capability payloads", func(t *testing.T) {
			validPayload, err := agentstore.EncodeRelease(release)
			if err != nil {
				t.Fatalf("encode release payload: %v", err)
			}
			invalidPayload := strings.Replace(string(validPayload), `"agr_repository"`, `"agr_invalid_payload"`, 1)
			invalidPayload = strings.Replace(invalidPayload, `"conversation.read"`, `"invalid capability!"`, 1)
			transaction, err := pool.BeginTx(t.Context(), pgx.TxOptions{})
			if err != nil {
				t.Fatalf("begin invalid release write: %v", err)
			}
			defer rollbackTransaction(transaction)
			if _, err := setStoreTenant(t.Context(), transaction, tenantID.String()); err != nil {
				t.Fatalf("bind invalid release tenant: %v", err)
			}
			var changed bool
			err = transaction.QueryRow(t.Context(), `
SELECT wanwork_im.write_agent_release_revision($1, $2, $3, $4, $5)`,
				tenantID.String(), "agr_invalid_payload", int64(0), int64(1), invalidPayload,
			).Scan(&changed)
			var postgresError *pgconn.PgError
			if !errors.As(err, &postgresError) || postgresError.Code != "23514" || changed {
				t.Fatalf("invalid capability write changed=%v error=%v, want SQLSTATE 23514", changed, err)
			}

			readTransaction, err := pool.BeginTx(t.Context(), pgx.TxOptions{})
			if err != nil {
				t.Fatalf("begin invalid release readback: %v", err)
			}
			defer rollbackTransaction(readTransaction)
			if _, err := setStoreTenant(t.Context(), readTransaction, tenantID.String()); err != nil {
				t.Fatalf("bind invalid release readback tenant: %v", err)
			}
			var rows int
			if err := readTransaction.QueryRow(t.Context(), `
SELECT count(*) FROM wanwork_im.agent_releases WHERE tenant_id = $1 AND release_id = $2`,
				tenantID.String(), "agr_invalid_payload").Scan(&rows); err != nil || rows != 0 {
				t.Fatalf("invalid release rows=%d error=%v, want no durable row", rows, err)
			}
		})
	})

	t.Run("trusted context resolves identity authority in one tenant read snapshot", func(t *testing.T) {
		unit, _ := newStoreIntegrationUnit(t, adminURL)
		tenantID := mustTenantID(t, "ten_alpha")
		realmID, err := im.ParseProviderRealmID("rlm_clerk")
		if err != nil {
			t.Fatalf("parse Clerk realm: %v", err)
		}
		externalReference, err := im.NewExternalIdentityRef(
			im.IdentityProviderClerk, realmID, "user_alice",
		)
		if err != nil {
			t.Fatalf("create Clerk reference: %v", err)
		}
		profile, err := auth.NewProviderProfile(
			im.IdentityProviderClerk,
			realmID,
			"clerk.example",
			"wanwork-web",
			[]auth.Capability{auth.CapabilityVerify},
			1024,
		)
		if err != nil {
			t.Fatalf("create provider profile: %v", err)
		}
		now := time.Date(2026, 8, 29, 12, 0, 0, 0, time.UTC)
		identity := auth.VerifiedIdentity{
			ExternalRef: externalReference,
			SessionID:   "sess_store_integration",
			IssuedAt:    now.Add(-time.Minute),
			ExpiresAt:   now.Add(time.Hour),
		}
		principalID, err := im.ParseHumanPrincipalID("hpr_alice")
		if err != nil {
			t.Fatalf("parse principal: %v", err)
		}
		actorReference := mustActorRef(t, tenantID, "usr_alice")
		err = unit.Read(t.Context(), tenantID, func(
			ctx context.Context,
			repositories store.TenantRepositories,
		) error {
			resolved, err := auth.ResolveTrustedRequestContext(
				ctx, profile, identity, tenantID, repositories.Identity(), now,
			)
			if err != nil {
				return fmt.Errorf("resolve trusted context: %w", err)
			}
			if resolved.PrincipalID() != principalID || resolved.ActorRef() != actorReference ||
				resolved.Membership().Revision() != 1 || resolved.Actor().Revision() != 1 {
				return fmt.Errorf("unexpected trusted context: %#v", resolved)
			}
			if _, err := repositories.Identity().CurrentTenantMembership(
				ctx, mustTenantID(t, "ten_beta"), principalID,
			); !errors.Is(err, store.ErrInvalidRequest) {
				return fmt.Errorf("cross-tenant identity lookup error = %v", err)
			}
			return nil
		})
		if err != nil {
			t.Fatalf("trusted context read: %v", err)
		}
	})

	t.Run("failure rollback poison and callback lifetime", func(t *testing.T) {
		unit, _ := newStoreIntegrationUnit(t, adminURL)
		tenantID := mustTenantID(t, "ten_alpha")
		conversation := mustConversationSnapshot(t, tenantID, "cnv_rollback", 1, im.ConversationActive)
		command := mustCommand(t, "conversation.create", "rollback-create", "rollback-request")
		operationFailure := errors.New("operation canary")
		_, err := unit.Execute(t.Context(), tenantID, command, func(
			ctx context.Context,
			repositories store.TenantRepositories,
		) (store.SHA256Digest, error) {
			if _, err := repositories.Conversations().CompareAndSwapConversation(
				ctx,
				0,
				conversation,
			); err != nil {
				return store.SHA256Digest{}, err
			}
			return store.SHA256Digest{}, operationFailure
		})
		if !errors.Is(err, operationFailure) {
			t.Fatalf("rollback Execute error = %v", err)
		}
		if _, err := unit.Resolve(t.Context(), tenantID, command); !errors.Is(err, store.ErrNotFound) {
			t.Fatalf("rollback receipt error = %v", err)
		}
		assertConversationNotFound(t, unit, tenantID, conversation.Ref())

		createCommand := mustCommand(t, "conversation.create", "poison-seed", "poison-seed-request")
		if _, err := unit.Execute(t.Context(), tenantID, createCommand, func(
			ctx context.Context,
			repositories store.TenantRepositories,
		) (store.SHA256Digest, error) {
			_, err := repositories.Conversations().CompareAndSwapConversation(ctx, 0, conversation)
			return store.DigestBytes([]byte("poison-seed-result")), err
		}); err != nil {
			t.Fatalf("seed poison conversation: %v", err)
		}
		poisonCommand := mustCommand(t, "conversation.create", "poison-ignore", "poison-ignore-request")
		_, err = unit.Execute(t.Context(), tenantID, poisonCommand, func(
			ctx context.Context,
			repositories store.TenantRepositories,
		) (store.SHA256Digest, error) {
			_, _ = repositories.Conversations().CompareAndSwapConversation(ctx, 0, conversation)
			return store.DigestBytes([]byte("must-not-commit")), nil
		})
		if !errors.Is(err, store.ErrRevisionConflict) {
			t.Fatalf("ignored CAS error = %v, want %v", err, store.ErrRevisionConflict)
		}
		if _, err := unit.Resolve(t.Context(), tenantID, poisonCommand); !errors.Is(
			err,
			store.ErrNotFound,
		) {
			t.Fatalf("poison receipt error = %v", err)
		}

		var escaped store.TenantRepositories
		escapeCommand := mustCommand(t, "conversation.read", "escape-callback", "escape-request")
		if _, err := unit.Execute(t.Context(), tenantID, escapeCommand, func(
			_ context.Context,
			repositories store.TenantRepositories,
		) (store.SHA256Digest, error) {
			escaped = repositories
			return store.DigestBytes([]byte("escape-result")), nil
		}); err != nil {
			t.Fatalf("escape Execute: %v", err)
		}
		if _, err := escaped.Conversations().CurrentConversation(
			t.Context(),
			conversation.Ref(),
		); !errors.Is(err, store.ErrTransactionClosed) {
			t.Fatalf("escaped repository error = %v", err)
		}

		panicCommand := mustCommand(t, "conversation.read", "panic-callback", "panic-request")
		var escapedAfterPanic store.TenantRepositories
		func() {
			defer func() {
				if recover() == nil {
					t.Fatal("expected operation panic")
				}
			}()
			_, _ = unit.Execute(t.Context(), tenantID, panicCommand, func(
				_ context.Context,
				repositories store.TenantRepositories,
			) (store.SHA256Digest, error) {
				escapedAfterPanic = repositories
				panic("operation panic canary")
			})
		}()
		if _, err := escapedAfterPanic.Conversations().CurrentConversation(
			t.Context(),
			conversation.Ref(),
		); !errors.Is(err, store.ErrTransactionClosed) {
			t.Fatalf("panic-escaped repository error = %v", err)
		}
		if _, err := unit.Resolve(t.Context(), tenantID, panicCommand); !errors.Is(
			err,
			store.ErrNotFound,
		) {
			t.Fatalf("panic receipt error = %v", err)
		}
	})

	t.Run("out of band receipt conflict releases command lock before pool handoff", func(t *testing.T) {
		unit, pool := newStoreIntegrationUnit(t, adminURL)
		tenantID := mustTenantID(t, "ten_alpha")
		command := mustCommand(t, "conversation.create", "external-receipt", "external-request")
		resultDigest := store.DigestBytes([]byte("external-result"))
		var runtimeRole string
		if err := pool.QueryRow(t.Context(), "SELECT current_user").Scan(&runtimeRole); err != nil {
			t.Fatalf("read out-of-band runtime role: %v", err)
		}
		var operationCalls atomic.Int64
		receipt, err := unit.Execute(t.Context(), tenantID, command, func(
			ctx context.Context,
			_ store.TenantRepositories,
		) (store.SHA256Digest, error) {
			operationCalls.Add(1)
			externalConfig := pool.Config().ConnConfig.Copy()
			externalConnection, err := pgx.ConnectConfig(ctx, externalConfig)
			if err != nil {
				return store.SHA256Digest{}, fmt.Errorf("connect out-of-band writer: %w", err)
			}
			defer func() { _ = externalConnection.Close(context.Background()) }()
			if _, err := externalConnection.Exec(
				ctx,
				"SET ROLE "+pgx.Identifier{runtimeRole}.Sanitize(),
			); err != nil {
				return store.SHA256Digest{}, fmt.Errorf("select out-of-band runtime role: %w", err)
			}
			externalTransaction, err := externalConnection.BeginTx(ctx, pgx.TxOptions{})
			if err != nil {
				return store.SHA256Digest{}, fmt.Errorf("begin out-of-band writer: %w", err)
			}
			defer rollbackTransaction(externalTransaction)
			if _, err := setStoreTenant(ctx, externalTransaction, tenantID.String()); err != nil {
				return store.SHA256Digest{}, fmt.Errorf("bind out-of-band tenant: %w", err)
			}
			var committedAt time.Time
			if err := externalTransaction.QueryRow(ctx, `
SELECT wanwork_im.write_tenant_command_receipt($1, $2, $3, $4, $5)`,
				tenantID.String(),
				command.Kind(),
				command.IdempotencyKey(),
				command.RequestDigest().Hex(),
				resultDigest.Hex(),
			).Scan(&committedAt); err != nil {
				return store.SHA256Digest{}, fmt.Errorf("write out-of-band receipt: %w", err)
			}
			if committedAt.IsZero() {
				return store.SHA256Digest{}, errors.New("out-of-band receipt has zero commit time")
			}
			if err := externalTransaction.Commit(ctx); err != nil {
				return store.SHA256Digest{}, fmt.Errorf("commit out-of-band receipt: %w", err)
			}
			return resultDigest, nil
		})
		if err != nil || !receipt.Replayed() || receipt.ResolvedAfterUnknown() ||
			receipt.ResultDigest() != resultDigest || operationCalls.Load() != 1 {
			t.Fatalf("out-of-band receipt Execute = (%#v, %v), calls = %d", receipt, err, operationCalls.Load())
		}

		replayed, err := unit.Execute(t.Context(), tenantID, command, func(
			context.Context,
			store.TenantRepositories,
		) (store.SHA256Digest, error) {
			operationCalls.Add(1)
			return store.SHA256Digest{}, errors.New("replay callback must not run")
		})
		if err != nil || !replayed.Replayed() || replayed.ResultDigest() != resultDigest ||
			operationCalls.Load() != 1 {
			t.Fatalf("out-of-band receipt replay = (%#v, %v), calls = %d", replayed, err, operationCalls.Load())
		}
	})

	t.Run("concurrent exact retry and CAS have one writer", func(t *testing.T) {
		unit, _ := newStoreIntegrationUnit(t, adminURL)
		tenantID := mustTenantID(t, "ten_alpha")
		conversation := mustConversationSnapshot(t, tenantID, "cnv_concurrent", 1, im.ConversationActive)
		command := mustCommand(t, "conversation.create", "concurrent-exact", "concurrent-request")
		resultDigest := store.DigestBytes([]byte("concurrent-result"))
		var operationCalls atomic.Int64
		const workers = 64
		receipts := make([]store.CommitReceipt, workers)
		errorsByWorker := make([]error, workers)
		start := make(chan struct{})
		var waitGroup sync.WaitGroup
		for index := 0; index < workers; index++ {
			waitGroup.Add(1)
			go func(index int) {
				defer waitGroup.Done()
				<-start
				receipts[index], errorsByWorker[index] = unit.Execute(
					context.Background(),
					tenantID,
					command,
					func(
						ctx context.Context,
						repositories store.TenantRepositories,
					) (store.SHA256Digest, error) {
						operationCalls.Add(1)
						_, err := repositories.Conversations().CompareAndSwapConversation(
							ctx,
							0,
							conversation,
						)
						return resultDigest, err
					},
				)
			}(index)
		}
		close(start)
		waitGroup.Wait()
		fresh := 0
		for index := range workers {
			if errorsByWorker[index] != nil {
				t.Fatalf("exact worker %d error = %v", index, errorsByWorker[index])
			}
			if !receipts[index].Replayed() {
				fresh++
			}
			if receipts[index].ResultDigest() != resultDigest {
				t.Fatalf("exact worker %d result digest drift", index)
			}
		}
		if fresh != 1 || operationCalls.Load() != 1 {
			t.Fatalf("fresh receipts = %d, operation calls = %d", fresh, operationCalls.Load())
		}

		next := mustConversationSnapshot(t, tenantID, "cnv_concurrent", 2, im.ConversationArchived)
		casErrors := make([]error, workers)
		start = make(chan struct{})
		waitGroup = sync.WaitGroup{}
		for index := 0; index < workers; index++ {
			waitGroup.Add(1)
			go func(index int) {
				defer waitGroup.Done()
				<-start
				workerCommand := mustCommand(
					t,
					"conversation.update",
					fmt.Sprintf("cas-%02d", index),
					fmt.Sprintf("cas-request-%02d", index),
				)
				_, casErrors[index] = unit.Execute(
					context.Background(),
					tenantID,
					workerCommand,
					func(
						ctx context.Context,
						repositories store.TenantRepositories,
					) (store.SHA256Digest, error) {
						_, err := repositories.Conversations().CompareAndSwapConversation(
							ctx,
							1,
							next,
						)
						return store.DigestBytes([]byte("cas-result")), err
					},
				)
			}(index)
		}
		close(start)
		waitGroup.Wait()
		winners := 0
		for index, err := range casErrors {
			if err == nil {
				winners++
				continue
			}
			if !errors.Is(err, store.ErrRevisionConflict) &&
				!errors.Is(err, store.ErrStoreUnavailable) {
				t.Fatalf("CAS worker %d error = %v", index, err)
			}
		}
		if winners != 1 {
			t.Fatalf("CAS winners = %d, want 1", winners)
		}
		var current im.ConversationSnapshot
		if err := unit.Read(t.Context(), tenantID, func(
			ctx context.Context,
			repositories store.TenantRepositories,
		) error {
			var err error
			current, err = repositories.Conversations().CurrentConversation(ctx, conversation.Ref())
			return err
		}); err != nil || current.Revision() != 2 || current.Status() != im.ConversationArchived {
			t.Fatalf("current after CAS = %#v, error = %v", current, err)
		}
	})

	t.Run("unknown commit is reconciled on a new connection", func(t *testing.T) {
		unit, _ := newStoreIntegrationUnit(t, adminURL)
		tenantID := mustTenantID(t, "ten_alpha")
		conversation := mustConversationSnapshot(t, tenantID, "cnv_unknown", 1, im.ConversationActive)
		command := mustCommand(t, "conversation.create", "unknown-commit", "unknown-request")
		resultDigest := store.DigestBytes([]byte("unknown-result"))
		var injected atomic.Bool
		unit.commitHook = func(ctx context.Context, transaction pgx.Tx) error {
			if !injected.CompareAndSwap(false, true) {
				return transaction.Commit(ctx)
			}
			if err := transaction.Commit(ctx); err != nil {
				return err
			}
			return errors.New("synthetic commit acknowledgement loss")
		}
		receipt, err := unit.Execute(t.Context(), tenantID, command, func(
			ctx context.Context,
			repositories store.TenantRepositories,
		) (store.SHA256Digest, error) {
			_, err := repositories.Conversations().CompareAndSwapConversation(ctx, 0, conversation)
			return resultDigest, err
		})
		if err != nil || !receipt.Replayed() || !receipt.ResolvedAfterUnknown() ||
			receipt.ResultDigest() != resultDigest {
			t.Fatalf("unknown Execute = (%#v, %v)", receipt, err)
		}
		resolved, err := unit.Resolve(t.Context(), tenantID, command)
		if err != nil || resolved.ResultDigest() != resultDigest {
			t.Fatalf("Resolve after unknown = (%#v, %v)", resolved, err)
		}

		unit.commitHook = func(_ context.Context, transaction pgx.Tx) error {
			rollbackTransaction(transaction)
			return pgx.ErrTxCommitRollback
		}
		rolledBack := mustConversationSnapshot(t, tenantID, "cnv_rolled_back", 1, im.ConversationActive)
		rollbackCommand := mustCommand(t, "conversation.create", "known-rollback", "known-rollback-request")
		_, err = unit.Execute(t.Context(), tenantID, rollbackCommand, func(
			ctx context.Context,
			repositories store.TenantRepositories,
		) (store.SHA256Digest, error) {
			_, err := repositories.Conversations().CompareAndSwapConversation(ctx, 0, rolledBack)
			return store.DigestBytes([]byte("known-rollback-result")), err
		})
		if !errors.Is(err, store.ErrStoreUnavailable) {
			t.Fatalf("known rollback error = %v", err)
		}
		if _, err := unit.Resolve(t.Context(), tenantID, rollbackCommand); !errors.Is(
			err,
			store.ErrNotFound,
		) {
			t.Fatalf("known rollback receipt error = %v", err)
		}
		assertConversationNotFound(t, unit, tenantID, rolledBack.Ref())
	})

	t.Run("runtime role can only write through fixed functions", func(t *testing.T) {
		unit, pool := newStoreIntegrationUnit(t, adminURL)
		tenantID := mustTenantID(t, "ten_alpha")
		conversation := mustConversationSnapshot(t, tenantID, "cnv_immutable", 1, im.ConversationActive)
		command := mustCommand(t, "conversation.create", "immutable-create", "immutable-request")
		if _, err := unit.Execute(t.Context(), tenantID, command, func(
			ctx context.Context,
			repositories store.TenantRepositories,
		) (store.SHA256Digest, error) {
			_, err := repositories.Conversations().CompareAndSwapConversation(ctx, 0, conversation)
			return store.DigestBytes([]byte("immutable-result")), err
		}); err != nil {
			t.Fatalf("seed immutable history: %v", err)
		}
		var sessionUser, currentUser string
		if err := pool.QueryRow(t.Context(), "SELECT session_user, current_user").Scan(
			&sessionUser,
			&currentUser,
		); err != nil || sessionUser == currentUser {
			t.Fatalf("runtime identities session=%q current=%q error=%v", sessionUser, currentUser, err)
		}
		var superuser, bypassRLS, inherit bool
		if err := pool.QueryRow(t.Context(), `
SELECT role_value.rolsuper,
       role_value.rolbypassrls,
       role_value.rolinherit
FROM pg_catalog.pg_roles AS role_value
WHERE role_value.rolname = current_user`).Scan(&superuser, &bypassRLS, &inherit); err != nil {
			t.Fatalf("read runtime role attributes: %v", err)
		}
		if superuser || bypassRLS || inherit {
			t.Fatalf("unsafe runtime role: super=%v bypass=%v inherit=%v", superuser, bypassRLS, inherit)
		}
		type sqlFixture struct {
			name string
			sql  string
		}
		type tableFixture struct {
			name         string
			updateColumn string
		}
		fixtures := make([]sqlFixture, 0, 93)
		for _, table := range []tableFixture{
			{name: "actor_heads", updateColumn: "current_revision"},
			{name: "actor_snapshots", updateColumn: "status"},
			{name: "conversation_access_heads", updateColumn: "current_revision"},
			{name: "conversation_access_snapshots", updateColumn: "can_read"},
			{name: "conversation_heads", updateColumn: "current_revision"},
			{name: "conversation_membership_heads", updateColumn: "current_revision"},
			{name: "conversation_membership_snapshots", updateColumn: "status"},
			{name: "conversation_snapshots", updateColumn: "status"},
			{name: "human_identity_binding_heads", updateColumn: "current_revision"},
			{name: "human_identity_binding_snapshots", updateColumn: "status"},
			{name: "human_principal_heads", updateColumn: "current_revision"},
			{name: "human_principal_snapshots", updateColumn: "status"},
			{name: "provider_actor_binding_heads", updateColumn: "current_revision"},
			{name: "provider_actor_binding_snapshots", updateColumn: "status"},
			{name: "provider_conversation_binding_heads", updateColumn: "current_revision"},
			{name: "provider_conversation_binding_snapshots", updateColumn: "status"},
			{name: "provider_realms", updateColumn: "status"},
			{name: "tenant_membership_heads", updateColumn: "current_revision"},
			{name: "tenant_membership_snapshots", updateColumn: "status"},
			{name: "tenant_command_receipts", updateColumn: "result_sha256"},
			{name: "tenants", updateColumn: "status"},
			{name: "workspaces", updateColumn: "status"},
			{name: "agent_definitions", updateColumn: "status"},
			{name: "agent_releases", updateColumn: "status"},
			{name: "agent_passports", updateColumn: "status"},
			{name: "agent_installation_heads", updateColumn: "current_revision"},
			{name: "agent_installation_snapshots", updateColumn: "status"},
		} {
			qualifiedTable := "wanwork_im." + pgx.Identifier{table.name}.Sanitize()
			quotedUpdateColumn := pgx.Identifier{table.updateColumn}.Sanitize()
			for _, operation := range []sqlFixture{
				{name: "insert", sql: "INSERT INTO " + qualifiedTable + " DEFAULT VALUES"},
				{name: "update", sql: "UPDATE " + qualifiedTable + " SET " + quotedUpdateColumn + " = " + quotedUpdateColumn + " WHERE false"},
				{name: "delete", sql: "DELETE FROM " + qualifiedTable + " WHERE false"},
				{name: "truncate", sql: "TRUNCATE TABLE " + qualifiedTable},
			} {
				fixtures = append(fixtures, sqlFixture{
					name: operation.name + " " + table.name,
					sql:  operation.sql,
				})
			}
		}
		fixtures = append(fixtures,
			sqlFixture{name: "create schema", sql: "CREATE SCHEMA runtime_escape"},
			sqlFixture{name: "create table", sql: "CREATE TABLE wanwork_im.runtime_escape (id bigint)"},
			sqlFixture{
				name: "create function",
				sql: "CREATE FUNCTION wanwork_im.runtime_escape() RETURNS boolean " +
					"LANGUAGE sql AS 'SELECT true'",
			},
			sqlFixture{
				name: "alter policy",
				sql: "ALTER POLICY conversation_heads_exact_tenant " +
					"ON wanwork_im.conversation_heads USING (true)",
			},
			sqlFixture{name: "create temporary table", sql: "CREATE TEMPORARY TABLE runtime_escape (id bigint)"},
		)
		for _, fixture := range fixtures {
			t.Run(fixture.name, func(t *testing.T) {
				transaction, err := pool.BeginTx(t.Context(), pgx.TxOptions{})
				if err != nil {
					t.Fatalf("begin immutable attack: %v", err)
				}
				defer rollbackTransaction(transaction)
				if err := bindTenantTransaction(t.Context(), transaction, tenantID); err != nil {
					t.Fatalf("bind immutable attack tenant: %v", err)
				}
				_, err = transaction.Exec(t.Context(), fixture.sql)
				var postgresError *pgconn.PgError
				if !errors.As(err, &postgresError) || postgresError.Code != "42501" {
					t.Fatalf("immutable attack error = %v, want SQLSTATE 42501", err)
				}
			})
		}
	})
}

func newStoreIntegrationUnit(t *testing.T, adminURL string) (*UnitOfWork, *pgxpool.Pool) {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), 30*time.Second)
	defer cancel()
	adminConfig, err := pgx.ParseConfig(adminURL)
	if err != nil {
		t.Fatalf("parse %s: %v", storeIntegrationURL, err)
	}
	adminConnection, err := pgx.ConnectConfig(ctx, adminConfig.Copy())
	if err != nil {
		t.Fatalf("connect PostgreSQL admin: %v", err)
	}
	databaseName := fmt.Sprintf(
		"wanwork_store_%d_%d",
		os.Getpid(),
		storeDatabaseSequence.Add(1),
	)
	quotedDatabase := pgx.Identifier{databaseName}.Sanitize()
	if _, err := adminConnection.Exec(ctx, "CREATE DATABASE "+quotedDatabase); err != nil {
		_ = adminConnection.Close(context.Background())
		t.Fatalf("create store database: %v", err)
	}
	if _, err := adminConnection.Exec(
		ctx,
		"REVOKE CONNECT, CREATE, TEMPORARY ON DATABASE "+quotedDatabase+" FROM PUBLIC",
	); err != nil {
		t.Fatalf("revoke public database access: %v", err)
	}
	databaseConfig := adminConfig.Copy()
	databaseConfig.Database = databaseName
	ownerConnection, err := pgx.ConnectConfig(ctx, databaseConfig)
	if err != nil {
		_, _ = adminConnection.Exec(context.Background(), "DROP DATABASE "+quotedDatabase+" WITH (FORCE)")
		_ = adminConnection.Close(context.Background())
		t.Fatalf("connect store database: %v", err)
	}
	if _, err := migrations.Apply(ctx, ownerConnection); err != nil {
		t.Fatalf("apply store migrations: %v", err)
	}
	seedStoreRoots(t, ownerConnection)

	roleName := fmt.Sprintf(
		"wanwork_store_role_%d_%d",
		os.Getpid(),
		storeDatabaseSequence.Add(1),
	)
	loginName := fmt.Sprintf(
		"wanwork_store_login_%d_%d",
		os.Getpid(),
		storeDatabaseSequence.Add(1),
	)
	loginPassword := fmt.Sprintf(
		"test-only-%d-%d",
		os.Getpid(),
		storeDatabaseSequence.Add(1),
	)
	quotedRole := pgx.Identifier{roleName}.Sanitize()
	quotedLogin := pgx.Identifier{loginName}.Sanitize()
	if _, err := ownerConnection.Exec(
		ctx,
		"CREATE ROLE "+quotedRole+" NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT",
	); err != nil {
		t.Fatalf("create store role: %v", err)
	}
	if _, err := ownerConnection.Exec(
		ctx,
		"CREATE ROLE "+quotedLogin+" LOGIN PASSWORD '"+loginPassword+
			"' NOSUPERUSER NOINHERIT NOCREATEROLE NOCREATEDB NOREPLICATION NOBYPASSRLS CONNECTION LIMIT -1",
	); err != nil {
		t.Fatalf("create store login: %v", err)
	}
	if _, err := ownerConnection.Exec(
		ctx,
		"GRANT "+quotedRole+" TO "+quotedLogin+
			" WITH ADMIN FALSE, INHERIT FALSE, SET TRUE",
	); err != nil {
		t.Fatalf("grant store role to login: %v", err)
	}
	if _, err := ownerConnection.Exec(
		ctx,
		"GRANT CONNECT ON DATABASE "+quotedDatabase+" TO "+quotedLogin,
	); err != nil {
		t.Fatalf("grant store login database access: %v", err)
	}
	grantStoreRole(t, ownerConnection, quotedRole)
	if _, err := migrations.Apply(ctx, ownerConnection); err != nil {
		t.Fatalf("repeat migrations after runtime grants: %v", err)
	}
	poolConfig, err := pgxpool.ParseConfig(adminURL)
	if err != nil {
		t.Fatalf("parse store pool config: %v", err)
	}
	poolConfig.ConnConfig.Database = databaseName
	poolConfig.ConnConfig.User = loginName
	poolConfig.ConnConfig.Password = loginPassword
	poolConfig.MaxConns = 16
	poolConfig.AfterConnect = func(ctx context.Context, connection *pgx.Conn) error {
		_, err := connection.Exec(ctx, "SET ROLE "+quotedRole)
		return err
	}
	pool, err := pgxpool.NewWithConfig(ctx, poolConfig)
	if err != nil {
		t.Fatalf("create store pool: %v", err)
	}
	if err := pool.Ping(ctx); err != nil {
		t.Fatalf("ping store pool: %v", err)
	}
	t.Cleanup(func() {
		pool.Close()
		closeContext, closeCancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer closeCancel()
		_ = ownerConnection.Close(closeContext)
		_, _ = adminConnection.Exec(closeContext, "DROP DATABASE "+quotedDatabase+" WITH (FORCE)")
		_, _ = adminConnection.Exec(closeContext, "DROP ROLE "+quotedLogin+", "+quotedRole)
		_ = adminConnection.Close(closeContext)
	})
	unit := &UnitOfWork{pool: pool, commitHook: commitTransaction}
	return unit, pool
}

func seedStoreRoots(t *testing.T, connection *pgx.Conn) {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), 15*time.Second)
	defer cancel()
	transaction, err := connection.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.Serializable})
	if err != nil {
		t.Fatalf("begin store seed: %v", err)
	}
	defer rollbackTransaction(transaction)
	if _, err := transaction.Exec(ctx, `
INSERT INTO wanwork_im.provider_realms (provider, realm_id, status, revision)

VALUES ('clerk', 'rlm_clerk', 'active', 1),
       ('rongcloud', 'rlm_rong', 'active', 1)`); err != nil {
		t.Fatalf("seed provider realm: %v", err)
	}
	for _, statement := range []string{
		`INSERT INTO wanwork_im.human_principal_heads (principal_id, current_revision)
         VALUES ('hpr_alice', 1)`,
		`INSERT INTO wanwork_im.human_principal_snapshots (principal_id, revision, status)
         VALUES ('hpr_alice', 1, 'active')`,
		`INSERT INTO wanwork_im.human_identity_binding_heads (
             provider, realm_id, subject_id, current_revision, current_principal_id, current_status
         ) VALUES ('clerk', 'rlm_clerk', 'user_alice', 1, 'hpr_alice', 'active')`,
		`INSERT INTO wanwork_im.human_identity_binding_snapshots (
             provider, realm_id, subject_id, revision, principal_id, status
         ) VALUES ('clerk', 'rlm_clerk', 'user_alice', 1, 'hpr_alice', 'active')`,
	} {
		if _, err := transaction.Exec(ctx, statement); err != nil {
			t.Fatalf("seed global identity: %v", err)
		}
	}
	if _, err := setStoreTenant(ctx, transaction, "ten_alpha"); err != nil {
		t.Fatalf("set alpha seed tenant: %v", err)
	}
	for _, statement := range []string{
		`INSERT INTO wanwork_im.tenants (tenant_id, status, revision)
         VALUES ('ten_alpha', 'active', 1)`,
		`INSERT INTO wanwork_im.workspaces (tenant_id, workspace_id, status, revision)
         VALUES ('ten_alpha', 'wsp_alpha', 'active', 1)`,
		`INSERT INTO wanwork_im.actor_heads (tenant_id, actor_id, current_revision)
         VALUES ('ten_alpha', 'usr_alice', 1)`,
		`INSERT INTO wanwork_im.actor_snapshots (
             tenant_id, actor_id, revision, subject_type, status
         ) VALUES ('ten_alpha', 'usr_alice', 1, 'human', 'active')`,
		`INSERT INTO wanwork_im.actor_heads (tenant_id, actor_id, current_revision)
         VALUES ('ten_alpha', 'agt_repository', 1)`,
		`INSERT INTO wanwork_im.actor_snapshots (
             tenant_id, actor_id, revision, subject_type, status
         ) VALUES ('ten_alpha', 'agt_repository', 1, 'agent', 'active')`,
		`INSERT INTO wanwork_im.tenant_membership_heads (
             tenant_id, principal_id, actor_id, current_revision
         ) VALUES ('ten_alpha', 'hpr_alice', 'usr_alice', 1)`,
		`INSERT INTO wanwork_im.tenant_membership_snapshots (
             tenant_id, principal_id, actor_id, revision, role, status
         ) VALUES ('ten_alpha', 'hpr_alice', 'usr_alice', 1, 'owner', 'active')`,
	} {
		if _, err := transaction.Exec(ctx, statement); err != nil {
			t.Fatalf("seed alpha roots: %v", err)
		}
	}
	if _, err := setStoreTenant(ctx, transaction, "ten_beta"); err != nil {
		t.Fatalf("set beta seed tenant: %v", err)
	}
	for _, statement := range []string{
		`INSERT INTO wanwork_im.tenants (tenant_id, status, revision)
         VALUES ('ten_beta', 'active', 1)`,
		`INSERT INTO wanwork_im.workspaces (tenant_id, workspace_id, status, revision)
         VALUES ('ten_beta', 'wsp_beta', 'active', 1)`,
	} {
		if _, err := transaction.Exec(ctx, statement); err != nil {
			t.Fatalf("seed beta roots: %v", err)
		}
	}
	if err := transaction.Commit(ctx); err != nil {
		t.Fatalf("commit store seed: %v", err)
	}
}

func grantStoreRole(t *testing.T, connection *pgx.Conn, quotedRole string) {
	t.Helper()
	for _, statement := range []string{
		"GRANT USAGE ON SCHEMA wanwork_im TO " + quotedRole,
		`GRANT EXECUTE ON FUNCTION
             wanwork_im.write_conversation_revision(text, text, bigint, bigint, text, text, text),
             wanwork_im.write_provider_conversation_binding_revision(text, text, text, text, bigint, bigint, text, text),
             wanwork_im.write_conversation_membership_revision(text, text, text, bigint, bigint, text, text),
             wanwork_im.write_conversation_access_revision(text, text, text, bigint, bigint, boolean, boolean, boolean, boolean, boolean, boolean),
             wanwork_im.write_tenant_command_receipt(text, text, text, text, text),
             wanwork_im.write_agent_definition_revision(text, text, bigint, bigint, text),
             wanwork_im.write_agent_release_revision(text, text, bigint, bigint, text),
             wanwork_im.write_agent_passport_revision(text, text, bigint, bigint, text),
             wanwork_im.write_agent_installation_revision(text, text, bigint, bigint, text)
         TO ` + quotedRole,
		`GRANT SELECT ON
		     wanwork_im.conversation_heads,
		     wanwork_im.conversation_snapshots,
             wanwork_im.provider_conversation_binding_heads,
             wanwork_im.provider_conversation_binding_snapshots,
             wanwork_im.conversation_membership_heads,
             wanwork_im.conversation_membership_snapshots,
             wanwork_im.conversation_access_heads,
             wanwork_im.conversation_access_snapshots,
		     wanwork_im.tenant_command_receipts,
		     wanwork_im.actor_heads,
		     wanwork_im.actor_snapshots,
		     wanwork_im.human_identity_binding_heads,
		     wanwork_im.human_identity_binding_snapshots,
		     wanwork_im.human_principal_heads,
		     wanwork_im.human_principal_snapshots,
		     wanwork_im.tenant_membership_heads,
		     wanwork_im.tenant_membership_snapshots,
		     wanwork_im.agent_definitions,
		     wanwork_im.agent_releases,
		     wanwork_im.agent_passports,
		     wanwork_im.agent_installation_heads,
		     wanwork_im.agent_installation_snapshots TO ` + quotedRole,
	} {
		if _, err := connection.Exec(t.Context(), statement); err != nil {
			t.Fatalf("grant store role: %v", err)
		}
	}
}

func setStoreTenant(ctx context.Context, transaction pgx.Tx, tenantID string) (string, error) {
	var recorded string
	err := transaction.QueryRow(
		ctx,
		"SELECT pg_catalog.set_config('wanwork.tenant_id', $1, true)",
		tenantID,
	).Scan(&recorded)
	return recorded, err
}

func assertConversationNotFound(
	t *testing.T,
	unit *UnitOfWork,
	tenantID im.TenantID,
	reference im.ConversationRef,
) {
	t.Helper()
	err := unit.Read(t.Context(), tenantID, func(
		ctx context.Context,
		repositories store.TenantRepositories,
	) error {
		_, err := repositories.Conversations().CurrentConversation(ctx, reference)
		return err
	})
	if !errors.Is(err, store.ErrNotFound) {
		t.Fatalf("conversation %s error = %v, want %v", reference.ConversationID().String(), err, store.ErrNotFound)
	}
}

func mustCommand(t *testing.T, kind, key, body string) store.CommandIdentity {
	t.Helper()
	command, err := store.NewCommandIdentity(kind, key, store.DigestBytes([]byte(body)))
	if err != nil {
		t.Fatalf("create command %q/%q: %v", kind, key, err)
	}
	return command
}

func mustConversationSnapshot(
	t *testing.T,
	tenantID im.TenantID,
	conversationValue string,
	revision uint64,
	status im.ConversationStatus,
) im.ConversationSnapshot {
	t.Helper()
	reference := mustConversationRef(t, tenantID, conversationValue)
	workspaceID, err := im.ParseWorkspaceID("wsp_alpha")
	if err != nil {
		t.Fatalf("parse workspace: %v", err)
	}
	snapshot, err := im.NewConversationSnapshot(
		reference,
		&workspaceID,
		im.ConversationGroup,
		status,
		im.ConversationID{},
		im.MessageID{},
		im.InvocationID{},
		revision,
	)
	if err != nil {
		t.Fatalf("create conversation snapshot: %v", err)
	}
	return snapshot
}

func mustActorRef(t *testing.T, tenantID im.TenantID, actorValue string) im.ActorRef {
	t.Helper()
	actorID, err := im.ParseActorID(actorValue)
	if err != nil {
		t.Fatalf("parse actor: %v", err)
	}
	reference, err := im.NewActorRef(tenantID, actorID)
	if err != nil {
		t.Fatalf("create actor reference: %v", err)
	}
	return reference
}

func mustProviderConversationRef(t *testing.T, subjectID string) im.ProviderConversationRef {
	t.Helper()
	realmID, err := im.ParseProviderRealmID("rlm_rong")
	if err != nil {
		t.Fatalf("parse provider realm: %v", err)
	}
	reference, err := im.NewProviderConversationRef(
		im.IdentityProviderRongCloud,
		realmID,
		subjectID,
	)
	if err != nil {
		t.Fatalf("create provider conversation reference: %v", err)
	}
	return reference
}

func mustProviderConversationBinding(
	t *testing.T,
	externalReference im.ProviderConversationRef,
	conversationReference im.ConversationRef,
	revision uint64,
) im.ProviderConversationBinding {
	t.Helper()
	binding, err := im.NewProviderConversationBinding(
		externalReference,
		conversationReference,
		im.ExternalIdentityBindingActive,
		revision,
	)
	if err != nil {
		t.Fatalf("create provider conversation binding: %v", err)
	}
	return binding
}

func mustMembership(
	t *testing.T,
	conversationReference im.ConversationRef,
	actorReference im.ActorRef,
	revision uint64,
) im.ConversationMembershipSnapshot {
	t.Helper()
	membership, err := im.NewConversationMembershipSnapshot(
		conversationReference,
		actorReference,
		im.ConversationMembershipOwner,
		im.ConversationMembershipActive,
		revision,
	)
	if err != nil {
		t.Fatalf("create conversation membership: %v", err)
	}
	return membership
}

func mustAccess(
	t *testing.T,
	conversationReference im.ConversationRef,
	actorReference im.ActorRef,
	permissions []im.ConversationPermission,
	revision uint64,
) im.ConversationAccessSnapshot {
	t.Helper()
	access, err := im.NewConversationAccessSnapshot(
		conversationReference,
		actorReference,
		permissions,
		revision,
	)
	if err != nil {
		t.Fatalf("create conversation access: %v", err)
	}
	return access
}

func mustAgentDefinitionID(t *testing.T, value string) im.AgentDefinitionID {
	t.Helper()
	id, err := im.ParseAgentDefinitionID(value)
	if err != nil {
		t.Fatalf("parse Agent definition: %v", err)
	}
	return id
}

func mustPublisherID(t *testing.T, value string) agentstore.PublisherID {
	t.Helper()
	id, err := agentstore.ParsePublisherID(value)
	if err != nil {
		t.Fatalf("parse publisher: %v", err)
	}
	return id
}

func mustPrincipalID(t *testing.T, value string) im.HumanPrincipalID {
	t.Helper()
	id, err := im.ParseHumanPrincipalID(value)
	if err != nil {
		t.Fatalf("parse principal: %v", err)
	}
	return id
}

func mustWorkspaceID(t *testing.T, value string) im.WorkspaceID {
	t.Helper()
	id, err := im.ParseWorkspaceID(value)
	if err != nil {
		t.Fatalf("parse workspace: %v", err)
	}
	return id
}

func mustActorID(t *testing.T, value string) im.ActorID {
	t.Helper()
	id, err := im.ParseActorID(value)
	if err != nil {
		t.Fatalf("parse Actor: %v", err)
	}
	return id
}

func mustAgentDefinition(
	t *testing.T,
	id im.AgentDefinitionID,
	tenant im.TenantID,
	claimedBy im.HumanPrincipalID,
	publisher agentstore.PublisherID,
	revision uint64,
	status agentstore.DefinitionStatus,
) agentstore.DefinitionSnapshot {
	t.Helper()
	value, err := agentstore.NewDefinitionSnapshot(id, tenant, claimedBy, publisher, "Repository Agent", "integration", status, revision)
	if err != nil {
		t.Fatalf("create Agent definition: %v", err)
	}
	return value
}

func mustAgentRelease(
	t *testing.T,
	definitionID im.AgentDefinitionID,
	revision uint64,
	status agentstore.ReleaseStatus,
) agentstore.ReleaseSnapshot {
	t.Helper()
	releaseID, err := agentstore.ParseReleaseID("agr_repository")
	if err != nil {
		t.Fatalf("parse release: %v", err)
	}
	version, err := im.ParseAgentVersion("1.0.0")
	if err != nil {
		t.Fatalf("parse Agent version: %v", err)
	}
	route, err := agentstore.NewDataRoute(
		"conversation.context", agentstore.DataInput, agentstore.DataInternal, []string{"local"}, 1,
	)
	if err != nil {
		t.Fatalf("create Agent route: %v", err)
	}
	publishedAt := time.Date(2026, 8, 29, 11, 0, 0, 0, time.UTC)
	value, err := agentstore.NewReleaseSnapshot(
		releaseID, definitionID, version,
		agentstore.DigestBytes([]byte("agent-artifact")), agentstore.DigestBytes([]byte("agent-manifest")),
		agentstore.DigestBytes([]byte("agent-persona")), []agentstore.Capability{"conversation.read"}, nil,
		[]agentstore.DataRoute{route}, agentstore.IsolationProcess, status, publishedAt, revision,
	)
	if err != nil {
		t.Fatalf("create Agent release: %v", err)
	}
	return value
}

func mustAgentPassport(
	t *testing.T,
	definition agentstore.DefinitionSnapshot,
	release agentstore.ReleaseSnapshot,
	revision uint64,
) agentstore.TrustPassport {
	t.Helper()
	issuedAt := release.PublishedAt().Add(-time.Hour)
	claims := []agentstore.AttestationClaim{
		agentstore.AttestationPublisherVerified,
		agentstore.AttestationSecurityReviewed,
		agentstore.AttestationDataRoutesReviewed,
	}
	attestations := make([]agentstore.TrustAttestation, 0, len(claims))
	for _, claim := range claims {
		attestation, err := agentstore.NewTrustAttestation(
			definition.PublisherID(), claim, 1, agentstore.DigestBytes([]byte(string(claim))),
			issuedAt, release.PublishedAt().Add(24*time.Hour),
		)
		if err != nil {
			t.Fatalf("create Agent attestation: %v", err)
		}
		attestations = append(attestations, attestation)
	}
	value, err := agentstore.NewTrustPassport(definition, release, attestations, agentstore.PassportActive, revision)
	if err != nil {
		t.Fatalf("create Agent Passport: %v", err)
	}
	return value
}

func mustAgentInstallation(
	t *testing.T,
	tenant im.TenantID,
	workspace im.WorkspaceID,
	actor im.ActorID,
	installedBy im.HumanPrincipalID,
	passport agentstore.TrustPassport,
	createdAt time.Time,
	status agentstore.InstallationStatus,
	revision uint64,
) agentstore.InstallationSnapshot {
	t.Helper()
	installationID, err := agentstore.ParseInstallationID("ins_repository")
	if err != nil {
		t.Fatalf("parse installation: %v", err)
	}
	value, err := agentstore.NewInstallationSnapshot(
		installationID, tenant, workspace, actor, installedBy, passport,
		[]agentstore.Capability{"conversation.read"}, []string{"conversation.context"},
		status, createdAt, time.Time{}, revision,
	)
	if err != nil {
		t.Fatalf("create Agent installation: %v", err)
	}
	return value
}
