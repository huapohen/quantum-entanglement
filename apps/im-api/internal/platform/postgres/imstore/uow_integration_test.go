package imstore

import (
	"context"
	"errors"
	"fmt"
	"os"
	"reflect"
	"sync"
	"sync/atomic"
	"testing"
	"time"

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

	t.Run("runtime role cannot rewrite immutable history", func(t *testing.T) {
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
		for _, fixture := range []struct {
			name string
			sql  string
		}{
			{
				name: "update conversation snapshot",
				sql: `UPDATE wanwork_im.conversation_snapshots
                      SET status = 'closed'
                      WHERE tenant_id = 'ten_alpha'
                        AND conversation_id = 'cnv_immutable'
                        AND revision = 1`,
			},
			{
				name: "delete conversation snapshot",
				sql: `DELETE FROM wanwork_im.conversation_snapshots
                      WHERE tenant_id = 'ten_alpha'
                        AND conversation_id = 'cnv_immutable'
                        AND revision = 1`,
			},
			{
				name: "truncate conversation snapshot",
				sql:  "TRUNCATE TABLE wanwork_im.conversation_snapshots",
			},
			{
				name: "update receipt",
				sql: `UPDATE wanwork_im.tenant_command_receipts
                      SET result_sha256 = repeat('0', 64)
                      WHERE tenant_id = 'ten_alpha'
                        AND command_kind = 'conversation.create'
                        AND idempotency_key = 'immutable-create'`,
			},
			{
				name: "delete receipt",
				sql: `DELETE FROM wanwork_im.tenant_command_receipts
                      WHERE tenant_id = 'ten_alpha'
                        AND command_kind = 'conversation.create'
                        AND idempotency_key = 'immutable-create'`,
			},
		} {
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
	quotedRole := pgx.Identifier{roleName}.Sanitize()
	if _, err := ownerConnection.Exec(
		ctx,
		"CREATE ROLE "+quotedRole+" NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT",
	); err != nil {
		t.Fatalf("create store role: %v", err)
	}
	grantStoreRole(t, ownerConnection, quotedRole)
	poolConfig, err := pgxpool.ParseConfig(adminURL)
	if err != nil {
		t.Fatalf("parse store pool config: %v", err)
	}
	poolConfig.ConnConfig.Database = databaseName
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
		_, _ = adminConnection.Exec(closeContext, "DROP ROLE "+quotedRole)
		_ = adminConnection.Close(closeContext)
	})
	unit, err := NewUnitOfWork(pool)
	if err != nil {
		t.Fatalf("create unit of work: %v", err)
	}
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
VALUES ('rongcloud', 'rlm_rong', 'active', 1)`); err != nil {
		t.Fatalf("seed provider realm: %v", err)
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
		`GRANT SELECT ON
             wanwork_im.provider_realms,
             wanwork_im.tenants,
             wanwork_im.workspaces,
             wanwork_im.actor_heads,
             wanwork_im.actor_snapshots,
             wanwork_im.conversation_heads,
             wanwork_im.conversation_snapshots,
             wanwork_im.provider_conversation_binding_heads,
             wanwork_im.provider_conversation_binding_snapshots,
             wanwork_im.conversation_membership_heads,
             wanwork_im.conversation_membership_snapshots,
             wanwork_im.conversation_access_heads,
             wanwork_im.conversation_access_snapshots,
             wanwork_im.tenant_command_receipts TO ` + quotedRole,
		`GRANT INSERT, UPDATE ON
             wanwork_im.conversation_heads,
             wanwork_im.provider_conversation_binding_heads,
             wanwork_im.conversation_membership_heads,
             wanwork_im.conversation_access_heads TO ` + quotedRole,
		`GRANT INSERT ON
             wanwork_im.conversation_snapshots,
             wanwork_im.provider_conversation_binding_snapshots,
             wanwork_im.conversation_membership_snapshots,
             wanwork_im.conversation_access_snapshots,
             wanwork_im.tenant_command_receipts TO ` + quotedRole,
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
