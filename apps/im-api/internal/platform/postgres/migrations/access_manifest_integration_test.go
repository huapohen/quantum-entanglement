package migrations

import (
	"context"
	"errors"
	"fmt"
	"os"
	"strings"
	"testing"

	"github.com/jackc/pgx/v5"
)

func TestAuthorityAccessManifestAgainstPostgres(t *testing.T) {
	adminURL := os.Getenv(postgresIntegrationURL)
	if adminURL == "" {
		t.Skip(postgresIntegrationURL + " is not set")
	}
	connection, databaseConfig := newIntegrationDatabase(t, adminURL)
	if _, err := Apply(t.Context(), connection); err != nil {
		t.Fatalf("apply authority access prerequisites: %v", err)
	}
	manifest := provisionAuthorityAccess(t, connection)
	if _, err := connection.Exec(t.Context(), "SET ROLE "+pgx.Identifier{manifest.OwnerRole}.Sanitize()); err != nil {
		t.Fatalf("set authority owner role: %v", err)
	}
	if _, err := Apply(t.Context(), connection); err != nil {
		t.Fatalf("repeat migrations as exact owner: %v", err)
	}
	if err := ValidateAuthorityAccess(t.Context(), connection, manifest); err != nil {
		t.Fatalf("validate exact authority access: %v", err)
	}

	if _, err := connection.Exec(t.Context(), "RESET ROLE"); err != nil {
		t.Fatalf("reset authority owner role: %v", err)
	}
	if err := ValidateAuthorityAccess(t.Context(), connection, manifest); !errors.Is(
		err,
		ErrAuthorityAccessDrift,
	) {
		t.Fatalf("wrong current role error = %v, want %v", err, ErrAuthorityAccessDrift)
	}
	if _, err := connection.Exec(t.Context(), "SET ROLE "+pgx.Identifier{manifest.OwnerRole}.Sanitize()); err != nil {
		t.Fatalf("restore authority owner role: %v", err)
	}
	quotedRuntime := pgx.Identifier{manifest.RuntimeRole}.Sanitize()
	quotedRuntimeLogin := pgx.Identifier{manifest.RuntimeLoginRoles[0]}.Sanitize()

	for _, fixture := range []struct {
		name   string
		tamper string
		repair string
	}{
		{
			name: "raw table write grant",
			tamper: "GRANT INSERT ON wanwork_im.conversation_heads TO " +
				quotedRuntime,
			repair: "REVOKE INSERT ON wanwork_im.conversation_heads FROM " +
				quotedRuntime,
		},
		{
			name:   "runtime maintain grant",
			tamper: "GRANT MAINTAIN ON wanwork_im.conversation_heads TO " + quotedRuntime,
			repair: "REVOKE MAINTAIN ON wanwork_im.conversation_heads FROM " + quotedRuntime,
		},
		{
			name: "public function execute",
			tamper: `GRANT EXECUTE ON FUNCTION wanwork_im.write_conversation_revision(
                text, text, bigint, bigint, text, text, text
            ) TO PUBLIC`,
			repair: `REVOKE EXECUTE ON FUNCTION wanwork_im.write_conversation_revision(
                text, text, bigint, bigint, text, text, text
			) FROM PUBLIC`,
		},
		{
			name: "extra runtime read",
			tamper: "GRANT SELECT ON wanwork_im.tenants TO " +
				quotedRuntime,
			repair: "REVOKE SELECT ON wanwork_im.tenants FROM " +
				quotedRuntime,
		},
		{
			name:   "direct login schema privilege",
			tamper: "GRANT USAGE ON SCHEMA wanwork_im TO " + quotedRuntimeLogin,
			repair: "REVOKE USAGE ON SCHEMA wanwork_im FROM " + quotedRuntimeLogin,
		},
		{
			name:   "direct login table privilege",
			tamper: "GRANT SELECT ON wanwork_im.conversation_heads TO " + quotedRuntimeLogin,
			repair: "REVOKE SELECT ON wanwork_im.conversation_heads FROM " + quotedRuntimeLogin,
		},
		{
			name: "direct login function privilege",
			tamper: `GRANT EXECUTE ON FUNCTION wanwork_im.write_tenant_command_receipt(
                text, text, text, text, text
            ) TO ` + quotedRuntimeLogin,
			repair: `REVOKE EXECUTE ON FUNCTION wanwork_im.write_tenant_command_receipt(
                text, text, text, text, text
            ) FROM ` + quotedRuntimeLogin,
		},
		{
			name: "column privilege",
			tamper: "GRANT UPDATE (current_revision) ON wanwork_im.conversation_heads TO " +
				quotedRuntime,
			repair: "REVOKE UPDATE (current_revision) ON wanwork_im.conversation_heads FROM " +
				quotedRuntime,
		},
		{
			name: "missing function execute",
			tamper: `REVOKE EXECUTE ON FUNCTION wanwork_im.write_tenant_command_receipt(
                text, text, text, text, text
            ) FROM ` + pgx.Identifier{manifest.RuntimeRole}.Sanitize(),
			repair: `GRANT EXECUTE ON FUNCTION wanwork_im.write_tenant_command_receipt(
                text, text, text, text, text
            ) TO ` + pgx.Identifier{manifest.RuntimeRole}.Sanitize(),
		},
		{
			name: "unexpected authority table",
			tamper: `CREATE TABLE wanwork_im.unexpected_authority_table (
                tenant_id text NOT NULL
            )`,
			repair: "DROP TABLE wanwork_im.unexpected_authority_table",
		},
		{
			name: "unexpected authority function",
			tamper: `CREATE FUNCTION wanwork_im.unexpected_authority_function()
                RETURNS boolean LANGUAGE sql AS 'SELECT true'`,
			repair: "DROP FUNCTION wanwork_im.unexpected_authority_function()",
		},
		{
			name: "unexpected metadata table",
			tamper: `CREATE TABLE wanwork_meta.unexpected_authority_table (
                version bigint NOT NULL
            )`,
			repair: "DROP TABLE wanwork_meta.unexpected_authority_table",
		},
		{
			name: "unexpected metadata function",
			tamper: `CREATE FUNCTION wanwork_meta.unexpected_authority_function()
                RETURNS boolean LANGUAGE sql AS 'SELECT true'`,
			repair: "DROP FUNCTION wanwork_meta.unexpected_authority_function()",
		},
		{
			name: "public function default privilege",
			tamper: "ALTER DEFAULT PRIVILEGES FOR ROLE " +
				pgx.Identifier{manifest.OwnerRole}.Sanitize() +
				" GRANT EXECUTE ON FUNCTIONS TO PUBLIC",
			repair: "ALTER DEFAULT PRIVILEGES FOR ROLE " +
				pgx.Identifier{manifest.OwnerRole}.Sanitize() +
				" REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC",
		},
	} {
		t.Run(fixture.name, func(t *testing.T) {
			if _, err := connection.Exec(t.Context(), fixture.tamper); err != nil {
				t.Fatalf("tamper authority access: %v", err)
			}
			if err := ValidateAuthorityAccess(t.Context(), connection, manifest); !errors.Is(
				err,
				ErrAuthorityAccessDrift,
			) {
				t.Fatalf("authority access drift error = %v, want %v", err, ErrAuthorityAccessDrift)
			}
			if _, err := connection.Exec(t.Context(), fixture.repair); err != nil {
				t.Fatalf("repair authority access: %v", err)
			}
			if err := ValidateAuthorityAccess(t.Context(), connection, manifest); err != nil {
				t.Fatalf("validate repaired authority access: %v", err)
			}
		})
	}
	assertRuntimeLoginAccess(t, databaseConfig, manifest)
	assertMigrationLoginCanSetExactOwner(t, databaseConfig, manifest)
}

