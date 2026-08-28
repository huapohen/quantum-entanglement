package migrations

import (
	"context"
	"errors"
	"fmt"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
)

func TestConversationAuthorityAgainstPostgres(t *testing.T) {
	adminURL := os.Getenv(postgresIntegrationURL)
	if adminURL == "" {
		t.Skip(postgresIntegrationURL + " is not set")
	}

	t.Run("membership access revocation and receipts are separate authority", func(t *testing.T) {
		connection, _ := newIntegrationDatabase(t, adminURL)
		applyMigrationsForConversationAuthorityTest(t, connection)
		seedIdentityAuthority(t, connection)
		seedConversations(t, connection)
		seedConversationAuthority(t, connection)

		ctx, cancel := context.WithTimeout(t.Context(), 15*time.Second)
		defer cancel()
		var role, membershipStatus string
		var canRead, canSend, canManageMembers, canManageConversation bool
		var canInvokeAgent, canPublishArtifact bool
		if err := connection.QueryRow(ctx, `
SELECT membership.role,
       membership.status,
       access.can_read,
       access.can_send_message,
       access.can_manage_members,
       access.can_manage_conversation,
       access.can_invoke_agent,
       access.can_publish_artifact_reference
FROM wanwork_im.conversation_membership_heads AS membership_head
JOIN wanwork_im.conversation_membership_snapshots AS membership
  ON membership.tenant_id = membership_head.tenant_id
 AND membership.conversation_id = membership_head.conversation_id
 AND membership.actor_id = membership_head.actor_id
 AND membership.revision = membership_head.current_revision
JOIN wanwork_im.conversation_access_heads AS access_head
  ON access_head.tenant_id = membership_head.tenant_id
 AND access_head.conversation_id = membership_head.conversation_id
 AND access_head.actor_id = membership_head.actor_id
JOIN wanwork_im.conversation_access_snapshots AS access
  ON access.tenant_id = access_head.tenant_id
 AND access.conversation_id = access_head.conversation_id
 AND access.actor_id = access_head.actor_id
 AND access.revision = access_head.current_revision
WHERE membership_head.tenant_id = 'ten_alpha'
  AND membership_head.conversation_id = 'cnv_alpha_group'
  AND membership_head.actor_id = 'usr_alice'`).Scan(
			&role,
			&membershipStatus,
			&canRead,
			&canSend,
			&canManageMembers,
			&canManageConversation,
			&canInvokeAgent,
			&canPublishArtifact,
		); err != nil {
			t.Fatalf("read conversation authority: %v", err)
		}
		if role != "owner" || membershipStatus != "active" || !canRead || !canSend ||
			!canManageMembers || !canManageConversation || canInvokeAgent || canPublishArtifact {
			t.Fatalf(
				"unexpected initial authority: role=%s status=%s permissions=%v/%v/%v/%v/%v/%v",
				role,
				membershipStatus,
				canRead,
				canSend,
				canManageMembers,
				canManageConversation,
				canInvokeAgent,
				canPublishArtifact,
			)
		}

		transaction, err := connection.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.Serializable})
		if err != nil {
			t.Fatalf("begin conversation authority revoke: %v", err)
		}
		if _, err := setTenantContext(ctx, transaction, "ten_alpha"); err != nil {
			_ = transaction.Rollback(context.Background())
			t.Fatalf("set authority tenant: %v", err)
		}
		for _, statement := range []string{
			`INSERT INTO wanwork_im.conversation_membership_snapshots (
                 tenant_id, conversation_id, actor_id, revision, role, status
             ) VALUES (
                 'ten_alpha', 'cnv_alpha_group', 'usr_alice', 2, 'owner', 'removed'
             )`,
			`INSERT INTO wanwork_im.conversation_access_snapshots (
                 tenant_id, conversation_id, actor_id, revision,
                 can_read, can_send_message, can_manage_members,
                 can_manage_conversation, can_invoke_agent,
                 can_publish_artifact_reference
             ) VALUES (
                 'ten_alpha', 'cnv_alpha_group', 'usr_alice', 2,
                 false, false, false, false, false, false
             )`,
		} {
			if _, err := transaction.Exec(ctx, statement); err != nil {
				_ = transaction.Rollback(context.Background())
				t.Fatalf("append authority revocation: %v", err)
			}
		}
		for _, statement := range []string{
			`UPDATE wanwork_im.conversation_membership_heads
             SET current_revision = 2
             WHERE tenant_id = 'ten_alpha'
               AND conversation_id = 'cnv_alpha_group'
               AND actor_id = 'usr_alice'
               AND current_revision = 1`,
			`UPDATE wanwork_im.conversation_access_heads
             SET current_revision = 2
             WHERE tenant_id = 'ten_alpha'
               AND conversation_id = 'cnv_alpha_group'
               AND actor_id = 'usr_alice'
               AND current_revision = 1`,
		} {
			commandTag, err := transaction.Exec(ctx, statement)
			if err != nil || commandTag.RowsAffected() != 1 {
				_ = transaction.Rollback(context.Background())
				t.Fatalf("authority CAS rows = %d, error = %v", commandTag.RowsAffected(), err)
			}
		}
		if err := transaction.Commit(ctx); err != nil {
			t.Fatalf("commit authority revocation: %v", err)
		}

		var activePermissions int
		if err := connection.QueryRow(ctx, `
SELECT (
    can_read::int
    + can_send_message::int
    + can_manage_members::int
    + can_manage_conversation::int
    + can_invoke_agent::int
    + can_publish_artifact_reference::int
)
FROM wanwork_im.conversation_access_heads AS access_head
JOIN wanwork_im.conversation_access_snapshots AS access
  ON access.tenant_id = access_head.tenant_id
 AND access.conversation_id = access_head.conversation_id
 AND access.actor_id = access_head.actor_id
 AND access.revision = access_head.current_revision
WHERE access_head.tenant_id = 'ten_alpha'
  AND access_head.conversation_id = 'cnv_alpha_group'
  AND access_head.actor_id = 'usr_alice'`).Scan(&activePermissions); err != nil || activePermissions != 0 {
			t.Fatalf("active permission count = %d, error = %v", activePermissions, err)
		}

		var requestDigest, resultDigest string
		if err := connection.QueryRow(ctx, `
SELECT request_sha256, result_sha256
FROM wanwork_im.tenant_command_receipts
WHERE tenant_id = 'ten_alpha'
  AND command_kind = 'conversation.authority.create'
  AND idempotency_key = 'create-alpha-authority'`).Scan(
			&requestDigest,
			&resultDigest,
		); err != nil {
			t.Fatalf("read command receipt: %v", err)
		}
		if requestDigest != strings.Repeat("a", 64) || resultDigest != strings.Repeat("b", 64) {
			t.Fatalf("unexpected command receipt digests: %q %q", requestDigest, resultDigest)
		}

		assertConversationAuthorityTransactionFails(t, connection, "orphan membership head", "23503", func(tx pgx.Tx) error {
			if _, err := setTenantContext(t.Context(), tx, "ten_alpha"); err != nil {
				return err
			}
			_, err := tx.Exec(t.Context(), `
INSERT INTO wanwork_im.conversation_membership_heads (
    tenant_id, conversation_id, actor_id, current_revision
) VALUES ('ten_alpha', 'cnv_alpha_group', 'agt_orphan', 1)`)
			return err
		})
		for _, actorID := range []string{"sys_scheduler", "svc_provider"} {
			assertConversationAuthorityTransactionFails(t, connection, "non-participant "+actorID, "23514", func(tx pgx.Tx) error {
				if _, err := setTenantContext(t.Context(), tx, "ten_alpha"); err != nil {
					return err
				}
				_, err := tx.Exec(t.Context(), `
INSERT INTO wanwork_im.conversation_membership_heads (
    tenant_id, conversation_id, actor_id, current_revision
) VALUES ('ten_alpha', 'cnv_alpha_group', $1, 1)`, actorID)
				return err
			})
		}
		assertConversationAuthorityTransactionFails(t, connection, "access without membership", "23503", func(tx pgx.Tx) error {
			if _, err := setTenantContext(t.Context(), tx, "ten_alpha"); err != nil {
				return err
			}
			_, err := tx.Exec(t.Context(), `
INSERT INTO wanwork_im.conversation_access_heads (
    tenant_id, conversation_id, actor_id, current_revision
) VALUES ('ten_alpha', 'cnv_alpha_group', 'agt_missing', 1)`)
			return err
		})
		assertConversationAuthorityTransactionFails(t, connection, "invalid membership role", "23514", func(tx pgx.Tx) error {
			if _, err := setTenantContext(t.Context(), tx, "ten_alpha"); err != nil {
				return err
			}
			_, err := tx.Exec(t.Context(), `
INSERT INTO wanwork_im.conversation_membership_snapshots (
    tenant_id, conversation_id, actor_id, revision, role, status
) VALUES (
    'ten_alpha', 'cnv_alpha_group', 'usr_alice', 3, 'admin', 'active'
)`)
			return err
		})
		assertConversationAuthorityTransactionFails(t, connection, "invalid receipt digest", "23514", func(tx pgx.Tx) error {
			if _, err := setTenantContext(t.Context(), tx, "ten_alpha"); err != nil {
				return err
			}
			_, err := tx.Exec(t.Context(), `
INSERT INTO wanwork_im.tenant_command_receipts (
    tenant_id, command_kind, idempotency_key, request_sha256, result_sha256
) VALUES (
    'ten_alpha', 'conversation.authority.create', 'bad-digest', 'ABC', $1
)`, strings.Repeat("b", 64))
			return err
		})
		assertConversationAuthorityTransactionFails(t, connection, "idempotency key reuse", "23505", func(tx pgx.Tx) error {
			if _, err := setTenantContext(t.Context(), tx, "ten_alpha"); err != nil {
				return err
			}
			_, err := tx.Exec(t.Context(), `
INSERT INTO wanwork_im.tenant_command_receipts (
    tenant_id, command_kind, idempotency_key, request_sha256, result_sha256
) VALUES (
    'ten_alpha', 'conversation.authority.create', 'create-alpha-authority', $1, $2
)`, strings.Repeat("c", 64), strings.Repeat("d", 64))
			return err
		})
	})

	t.Run("conversation authority RLS is fail closed", func(t *testing.T) {
		connection, _ := newIntegrationDatabase(t, adminURL)
		applyMigrationsForConversationAuthorityTest(t, connection)
		seedIdentityAuthority(t, connection)
		seedConversations(t, connection)
		seedConversationAuthority(t, connection)
		roleName := fmt.Sprintf(
			"wanwork_authority_rls_%d_%d",
			os.Getpid(),
			identityRoleSequence.Add(1),
		)
		quotedRole := pgx.Identifier{roleName}.Sanitize()
		if _, err := connection.Exec(t.Context(),
			"CREATE ROLE "+quotedRole+" NOLOGIN NOSUPERUSER NOBYPASSRLS",
		); err != nil {
			t.Fatalf("create authority RLS role: %v", err)
		}
		t.Cleanup(func() {
			_, _ = connection.Exec(context.Background(), "DROP OWNED BY "+quotedRole)
			_, _ = connection.Exec(context.Background(), "DROP ROLE "+quotedRole)
		})
		if _, err := connection.Exec(t.Context(),
			"GRANT USAGE ON SCHEMA wanwork_im TO "+quotedRole,
		); err != nil {
			t.Fatalf("grant authority schema use: %v", err)
		}
		if _, err := connection.Exec(t.Context(),
			"GRANT SELECT ON wanwork_im.conversation_membership_heads TO "+quotedRole,
		); err != nil {
			t.Fatalf("grant authority reads: %v", err)
		}
		if _, err := connection.Exec(t.Context(),
			"GRANT INSERT ON wanwork_im.tenant_command_receipts TO "+quotedRole,
		); err != nil {
			t.Fatalf("grant receipt writes: %v", err)
		}

		assertConversationAuthorityRLSCount(t, connection, quotedRole, "", 0)
		assertConversationAuthorityRLSCount(t, connection, quotedRole, "ten_beta", 0)
		assertConversationAuthorityRLSCount(t, connection, quotedRole, "ten_alpha", 1)

		transaction, err := connection.BeginTx(t.Context(), pgx.TxOptions{})
		if err != nil {
			t.Fatalf("begin wrong-tenant receipt write: %v", err)
		}
		if _, err := transaction.Exec(t.Context(), "SET LOCAL ROLE "+quotedRole); err != nil {
			_ = transaction.Rollback(context.Background())
			t.Fatalf("set authority RLS role: %v", err)
		}
		if _, err := setTenantContext(t.Context(), transaction, "ten_beta"); err != nil {
			_ = transaction.Rollback(context.Background())
			t.Fatalf("set wrong receipt tenant: %v", err)
		}
		_, err = transaction.Exec(t.Context(), `
INSERT INTO wanwork_im.tenant_command_receipts (
    tenant_id, command_kind, idempotency_key, request_sha256, result_sha256
) VALUES ('ten_alpha', 'authority.test', 'wrong-scope', $1, $2)`,
			strings.Repeat("1", 64),
			strings.Repeat("2", 64),
		)
		if !hasPostgresCode(err, "42501") {
			_ = transaction.Rollback(context.Background())
			t.Fatalf("wrong-tenant receipt insert error = %v, want SQLSTATE 42501", err)
		}
		_ = transaction.Rollback(context.Background())
	})

	for _, fixture := range []struct {
		name      string
		tamperSQL string
	}{
		{
			name:      "disabled access RLS",
			tamperSQL: "ALTER TABLE wanwork_im.conversation_access_heads DISABLE ROW LEVEL SECURITY",
		},
		{
			name: "weakened participant constraint",
			tamperSQL: `
ALTER TABLE wanwork_im.conversation_membership_heads
    DROP CONSTRAINT conversation_membership_heads_actor_id_check;
ALTER TABLE wanwork_im.conversation_membership_heads
    ADD CONSTRAINT conversation_membership_heads_actor_id_check CHECK (actor_id <> '')`,
		},
		{
			name:      "authority public grant",
			tamperSQL: "GRANT SELECT ON wanwork_im.conversation_access_snapshots TO PUBLIC",
		},
		{
			name:      "authority extra index",
			tamperSQL: "CREATE INDEX unexpected_membership_index ON wanwork_im.conversation_membership_heads (actor_id)",
		},
		{
			name:      "authority extra column",
			tamperSQL: "ALTER TABLE wanwork_im.conversation_access_snapshots ADD COLUMN unexpected boolean",
		},
		{
			name: "weakened receipt digest",
			tamperSQL: `
ALTER TABLE wanwork_im.tenant_command_receipts
    DROP CONSTRAINT tenant_command_receipts_request_sha256_check;
ALTER TABLE wanwork_im.tenant_command_receipts
    ADD CONSTRAINT tenant_command_receipts_request_sha256_check CHECK (request_sha256 <> '')`,
		},
	} {
		t.Run("conversation authority postcondition drift "+fixture.name, func(t *testing.T) {
			connection, _ := newIntegrationDatabase(t, adminURL)
			applyMigrationsForConversationAuthorityTest(t, connection)
			if _, err := connection.Exec(
				t.Context(),
				fixture.tamperSQL,
				pgx.QueryExecModeSimpleProtocol,
			); err != nil {
				t.Fatalf("tamper conversation authority schema: %v", err)
			}
			if _, err := Apply(t.Context(), connection); !errors.Is(err, ErrMigrationSchema) {
				t.Fatalf("Apply conversation authority drift error = %v, want %v", err, ErrMigrationSchema)
			}
		})
	}
}

