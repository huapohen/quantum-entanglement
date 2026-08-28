package migrations

import (
	"context"
	"errors"
	"fmt"
	"os"
	"sync/atomic"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

var identityRoleSequence atomic.Uint64

func TestIdentityAuthorityAgainstPostgres(t *testing.T) {
	adminURL := os.Getenv(postgresIntegrationURL)
	if adminURL == "" {
		t.Skip(postgresIntegrationURL + " is not set")
	}

	t.Run("deferred snapshots and authority boundaries", func(t *testing.T) {
		connection, _ := newIntegrationDatabase(t, adminURL)
		applyMigrationsForIdentityTest(t, connection)
		seedIdentityAuthority(t, connection)

		ctx, cancel := context.WithTimeout(t.Context(), 15*time.Second)
		defer cancel()
		var principals int
		if err := connection.QueryRow(ctx, `
SELECT count(*)
FROM wanwork_im.human_principal_heads`).Scan(&principals); err != nil || principals != 1 {
			t.Fatalf("principal count = %d, error = %v", principals, err)
		}

		transaction, err := connection.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.Serializable})
		if err != nil {
			t.Fatalf("begin principal CAS: %v", err)
		}
		if _, err := transaction.Exec(ctx, `
INSERT INTO wanwork_im.human_principal_snapshots (
    principal_id, revision, status
) VALUES ('hpr_alice', 2, 'suspended')`); err != nil {
			_ = transaction.Rollback(context.Background())
			t.Fatalf("insert principal revision: %v", err)
		}
		commandTag, err := transaction.Exec(ctx, `
UPDATE wanwork_im.human_principal_heads
SET current_revision = 2
WHERE principal_id = 'hpr_alice'
  AND current_revision = 1`)
		if err != nil || commandTag.RowsAffected() != 1 {
			_ = transaction.Rollback(context.Background())
			t.Fatalf("principal CAS rows = %d, error = %v", commandTag.RowsAffected(), err)
		}
		if err := transaction.Commit(ctx); err != nil {
			t.Fatalf("commit principal CAS: %v", err)
		}

		assertIdentityTransactionFails(t, connection, "orphan head", "23503", func(tx pgx.Tx) error {
			_, err := tx.Exec(t.Context(), `
INSERT INTO wanwork_im.human_principal_heads (principal_id, current_revision)
VALUES ('hpr_orphan', 1)`)
			return err
		})
		assertIdentityTransactionFails(t, connection, "duplicate active Clerk target", "23505", func(tx pgx.Tx) error {
			if _, err := tx.Exec(t.Context(), `
INSERT INTO wanwork_im.human_identity_binding_heads (
    provider, realm_id, subject_id, current_revision, current_principal_id, current_status
) VALUES ('clerk', 'rlm_clerk', 'user_alice_alias', 1, 'hpr_alice', 'active')`); err != nil {
				return err
			}
			_, err := tx.Exec(t.Context(), `
INSERT INTO wanwork_im.human_identity_binding_snapshots (
    provider, realm_id, subject_id, revision, principal_id, status
) VALUES ('clerk', 'rlm_clerk', 'user_alice_alias', 1, 'hpr_alice', 'active')`)
			return err
		})
		assertIdentityTransactionFails(t, connection, "actor prefix type drift", "23514", func(tx pgx.Tx) error {
			if _, err := setTenantContext(t.Context(), tx, "ten_alpha"); err != nil {
				return err
			}
			if _, err := tx.Exec(t.Context(), `
INSERT INTO wanwork_im.actor_heads (tenant_id, actor_id, current_revision)
VALUES ('ten_alpha', 'agt_wrong_type', 1)`); err != nil {
				return err
			}
			_, err := tx.Exec(t.Context(), `
INSERT INTO wanwork_im.actor_snapshots (
    tenant_id, actor_id, revision, subject_type, status
) VALUES ('ten_alpha', 'agt_wrong_type', 1, 'human', 'active')`)
			return err
		})
		assertIdentityTransactionFails(t, connection, "cross tenant actor membership", "23503", func(tx pgx.Tx) error {
			if _, err := setTenantContext(t.Context(), tx, "ten_beta"); err != nil {
				return err
			}
			_, err := tx.Exec(t.Context(), `
INSERT INTO wanwork_im.tenant_membership_heads (
    tenant_id, principal_id, actor_id, current_revision
) VALUES ('ten_beta', 'hpr_alice', 'usr_alice', 1)`)
			return err
		})
		assertIdentityTransactionFails(t, connection, "provider Actor mismatch", "23514", func(tx pgx.Tx) error {
			if _, err := setTenantContext(t.Context(), tx, "ten_alpha"); err != nil {
				return err
			}
			_, err := tx.Exec(t.Context(), `
INSERT INTO wanwork_im.provider_actor_binding_heads (
    tenant_id, provider, realm_id, provider_user_id,
    current_revision, current_actor_id, current_status
) VALUES (
    'ten_alpha', 'rongcloud', 'rlm_rong', 'usr_other',
    1, 'usr_alice', 'active'
)`)
			return err
		})
	})

	t.Run("RLS is fail closed for an unprivileged role", func(t *testing.T) {
		connection, _ := newIntegrationDatabase(t, adminURL)
		applyMigrationsForIdentityTest(t, connection)
		seedIdentityAuthority(t, connection)
		roleName := fmt.Sprintf(
			"wanwork_rls_%d_%d",
			os.Getpid(),
			identityRoleSequence.Add(1),
		)
		quotedRole := pgx.Identifier{roleName}.Sanitize()
		if _, err := connection.Exec(t.Context(),
			"CREATE ROLE "+quotedRole+" NOLOGIN NOSUPERUSER NOBYPASSRLS",
		); err != nil {
			t.Fatalf("create RLS role: %v", err)
		}
		t.Cleanup(func() {
			_, _ = connection.Exec(context.Background(), "DROP OWNED BY "+quotedRole)
			_, _ = connection.Exec(context.Background(), "DROP ROLE "+quotedRole)
		})
		if _, err := connection.Exec(t.Context(),
			"GRANT USAGE ON SCHEMA wanwork_im TO "+quotedRole,
		); err != nil {
			t.Fatalf("grant schema use: %v", err)
		}
		if _, err := connection.Exec(t.Context(),
			"GRANT SELECT ON wanwork_im.tenants, wanwork_im.workspaces TO "+quotedRole,
		); err != nil {
			t.Fatalf("grant RLS reads: %v", err)
		}
		if _, err := connection.Exec(t.Context(),
			"GRANT INSERT ON wanwork_im.workspaces TO "+quotedRole,
		); err != nil {
			t.Fatalf("grant RLS writes: %v", err)
		}

		assertRLSCount(t, connection, quotedRole, "", 0)
		assertRLSCount(t, connection, quotedRole, "ten_beta", 1)
		assertRLSCount(t, connection, quotedRole, "ten_alpha", 1)

		transaction, err := connection.BeginTx(t.Context(), pgx.TxOptions{})
		if err != nil {
			t.Fatalf("begin wrong-tenant write: %v", err)
		}
		if _, err := transaction.Exec(t.Context(), "SET LOCAL ROLE "+quotedRole); err != nil {
			_ = transaction.Rollback(context.Background())
			t.Fatalf("set RLS role: %v", err)
		}
		if _, err := setTenantContext(t.Context(), transaction, "ten_beta"); err != nil {
			_ = transaction.Rollback(context.Background())
			t.Fatalf("set wrong tenant: %v", err)
		}
		_, err = transaction.Exec(t.Context(), `
INSERT INTO wanwork_im.workspaces (
    tenant_id, workspace_id, status, revision
) VALUES ('ten_alpha', 'wsp_wrong_scope', 'active', 1)`)
		if !hasPostgresCode(err, "42501") {
			_ = transaction.Rollback(context.Background())
			t.Fatalf("wrong-tenant insert error = %v, want SQLSTATE 42501", err)
		}
		_ = transaction.Rollback(context.Background())
	})

	for _, fixture := range []struct {
		name      string
		tamperSQL string
	}{
		{
			name:      "disabled identity RLS",
			tamperSQL: "ALTER TABLE wanwork_im.actor_heads DISABLE ROW LEVEL SECURITY",
		},
		{
			name: "weakened Actor type constraint",
			tamperSQL: `
ALTER TABLE wanwork_im.actor_snapshots
    DROP CONSTRAINT actor_snapshots_type_check;
ALTER TABLE wanwork_im.actor_snapshots
    ADD CONSTRAINT actor_snapshots_type_check CHECK (subject_type <> '')`,
		},
		{
			name:      "identity public grant",
			tamperSQL: "GRANT SELECT ON wanwork_im.actor_heads TO PUBLIC",
		},
		{
			name:      "identity extra index",
			tamperSQL: "CREATE INDEX unexpected_actor_index ON wanwork_im.actor_heads (actor_id)",
		},
		{
			name:      "identity extra column",
			tamperSQL: "ALTER TABLE wanwork_im.actor_heads ADD COLUMN unexpected text",
		},
	} {
		t.Run("identity postcondition drift "+fixture.name, func(t *testing.T) {
			connection, _ := newIntegrationDatabase(t, adminURL)
			applyMigrationsForIdentityTest(t, connection)
			if _, err := connection.Exec(
				t.Context(),
				fixture.tamperSQL,
				pgx.QueryExecModeSimpleProtocol,
			); err != nil {
				t.Fatalf("tamper identity schema: %v", err)
			}
			if _, err := Apply(t.Context(), connection); !errors.Is(err, ErrMigrationSchema) {
				t.Fatalf("Apply identity drift error = %v, want %v", err, ErrMigrationSchema)
			}
		})
	}

	t.Run("DownSQL works only when explicitly invoked in a disposable database", func(t *testing.T) {
		connection, _ := newIntegrationDatabase(t, adminURL)
		applyMigrationsForIdentityTest(t, connection)
		catalog, err := Catalog()
		if err != nil {
			t.Fatalf("load catalog: %v", err)
		}
		transaction, err := connection.BeginTx(t.Context(), pgx.TxOptions{IsoLevel: pgx.Serializable})
		if err != nil {
			t.Fatalf("begin explicit down: %v", err)
		}
		defer func() { _ = transaction.Rollback(context.Background()) }()
		for index := len(catalog) - 1; index >= 0; index-- {
			if _, err := transaction.Exec(
				t.Context(),
				catalog[index].DownSQL,
				pgx.QueryExecModeSimpleProtocol,
			); err != nil {
				t.Fatalf("explicit DownSQL %d: %v", catalog[index].Version, err)
			}
		}
		if err := transaction.Commit(t.Context()); err != nil {
			t.Fatalf("commit explicit down: %v", err)
		}
		var schemaName *string
		if err := connection.QueryRow(
			t.Context(),
			"SELECT pg_catalog.to_regnamespace('wanwork_im')::text",
		).Scan(&schemaName); err != nil || schemaName != nil {
			t.Fatalf("wanwork_im schema after down = %v, error = %v", schemaName, err)
		}
	})
}

