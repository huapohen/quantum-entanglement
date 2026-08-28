package migrations

import (
	"context"
	"errors"
	"fmt"
	"os"
	"strings"
	"testing"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
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
		t.Fatalf("set owner from unlisted admin session: %v", err)
	}
	if err := ValidateAuthorityAccess(t.Context(), connection, manifest); !errors.Is(
		err,
		ErrAuthorityAccessDrift,
	) {
		t.Fatalf("unlisted admin session error = %v, want %v", err, ErrAuthorityAccessDrift)
	}
	if _, err := connection.Exec(t.Context(), "RESET ROLE"); err != nil {
		t.Fatalf("reset unlisted admin session: %v", err)
	}
	authorityConfig := databaseConfig.Copy()
	authorityConfig.User = manifest.MigrationLoginRoles[0]
	authorityConnection, err := pgx.ConnectConfig(t.Context(), authorityConfig)
	if err != nil {
		t.Fatalf("connect authority migration login: %v", err)
	}
	defer func() { _ = authorityConnection.Close(context.Background()) }()
	if _, err := authorityConnection.Exec(t.Context(), "SET ROLE "+pgx.Identifier{manifest.OwnerRole}.Sanitize()); err != nil {
		t.Fatalf("set authority owner role: %v", err)
	}
	if _, err := Apply(t.Context(), authorityConnection); err != nil {
		t.Fatalf("repeat migrations as exact owner: %v", err)
	}
	if err := ValidateAuthorityAccess(t.Context(), authorityConnection, manifest); err != nil {
		t.Fatalf("validate exact authority access: %v", err)
	}
	wrongDatabase := manifest
	wrongDatabase.DatabaseName += "_wrong"
	if err := ValidateAuthorityAccess(t.Context(), authorityConnection, wrongDatabase); !errors.Is(
		err,
		ErrAuthorityAccessDrift,
	) {
		t.Fatalf("wrong database identity error = %v, want %v", err, ErrAuthorityAccessDrift)
	}

	if _, err := authorityConnection.Exec(t.Context(), "RESET ROLE"); err != nil {
		t.Fatalf("reset authority owner role: %v", err)
	}
	if err := ValidateAuthorityAccess(t.Context(), authorityConnection, manifest); !errors.Is(
		err,
		ErrAuthorityAccessDrift,
	) {
		t.Fatalf("wrong current role error = %v, want %v", err, ErrAuthorityAccessDrift)
	}
	if _, err := authorityConnection.Exec(t.Context(), "SET ROLE "+pgx.Identifier{manifest.OwnerRole}.Sanitize()); err != nil {
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
			if _, err := authorityConnection.Exec(t.Context(), fixture.tamper); err != nil {
				t.Fatalf("tamper authority access: %v", err)
			}
			if err := ValidateAuthorityAccess(t.Context(), authorityConnection, manifest); !errors.Is(
				err,
				ErrAuthorityAccessDrift,
			) {
				t.Fatalf("authority access drift error = %v, want %v", err, ErrAuthorityAccessDrift)
			}
			if _, err := authorityConnection.Exec(t.Context(), fixture.repair); err != nil {
				t.Fatalf("repair authority access: %v", err)
			}
			if err := ValidateAuthorityAccess(t.Context(), authorityConnection, manifest); err != nil {
				t.Fatalf("validate repaired authority access: %v", err)
			}
		})
	}
	assertAdminAuthorityAccessDrift(t, connection, authorityConnection, manifest)
	assertDuplicateMembershipGrantorDrift(t, connection, authorityConnection, manifest)
	assertRuntimeLoginAccess(t, databaseConfig, manifest)
	assertMigrationLoginCanSetExactOwner(t, databaseConfig, manifest)
}

