package runtimepool

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/url"
	"os"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/platform/postgres/migrations"
)

const runtimePoolIntegrationURL = "WANWORK_TEST_POSTGRES_ADMIN_URL"

var runtimePoolIntegrationSequence atomic.Uint64

func TestRuntimePoolAgainstPostgres(t *testing.T) {
	adminURL := os.Getenv(runtimePoolIntegrationURL)
	if adminURL == "" {
		t.Skip(runtimePoolIntegrationURL + " is not set")
	}
	admin, connectionString, manifest := provisionRuntimePoolFixture(t, adminURL)
	input := Config{
		ConnectionString:       connectionString,
		Manifest:               manifest,
		MaxConnections:         1,
		MinIdleConnections:     0,
		ConnectTimeout:         3 * time.Second,
		PingTimeout:            time.Second,
		AllowInsecureLocalhost: true,
	}
	pool, err := Open(t.Context(), input)
	if err != nil {
		t.Fatalf("open exact runtime pool: %v", err)
	}
	defer pool.Close()
	if err := pool.Ready(t.Context()); err != nil {
		t.Fatalf("initial exact readiness: %v", err)
	}
	assertRuntimeCannotWriteTables(t, pool)
	assertLocalTenantTransactionDoesNotPollute(t, pool)

	for name, contaminate := range map[string]func(*testing.T, *pgx.Conn){
		"reset role": func(t *testing.T, connection *pgx.Conn) {
			_, err := connection.Exec(t.Context(), "RESET ROLE")
			if err != nil {
				t.Fatalf("reset role: %v", err)
			}
		},
		"search path": func(t *testing.T, connection *pgx.Conn) {
			_, err := connection.Exec(t.Context(), "SET SESSION search_path = public")
			if err != nil {
				t.Fatalf("set search path: %v", err)
			}
		},
		"application name": func(t *testing.T, connection *pgx.Conn) {
			_, err := connection.Exec(t.Context(), "SET SESSION application_name = 'polluted'")
			if err != nil {
				t.Fatalf("set application name: %v", err)
			}
		},
		"tenant authority": func(t *testing.T, connection *pgx.Conn) {
			_, err := connection.Exec(t.Context(), "SET SESSION wanwork.tenant_id = 'tenant_pollution'")
			if err != nil {
				t.Fatalf("set tenant authority: %v", err)
			}
		},
		"session setting": func(t *testing.T, connection *pgx.Conn) {
			_, err := connection.Exec(t.Context(), "SET SESSION statement_timeout = '2s'")
			if err != nil {
				t.Fatalf("set statement timeout: %v", err)
			}
		},
		"advisory lock": func(t *testing.T, connection *pgx.Conn) {
			_, err := connection.Exec(t.Context(), "SELECT pg_catalog.pg_advisory_lock(987654321)")
			if err != nil {
				t.Fatalf("hold advisory lock: %v", err)
			}
		},
		"listener": func(t *testing.T, connection *pgx.Conn) {
			_, err := connection.Exec(t.Context(), "LISTEN wanwork_pool_pollution")
			if err != nil {
				t.Fatalf("listen: %v", err)
			}
		},
	} {
		t.Run("quarantines "+name, func(t *testing.T) {
			assertIdlePollutionIsQuarantined(t, pool, contaminate)
		})
	}

	t.Run("quarantines open transaction", func(t *testing.T) {
		connection, err := pool.Acquire(t.Context())
		if err != nil {
			t.Fatalf("acquire before transaction contamination: %v", err)
		}
		oldPID := backendPID(t, connection.Conn())
		if _, err := connection.Exec(t.Context(), "BEGIN"); err != nil {
			connection.Release()
			t.Fatalf("begin leaked transaction: %v", err)
		}
		connection.Release()
		replacement, err := pool.Acquire(t.Context())
		if err != nil {
			t.Fatalf("acquire after transaction quarantine: %v", err)
		}
		defer replacement.Release()
		if replacementPID := backendPID(t, replacement.Conn()); replacementPID == oldPID {
			t.Fatalf("transaction-polluted backend %d was reused", oldPID)
		}
		assertRuntimeSession(t, replacement.Conn(), manifest)
	})

	t.Run("fails closed on access drift and recovers after full revalidation", func(t *testing.T) {
		setAdminRole(t, admin, manifest.OwnerRole)
		functionIdentity := "write_tenant_command_receipt(text, text, text, text, text)"
		if _, err := admin.Exec(
			t.Context(),
			"REVOKE EXECUTE ON FUNCTION wanwork_im."+functionIdentity+" FROM "+
				pgx.Identifier{manifest.RuntimeRole}.Sanitize(),
		); err != nil {
			t.Fatalf("revoke runtime function access: %v", err)
		}
		resetAdminRole(t, admin)
		if err := pool.Ready(t.Context()); !errors.Is(err, ErrNotReady) {
			t.Fatalf("access drift readiness error = %v, want %v", err, ErrNotReady)
		}
		setAdminRole(t, admin, manifest.OwnerRole)
		if _, err := admin.Exec(
			t.Context(),
			"GRANT EXECUTE ON FUNCTION wanwork_im."+functionIdentity+" TO "+
				pgx.Identifier{manifest.RuntimeRole}.Sanitize(),
		); err != nil {
			t.Fatalf("repair runtime function access: %v", err)
		}
		resetAdminRole(t, admin)
		if err := pool.Ready(t.Context()); err != nil {
			t.Fatalf("readiness after repaired full validation: %v", err)
		}
	})

	t.Run("honors readiness cancellation and pool exhaustion", func(t *testing.T) {
		cancelled, cancel := context.WithCancel(t.Context())
		cancel()
		if err := pool.Ready(cancelled); !errors.Is(err, ErrNotReady) {
			t.Fatalf("cancelled readiness error = %v, want %v", err, ErrNotReady)
		}
		held, err := pool.Acquire(t.Context())
		if err != nil {
			t.Fatalf("hold sole runtime connection: %v", err)
		}
		deadline, stop := context.WithTimeout(t.Context(), 25*time.Millisecond)
		defer stop()
		if err := pool.Ready(deadline); !errors.Is(err, ErrNotReady) {
			held.Release()
			t.Fatalf("exhausted readiness error = %v, want %v", err, ErrNotReady)
		}
		held.Release()
	})
}

