package migrations

import (
	"context"
	"errors"
	"time"

	"github.com/jackc/pgx/v5"
)

const (
	migrationLockKey     int64 = 0x57414e57494d0001
	unlockTimeout              = 5 * time.Second
	minimumServerVersion       = 180000
	maximumServerVersion       = 190000
)

var (
	ErrInvalidConnection = errors.New("invalid PostgreSQL migration connection")
	ErrPostgresVersion   = errors.New("unsupported PostgreSQL migration server version")
	ErrMigrationLock     = errors.New("PostgreSQL migration lock failed")
	ErrLedgerSchema      = errors.New("invalid PostgreSQL migration ledger schema")
	ErrLedgerDrift       = errors.New("PostgreSQL migration ledger drift")
	ErrFutureSchema      = errors.New("PostgreSQL schema is newer than this binary")
	ErrMigrationSchema   = errors.New("PostgreSQL migration postcondition drift")
	ErrMigrationFailed   = errors.New("PostgreSQL migration failed")
	ErrCommitUnknown     = errors.New("PostgreSQL migration commit outcome is unknown")
)

type AppliedMigration struct {
	Version  int64
	Name     string
	Checksum string
}

type State struct {
	Applied []AppliedMigration
}

// Apply acquires a session-scoped advisory lock, validates the exact ledger, and applies every
// pending migration in its own transaction. It never executes DownSQL. The connection is closed
// after a commit-unknown result, a panic, or an unlock failure so a pooled caller cannot reuse a
// session that may still own the lock. Reconnect and call Apply again to reconcile the checksum
// ledger instead of blindly assuming rollback.
func Apply(ctx context.Context, connection *pgx.Conn) (State, error) {
	if connection == nil || connection.IsClosed() || ctx == nil {
		return State{}, ErrInvalidConnection
	}
	return withMigrationLock(ctx, connection, func() (State, error) {
		return applyLocked(ctx, connection)
	})
}

func withMigrationLock(
	ctx context.Context,
	connection *pgx.Conn,
	operation func() (State, error),
) (state State, resultErr error) {
	if _, err := connection.Exec(ctx, "SELECT pg_advisory_lock($1)", migrationLockKey); err != nil {
		quarantineMigrationConnection(connection)
		return State{}, ErrMigrationLock
	}
	defer func() {
		panicValue := recover()
		unlockErr := unlockMigrationConnection(connection)
		quarantine := panicValue != nil || errors.Is(resultErr, ErrCommitUnknown) || unlockErr != nil
		if quarantine {
			quarantineMigrationConnection(connection)
		}
		if unlockErr != nil {
			state = State{}
			resultErr = errors.Join(resultErr, ErrMigrationLock)
		}
		if panicValue != nil {
			panic(panicValue)
		}
	}()
	return operation()
}

func quarantineMigrationConnection(connection *pgx.Conn) {
	closeContext, cancel := context.WithTimeout(context.Background(), unlockTimeout)
	defer cancel()
	_ = connection.Close(closeContext)
}

func unlockMigrationConnection(connection *pgx.Conn) error {
	unlockContext, cancel := context.WithTimeout(context.Background(), unlockTimeout)
	defer cancel()
	var unlocked bool
	if err := connection.QueryRow(
		unlockContext,
		"SELECT pg_advisory_unlock($1)",
		migrationLockKey,
	).Scan(&unlocked); err != nil || !unlocked {
		return ErrMigrationLock
	}
	return nil
}

func applyLocked(ctx context.Context, connection *pgx.Conn) (State, error) {
	catalog, err := Catalog()
	if err != nil {
		return State{}, err
	}
	if err := validateServerVersion(ctx, connection); err != nil {
		return State{}, err
	}
	if err := bootstrapLedger(ctx, connection); err != nil {
		return State{}, err
	}
	applied, err := readApplied(ctx, connection)
	if err != nil {
		return State{}, err
	}
	if err := validateApplied(catalog, applied); err != nil {
		return State{}, err
	}
	if err := validateAppliedPostconditions(ctx, connection, applied); err != nil {
		return State{}, err
	}
	for index := len(applied); index < len(catalog); index++ {
		migration := catalog[index]
		if err := applyOne(ctx, connection, migration); err != nil {
			return State{}, err
		}
		applied = append(applied, AppliedMigration{
			Version: migration.Version, Name: migration.Name, Checksum: migration.Checksum,
		})
	}
	return State{Applied: append([]AppliedMigration(nil), applied...)}, nil
}

