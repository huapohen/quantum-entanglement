package migrations

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"regexp"
	"slices"
)

const (
	authorityManifestDigestDomain      = "wanwork.im/postgres-authority-manifest/1\n"
	authoritySpecificationDigestDomain = "wanwork.im/postgres-authority-specification/1\n"
	migrationCatalogDigestDomain       = "wanwork.im/postgres-migration-catalog/1\n"
	migrationDownDigestDomain          = "wanwork.im/postgres-migration-down/1\n"
)

var canonicalSHA256Digest = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)

type canonicalAuthorityManifest struct {
	DatabaseName        string   `json:"databaseName"`
	DatabaseOwnerRole   string   `json:"databaseOwnerRole"`
	MigrationLoginRoles []string `json:"migrationLoginRoles"`
	MigratorRole        string   `json:"migratorRole"`
	OwnerRole           string   `json:"ownerRole"`
	RuntimeLoginRoles   []string `json:"runtimeLoginRoles"`
	RuntimeRole         string   `json:"runtimeRole"`
}

type canonicalMigrationCatalogEntry struct {
	DownDigest string `json:"downDigest"`
	Name       string `json:"name"`
	UpDigest   string `json:"upDigest"`
	Version    int64  `json:"version"`
}

// DigestAuthorityAccessManifest returns the domain-separated digest of a normalized role
// manifest. Login-role ordering is semantic-set ordering and therefore cannot change the digest.
func DigestAuthorityAccessManifest(manifest AuthorityAccessManifest) (string, error) {
	if manifest.Validate() != nil {
		return "", ErrInvalidAuthorityAccessManifest
	}
	migrationLogins := slices.Clone(manifest.MigrationLoginRoles)
	runtimeLogins := slices.Clone(manifest.RuntimeLoginRoles)
	slices.Sort(migrationLogins)
	slices.Sort(runtimeLogins)
	canonical, err := json.Marshal(canonicalAuthorityManifest{
		DatabaseName:        manifest.DatabaseName,
		DatabaseOwnerRole:   manifest.DatabaseOwnerRole,
		MigrationLoginRoles: migrationLogins,
		MigratorRole:        manifest.MigratorRole,
		OwnerRole:           manifest.OwnerRole,
		RuntimeLoginRoles:   runtimeLogins,
		RuntimeRole:         manifest.RuntimeRole,
	})
	if err != nil {
		return "", ErrInvalidAuthorityAccessManifest
	}
	return authorityDigest(authorityManifestDigestDomain, canonical), nil
}

// CurrentMigrationCatalogDigest binds ordered migration identity, apply checksum, and rollback
// checksum without exposing migration SQL in plans or receipts.
func CurrentMigrationCatalogDigest() (string, error) {
	catalog, err := Catalog()
	if err != nil {
		return "", ErrInvalidCatalog
	}
	entries := make([]canonicalMigrationCatalogEntry, 0, len(catalog))
	for _, migration := range catalog {
		entries = append(entries, canonicalMigrationCatalogEntry{
			DownDigest: authorityDigest(migrationDownDigestDomain, []byte(migration.DownSQL)),
			Name:       migration.Name,
			UpDigest:   "sha256:" + migration.Checksum,
			Version:    migration.Version,
		})
	}
	canonical, err := json.Marshal(entries)
	if err != nil {
		return "", ErrInvalidCatalog
	}
	return authorityDigest(migrationCatalogDigestDomain, canonical), nil
}

// DigestAuthorityAccessSpecification returns the digest consumed by a cutover plan. Only a valid,
// already-normalized specification can be digested.
func DigestAuthorityAccessSpecification(specification AuthorityAccessSpecification) (string, error) {
	if !validAuthorityAccessSpecification(specification) {
		return "", ErrAuthorityAccessSpecification
	}
	canonical, err := json.Marshal(specification)
	if err != nil {
		return "", ErrAuthorityAccessSpecification
	}
	return authorityDigest(authoritySpecificationDigestDomain, canonical), nil
}

func authorityDigest(domain string, canonical []byte) string {
	hash := sha256.New()
	_, _ = hash.Write([]byte(domain))
	_, _ = hash.Write(canonical)
	return "sha256:" + hex.EncodeToString(hash.Sum(nil))
}