func assertIdlePollutionIsQuarantined(
	t *testing.T,
	pool *Pool,
	contaminate func(*testing.T, *pgx.Conn),
) {
	t.Helper()
	connection, err := pool.Acquire(t.Context())
	if err != nil {
		t.Fatalf("acquire before idle contamination: %v", err)
	}
	oldPID := backendPID(t, connection.Conn())
	contaminate(t, connection.Conn())
	connection.Release()
	if contaminated, err := pool.Acquire(t.Context()); !errors.Is(err, ErrNotReady) {
		if contaminated != nil {
			contaminated.Release()
		}
		t.Fatalf("polluted acquire error = %v, want %v", err, ErrNotReady)
	}
	replacement, err := pool.Acquire(t.Context())
	if err != nil {
		t.Fatalf("acquire freshly attested replacement: %v", err)
	}
	defer replacement.Release()
	if replacementPID := backendPID(t, replacement.Conn()); replacementPID == oldPID {
		t.Fatalf("idle-polluted backend %d was reused", oldPID)
	}
}

func assertLocalTenantTransactionDoesNotPollute(t *testing.T, pool *Pool) {
	t.Helper()
	connection, err := pool.Acquire(t.Context())
	if err != nil {
		t.Fatalf("acquire for local tenant transaction: %v", err)
	}
	oldPID := backendPID(t, connection.Conn())
	transaction, err := connection.Begin(t.Context())
	if err != nil {
		connection.Release()
		t.Fatalf("begin local tenant transaction: %v", err)
	}
	var recorded string
	if err := transaction.QueryRow(
		t.Context(),
		"SELECT pg_catalog.set_config('wanwork.tenant_id', 'tenant_canary', true)",
	).Scan(&recorded); err != nil || recorded != "tenant_canary" {
		_ = transaction.Rollback(context.Background())
		connection.Release()
		t.Fatalf("record local tenant value=%q error=%v", recorded, err)
	}
	if err := transaction.Commit(t.Context()); err != nil {
		connection.Release()
		t.Fatalf("commit local tenant transaction: %v", err)
	}
	connection.Release()
	reused, err := pool.Acquire(t.Context())
	if err != nil {
		t.Fatalf("normal tenant placeholder rejected: %v", err)
	}
	defer reused.Release()
	if reusedPID := backendPID(t, reused.Conn()); reusedPID != oldPID {
		t.Fatalf("normal local tenant transaction churned backend old=%d new=%d", oldPID, reusedPID)
	}
}