func validateServerVersion(ctx context.Context, connection *pgx.Conn) error {
	var version int
	if err := connection.QueryRow(
		ctx,
		"SELECT current_setting('server_version_num')::integer",
	).Scan(&version); err != nil || version < minimumServerVersion || version >= maximumServerVersion {
		return ErrPostgresVersion
	}
	return nil
}

func bootstrapLedger(ctx context.Context, connection *pgx.Conn) error {
	transaction, err := connection.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.Serializable})
	if err != nil {
		return ErrMigrationFailed
	}
	defer func() { _ = transaction.Rollback(context.Background()) }()
	const bootstrapSQL = `
CREATE SCHEMA IF NOT EXISTS wanwork_meta;
CREATE TABLE IF NOT EXISTS wanwork_meta.schema_migrations (
    version bigint NOT NULL,
    name text COLLATE "C" NOT NULL,
    checksum text COLLATE "C" NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT schema_migrations_pkey PRIMARY KEY (version),
    CONSTRAINT schema_migrations_version_check CHECK (version > 0),
    CONSTRAINT schema_migrations_name_check
        CHECK (name ~ '^[a-z][a-z0-9]*(_[a-z0-9]+)*$'),
    CONSTRAINT schema_migrations_checksum_check
        CHECK (checksum ~ '^[0-9a-f]{64}$')
);`
	if _, err := transaction.Exec(ctx, bootstrapSQL, pgx.QueryExecModeSimpleProtocol); err != nil {
		return ErrMigrationFailed
	}
	if err := validateLedgerSchema(ctx, transaction); err != nil {
		return err
	}
	if err := transaction.Commit(ctx); err != nil {
		return ErrCommitUnknown
	}
	return nil
}

type ledgerColumn struct {
	name               string
	formatType         string
	notNull            bool
	defaultSQL         *string
	collationNamespace *string
	collationName      *string
	identityKind       string
	generatedKind      string
}

type ledgerRelation struct {
	currentUser        string
	schemaOwner        string
	relationOwner      string
	relationKind       string
	persistence        string
	rowSecurity        bool
	forceRowSecurity   bool
	ownerOnlySchemaACL bool
	ownerOnlyTableACL  bool
	noColumnACL        bool
	noUserTriggers     bool
	noRewriteRules     bool
	noPolicies         bool
	noPublications     bool
	onlyPrimaryIndex   bool
}

type ledgerConstraint struct {
	name       string
	kind       string
	definition string
	validated  bool
	deferrable bool
	deferred   bool
}