func assertDuplicateMembershipGrantorDrift(
	t *testing.T,
	adminConnection *pgx.Conn,
	validationConnection *pgx.Conn,
	manifest AuthorityAccessManifest,
) {
	t.Helper()
	quotedOwner := pgx.Identifier{manifest.OwnerRole}.Sanitize()
	quotedMigrator := pgx.Identifier{manifest.MigratorRole}.Sanitize()
	rogueGrantor := "wanwork_rogue_grantor_" + fmt.Sprintf(
		"%d_%d",
		os.Getpid(),
		integrationDatabaseSequence.Add(1),
	)
	quotedRogue := pgx.Identifier{rogueGrantor}.Sanitize()
	if _, err := adminConnection.Exec(t.Context(),
		"CREATE ROLE "+quotedRogue+
			" NOLOGIN NOSUPERUSER NOINHERIT NOCREATEROLE NOCREATEDB NOREPLICATION NOBYPASSRLS",
	); err != nil {
		t.Fatalf("create duplicate membership grantor: %v", err)
	}
	t.Cleanup(func() {
		_, _ = adminConnection.Exec(context.Background(), "RESET ROLE")
		_, _ = adminConnection.Exec(
			context.Background(),
			"REVOKE "+quotedOwner+" FROM "+quotedRogue+" CASCADE",
		)
		_, _ = adminConnection.Exec(context.Background(), "DROP ROLE "+quotedRogue)
	})
	if _, err := adminConnection.Exec(t.Context(),
		"GRANT "+quotedOwner+" TO "+quotedRogue+
			" WITH ADMIN TRUE, INHERIT FALSE, SET TRUE",
	); err != nil {
		t.Fatalf("grant owner admin option to duplicate grantor: %v", err)
	}
	if _, err := adminConnection.Exec(t.Context(), "SET ROLE "+quotedRogue); err != nil {
		t.Fatalf("set duplicate membership grantor: %v", err)
	}
	if _, err := adminConnection.Exec(t.Context(),
		"GRANT "+quotedOwner+" TO "+quotedMigrator+
			" WITH ADMIN FALSE, INHERIT FALSE, SET TRUE",
	); err != nil {
		t.Fatalf("create duplicate membership grantor row: %v", err)
	}
	if _, err := adminConnection.Exec(t.Context(), "RESET ROLE"); err != nil {
		t.Fatalf("reset duplicate membership grantor: %v", err)
	}
	if err := ValidateAuthorityAccess(t.Context(), validationConnection, manifest); !errors.Is(
		err,
		ErrAuthorityAccessDrift,
	) {
		t.Fatalf("duplicate grantor drift error = %v, want %v", err, ErrAuthorityAccessDrift)
	}
	if _, err := adminConnection.Exec(t.Context(), "SET ROLE "+quotedRogue); err != nil {
		t.Fatalf("set duplicate membership grantor for repair: %v", err)
	}
	if _, err := adminConnection.Exec(t.Context(),
		"REVOKE "+quotedOwner+" FROM "+quotedMigrator,
	); err != nil {
		t.Fatalf("revoke duplicate membership grantor row: %v", err)
	}
	if _, err := adminConnection.Exec(t.Context(), "RESET ROLE"); err != nil {
		t.Fatalf("reset duplicate membership grantor after repair: %v", err)
	}
	if _, err := adminConnection.Exec(t.Context(),
		"REVOKE "+quotedOwner+" FROM "+quotedRogue+" CASCADE",
	); err != nil {
		t.Fatalf("revoke owner from duplicate grantor: %v", err)
	}
	if _, err := adminConnection.Exec(t.Context(), "DROP ROLE "+quotedRogue); err != nil {
		t.Fatalf("drop duplicate membership grantor: %v", err)
	}
	if err := ValidateAuthorityAccess(t.Context(), validationConnection, manifest); err != nil {
		t.Fatalf("validate duplicate grantor repair: %v", err)
	}
}

