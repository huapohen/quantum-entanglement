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
		if len(first.Applied) != 1 || len(second.Applied) != 1 ||
			first.Applied[0] != second.Applied[0] {
			t.Fatalf("unexpected migration states: first=%#v second=%#v", first, second)
		}
		var rows int
		if err := connection.QueryRow(ctx, `
SELECT count(*)
FROM wanwork_meta.schema_migrations`).Scan(&rows); err != nil || rows != 1 {
			t.Fatalf("ledger rows = %d, err = %v", rows, err)
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
VALUES (2, 'future', $1)`, strings.Repeat("0", 64)); err != nil {
			t.Fatalf("insert future row: %v", err)
		}
		if _, err := Apply(ctx, connection); !errors.Is(err, ErrFutureSchema) {
			t.Fatalf("Apply future ledger error = %v, want %v", err, ErrFutureSchema)
		}
		if connection.IsClosed() {
			t.Fatal("deterministic future schema must not quarantine the connection")
		}
	})

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
			if errorsByWorker[index] != nil || len(results[index].Applied) != 1 {
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
