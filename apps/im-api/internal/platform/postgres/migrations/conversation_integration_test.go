package migrations

import (
	"context"
	"errors"
	"fmt"
	"os"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
)

func TestConversationAgainstPostgres(t *testing.T) {
	adminURL := os.Getenv(postgresIntegrationURL)
	if adminURL == "" {
		t.Skip(postgresIntegrationURL + " is not set")
	}

	t.Run("ordinary conversations freeze scope type and provider routing", func(t *testing.T) {
		connection, _ := newIntegrationDatabase(t, adminURL)
		applyMigrationsForConversationTest(t, connection)
		seedIdentityAuthority(t, connection)
		seedConversations(t, connection)

		ctx, cancel := context.WithTimeout(t.Context(), 15*time.Second)
		defer cancel()
		var conversations int
		if err := connection.QueryRow(ctx, `
SELECT count(*)
FROM wanwork_im.conversation_heads
WHERE tenant_id = 'ten_alpha'`).Scan(&conversations); err != nil || conversations != 2 {
			t.Fatalf("conversation count = %d, error = %v", conversations, err)
		}
		var nilWorkspace bool
		if err := connection.QueryRow(ctx, `
SELECT workspace_id IS NULL
FROM wanwork_im.conversation_snapshots
WHERE tenant_id = 'ten_alpha'
  AND conversation_id = 'cnv_alpha_direct'
  AND revision = 1`).Scan(&nilWorkspace); err != nil || !nilWorkspace {
			t.Fatalf("nil workspace = %v, error = %v", nilWorkspace, err)
		}

		transaction, err := connection.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.Serializable})
		if err != nil {
			t.Fatalf("begin conversation CAS: %v", err)
		}
		if _, err := setTenantContext(ctx, transaction, "ten_alpha"); err != nil {
			_ = transaction.Rollback(context.Background())
			t.Fatalf("set conversation tenant: %v", err)
		}
		if _, err := transaction.Exec(ctx, `
INSERT INTO wanwork_im.conversation_snapshots (
    tenant_id, conversation_id, revision, workspace_id, conversation_type, status
) VALUES (
    'ten_alpha', 'cnv_alpha_group', 2, 'wsp_alpha', 'group', 'archived'
)`); err != nil {
			_ = transaction.Rollback(context.Background())
			t.Fatalf("insert conversation revision: %v", err)
		}
		commandTag, err := transaction.Exec(ctx, `
UPDATE wanwork_im.conversation_heads
SET current_revision = 2
WHERE tenant_id = 'ten_alpha'
  AND conversation_id = 'cnv_alpha_group'
  AND current_revision = 1`)
		if err != nil || commandTag.RowsAffected() != 1 {
			_ = transaction.Rollback(context.Background())
			t.Fatalf("conversation CAS rows = %d, error = %v", commandTag.RowsAffected(), err)
		}
		if err := transaction.Commit(ctx); err != nil {
			t.Fatalf("commit conversation CAS: %v", err)
		}

		assertConversationTransactionFails(t, connection, "orphan conversation head", "23503", func(tx pgx.Tx) error {
			if _, err := setTenantContext(t.Context(), tx, "ten_alpha"); err != nil {
				return err
			}
			_, err := tx.Exec(t.Context(), `
INSERT INTO wanwork_im.conversation_heads (
    tenant_id, conversation_id, conversation_type, current_revision
) VALUES ('ten_alpha', 'cnv_orphan', 'group', 1)`)
			return err
		})
		assertConversationTransactionFails(t, connection, "Agent thread is not persisted yet", "23514", func(tx pgx.Tx) error {
			if _, err := setTenantContext(t.Context(), tx, "ten_alpha"); err != nil {
				return err
			}
			_, err := tx.Exec(t.Context(), `
INSERT INTO wanwork_im.conversation_heads (
    tenant_id, conversation_id, conversation_type, current_revision
) VALUES ('ten_alpha', 'cnv_thread', 'agent_thread', 1)`)
			return err
		})
		assertConversationTransactionFails(t, connection, "conversation type mutation", "23503", func(tx pgx.Tx) error {
			if _, err := setTenantContext(t.Context(), tx, "ten_alpha"); err != nil {
				return err
			}
			_, err := tx.Exec(t.Context(), `
INSERT INTO wanwork_im.conversation_snapshots (
    tenant_id, conversation_id, revision, workspace_id, conversation_type, status
) VALUES (
    'ten_alpha', 'cnv_alpha_group', 3, 'wsp_alpha', 'direct', 'active'
)`)
			return err
		})
		assertConversationTransactionFails(t, connection, "cross tenant workspace", "23503", func(tx pgx.Tx) error {
			if _, err := setTenantContext(t.Context(), tx, "ten_beta"); err != nil {
				return err
			}
			if _, err := tx.Exec(t.Context(), `
INSERT INTO wanwork_im.conversation_heads (
    tenant_id, conversation_id, conversation_type, current_revision
) VALUES ('ten_beta', 'cnv_cross_workspace', 'group', 1)`); err != nil {
				return err
			}
			_, err := tx.Exec(t.Context(), `
INSERT INTO wanwork_im.conversation_snapshots (
    tenant_id, conversation_id, revision, workspace_id, conversation_type, status
) VALUES (
    'ten_beta', 'cnv_cross_workspace', 1, 'wsp_alpha', 'group', 'active'
)`)
			return err
		})
		assertConversationTransactionFails(t, connection, "provider binding cannot target direct", "23503", func(tx pgx.Tx) error {
			if _, err := setTenantContext(t.Context(), tx, "ten_alpha"); err != nil {
				return err
			}
			_, err := tx.Exec(t.Context(), `
INSERT INTO wanwork_im.provider_conversation_binding_heads (
    tenant_id, provider, realm_id, provider_conversation_id,
    current_revision, current_conversation_id, current_conversation_type, current_status
) VALUES (
    'ten_alpha', 'rongcloud', 'rlm_rong', 'cnv_alpha_direct',
    1, 'cnv_alpha_direct', 'group', 'active'
)`)
			return err
		})
		assertConversationTransactionFails(t, connection, "provider target mismatch", "23514", func(tx pgx.Tx) error {
			if _, err := setTenantContext(t.Context(), tx, "ten_alpha"); err != nil {
				return err
			}
			_, err := tx.Exec(t.Context(), `
INSERT INTO wanwork_im.provider_conversation_binding_heads (
    tenant_id, provider, realm_id, provider_conversation_id,
    current_revision, current_conversation_id, current_conversation_type, current_status
) VALUES (
    'ten_alpha', 'rongcloud', 'rlm_rong', 'cnv_provider_other',
    1, 'cnv_alpha_group', 'group', 'active'
)`)
			return err
		})
		assertConversationTransactionFails(t, connection, "provider subject cannot cross tenants", "23505", func(tx pgx.Tx) error {
			if _, err := setTenantContext(t.Context(), tx, "ten_beta"); err != nil {
				return err
			}
			if _, err := tx.Exec(t.Context(), `
INSERT INTO wanwork_im.conversation_heads (
    tenant_id, conversation_id, conversation_type, current_revision
) VALUES ('ten_beta', 'cnv_alpha_group', 'group', 1)`); err != nil {
				return err
			}
			if _, err := tx.Exec(t.Context(), `
INSERT INTO wanwork_im.conversation_snapshots (
    tenant_id, conversation_id, revision, workspace_id, conversation_type, status
) VALUES (
    'ten_beta', 'cnv_alpha_group', 1, 'wsp_beta', 'group', 'active'
)`); err != nil {
				return err
			}
			_, err := tx.Exec(t.Context(), `
INSERT INTO wanwork_im.provider_conversation_binding_heads (
    tenant_id, provider, realm_id, provider_conversation_id,
    current_revision, current_conversation_id, current_conversation_type, current_status
) VALUES (
    'ten_beta', 'rongcloud', 'rlm_rong', 'cnv_alpha_group',
    1, 'cnv_alpha_group', 'group', 'active'
)`)
			return err
		})
	})

	t.Run("conversation RLS is fail closed for an unprivileged role", func(t *testing.T) {
		connection, _ := newIntegrationDatabase(t, adminURL)
		applyMigrationsForConversationTest(t, connection)
		seedIdentityAuthority(t, connection)
		seedConversations(t, connection)
		roleName := fmt.Sprintf(
			"wanwork_conversation_rls_%d_%d",
			os.Getpid(),
			identityRoleSequence.Add(1),
		)
		quotedRole := pgx.Identifier{roleName}.Sanitize()
		if _, err := connection.Exec(t.Context(),
			"CREATE ROLE "+quotedRole+" NOLOGIN NOSUPERUSER NOBYPASSRLS",
		); err != nil {
			t.Fatalf("create conversation RLS role: %v", err)
		}
		t.Cleanup(func() {
			_, _ = connection.Exec(context.Background(), "DROP OWNED BY "+quotedRole)
			_, _ = connection.Exec(context.Background(), "DROP ROLE "+quotedRole)
		})
		if _, err := connection.Exec(t.Context(),
			"GRANT USAGE ON SCHEMA wanwork_im TO "+quotedRole,
		); err != nil {
			t.Fatalf("grant conversation schema use: %v", err)
		}
		if _, err := connection.Exec(t.Context(),
			"GRANT SELECT, INSERT ON wanwork_im.conversation_heads TO "+quotedRole,
		); err != nil {
			t.Fatalf("grant conversation table access: %v", err)
		}

		assertConversationRLSCount(t, connection, quotedRole, "", 0)
		assertConversationRLSCount(t, connection, quotedRole, "ten_beta", 0)
		assertConversationRLSCount(t, connection, quotedRole, "ten_alpha", 2)

		transaction, err := connection.BeginTx(t.Context(), pgx.TxOptions{})
		if err != nil {
			t.Fatalf("begin wrong-tenant conversation write: %v", err)
		}
		if _, err := transaction.Exec(t.Context(), "SET LOCAL ROLE "+quotedRole); err != nil {
			_ = transaction.Rollback(context.Background())
			t.Fatalf("set conversation RLS role: %v", err)
		}
		if _, err := setTenantContext(t.Context(), transaction, "ten_beta"); err != nil {
			_ = transaction.Rollback(context.Background())
			t.Fatalf("set wrong conversation tenant: %v", err)
		}
		_, err = transaction.Exec(t.Context(), `
INSERT INTO wanwork_im.conversation_heads (
    tenant_id, conversation_id, conversation_type, current_revision
) VALUES ('ten_alpha', 'cnv_wrong_scope', 'group', 1)`)
		if !hasPostgresCode(err, "42501") {
			_ = transaction.Rollback(context.Background())
			t.Fatalf("wrong-tenant conversation insert error = %v, want SQLSTATE 42501", err)
		}
		_ = transaction.Rollback(context.Background())
	})

	for _, fixture := range []struct {
		name      string
		tamperSQL string
	}{
		{
			name:      "disabled conversation RLS",
			tamperSQL: "ALTER TABLE wanwork_im.conversation_heads DISABLE ROW LEVEL SECURITY",
		},
		{
			name: "weakened conversation type constraint",
			tamperSQL: `
ALTER TABLE wanwork_im.conversation_heads
    DROP CONSTRAINT conversation_heads_type_check;
ALTER TABLE wanwork_im.conversation_heads
    ADD CONSTRAINT conversation_heads_type_check CHECK (conversation_type <> '')`,
		},
		{
			name:      "conversation public grant",
			tamperSQL: "GRANT SELECT ON wanwork_im.conversation_heads TO PUBLIC",
		},
		{
			name:      "conversation extra index",
			tamperSQL: "CREATE INDEX unexpected_conversation_index ON wanwork_im.conversation_heads (conversation_id)",
		},
		{
			name:      "conversation extra column",
			tamperSQL: "ALTER TABLE wanwork_im.conversation_heads ADD COLUMN unexpected text",
		},
		{
			name:      "missing provider subject uniqueness",
			tamperSQL: "DROP INDEX wanwork_im.provider_conversation_heads_active_subject_uk",
		},
	} {
		t.Run("conversation postcondition drift "+fixture.name, func(t *testing.T) {
			connection, _ := newIntegrationDatabase(t, adminURL)
			applyMigrationsForConversationTest(t, connection)
			if _, err := connection.Exec(
				t.Context(),
				fixture.tamperSQL,
				pgx.QueryExecModeSimpleProtocol,
			); err != nil {
				t.Fatalf("tamper conversation schema: %v", err)
			}
			if _, err := Apply(t.Context(), connection); !errors.Is(err, ErrMigrationSchema) {
				t.Fatalf("Apply conversation drift error = %v, want %v", err, ErrMigrationSchema)
			}
		})
	}
}

