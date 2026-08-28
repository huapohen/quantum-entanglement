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
	DatabaseOwnerRole   string
	OwnerRole           string
	MigratorRole        string
	RuntimeRole         string
	MigrationLoginRoles []string
	RuntimeLoginRoles   []string
}

func DefaultAuthorityAccessManifest() AuthorityAccessManifest {
	return AuthorityAccessManifest{
		DatabaseOwnerRole: "wanwork_im_provisioner",
		OwnerRole:         "wanwork_im_owner",
		MigratorRole:      "wanwork_im_migrator",
		RuntimeRole:       "wanwork_im_runtime",
	}
}

// ValidateAuthorityAccess must run on a connection whose current role is manifest.OwnerRole.
// Role and database provisioning remain DBA/IaC responsibilities; this function is read-only and
// fails closed instead of attempting to repair ownership, memberships, or grants.
func ValidateAuthorityAccess(
	ctx context.Context,
	connection *pgx.Conn,
	manifest AuthorityAccessManifest,
) error {
	if ctx == nil || connection == nil || connection.IsClosed() || !validAuthorityAccessManifest(manifest) {
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
	if !exactAuthorityAccess(ctx, transaction, manifest) {
		return ErrAuthorityAccessDrift
	}
	if err := transaction.Commit(ctx); err != nil {
		return ErrAuthorityAccessDrift
	}
	return nil
}

func validAuthorityAccessManifest(manifest AuthorityAccessManifest) bool {
	core := []string{manifest.OwnerRole, manifest.MigratorRole, manifest.RuntimeRole}
	if !canonicalAccessRoleName.MatchString(manifest.DatabaseOwnerRole) ||
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

func exactAuthorityAccess(
	ctx context.Context,
	transaction pgx.Tx,
	manifest AuthorityAccessManifest,
) bool {
	var currentUser string
	if err := transaction.QueryRow(ctx, "SELECT current_user").Scan(&currentUser); err != nil ||
		currentUser != manifest.OwnerRole {
		return false
	}
	return exactAuthorityRoles(ctx, transaction, manifest) &&
		exactAuthorityMemberships(ctx, transaction, manifest) &&
		exactAuthorityDatabasePrivileges(ctx, transaction, manifest) &&
		exactAuthorityNamespaces(ctx, transaction, manifest) &&
		exactAuthorityRelations(ctx, transaction, manifest) &&
		exactAuthorityFunctions(ctx, transaction, manifest)
}

type authorityAccessRole struct {
	name          string
	login         bool
	superuser     bool
	inherit       bool
	createRole    bool
	createDB      bool
	replication   bool
	bypassRLS     bool
	connectionCap int
	noValidUntil  bool
	noSettings    bool
}

func exactAuthorityRoles(
	ctx context.Context,
	transaction pgx.Tx,
	manifest AuthorityAccessManifest,
) bool {
	allRoles := authorityAccessRoleNames(manifest)
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
       role_value.rolvaliduntil IS NULL,
       role_value.rolconfig IS NULL
FROM pg_catalog.pg_roles AS role_value
WHERE role_value.rolname = ANY($1::text[])
ORDER BY role_value.rolname`, allRoles)
	if err != nil {
		return false
	}
	defer rows.Close()
	roles := make(map[string]authorityAccessRole, len(allRoles))
	for rows.Next() {
		var role authorityAccessRole
		if err := rows.Scan(
			&role.name,
			&role.login,
			&role.superuser,
			&role.inherit,
			&role.createRole,
			&role.createDB,
			&role.replication,
			&role.bypassRLS,
			&role.connectionCap,
			&role.noValidUntil,
			&role.noSettings,
		); err != nil {
			return false
		}
		roles[role.name] = role
	}
	if rows.Err() != nil || len(roles) != len(allRoles) {
		return false
	}
	loginRoles := make(map[string]struct{}, len(manifest.MigrationLoginRoles)+len(manifest.RuntimeLoginRoles))
	for _, name := range append(append([]string(nil), manifest.MigrationLoginRoles...), manifest.RuntimeLoginRoles...) {
		loginRoles[name] = struct{}{}
	}
	for _, name := range allRoles {
		role := roles[name]
		_, mustLogin := loginRoles[name]
		if role.login != mustLogin || role.superuser || role.inherit || role.createRole ||
			role.createDB || role.replication || role.bypassRLS || role.connectionCap != -1 ||
			!role.noValidUntil || !role.noSettings {
			return false
		}
	}
	return noAuthorityRoleSettings(ctx, transaction, allRoles)
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

type authorityAccessMembership struct {
	granted string
	member  string
	grantor string
	admin   bool
	inherit bool
	setRole bool
}

func exactAuthorityMemberships(
	ctx context.Context,
	transaction pgx.Tx,
	manifest AuthorityAccessManifest,
) bool {
	allRoles := authorityAccessRoleNames(manifest)
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
	actual := make([]authorityAccessMembership, 0)
	for rows.Next() {
		var membership authorityAccessMembership
		if err := rows.Scan(
			&membership.granted,
			&membership.member,
			&membership.grantor,
			&membership.admin,
			&membership.inherit,
			&membership.setRole,
		); err != nil {
			return false
		}
		actual = append(actual, membership)
	}
	if rows.Err() != nil {
		return false
	}
	expected := []authorityAccessMembership{{
		granted: manifest.OwnerRole,
		member:  manifest.MigratorRole,
		grantor: manifest.DatabaseOwnerRole,
		setRole: true,
	}}
	for _, member := range manifest.MigrationLoginRoles {
		expected = append(expected, authorityAccessMembership{
			granted: manifest.MigratorRole,
			member:  member,
			grantor: manifest.DatabaseOwnerRole,
			setRole: true,
		})
	}
	for _, member := range manifest.RuntimeLoginRoles {
		expected = append(expected, authorityAccessMembership{
			granted: manifest.RuntimeRole,
			member:  member,
			grantor: manifest.DatabaseOwnerRole,
			setRole: true,
		})
	}
	slices.SortFunc(expected, compareAuthorityAccessMembership)
	return slices.Equal(actual, expected)
}

func compareAuthorityAccessMembership(left, right authorityAccessMembership) int {
	if left.granted != right.granted {
		if left.granted < right.granted {
			return -1
		}
		return 1
	}
	if left.member < right.member {
		return -1
	}
	if left.member > right.member {
		return 1
	}
	if left.grantor < right.grantor {
		return -1
	}
	if left.grantor > right.grantor {
		return 1
	}
	return 0
}

func exactAuthorityDatabasePrivileges(
	ctx context.Context,
	transaction pgx.Tx,
	manifest AuthorityAccessManifest,
) bool {
	var databaseName, databaseOwner string
	if err := transaction.QueryRow(ctx, `
SELECT database_value.datname, owner.rolname
FROM pg_catalog.pg_database AS database_value
JOIN pg_catalog.pg_roles AS owner ON owner.oid = database_value.datdba
WHERE database_value.datname = current_database()`).Scan(
		&databaseName,
		&databaseOwner,
	); err != nil || databaseOwner != manifest.DatabaseOwnerRole {
		return false
	}
	roles := authorityAccessRoleNames(manifest)
	loginRoles := make(map[string]struct{}, len(manifest.MigrationLoginRoles)+len(manifest.RuntimeLoginRoles))
	for _, role := range append(append([]string(nil), manifest.MigrationLoginRoles...), manifest.RuntimeLoginRoles...) {
		loginRoles[role] = struct{}{}
	}
	for _, role := range roles {
		var connect, createDatabaseObject, temporary bool
		if err := transaction.QueryRow(ctx, `
SELECT pg_catalog.has_database_privilege($1, current_database(), 'CONNECT'),
       pg_catalog.has_database_privilege($1, current_database(), 'CREATE'),
       pg_catalog.has_database_privilege($1, current_database(), 'TEMPORARY')`, role).Scan(
			&connect,
			&createDatabaseObject,
			&temporary,
		); err != nil || temporary || createDatabaseObject != (role == manifest.OwnerRole) {
			return false
		}
		_, mustConnect := loginRoles[role]
		if connect != mustConnect {
			return false
		}
	}
	actual, ok := readNonOwnerDatabaseACL(ctx, transaction, databaseName)
	if !ok {
		return false
	}
	expected := []authorityAccessACL{{
		object:    databaseName,
		grantee:   manifest.OwnerRole,
		grantor:   manifest.DatabaseOwnerRole,
		privilege: "CREATE",
	}}
	for role := range loginRoles {
		expected = append(expected, authorityAccessACL{
			object:    databaseName,
			grantee:   role,
			grantor:   manifest.DatabaseOwnerRole,
			privilege: "CONNECT",
		})
	}
	slices.SortFunc(expected, compareAuthorityAccessACL)
	return slices.Equal(actual, expected)
}

func readNonOwnerDatabaseACL(
	ctx context.Context,
	transaction pgx.Tx,
	databaseName string,
) ([]authorityAccessACL, bool) {
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
	values := make([]authorityAccessACL, 0)
	for rows.Next() {
		var value authorityAccessACL
		if err := rows.Scan(
			&value.object,
			&value.grantee,
			&value.grantor,
			&value.privilege,
			&value.grantable,
		); err != nil || value.object != databaseName || value.grantee == "" ||
			value.grantor == "" || value.grantable {
			return nil, false
		}
		values = append(values, value)
	}
	return values, rows.Err() == nil
}

func exactAuthorityNamespaces(
	ctx context.Context,
	transaction pgx.Tx,
	manifest AuthorityAccessManifest,
) bool {
	rows, err := transaction.Query(ctx, `
SELECT namespace.nspname, owner.rolname
FROM pg_catalog.pg_namespace AS namespace
JOIN pg_catalog.pg_roles AS owner ON owner.oid = namespace.nspowner
WHERE namespace.nspname = ANY($1::text[])
ORDER BY namespace.nspname`, []string{"wanwork_im", "wanwork_meta"})
	if err != nil {
		return false
	}
	defer rows.Close()
	count := 0
	for rows.Next() {
		var name, owner string
		if err := rows.Scan(&name, &owner); err != nil || owner != manifest.OwnerRole {
			return false
		}
		count++
	}
	if rows.Err() != nil || count != 2 {
		return false
	}
	actual, ok := readNonOwnerSchemaACL(ctx, transaction, manifest.OwnerRole)
	if !ok {
		return false
	}
	expected := []authorityAccessACL{{
		object:    "wanwork_im",
		grantee:   manifest.RuntimeRole,
		grantor:   manifest.OwnerRole,
		privilege: "USAGE",
	}}
	return slices.Equal(actual, expected)
}

type authorityAccessACL struct {
	object    string
	grantee   string
	grantor   string
	privilege string
	grantable bool
}

func compareAuthorityAccessACL(left, right authorityAccessACL) int {
	for _, values := range [][2]string{
		{left.object, right.object},
		{left.grantee, right.grantee},
		{left.grantor, right.grantor},
		{left.privilege, right.privilege},
	} {
		if values[0] < values[1] {
			return -1
		}
		if values[0] > values[1] {
			return 1
		}
	}
	return 0
}

func readNonOwnerSchemaACL(
	ctx context.Context,
	transaction pgx.Tx,
	ownerRole string,
) ([]authorityAccessACL, bool) {
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
ORDER BY namespace.nspname, grantee.rolname, grantor.rolname, acl.privilege_type`, []string{"wanwork_im", "wanwork_meta"})
	if err != nil {
		return nil, false
	}
	defer rows.Close()
	values := make([]authorityAccessACL, 0)
	for rows.Next() {
		var value authorityAccessACL
		if err := rows.Scan(
			&value.object,
			&value.grantee,
			&value.grantor,
			&value.privilege,
			&value.grantable,
		); err != nil || value.grantee == "" || value.grantor == "" ||
			value.grantee == ownerRole || value.grantable {
			return nil, false
		}
		values = append(values, value)
	}
	return values, rows.Err() == nil
}

func exactAuthorityRelations(
	ctx context.Context,
	transaction pgx.Tx,
	manifest AuthorityAccessManifest,
) bool {
	tableNames := authorityAccessTableNames()
	rows, err := transaction.Query(ctx, `
SELECT relation.relname, relation.relkind::text, owner.rolname
FROM pg_catalog.pg_class AS relation
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
JOIN pg_catalog.pg_roles AS owner ON owner.oid = relation.relowner
WHERE namespace.nspname = 'wanwork_im'
  AND relation.relkind = ANY($1::"char"[])
ORDER BY relation.relname`, []string{"r", "p", "v", "m", "S", "f"})
	if err != nil {
		return false
	}
	defer rows.Close()
	actualNames := make([]string, 0, len(tableNames))
	for rows.Next() {
		var name, kind, owner string
		if err := rows.Scan(&name, &kind, &owner); err != nil || kind != "r" || owner != manifest.OwnerRole {
			return false
		}
		actualNames = append(actualNames, name)
	}
	if rows.Err() != nil || !slices.Equal(actualNames, tableNames) {
		return false
	}
	var exactMetaRelations bool
	if err := transaction.QueryRow(ctx, `
SELECT count(*) = 1
       AND count(*) FILTER (
           WHERE relation.relname = 'schema_migrations'
             AND relation.relkind = 'r'
             AND owner.rolname = $1
       ) = 1
FROM pg_catalog.pg_class AS relation
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
JOIN pg_catalog.pg_roles AS owner ON owner.oid = relation.relowner
WHERE namespace.nspname = 'wanwork_meta'
	  AND relation.relkind = ANY($2::"char"[])`, manifest.OwnerRole, []string{"r", "p", "v", "m", "S", "f"}).Scan(
		&exactMetaRelations,
	); err != nil || !exactMetaRelations {
		return false
	}
	actualACL, ok := readNonOwnerTableACL(ctx, transaction, manifest.OwnerRole)
	if !ok {
		return false
	}
	expectedACL := make([]authorityAccessACL, 0, len(runtimeAuthorityReadTables))
	for _, tableName := range runtimeAuthorityReadTables {
		expectedACL = append(expectedACL, authorityAccessACL{
			object:    tableName,
			grantee:   manifest.RuntimeRole,
			grantor:   manifest.OwnerRole,
			privilege: "SELECT",
		})
	}
	return slices.Equal(actualACL, expectedACL) && noNonOwnerColumnACL(ctx, transaction)
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
}

func authorityAccessTableNames() []string {
	names := make([]string, 0, 22)
	for _, spec := range authorityRootSpecs() {
		names = append(names, spec.name)
	}
	names = append(names, identityAuthorityTableNames...)
	names = append(names, conversationTableNames...)
	names = append(names, conversationAuthorityTableNames...)
	slices.Sort(names)
	return names
}

func readNonOwnerTableACL(
	ctx context.Context,
	transaction pgx.Tx,
	ownerRole string,
) ([]authorityAccessACL, bool) {
	rows, err := transaction.Query(ctx, `
SELECT relation.relname,
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
ORDER BY relation.relname, grantee.rolname, grantor.rolname, acl.privilege_type`, []string{"wanwork_im", "wanwork_meta"})
	if err != nil {
		return nil, false
	}
	defer rows.Close()
	values := make([]authorityAccessACL, 0)
	for rows.Next() {
		var value authorityAccessACL
		if err := rows.Scan(
			&value.object,
			&value.grantee,
			&value.grantor,
			&value.privilege,
			&value.grantable,
		); err != nil || value.grantee == "" || value.grantor == "" ||
			value.grantee == ownerRole || value.grantable {
			return nil, false
		}
		values = append(values, value)
	}
	return values, rows.Err() == nil
}

func noNonOwnerColumnACL(ctx context.Context, transaction pgx.Tx) bool {
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
)`, []string{"wanwork_im", "wanwork_meta"}).Scan(&clean)
	return err == nil && clean
}

func exactAuthorityFunctions(
	ctx context.Context,
	transaction pgx.Tx,
	manifest AuthorityAccessManifest,
) bool {
	if validateFunctionOnlyWrites(ctx, transaction) != nil ||
		!exactOwnerFunctionDefaultPrivileges(ctx, transaction, manifest.OwnerRole) ||
		!noAuthorityMetaFunctions(ctx, transaction) {
		return false
	}
	rows, err := transaction.Query(ctx, `
SELECT procedure.proname,
       pg_catalog.pg_get_function_identity_arguments(procedure.oid),
       owner.rolname
FROM pg_catalog.pg_proc AS procedure
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
JOIN pg_catalog.pg_roles AS owner ON owner.oid = procedure.proowner
WHERE namespace.nspname = 'wanwork_im'
ORDER BY procedure.proname, pg_catalog.pg_get_function_identity_arguments(procedure.oid)`)
	if err != nil {
		return false
	}
	defer rows.Close()
	specs := storedAuthorityFunctionManifest()
	index := 0
	for rows.Next() {
		var name, identityArguments, owner string
		if err := rows.Scan(&name, &identityArguments, &owner); err != nil || index >= len(specs) ||
			name != specs[index].name || identityArguments != specs[index].identityArguments ||
			owner != manifest.OwnerRole {
			return false
		}
		index++
	}
	if rows.Err() != nil || index != len(specs) {
		return false
	}
	actualACL, ok := readNonOwnerFunctionACL(ctx, transaction, manifest.OwnerRole)
	if !ok {
		return false
	}
	expectedACL := make([]authorityAccessACL, 0, len(specs))
	for _, spec := range specs {
		expectedACL = append(expectedACL, authorityAccessACL{
			object:    spec.name,
			grantee:   manifest.RuntimeRole,
			grantor:   manifest.OwnerRole,
			privilege: "EXECUTE",
		})
	}
	return slices.Equal(actualACL, expectedACL)
}

func noAuthorityMetaFunctions(ctx context.Context, transaction pgx.Tx) bool {
	var clean bool
	err := transaction.QueryRow(ctx, `
SELECT NOT EXISTS (
    SELECT 1
    FROM pg_catalog.pg_proc AS procedure
    JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
    WHERE namespace.nspname = 'wanwork_meta'
)`).Scan(&clean)
	return err == nil && clean
}

func exactOwnerFunctionDefaultPrivileges(
	ctx context.Context,
	transaction pgx.Tx,
	ownerRole string,
) bool {
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
	ownerRole string,
) ([]authorityAccessACL, bool) {
	rows, err := transaction.Query(ctx, `
SELECT procedure.proname,
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
WHERE namespace.nspname = 'wanwork_im'
  AND acl.grantee <> procedure.proowner
ORDER BY procedure.proname, grantee.rolname, grantor.rolname, acl.privilege_type`)
	if err != nil {
		return nil, false
	}
	defer rows.Close()
	values := make([]authorityAccessACL, 0)
	for rows.Next() {
		var value authorityAccessACL
		if err := rows.Scan(
			&value.object,
			&value.grantee,
			&value.grantor,
			&value.privilege,
			&value.grantable,
		); err != nil || value.grantee == "" || value.grantor == "" ||
			value.grantee == ownerRole || value.grantable {
			return nil, false
		}
		values = append(values, value)
	}
	return values, rows.Err() == nil
}

func authorityAccessRoleNames(manifest AuthorityAccessManifest) []string {
	roles := []string{manifest.OwnerRole, manifest.MigratorRole, manifest.RuntimeRole}
	roles = append(roles, manifest.MigrationLoginRoles...)
	roles = append(roles, manifest.RuntimeLoginRoles...)
	slices.Sort(roles)
	return roles
}