func applyMigrationsForConversationAuthorityTest(t *testing.T, connection *pgx.Conn) {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), 15*time.Second)
	defer cancel()
	state, err := Apply(ctx, connection)
	if err != nil {
		t.Fatalf("Apply conversation authority catalog: %v", err)
	}
	if len(state.Applied) != 5 {
		t.Fatalf("applied migration count = %d, want 5", len(state.Applied))
	}
}

func seedConversationAuthority(t *testing.T, connection *pgx.Conn) {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), 15*time.Second)
	defer cancel()
	transaction, err := connection.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.Serializable})
	if err != nil {
		t.Fatalf("begin conversation authority seed: %v", err)
	}
	defer func() { _ = transaction.Rollback(context.Background()) }()
	if _, err := setTenantContext(ctx, transaction, "ten_alpha"); err != nil {
		t.Fatalf("set conversation authority seed tenant: %v", err)
	}
	for _, statement := range []string{
		`INSERT INTO wanwork_im.conversation_membership_heads (
             tenant_id, conversation_id, actor_id, current_revision
         ) VALUES ('ten_alpha', 'cnv_alpha_group', 'usr_alice', 1)`,
		`INSERT INTO wanwork_im.conversation_membership_snapshots (
             tenant_id, conversation_id, actor_id, revision, role, status
         ) VALUES (
             'ten_alpha', 'cnv_alpha_group', 'usr_alice', 1, 'owner', 'active'
         )`,
		`INSERT INTO wanwork_im.conversation_access_heads (
             tenant_id, conversation_id, actor_id, current_revision
         ) VALUES ('ten_alpha', 'cnv_alpha_group', 'usr_alice', 1)`,
		`INSERT INTO wanwork_im.conversation_access_snapshots (
             tenant_id, conversation_id, actor_id, revision,
             can_read, can_send_message, can_manage_members,
             can_manage_conversation, can_invoke_agent,
             can_publish_artifact_reference
         ) VALUES (
             'ten_alpha', 'cnv_alpha_group', 'usr_alice', 1,
             true, true, true, true, false, false
         )`,
	} {
		if _, err := transaction.Exec(ctx, statement); err != nil {
			t.Fatalf("seed conversation authority: %v", err)
		}
	}
	if _, err := transaction.Exec(ctx, `
INSERT INTO wanwork_im.tenant_command_receipts (
    tenant_id, command_kind, idempotency_key, request_sha256, result_sha256
) VALUES (
    'ten_alpha', 'conversation.authority.create', 'create-alpha-authority', $1, $2
)`, strings.Repeat("a", 64), strings.Repeat("b", 64)); err != nil {
		t.Fatalf("seed conversation authority receipt: %v", err)
	}
	if err := transaction.Commit(ctx); err != nil {
		t.Fatalf("commit conversation authority seed: %v", err)
	}
}