func assertRuntimeCannotWriteTables(t *testing.T, pool *Pool) {
	t.Helper()
	connection, err := pool.Acquire(t.Context())
	if err != nil {
		t.Fatalf("acquire runtime permission probe: %v", err)
	}
	defer connection.Release()
	_, err = connection.Exec(t.Context(), `
INSERT INTO wanwork_im.tenant_command_receipts (
    tenant_id, command_kind, idempotency_key, request_sha256, result_sha256
) VALUES ('tenant_probe', 'probe', 'probe', repeat('0', 64), repeat('0', 64))`)
	if !postgresCode(err, "42501") {
		t.Fatalf("raw runtime write error = %v, want SQLSTATE 42501", err)
	}
	var canMaintain bool
	if err := connection.QueryRow(
		t.Context(),
		"SELECT pg_catalog.has_table_privilege(current_user, 'wanwork_im.conversation_heads', 'MAINTAIN')",
	).Scan(&canMaintain); err != nil || canMaintain {
		t.Fatalf("runtime maintain privilege=%v error=%v", canMaintain, err)
	}
}

func assertRuntimeSession(
	t *testing.T,
	connection *pgx.Conn,
	manifest migrations.AuthorityAccessManifest,
) {
	t.Helper()
	var sessionUser, currentUser, databaseName, searchPath, applicationName string
	if err := connection.QueryRow(t.Context(), `
SELECT session_user, current_user, current_database(),
       current_setting('search_path'), current_setting('application_name')`).Scan(
		&sessionUser,
		&currentUser,
		&databaseName,
		&searchPath,
		&applicationName,
	); err != nil || sessionUser != manifest.RuntimeLoginRoles[0] ||
		currentUser != manifest.RuntimeRole || databaseName != manifest.DatabaseName ||
		searchPath != "pg_catalog" || applicationName != "wanwork-im-runtime" {
		t.Fatalf(
			"runtime session session=%q current=%q database=%q path=%q app=%q error=%v",
			sessionUser,
			currentUser,
			databaseName,
			searchPath,
			applicationName,
			err,
		)
	}
}

func backendPID(t *testing.T, connection *pgx.Conn) uint32 {
	t.Helper()
	return connection.PgConn().PID()
}

