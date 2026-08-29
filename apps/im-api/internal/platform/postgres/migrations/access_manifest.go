package migrations

import (
	"context"
	"errors"
	"regexp"
	"slices"

	"github.com/jackc/pgx/v5"
)

var (
	ErrInvalidAuthorityAccessManifest = errors.New("invalid PostgreSQL authority access manifest")
	ErrAuthorityAccessDrift           = errors.New("PostgreSQL authority access manifest drift")

	canonicalAccessRoleName = regexp.MustCompile(`^[a-z][a-z0-9_]{0,62}$`)
)

// AuthorityAccessManifest freezes the cluster roles that may enter the migration and runtime
// group roles. Environment login roles are provisioned outside schema migrations and must be
// listed explicitly so an unexpected member cannot silently gain SET ROLE authority.
type AuthorityAccessManifest struct {
	DatabaseName        string
	DatabaseOwnerRole   string
	OwnerRole           string
	MigratorRole        string
	RuntimeRole         string
	MigrationLoginRoles []string
	RuntimeLoginRoles   []string
}

// Validate checks the environment-independent shape of the exact access manifest. It performs no
// database I/O and never normalizes or silently drops a role supplied by the caller.
func (manifest AuthorityAccessManifest) Validate() error {
	if !validAuthorityAccessManifest(manifest) {
		return ErrInvalidAuthorityAccessManifest
	}
	return nil
}

func DefaultAuthorityAccessManifest() AuthorityAccessManifest {
	return AuthorityAccessManifest{
		DatabaseName:      "wanwork_im",
		DatabaseOwnerRole: "wanwork_im_provisioner",
		OwnerRole:         "wanwork_im_owner",
		MigratorRole:      "wanwork_im_migrator",
		RuntimeRole:       "wanwork_im_runtime",
	}
}

// ValidateAuthorityAccess must run on a listed migration login connection after it has selected
// manifest.OwnerRole. Role and database provisioning remain DBA/IaC responsibilities; this
// function is read-only and fails closed instead of attempting to repair ownership, memberships,
// or grants.
func ValidateAuthorityAccess(
	ctx context.Context,
	connection *pgx.Conn,
	manifest AuthorityAccessManifest,
) error {
	return validateAuthorityAccess(ctx, connection, manifest, exactMigrationAuthorityAccess)
}

// ValidateRuntimeAuthorityAccess performs the same exact catalog comparison through a listed
// runtime login after it has selected manifest.RuntimeRole. It allows the long-lived API process
// to fail readiness closed without retaining an owner-capable migration credential. The check is
// read-only and never repairs drift.
func ValidateRuntimeAuthorityAccess(
	ctx context.Context,
	connection *pgx.Conn,
	manifest AuthorityAccessManifest,
) error {
	return validateAuthorityAccess(ctx, connection, manifest, exactRuntimeAuthorityAccess)
}

type authorityAccessComparator func(context.Context, pgx.Tx, AuthorityAccessManifest) bool

func validateAuthorityAccess(
	ctx context.Context,
	connection *pgx.Conn,
	manifest AuthorityAccessManifest,
	compare authorityAccessComparator,
) error {
	if ctx == nil || connection == nil || connection.IsClosed() || manifest.Validate() != nil {
		return ErrInvalidAuthorityAccessManifest
	}
	transaction, err := connection.BeginTx(ctx, pgx.TxOptions{
		IsoLevel:   pgx.RepeatableRead,
		AccessMode: pgx.ReadOnly,
	})
	if err != nil {
		return ErrAuthorityAccessDrift
	}
	defer func() { rollbackMigrationTransaction(transaction) }()
	if err := prepareMigrationTransaction(ctx, transaction); err != nil {
		return ErrAuthorityAccessDrift
	}
	if compare == nil || !compare(ctx, transaction, manifest) {
		return ErrAuthorityAccessDrift
	}
	if err := transaction.Commit(ctx); err != nil {
		return ErrAuthorityAccessDrift
	}
	return nil
}

