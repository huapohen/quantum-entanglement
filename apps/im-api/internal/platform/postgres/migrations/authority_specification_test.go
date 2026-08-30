package migrations

import (
	"encoding/json"
	"errors"
	"slices"
	"strings"
	"testing"
)

func TestCurrentAuthorityAccessSpecification(t *testing.T) {
	manifest := specificationTestManifest()
	specification, err := CurrentAuthorityAccessSpecification(manifest)
	if err != nil {
		t.Fatalf("CurrentAuthorityAccessSpecification: %v", err)
	}
	if specification.Format != AuthorityAccessSpecificationFormat ||
		specification.PostgreSQLMajor != AuthorityAccessPostgreSQLMajor {
		t.Fatalf("specification identity = %#v", specification)
	}
	if !canonicalSHA256Digest.MatchString(specification.MigrationCatalogDigest) ||
		!canonicalSHA256Digest.MatchString(specification.AuthorityManifestDigest) ||
		specification.ExecutorCompatibilityVersion != AuthorityAccessExecutorCompatibility ||
		specification.ValidatorCompatibilityVersion != AuthorityAccessValidatorCompatibility {
		t.Fatalf("specification compatibility binding = %#v", specification)
	}
	if specification.DatabaseOwner != (AuthorityDatabaseOwnerSpecification{
		ConnectionLimit:   -1,
		CreateRole:        true,
		Database:          manifest.DatabaseName,
		Role:              manifest.DatabaseOwnerRole,
		PreflightRequired: true,
	}) {
		t.Fatalf("database owner = %#v", specification.DatabaseOwner)
	}
	if len(specification.Roles) != 5 || len(specification.Memberships) != 3 ||
		len(specification.Objects) != 44 || len(specification.Privileges) != 39 ||
		len(specification.DefaultPrivileges) != 1 {
		t.Fatalf(
			"specification counts roles=%d memberships=%d objects=%d privileges=%d defaults=%d",
			len(specification.Roles),
			len(specification.Memberships),
			len(specification.Objects),
			len(specification.Privileges),
			len(specification.DefaultPrivileges),
		)
	}
	if specification.UnexpectedObjects || specification.UnexpectedPrivileges ||
		specification.RoleSettings || specification.ColumnPrivileges ||
		specification.FunctionsInMetadata {
		t.Fatalf("negative authority constraints are not fail closed: %#v", specification)
	}

	wantRoleNames := []string{
		manifest.MigrationLoginRoles[0],
		manifest.RuntimeLoginRoles[0],
		manifest.MigratorRole,
		manifest.OwnerRole,
		manifest.RuntimeRole,
	}
	slices.Sort(wantRoleNames)
	roleNames := make([]string, 0, len(specification.Roles))
	for _, role := range specification.Roles {
		roleNames = append(roleNames, role.Name)
		wantLogin := role.Name == manifest.MigrationLoginRoles[0] ||
			role.Name == manifest.RuntimeLoginRoles[0]
		if role.Login != wantLogin || role.Superuser || role.Inherit || role.CreateRole ||
			role.CreateDatabase || role.Replication || role.BypassRLS ||
			role.ConnectionLimit != -1 || role.ValidUntil || role.Settings {
			t.Fatalf("role specification = %#v", role)
		}
	}
	if !slices.Equal(roleNames, wantRoleNames) {
		t.Fatalf("role names = %q, want %q", roleNames, wantRoleNames)
	}

	assertAuthoritySpecificationContains(t, specification)
	payload, err := json.Marshal(specification)
	if err != nil {
		t.Fatalf("marshal specification: %v", err)
	}
	lowerPayload := strings.ToLower(string(payload))
	for _, forbidden := range []string{"password", "dsn", "connectionstring", "privatekey", "token"} {
		if strings.Contains(lowerPayload, forbidden) {
			t.Fatalf("specification contains forbidden credential field %q: %s", forbidden, payload)
		}
	}
}