func provisionRuntimePoolFixture(
	t *testing.T,
	adminURL string,
) (*pgx.Conn, string, migrations.AuthorityAccessManifest) {
	t.Helper()
	adminConfig, err := pgx.ParseConfig(adminURL)
	if err != nil {
		t.Fatalf("parse %s: %v", runtimePoolIntegrationURL, err)
	}
	admin, err := pgx.ConnectConfig(t.Context(), adminConfig)
	if err != nil {
		t.Fatalf("connect integration admin: %v", err)
	}
	t.Cleanup(func() { _ = admin.Close(context.Background()) })
	suffix := fmt.Sprintf("%d_%d", os.Getpid(), runtimePoolIntegrationSequence.Add(1))
	databaseName := "wanwork_pool_" + suffix
	quotedDatabase := pgx.Identifier{databaseName}.Sanitize()
	if _, err := admin.Exec(t.Context(), "CREATE DATABASE "+quotedDatabase+" TEMPLATE template0"); err != nil {
		t.Fatalf("create runtime pool database: %v", err)
	}
	databaseConfig := adminConfig.Copy()
	databaseConfig.Database = databaseName
	databaseConnection, err := pgx.ConnectConfig(t.Context(), databaseConfig)
	if err != nil {
		t.Fatalf("connect runtime pool database: %v", err)
	}
	roles := make([]string, 0, 5)
	t.Cleanup(func() {
		_ = databaseConnection.Close(context.Background())
		_, _ = admin.Exec(
			context.Background(),
			"DROP DATABASE "+quotedDatabase+" WITH (FORCE)",
		)
		if len(roles) != 0 {
			quotedRoles := make([]string, 0, len(roles))
			for _, role := range roles {
				quotedRoles = append(quotedRoles, pgx.Identifier{role}.Sanitize())
			}
			_, _ = admin.Exec(context.Background(), "DROP ROLE "+strings.Join(quotedRoles, ", "))
		}
	})
	if _, err := migrations.Apply(t.Context(), databaseConnection); err != nil {
		t.Fatalf("apply runtime pool migrations: %v", err)
	}
	var databaseOwner string
	if err := databaseConnection.QueryRow(t.Context(), "SELECT current_user").Scan(&databaseOwner); err != nil {
		t.Fatalf("read integration database owner: %v", err)
	}
	manifest := migrations.AuthorityAccessManifest{
		DatabaseName:        databaseName,
		DatabaseOwnerRole:   databaseOwner,
		OwnerRole:           "wanwork_owner_" + suffix,
		MigratorRole:        "wanwork_migrator_" + suffix,
		RuntimeRole:         "wanwork_runtime_" + suffix,
		MigrationLoginRoles: []string{"wanwork_deploy_" + suffix},
		RuntimeLoginRoles:   []string{"wanwork_app_" + suffix},
	}
	roles = append(roles,
		manifest.OwnerRole,
		manifest.MigratorRole,
		manifest.RuntimeRole,
		manifest.MigrationLoginRoles[0],
		manifest.RuntimeLoginRoles[0],
	)
	for _, role := range roles {
		login := "NOLOGIN"
		if role == manifest.MigrationLoginRoles[0] || role == manifest.RuntimeLoginRoles[0] {
			login = "LOGIN"
		}
		if _, err := databaseConnection.Exec(
			t.Context(),
			"CREATE ROLE "+pgx.Identifier{role}.Sanitize()+" "+login+
				" NOSUPERUSER NOINHERIT NOCREATEROLE NOCREATEDB NOREPLICATION NOBYPASSRLS CONNECTION LIMIT -1",
		); err != nil {
			t.Fatalf("create runtime pool role %s: %v", role, err)
		}
	}
	grantRuntimePoolRole(t, databaseConnection, manifest.OwnerRole, manifest.MigratorRole)
	grantRuntimePoolRole(t, databaseConnection, manifest.MigratorRole, manifest.MigrationLoginRoles[0])
	grantRuntimePoolRole(t, databaseConnection, manifest.RuntimeRole, manifest.RuntimeLoginRoles[0])
	configureRuntimePoolAuthority(t, databaseConnection, manifest)
	return databaseConnection, runtimePoolConnectionString(t, databaseConfig, manifest), manifest
}