func validAuthorityAccessManifest(manifest AuthorityAccessManifest) bool {
	core := []string{manifest.OwnerRole, manifest.MigratorRole, manifest.RuntimeRole}
	if len(manifest.MigrationLoginRoles) == 0 || len(manifest.RuntimeLoginRoles) == 0 ||
		!canonicalAccessRoleName.MatchString(manifest.DatabaseName) ||
		!canonicalAccessRoleName.MatchString(manifest.DatabaseOwnerRole) ||
		!uniqueCanonicalAccessRoles(core) ||
		!uniqueCanonicalAccessRoles(manifest.MigrationLoginRoles) ||
		!uniqueCanonicalAccessRoles(manifest.RuntimeLoginRoles) {
		return false
	}
	all := append([]string{manifest.DatabaseOwnerRole}, core...)
	all = append(append(all, manifest.MigrationLoginRoles...), manifest.RuntimeLoginRoles...)
	return uniqueCanonicalAccessRoles(all)
}

func uniqueCanonicalAccessRoles(roles []string) bool {
	seen := make(map[string]struct{}, len(roles))
	for _, role := range roles {
		if !canonicalAccessRoleName.MatchString(role) {
			return false
		}
		if _, exists := seen[role]; exists {
			return false
		}
		seen[role] = struct{}{}
	}
	return true
}

func exactMigrationAuthorityAccess(
	ctx context.Context,
	transaction pgx.Tx,
	manifest AuthorityAccessManifest,
) bool {
	var sessionUser, currentUser string
	if err := transaction.QueryRow(ctx, "SELECT session_user, current_user").Scan(
		&sessionUser,
		&currentUser,
	); err != nil || currentUser != manifest.OwnerRole ||
		!slices.Contains(manifest.MigrationLoginRoles, sessionUser) {
		return false
	}
	return exactAuthorityAccessObjects(ctx, transaction, manifest)
}

func exactRuntimeAuthorityAccess(
	ctx context.Context,
	transaction pgx.Tx,
	manifest AuthorityAccessManifest,
) bool {
	var sessionUser, currentUser string
	if err := transaction.QueryRow(ctx, "SELECT session_user, current_user").Scan(
		&sessionUser,
		&currentUser,
	); err != nil || currentUser != manifest.RuntimeRole ||
		!slices.Contains(manifest.RuntimeLoginRoles, sessionUser) {
		return false
	}
	return exactAuthorityAccessObjects(ctx, transaction, manifest)
}

func exactAuthorityAccessObjects(
	ctx context.Context,
	transaction pgx.Tx,
	manifest AuthorityAccessManifest,
) bool {
	specification, err := CurrentAuthorityAccessSpecification(manifest)
	if err != nil {
		return false
	}
	return exactAuthorityRoles(ctx, transaction, specification) &&
		exactAuthorityMemberships(ctx, transaction, specification) &&
		exactAuthorityDatabasePrivileges(ctx, transaction, specification) &&
		exactAuthorityNamespaces(ctx, transaction, specification) &&
		exactAuthorityRelations(ctx, transaction, specification) &&
		exactAuthorityFunctions(ctx, transaction, specification)
}

func exactAuthorityRoles(
	ctx context.Context,
	transaction pgx.Tx,
	specification AuthorityAccessSpecification,
) bool {
	allRoles := make([]string, 0, len(specification.Roles))
	for _, role := range specification.Roles {
		allRoles = append(allRoles, role.Name)
	}
	rows, err := transaction.Query(ctx, `
SELECT role_value.rolname,
       role_value.rolcanlogin,
       role_value.rolsuper,
       role_value.rolinherit,
       role_value.rolcreaterole,
       role_value.rolcreatedb,
       role_value.rolreplication,
       role_value.rolbypassrls,
       role_value.rolconnlimit,
	   role_value.rolvaliduntil IS NOT NULL,
	   role_value.rolconfig IS NOT NULL
FROM pg_catalog.pg_roles AS role_value
WHERE role_value.rolname = ANY($1::text[])
ORDER BY role_value.rolname`, allRoles)
	if err != nil {
		return false
	}
	defer rows.Close()
	roles := make([]AuthorityRoleSpecification, 0, len(allRoles))
	for rows.Next() {
		var role AuthorityRoleSpecification
		if err := rows.Scan(
			&role.Name,
			&role.Login,
			&role.Superuser,
			&role.Inherit,
			&role.CreateRole,
			&role.CreateDatabase,
			&role.Replication,
			&role.BypassRLS,
			&role.ConnectionLimit,
			&role.ValidUntil,
			&role.Settings,
		); err != nil {
			return false
		}
		roles = append(roles, role)
	}
	return rows.Err() == nil && slices.Equal(roles, specification.Roles) &&
		!specification.RoleSettings && noAuthorityRoleSettings(ctx, transaction, allRoles)
}

