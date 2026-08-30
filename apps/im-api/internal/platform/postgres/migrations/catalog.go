package migrations

import (
	"crypto/sha256"
	"embed"
	"encoding/hex"
	"errors"
	"fmt"
	"regexp"
	"strings"
)

const migrationDigestDomain = "wanwork.im/postgres-migration/1\n"

var (
	ErrInvalidCatalog = errors.New("invalid PostgreSQL migration catalog")

	canonicalMigrationName = regexp.MustCompile(`^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$`)

	//go:embed sql/*.sql
	migrationFiles embed.FS
)

type Migration struct {
	Version  int64
	Name     string
	UpSQL    string
	DownSQL  string
	Checksum string
}

type migrationSpec struct {
	version int64
	name    string
}

var migrationSpecs = [...]migrationSpec{
	{version: 1, name: "authority_roots"},
	{version: 2, name: "identity_authority"},
	{version: 3, name: "conversation"},
	{version: 4, name: "conversation_authority"},
	{version: 5, name: "function_only_writes"},
	{version: 6, name: "event_store"},
	{version: 7, name: "event_retry_identity"},
	{version: 8, name: "event_projection_checkpoint"},
	{version: 9, name: "native_im_inbox"},
	{version: 10, name: "native_im_inbox_semantics"},
	{version: 11, name: "agent_store_control_plane"},
	{version: 12, name: "agent_store_write_functions"},
	{version: 13, name: "agent_store_capability_constraints"},
	{version: 14, name: "agent_provider_effect_outbox"},
	{version: 15, name: "agent_provider_effect_write_functions"},
}

func Catalog() ([]Migration, error) {
	catalog := make([]Migration, 0, len(migrationSpecs))
	for index, spec := range migrationSpecs {
		if spec.version != int64(index+1) || !canonicalMigrationName.MatchString(spec.name) {
			return nil, ErrInvalidCatalog
		}
		prefix := fmt.Sprintf("sql/%04d_%s", spec.version, spec.name)
		up, err := migrationFiles.ReadFile(prefix + ".up.sql")
		if err != nil {
			return nil, ErrInvalidCatalog
		}
		down, err := migrationFiles.ReadFile(prefix + ".down.sql")
		if err != nil {
			return nil, ErrInvalidCatalog
		}
		upSQL := normalizeSQL(up)
		downSQL := normalizeSQL(down)
		if !validMigrationSQLForSpec(upSQL, spec) || !validMigrationSQLForSpec(downSQL, spec) {
			return nil, ErrInvalidCatalog
		}
		digest := sha256.Sum256([]byte(migrationDigestDomain + upSQL))
		catalog = append(catalog, Migration{
			Version:  spec.version,
			Name:     spec.name,
			UpSQL:    upSQL,
			DownSQL:  downSQL,
			Checksum: hex.EncodeToString(digest[:]),
		})
	}
	return catalog, nil
}

func normalizeSQL(raw []byte) string {
	return strings.TrimSuffix(strings.ReplaceAll(string(raw), "\r\n", "\n"), "\n") + "\n"
}

func validMigrationSQL(sql string) bool {
	return validMigrationSQLWithFunctionDDL(sql, false)
}

func validMigrationSQLForSpec(sql string, spec migrationSpec) bool {
	allowFunctionDDL := (spec.version == 5 && spec.name == "function_only_writes") ||
		(spec.version == 6 && spec.name == "event_store") ||
		(spec.version == 8 && spec.name == "event_projection_checkpoint") ||
		(spec.version == 9 && spec.name == "native_im_inbox") ||
		(spec.version == 10 && spec.name == "native_im_inbox_semantics") ||
		(spec.version == 12 && spec.name == "agent_store_write_functions") ||
		(spec.version == 15 && spec.name == "agent_provider_effect_write_functions")
	return validMigrationSQLWithFunctionDDL(sql, allowFunctionDDL)
}

func validMigrationSQLWithFunctionDDL(sql string, allowFunctionDDL bool) bool {
	if strings.TrimSpace(sql) == "" || strings.ContainsRune(sql, '\x00') ||
		strings.Contains(sql, "\r") {
		return false
	}
	return validMigrationStatements(sql, allowFunctionDDL)
}
