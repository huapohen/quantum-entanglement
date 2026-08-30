package migrations

import (
	"strings"
	"testing"
)

func TestProviderEffectOutboxMigrationFreezesDurabilityAndPrivacyBoundary(t *testing.T) {
	catalog, err := Catalog()
	if err != nil {
		t.Fatalf("load catalog: %v", err)
	}
	if len(catalog) < 14 || catalog[13].Version != 14 {
		t.Fatalf("provider effect migration is not version 14: %#v", catalog)
	}
	up := catalog[13].UpSQL
	down := catalog[13].DownSQL
	for _, column := range []string{
		"tenant_id text COLLATE \"C\" NOT NULL",
		"workspace_id text COLLATE \"C\"",
		"installation_id text COLLATE \"C\" NOT NULL",
		"effect_id text COLLATE \"C\" NOT NULL",
		"effect_kind text COLLATE \"C\" NOT NULL",
		"provider_realm_id text COLLATE \"C\" NOT NULL",
		"provider_subject_id text COLLATE \"C\"",
		"operation_key text COLLATE \"C\" NOT NULL",
		"request_ref text COLLATE \"C\" NOT NULL",
		"request_sha256 text COLLATE \"C\" NOT NULL",
		"status text COLLATE \"C\" NOT NULL",
		"attempt_count bigint NOT NULL",
		"provider_receipt_digest text COLLATE \"C\"",
		"provider_external_id text COLLATE \"C\"",
		"lease_token_digest text COLLATE \"C\"",
		"lease_expires_at timestamptz",
	} {
		if !strings.Contains(up, column) {
			t.Fatalf("provider effect schema missing column declaration %q", column)
		}
	}
	for _, constraint := range []string{
		"PRIMARY KEY (tenant_id, effect_id)",
		"UNIQUE (tenant_id, operation_key)",
		"agent_provider_effects_tenant_fk",
		"agent_provider_effects_workspace_fk",
		"agent_provider_effects_installation_fk",
		"agent_provider_effects_request_sha256_check",
		"agent_provider_effects_receipt_pair_check",
		"agent_provider_effects_lease_digest_check",
		"agent_provider_effects_state_shape_check",
		"agent_provider_effects_time_order_check",
	} {
		if !strings.Contains(up, constraint) {
			t.Fatalf("provider effect schema missing constraint %q", constraint)
		}
	}
	for _, marker := range []string{
		"ALTER TABLE wanwork_im.agent_provider_effects ENABLE ROW LEVEL SECURITY",
		"ALTER TABLE wanwork_im.agent_provider_effects FORCE ROW LEVEL SECURITY",
		"CREATE POLICY agent_provider_effects_exact_tenant",
		"current_setting('wanwork.tenant_id', true)",
		"CREATE INDEX agent_provider_effects_due_idx",
	} {
		if !strings.Contains(up, marker) {
			t.Fatalf("provider effect schema missing boundary marker %q", marker)
		}
	}
	for _, forbidden := range []string{
		"provider_token", "access_token", "secret", "ext_info", "request_body", "payload",
		"password", "api_key", "credential", "endpoint", "IF NOT EXISTS",
	} {
		if strings.Contains(strings.ToLower(up), strings.ToLower(forbidden)) {
			t.Fatalf("provider effect schema contains forbidden secret/payload marker %q", forbidden)
		}
	}
	if !strings.Contains(down, "DROP INDEX wanwork_im.agent_provider_effects_due_idx") ||
		!strings.Contains(down, "DROP TABLE wanwork_im.agent_provider_effects") {
		t.Fatalf("provider effect down migration does not remove index and table: %s", down)
	}
	if !containsString(runtimeAuthorityReadTables, "agent_provider_effects") ||
		!containsString(authorityAccessTableNames(), "agent_provider_effects") {
		t.Fatal("provider effect relation is missing from authority access manifest")
	}
}

func containsString(values []string, want string) bool {
	for _, value := range values {
		if value == want {
			return true
		}
	}
	return false
}
