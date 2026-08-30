package migrations

import (
	"errors"
	"strings"
	"testing"
)

func TestCatalogFreezesChecksummedContiguousMigrations(t *testing.T) {
	first, err := Catalog()
	if err != nil {
		t.Fatalf("load catalog: %v", err)
	}
	second, err := Catalog()
	if err != nil {
		t.Fatalf("load catalog again: %v", err)
	}
	if len(first) != 12 || len(second) != 12 {
		t.Fatalf("unexpected migration count: %d, %d", len(first), len(second))
	}
	migration := first[0]
	if migration.Version != 1 || migration.Name != "authority_roots" ||
		len(migration.Checksum) != 64 || migration.Checksum != second[0].Checksum ||
		migration.UpSQL != second[0].UpSQL || migration.DownSQL != second[0].DownSQL {
		t.Fatalf("unexpected deterministic migration: %#v", migration)
	}
	for _, marker := range []string{
		`CREATE SCHEMA wanwork_im`,
		`CREATE TABLE wanwork_im.provider_realms`,
		`CREATE TABLE wanwork_im.tenants`,
		`CREATE TABLE wanwork_im.workspaces`,
		`ENABLE ROW LEVEL SECURITY`,
		`FORCE ROW LEVEL SECURITY`,
	} {
		if !strings.Contains(migration.UpSQL, marker) {
			t.Fatalf("migration missing %q", marker)
		}
	}
	for _, forbidden := range []string{
		"credential", "password", "api_key", "secret_value", "endpoint", "IF NOT EXISTS",
	} {
		if strings.Contains(strings.ToLower(migration.UpSQL), strings.ToLower(forbidden)) {
			t.Fatalf("migration contains forbidden text %q", forbidden)
		}
	}
	writeFunctions := first[11]
	if writeFunctions.Version != 12 || writeFunctions.Name != "agent_store_write_functions" ||
		len(writeFunctions.Checksum) != 64 || writeFunctions.Checksum != second[11].Checksum ||
		writeFunctions.UpSQL != second[11].UpSQL || writeFunctions.DownSQL != second[11].DownSQL {
		t.Fatalf("unexpected deterministic Agent Store write function migration: %#v", writeFunctions)
	}
	first[0].UpSQL = "tampered"
	if second[0].UpSQL == "tampered" {
		t.Fatal("catalog leaked mutable state")
	}

	identity := first[1]
	if identity.Version != 2 || identity.Name != "identity_authority" ||
		len(identity.Checksum) != 64 || identity.Checksum != second[1].Checksum ||
		identity.UpSQL != second[1].UpSQL || identity.DownSQL != second[1].DownSQL {
		t.Fatalf("unexpected deterministic identity migration: %#v", identity)
	}
	for _, marker := range []string{
		`CREATE TABLE wanwork_im.human_principal_heads`,
		`CREATE TABLE wanwork_im.human_identity_binding_heads`,
		`CREATE TABLE wanwork_im.actor_heads`,
		`CREATE TABLE wanwork_im.tenant_membership_heads`,
		`CREATE TABLE wanwork_im.provider_actor_binding_heads`,
		`DEFERRABLE INITIALLY DEFERRED`,
		`ENABLE ROW LEVEL SECURITY`,
		`FORCE ROW LEVEL SECURITY`,
	} {
		if !strings.Contains(identity.UpSQL, marker) {
			t.Fatalf("identity migration missing %q", marker)
		}
	}
	for _, forbidden := range []string{
		"credential", "password", "api_key", "secret_value", "endpoint", "IF NOT EXISTS",
	} {
		if strings.Contains(strings.ToLower(identity.UpSQL), strings.ToLower(forbidden)) {
			t.Fatalf("identity migration contains forbidden text %q", forbidden)
		}
	}

	conversation := first[2]
	if conversation.Version != 3 || conversation.Name != "conversation" ||
		len(conversation.Checksum) != 64 || conversation.Checksum != second[2].Checksum ||
		conversation.UpSQL != second[2].UpSQL || conversation.DownSQL != second[2].DownSQL {
		t.Fatalf("unexpected deterministic conversation migration: %#v", conversation)
	}
	for _, marker := range []string{
		`CREATE TABLE wanwork_im.conversation_heads`,
		`CREATE TABLE wanwork_im.conversation_snapshots`,
		`CREATE TABLE wanwork_im.provider_conversation_binding_heads`,
		`DEFERRABLE INITIALLY DEFERRED`,
		`ENABLE ROW LEVEL SECURITY`,
		`FORCE ROW LEVEL SECURITY`,
	} {
		if !strings.Contains(conversation.UpSQL, marker) {
			t.Fatalf("conversation migration missing %q", marker)
		}
	}
	for _, forbidden := range []string{
		"agent_thread", "root_message", "credential", "password", "api_key", "secret_value",
		"endpoint", "IF NOT EXISTS",
	} {
		if strings.Contains(strings.ToLower(conversation.UpSQL), strings.ToLower(forbidden)) {
			t.Fatalf("conversation migration contains forbidden text %q", forbidden)
		}
	}

	authority := first[3]
	if authority.Version != 4 || authority.Name != "conversation_authority" ||
		len(authority.Checksum) != 64 || authority.Checksum != second[3].Checksum ||
		authority.UpSQL != second[3].UpSQL || authority.DownSQL != second[3].DownSQL {
		t.Fatalf("unexpected deterministic conversation authority migration: %#v", authority)
	}
	for _, marker := range []string{
		`CREATE TABLE wanwork_im.conversation_membership_heads`,
		`CREATE TABLE wanwork_im.conversation_access_heads`,
		`CREATE TABLE wanwork_im.tenant_command_receipts`,
		`DEFERRABLE INITIALLY DEFERRED`,
		`ENABLE ROW LEVEL SECURITY`,
		`FORCE ROW LEVEL SECURITY`,
	} {
		if !strings.Contains(authority.UpSQL, marker) {
			t.Fatalf("conversation authority migration missing %q", marker)
		}
	}
	for _, forbidden := range []string{
		"credential", "password", "api_key", "secret_value", "endpoint", "IF NOT EXISTS",
	} {
		if strings.Contains(strings.ToLower(authority.UpSQL), strings.ToLower(forbidden)) {
			t.Fatalf("conversation authority migration contains forbidden text %q", forbidden)
		}
	}

	functions := first[4]
	if functions.Version != 5 || functions.Name != "function_only_writes" ||
		len(functions.Checksum) != 64 || functions.Checksum != second[4].Checksum ||
		functions.UpSQL != second[4].UpSQL || functions.DownSQL != second[4].DownSQL {
		t.Fatalf("unexpected deterministic function migration: %#v", functions)
	}
	for _, marker := range []string{
		`CREATE FUNCTION wanwork_im.write_conversation_revision`,
		`CREATE FUNCTION wanwork_im.write_provider_conversation_binding_revision`,
		`CREATE FUNCTION wanwork_im.write_conversation_membership_revision`,
		`CREATE FUNCTION wanwork_im.write_conversation_access_revision`,
		`CREATE FUNCTION wanwork_im.write_tenant_command_receipt`,
		`SECURITY DEFINER`,
		`SET search_path TO pg_catalog`,
		`FROM PUBLIC`,
	} {
		if !strings.Contains(functions.UpSQL, marker) {
			t.Fatalf("function migration missing %q", marker)
		}
	}
	if strings.Count(functions.UpSQL, "CREATE FUNCTION") != 5 ||
		strings.Count(functions.UpSQL, "REVOKE ALL ON FUNCTION") != 5 ||
		strings.Contains(functions.UpSQL, "CREATE OR REPLACE") ||
		strings.Contains(functions.UpSQL, "SECURITY INVOKER") ||
		strings.Contains(functions.UpSQL, "LANGUAGE sql") {
		t.Fatal("function migration escaped its exact surface")
	}
	events := first[5]
	if events.Version != 6 || events.Name != "event_store" ||
		len(events.Checksum) != 64 || events.Checksum != second[5].Checksum ||
		events.UpSQL != second[5].UpSQL || events.DownSQL != second[5].DownSQL {
		t.Fatalf("unexpected deterministic event migration: %#v", events)
	}
	for _, marker := range []string{
		`CREATE TABLE wanwork_im.event_stream_heads`,
		`CREATE TABLE wanwork_im.event_tenant_heads`,
		`CREATE TABLE wanwork_im.event_log`,
		`CREATE FUNCTION wanwork_im.write_event`,
		`ENABLE ROW LEVEL SECURITY`,
		`FORCE ROW LEVEL SECURITY`,
	} {
		if !strings.Contains(events.UpSQL, marker) {
			t.Fatalf("event migration missing %q", marker)
		}
	}
	retryIdentity := first[6]
	if retryIdentity.Version != 7 || retryIdentity.Name != "event_retry_identity" ||
		len(retryIdentity.Checksum) != 64 || retryIdentity.Checksum != second[6].Checksum ||
		retryIdentity.UpSQL != second[6].UpSQL || retryIdentity.DownSQL != second[6].DownSQL {
		t.Fatalf("unexpected deterministic event retry identity migration: %#v", retryIdentity)
	}
	for _, marker := range []string{
		`PRIMARY KEY (tenant_id, workspace_id, event_id)`,
		`CREATE UNIQUE INDEX event_log_scope_idempotency_key_uk`,
	} {
		if !strings.Contains(retryIdentity.UpSQL, marker) {
			t.Fatalf("event retry identity migration missing %q", marker)
		}
	}
	checkpoint := first[7]
	if checkpoint.Version != 8 || checkpoint.Name != "event_projection_checkpoint" ||
		len(checkpoint.Checksum) != 64 || checkpoint.Checksum != second[7].Checksum ||
		checkpoint.UpSQL != second[7].UpSQL || checkpoint.DownSQL != second[7].DownSQL {
		t.Fatalf("unexpected deterministic event projection checkpoint migration: %#v", checkpoint)
	}
	for _, marker := range []string{
		`CREATE TABLE wanwork_im.event_projection_checkpoints`,
		`PRIMARY KEY (tenant_id, workspace_id, projection_id)`,
		`ENABLE ROW LEVEL SECURITY`,
		`FORCE ROW LEVEL SECURITY`,
	} {
		if !strings.Contains(checkpoint.UpSQL, marker) {
			t.Fatalf("event projection checkpoint migration missing %q", marker)
		}
	}
	inbox := first[8]
	if inbox.Version != 9 || inbox.Name != "native_im_inbox" ||
		len(inbox.Checksum) != 64 || inbox.Checksum != second[8].Checksum ||
		inbox.UpSQL != second[8].UpSQL || inbox.DownSQL != second[8].DownSQL {
		t.Fatalf("unexpected deterministic native IM inbox migration: %#v", inbox)
	}
	for _, marker := range []string{
		`CREATE TABLE wanwork_im.native_im_inbox`,
		`PRIMARY KEY (tenant_id, workspace_id, provider, channel_id, event_id)`,
		`CREATE FUNCTION wanwork_im.admit_native_im_inbox`,
		`ENABLE ROW LEVEL SECURITY`,
		`FORCE ROW LEVEL SECURITY`,
	} {
		if !strings.Contains(inbox.UpSQL, marker) {
			t.Fatalf("native IM inbox migration missing %q", marker)
		}
	}
	semantics := first[9]
	if semantics.Version != 10 || semantics.Name != "native_im_inbox_semantics" ||
		len(semantics.Checksum) != 64 || semantics.Checksum != second[9].Checksum ||
		semantics.UpSQL != second[9].UpSQL || semantics.DownSQL != second[9].DownSQL {
		t.Fatalf("unexpected deterministic native IM inbox semantics migration: %#v", semantics)
	}
	for _, marker := range []string{
		`DROP FUNCTION wanwork_im.admit_native_im_inbox`,
		`pg_catalog.sha256`,
		`delivery_count < 9223372036854775807`,
		`REVOKE ALL ON FUNCTION wanwork_im.admit_native_im_inbox`,
	} {
		if !strings.Contains(semantics.UpSQL, marker) {
			t.Fatalf("native IM inbox semantics migration missing %q", marker)
		}
	}
	agentStore := first[10]
	if agentStore.Version != 11 || agentStore.Name != "agent_store_control_plane" ||
		len(agentStore.Checksum) != 64 || agentStore.Checksum != second[10].Checksum ||
		agentStore.UpSQL != second[10].UpSQL || agentStore.DownSQL != second[10].DownSQL {
		t.Fatalf("unexpected deterministic Agent Store migration: %#v", agentStore)
	}
	for _, marker := range []string{
		`CREATE TABLE wanwork_im.agent_definitions`,
		`CREATE TABLE wanwork_im.agent_releases`,
		`CREATE TABLE wanwork_im.agent_passports`,
		`CREATE TABLE wanwork_im.agent_installation_heads`,
		`CREATE TABLE wanwork_im.agent_installation_snapshots`,
		`DEFERRABLE INITIALLY DEFERRED`,
		`FORCE ROW LEVEL SECURITY`,
	} {
		if !strings.Contains(agentStore.UpSQL, marker) {
			t.Fatalf("Agent Store migration missing %q", marker)
		}
	}
	for _, forbidden := range []string{
		"credential", "password", "api_key", "secret_value", "endpoint", "IF NOT EXISTS",
	} {
		if strings.Contains(strings.ToLower(agentStore.UpSQL), strings.ToLower(forbidden)) {
			t.Fatalf("Agent Store migration contains forbidden text %q", forbidden)
		}
	}
}

