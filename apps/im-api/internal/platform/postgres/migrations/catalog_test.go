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
	if len(first) != 1 || len(second) != 1 {
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
	first[0].UpSQL = "tampered"
	if second[0].UpSQL == "tampered" {
		t.Fatal("catalog leaked mutable state")
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
		"ROLLBACK;\n",
		"SET search_path TO public;\n",
		"\x00",
	} {
		if validMigrationSQL(sql) {
			t.Fatalf("expected unsafe SQL rejection: %q", sql)
		}
	}
}
