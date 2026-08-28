package runtimepool

import (
	"context"
	"errors"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/platform/postgres/migrations"
)

var (
	ErrRuntimeConnectionDrift = errors.New("PostgreSQL runtime connection authority drift")
	ErrNotReady               = errors.New("PostgreSQL runtime pool is not ready")
)

// Pool is an opaque runtime-only pool. Its raw pgx pool is deliberately not exposed, so
// production persistence composition cannot accidentally substitute an owner or migrator pool.
type Pool struct {
	inner        *pgxpool.Pool
	manifest     migrations.AuthorityAccessManifest
	runtimeLogin string
}

// Open constructs the pool and proves one live connection before returning. Every newly opened
// connection receives the same full authority validation, while every acquisition rechecks its
// session state. No owner or migration credential is needed or retained.
func Open(ctx context.Context, input Config) (*Pool, error) {
	if ctx == nil || ctx.Err() != nil {
		return nil, ErrInvalidConfig
	}
	input.Manifest = cloneManifest(input.Manifest)
	parsed, err := parseConfig(input)
	if err != nil {
		return nil, err
	}
	runtimeLogin := parsed.ConnConfig.User
	parsed.AfterConnect = func(hookContext context.Context, connection *pgx.Conn) error {
		return attestNewConnection(hookContext, connection, input.Manifest, runtimeLogin)
	}
	parsed.PrepareConn = func(hookContext context.Context, connection *pgx.Conn) (bool, error) {
		if !connectionStateExact(hookContext, connection, input.Manifest, runtimeLogin) {
			// Returning false with a fixed error destroys the contaminated connection and makes
			// authority drift visible to the current caller. A later acquisition may rebuild.
			return false, ErrRuntimeConnectionDrift
		}
		return true, nil
	}
	inner, err := pgxpool.NewWithConfig(ctx, parsed)
	if err != nil {
		return nil, ErrNotReady
	}
	pool := &Pool{
		inner:        inner,
		manifest:     input.Manifest,
		runtimeLogin: runtimeLogin,
	}
	if err := pool.Ready(ctx); err != nil {
		inner.Close()
		return nil, err
	}
	return pool, nil
}

// Acquire returns a connection only after pgxpool has re-attested its idle session state.
func (pool *Pool) Acquire(ctx context.Context) (*pgxpool.Conn, error) {
	if pool == nil || pool.inner == nil || ctx == nil || ctx.Err() != nil {
		return nil, ErrNotReady
	}
	connection, err := pool.inner.Acquire(ctx)
	if err != nil {
		return nil, ErrNotReady
	}
	return connection, nil
}

// Ready proves both connection-session identity and the complete read-only exact access manifest.
func (pool *Pool) Ready(ctx context.Context) error {
	connection, err := pool.Acquire(ctx)
	if err != nil {
		return ErrNotReady
	}
	defer connection.Release()
	if !connectionStateExact(ctx, connection.Conn(), pool.manifest, pool.runtimeLogin) ||
		migrations.ValidateRuntimeAuthorityAccess(ctx, connection.Conn(), pool.manifest) != nil {
		return ErrNotReady
	}
	return nil
}

func (pool *Pool) Close() {
	if pool != nil && pool.inner != nil {
		pool.inner.Close()
	}
}

func attestNewConnection(
	ctx context.Context,
	connection *pgx.Conn,
	manifest migrations.AuthorityAccessManifest,
	runtimeLogin string,
) error {
	if ctx == nil || ctx.Err() != nil || connection == nil || connection.IsClosed() ||
		connection.PgConn().IsBusy() ||
		connection.PgConn().TxStatus() != 'I' {
		return ErrRuntimeConnectionDrift
	}
	var sessionUser, currentUser, databaseName string
	if err := connection.QueryRow(ctx, "SELECT session_user, current_user, current_database()").Scan(
		&sessionUser,
		&currentUser,
		&databaseName,
	); err != nil || sessionUser != runtimeLogin || currentUser != runtimeLogin ||
		databaseName != manifest.DatabaseName {
		return ErrRuntimeConnectionDrift
	}
	if _, err := connection.Exec(
		ctx,
		"SET ROLE "+pgx.Identifier{manifest.RuntimeRole}.Sanitize(),
	); err != nil {
		return ErrRuntimeConnectionDrift
	}
	if _, err := connection.Exec(ctx, "SET SESSION search_path = pg_catalog"); err != nil {
		return ErrRuntimeConnectionDrift
	}
	if _, err := connection.Exec(ctx, "SET SESSION application_name = 'wanwork-im-runtime'"); err != nil {
		return ErrRuntimeConnectionDrift
	}
	if !connectionStateExact(ctx, connection, manifest, runtimeLogin) ||
		migrations.ValidateRuntimeAuthorityAccess(ctx, connection, manifest) != nil ||
		!connectionStateExact(ctx, connection, manifest, runtimeLogin) {
		return ErrRuntimeConnectionDrift
	}
	return nil
}

func connectionStateExact(
	ctx context.Context,
	connection *pgx.Conn,
	manifest migrations.AuthorityAccessManifest,
	runtimeLogin string,
) bool {
	if ctx == nil || ctx.Err() != nil || connection == nil || connection.IsClosed() ||
		connection.PgConn().IsBusy() ||
		connection.PgConn().TxStatus() != 'I' {
		return false
	}
	var sessionUser, currentUser, databaseName string
	var exactSearchPath, exactApplicationName, tenantSettingAbsent bool
	var advisoryLocksAbsent, listenersAbsent bool
	var exactSessionSettings bool
	err := connection.QueryRow(ctx, `
SELECT session_user,
       current_user,
       current_database(),
       current_setting('search_path') = 'pg_catalog',
	   current_setting('application_name') = 'wanwork-im-runtime',
	   NULLIF(current_setting('wanwork.tenant_id', true), '') IS NULL,
       NOT EXISTS (
           SELECT 1
           FROM pg_catalog.pg_locks
           WHERE pid = pg_catalog.pg_backend_pid()
             AND locktype = 'advisory'
       ),
       NOT EXISTS (SELECT 1 FROM pg_catalog.pg_listening_channels()),
       COALESCE((
           SELECT pg_catalog.array_agg(setting.name ORDER BY setting.name)
           FROM pg_catalog.pg_settings AS setting
           WHERE setting.source = 'session'
	   ), ARRAY[]::text[]) = ARRAY['application_name', 'search_path']::text[]`).Scan(
		&sessionUser,
		&currentUser,
		&databaseName,
		&exactSearchPath,
		&exactApplicationName,
		&tenantSettingAbsent,
		&advisoryLocksAbsent,
		&listenersAbsent,
		&exactSessionSettings,
	)
	return err == nil && connection.PgConn().TxStatus() == 'I' &&
		sessionUser == runtimeLogin && currentUser == manifest.RuntimeRole &&
		databaseName == manifest.DatabaseName && exactSearchPath && exactApplicationName && tenantSettingAbsent &&
		advisoryLocksAbsent && listenersAbsent && exactSessionSettings
}

func cloneManifest(
	manifest migrations.AuthorityAccessManifest,
) migrations.AuthorityAccessManifest {
	manifest.MigrationLoginRoles = append([]string(nil), manifest.MigrationLoginRoles...)
	manifest.RuntimeLoginRoles = append([]string(nil), manifest.RuntimeLoginRoles...)
	return manifest
}