func TestCatalogRejectsDescriptorAndSQLDrift(t *testing.T) {
	originalSpecs := migrationSpecs
	t.Cleanup(func() { migrationSpecs = originalSpecs })

	migrationSpecs[0].version = 2
	if _, err := Catalog(); !errors.Is(err, ErrInvalidCatalog) {
		t.Fatalf("expected version gap rejection, got %v", err)
	}
	migrationSpecs = originalSpecs
	migrationSpecs[0].name = "Authority Roots"
	if _, err := Catalog(); !errors.Is(err, ErrInvalidCatalog) {
		t.Fatalf("expected name rejection, got %v", err)
	}
}

func TestMigrationSQLRejectsAmbientTransactionsAndSearchPath(t *testing.T) {
	for _, sql := range []string{
		"BEGIN;\nSELECT 1;\n",
		"BEGIN TRANSACTION;\nSELECT 1;\n",
		"SELECT 1;\nCOMMIT;\n",
		"END;\n",
		"COMMIT WORK;\n",
		"ROLLBACK WORK;\n",
		"ABORT;\n",
		"START TRANSACTION;\n",
		"PREPARE TRANSACTION 'migration';\n",
		"SAVEPOINT migration;\n",
		"RELEASE SAVEPOINT migration;\n",
		"SET search_path TO public;\n",
		"SET LOCAL search_path TO public;\n",
		"RESET search_path;\n",
		"SELECT set_config('search_path', 'public', false);\n",
		"DO $$ BEGIN PERFORM set_config('search_path', 'public', false); END $$;\n",
		"CREATE FUNCTION public.mutate_path() RETURNS void AS $$ SELECT 1 $$ LANGUAGE sql;\n",
		"GRANT ALL ON SCHEMA wanwork_im TO PUBLIC;\n",
		"CREATE OR REPLACE VIEW public.bad AS SELECT 1;\n",
		"CREATE TABLE wanwork_im.bad AS SELECT pg_catalog.set_config('search_path', 'public', false);\n",
		"CREATE TABLE wanwork_im.bad (value text DEFAULT pg_catalog.set_config('search_path', 'public', false));\n",
		"ALTER TABLE wanwork_im.safe ADD COLUMN value text DEFAULT pg_catalog.set_config('search_path', 'public', false);\n",
		"CREATE TABLE wanwork_im.bad (value bigint DEFAULT pg_catalog.pg_sleep(1));\n",
		"CREATE /* unterminated",
		"CREATE TABLE public.bad (value text DEFAULT 'unterminated);\n",
		"CREATE TABLE public.bad (value text DEFAULT $tag$unterminated);\n",
		"\x00",
	} {
		if validMigrationSQL(sql) {
			t.Fatalf("expected unsafe SQL rejection: %q", sql)
		}
	}
}