func applyMigrationsForIdentityTest(t *testing.T, connection *pgx.Conn) {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), 15*time.Second)
	defer cancel()
	state, err := Apply(ctx, connection)
	if err != nil {
		t.Fatalf("Apply identity catalog: %v", err)
	}
	if len(state.Applied) != 2 {
		t.Fatalf("applied migration count = %d, want 2", len(state.Applied))
	}
}

func seedIdentityAuthority(t *testing.T, connection *pgx.Conn) {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), 15*time.Second)
	defer cancel()
	transaction, err := connection.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.Serializable})
	if err != nil {
		t.Fatalf("begin identity seed: %v", err)
	}
	defer func() { _ = transaction.Rollback(context.Background()) }()
	seedSQL := []string{
		`INSERT INTO wanwork_im.provider_realms (provider, realm_id, status, revision)
         VALUES ('clerk', 'rlm_clerk', 'active', 1),
                ('rongcloud', 'rlm_rong', 'active', 1)`,
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
	}
	for _, statement := range seedSQL {
		if _, err := transaction.Exec(ctx, statement); err != nil {
			t.Fatalf("seed global identity: %v", err)
		}
	}
	if _, err := setTenantContext(ctx, transaction, "ten_alpha"); err != nil {
		t.Fatalf("set alpha tenant: %v", err)
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
		`INSERT INTO wanwork_im.tenant_membership_heads (
             tenant_id, principal_id, actor_id, current_revision
         ) VALUES ('ten_alpha', 'hpr_alice', 'usr_alice', 1)`,
		`INSERT INTO wanwork_im.tenant_membership_snapshots (
             tenant_id, principal_id, actor_id, revision, role, status
         ) VALUES ('ten_alpha', 'hpr_alice', 'usr_alice', 1, 'owner', 'active')`,
		`INSERT INTO wanwork_im.provider_actor_binding_heads (
             tenant_id, provider, realm_id, provider_user_id,
             current_revision, current_actor_id, current_status
         ) VALUES (
             'ten_alpha', 'rongcloud', 'rlm_rong', 'usr_alice',
             1, 'usr_alice', 'active'
         )`,
		`INSERT INTO wanwork_im.provider_actor_binding_snapshots (
             tenant_id, provider, realm_id, provider_user_id,
             revision, actor_id, status
         ) VALUES (
             'ten_alpha', 'rongcloud', 'rlm_rong', 'usr_alice',
             1, 'usr_alice', 'active'
         )`,
	} {
		if _, err := transaction.Exec(ctx, statement); err != nil {
			t.Fatalf("seed alpha identity: %v", err)
		}
	}
	if _, err := setTenantContext(ctx, transaction, "ten_beta"); err != nil {
		t.Fatalf("set beta tenant: %v", err)
	}
	for _, statement := range []string{
		`INSERT INTO wanwork_im.tenants (tenant_id, status, revision)
         VALUES ('ten_beta', 'active', 1)`,
		`INSERT INTO wanwork_im.workspaces (tenant_id, workspace_id, status, revision)
         VALUES ('ten_beta', 'wsp_beta', 'active', 1)`,
	} {
		if _, err := transaction.Exec(ctx, statement); err != nil {
			t.Fatalf("seed beta identity: %v", err)
		}
	}
	if err := transaction.Commit(ctx); err != nil {
		t.Fatalf("commit identity seed: %v", err)
	}
}