func TestCurrentAuthorityAccessSpecificationReturnsDetachedDeterministicSnapshots(t *testing.T) {
	manifest := specificationTestManifest()
	first, err := CurrentAuthorityAccessSpecification(manifest)
	if err != nil {
		t.Fatalf("first specification: %v", err)
	}
	second, err := CurrentAuthorityAccessSpecification(manifest)
	if err != nil {
		t.Fatalf("second specification: %v", err)
	}
	firstPayload, err := json.Marshal(first)
	if err != nil {
		t.Fatalf("marshal first: %v", err)
	}
	secondPayload, err := json.Marshal(second)
	if err != nil {
		t.Fatalf("marshal second: %v", err)
	}
	if string(firstPayload) != string(secondPayload) {
		t.Fatalf("deterministic snapshots differ\nfirst:  %s\nsecond: %s", firstPayload, secondPayload)
	}

	first.Roles[0].Name = "mutated_role"
	first.Memberships[0].GrantedRole = "mutated_grant"
	first.Objects[0].Name = "mutated_object"
	first.Privileges[0].Privilege = "MUTATED"
	first.DefaultPrivileges[0].Privilege = "MUTATED"
	third, err := CurrentAuthorityAccessSpecification(manifest)
	if err != nil {
		t.Fatalf("third specification: %v", err)
	}
	thirdPayload, err := json.Marshal(third)
	if err != nil {
		t.Fatalf("marshal third: %v", err)
	}
	if string(secondPayload) != string(thirdPayload) {
		t.Fatalf("caller mutation changed specification source\nwant: %s\ngot:  %s", secondPayload, thirdPayload)
	}
}

func TestAuthorityValidatorExpectationViewsPartitionTheSpecification(t *testing.T) {
	manifest := specificationTestManifest()
	specification, err := CurrentAuthorityAccessSpecification(manifest)
	if err != nil {
		t.Fatalf("CurrentAuthorityAccessSpecification: %v", err)
	}

	roleNames := make([]string, 0, len(specification.Roles))
	for _, role := range specification.Roles {
		roleNames = append(roleNames, role.Name)
	}
	if want := authorityAccessRoleNames(manifest); !slices.Equal(roleNames, want) {
		t.Fatalf("validator role names = %q, specification role names = %q", want, roleNames)
	}

	objectViews := make([]AuthorityObjectSpecification, 0, len(specification.Objects))
	for _, kind := range []AuthorityObjectKind{
		AuthorityObjectDatabase,
		AuthorityObjectSchema,
		AuthorityObjectRelation,
		AuthorityObjectFunction,
	} {
		objectViews = append(objectViews, authorityObjectsFor(specification, kind, "")...)
	}
	slices.SortFunc(objectViews, compareAuthorityObjectSpecification)
	if !slices.Equal(objectViews, specification.Objects) {
		t.Fatalf("validator object views do not exactly partition the specification")
	}

	privilegeViews := make([]AuthorityPrivilegeSpecification, 0, len(specification.Privileges))
	for _, scope := range []AuthorityPrivilegeScope{
		AuthorityPrivilegeDatabase,
		AuthorityPrivilegeSchema,
		AuthorityPrivilegeRelation,
		AuthorityPrivilegeFunction,
	} {
		privilegeViews = append(privilegeViews, authorityPrivilegesFor(specification, scope)...)
	}
	slices.SortFunc(privilegeViews, compareAuthorityPrivilegeSpecification)
	if !slices.Equal(privilegeViews, specification.Privileges) {
		t.Fatalf("validator privilege views do not exactly partition the specification")
	}
}

func TestCurrentAuthorityAccessSpecificationRejectsInvalidManifest(t *testing.T) {
	tests := map[string]func(*AuthorityAccessManifest){
		"empty database": func(manifest *AuthorityAccessManifest) { manifest.DatabaseName = "" },
		"overlapping root": func(manifest *AuthorityAccessManifest) {
			manifest.DatabaseOwnerRole = manifest.OwnerRole
		},
		"missing migration login": func(manifest *AuthorityAccessManifest) {
			manifest.MigrationLoginRoles = nil
		},
		"duplicate runtime login": func(manifest *AuthorityAccessManifest) {
			manifest.RuntimeLoginRoles = []string{"runtime_login", "runtime_login"}
		},
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			manifest := specificationTestManifest()
			mutate(&manifest)
			if _, err := CurrentAuthorityAccessSpecification(manifest); !errors.Is(
				err,
				ErrAuthorityAccessSpecification,
			) {
				t.Fatalf("error = %v, want %v", err, ErrAuthorityAccessSpecification)
			}
		})
	}
}