func TestMigrationSQLLexerIgnoresCommentAndLiteralCanaries(t *testing.T) {
	for _, sql := range []string{
		"-- COMMIT; SET search_path\nCREATE TABLE public.safe (id bigint);\n",
		"/* ROLLBACK; /* SET search_path */ COMMIT; */ CREATE TABLE public.safe (id bigint);\n",
		"CREATE TABLE public.safe (value text DEFAULT 'COMMIT; SET search_path');\n",
		"CREATE TABLE public.safe (value text DEFAULT $$COMMIT; SET search_path$$);\n",
		"CREATE TABLE \"semi;colon\" (id bigint);\n",
		"CREATE UNIQUE INDEX safe_uk ON public.safe (id);\nALTER TABLE public.safe ENABLE ROW LEVEL SECURITY;\n",
		"DROP POLICY safe_policy ON public.safe;\nDROP TABLE public.safe;\nDROP SCHEMA public;\n",
	} {
		if !validMigrationSQL(sql) {
			t.Fatalf("expected safe DDL acceptance: %q", sql)
		}
	}
}

func TestMigrationSQLAllowsOnlyVersionFiveAuthorityWriteFunctions(t *testing.T) {
	functionSQL := `CREATE FUNCTION wanwork_im.write_conversation_revision(
    p_tenant_id text,
    p_conversation_id text,
    p_expected_revision bigint,
    p_next_revision bigint,
    p_workspace_id text,
    p_conversation_type text,
    p_status text
) RETURNS boolean
LANGUAGE plpgsql
VOLATILE
STRICT
SECURITY DEFINER
PARALLEL UNSAFE
SET search_path TO pg_catalog
AS $function$
BEGIN
    RETURN true;
END
$function$;
REVOKE ALL ON FUNCTION wanwork_im.write_conversation_revision(
    text, text, bigint, bigint, text, text, text
) FROM PUBLIC;
`
	if validMigrationSQL(functionSQL) {
		t.Fatal("ordinary migrations must reject CREATE FUNCTION")
	}
	if validMigrationSQLForSpec(functionSQL, migrationSpec{version: 6, name: "other"}) {
		t.Fatal("future migrations must not inherit CREATE FUNCTION permission")
	}
	if validMigrationSQLForSpec(functionSQL, migrationSpec{version: 5, name: "other"}) {
		t.Fatal("version five with a different name must reject CREATE FUNCTION")
	}
	if !validMigrationSQLForSpec(
		functionSQL,
		migrationSpec{version: 5, name: "function_only_writes"},
	) {
		t.Fatal("version five must accept the fixed authority function shape")
	}
	downSQL := `DROP FUNCTION wanwork_im.write_conversation_revision(
    text, text, bigint, bigint, text, text, text
);`
	if !validMigrationSQLForSpec(
		downSQL,
		migrationSpec{version: 5, name: "function_only_writes"},
	) {
		t.Fatal("version five must accept the exact authority function drop")
	}
}