func configureRuntimePoolAuthority(
	t *testing.T,
	connection *pgx.Conn,
	manifest migrations.AuthorityAccessManifest,
) {
	t.Helper()
	quotedDatabase := pgx.Identifier{manifest.DatabaseName}.Sanitize()
	quotedOwner := pgx.Identifier{manifest.OwnerRole}.Sanitize()
	quotedRuntime := pgx.Identifier{manifest.RuntimeRole}.Sanitize()
	for _, statement := range []string{
		"REVOKE CONNECT, CREATE, TEMPORARY ON DATABASE " + quotedDatabase + " FROM PUBLIC",
		"GRANT CREATE ON DATABASE " + quotedDatabase + " TO " + quotedOwner,
		"GRANT CONNECT ON DATABASE " + quotedDatabase + " TO " +
			pgx.Identifier{manifest.MigrationLoginRoles[0]}.Sanitize(),
		"GRANT CONNECT ON DATABASE " + quotedDatabase + " TO " +
			pgx.Identifier{manifest.RuntimeLoginRoles[0]}.Sanitize(),
	} {
		if _, err := connection.Exec(t.Context(), statement); err != nil {
			t.Fatalf("configure runtime pool database access: %v", err)
		}
	}
	tables := runtimePoolRelations(t, connection)
	for _, table := range tables {
		if _, err := connection.Exec(
			t.Context(),
			"ALTER TABLE "+table+" OWNER TO "+quotedOwner,
		); err != nil {
			t.Fatalf("transfer runtime pool table %s: %v", table, err)
		}
	}
	functions := runtimePoolFunctions(t, connection)
	for _, function := range functions {
		if _, err := connection.Exec(
			t.Context(),
			"ALTER FUNCTION wanwork_im."+function+" OWNER TO "+quotedOwner,
		); err != nil {
			t.Fatalf("transfer runtime pool function %s: %v", function, err)
		}
	}
	for _, schema := range []string{"wanwork_meta", "wanwork_im"} {
		if _, err := connection.Exec(
			t.Context(),
			"ALTER SCHEMA "+pgx.Identifier{schema}.Sanitize()+" OWNER TO "+quotedOwner,
		); err != nil {
			t.Fatalf("transfer runtime pool schema %s: %v", schema, err)
		}
	}
	setAdminRole(t, connection, manifest.OwnerRole)
	defer resetAdminRole(t, connection)
	if _, err := connection.Exec(
		t.Context(),
		"ALTER DEFAULT PRIVILEGES FOR ROLE "+quotedOwner+" REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC",
	); err != nil {
		t.Fatalf("freeze runtime pool default function privileges: %v", err)
	}
	if _, err := connection.Exec(t.Context(), "GRANT USAGE ON SCHEMA wanwork_im TO "+quotedRuntime); err != nil {
		t.Fatalf("grant runtime pool schema usage: %v", err)
	}
	readTables := []string{
		"actor_heads",
		"actor_snapshots",
		"conversation_access_heads",
		"conversation_access_snapshots",
		"conversation_heads",
		"conversation_membership_heads",
		"conversation_membership_snapshots",
		"conversation_snapshots",
		"provider_conversation_binding_heads",
		"provider_conversation_binding_snapshots",
		"tenant_command_receipts",
		"event_stream_heads",
		"event_tenant_heads",
		"event_log",
		"event_projection_checkpoints",
		"human_identity_binding_heads",
		"human_identity_binding_snapshots",
		"human_principal_heads",
		"human_principal_snapshots",
		"native_im_inbox",
		"tenant_membership_heads",
		"tenant_membership_snapshots",
		"agent_definitions",
		"agent_releases",
		"agent_passports",
		"agent_installation_heads",
		"agent_installation_snapshots",
	}
	for index, table := range readTables {
		readTables[index] = "wanwork_im." + pgx.Identifier{table}.Sanitize()
	}
	if _, err := connection.Exec(
		t.Context(),
		"GRANT SELECT ON "+strings.Join(readTables, ", ")+" TO "+quotedRuntime,
	); err != nil {
		t.Fatalf("grant runtime pool reads: %v", err)
	}
	qualifiedFunctions := make([]string, 0, len(functions))
	for _, function := range functions {
		qualifiedFunctions = append(qualifiedFunctions, "wanwork_im."+function)
	}
	if _, err := connection.Exec(
		t.Context(),
		"GRANT EXECUTE ON FUNCTION "+strings.Join(qualifiedFunctions, ", ")+" TO "+quotedRuntime,
	); err != nil {
		t.Fatalf("grant runtime pool functions: %v", err)
	}
}