func validateLedgerSchema(ctx context.Context, transaction pgx.Tx) error {
	var relation ledgerRelation
	if err := transaction.QueryRow(ctx, `
SELECT current_user,
       schema_owner.rolname,
       relation_owner.rolname,
       relation.relkind::text,
       relation.relpersistence::text,
       relation.relrowsecurity,
       relation.relforcerowsecurity,
       NOT EXISTS (
           SELECT 1
           FROM pg_catalog.aclexplode(
               COALESCE(
                   namespace.nspacl,
                   pg_catalog.acldefault('n', namespace.nspowner)
               )
           ) AS acl
           WHERE acl.grantee <> namespace.nspowner
       ),
       NOT EXISTS (
           SELECT 1
           FROM pg_catalog.aclexplode(
               COALESCE(
                   relation.relacl,
                   pg_catalog.acldefault('r', relation.relowner)
               )
           ) AS acl
           WHERE acl.grantee <> relation.relowner
       ),
       NOT EXISTS (
           SELECT 1
           FROM pg_catalog.pg_attribute AS protected_attribute
           WHERE protected_attribute.attrelid = relation.oid
             AND protected_attribute.attnum > 0
             AND NOT protected_attribute.attisdropped
             AND protected_attribute.attacl IS NOT NULL
       ),
       NOT EXISTS (
           SELECT 1
           FROM pg_catalog.pg_trigger AS trigger_value
           WHERE trigger_value.tgrelid = relation.oid
             AND NOT trigger_value.tgisinternal
       ),
       NOT EXISTS (
           SELECT 1
           FROM pg_catalog.pg_rewrite AS rewrite_value
           WHERE rewrite_value.ev_class = relation.oid
       ),
       NOT EXISTS (
           SELECT 1
           FROM pg_catalog.pg_policy AS policy_value
           WHERE policy_value.polrelid = relation.oid
       ),
       NOT EXISTS (
           SELECT 1
           FROM pg_catalog.pg_publication_rel AS publication_value
           WHERE publication_value.prrelid = relation.oid
       ) AND NOT EXISTS (
           SELECT 1
           FROM pg_catalog.pg_publication AS publication_value
           WHERE publication_value.puballtables
       ),
       (
           SELECT count(*) = 1
              AND bool_and(index_value.indisprimary)
              AND bool_and(index_value.indisunique)
              AND bool_and(index_value.indisvalid)
              AND bool_and(index_value.indisready)
              AND bool_and(index_value.indislive)
              AND bool_and(index_value.indexprs IS NULL)
              AND bool_and(index_value.indpred IS NULL)
           FROM pg_catalog.pg_index AS index_value
           WHERE index_value.indrelid = relation.oid
       )
FROM pg_catalog.pg_class AS relation
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
JOIN pg_catalog.pg_roles AS schema_owner ON schema_owner.oid = namespace.nspowner
JOIN pg_catalog.pg_roles AS relation_owner ON relation_owner.oid = relation.relowner
WHERE namespace.nspname = 'wanwork_meta'
  AND relation.relname = 'schema_migrations'`).Scan(
		&relation.currentUser,
		&relation.schemaOwner,
		&relation.relationOwner,
		&relation.relationKind,
		&relation.persistence,
		&relation.rowSecurity,
		&relation.forceRowSecurity,
		&relation.ownerOnlySchemaACL,
		&relation.ownerOnlyTableACL,
		&relation.noColumnACL,
		&relation.noUserTriggers,
		&relation.noRewriteRules,
		&relation.noPolicies,
		&relation.noPublications,
		&relation.onlyPrimaryIndex,
	); err != nil || !exactLedgerRelation(relation) {
		return ErrLedgerSchema
	}

	rows, err := transaction.Query(ctx, `
SELECT attribute.attname,
       pg_catalog.format_type(attribute.atttypid, attribute.atttypmod),
       attribute.attnotnull,
       pg_catalog.pg_get_expr(default_value.adbin, default_value.adrelid),
       collation_namespace.nspname,
       collation_value.collname,
       attribute.attidentity::text,
       attribute.attgenerated::text
FROM pg_catalog.pg_attribute AS attribute
JOIN pg_catalog.pg_class AS relation ON relation.oid = attribute.attrelid
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
LEFT JOIN pg_catalog.pg_attrdef AS default_value
       ON default_value.adrelid = relation.oid AND default_value.adnum = attribute.attnum
LEFT JOIN pg_catalog.pg_collation AS collation_value
       ON collation_value.oid = attribute.attcollation
LEFT JOIN pg_catalog.pg_namespace AS collation_namespace
       ON collation_namespace.oid = collation_value.collnamespace
WHERE namespace.nspname = 'wanwork_meta'
  AND relation.relname = 'schema_migrations'
  AND attribute.attnum > 0
  AND NOT attribute.attisdropped
ORDER BY attribute.attnum`)
	if err != nil {
		return ErrLedgerSchema
	}
	defer rows.Close()
	columns := make([]ledgerColumn, 0, 4)
	for rows.Next() {
		var column ledgerColumn
		if err := rows.Scan(
			&column.name,
			&column.formatType,
			&column.notNull,
			&column.defaultSQL,
			&column.collationNamespace,
			&column.collationName,
			&column.identityKind,
			&column.generatedKind,
		); err != nil {
			return ErrLedgerSchema
		}
		columns = append(columns, column)
	}
	if rows.Err() != nil || !exactLedgerColumns(columns) {
		return ErrLedgerSchema
	}

	constraintRows, err := transaction.Query(ctx, `
SELECT constraint_value.conname,
       constraint_value.contype::text,
       pg_catalog.pg_get_constraintdef(constraint_value.oid, false),
       constraint_value.convalidated,
       constraint_value.condeferrable,
       constraint_value.condeferred
FROM pg_catalog.pg_constraint AS constraint_value
JOIN pg_catalog.pg_class AS relation ON relation.oid = constraint_value.conrelid
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
WHERE namespace.nspname = 'wanwork_meta'
  AND relation.relname = 'schema_migrations'
ORDER BY constraint_value.conname`)
	if err != nil {
		return ErrLedgerSchema
	}
	defer constraintRows.Close()
	constraints := make([]ledgerConstraint, 0, 4)
	for constraintRows.Next() {
		var constraint ledgerConstraint
		if err := constraintRows.Scan(
			&constraint.name,
			&constraint.kind,
			&constraint.definition,
			&constraint.validated,
			&constraint.deferrable,
			&constraint.deferred,
		); err != nil {
			return ErrLedgerSchema
		}
		constraints = append(constraints, constraint)
	}
	if constraintRows.Err() != nil || !exactLedgerConstraints(constraints) {
		return ErrLedgerSchema
	}
	return nil
}