func assertIdentityTransactionFails(
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

func assertRLSCount(
	t *testing.T,
	connection *pgx.Conn,
	quotedRole string,
	tenantID string,
	want int,
) {
	t.Helper()
	transaction, err := connection.BeginTx(t.Context(), pgx.TxOptions{AccessMode: pgx.ReadOnly})
	if err != nil {
		t.Fatalf("begin RLS read: %v", err)
	}
	defer func() { _ = transaction.Rollback(context.Background()) }()
	if _, err := transaction.Exec(t.Context(), "SET LOCAL ROLE "+quotedRole); err != nil {
		t.Fatalf("set RLS role: %v", err)
	}
	if tenantID != "" {
		if _, err := setTenantContext(t.Context(), transaction, tenantID); err != nil {
			t.Fatalf("set RLS tenant: %v", err)
		}
	}
	var count int
	if err := transaction.QueryRow(t.Context(), `
SELECT count(*)
FROM wanwork_im.tenants`).Scan(&count); err != nil {
		t.Fatalf("query RLS count: %v", err)
	}
	if count != want {
		t.Fatalf("RLS count for %q = %d, want %d", tenantID, count, want)
	}
	if err := transaction.Commit(t.Context()); err != nil {
		t.Fatalf("commit RLS read: %v", err)
	}
}

func setTenantContext(ctx context.Context, transaction pgx.Tx, tenantID string) (string, error) {
	var recorded string
	err := transaction.QueryRow(
		ctx,
		"SELECT set_config('wanwork.tenant_id', $1, true)",
		tenantID,
	).Scan(&recorded)
	return recorded, err
}

func hasPostgresCode(err error, code string) bool {
	var postgresError *pgconn.PgError
	return errors.As(err, &postgresError) && postgresError.Code == code
}