func assertRuntimeLoginAccess(
	t *testing.T,
	databaseConfig *pgx.ConnConfig,
	manifest AuthorityAccessManifest,
) {
	t.Helper()
	config := databaseConfig.Copy()
	config.User = manifest.RuntimeLoginRoles[0]
	connection, err := pgx.ConnectConfig(t.Context(), config)
	if err != nil {
		t.Fatalf("connect runtime login: %v", err)
	}
	defer func() { _ = connection.Close(context.Background()) }()
	var inheritedUsage bool
	if err := connection.QueryRow(t.Context(), `
SELECT pg_catalog.has_schema_privilege(current_user, 'wanwork_im', 'USAGE')`).Scan(
		&inheritedUsage,
	); err != nil || inheritedUsage {
		t.Fatalf("runtime login inherited schema usage=%v error=%v", inheritedUsage, err)
	}
	if _, err := connection.Exec(
		t.Context(),
		"SET ROLE "+pgx.Identifier{manifest.RuntimeRole}.Sanitize(),
	); err != nil {
		t.Fatalf("runtime login set exact role: %v", err)
	}
	var currentUser string
	if err := connection.QueryRow(t.Context(), "SELECT current_user").Scan(&currentUser); err != nil ||
		currentUser != manifest.RuntimeRole {
		t.Fatalf("runtime current user=%q error=%v", currentUser, err)
	}
	for _, statement := range []string{
		"INSERT INTO wanwork_im.conversation_heads DEFAULT VALUES",
		"CREATE TEMPORARY TABLE runtime_escape (id bigint)",
		"SET ROLE " + pgx.Identifier{manifest.OwnerRole}.Sanitize(),
		"SET ROLE " + pgx.Identifier{manifest.MigratorRole}.Sanitize(),
	} {
		if _, err := connection.Exec(t.Context(), statement); !hasPostgresCode(err, "42501") {
			t.Fatalf("runtime statement %q error=%v, want SQLSTATE 42501", statement, err)
		}
	}
}