func noAuthorityRoleSettings(
	ctx context.Context,
	transaction pgx.Tx,
	roleNames []string,
) bool {
	var clean bool
	err := transaction.QueryRow(ctx, `
SELECT NOT EXISTS (
    SELECT 1
    FROM pg_catalog.pg_db_role_setting AS role_setting
    JOIN pg_catalog.pg_roles AS role_value ON role_value.oid = role_setting.setrole
    WHERE role_value.rolname = ANY($1::text[])
)`, roleNames).Scan(&clean)
	return err == nil && clean
}

func exactAuthorityMemberships(
	ctx context.Context,
	transaction pgx.Tx,
	specification AuthorityAccessSpecification,
) bool {
	allRoles := make([]string, 0, len(specification.Roles))
	for _, role := range specification.Roles {
		allRoles = append(allRoles, role.Name)
	}
	rows, err := transaction.Query(ctx, `
SELECT granted_role.rolname,
       member_role.rolname,
       grantor_role.rolname,
       membership.admin_option,
       membership.inherit_option,
       membership.set_option
FROM pg_catalog.pg_auth_members AS membership
JOIN pg_catalog.pg_roles AS granted_role ON granted_role.oid = membership.roleid
JOIN pg_catalog.pg_roles AS member_role ON member_role.oid = membership.member
JOIN pg_catalog.pg_roles AS grantor_role ON grantor_role.oid = membership.grantor
WHERE granted_role.rolname = ANY($1::text[])
   OR member_role.rolname = ANY($1::text[])
ORDER BY granted_role.rolname, member_role.rolname, grantor_role.rolname`, allRoles)
	if err != nil {
		return false
	}
	defer rows.Close()
	actual := make([]AuthorityMembershipSpecification, 0)
	for rows.Next() {
		var membership AuthorityMembershipSpecification
		if err := rows.Scan(
			&membership.GrantedRole,
			&membership.MemberRole,
			&membership.GrantorRole,
			&membership.AdminOption,
			&membership.InheritOption,
			&membership.SetOption,
		); err != nil {
			return false
		}
		actual = append(actual, membership)
	}
	return rows.Err() == nil && slices.Equal(actual, specification.Memberships)
}

func exactAuthorityDatabasePrivileges(
	ctx context.Context,
	transaction pgx.Tx,
	specification AuthorityAccessSpecification,
) bool {
	databaseObjects := authorityObjectsFor(
		specification,
		AuthorityObjectDatabase,
		"",
	)
	if len(databaseObjects) != 1 {
		return false
	}
	databaseObject := databaseObjects[0]
	var databaseName, databaseOwner string
	if err := transaction.QueryRow(ctx, `
SELECT database_value.datname, owner.rolname
FROM pg_catalog.pg_database AS database_value
JOIN pg_catalog.pg_roles AS owner ON owner.oid = database_value.datdba
WHERE database_value.datname = current_database()`).Scan(
		&databaseName,
		&databaseOwner,
	); err != nil || databaseName != databaseObject.Name ||
		databaseOwner != databaseObject.OwnerRole {
		return false
	}
	for _, role := range specification.Roles {
		var connect, createDatabaseObject, temporary bool
		if err := transaction.QueryRow(ctx, `
SELECT pg_catalog.has_database_privilege($1, current_database(), 'CONNECT'),
       pg_catalog.has_database_privilege($1, current_database(), 'CREATE'),
	   pg_catalog.has_database_privilege($1, current_database(), 'TEMPORARY')`, role.Name).Scan(
			&connect,
			&createDatabaseObject,
			&temporary,
		); err != nil || temporary {
			return false
		}
		mustConnect := authorityRoleHasPrivilege(
			specification,
			AuthorityPrivilegeDatabase,
			databaseName,
			role.Name,
			"CONNECT",
		)
		mustCreate := authorityRoleHasPrivilege(
			specification,
			AuthorityPrivilegeDatabase,
			databaseName,
			role.Name,
			"CREATE",
		)
		if connect != mustConnect || createDatabaseObject != mustCreate {
			return false
		}
	}
	actual, ok := readNonOwnerDatabaseACL(ctx, transaction, databaseName)
	if !ok {
		return false
	}
	expected := authorityPrivilegesFor(specification, AuthorityPrivilegeDatabase)
	return slices.Equal(actual, expected)
}