func TestAuthoritySpecificationRejectsDatabaseOwnerAttributeDrift(t *testing.T) {
	mutations := map[string]func(*AuthorityDatabaseOwnerSpecification){
		"login":           func(value *AuthorityDatabaseOwnerSpecification) { value.Login = true },
		"superuser":       func(value *AuthorityDatabaseOwnerSpecification) { value.Superuser = true },
		"inherit":         func(value *AuthorityDatabaseOwnerSpecification) { value.Inherit = true },
		"missing create":  func(value *AuthorityDatabaseOwnerSpecification) { value.CreateRole = false },
		"create database": func(value *AuthorityDatabaseOwnerSpecification) { value.CreateDatabase = true },
		"replication":     func(value *AuthorityDatabaseOwnerSpecification) { value.Replication = true },
		"bypass rls":      func(value *AuthorityDatabaseOwnerSpecification) { value.BypassRLS = true },
		"connection limit": func(value *AuthorityDatabaseOwnerSpecification) {
			value.ConnectionLimit = 1
		},
		"valid until": func(value *AuthorityDatabaseOwnerSpecification) { value.ValidUntil = true },
		"settings":    func(value *AuthorityDatabaseOwnerSpecification) { value.Settings = true },
	}
	for name, mutate := range mutations {
		t.Run(name, func(t *testing.T) {
			specification, err := CurrentAuthorityAccessSpecification(specificationTestManifest())
			if err != nil {
				t.Fatalf("CurrentAuthorityAccessSpecification: %v", err)
			}
			mutate(&specification.DatabaseOwner)
			if _, err := DigestAuthorityAccessSpecification(specification); !errors.Is(
				err,
				ErrAuthorityAccessSpecification,
			) {
				t.Fatalf("DigestAuthorityAccessSpecification error = %v, want %v", err, ErrAuthorityAccessSpecification)
			}
		})
	}
}

func assertAuthoritySpecificationContains(t *testing.T, specification AuthorityAccessSpecification) {
	t.Helper()
	findObject := func(want AuthorityObjectSpecification) bool {
		return slices.Contains(specification.Objects, want)
	}
	for _, object := range []AuthorityObjectSpecification{
		{
			Kind:      AuthorityObjectDatabase,
			Name:      "wanwork_im",
			OwnerRole: "database_owner",
		},
		{
			Kind:      AuthorityObjectRelation,
			Schema:    "wanwork_meta",
			Name:      "schema_migrations",
			OwnerRole: "schema_owner",
		},
		{
			Kind:   AuthorityObjectFunction,
			Schema: "wanwork_im",
			Name:   "write_tenant_command_receipt",
			IdentityArguments: "p_tenant_id text, p_command_kind text, p_idempotency_key text, " +
				"p_request_sha256 text, p_result_sha256 text",
			OwnerRole: "schema_owner",
		},
	} {
		if !findObject(object) {
			t.Fatalf("missing object specification %#v", object)
		}
	}

	for _, privilege := range []AuthorityPrivilegeSpecification{
		{
			Scope:       AuthorityPrivilegeDatabase,
			Object:      "wanwork_im",
			GranteeRole: "runtime_login",
			GrantorRole: "database_owner",
			Privilege:   "CONNECT",
		},
		{
			Scope:       AuthorityPrivilegeRelation,
			Schema:      "wanwork_im",
			Object:      "tenant_command_receipts",
			GranteeRole: "runtime_role",
			GrantorRole: "schema_owner",
			Privilege:   "SELECT",
		},
		{
			Scope:  AuthorityPrivilegeFunction,
			Schema: "wanwork_im",
			Object: "write_tenant_command_receipt",
			IdentityArguments: "p_tenant_id text, p_command_kind text, p_idempotency_key text, " +
				"p_request_sha256 text, p_result_sha256 text",
			GranteeRole: "runtime_role",
			GrantorRole: "schema_owner",
			Privilege:   "EXECUTE",
		},
	} {
		if !slices.Contains(specification.Privileges, privilege) {
			t.Fatalf("missing privilege specification %#v", privilege)
		}
	}

	wantDefault := AuthorityDefaultPrivilegeSpecification{
		OwnerRole:   "schema_owner",
		ObjectType:  "FUNCTION",
		GranteeRole: "schema_owner",
		GrantorRole: "schema_owner",
		Privilege:   "EXECUTE",
	}
	if !slices.Contains(specification.DefaultPrivileges, wantDefault) {
		t.Fatalf("missing default privilege %#v", wantDefault)
	}
}

func specificationTestManifest() AuthorityAccessManifest {
	return AuthorityAccessManifest{
		DatabaseName:        "wanwork_im",
		DatabaseOwnerRole:   "database_owner",
		OwnerRole:           "schema_owner",
		MigratorRole:        "migrator_role",
		RuntimeRole:         "runtime_role",
		MigrationLoginRoles: []string{"migration_login"},
		RuntimeLoginRoles:   []string{"runtime_login"},
	}
}