func runtimePoolRelations(t *testing.T, connection *pgx.Conn) []string {
	t.Helper()
	rows, err := connection.Query(t.Context(), `
SELECT pg_catalog.quote_ident(namespace.nspname) || '.' || pg_catalog.quote_ident(relation.relname)
FROM pg_catalog.pg_class AS relation
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
WHERE namespace.nspname = ANY(ARRAY['wanwork_im', 'wanwork_meta'])
  AND relation.relkind = 'r'
ORDER BY namespace.nspname, relation.relname`)
	if err != nil {
		t.Fatalf("list runtime pool relations: %v", err)
	}
	values, err := pgx.CollectRows(rows, pgx.RowTo[string])
	if err != nil || len(values) != 33 {
		t.Fatalf("runtime pool relation count=%d error=%v", len(values), err)
	}
	return values
}

func runtimePoolFunctions(t *testing.T, connection *pgx.Conn) []string {
	t.Helper()
	rows, err := connection.Query(t.Context(), `
SELECT pg_catalog.quote_ident(procedure.proname) || '(' ||
       pg_catalog.pg_get_function_identity_arguments(procedure.oid) || ')'
FROM pg_catalog.pg_proc AS procedure
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
WHERE namespace.nspname = 'wanwork_im'
ORDER BY procedure.proname, pg_catalog.pg_get_function_identity_arguments(procedure.oid)`)
	if err != nil {
		t.Fatalf("list runtime pool functions: %v", err)
	}
	values, err := pgx.CollectRows(rows, pgx.RowTo[string])
	if err != nil || len(values) != 12 {
		t.Fatalf("runtime pool function count=%d error=%v", len(values), err)
	}
	return values
}

func grantRuntimePoolRole(t *testing.T, connection *pgx.Conn, granted, member string) {
	t.Helper()
	if _, err := connection.Exec(
		t.Context(),
		"GRANT "+pgx.Identifier{granted}.Sanitize()+" TO "+
			pgx.Identifier{member}.Sanitize()+" WITH INHERIT FALSE",
	); err != nil {
		t.Fatalf("grant runtime pool role %s to %s: %v", granted, member, err)
	}
}

func runtimePoolConnectionString(
	t *testing.T,
	adminConfig *pgx.ConnConfig,
	manifest migrations.AuthorityAccessManifest,
) string {
	t.Helper()
	query := url.Values{"sslmode": []string{"disable"}}
	value := url.URL{
		Scheme: "postgresql",
		User:   url.User(manifest.RuntimeLoginRoles[0]),
		Path:   "/" + manifest.DatabaseName,
	}
	if strings.HasPrefix(adminConfig.Host, "/") {
		query.Set("host", adminConfig.Host)
		query.Set("port", strconv.Itoa(int(adminConfig.Port)))
	} else {
		address := net.ParseIP(adminConfig.Host)
		if address == nil || !address.IsLoopback() {
			t.Fatalf("integration admin host %q is not a local test endpoint", adminConfig.Host)
		}
		value.Host = net.JoinHostPort(adminConfig.Host, strconv.Itoa(int(adminConfig.Port)))
	}
	value.RawQuery = query.Encode()
	return value.String()
}

func setAdminRole(t *testing.T, connection *pgx.Conn, role string) {
	t.Helper()
	if _, err := connection.Exec(t.Context(), "SET ROLE "+pgx.Identifier{role}.Sanitize()); err != nil {
		t.Fatalf("set integration owner role: %v", err)
	}
}

func resetAdminRole(t *testing.T, connection *pgx.Conn) {
	t.Helper()
	if _, err := connection.Exec(t.Context(), "RESET ROLE"); err != nil {
		t.Fatalf("reset integration owner role: %v", err)
	}
}

func postgresCode(err error, code string) bool {
	var postgresError *pgconn.PgError
	return errors.As(err, &postgresError) && postgresError.Code == code
}
