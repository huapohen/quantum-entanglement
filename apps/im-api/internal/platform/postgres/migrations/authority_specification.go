package migrations

import (
	"errors"
	"slices"
)

const (
	AuthorityAccessSpecificationFormat    = "wanwork.im.postgres-authority-access-spec/1"
	AuthorityAccessPostgreSQLMajor        = minimumServerVersion / 10000
	AuthorityAccessExecutorCompatibility  = "wanwork.im.postgres-authority-executor/1"
	AuthorityAccessValidatorCompatibility = "wanwork.im.postgres-authority-validator/1"
)

var ErrAuthorityAccessSpecification = errors.New("invalid PostgreSQL authority access specification")

type AuthorityObjectKind string

const (
	AuthorityObjectDatabase AuthorityObjectKind = "database"
	AuthorityObjectSchema   AuthorityObjectKind = "schema"
	AuthorityObjectRelation AuthorityObjectKind = "relation"
	AuthorityObjectFunction AuthorityObjectKind = "function"
)

type AuthorityPrivilegeScope string

const (
	AuthorityPrivilegeDatabase AuthorityPrivilegeScope = "database"
	AuthorityPrivilegeSchema   AuthorityPrivilegeScope = "schema"
	AuthorityPrivilegeRelation AuthorityPrivilegeScope = "relation"
	AuthorityPrivilegeFunction AuthorityPrivilegeScope = "function"
)

// AuthorityAccessSpecification is a detached, fully resolved snapshot of the exact database
// authority contract. Every call returns fresh slice backing arrays so callers cannot mutate the
// validator's source of truth. It contains identities and privileges, never a DSN or credential.
type AuthorityAccessSpecification struct {
	Format                        string                                   `json:"format"`
	PostgreSQLMajor               int                                      `json:"postgresqlMajor"`
	MigrationCatalogDigest        string                                   `json:"migrationCatalogDigest"`
	AuthorityManifestDigest       string                                   `json:"authorityManifestDigest"`
	ExecutorCompatibilityVersion  string                                   `json:"executorCompatibilityVersion"`
	ValidatorCompatibilityVersion string                                   `json:"validatorCompatibilityVersion"`
	DatabaseOwner                 AuthorityDatabaseOwnerSpecification      `json:"databaseOwner"`
	Roles                         []AuthorityRoleSpecification             `json:"roles"`
	Memberships                   []AuthorityMembershipSpecification       `json:"memberships"`
	Objects                       []AuthorityObjectSpecification           `json:"objects"`
	Privileges                    []AuthorityPrivilegeSpecification        `json:"privileges"`
	DefaultPrivileges             []AuthorityDefaultPrivilegeSpecification `json:"defaultPrivileges"`
	UnexpectedObjects             bool                                     `json:"unexpectedObjects"`
	UnexpectedPrivileges          bool                                     `json:"unexpectedPrivileges"`
	RoleSettings                  bool                                     `json:"roleSettings"`
	ColumnPrivileges              bool                                     `json:"columnPrivileges"`
	FunctionsInMetadata           bool                                     `json:"functionsInMetadataSchema"`
}

// AuthorityDatabaseOwnerSpecification records the external deployment root that owns the
// database and grants memberships. Its cluster attributes are a Gate A0 provisioner-preflight
// responsibility and are deliberately not inferred by the migration/runtime validator.
type AuthorityDatabaseOwnerSpecification struct {
	Database          string `json:"database"`
	Role              string `json:"role"`
	PreflightRequired bool   `json:"preflightRequired"`
}

type AuthorityRoleSpecification struct {
	Name            string `json:"name"`
	Login           bool   `json:"login"`
	Superuser       bool   `json:"superuser"`
	Inherit         bool   `json:"inherit"`
	CreateRole      bool   `json:"createRole"`
	CreateDatabase  bool   `json:"createDatabase"`
	Replication     bool   `json:"replication"`
	BypassRLS       bool   `json:"bypassRls"`
	ConnectionLimit int    `json:"connectionLimit"`
	ValidUntil      bool   `json:"validUntil"`
	Settings        bool   `json:"settings"`
}

type AuthorityMembershipSpecification struct {
	GrantedRole   string `json:"grantedRole"`
	MemberRole    string `json:"memberRole"`
	GrantorRole   string `json:"grantorRole"`
	AdminOption   bool   `json:"adminOption"`
	InheritOption bool   `json:"inheritOption"`
	SetOption     bool   `json:"setOption"`
}

