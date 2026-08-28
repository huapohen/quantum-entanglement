package migrations

import (
	"context"
	"errors"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
)

func TestFunctionOnlyWritesAgainstPostgres(t *testing.T) {
	adminURL := os.Getenv(postgresIntegrationURL)
	if adminURL == "" {
		t.Skip(postgresIntegrationURL + " is not set")
	}

	t.Run("five functions enforce tenant and successor writes", func(t *testing.T) {
		connection, _ := newIntegrationDatabase(t, adminURL)
		applyMigrationsForConversationAuthorityTest(t, connection)
		seedIdentityAuthority(t, connection)

		assertFunctionWriteDeniedWithoutTenant(t, connection)
		assertFunctionWritesCommit(t, connection)
		assertFunctionRevisionConflictsWriteNothing(t, connection)
		assertFunctionWriteDeniedAcrossTenants(t, connection)
	})

	for _, fixture := range []struct {
		name      string
		tamperSQL string
	}{
		{
			name: "security invoker",
			tamperSQL: `ALTER FUNCTION wanwork_im.write_conversation_revision(
                text, text, bigint, bigint, text, text, text
            ) SECURITY INVOKER`,
		},
		{
			name: "stable volatility",
			tamperSQL: `ALTER FUNCTION wanwork_im.write_conversation_revision(
                text, text, bigint, bigint, text, text, text
            ) STABLE`,
		},
		{
			name: "unsafe search path",
			tamperSQL: `ALTER FUNCTION wanwork_im.write_conversation_revision(
                text, text, bigint, bigint, text, text, text
            ) SET search_path TO pg_catalog, public`,
		},
		{
			name: "public execute",
			tamperSQL: `GRANT EXECUTE ON FUNCTION wanwork_im.write_conversation_revision(
                text, text, bigint, bigint, text, text, text
            ) TO PUBLIC`,
		},
		{
			name: "extra overload",
			tamperSQL: `CREATE FUNCTION wanwork_im.write_conversation_revision(text)
                RETURNS boolean LANGUAGE sql AS 'SELECT true'`,
		},
	} {
		t.Run("postcondition rejects "+fixture.name, func(t *testing.T) {
			connection, _ := newIntegrationDatabase(t, adminURL)
			applyMigrationsForConversationAuthorityTest(t, connection)
			if _, err := connection.Exec(t.Context(), fixture.tamperSQL); err != nil {
				t.Fatalf("tamper function manifest: %v", err)
			}
			if _, err := Apply(t.Context(), connection); !errors.Is(err, ErrMigrationSchema) {
				t.Fatalf("Apply function drift error = %v, want %v", err, ErrMigrationSchema)
			}
			if connection.IsClosed() {
				t.Fatal("deterministic function drift must not quarantine the connection")
			}
		})
	}
}

func assertFunctionWriteDeniedWithoutTenant(t *testing.T, connection *pgx.Conn) {
	t.Helper()
	var written bool
	err := connection.QueryRow(t.Context(), `
SELECT wanwork_im.write_conversation_revision(
    'ten_alpha', 'cnv_function_unset', 0, 1, '', 'group', 'active'
)`).Scan(&written)
	if !hasPostgresCode(err, "42501") {
		t.Fatalf("unset tenant function error = %v, want SQLSTATE 42501", err)
	}
	assertFunctionConversationCount(t, connection, "ten_alpha", "cnv_function_unset", 0, 0)
}

func assertFunctionWritesCommit(t *testing.T, connection *pgx.Conn) {
	t.Helper()
	transaction, err := connection.BeginTx(t.Context(), pgx.TxOptions{IsoLevel: pgx.Serializable})
	if err != nil {
		t.Fatalf("begin function writes: %v", err)
	}
	defer func() { _ = transaction.Rollback(context.Background()) }()
	if _, err := setTenantContext(t.Context(), transaction, "ten_alpha"); err != nil {
		t.Fatalf("set function tenant: %v", err)
	}
	for _, fixture := range []struct {
		name      string
		statement string
	}{
		{name: "conversation", statement: `SELECT wanwork_im.write_conversation_revision(
            'ten_alpha', 'cnv_function_group', 0, 1, '', 'group', 'active'
        )`},
		{name: "provider binding", statement: `SELECT wanwork_im.write_provider_conversation_binding_revision(
            'ten_alpha', 'rongcloud', 'rlm_rong', 'cnv_function_group',
            0, 1, 'cnv_function_group', 'active'
        )`},
		{name: "membership", statement: `SELECT wanwork_im.write_conversation_membership_revision(
            'ten_alpha', 'cnv_function_group', 'usr_alice', 0, 1, 'owner', 'active'
        )`},
		{name: "access", statement: `SELECT wanwork_im.write_conversation_access_revision(
            'ten_alpha', 'cnv_function_group', 'usr_alice', 0, 1,
            true, true, true, true, false, false
        )`},
	} {
		var written bool
		if err := transaction.QueryRow(t.Context(), fixture.statement).Scan(&written); err != nil || !written {
			t.Fatalf("%s function written=%v error=%v", fixture.name, written, err)
		}
	}
	var committedAt time.Time
	if err := transaction.QueryRow(t.Context(), `
SELECT wanwork_im.write_tenant_command_receipt(
    'ten_alpha', 'function.write', 'function-write-1', $1, $2
)`, strings.Repeat("a", 64), strings.Repeat("b", 64)).Scan(&committedAt); err != nil || committedAt.IsZero() {
		t.Fatalf("receipt function committed_at=%v error=%v", committedAt, err)
	}
	if err := transaction.Commit(t.Context()); err != nil {
		t.Fatalf("commit function writes: %v", err)
	}

	assertFunctionConversationCount(t, connection, "ten_alpha", "cnv_function_group", 1, 1)
	var nilWorkspace bool
	if err := connection.QueryRow(t.Context(), `
SELECT workspace_id IS NULL
FROM wanwork_im.conversation_snapshots
WHERE tenant_id = 'ten_alpha'
  AND conversation_id = 'cnv_function_group'
  AND revision = 1`).Scan(&nilWorkspace); err != nil || !nilWorkspace {
		t.Fatalf("function workspace sentinel produced NULL=%v error=%v", nilWorkspace, err)
	}
	for table := range map[string]struct{}{
		"provider_conversation_binding_snapshots": {},
		"conversation_membership_snapshots":       {},
		"conversation_access_snapshots":           {},
		"tenant_command_receipts":                 {},
	} {
		var rows int
		if err := connection.QueryRow(
			t.Context(),
			"SELECT count(*) FROM wanwork_im."+pgx.Identifier{table}.Sanitize()+" WHERE tenant_id = 'ten_alpha'",
		).Scan(&rows); err != nil || rows != 1 {
			t.Fatalf("%s rows=%d error=%v", table, rows, err)
		}
	}
}

