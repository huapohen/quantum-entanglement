package migrations

import (
	"context"
	"errors"
	"fmt"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
)

const postgresIntegrationURL = "WANWORK_TEST_POSTGRES_ADMIN_URL"

var integrationDatabaseSequence atomic.Uint64

func TestApplyAgainstPostgres(t *testing.T) {
	adminURL := os.Getenv(postgresIntegrationURL)
	if adminURL == "" {
		t.Skip(postgresIntegrationURL + " is not set")
	}

	t.Run("fresh and repeat", func(t *testing.T) {
		connection, _ := newIntegrationDatabase(t, adminURL)
		ctx, cancel := context.WithTimeout(t.Context(), 15*time.Second)
		defer cancel()

		first, err := Apply(ctx, connection)
		if err != nil {
			t.Fatalf("fresh Apply: %v", err)
		}
		second, err := Apply(ctx, connection)
		if err != nil {
			t.Fatalf("repeat Apply: %v", err)
		}
		if len(first.Applied) != 9 || len(second.Applied) != 9 ||
			first.Applied[0] != second.Applied[0] || first.Applied[1] != second.Applied[1] ||
			first.Applied[2] != second.Applied[2] || first.Applied[3] != second.Applied[3] ||
			first.Applied[4] != second.Applied[4] || first.Applied[5] != second.Applied[5] {
			t.Fatalf("unexpected migration states: first=%#v second=%#v", first, second)
		}
		var rows int
		if err := connection.QueryRow(ctx, `
SELECT count(*)
		FROM wanwork_meta.schema_migrations`).Scan(&rows); err != nil || rows != 9 {
			t.Fatalf("ledger rows = %d, err = %v", rows, err)
		}
	})

	t.Run("conversation authority schema digest fixture", func(t *testing.T) {
		connection, _ := newIntegrationDatabase(t, adminURL)
		ctx, cancel := context.WithTimeout(t.Context(), 15*time.Second)
		defer cancel()
		catalog, err := Catalog()
		if err != nil {
			t.Fatalf("load catalog: %v", err)
		}
		transaction, err := connection.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.Serializable})
		if err != nil {
			t.Fatalf("begin conversation authority migration: %v", err)
		}
		defer func() { _ = transaction.Rollback(context.Background()) }()
		for _, migration := range catalog[:4] {
			if _, err := transaction.Exec(
				ctx,
				migration.UpSQL,
				pgx.QueryExecModeSimpleProtocol,
			); err != nil {
				t.Fatalf("apply migration %d: %v", migration.Version, err)
			}
		}
		digest, err := tableSchemaDigest(ctx, transaction, conversationAuthorityTableNames)
		if err != nil {
			t.Fatalf("digest conversation authority schema: %v", err)
		}
		if digest != conversationAuthoritySchemaDigest {
			t.Fatalf(
				"conversation authority schema digest = %s, want %s",
				digest,
				conversationAuthoritySchemaDigest,
			)
		}
	})

	t.Run("event store schema digest fixtures", func(t *testing.T) {
		connection, _ := newIntegrationDatabase(t, adminURL)
		ctx, cancel := context.WithTimeout(t.Context(), 15*time.Second)
		defer cancel()
		catalog, err := Catalog()
		if err != nil {
			t.Fatalf("load catalog: %v", err)
		}
		transaction, err := connection.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.Serializable})
		if err != nil {
			t.Fatalf("begin event store migrations: %v", err)
		}
		defer func() { _ = transaction.Rollback(context.Background()) }()
		for _, migration := range catalog[:6] {
			if _, err := transaction.Exec(ctx, migration.UpSQL, pgx.QueryExecModeSimpleProtocol); err != nil {
				t.Fatalf("apply migration %d: %v", migration.Version, err)
			}
		}
		versionSix, err := tableSchemaDigest(ctx, transaction, eventStoreTableNames)
		if err != nil || versionSix != eventStoreSchemaDigestV6 {
			t.Fatalf("event store v6 schema digest = %s/%v, want %s", versionSix, err, eventStoreSchemaDigestV6)
		}
		if _, err := transaction.Exec(ctx, catalog[6].UpSQL, pgx.QueryExecModeSimpleProtocol); err != nil {
			t.Fatalf("apply migration 7: %v", err)
		}
		versionSeven, err := tableSchemaDigest(ctx, transaction, eventStoreTableNames)
		if err != nil || versionSeven != eventStoreSchemaDigestV7 {
			t.Fatalf("event store v7 schema digest = %s/%v, want %s", versionSeven, err, eventStoreSchemaDigestV7)
		}
	})

	t.Run("new migration cannot weaken an older postcondition", func(t *testing.T) {
		connection, _ := newIntegrationDatabase(t, adminURL)
		ctx, cancel := context.WithTimeout(t.Context(), 15*time.Second)
		defer cancel()
		catalog, err := Catalog()
		if err != nil {
			t.Fatalf("load catalog: %v", err)
		}
		if err := bootstrapLedger(ctx, connection); err != nil {
			t.Fatalf("bootstrap ledger: %v", err)
		}
		for _, migration := range catalog[:3] {
			if err := applyOne(ctx, connection, migration); err != nil {
				t.Fatalf("apply prerequisite migration %d: %v", migration.Version, err)
			}
		}
		malicious := catalog[3]
		malicious.UpSQL += `
ALTER TABLE wanwork_im.actor_heads DISABLE ROW LEVEL SECURITY;
`
		if err := applyOne(ctx, connection, malicious); !errors.Is(err, ErrMigrationSchema) {
			t.Fatalf("malicious migration error = %v, want %v", err, ErrMigrationSchema)
		}
		var rowSecurity bool
		if err := connection.QueryRow(ctx, `
SELECT relation.relrowsecurity
FROM pg_catalog.pg_class AS relation
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
WHERE namespace.nspname = 'wanwork_im'
  AND relation.relname = 'actor_heads'`).Scan(&rowSecurity); err != nil || !rowSecurity {
			t.Fatalf("actor RLS after malicious rollback = %v, error = %v", rowSecurity, err)
		}
		var ledgerRows int
		if err := connection.QueryRow(ctx, `
SELECT count(*)
FROM wanwork_meta.schema_migrations`).Scan(&ledgerRows); err != nil || ledgerRows != 3 {
			t.Fatalf("ledger rows after malicious rollback = %d, error = %v", ledgerRows, err)
		}
		var authorityTable *string
		if err := connection.QueryRow(ctx, `
SELECT pg_catalog.to_regclass('wanwork_im.conversation_access_heads')::text`).Scan(
			&authorityTable,
		); err != nil || authorityTable != nil {
			t.Fatalf("authority table after malicious rollback = %v, error = %v", authorityTable, err)
		}
	})

	t.Run("migration transactions ignore hostile ambient search path", func(t *testing.T) {
		connection, _ := newIntegrationDatabase(t, adminURL)
		ctx, cancel := context.WithTimeout(t.Context(), 15*time.Second)
		defer cancel()
		if _, err := connection.Exec(ctx, `
CREATE SCHEMA hostile;
CREATE FUNCTION hostile.clock_timestamp()
RETURNS timestamptz
LANGUAGE sql
IMMUTABLE
AS $$ SELECT '2000-01-01T00:00:00Z'::timestamptz $$;
SET search_path = hostile, pg_catalog`, pgx.QueryExecModeSimpleProtocol); err != nil {
			t.Fatalf("create hostile search path: %v", err)
		}
		if _, err := Apply(ctx, connection); err != nil {
			t.Fatalf("Apply with hostile search path: %v", err)
		}
		var searchPath string
		if err := connection.QueryRow(ctx, "SELECT current_setting('search_path')").Scan(
			&searchPath,
		); err != nil || searchPath != "hostile, pg_catalog" {
			t.Fatalf("ambient search path = %q, error = %v", searchPath, err)
		}
		var defaultExpression string
		if err := connection.QueryRow(ctx, `
SELECT pg_catalog.pg_get_expr(attribute_default.adbin, attribute_default.adrelid)
FROM pg_catalog.pg_attrdef AS attribute_default
JOIN pg_catalog.pg_class AS relation ON relation.oid = attribute_default.adrelid
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
JOIN pg_catalog.pg_attribute AS attribute
  ON attribute.attrelid = relation.oid
 AND attribute.attnum = attribute_default.adnum
WHERE namespace.nspname = 'wanwork_im'
  AND relation.relname = 'tenants'
  AND attribute.attname = 'recorded_at'`).Scan(&defaultExpression); err != nil ||
			defaultExpression != "pg_catalog.clock_timestamp()" {
			t.Fatalf("recorded_at default = %q, error = %v", defaultExpression, err)
		}
	})

	t.Run("conversation schema digest fixture", func(t *testing.T) {
		connection, _ := newIntegrationDatabase(t, adminURL)
		ctx, cancel := context.WithTimeout(t.Context(), 15*time.Second)
		defer cancel()
		catalog, err := Catalog()
		if err != nil {
			t.Fatalf("load catalog: %v", err)
		}
		transaction, err := connection.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.Serializable})
		if err != nil {
			t.Fatalf("begin conversation migration: %v", err)
		}
		defer func() { _ = transaction.Rollback(context.Background()) }()
		for _, migration := range catalog[:3] {
			if _, err := transaction.Exec(
				ctx,
				migration.UpSQL,
				pgx.QueryExecModeSimpleProtocol,
			); err != nil {
				t.Fatalf("apply migration %d: %v", migration.Version, err)
			}
		}
		digest, err := tableSchemaDigest(ctx, transaction, conversationTableNames)
		if err != nil {
			t.Fatalf("digest conversation schema: %v", err)
		}
		if digest != conversationSchemaDigest {
			t.Fatalf("conversation schema digest = %s, want %s", digest, conversationSchemaDigest)
		}
	})

	t.Run("nil context is rejected without touching the connection", func(t *testing.T) {
		connection, _ := newIntegrationDatabase(t, adminURL)
		if _, err := Apply(nil, connection); !errors.Is(err, ErrInvalidConnection) {
			t.Fatalf("Apply nil context error = %v, want %v", err, ErrInvalidConnection)
		}
		if connection.IsClosed() {
			t.Fatal("invalid input must not quarantine a healthy connection")
		}
	})

	t.Run("identity authority schema digest fixture", func(t *testing.T) {
		connection, _ := newIntegrationDatabase(t, adminURL)
		ctx, cancel := context.WithTimeout(t.Context(), 15*time.Second)
		defer cancel()
		catalog, err := Catalog()
		if err != nil {
			t.Fatalf("load catalog: %v", err)
		}
		transaction, err := connection.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.Serializable})
		if err != nil {
			t.Fatalf("begin identity migration: %v", err)
		}
		defer func() { _ = transaction.Rollback(context.Background()) }()
		for _, migration := range catalog[:2] {
			if _, err := transaction.Exec(
				ctx,
				migration.UpSQL,
				pgx.QueryExecModeSimpleProtocol,
			); err != nil {
				t.Fatalf("apply migration %d: %v", migration.Version, err)
			}
		}
		digest, err := tableSchemaDigest(ctx, transaction, identityAuthorityTableNames)
		if err != nil {
			t.Fatalf("digest identity schema: %v", err)
		}
		if digest != identityAuthoritySchemaDigest {
			t.Fatalf("identity authority schema digest = %s, want %s", digest, identityAuthoritySchemaDigest)
		}
	})

	t.Run("same names with weak constraints", func(t *testing.T) {
		connection, _ := newIntegrationDatabase(t, adminURL)
		ctx, cancel := context.WithTimeout(t.Context(), 15*time.Second)
		defer cancel()
		_, err := connection.Exec(ctx, `
CREATE SCHEMA wanwork_meta;
CREATE TABLE wanwork_meta.schema_migrations (
    version bigint NOT NULL,
    name text COLLATE "C" NOT NULL,
    checksum text COLLATE "C" NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT schema_migrations_pkey PRIMARY KEY (version),
    CONSTRAINT schema_migrations_version_check CHECK (version >= 0),
    CONSTRAINT schema_migrations_name_check CHECK (name <> ''),
    CONSTRAINT schema_migrations_checksum_check CHECK (checksum <> '')
)`, pgx.QueryExecModeSimpleProtocol)
		if err != nil {
			t.Fatalf("create weak ledger: %v", err)
		}
		if _, err := Apply(ctx, connection); !errors.Is(err, ErrLedgerSchema) {
			t.Fatalf("Apply weak ledger error = %v, want %v", err, ErrLedgerSchema)
		}
	})

	t.Run("checksum drift", func(t *testing.T) {
		connection, _ := newIntegrationDatabase(t, adminURL)
		ctx, cancel := context.WithTimeout(t.Context(), 15*time.Second)
		defer cancel()
		if _, err := Apply(ctx, connection); err != nil {
			t.Fatalf("seed Apply: %v", err)
		}
		if _, err := connection.Exec(ctx, `
UPDATE wanwork_meta.schema_migrations
SET checksum = $1
WHERE version = 1`, strings.Repeat("0", 64)); err != nil {
			t.Fatalf("tamper checksum: %v", err)
		}
		if _, err := Apply(ctx, connection); !errors.Is(err, ErrLedgerDrift) {
			t.Fatalf("Apply checksum drift error = %v, want %v", err, ErrLedgerDrift)
		}
		if connection.IsClosed() {
			t.Fatal("deterministic ledger drift must not quarantine the connection")
		}
	})

	t.Run("future ledger", func(t *testing.T) {
		connection, _ := newIntegrationDatabase(t, adminURL)
		ctx, cancel := context.WithTimeout(t.Context(), 15*time.Second)
		defer cancel()
		if _, err := Apply(ctx, connection); err != nil {
			t.Fatalf("seed Apply: %v", err)
		}
		if _, err := connection.Exec(ctx, `
INSERT INTO wanwork_meta.schema_migrations (version, name, checksum)
VALUES (10, 'future', $1)`, strings.Repeat("0", 64)); err != nil {
			t.Fatalf("insert future row: %v", err)
		}
		if _, err := Apply(ctx, connection); !errors.Is(err, ErrFutureSchema) {
			t.Fatalf("Apply future ledger error = %v, want %v", err, ErrFutureSchema)
		}
		if connection.IsClosed() {
			t.Fatal("deterministic future schema must not quarantine the connection")
		}
	})

	for _, fixture := range []struct {
		name      string
		tamperSQL string
	}{
		{
			name:      "disabled row security",
			tamperSQL: "ALTER TABLE wanwork_im.tenants DISABLE ROW LEVEL SECURITY",
		},
		{
			name: "weakened tenant policy",
			tamperSQL: `
DROP POLICY tenants_exact_tenant ON wanwork_im.tenants;
CREATE POLICY tenants_exact_tenant ON wanwork_im.tenants
    USING (true)
    WITH CHECK (true)`,
		},
		{
			name: "same name weakened constraint",
			tamperSQL: `
ALTER TABLE wanwork_im.provider_realms
    DROP CONSTRAINT provider_realms_status_check;
ALTER TABLE wanwork_im.provider_realms
    ADD CONSTRAINT provider_realms_status_check CHECK (status <> '')`,
		},
		{
			name:      "extra index",
			tamperSQL: "CREATE INDEX unexpected_workspace_index ON wanwork_im.workspaces (workspace_id)",
		},
		{
			name:      "public table grant",
			tamperSQL: "GRANT SELECT ON wanwork_im.workspaces TO PUBLIC",
		},
		{
			name:      "changed default",
			tamperSQL: "ALTER TABLE wanwork_im.workspaces ALTER COLUMN recorded_at SET DEFAULT now()",
		},
		{
			name: "event append digest constraint removed",
			tamperSQL: `ALTER TABLE wanwork_im.event_log
    DROP CONSTRAINT event_log_append_digest_check`,
		},
		{
			name:      "missing table",
			tamperSQL: "DROP TABLE wanwork_im.workspaces CASCADE",
		},
	} {
		t.Run("postcondition drift "+fixture.name, func(t *testing.T) {
			connection, _ := newIntegrationDatabase(t, adminURL)
			ctx, cancel := context.WithTimeout(t.Context(), 15*time.Second)
			defer cancel()
			if _, err := Apply(ctx, connection); err != nil {
				t.Fatalf("seed Apply: %v", err)
			}
			if _, err := connection.Exec(
				ctx,
				fixture.tamperSQL,
				pgx.QueryExecModeSimpleProtocol,
			); err != nil {
				t.Fatalf("tamper root schema: %v", err)
			}
			if _, err := Apply(ctx, connection); !errors.Is(err, ErrMigrationSchema) {
				t.Fatalf("Apply postcondition drift error = %v, want %v", err, ErrMigrationSchema)
			}
			if connection.IsClosed() {
				t.Fatal("deterministic postcondition drift must not quarantine the connection")
			}
		})
	}

	t.Run("two migrators serialize", func(t *testing.T) {
		firstConnection, config := newIntegrationDatabase(t, adminURL)
		secondConnection, err := pgx.ConnectConfig(t.Context(), config.Copy())
		if err != nil {
			t.Fatalf("connect second migrator: %v", err)
		}
		t.Cleanup(func() { _ = secondConnection.Close(context.Background()) })

		connections := []*pgx.Conn{firstConnection, secondConnection}
		results := make([]State, len(connections))
		errorsByWorker := make([]error, len(connections))
		start := make(chan struct{})
		var workers sync.WaitGroup
		for index := range connections {
			workers.Add(1)
			go func(index int) {
				defer workers.Done()
				<-start
				ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
				defer cancel()
				results[index], errorsByWorker[index] = Apply(ctx, connections[index])
			}(index)
		}
		close(start)
		workers.Wait()
		for index := range connections {
			if errorsByWorker[index] != nil || len(results[index].Applied) != 9 {
				t.Fatalf(
					"migrator %d state=%#v error=%v",
					index,
					results[index],
					errorsByWorker[index],
				)
			}
		}
	})

	t.Run("unknown lock acquisition quarantines connection", func(t *testing.T) {
		lockOwner, config := newIntegrationDatabase(t, adminURL)
		if _, err := lockOwner.Exec(
			t.Context(),
			"SELECT pg_advisory_lock($1)",
			migrationLockKey,
		); err != nil {
			t.Fatalf("acquire blocking lock: %v", err)
		}
		defer func() {
			_, _ = lockOwner.Exec(
				context.Background(),
				"SELECT pg_advisory_unlock($1)",
				migrationLockKey,
			)
		}()

		blocked, err := pgx.ConnectConfig(t.Context(), config.Copy())
		if err != nil {
			t.Fatalf("connect blocked migrator: %v", err)
		}
		t.Cleanup(func() { _ = blocked.Close(context.Background()) })
		ctx, cancel := context.WithTimeout(t.Context(), 100*time.Millisecond)
		defer cancel()
		if _, err := Apply(ctx, blocked); !errors.Is(err, ErrMigrationLock) {
			t.Fatalf("blocked Apply error = %v, want %v", err, ErrMigrationLock)
		}
		if !blocked.IsClosed() {
			t.Fatal("unknown lock acquisition outcome must quarantine the connection")
		}
	})

	t.Run("panic quarantines connection and releases lock", func(t *testing.T) {
		connection, config := newIntegrationDatabase(t, adminURL)
		func() {
			defer func() {
				if recover() == nil {
					t.Fatal("expected injected operation panic")
				}
			}()
			_, _ = withMigrationLock(t.Context(), connection, func() (State, error) {
				panic("integration panic canary")
			})
		}()
		if !connection.IsClosed() {
			t.Fatal("panicked migration connection must be quarantined")
		}

		probe, err := pgx.ConnectConfig(t.Context(), config.Copy())
		if err != nil {
			t.Fatalf("connect lock probe: %v", err)
		}
		defer func() { _ = probe.Close(context.Background()) }()
		var acquired bool
		if err := probe.QueryRow(
			t.Context(),
			"SELECT pg_try_advisory_lock($1)",
			migrationLockKey,
		).Scan(&acquired); err != nil || !acquired {
			t.Fatalf("probe lock acquired=%v error=%v", acquired, err)
		}
		if _, err := probe.Exec(t.Context(), "SELECT pg_advisory_unlock($1)", migrationLockKey); err != nil {
			t.Fatalf("unlock probe: %v", err)
		}
	})
}