func assertAdminAuthorityAccessDrift(
	t *testing.T,
	adminConnection *pgx.Conn,
	validationConnection *pgx.Conn,
	manifest AuthorityAccessManifest,
) {
	t.Helper()
	quotedOwner := pgx.Identifier{manifest.OwnerRole}.Sanitize()
	quotedMigrator := pgx.Identifier{manifest.MigratorRole}.Sanitize()
	quotedRuntime := pgx.Identifier{manifest.RuntimeRole}.Sanitize()
	quotedRuntimeLogin := pgx.Identifier{manifest.RuntimeLoginRoles[0]}.Sanitize()
	var databaseName string
	if err := adminConnection.QueryRow(t.Context(), "SELECT current_database()").Scan(&databaseName); err != nil {
		t.Fatalf("read authority database for admin drift: %v", err)
	}
	quotedDatabase := pgx.Identifier{databaseName}.Sanitize()
	for _, fixture := range []struct {
		name   string
		tamper string
		repair string
	}{
		{
			name:   "runtime role login attribute",
			tamper: "ALTER ROLE " + quotedRuntime + " LOGIN",
			repair: "ALTER ROLE " + quotedRuntime + " NOLOGIN",
		},
		{
			name:   "runtime role inherit attribute",
			tamper: "ALTER ROLE " + quotedRuntime + " INHERIT",
			repair: "ALTER ROLE " + quotedRuntime + " NOINHERIT",
		},
		{
			name: "runtime role database setting",
			tamper: "ALTER ROLE " + quotedRuntime + " IN DATABASE " + quotedDatabase +
				" SET search_path = malicious, pg_catalog",
			repair: "ALTER ROLE " + quotedRuntime + " IN DATABASE " + quotedDatabase +
				" RESET search_path",
		},
		{
			name: "membership admin option",
			tamper: "GRANT " + quotedOwner + " TO " + quotedMigrator +
				" WITH ADMIN TRUE",
			repair: "GRANT " + quotedOwner + " TO " + quotedMigrator +
				" WITH ADMIN FALSE, INHERIT FALSE, SET TRUE",
		},
		{
			name: "membership inherit option",
			tamper: "GRANT " + quotedOwner + " TO " + quotedMigrator +
				" WITH INHERIT TRUE",
			repair: "GRANT " + quotedOwner + " TO " + quotedMigrator +
				" WITH ADMIN FALSE, INHERIT FALSE, SET TRUE",
		},
		{
			name: "membership set option",
			tamper: "GRANT " + quotedOwner + " TO " + quotedMigrator +
				" WITH SET FALSE",
			repair: "GRANT " + quotedOwner + " TO " + quotedMigrator +
				" WITH ADMIN FALSE, INHERIT FALSE, SET TRUE",
		},
		{
			name:   "public database connect",
			tamper: "GRANT CONNECT ON DATABASE " + quotedDatabase + " TO PUBLIC",
			repair: "REVOKE CONNECT ON DATABASE " + quotedDatabase + " FROM PUBLIC",
		},
		{
			name: "runtime database create",
			tamper: "GRANT CREATE ON DATABASE " + quotedDatabase + " TO " +
				quotedRuntime,
			repair: "REVOKE CREATE ON DATABASE " + quotedDatabase + " FROM " +
				quotedRuntime,
		},
		{
			name: "runtime database temporary",
			tamper: "GRANT TEMPORARY ON DATABASE " + quotedDatabase + " TO " +
				quotedRuntime,
			repair: "REVOKE TEMPORARY ON DATABASE " + quotedDatabase + " FROM " +
				quotedRuntime,
		},
		{
			name: "direct login database create",
			tamper: "GRANT CREATE ON DATABASE " + quotedDatabase + " TO " +
				quotedRuntimeLogin,
			repair: "REVOKE CREATE ON DATABASE " + quotedDatabase + " FROM " +
				quotedRuntimeLogin,
		},
		{
			name:   "schema owner",
			tamper: "ALTER SCHEMA wanwork_im OWNER TO " + pgx.Identifier{manifest.DatabaseOwnerRole}.Sanitize(),
			repair: "ALTER SCHEMA wanwork_im OWNER TO " + quotedOwner,
		},
		{
			name: "table owner",
			tamper: "ALTER TABLE wanwork_im.conversation_heads OWNER TO " +
				pgx.Identifier{manifest.DatabaseOwnerRole}.Sanitize(),
			repair: "ALTER TABLE wanwork_im.conversation_heads OWNER TO " + quotedOwner,
		},
		{
			name: "function owner",
			tamper: `ALTER FUNCTION wanwork_im.write_tenant_command_receipt(
                text, text, text, text, text
            ) OWNER TO ` + pgx.Identifier{manifest.DatabaseOwnerRole}.Sanitize(),
			repair: `ALTER FUNCTION wanwork_im.write_tenant_command_receipt(
                text, text, text, text, text
            ) OWNER TO ` + quotedOwner,
		},
	} {
		t.Run(fixture.name, func(t *testing.T) {
			if _, err := adminConnection.Exec(t.Context(), fixture.tamper); err != nil {
				t.Fatalf("tamper admin authority access: %v", err)
			}
			if err := ValidateAuthorityAccess(t.Context(), validationConnection, manifest); !errors.Is(
				err,
				ErrAuthorityAccessDrift,
			) {
				t.Fatalf("admin authority access drift error = %v, want %v", err, ErrAuthorityAccessDrift)
			}
			if _, err := adminConnection.Exec(t.Context(), fixture.repair); err != nil {
				t.Fatalf("repair admin authority access: %v", err)
			}
			if err := ValidateAuthorityAccess(t.Context(), validationConnection, manifest); err != nil {
				t.Fatalf("validate repaired admin authority access: %v", err)
			}
		})
	}
}