func readNonOwnerDatabaseACL(
	ctx context.Context,
	transaction pgx.Tx,
	databaseName string,
) ([]AuthorityPrivilegeSpecification, bool) {
	rows, err := transaction.Query(ctx, `
SELECT database_value.datname,
       COALESCE(grantee.rolname, ''),
       COALESCE(grantor.rolname, ''),
       acl.privilege_type,
       acl.is_grantable
FROM pg_catalog.pg_database AS database_value
CROSS JOIN LATERAL pg_catalog.aclexplode(
    COALESCE(database_value.datacl, pg_catalog.acldefault('d', database_value.datdba))
) AS acl
LEFT JOIN pg_catalog.pg_roles AS grantee ON grantee.oid = acl.grantee
LEFT JOIN pg_catalog.pg_roles AS grantor ON grantor.oid = acl.grantor
WHERE database_value.datname = current_database()
  AND acl.grantee <> database_value.datdba
ORDER BY grantee.rolname, grantor.rolname, acl.privilege_type`)
	if err != nil {
		return nil, false
	}
	defer rows.Close()
	values := make([]AuthorityPrivilegeSpecification, 0)
	for rows.Next() {
		value := AuthorityPrivilegeSpecification{Scope: AuthorityPrivilegeDatabase}
		if err := rows.Scan(
			&value.Object,
			&value.GranteeRole,
			&value.GrantorRole,
			&value.Privilege,
			&value.Grantable,
		); err != nil || value.Object != databaseName || value.GranteeRole == "" ||
			value.GrantorRole == "" || value.Grantable {
			return nil, false
		}
		values = append(values, value)
	}
	slices.SortFunc(values, compareAuthorityPrivilegeSpecification)
	return values, rows.Err() == nil
}

func exactAuthorityNamespaces(
	ctx context.Context,
	transaction pgx.Tx,
	specification AuthorityAccessSpecification,
) bool {
	expectedObjects := authorityObjectsFor(specification, AuthorityObjectSchema, "")
	names := make([]string, 0, len(expectedObjects))
	for _, object := range expectedObjects {
		names = append(names, object.Name)
	}
	rows, err := transaction.Query(ctx, `
SELECT namespace.nspname, owner.rolname
FROM pg_catalog.pg_namespace AS namespace
JOIN pg_catalog.pg_roles AS owner ON owner.oid = namespace.nspowner
WHERE namespace.nspname = ANY($1::text[])
ORDER BY namespace.nspname`, names)
	if err != nil {
		return false
	}
	defer rows.Close()
	actualObjects := make([]AuthorityObjectSpecification, 0, len(expectedObjects))
	for rows.Next() {
		object := AuthorityObjectSpecification{Kind: AuthorityObjectSchema}
		if err := rows.Scan(&object.Name, &object.OwnerRole); err != nil {
			return false
		}
		actualObjects = append(actualObjects, object)
	}
	if rows.Err() != nil || !slices.Equal(actualObjects, expectedObjects) {
		return false
	}
	actual, ok := readNonOwnerSchemaACL(ctx, transaction, expectedObjects)
	if !ok {
		return false
	}
	expected := authorityPrivilegesFor(specification, AuthorityPrivilegeSchema)
	return slices.Equal(actual, expected)
}