func applyMigrationsForConversationTest(t *testing.T, connection *pgx.Conn) {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), 15*time.Second)
	defer cancel()
	state, err := Apply(ctx, connection)
	if err != nil {
		t.Fatalf("Apply conversation catalog: %v", err)
	}
	if len(state.Applied) != 12 {
		t.Fatalf("applied migration count = %d, want 10", len(state.Applied))
	}
}

func seedConversations(t *testing.T, connection *pgx.Conn) {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), 15*time.Second)
	defer cancel()
	transaction, err := connection.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.Serializable})
	if err != nil {
		t.Fatalf("begin conversation seed: %v", err)
	}
	defer func() { _ = transaction.Rollback(context.Background()) }()
	if _, err := setTenantContext(ctx, transaction, "ten_alpha"); err != nil {
		t.Fatalf("set conversation seed tenant: %v", err)
	}
	for _, statement := range []string{
		`INSERT INTO wanwork_im.conversation_heads (
             tenant_id, conversation_id, conversation_type, current_revision
         ) VALUES ('ten_alpha', 'cnv_alpha_group', 'group', 1)`,
		`INSERT INTO wanwork_im.conversation_snapshots (
             tenant_id, conversation_id, revision, workspace_id, conversation_type, status
         ) VALUES (
             'ten_alpha', 'cnv_alpha_group', 1, 'wsp_alpha', 'group', 'active'
         )`,
		`INSERT INTO wanwork_im.conversation_heads (
             tenant_id, conversation_id, conversation_type, current_revision
         ) VALUES ('ten_alpha', 'cnv_alpha_direct', 'direct', 1)`,
		`INSERT INTO wanwork_im.conversation_snapshots (
             tenant_id, conversation_id, revision, workspace_id, conversation_type, status
         ) VALUES (
             'ten_alpha', 'cnv_alpha_direct', 1, NULL, 'direct', 'active'
         )`,
		`INSERT INTO wanwork_im.provider_conversation_binding_heads (
             tenant_id, provider, realm_id, provider_conversation_id,
             current_revision, current_conversation_id, current_conversation_type, current_status
         ) VALUES (
             'ten_alpha', 'rongcloud', 'rlm_rong', 'cnv_alpha_group',
             1, 'cnv_alpha_group', 'group', 'active'
         )`,
		`INSERT INTO wanwork_im.provider_conversation_binding_snapshots (
             tenant_id, provider, realm_id, provider_conversation_id,
             revision, conversation_id, conversation_type, status
         ) VALUES (
             'ten_alpha', 'rongcloud', 'rlm_rong', 'cnv_alpha_group',
             1, 'cnv_alpha_group', 'group', 'active'
         )`,
	} {
		if _, err := transaction.Exec(ctx, statement); err != nil {
			t.Fatalf("seed conversation authority: %v", err)
		}
	}
	if err := transaction.Commit(ctx); err != nil {
		t.Fatalf("commit conversation seed: %v", err)
	}
}