func exactLedgerRelation(relation ledgerRelation) bool {
	return relation.currentUser != "" &&
		relation.currentUser == relation.schemaOwner &&
		relation.currentUser == relation.relationOwner &&
		relation.relationKind == "r" &&
		relation.persistence == "p" &&
		!relation.rowSecurity &&
		!relation.forceRowSecurity &&
		relation.ownerOnlySchemaACL &&
		relation.ownerOnlyTableACL &&
		relation.noColumnACL &&
		relation.noUserTriggers &&
		relation.noRewriteRules &&
		relation.noPolicies &&
		relation.noPublications &&
		relation.onlyPrimaryIndex
}

func exactLedgerColumns(columns []ledgerColumn) bool {
	return exactColumns(columns, []ledgerColumn{
		{name: "version", formatType: "bigint", notNull: true},
		{
			name: "name", formatType: "text", notNull: true,
			collationNamespace: stringPointer("pg_catalog"), collationName: stringPointer("C"),
		},
		{
			name: "checksum", formatType: "text", notNull: true,
			collationNamespace: stringPointer("pg_catalog"), collationName: stringPointer("C"),
		},
		{
			name: "applied_at", formatType: "timestamp with time zone", notNull: true,
			defaultSQL: stringPointer("clock_timestamp()"),
		},
	})
}

func valueOrEmpty(value *string) string {
	if value == nil {
		return ""
	}
	return *value
}

func stringPointer(value string) *string {
	return &value
}

func exactColumns(got, expected []ledgerColumn) bool {
	if len(got) != len(expected) {
		return false
	}
	for index := range expected {
		gotColumn := got[index]
		expectedColumn := expected[index]
		if gotColumn.name != expectedColumn.name ||
			gotColumn.formatType != expectedColumn.formatType ||
			gotColumn.notNull != expectedColumn.notNull ||
			valueOrEmpty(gotColumn.defaultSQL) != valueOrEmpty(expectedColumn.defaultSQL) ||
			valueOrEmpty(gotColumn.collationNamespace) != valueOrEmpty(expectedColumn.collationNamespace) ||
			valueOrEmpty(gotColumn.collationName) != valueOrEmpty(expectedColumn.collationName) ||
			gotColumn.identityKind != expectedColumn.identityKind ||
			gotColumn.generatedKind != expectedColumn.generatedKind {
			return false
		}
	}
	return true
}