func readNonOwnerSchemaACL(
	ctx context.Context,
	transaction pgx.Tx,
	objects []AuthorityObjectSpecification,
) ([]AuthorityPrivilegeSpecification, bool) {
	names := make([]string, 0, len(objects))
	owners := make(map[string]string, len(objects))
	for _, object := range objects {
		names = append(names, object.Name)
		owners[object.Name] = object.OwnerRole
	}
	rows, err := transaction.Query(ctx, `
SELECT namespace.nspname,
       COALESCE(grantee.rolname, ''),
       COALESCE(grantor.rolname, ''),
       acl.privilege_type,
       acl.is_grantable
FROM pg_catalog.pg_namespace AS namespace
CROSS JOIN LATERAL pg_catalog.aclexplode(
    COALESCE(namespace.nspacl, pg_catalog.acldefault('n', namespace.nspowner))
) AS acl
LEFT JOIN pg_catalog.pg_roles AS grantee ON grantee.oid = acl.grantee
LEFT JOIN pg_catalog.pg_roles AS grantor ON grantor.oid = acl.grantor
WHERE namespace.nspname = ANY($1::text[])
  AND acl.grantee <> namespace.nspowner
ORDER BY namespace.nspname, grantee.rolname, grantor.rolname, acl.privilege_type`, names)
	if err != nil {
		return nil, false
	}
	defer rows.Close()
	values := make([]AuthorityPrivilegeSpecification, 0)
	for rows.Next() {
		value := AuthorityPrivilegeSpecification{Scope: AuthorityPrivilegeSchema}
		if err := rows.Scan(
			&value.Object,
			&value.GranteeRole,
			&value.GrantorRole,
			&value.Privilege,
			&value.Grantable,
		); err != nil || value.GranteeRole == "" || value.GrantorRole == "" ||
			value.GranteeRole == owners[value.Object] || value.Grantable {
			return nil, false
		}
		values = append(values, value)
	}
	slices.SortFunc(values, compareAuthorityPrivilegeSpecification)
	return values, rows.Err() == nil
}

func exactAuthorityRelations(
	ctx context.Context,
	transaction pgx.Tx,
	specification AuthorityAccessSpecification,
) bool {
	expectedObjects := authorityObjectsFor(specification, AuthorityObjectRelation, "")
	schemas := authoritySchemasForObjects(expectedObjects)
	rows, err := transaction.Query(ctx, `
SELECT namespace.nspname, relation.relname, relation.relkind::text, owner.rolname
FROM pg_catalog.pg_class AS relation
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
JOIN pg_catalog.pg_roles AS owner ON owner.oid = relation.relowner
WHERE namespace.nspname = ANY($1::text[])
  AND relation.relkind = ANY($2::"char"[])
ORDER BY namespace.nspname, relation.relname`, schemas, []string{"r", "p", "v", "m", "S", "f"})
	if err != nil {
		return false
	}
	defer rows.Close()
	actualObjects := make([]AuthorityObjectSpecification, 0, len(expectedObjects))
	for rows.Next() {
		object := AuthorityObjectSpecification{Kind: AuthorityObjectRelation}
		var relationKind string
		if err := rows.Scan(
			&object.Schema,
			&object.Name,
			&relationKind,
			&object.OwnerRole,
		); err != nil || relationKind != "r" {
			return false
		}
		actualObjects = append(actualObjects, object)
	}
	if rows.Err() != nil || !slices.Equal(actualObjects, expectedObjects) {
		return false
	}
	actualACL, ok := readNonOwnerTableACL(ctx, transaction, expectedObjects)
	if !ok {
		return false
	}
	expectedACL := authorityPrivilegesFor(specification, AuthorityPrivilegeRelation)
	return slices.Equal(actualACL, expectedACL) && !specification.ColumnPrivileges &&
		noNonOwnerColumnACL(ctx, transaction, schemas)
}

var runtimeAuthorityReadTables = []string{
	"conversation_access_heads",
	"conversation_access_snapshots",
	"conversation_heads",
	"conversation_membership_heads",
	"conversation_membership_snapshots",
	"conversation_snapshots",
	"provider_conversation_binding_heads",
	"provider_conversation_binding_snapshots",
	"tenant_command_receipts",
	"event_stream_heads",
	"event_tenant_heads",
	"event_log",
}