func assertConversationTransactionFails(
	t *testing.T,
	connection *pgx.Conn,
	name string,
	code string,
	operation func(pgx.Tx) error,
) {
	t.Helper()
	transaction, err := connection.BeginTx(t.Context(), pgx.TxOptions{IsoLevel: pgx.Serializable})
	if err != nil {
		t.Fatalf("%s begin: %v", name, err)
	}
	operationErr := operation(transaction)
	if operationErr == nil {
		operationErr = transaction.Commit(t.Context())
	} else {
		_ = transaction.Rollback(context.Background())
	}
	if !hasPostgresCode(operationErr, code) {
		t.Fatalf("%s error = %v, want SQLSTATE %s", name, operationErr, code)
	}
}

func assertConversationRLSCount(
	t *testing.T,
	connection *pgx.Conn,
	quotedRole string,
	tenantID string,
	want int,
) {
	t.Helper()
	transaction, err := connection.BeginTx(t.Context(), pgx.TxOptions{AccessMode: pgx.ReadOnly})
	if err != nil {
		t.Fatalf("begin conversation RLS read: %v", err)
	}
	defer func() { _ = transaction.Rollback(context.Background()) }()
	if _, err := transaction.Exec(t.Context(), "SET LOCAL ROLE "+quotedRole); err != nil {
		t.Fatalf("set conversation RLS role: %v", err)
	}
	if tenantID != "" {
		if _, err := setTenantContext(t.Context(), transaction, tenantID); err != nil {
			t.Fatalf("set conversation RLS tenant: %v", err)
		}
	}
	var count int
	if err := transaction.QueryRow(t.Context(), `
SELECT count(*)
FROM wanwork_im.conversation_heads`).Scan(&count); err != nil {
		t.Fatalf("query conversation RLS count: %v", err)
	}
	if count != want {
		t.Fatalf("conversation RLS count for %q = %d, want %d", tenantID, count, want)
	}
	if err := transaction.Commit(t.Context()); err != nil {
		t.Fatalf("commit conversation RLS read: %v", err)
	}
}