func assertMigrationLoginCanSetExactOwner(
	t *testing.T,
	databaseConfig *pgx.ConnConfig,
	manifest AuthorityAccessManifest,
) {
	t.Helper()
	config := databaseConfig.Copy()
	config.User = manifest.MigrationLoginRoles[0]
	connection, err := pgx.ConnectConfig(t.Context(), config)
	if err != nil {
		t.Fatalf("connect migration login: %v", err)
	}
	defer func() { _ = connection.Close(context.Background()) }()
	if _, err := connection.Exec(
		t.Context(),
		"SET ROLE "+pgx.Identifier{manifest.OwnerRole}.Sanitize(),
	); err != nil {
		t.Fatalf("migration login set owner role: %v", err)
	}
	if _, err := Apply(t.Context(), connection); err != nil {
		t.Fatalf("migration login repeat Apply: %v", err)
	}
	if err := ValidateAuthorityAccess(t.Context(), connection, manifest); err != nil {
		t.Fatalf("migration login validate authority access: %v", err)
	}
}

func provisionAuthorityAccess(t *testing.T, connection *pgx.Conn) AuthorityAccessManifest {
	t.Helper()
	suffix := fmt.Sprintf("%d_%d", os.Getpid(), integrationDatabaseSequence.Add(1))
	var databaseOwner string
	if err := connection.QueryRow(t.Context(), "SELECT current_user").Scan(&databaseOwner); err != nil {
		t.Fatalf("read authority database owner: %v", err)
	}
	manifest := AuthorityAccessManifest{
		DatabaseOwnerRole:   databaseOwner,
		OwnerRole:           "wanwork_owner_" + suffix,
		MigratorRole:        "wanwork_migrator_" + suffix,
		RuntimeRole:         "wanwork_runtime_" + suffix,
		MigrationLoginRoles: []string{"wanwork_deploy_" + suffix},
		RuntimeLoginRoles:   []string{"wanwork_app_" + suffix},
	}
	quotedRoles := make([]string, 0, 5)
	for _, role := range authorityAccessRoleNames(manifest) {
		quotedRole := pgx.Identifier{role}.Sanitize()
		quotedRoles = append(quotedRoles, quotedRole)
		login := "NOLOGIN"
		if role == manifest.MigrationLoginRoles[0] || role == manifest.RuntimeLoginRoles[0] {
			login = "LOGIN"
		}
		if _, err := connection.Exec(t.Context(),
			"CREATE ROLE "+quotedRole+" "+login+
				" NOSUPERUSER NOINHERIT NOCREATEROLE NOCREATEDB NOREPLICATION NOBYPASSRLS CONNECTION LIMIT -1",
		); err != nil {
			t.Fatalf("create authority access role %s: %v", role, err)
		}
	}
	t.Cleanup(func() {
		_, _ = connection.Exec(context.Background(), "RESET ROLE")
		_, _ = connection.Exec(
			context.Background(),
			"DROP OWNED BY "+strings.Join(quotedRoles, ", ")+" CASCADE",
		)
		_, _ = connection.Exec(
			context.Background(),
			"DROP ROLE "+strings.Join(quotedRoles, ", "),
		)
	})
	grantRoleMembership(t, connection, manifest.OwnerRole, manifest.MigratorRole)
	grantRoleMembership(t, connection, manifest.MigratorRole, manifest.MigrationLoginRoles[0])
	grantRoleMembership(t, connection, manifest.RuntimeRole, manifest.RuntimeLoginRoles[0])

	var databaseName string
	if err := connection.QueryRow(t.Context(), "SELECT current_database()").Scan(&databaseName); err != nil {
		t.Fatalf("read authority database name: %v", err)
	}
	quotedDatabase := pgx.Identifier{databaseName}.Sanitize()
	quotedOwner := pgx.Identifier{manifest.OwnerRole}.Sanitize()
	quotedRuntime := pgx.Identifier{manifest.RuntimeRole}.Sanitize()
	for _, statement := range []string{
		"REVOKE CONNECT, CREATE, TEMPORARY ON DATABASE " + quotedDatabase + " FROM PUBLIC",
		"GRANT CREATE ON DATABASE " + quotedDatabase + " TO " + quotedOwner,
		"GRANT CONNECT ON DATABASE " + quotedDatabase + " TO " +
			pgx.Identifier{manifest.MigrationLoginRoles[0]}.Sanitize(),
		"GRANT CONNECT ON DATABASE " + quotedDatabase + " TO " +
			pgx.Identifier{manifest.RuntimeLoginRoles[0]}.Sanitize(),
		"ALTER TABLE wanwork_meta.schema_migrations OWNER TO " + quotedOwner,
		"ALTER SCHEMA wanwork_meta OWNER TO " + quotedOwner,
	} {
		if _, err := connection.Exec(t.Context(), statement); err != nil {
			t.Fatalf("configure authority database ownership: %v", err)
		}
	}
	for _, tableName := range authorityAccessTableNames() {
		if _, err := connection.Exec(t.Context(),
			"ALTER TABLE wanwork_im."+pgx.Identifier{tableName}.Sanitize()+" OWNER TO "+quotedOwner,
		); err != nil {
			t.Fatalf("transfer authority table %s: %v", tableName, err)
		}
	}
	for _, identity := range authorityFunctionSQLIdentities() {
		if _, err := connection.Exec(t.Context(),
			"ALTER FUNCTION wanwork_im."+identity+" OWNER TO "+quotedOwner,
		); err != nil {
			t.Fatalf("transfer authority function %s: %v", identity, err)
		}
	}
	if _, err := connection.Exec(t.Context(), "ALTER SCHEMA wanwork_im OWNER TO "+quotedOwner); err != nil {
		t.Fatalf("transfer authority schema: %v", err)
	}
	if _, err := connection.Exec(t.Context(), "SET ROLE "+quotedOwner); err != nil {
		t.Fatalf("set authority owner for grants: %v", err)
	}
	defer func() { _, _ = connection.Exec(context.Background(), "RESET ROLE") }()
	if _, err := connection.Exec(t.Context(),
		"ALTER DEFAULT PRIVILEGES FOR ROLE "+quotedOwner+
			" REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC",
	); err != nil {
		t.Fatalf("freeze owner function default privileges: %v", err)
	}
	if _, err := connection.Exec(t.Context(), "GRANT USAGE ON SCHEMA wanwork_im TO "+quotedRuntime); err != nil {
		t.Fatalf("grant runtime schema usage: %v", err)
	}
	readTables := make([]string, 0, len(runtimeAuthorityReadTables))
	for _, tableName := range runtimeAuthorityReadTables {
		readTables = append(readTables, "wanwork_im."+pgx.Identifier{tableName}.Sanitize())
	}
	if _, err := connection.Exec(
		t.Context(),
		"GRANT SELECT ON "+strings.Join(readTables, ", ")+" TO "+quotedRuntime,
	); err != nil {
		t.Fatalf("grant runtime authority reads: %v", err)
	}
	executeFunctions := make([]string, 0, 5)
	for _, identity := range authorityFunctionSQLIdentities() {
		executeFunctions = append(executeFunctions, "wanwork_im."+identity)
	}
	if _, err := connection.Exec(
		t.Context(),
		"GRANT EXECUTE ON FUNCTION "+strings.Join(executeFunctions, ", ")+" TO "+quotedRuntime,
	); err != nil {
		t.Fatalf("grant runtime authority functions: %v", err)
	}
	if _, err := connection.Exec(t.Context(), "RESET ROLE"); err != nil {
		t.Fatalf("reset authority owner after grants: %v", err)
	}
	return manifest
}

func grantRoleMembership(t *testing.T, connection *pgx.Conn, granted string, member string) {
	t.Helper()
	if _, err := connection.Exec(
		t.Context(),
		"GRANT "+pgx.Identifier{granted}.Sanitize()+" TO "+pgx.Identifier{member}.Sanitize()+
			" WITH INHERIT FALSE",
	); err != nil {
		t.Fatalf("grant role %s to %s: %v", granted, member, err)
	}
}

func authorityFunctionSQLIdentities() []string {
	return []string{
		"write_conversation_access_revision(text, text, text, bigint, bigint, boolean, boolean, boolean, boolean, boolean, boolean)",
		"write_conversation_membership_revision(text, text, text, bigint, bigint, text, text)",
		"write_conversation_revision(text, text, bigint, bigint, text, text, text)",
		"write_provider_conversation_binding_revision(text, text, text, text, bigint, bigint, text, text)",
		"write_tenant_command_receipt(text, text, text, text, text)",
	}
}