func authorityAccessTableNames() []string {
	names := make([]string, 0, 25)
	for _, spec := range authorityRootSpecs() {
		names = append(names, spec.name)
	}
	names = append(names, identityAuthorityTableNames...)
	names = append(names, conversationTableNames...)
	names = append(names, conversationAuthorityTableNames...)
	names = append(names, eventStoreTableNames...)
	slices.Sort(names)
	return names
}

func readNonOwnerTableACL(
	ctx context.Context,
	transaction pgx.Tx,
	objects []AuthorityObjectSpecification,
) ([]AuthorityPrivilegeSpecification, bool) {
	schemas := make([]string, 0)
	owners := make(map[string]string, len(objects))
	for _, object := range objects {
		if !slices.Contains(schemas, object.Schema) {
			schemas = append(schemas, object.Schema)
		}
		owners[object.Schema+"\x00"+object.Name] = object.OwnerRole
	}
	rows, err := transaction.Query(ctx, `
SELECT namespace.nspname,
       relation.relname,
       COALESCE(grantee.rolname, ''),
       COALESCE(grantor.rolname, ''),
       acl.privilege_type,
       acl.is_grantable
FROM pg_catalog.pg_class AS relation
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
CROSS JOIN LATERAL pg_catalog.aclexplode(
    COALESCE(relation.relacl, pg_catalog.acldefault('r', relation.relowner))
) AS acl
LEFT JOIN pg_catalog.pg_roles AS grantee ON grantee.oid = acl.grantee
LEFT JOIN pg_catalog.pg_roles AS grantor ON grantor.oid = acl.grantor
WHERE namespace.nspname = ANY($1::text[])
  AND relation.relkind = 'r'
  AND acl.grantee <> relation.relowner
ORDER BY namespace.nspname, relation.relname, grantee.rolname, grantor.rolname, acl.privilege_type`, schemas)
	if err != nil {
		return nil, false
	}
	defer rows.Close()
	values := make([]AuthorityPrivilegeSpecification, 0)
	for rows.Next() {
		value := AuthorityPrivilegeSpecification{Scope: AuthorityPrivilegeRelation}
		if err := rows.Scan(
			&value.Schema,
			&value.Object,
			&value.GranteeRole,
			&value.GrantorRole,
			&value.Privilege,
			&value.Grantable,
		); err != nil || value.GranteeRole == "" || value.GrantorRole == "" ||
			value.GranteeRole == owners[value.Schema+"\x00"+value.Object] || value.Grantable {
			return nil, false
		}
		values = append(values, value)
	}
	slices.SortFunc(values, compareAuthorityPrivilegeSpecification)
	return values, rows.Err() == nil
}

func noNonOwnerColumnACL(
	ctx context.Context,
	transaction pgx.Tx,
	schemas []string,
) bool {
	var clean bool
	err := transaction.QueryRow(ctx, `
SELECT NOT EXISTS (
    SELECT 1
    FROM pg_catalog.pg_attribute AS attribute
    JOIN pg_catalog.pg_class AS relation ON relation.oid = attribute.attrelid
    JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
    CROSS JOIN LATERAL pg_catalog.aclexplode(attribute.attacl) AS acl
    WHERE namespace.nspname = ANY($1::text[])
      AND attribute.attnum > 0
      AND NOT attribute.attisdropped
      AND attribute.attacl IS NOT NULL
      AND acl.grantee <> relation.relowner
)`, schemas).Scan(&clean)
	return err == nil && clean
}