func assertConversationAuthorityTransactionFails(
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

func assertConversationAuthorityRLSCount(
	t *testing.T,
	connection *pgx.Conn,
	quotedRole string,
	tenantID string,
	want int,
) {
	t.Helper()
	transaction, err := connection.BeginTx(t.Context(), pgx.TxOptions{AccessMode: pgx.ReadOnly})
	if err != nil {
		t.Fatalf("begin conversation authority RLS read: %v", err)
	}
	defer func() { _ = transaction.Rollback(context.Background()) }()
	if _, err := transaction.Exec(t.Context(), "SET LOCAL ROLE "+quotedRole); err != nil {
		t.Fatalf("set conversation authority RLS role: %v", err)
	}
	if tenantID != "" {
		if _, err := setTenantContext(t.Context(), transaction, tenantID); err != nil {
			t.Fatalf("set conversation authority RLS tenant: %v", err)
		}
	}
	var count int
	if err := transaction.QueryRow(t.Context(), `
SELECT count(*)
FROM wanwork_im.conversation_membership_heads`).Scan(&count); err != nil {
		t.Fatalf("query conversation authority RLS count: %v", err)
	}
	if count != want {
		t.Fatalf("conversation authority RLS count for %q = %d, want %d", tenantID, count, want)
	}
	if err := transaction.Commit(t.Context()); err != nil {
		t.Fatalf("commit conversation authority RLS read: %v", err)
	}
}