type AuthorityObjectSpecification struct {
	Kind              AuthorityObjectKind `json:"kind"`
	Schema            string              `json:"schema"`
	Name              string              `json:"name"`
	IdentityArguments string              `json:"identityArguments"`
	OwnerRole         string              `json:"ownerRole"`
}

type AuthorityPrivilegeSpecification struct {
	Scope             AuthorityPrivilegeScope `json:"scope"`
	Schema            string                  `json:"schema"`
	Object            string                  `json:"object"`
	IdentityArguments string                  `json:"identityArguments"`
	GranteeRole       string                  `json:"granteeRole"`
	GrantorRole       string                  `json:"grantorRole"`
	Privilege         string                  `json:"privilege"`
	Grantable         bool                    `json:"grantable"`
}

type AuthorityDefaultPrivilegeSpecification struct {
	OwnerRole   string `json:"ownerRole"`
	Schema      string `json:"schema"`
	ObjectType  string `json:"objectType"`
	GranteeRole string `json:"granteeRole"`
	GrantorRole string `json:"grantorRole"`
	Privilege   string `json:"privilege"`
	Grantable   bool   `json:"grantable"`
}

// CurrentAuthorityAccessSpecification resolves the only supported access graph. It performs no
// database I/O and rejects malformed or overlapping role identities instead of normalizing them.
func CurrentAuthorityAccessSpecification(
	manifest AuthorityAccessManifest,
) (AuthorityAccessSpecification, error) {
	if manifest.Validate() != nil {
		return AuthorityAccessSpecification{}, ErrAuthorityAccessSpecification
	}
	migrationCatalogDigest, err := CurrentMigrationCatalogDigest()
	if err != nil {
		return AuthorityAccessSpecification{}, ErrAuthorityAccessSpecification
	}
	authorityManifestDigest, err := DigestAuthorityAccessManifest(manifest)
	if err != nil {
		return AuthorityAccessSpecification{}, ErrAuthorityAccessSpecification
	}
	specification := AuthorityAccessSpecification{
		Format:                        AuthorityAccessSpecificationFormat,
		PostgreSQLMajor:               AuthorityAccessPostgreSQLMajor,
		MigrationCatalogDigest:        migrationCatalogDigest,
		AuthorityManifestDigest:       authorityManifestDigest,
		ExecutorCompatibilityVersion:  AuthorityAccessExecutorCompatibility,
		ValidatorCompatibilityVersion: AuthorityAccessValidatorCompatibility,
		DatabaseOwner: AuthorityDatabaseOwnerSpecification{
			Database:          manifest.DatabaseName,
			Role:              manifest.DatabaseOwnerRole,
			PreflightRequired: true,
		},
		Roles:                authorityRoleSpecifications(manifest),
		Memberships:          authorityMembershipSpecifications(manifest),
		Objects:              authorityObjectSpecifications(manifest),
		Privileges:           authorityPrivilegeSpecifications(manifest),
		DefaultPrivileges:    authorityDefaultPrivilegeSpecifications(manifest),
		UnexpectedObjects:    false,
		UnexpectedPrivileges: false,
		RoleSettings:         false,
		ColumnPrivileges:     false,
		FunctionsInMetadata:  false,
	}
	if !validAuthorityAccessSpecification(specification) {
		return AuthorityAccessSpecification{}, ErrAuthorityAccessSpecification
	}
	return specification, nil
}

func authorityRoleSpecifications(manifest AuthorityAccessManifest) []AuthorityRoleSpecification {
	loginRoles := make(map[string]struct{}, len(manifest.MigrationLoginRoles)+len(manifest.RuntimeLoginRoles))
	for _, role := range manifest.MigrationLoginRoles {
		loginRoles[role] = struct{}{}
	}
	for _, role := range manifest.RuntimeLoginRoles {
		loginRoles[role] = struct{}{}
	}
	roles := authorityAccessRoleNames(manifest)
	values := make([]AuthorityRoleSpecification, 0, len(roles))
	for _, role := range roles {
		_, login := loginRoles[role]
		values = append(values, AuthorityRoleSpecification{
			Name:            role,
			Login:           login,
			ConnectionLimit: -1,
		})
	}
	return values
}