func exactAuthorityFunctions(
	ctx context.Context,
	transaction pgx.Tx,
	specification AuthorityAccessSpecification,
) bool {
	expectedObjects := authorityObjectsFor(specification, AuthorityObjectFunction, "")
	if len(expectedObjects) == 0 {
		return false
	}
	ownerRole := expectedObjects[0].OwnerRole
	if validateFunctionOnlyWritesForOwner(ctx, transaction, ownerRole) != nil ||
		!exactOwnerFunctionDefaultPrivileges(ctx, transaction, specification.DefaultPrivileges) ||
		specification.FunctionsInMetadata {
		return false
	}
	managedSchemas := make([]string, 0)
	for _, object := range authorityObjectsFor(specification, AuthorityObjectSchema, "") {
		managedSchemas = append(managedSchemas, object.Name)
	}
	rows, err := transaction.Query(ctx, `
SELECT namespace.nspname,
       procedure.proname,
       pg_catalog.pg_get_function_identity_arguments(procedure.oid),
       owner.rolname
FROM pg_catalog.pg_proc AS procedure
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
JOIN pg_catalog.pg_roles AS owner ON owner.oid = procedure.proowner
WHERE namespace.nspname = ANY($1::text[])
ORDER BY namespace.nspname,
         procedure.proname,
         pg_catalog.pg_get_function_identity_arguments(procedure.oid)`, managedSchemas)
	if err != nil {
		return false
	}
	defer rows.Close()
	actualObjects := make([]AuthorityObjectSpecification, 0, len(expectedObjects))
	for rows.Next() {
		object := AuthorityObjectSpecification{Kind: AuthorityObjectFunction}
		if err := rows.Scan(
			&object.Schema,
			&object.Name,
			&object.IdentityArguments,
			&object.OwnerRole,
		); err != nil {
			return false
		}
		actualObjects = append(actualObjects, object)
	}
	if rows.Err() != nil || !slices.Equal(actualObjects, expectedObjects) {
		return false
	}
	actualACL, ok := readNonOwnerFunctionACL(ctx, transaction, expectedObjects)
	if !ok {
		return false
	}
	expectedACL := authorityPrivilegesFor(specification, AuthorityPrivilegeFunction)
	return slices.Equal(actualACL, expectedACL)
}

func exactOwnerFunctionDefaultPrivileges(
	ctx context.Context,
	transaction pgx.Tx,
	expected []AuthorityDefaultPrivilegeSpecification,
) bool {
	if len(expected) != 1 || expected[0].ObjectType != "FUNCTION" ||
		expected[0].Schema != "" || expected[0].GranteeRole != expected[0].OwnerRole ||
		expected[0].GrantorRole != expected[0].OwnerRole || expected[0].Privilege != "EXECUTE" ||
		expected[0].Grantable {
		return false
	}
	ownerRole := expected[0].OwnerRole
	var exact bool
	err := transaction.QueryRow(ctx, `
SELECT (
           SELECT count(*) = 1
                  AND count(*) FILTER (
                      WHERE default_acl.defaclobjtype = 'f'
                        AND default_acl.defaclnamespace = 0
                  ) = 1
           FROM pg_catalog.pg_default_acl AS default_acl
           WHERE default_acl.defaclrole = owner.oid
       )
       AND (
           SELECT count(*) = 1
           FROM pg_catalog.pg_default_acl AS default_acl
           CROSS JOIN LATERAL pg_catalog.aclexplode(default_acl.defaclacl) AS acl
           WHERE default_acl.defaclrole = owner.oid
       )
       AND EXISTS (
           SELECT 1
           FROM pg_catalog.pg_default_acl AS default_acl
           CROSS JOIN LATERAL pg_catalog.aclexplode(default_acl.defaclacl) AS acl
           WHERE default_acl.defaclrole = owner.oid
             AND default_acl.defaclobjtype = 'f'
             AND default_acl.defaclnamespace = 0
             AND acl.grantee = owner.oid
             AND acl.grantor = owner.oid
             AND acl.privilege_type = 'EXECUTE'
             AND NOT acl.is_grantable
       )
FROM pg_catalog.pg_roles AS owner
WHERE owner.rolname = $1
`, ownerRole).Scan(&exact)
	return err == nil && exact
}