func exactLedgerConstraints(constraints []ledgerConstraint) bool {
	expected := []ledgerConstraint{
		{
			name:       "schema_migrations_applied_at_not_null",
			kind:       "n",
			definition: "NOT NULL applied_at",
			validated:  true,
		},
		{
			name:       "schema_migrations_checksum_check",
			kind:       "c",
			definition: `CHECK ((checksum ~ '^[0-9a-f]{64}$'::text))`,
			validated:  true,
		},
		{
			name:       "schema_migrations_checksum_not_null",
			kind:       "n",
			definition: "NOT NULL checksum",
			validated:  true,
		},
		{
			name:       "schema_migrations_name_check",
			kind:       "c",
			definition: `CHECK ((name ~ '^[a-z][a-z0-9]*(_[a-z0-9]+)*$'::text))`,
			validated:  true,
		},
		{
			name:       "schema_migrations_name_not_null",
			kind:       "n",
			definition: "NOT NULL name",
			validated:  true,
		},
		{
			name:       "schema_migrations_pkey",
			kind:       "p",
			definition: "PRIMARY KEY (version)",
			validated:  true,
		},
		{
			name:       "schema_migrations_version_check",
			kind:       "c",
			definition: "CHECK ((version > 0))",
			validated:  true,
		},
		{
			name:       "schema_migrations_version_not_null",
			kind:       "n",
			definition: "NOT NULL version",
			validated:  true,
		},
	}
	return exactConstraints(constraints, expected)
}

func exactConstraints(got, expected []ledgerConstraint) bool {
	if len(got) != len(expected) {
		return false
	}
	for index := range expected {
		if got[index] != expected[index] {
			return false
		}
	}
	return true
}

func readApplied(ctx context.Context, connection *pgx.Conn) ([]AppliedMigration, error) {
	rows, err := connection.Query(ctx, `
SELECT version, name, checksum
FROM wanwork_meta.schema_migrations
ORDER BY version`)
	if err != nil {
		return nil, ErrLedgerDrift
	}
	defer rows.Close()
	applied := make([]AppliedMigration, 0)
	for rows.Next() {
		var migration AppliedMigration
		if err := rows.Scan(&migration.Version, &migration.Name, &migration.Checksum); err != nil {
			return nil, ErrLedgerDrift
		}
		applied = append(applied, migration)
	}
	if rows.Err() != nil {
		return nil, ErrLedgerDrift
	}
	return applied, nil
}

func validateApplied(catalog []Migration, applied []AppliedMigration) error {
	if len(applied) > len(catalog) {
		return ErrFutureSchema
	}
	for index, recorded := range applied {
		if recorded.Version != int64(index+1) {
			if recorded.Version > int64(len(catalog)) {
				return ErrFutureSchema
			}
			return ErrLedgerDrift
		}
		expected := catalog[index]
		if recorded.Version != expected.Version || recorded.Name != expected.Name ||
			recorded.Checksum != expected.Checksum {
			return ErrLedgerDrift
		}
	}
	return nil
}

func applyOne(ctx context.Context, connection *pgx.Conn, migration Migration) error {
	transaction, err := connection.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.Serializable})
	if err != nil {
		return ErrMigrationFailed
	}
	defer func() { _ = transaction.Rollback(context.Background()) }()
	if _, err := transaction.Exec(
		ctx,
		migration.UpSQL,
		pgx.QueryExecModeSimpleProtocol,
	); err != nil {
		return ErrMigrationFailed
	}
	if err := validateMigrationPostcondition(ctx, transaction, migration.Version); err != nil {
		return err
	}
	if _, err := transaction.Exec(ctx, `
INSERT INTO wanwork_meta.schema_migrations (version, name, checksum)
VALUES ($1, $2, $3)`, migration.Version, migration.Name, migration.Checksum); err != nil {
		return ErrMigrationFailed
	}
	if err := transaction.Commit(ctx); err != nil {
		return ErrCommitUnknown
	}
	return nil
}