func authorityMembershipSpecifications(
	manifest AuthorityAccessManifest,
) []AuthorityMembershipSpecification {
	values := []AuthorityMembershipSpecification{{
		GrantedRole: manifest.OwnerRole,
		MemberRole:  manifest.MigratorRole,
		GrantorRole: manifest.DatabaseOwnerRole,
		SetOption:   true,
	}}
	for _, member := range manifest.MigrationLoginRoles {
		values = append(values, AuthorityMembershipSpecification{
			GrantedRole: manifest.MigratorRole,
			MemberRole:  member,
			GrantorRole: manifest.DatabaseOwnerRole,
			SetOption:   true,
		})
	}
	for _, member := range manifest.RuntimeLoginRoles {
		values = append(values, AuthorityMembershipSpecification{
			GrantedRole: manifest.RuntimeRole,
			MemberRole:  member,
			GrantorRole: manifest.DatabaseOwnerRole,
			SetOption:   true,
		})
	}
	slices.SortFunc(values, compareAuthorityMembershipSpecification)
	return values
}

func authorityObjectSpecifications(manifest AuthorityAccessManifest) []AuthorityObjectSpecification {
	values := []AuthorityObjectSpecification{
		{
			Kind:      AuthorityObjectDatabase,
			Name:      manifest.DatabaseName,
			OwnerRole: manifest.DatabaseOwnerRole,
		},
		{
			Kind:      AuthorityObjectSchema,
			Name:      "wanwork_im",
			OwnerRole: manifest.OwnerRole,
		},
		{
			Kind:      AuthorityObjectSchema,
			Name:      "wanwork_meta",
			OwnerRole: manifest.OwnerRole,
		},
		{
			Kind:      AuthorityObjectRelation,
			Schema:    "wanwork_meta",
			Name:      "schema_migrations",
			OwnerRole: manifest.OwnerRole,
		},
	}
	for _, table := range authorityAccessTableNames() {
		values = append(values, AuthorityObjectSpecification{
			Kind:      AuthorityObjectRelation,
			Schema:    "wanwork_im",
			Name:      table,
			OwnerRole: manifest.OwnerRole,
		})
	}
	for _, function := range storedAuthorityFunctionManifest() {
		values = append(values, AuthorityObjectSpecification{
			Kind:              AuthorityObjectFunction,
			Schema:            "wanwork_im",
			Name:              function.name,
			IdentityArguments: function.identityArguments,
			OwnerRole:         manifest.OwnerRole,
		})
	}
	slices.SortFunc(values, compareAuthorityObjectSpecification)
	return values
}

func authorityPrivilegeSpecifications(manifest AuthorityAccessManifest) []AuthorityPrivilegeSpecification {
	values := []AuthorityPrivilegeSpecification{{
		Scope:       AuthorityPrivilegeDatabase,
		Object:      manifest.DatabaseName,
		GranteeRole: manifest.OwnerRole,
		GrantorRole: manifest.DatabaseOwnerRole,
		Privilege:   "CREATE",
	}, {
		Scope:       AuthorityPrivilegeSchema,
		Object:      "wanwork_im",
		GranteeRole: manifest.RuntimeRole,
		GrantorRole: manifest.OwnerRole,
		Privilege:   "USAGE",
	}}
	for _, role := range append(
		append([]string(nil), manifest.MigrationLoginRoles...),
		manifest.RuntimeLoginRoles...,
	) {
		values = append(values, AuthorityPrivilegeSpecification{
			Scope:       AuthorityPrivilegeDatabase,
			Object:      manifest.DatabaseName,
			GranteeRole: role,
			GrantorRole: manifest.DatabaseOwnerRole,
			Privilege:   "CONNECT",
		})
	}
	for _, table := range runtimeAuthorityReadTables {
		values = append(values, AuthorityPrivilegeSpecification{
			Scope:       AuthorityPrivilegeRelation,
			Schema:      "wanwork_im",
			Object:      table,
			GranteeRole: manifest.RuntimeRole,
			GrantorRole: manifest.OwnerRole,
			Privilege:   "SELECT",
		})
	}
	for _, function := range storedAuthorityFunctionManifest() {
		values = append(values, AuthorityPrivilegeSpecification{
			Scope:             AuthorityPrivilegeFunction,
			Schema:            "wanwork_im",
			Object:            function.name,
			IdentityArguments: function.identityArguments,
			GranteeRole:       manifest.RuntimeRole,
			GrantorRole:       manifest.OwnerRole,
			Privilege:         "EXECUTE",
		})
	}
	slices.SortFunc(values, compareAuthorityPrivilegeSpecification)
	return values
}