func readNonOwnerFunctionACL(
	ctx context.Context,
	transaction pgx.Tx,
	objects []AuthorityObjectSpecification,
) ([]AuthorityPrivilegeSpecification, bool) {
	owners := make(map[string]string, len(objects))
	schemas := make([]string, 0)
	for _, object := range objects {
		if !slices.Contains(schemas, object.Schema) {
			schemas = append(schemas, object.Schema)
		}
		owners[object.Schema+"\x00"+object.Name+"\x00"+object.IdentityArguments] = object.OwnerRole
	}
	rows, err := transaction.Query(ctx, `
SELECT namespace.nspname,
       procedure.proname,
       pg_catalog.pg_get_function_identity_arguments(procedure.oid),
       COALESCE(grantee.rolname, ''),
       COALESCE(grantor.rolname, ''),
       acl.privilege_type,
       acl.is_grantable
FROM pg_catalog.pg_proc AS procedure
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
CROSS JOIN LATERAL pg_catalog.aclexplode(
    COALESCE(procedure.proacl, pg_catalog.acldefault('f', procedure.proowner))
) AS acl
LEFT JOIN pg_catalog.pg_roles AS grantee ON grantee.oid = acl.grantee
LEFT JOIN pg_catalog.pg_roles AS grantor ON grantor.oid = acl.grantor
WHERE namespace.nspname = ANY($1::text[])
  AND acl.grantee <> procedure.proowner
ORDER BY namespace.nspname,
         procedure.proname,
         pg_catalog.pg_get_function_identity_arguments(procedure.oid),
         grantee.rolname,
         grantor.rolname,
         acl.privilege_type`, schemas)
	if err != nil {
		return nil, false
	}
	defer rows.Close()
	values := make([]AuthorityPrivilegeSpecification, 0)
	for rows.Next() {
		value := AuthorityPrivilegeSpecification{Scope: AuthorityPrivilegeFunction}
		if err := rows.Scan(
			&value.Schema,
			&value.Object,
			&value.IdentityArguments,
			&value.GranteeRole,
			&value.GrantorRole,
			&value.Privilege,
			&value.Grantable,
		); err != nil || value.GranteeRole == "" || value.GrantorRole == "" ||
			value.GranteeRole == owners[value.Schema+"\x00"+value.Object+"\x00"+value.IdentityArguments] || value.Grantable {
			return nil, false
		}
		values = append(values, value)
	}
	slices.SortFunc(values, compareAuthorityPrivilegeSpecification)
	return values, rows.Err() == nil
}

func authorityObjectsFor(
	specification AuthorityAccessSpecification,
	kind AuthorityObjectKind,
	schema string,
) []AuthorityObjectSpecification {
	values := make([]AuthorityObjectSpecification, 0)
	for _, object := range specification.Objects {
		if object.Kind == kind && (schema == "" || object.Schema == schema) {
			values = append(values, object)
		}
	}
	return values
}

func authorityPrivilegesFor(
	specification AuthorityAccessSpecification,
	scope AuthorityPrivilegeScope,
) []AuthorityPrivilegeSpecification {
	values := make([]AuthorityPrivilegeSpecification, 0)
	for _, privilege := range specification.Privileges {
		if privilege.Scope == scope {
			values = append(values, privilege)
		}
	}
	return values
}

func authoritySchemasForObjects(objects []AuthorityObjectSpecification) []string {
	values := make([]string, 0)
	for _, object := range objects {
		if object.Schema != "" && !slices.Contains(values, object.Schema) {
			values = append(values, object.Schema)
		}
	}
	slices.Sort(values)
	return values
}

func authorityRoleHasPrivilege(
	specification AuthorityAccessSpecification,
	scope AuthorityPrivilegeScope,
	object string,
	grantee string,
	privilegeName string,
) bool {
	for _, privilege := range specification.Privileges {
		if privilege.Scope == scope && privilege.Object == object &&
			privilege.GranteeRole == grantee && privilege.Privilege == privilegeName {
			return true
		}
	}
	return false
}

func authorityAccessRoleNames(manifest AuthorityAccessManifest) []string {
	roles := []string{manifest.OwnerRole, manifest.MigratorRole, manifest.RuntimeRole}
	roles = append(roles, manifest.MigrationLoginRoles...)
	roles = append(roles, manifest.RuntimeLoginRoles...)
	slices.Sort(roles)
	return roles
}