func TestVersionFiveFunctionPolicyRejectsUnsafeVariants(t *testing.T) {
	validSpec := migrationSpec{version: 5, name: "function_only_writes"}
	validSQL := `CREATE FUNCTION wanwork_im.write_conversation_revision(
    p_tenant_id text, p_conversation_id text, p_expected_revision bigint,
    p_next_revision bigint, p_workspace_id text, p_conversation_type text, p_status text
) RETURNS boolean LANGUAGE plpgsql VOLATILE STRICT SECURITY DEFINER PARALLEL UNSAFE
SET search_path TO pg_catalog AS $$ BEGIN RETURN true; END $$;`
	for _, sql := range []string{
		strings.Replace(validSQL, "wanwork_im.", "public.", 1),
		strings.Replace(validSQL, "write_conversation_revision", "unknown_write", 1),
		strings.Replace(validSQL, "CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1),
		strings.Replace(validSQL, "VOLATILE", "STABLE", 1),
		strings.Replace(validSQL, "SECURITY DEFINER", "SECURITY INVOKER", 1),
		strings.Replace(validSQL, "PARALLEL UNSAFE", "PARALLEL SAFE", 1),
		strings.Replace(validSQL, "TO pg_catalog", "TO public", 1),
		strings.Replace(validSQL, "TO pg_catalog", "TO pg_catalog, public", 1),
		strings.Replace(validSQL, "TO pg_catalog", `TO pg_catalog, "public"`, 1),
		strings.Replace(validSQL, "STRICT", "STRICT CALLED ON NULL INPUT", 1),
		strings.Replace(validSQL, "SECURITY DEFINER", "SECURITY DEFINER LEAKPROOF", 1),
		strings.Replace(validSQL, "p_tenant_id text", "p_tenant_id text DEFAULT 'tenant'", 1),
		strings.Replace(validSQL, "p_status text", "p_status boolean", 1),
		`DROP FUNCTION IF EXISTS wanwork_im.write_conversation_revision(text, text, bigint, bigint, text, text, text);`,
		`DROP FUNCTION wanwork_im.write_conversation_revision(text, text, bigint, bigint, text, text, text) CASCADE;`,
		`DROP FUNCTION wanwork_im.write_conversation_revision(text, text, bigint, bigint, text, text, text), public.other(text);`,
		`REVOKE ALL ON FUNCTION wanwork_im.write_conversation_revision(text, text, bigint, bigint, text, text, text) FROM wanwork_im_runtime;`,
		`REVOKE GRANT OPTION FOR ALL ON FUNCTION wanwork_im.write_conversation_revision(text, text, bigint, bigint, text, text, text) FROM PUBLIC;`,
	} {
		if validMigrationSQLForSpec(sql, validSpec) {
			t.Fatalf("expected unsafe function SQL rejection: %q", sql)
		}
	}
}

func TestFunctionOnlyWriteMigrationFilesPassExactSQLPolicy(t *testing.T) {
	spec := migrationSpec{version: 5, name: "function_only_writes"}
	for _, suffix := range []string{"up", "down"} {
		raw, err := migrationFiles.ReadFile("sql/0005_function_only_writes." + suffix + ".sql")
		if err != nil {
			t.Fatalf("read function-only %s SQL: %v", suffix, err)
		}
		if !validMigrationSQLForSpec(normalizeSQL(raw), spec) {
			t.Fatalf("function-only %s SQL rejected by exact policy", suffix)
		}
	}
}