func assertRuntimeLoginAccess(
	t *testing.T,
	databaseConfig *pgx.ConnConfig,
	manifest AuthorityAccessManifest,
) {
	t.Helper()
	config := databaseConfig.Copy()
	config.User = manifest.RuntimeLoginRoles[0]
	var notices []*pgconn.Notice
	config.OnNotice = func(_ *pgconn.PgConn, notice *pgconn.Notice) {
		notices = append(notices, notice)
	}
	connection, err := pgx.ConnectConfig(t.Context(), config)
	if err != nil {
		t.Fatalf("connect runtime login: %v", err)
	}
	defer func() { _ = connection.Close(context.Background()) }()
	var sessionUser, currentUser string
	if err := connection.QueryRow(t.Context(), "SELECT session_user, current_user").Scan(
		&sessionUser,
		&currentUser,
	); err != nil || sessionUser != manifest.RuntimeLoginRoles[0] || currentUser != sessionUser {
		t.Fatalf("runtime login identities session=%q current=%q error=%v", sessionUser, currentUser, err)
	}
	if err := ValidateRuntimeAuthorityAccess(t.Context(), connection, manifest); !errors.Is(
		err,
		ErrAuthorityAccessDrift,
	) {
		t.Fatalf("runtime validation before SET ROLE error=%v, want access drift", err)
	}
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
	if err := connection.QueryRow(t.Context(), "SELECT current_user").Scan(&currentUser); err != nil ||
		currentUser != manifest.RuntimeRole {
		t.Fatalf("runtime current user=%q error=%v", currentUser, err)
	}
	if err := ValidateRuntimeAuthorityAccess(t.Context(), connection, manifest); err != nil {
		t.Fatalf("runtime exact access validation: %v", err)
	}
	allowedReads := make(map[string]struct{}, len(runtimeAuthorityReadTables))
	for _, tableName := range runtimeAuthorityReadTables {
		allowedReads[tableName] = struct{}{}
	}
	for _, tableName := range authorityAccessTableNames() {
		qualifiedTable := "wanwork_im." + pgx.Identifier{tableName}.Sanitize()
		var canSelect, canMaintain bool
		if err := connection.QueryRow(t.Context(), `
SELECT pg_catalog.has_table_privilege(current_user, $1, 'SELECT'),
       pg_catalog.has_table_privilege(current_user, $1, 'MAINTAIN')`, qualifiedTable).Scan(
			&canSelect,
			&canMaintain,
		); err != nil {
			t.Fatalf("read runtime table privileges for %s: %v", tableName, err)
		}
		_, mustSelect := allowedReads[tableName]
		if canSelect != mustSelect || canMaintain {
			t.Fatalf(
				"runtime table privileges %s select=%v/%v maintain=%v",
				tableName,
				canSelect,
				mustSelect,
				canMaintain,
			)
		}
		notices = nil
		_, analyzeErr := connection.Exec(t.Context(), "ANALYZE "+qualifiedTable)
		if analyzeErr != nil {
			if !hasPostgresCode(analyzeErr, "42501") {
				t.Fatalf("runtime analyze %s error=%v, want SQLSTATE 42501", tableName, analyzeErr)
			}
			continue
		}
		skippedNotice := false
		for _, notice := range notices {
			if notice.Code == "01000" && notice.Severity == "WARNING" {
				skippedNotice = true
				break
			}
		}
		if !skippedNotice {
			noticeSummary := "none"
			if len(notices) > 0 {
				noticeSummary = notices[0].Code + "/" + notices[0].Severity + "/" + notices[0].Message
			}
			t.Fatalf(
				"runtime analyze %s returned neither permission error nor skip warning: %s",
				tableName,
				noticeSummary,
			)
		}
	}
	for _, identity := range authorityFunctionSQLIdentities() {
		var canExecute bool
		if err := connection.QueryRow(t.Context(), `
SELECT pg_catalog.has_function_privilege(current_user, $1, 'EXECUTE')`,
			"wanwork_im."+identity,
		).Scan(&canExecute); err != nil || !canExecute {
			t.Fatalf("runtime function privilege %s=%v error=%v", identity, canExecute, err)
		}
	}
	for _, statement := range []string{
		"INSERT INTO wanwork_im.conversation_heads DEFAULT VALUES",
		"CREATE SCHEMA runtime_escape",
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
	var sessionUser, currentUser string
	if err := connection.QueryRow(t.Context(), "SELECT session_user, current_user").Scan(
		&sessionUser,
		&currentUser,
	); err != nil || sessionUser != manifest.MigrationLoginRoles[0] || currentUser != sessionUser {
		t.Fatalf("migration login identities session=%q current=%q error=%v", sessionUser, currentUser, err)
	}
	var inheritedUsage bool
	if err := connection.QueryRow(t.Context(), `
SELECT pg_catalog.has_schema_privilege(current_user, 'wanwork_im', 'USAGE')`).Scan(
		&inheritedUsage,
	); err != nil || inheritedUsage {
		t.Fatalf("migration login inherited schema usage=%v error=%v", inheritedUsage, err)
	}
	if _, err := connection.Exec(
		t.Context(),
		"SET ROLE "+pgx.Identifier{manifest.RuntimeRole}.Sanitize(),
	); !hasPostgresCode(err, "42501") {
		t.Fatalf("migration login set runtime error=%v, want SQLSTATE 42501", err)
	}
	if _, err := connection.Exec(
		t.Context(),
		"SET ROLE "+pgx.Identifier{manifest.OwnerRole}.Sanitize(),
	); err != nil {
		t.Fatalf("migration login set owner role: %v", err)
	}
	if err := connection.QueryRow(t.Context(), "SELECT session_user, current_user").Scan(
		&sessionUser,
		&currentUser,
	); err != nil || sessionUser != manifest.MigrationLoginRoles[0] || currentUser != manifest.OwnerRole {
		t.Fatalf("migration owner identities session=%q current=%q error=%v", sessionUser, currentUser, err)
	}
	for run := 1; run <= 2; run++ {
		if _, err := Apply(t.Context(), connection); err != nil {
			t.Fatalf("migration login repeat Apply run %d: %v", run, err)
		}
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
		DatabaseName:        "",
		DatabaseOwnerRole:   databaseOwner,
		OwnerRole:           "wanwork_owner_" + suffix,
		MigratorRole:        "wanwork_migrator_" + suffix,
		RuntimeRole:         "wanwork_runtime_" + suffix,
		MigrationLoginRoles: []string{"wanwork_deploy_" + suffix},
		RuntimeLoginRoles:   []string{"wanwork_app_" + suffix},
	}
	if err := connection.QueryRow(t.Context(), "SELECT current_database()").Scan(
		&manifest.DatabaseName,
	); err != nil {
		t.Fatalf("read authority database name: %v", err)
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