func newIntegrationDatabase(t *testing.T, adminURL string) (*pgx.Conn, *pgx.ConnConfig) {
	t.Helper()
	adminConfig, err := pgx.ParseConfig(adminURL)
	if err != nil {
		t.Fatalf("parse %s: %v", postgresIntegrationURL, err)
	}
	adminConnection, err := pgx.ConnectConfig(t.Context(), adminConfig)
	if err != nil {
		t.Fatalf("connect admin database: %v", err)
	}
	t.Cleanup(func() { _ = adminConnection.Close(context.Background()) })

	databaseName := fmt.Sprintf(
		"wanwork_mig_%d_%d",
		os.Getpid(),
		integrationDatabaseSequence.Add(1),
	)
	quotedDatabase := pgx.Identifier{databaseName}.Sanitize()
	if _, err := adminConnection.Exec(t.Context(), "CREATE DATABASE "+quotedDatabase+" TEMPLATE template0"); err != nil {
		t.Fatalf("create integration database: %v", err)
	}
	t.Cleanup(func() {
		_, _ = adminConnection.Exec(
			context.Background(),
			"DROP DATABASE "+quotedDatabase+" WITH (FORCE)",
		)
	})

	databaseConfig := adminConfig.Copy()
	databaseConfig.Database = databaseName
	connection, err := pgx.ConnectConfig(t.Context(), databaseConfig)
	if err != nil {
		t.Fatalf("connect integration database: %v", err)
	}
	t.Cleanup(func() { _ = connection.Close(context.Background()) })
	return connection, databaseConfig
}
