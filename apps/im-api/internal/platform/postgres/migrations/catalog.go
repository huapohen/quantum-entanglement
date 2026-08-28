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
		if !validMigrationSQL(upSQL) || !validMigrationSQL(downSQL) {
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
	if strings.TrimSpace(sql) == "" || strings.ContainsRune(sql, '\x00') ||
		strings.Contains(sql, "\r") {
		return false
	}
	upper := strings.ToUpper(sql)
	for _, forbidden := range []string{
		"BEGIN;", "BEGIN TRANSACTION", "COMMIT;", "ROLLBACK;", "SET SEARCH_PATH",
	} {
		if strings.Contains(upper, forbidden) {
			return false
		}
	}
	return true
}