func assertFunctionRevisionConflictsWriteNothing(t *testing.T, connection *pgx.Conn) {
	t.Helper()
	transaction, err := connection.BeginTx(t.Context(), pgx.TxOptions{IsoLevel: pgx.Serializable})
	if err != nil {
		t.Fatalf("begin function conflict: %v", err)
	}
	defer func() { _ = transaction.Rollback(context.Background()) }()
	if _, err := setTenantContext(t.Context(), transaction, "ten_alpha"); err != nil {
		t.Fatalf("set function conflict tenant: %v", err)
	}
	for name, statement := range map[string]string{
		"duplicate": `SELECT wanwork_im.write_conversation_revision(
            'ten_alpha', 'cnv_function_group', 0, 1, '', 'group', 'active'
        )`,
		"skipped": `SELECT wanwork_im.write_conversation_revision(
            'ten_alpha', 'cnv_function_group', 1, 3, '', 'group', 'archived'
        )`,
		"stale": `SELECT wanwork_im.write_conversation_revision(
            'ten_alpha', 'cnv_function_group', 2, 3, '', 'group', 'archived'
        )`,
	} {
		var written bool
		if err := transaction.QueryRow(t.Context(), statement).Scan(&written); err != nil || written {
			t.Fatalf("%s conflict written=%v error=%v", name, written, err)
		}
	}
	if err := transaction.Commit(t.Context()); err != nil {
		t.Fatalf("commit function conflicts: %v", err)
	}
	assertFunctionConversationCount(t, connection, "ten_alpha", "cnv_function_group", 1, 1)
}

func assertFunctionWriteDeniedAcrossTenants(t *testing.T, connection *pgx.Conn) {
	t.Helper()
	transaction, err := connection.BeginTx(t.Context(), pgx.TxOptions{IsoLevel: pgx.Serializable})
	if err != nil {
		t.Fatalf("begin cross-tenant function: %v", err)
	}
	defer func() { _ = transaction.Rollback(context.Background()) }()
	if _, err := setTenantContext(t.Context(), transaction, "ten_alpha"); err != nil {
		t.Fatalf("set cross-tenant function context: %v", err)
	}
	var written bool
	err = transaction.QueryRow(t.Context(), `
SELECT wanwork_im.write_conversation_revision(
    'ten_beta', 'cnv_function_cross', 0, 1, '', 'group', 'active'
)`).Scan(&written)
	if !hasPostgresCode(err, "42501") {
		t.Fatalf("cross-tenant function error = %v, want SQLSTATE 42501", err)
	}
	_ = transaction.Rollback(context.Background())
	assertFunctionConversationCount(t, connection, "ten_beta", "cnv_function_cross", 0, 0)
}

func assertFunctionConversationCount(
	t *testing.T,
	connection *pgx.Conn,
	tenantID string,
	conversationID string,
	wantHeads int,
	wantSnapshots int,
) {
	t.Helper()
	var heads, snapshots int
	if err := connection.QueryRow(t.Context(), `
SELECT (SELECT count(*) FROM wanwork_im.conversation_heads
        WHERE tenant_id = $1 AND conversation_id = $2),
       (SELECT count(*) FROM wanwork_im.conversation_snapshots
        WHERE tenant_id = $1 AND conversation_id = $2)`, tenantID, conversationID).Scan(
		&heads,
		&snapshots,
	); err != nil || heads != wantHeads || snapshots != wantSnapshots {
		t.Fatalf(
			"conversation %s/%s rows=%d/%d want=%d/%d error=%v",
			tenantID,
			conversationID,
			heads,
			snapshots,
			wantHeads,
			wantSnapshots,
			err,
		)
	}
}