func authorityDefaultPrivilegeSpecifications(
	manifest AuthorityAccessManifest,
) []AuthorityDefaultPrivilegeSpecification {
	return []AuthorityDefaultPrivilegeSpecification{{
		OwnerRole:   manifest.OwnerRole,
		ObjectType:  "FUNCTION",
		GranteeRole: manifest.OwnerRole,
		GrantorRole: manifest.OwnerRole,
		Privilege:   "EXECUTE",
	}}
}

func validAuthorityAccessSpecification(specification AuthorityAccessSpecification) bool {
	if specification.Format != AuthorityAccessSpecificationFormat ||
		specification.PostgreSQLMajor != AuthorityAccessPostgreSQLMajor ||
		!canonicalSHA256Digest.MatchString(specification.MigrationCatalogDigest) ||
		!canonicalSHA256Digest.MatchString(specification.AuthorityManifestDigest) ||
		specification.ExecutorCompatibilityVersion != AuthorityAccessExecutorCompatibility ||
		specification.ValidatorCompatibilityVersion != AuthorityAccessValidatorCompatibility ||
		specification.DatabaseOwner.Database == "" || specification.DatabaseOwner.Role == "" ||
		!specification.DatabaseOwner.PreflightRequired || len(specification.Roles) == 0 ||
		len(specification.Memberships) == 0 || len(specification.Objects) == 0 ||
		len(specification.Privileges) == 0 || len(specification.DefaultPrivileges) != 1 ||
		specification.UnexpectedObjects || specification.UnexpectedPrivileges ||
		specification.RoleSettings || specification.ColumnPrivileges ||
		specification.FunctionsInMetadata {
		return false
	}
	return slices.IsSortedFunc(specification.Roles, compareAuthorityRoleSpecification) &&
		slices.IsSortedFunc(specification.Memberships, compareAuthorityMembershipSpecification) &&
		slices.IsSortedFunc(specification.Objects, compareAuthorityObjectSpecification) &&
		slices.IsSortedFunc(specification.Privileges, compareAuthorityPrivilegeSpecification)
}

func compareAuthorityRoleSpecification(left, right AuthorityRoleSpecification) int {
	return compareText(left.Name, right.Name)
}

func compareAuthorityMembershipSpecification(
	left AuthorityMembershipSpecification,
	right AuthorityMembershipSpecification,
) int {
	for _, values := range [][2]string{
		{left.GrantedRole, right.GrantedRole},
		{left.MemberRole, right.MemberRole},
		{left.GrantorRole, right.GrantorRole},
	} {
		if result := compareText(values[0], values[1]); result != 0 {
			return result
		}
	}
	return 0
}

func compareAuthorityObjectSpecification(
	left AuthorityObjectSpecification,
	right AuthorityObjectSpecification,
) int {
	for _, values := range [][2]string{
		{string(left.Kind), string(right.Kind)},
		{left.Schema, right.Schema},
		{left.Name, right.Name},
		{left.IdentityArguments, right.IdentityArguments},
		{left.OwnerRole, right.OwnerRole},
	} {
		if result := compareText(values[0], values[1]); result != 0 {
			return result
		}
	}
	return 0
}

func compareAuthorityPrivilegeSpecification(
	left AuthorityPrivilegeSpecification,
	right AuthorityPrivilegeSpecification,
) int {
	for _, values := range [][2]string{
		{string(left.Scope), string(right.Scope)},
		{left.Schema, right.Schema},
		{left.Object, right.Object},
		{left.IdentityArguments, right.IdentityArguments},
		{left.GranteeRole, right.GranteeRole},
		{left.GrantorRole, right.GrantorRole},
		{left.Privilege, right.Privilege},
	} {
		if result := compareText(values[0], values[1]); result != 0 {
			return result
		}
	}
	return 0
}

func compareText(left, right string) int {
	if left < right {
		return -1
	}
	if left > right {
		return 1
	}
	return 0
}
