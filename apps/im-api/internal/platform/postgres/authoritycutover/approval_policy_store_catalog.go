package authoritycutover

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"slices"
	"strings"

	"github.com/jackc/pgx/v5"
)

const (
	approvalPolicyControlStoreCatalogDigestDomain   = "wanwork.im/postgres-approval-policy-control-store-catalog/1\n"
	approvalPolicyControlStoreCatalogDigestDomainV2 = "wanwork.im/postgres-approval-policy-control-store-catalog/2\n"
	approvalPolicyControlStoreCatalogDigest         = "sha256:8ac32cf0ef53b447fd1b152c5359f5854c4f50f7e513af1f71d19dd57d4d1ea0"
	approvalPolicyControlStoreCatalogDigestV2       = "sha256:523755fe0a80dc9de6e0a8a61536875b25303bf7787ee50172218c154a1bf7ca"
)

type approvalPolicyControlStoreCatalogColumn struct {
	Collation string `json:"collation"`
	Default   string `json:"default"`
	Generated string `json:"generated"`
	Identity  string `json:"identity"`
	Name      string `json:"name"`
	NotNull   bool   `json:"notNull"`
	Number    int16  `json:"number"`
	Table     string `json:"table"`
	Type      string `json:"type"`
}

type approvalPolicyControlStoreCatalogConstraint struct {
	Definition        string `json:"definition"`
	InitiallyDeferred bool   `json:"initiallyDeferred"`
	Name              string `json:"name"`
	NoInherit         bool   `json:"noInherit"`
	Table             string `json:"table"`
	Type              string `json:"type"`
	Validated         bool   `json:"validated"`
	Deferrable        bool   `json:"deferrable"`
}

type approvalPolicyControlStoreCatalogFunction struct {
	Arguments        string `json:"arguments"`
	Configuration    string `json:"configuration"`
	DefinitionDigest string `json:"definitionDigest"`
	Kind             string `json:"kind"`
	Language         string `json:"language"`
	Leakproof        bool   `json:"leakproof"`
	Name             string `json:"name"`
	Parallel         string `json:"parallel"`
	Result           string `json:"result"`
	SecurityDefiner  bool   `json:"securityDefiner"`
	Strict           bool   `json:"strict"`
	Volatility       string `json:"volatility"`
}

type approvalPolicyControlStoreCatalogIndex struct {
	Clustered  bool   `json:"clustered"`
	Definition string `json:"definition"`
	Live       bool   `json:"live"`
	Name       string `json:"name"`
	Primary    bool   `json:"primary"`
	Ready      bool   `json:"ready"`
	Replica    bool   `json:"replica"`
	Table      string `json:"table"`
	Unique     bool   `json:"unique"`
	Valid      bool   `json:"valid"`
}

type approvalPolicyControlStoreCatalogRelation struct {
	AccessMethod       string `json:"accessMethod"`
	ForceRowSecurity   bool   `json:"forceRowSecurity"`
	Kind               string `json:"kind"`
	Name               string `json:"name"`
	Options            string `json:"options"`
	Persistence        string `json:"persistence"`
	ReplicaIdentity    string `json:"replicaIdentity"`
	RowSecurity        bool   `json:"rowSecurity"`
	UsesDefaultStorage bool   `json:"usesDefaultStorage"`
}

type approvalPolicyControlStoreCatalogSurface struct {
	DefaultACLs     int64 `json:"defaultAcls"`
	Policies        int64 `json:"policies"`
	Publications    int64 `json:"publications"`
	Rules           int64 `json:"rules"`
	StandaloneTypes int64 `json:"standaloneTypes"`
	Triggers        int64 `json:"triggers"`
}

type approvalPolicyControlStoreCatalogManifest struct {
	Columns     []approvalPolicyControlStoreCatalogColumn     `json:"columns"`
	Constraints []approvalPolicyControlStoreCatalogConstraint `json:"constraints"`
	Functions   []approvalPolicyControlStoreCatalogFunction   `json:"functions"`
	Indexes     []approvalPolicyControlStoreCatalogIndex      `json:"indexes"`
	Relations   []approvalPolicyControlStoreCatalogRelation   `json:"relations"`
	Surface     approvalPolicyControlStoreCatalogSurface      `json:"surface"`
}

type approvalPolicyControlStoreCatalogQuerier interface {
	Query(context.Context, string, ...any) (pgx.Rows, error)
	QueryRow(context.Context, string, ...any) pgx.Row
}

type approvalPolicyControlStoreACLEntry struct {
	Grantable bool
	Grantee   string
	Grantor   string
	Kind      string
	Object    string
	Privilege string
}

type approvalPolicyControlStoreRole struct {
	BypassRLS        bool
	CanLogin         bool
	ConnectionLimit  int32
	CreateDatabase   bool
	CreateRole       bool
	HasConfiguration bool
	HasValidityLimit bool
	Inherit          bool
	Name             string
	Replication      bool
	Superuser        bool
}

func verifyApprovalPolicyControlStoreCatalog(
	ctx context.Context,
	connection *pgx.Conn,
	expectation ApprovalPolicyControlStoreExpectation,
) bool {
	transaction, err := connection.BeginTx(ctx, pgx.TxOptions{
		IsoLevel:   pgx.RepeatableRead,
		AccessMode: pgx.ReadOnly,
	})
	if err != nil {
		return false
	}
	defer func() { _ = transaction.Rollback(context.Background()) }()
	digest, err := readApprovalPolicyControlStoreCatalogDigest(ctx, transaction)
	if err != nil || digest != approvalPolicyControlStoreCatalogDigest ||
		!verifyApprovalPolicyControlStoreRolesAndACL(ctx, transaction, expectation) {
		return false
	}
	return transaction.Commit(ctx) == nil
}

func verifyApprovalPolicyControlStoreCatalogV2(
	ctx context.Context,
	connection *pgx.Conn,
	expectation ApprovalPolicyControlStoreExpectation,
) bool {
	transaction, err := connection.BeginTx(ctx, pgx.TxOptions{
		IsoLevel:   pgx.RepeatableRead,
		AccessMode: pgx.ReadOnly,
	})
	if err != nil {
		return false
	}
	defer func() { _ = transaction.Rollback(context.Background()) }()
	digest, err := readApprovalPolicyControlStoreCatalogDigestV2(ctx, transaction)
	if err != nil || digest != approvalPolicyControlStoreCatalogDigestV2 ||
		!verifyApprovalPolicyControlStoreRolesAndACLV2(ctx, transaction, expectation) {
		return false
	}
	return transaction.Commit(ctx) == nil
}

func verifyApprovalPolicyControlStoreRolesAndACL(
	ctx context.Context,
	query approvalPolicyControlStoreCatalogQuerier,
	expectation ApprovalPolicyControlStoreExpectation,
) bool {
	return verifyApprovalPolicyControlStoreRolesAndACLForRoles(
		ctx,
		query,
		expectation,
		[]string{
			expectation.ControlOwnerRole,
			expectation.ControlReaderRole,
			expectation.ControlLoginRole,
		},
		[]string{expectation.ControlReaderRole, expectation.ControlLoginRole},
		expectedApprovalPolicyControlStoreACL(expectation),
	)
}

func verifyApprovalPolicyControlStoreRolesAndACLV2(
	ctx context.Context,
	query approvalPolicyControlStoreCatalogQuerier,
	expectation ApprovalPolicyControlStoreExpectation,
) bool {
	return verifyApprovalPolicyControlStoreRolesAndACLForRoles(
		ctx,
		query,
		expectation,
		[]string{
			expectation.ControlOwnerRole,
			expectation.ControlReaderRole,
			expectation.ControlActivatorRole,
			expectation.ControlFencerRole,
		},
		[]string{
			expectation.ControlReaderRole,
			expectation.ControlActivatorRole,
			expectation.ControlFencerRole,
		},
		expectedApprovalPolicyControlStoreACLV2(expectation),
	)
}

func verifyApprovalPolicyControlStoreRolesAndACLForRoles(
	ctx context.Context,
	query approvalPolicyControlStoreCatalogQuerier,
	expectation ApprovalPolicyControlStoreExpectation,
	protectedRoles []string,
	functionalRoles []string,
	expectedACL []approvalPolicyControlStoreACLEntry,
) bool {
	roleRows, err := query.Query(ctx, `
SELECT role_value.rolname,
       role_value.rolsuper,
       role_value.rolinherit,
       role_value.rolcreaterole,
       role_value.rolcreatedb,
       role_value.rolcanlogin,
       role_value.rolreplication,
       role_value.rolconnlimit,
       role_value.rolbypassrls,
       role_value.rolvaliduntil IS NOT NULL,
       role_value.rolconfig IS NOT NULL
FROM pg_catalog.pg_roles AS role_value
WHERE role_value.rolname = ANY($1::text[])
ORDER BY role_value.rolname`, protectedRoles)
	if err != nil {
		return false
	}
	roles := make([]approvalPolicyControlStoreRole, 0, len(protectedRoles))
	for roleRows.Next() {
		var role approvalPolicyControlStoreRole
		if err := roleRows.Scan(
			&role.Name,
			&role.Superuser,
			&role.Inherit,
			&role.CreateRole,
			&role.CreateDatabase,
			&role.CanLogin,
			&role.Replication,
			&role.ConnectionLimit,
			&role.BypassRLS,
			&role.HasValidityLimit,
			&role.HasConfiguration,
		); err != nil {
			roleRows.Close()
			return false
		}
		roles = append(roles, role)
	}
	if roleRows.Err() != nil || len(roles) != len(protectedRoles) {
		return false
	}
	for _, role := range roles {
		if role.Superuser || role.Inherit || role.CreateRole || role.CreateDatabase ||
			role.Replication || role.BypassRLS || role.ConnectionLimit != -1 ||
			role.HasValidityLimit || role.HasConfiguration ||
			(role.Name == expectation.ControlOwnerRole) == role.CanLogin {
			return false
		}
	}
	var dangerousMemberships, ownerDefaultACLs, unexpectedSettings int64
	if err := query.QueryRow(ctx, `
SELECT
    (SELECT pg_catalog.count(*)
       FROM pg_catalog.pg_auth_members AS membership
       INNER JOIN pg_catalog.pg_roles AS member_role ON member_role.oid = membership.member
      WHERE member_role.rolname = ANY($1::text[])),
	    (SELECT pg_catalog.count(*)
	       FROM pg_catalog.pg_default_acl AS default_acl
       INNER JOIN pg_catalog.pg_roles AS owner_role ON owner_role.oid = default_acl.defaclrole
      WHERE owner_role.rolname = $2
        AND (default_acl.defaclnamespace = 0 OR default_acl.defaclnamespace = (
            SELECT schema_value.oid
            FROM pg_catalog.pg_namespace AS schema_value
	            WHERE schema_value.nspname = 'wanwork_policy_control'
	        ))),
	    (SELECT pg_catalog.count(*)
	       FROM pg_catalog.pg_db_role_setting AS setting
	       INNER JOIN pg_catalog.pg_roles AS configured_role
	           ON configured_role.oid = setting.setrole
	      WHERE configured_role.rolname = ANY($1::text[])
	        AND (setting.setdatabase = 0 OR setting.setdatabase = (
	            SELECT database_value.oid
	            FROM pg_catalog.pg_database AS database_value
	            WHERE database_value.datname = pg_catalog.current_database()
	        )))`, protectedRoles, expectation.ControlOwnerRole).Scan(
		&dangerousMemberships,
		&ownerDefaultACLs,
		&unexpectedSettings,
	); err != nil || dangerousMemberships != 0 || ownerDefaultACLs != 0 || unexpectedSettings != 0 {
		return false
	}

	var databaseOwner, encoding string
	var allowConnections, template bool
	var databaseConnectionLimit int32
	if err := query.QueryRow(ctx, `
SELECT owner_role.rolname,
       pg_catalog.pg_encoding_to_char(database_value.encoding),
       database_value.datistemplate,
       database_value.datallowconn,
       database_value.datconnlimit
FROM pg_catalog.pg_database AS database_value
INNER JOIN pg_catalog.pg_roles AS owner_role ON owner_role.oid = database_value.datdba
WHERE database_value.datname = pg_catalog.current_database()`).Scan(
		&databaseOwner,
		&encoding,
		&template,
		&allowConnections,
		&databaseConnectionLimit,
	); err != nil || databaseOwner != expectation.ControlOwnerRole || encoding != "UTF8" || template ||
		!allowConnections || databaseConnectionLimit != -1 {
		return false
	}

	var dangerousOwnerAccess int64
	if err := query.QueryRow(ctx, `
SELECT pg_catalog.count(*)
FROM pg_catalog.unnest($1::text[]) AS functional_role(role_name)
WHERE pg_catalog.pg_has_role(functional_role.role_name, $2, 'MEMBER')
   OR pg_catalog.pg_has_role(functional_role.role_name, $2, 'SET')`,
		functionalRoles,
		expectation.ControlOwnerRole,
	).Scan(&dangerousOwnerAccess); err != nil || dangerousOwnerAccess != 0 {
		return false
	}

	var schemaOwner string
	var rogueRelationOwners, rogueRoutineOwners int64
	if err := query.QueryRow(ctx, `
SELECT schema_owner.rolname,
       (SELECT pg_catalog.count(*)
          FROM pg_catalog.pg_class AS relation
         WHERE relation.relnamespace = schema_value.oid
           AND relation.relowner <> schema_value.nspowner),
       (SELECT pg_catalog.count(*)
          FROM pg_catalog.pg_proc AS routine
         WHERE routine.pronamespace = schema_value.oid
           AND routine.proowner <> schema_value.nspowner)
FROM pg_catalog.pg_namespace AS schema_value
INNER JOIN pg_catalog.pg_roles AS schema_owner ON schema_owner.oid = schema_value.nspowner
WHERE schema_value.nspname = 'wanwork_policy_control'`).Scan(
		&schemaOwner,
		&rogueRelationOwners,
		&rogueRoutineOwners,
	); err != nil || schemaOwner != expectation.ControlOwnerRole ||
		rogueRelationOwners != 0 || rogueRoutineOwners != 0 {
		return false
	}

	actualACL, err := readApprovalPolicyControlStoreACL(ctx, query)
	if err != nil {
		return false
	}
	return slices.Equal(actualACL, expectedACL)
}

func readApprovalPolicyControlStoreACL(
	ctx context.Context,
	query approvalPolicyControlStoreCatalogQuerier,
) ([]approvalPolicyControlStoreACLEntry, error) {
	rows, err := query.Query(ctx, `
WITH acl_entry AS (
    SELECT 'database'::text AS kind,
           database_value.datname AS object_name,
           exploded.grantor,
           exploded.grantee,
           exploded.privilege_type,
           exploded.is_grantable
    FROM pg_catalog.pg_database AS database_value
    CROSS JOIN LATERAL pg_catalog.aclexplode(database_value.datacl) AS exploded
    WHERE database_value.datname = pg_catalog.current_database()
    UNION ALL
    SELECT 'schema', schema_value.nspname, exploded.grantor, exploded.grantee,
           exploded.privilege_type, exploded.is_grantable
    FROM pg_catalog.pg_namespace AS schema_value
    CROSS JOIN LATERAL pg_catalog.aclexplode(schema_value.nspacl) AS exploded
    WHERE schema_value.nspname = 'wanwork_policy_control'
    UNION ALL
    SELECT 'table', relation.relname, exploded.grantor, exploded.grantee,
           exploded.privilege_type, exploded.is_grantable
    FROM pg_catalog.pg_class AS relation
    INNER JOIN pg_catalog.pg_namespace AS schema_value ON schema_value.oid = relation.relnamespace
    CROSS JOIN LATERAL pg_catalog.aclexplode(relation.relacl) AS exploded
    WHERE schema_value.nspname = 'wanwork_policy_control' AND relation.relkind = 'r'
    UNION ALL
    SELECT 'function', routine.proname, exploded.grantor, exploded.grantee,
           exploded.privilege_type, exploded.is_grantable
    FROM pg_catalog.pg_proc AS routine
    INNER JOIN pg_catalog.pg_namespace AS schema_value ON schema_value.oid = routine.pronamespace
    CROSS JOIN LATERAL pg_catalog.aclexplode(routine.proacl) AS exploded
    WHERE schema_value.nspname = 'wanwork_policy_control'
)
SELECT acl_entry.kind,
       acl_entry.object_name,
       grantor_role.rolname,
       CASE acl_entry.grantee WHEN 0 THEN 'PUBLIC' ELSE grantee_role.rolname END,
       acl_entry.privilege_type,
       acl_entry.is_grantable
FROM acl_entry
INNER JOIN pg_catalog.pg_roles AS grantor_role ON grantor_role.oid = acl_entry.grantor
LEFT JOIN pg_catalog.pg_roles AS grantee_role ON grantee_role.oid = acl_entry.grantee
ORDER BY acl_entry.kind, acl_entry.object_name, 4, acl_entry.privilege_type`)
	if err != nil {
		return nil, err
	}
	entries := make([]approvalPolicyControlStoreACLEntry, 0)
	for rows.Next() {
		var entry approvalPolicyControlStoreACLEntry
		if err := rows.Scan(
			&entry.Kind,
			&entry.Object,
			&entry.Grantor,
			&entry.Grantee,
			&entry.Privilege,
			&entry.Grantable,
		); err != nil {
			rows.Close()
			return nil, err
		}
		entries = append(entries, entry)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	sortApprovalPolicyControlStoreACL(entries)
	return entries, nil
}

func expectedApprovalPolicyControlStoreACL(
	expectation ApprovalPolicyControlStoreExpectation,
) []approvalPolicyControlStoreACLEntry {
	entries := make([]approvalPolicyControlStoreACLEntry, 0, 42)
	add := func(kind, object, grantee, privilege string) {
		entries = append(entries, approvalPolicyControlStoreACLEntry{
			Grantee:   grantee,
			Grantor:   expectation.ControlOwnerRole,
			Kind:      kind,
			Object:    object,
			Privilege: privilege,
		})
	}
	for _, privilege := range []string{"CONNECT", "CREATE", "TEMPORARY"} {
		add("database", expectation.ControlDatabase, expectation.ControlOwnerRole, privilege)
	}
	add("database", expectation.ControlDatabase, expectation.ControlReaderRole, "CONNECT")
	add("database", expectation.ControlDatabase, expectation.ControlLoginRole, "CONNECT")
	for _, privilege := range []string{"CREATE", "USAGE"} {
		add("schema", approvalPolicyControlStoreSchemaName, expectation.ControlOwnerRole, privilege)
	}
	add("schema", approvalPolicyControlStoreSchemaName, expectation.ControlReaderRole, "USAGE")
	add("schema", approvalPolicyControlStoreSchemaName, expectation.ControlLoginRole, "USAGE")
	for _, table := range []string{
		"approval_policy_activation_record",
		"approval_policy_archive",
		"approval_policy_head",
	} {
		for _, privilege := range []string{
			"DELETE", "INSERT", "MAINTAIN", "REFERENCES", "SELECT", "TRIGGER", "TRUNCATE", "UPDATE",
		} {
			add("table", table, expectation.ControlOwnerRole, privilege)
		}
	}
	for _, function := range []string{
		approvalPolicyControlStoreIdentityFunction,
		approvalPolicyControlStoreReadFunction,
		approvalPolicyControlStoreActivateFunction,
	} {
		add("function", function, expectation.ControlOwnerRole, "EXECUTE")
	}
	for _, function := range []string{
		approvalPolicyControlStoreIdentityFunction,
		approvalPolicyControlStoreReadFunction,
	} {
		add("function", function, expectation.ControlReaderRole, "EXECUTE")
	}
	for _, function := range []string{
		approvalPolicyControlStoreIdentityFunction,
		approvalPolicyControlStoreReadFunction,
		approvalPolicyControlStoreActivateFunction,
	} {
		add("function", function, expectation.ControlLoginRole, "EXECUTE")
	}
	sortApprovalPolicyControlStoreACL(entries)
	return entries
}

func expectedApprovalPolicyControlStoreACLV2(
	expectation ApprovalPolicyControlStoreExpectation,
) []approvalPolicyControlStoreACLEntry {
	entries := make([]approvalPolicyControlStoreACLEntry, 0, 75)
	add := func(kind, object, grantee, privilege string) {
		entries = append(entries, approvalPolicyControlStoreACLEntry{
			Grantee:   grantee,
			Grantor:   expectation.ControlOwnerRole,
			Kind:      kind,
			Object:    object,
			Privilege: privilege,
		})
	}
	for _, privilege := range []string{"CONNECT", "CREATE", "TEMPORARY"} {
		add("database", expectation.ControlDatabase, expectation.ControlOwnerRole, privilege)
	}
	for _, role := range []string{
		expectation.ControlReaderRole,
		expectation.ControlActivatorRole,
		expectation.ControlFencerRole,
	} {
		add("database", expectation.ControlDatabase, role, "CONNECT")
	}
	for _, privilege := range []string{"CREATE", "USAGE"} {
		add("schema", approvalPolicyControlStoreSchemaName, expectation.ControlOwnerRole, privilege)
	}
	for _, role := range []string{
		expectation.ControlReaderRole,
		expectation.ControlActivatorRole,
		expectation.ControlFencerRole,
	} {
		add("schema", approvalPolicyControlStoreSchemaName, role, "USAGE")
	}
	for _, table := range []string{
		"approval_execution_fence_counter",
		"approval_execution_fence_head",
		"approval_execution_fence_record",
		"approval_policy_activation_record",
		"approval_policy_archive",
		"approval_policy_head",
	} {
		for _, privilege := range []string{
			"DELETE", "INSERT", "MAINTAIN", "REFERENCES", "SELECT", "TRIGGER", "TRUNCATE", "UPDATE",
		} {
			add("table", table, expectation.ControlOwnerRole, privilege)
		}
	}
	for _, function := range []string{
		approvalPolicyControlStoreActivateFunction,
		approvalPolicyControlStoreAdmissionFunction,
		approvalPolicyControlStoreFenceOpenFunction,
		approvalPolicyControlStoreFenceReadFunction,
		approvalPolicyControlStoreIdentityFunction,
		approvalPolicyControlStoreReadFunction,
	} {
		add("function", function, expectation.ControlOwnerRole, "EXECUTE")
	}
	for _, function := range []string{
		approvalPolicyControlStoreFenceReadFunction,
		approvalPolicyControlStoreIdentityFunction,
		approvalPolicyControlStoreReadFunction,
	} {
		add("function", function, expectation.ControlReaderRole, "EXECUTE")
	}
	for _, function := range []string{
		approvalPolicyControlStoreActivateFunction,
		approvalPolicyControlStoreIdentityFunction,
		approvalPolicyControlStoreReadFunction,
	} {
		add("function", function, expectation.ControlActivatorRole, "EXECUTE")
	}
	for _, function := range []string{
		approvalPolicyControlStoreFenceOpenFunction,
		approvalPolicyControlStoreFenceReadFunction,
		approvalPolicyControlStoreIdentityFunction,
		approvalPolicyControlStoreReadFunction,
	} {
		add("function", function, expectation.ControlFencerRole, "EXECUTE")
	}
	sortApprovalPolicyControlStoreACL(entries)
	return entries
}

func sortApprovalPolicyControlStoreACL(entries []approvalPolicyControlStoreACLEntry) {
	slices.SortFunc(entries, func(left, right approvalPolicyControlStoreACLEntry) int {
		return strings.Compare(
			left.Kind+"\x00"+left.Object+"\x00"+left.Grantee+"\x00"+left.Privilege,
			right.Kind+"\x00"+right.Object+"\x00"+right.Grantee+"\x00"+right.Privilege,
		)
	})
}

func readApprovalPolicyControlStoreCatalogDigest(
	ctx context.Context,
	query approvalPolicyControlStoreCatalogQuerier,
) (string, error) {
	return readApprovalPolicyControlStoreCatalogDigestWithDomain(
		ctx,
		query,
		approvalPolicyControlStoreCatalogDigestDomain,
	)
}

func readApprovalPolicyControlStoreCatalogDigestV2(
	ctx context.Context,
	query approvalPolicyControlStoreCatalogQuerier,
) (string, error) {
	return readApprovalPolicyControlStoreCatalogDigestWithDomain(
		ctx,
		query,
		approvalPolicyControlStoreCatalogDigestDomainV2,
	)
}

func readApprovalPolicyControlStoreCatalogDigestWithDomain(
	ctx context.Context,
	query approvalPolicyControlStoreCatalogQuerier,
	domain string,
) (string, error) {
	manifest, err := readApprovalPolicyControlStoreCatalogManifest(ctx, query)
	if err != nil {
		return "", err
	}
	var canonical bytes.Buffer
	encoder := json.NewEncoder(&canonical)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(manifest); err != nil {
		return "", err
	}
	return domainSeparatedDigest(
		domain,
		bytes.TrimSuffix(canonical.Bytes(), []byte("\n")),
	), nil
}

func readApprovalPolicyControlStoreCatalogManifest(
	ctx context.Context,
	query approvalPolicyControlStoreCatalogQuerier,
) (approvalPolicyControlStoreCatalogManifest, error) {
	manifest := approvalPolicyControlStoreCatalogManifest{
		Columns:     make([]approvalPolicyControlStoreCatalogColumn, 0),
		Constraints: make([]approvalPolicyControlStoreCatalogConstraint, 0),
		Functions:   make([]approvalPolicyControlStoreCatalogFunction, 0),
		Indexes:     make([]approvalPolicyControlStoreCatalogIndex, 0),
		Relations:   make([]approvalPolicyControlStoreCatalogRelation, 0),
	}

	rows, err := query.Query(ctx, `
SELECT tc.relname,
       a.attnum,
       a.attname,
       pg_catalog.format_type(a.atttypid, a.atttypmod),
       a.attnotnull,
       a.attidentity::text,
       a.attgenerated::text,
       COALESCE(coll.collname, ''),
       COALESCE(pg_catalog.pg_get_expr(def.adbin, def.adrelid), '')
FROM pg_catalog.pg_attribute AS a
INNER JOIN pg_catalog.pg_class AS tc ON tc.oid = a.attrelid
INNER JOIN pg_catalog.pg_namespace AS ns ON ns.oid = tc.relnamespace
LEFT JOIN pg_catalog.pg_collation AS coll
    ON coll.oid = a.attcollation AND a.attcollation <> 0
LEFT JOIN pg_catalog.pg_attrdef AS def
    ON def.adrelid = a.attrelid AND def.adnum = a.attnum
WHERE ns.nspname = 'wanwork_policy_control'
  AND tc.relkind = 'r'
  AND a.attnum > 0
  AND NOT a.attisdropped
ORDER BY tc.relname, a.attnum`)
	if err != nil {
		return approvalPolicyControlStoreCatalogManifest{}, fmt.Errorf("read columns: %w", err)
	}
	for rows.Next() {
		var column approvalPolicyControlStoreCatalogColumn
		if err := rows.Scan(
			&column.Table,
			&column.Number,
			&column.Name,
			&column.Type,
			&column.NotNull,
			&column.Identity,
			&column.Generated,
			&column.Collation,
			&column.Default,
		); err != nil {
			rows.Close()
			return approvalPolicyControlStoreCatalogManifest{}, err
		}
		manifest.Columns = append(manifest.Columns, column)
	}
	if err := rows.Err(); err != nil {
		return approvalPolicyControlStoreCatalogManifest{}, err
	}

	rows, err = query.Query(ctx, `
SELECT table_class.relname,
       constraint_value.conname,
       constraint_value.contype::text,
       constraint_value.condeferrable,
       constraint_value.condeferred,
       constraint_value.convalidated,
       constraint_value.connoinherit,
       pg_catalog.pg_get_constraintdef(constraint_value.oid, true)
FROM pg_catalog.pg_constraint AS constraint_value
INNER JOIN pg_catalog.pg_class AS table_class ON table_class.oid = constraint_value.conrelid
INNER JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = constraint_value.connamespace
WHERE namespace.nspname = 'wanwork_policy_control'
ORDER BY table_class.relname, constraint_value.conname`)
	if err != nil {
		return approvalPolicyControlStoreCatalogManifest{}, fmt.Errorf("read constraints: %w", err)
	}
	for rows.Next() {
		var constraint approvalPolicyControlStoreCatalogConstraint
		if err := rows.Scan(
			&constraint.Table,
			&constraint.Name,
			&constraint.Type,
			&constraint.Deferrable,
			&constraint.InitiallyDeferred,
			&constraint.Validated,
			&constraint.NoInherit,
			&constraint.Definition,
		); err != nil {
			rows.Close()
			return approvalPolicyControlStoreCatalogManifest{}, err
		}
		manifest.Constraints = append(manifest.Constraints, constraint)
	}
	if err := rows.Err(); err != nil {
		return approvalPolicyControlStoreCatalogManifest{}, err
	}

	rows, err = query.Query(ctx, `
SELECT routine.proname,
       pg_catalog.pg_get_function_identity_arguments(routine.oid),
       pg_catalog.pg_get_function_result(routine.oid),
       language.lanname,
       routine.prokind::text,
       routine.provolatile::text,
       routine.proisstrict,
       routine.prosecdef,
       routine.proleakproof,
       routine.proparallel::text,
       COALESCE(pg_catalog.array_to_string(routine.proconfig, E'\\n'), ''),
       pg_catalog.pg_get_functiondef(routine.oid)
FROM pg_catalog.pg_proc AS routine
INNER JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = routine.pronamespace
INNER JOIN pg_catalog.pg_language AS language ON language.oid = routine.prolang
WHERE namespace.nspname = 'wanwork_policy_control'
ORDER BY routine.proname, pg_catalog.pg_get_function_identity_arguments(routine.oid)`)
	if err != nil {
		return approvalPolicyControlStoreCatalogManifest{}, fmt.Errorf("read functions: %w", err)
	}
	for rows.Next() {
		var definition string
		var function approvalPolicyControlStoreCatalogFunction
		if err := rows.Scan(
			&function.Name,
			&function.Arguments,
			&function.Result,
			&function.Language,
			&function.Kind,
			&function.Volatility,
			&function.Strict,
			&function.SecurityDefiner,
			&function.Leakproof,
			&function.Parallel,
			&function.Configuration,
			&definition,
		); err != nil {
			rows.Close()
			return approvalPolicyControlStoreCatalogManifest{}, err
		}
		digest := sha256.Sum256([]byte(definition))
		function.DefinitionDigest = "sha256:" + hex.EncodeToString(digest[:])
		manifest.Functions = append(manifest.Functions, function)
	}
	if err := rows.Err(); err != nil {
		return approvalPolicyControlStoreCatalogManifest{}, err
	}

	rows, err = query.Query(ctx, `
SELECT index_class.relname,
       table_class.relname,
       index_value.indisunique,
       index_value.indisprimary,
       index_value.indisvalid,
       index_value.indisready,
       index_value.indislive,
       index_value.indisreplident,
       index_value.indisclustered,
       pg_catalog.pg_get_indexdef(index_value.indexrelid)
FROM pg_catalog.pg_index AS index_value
INNER JOIN pg_catalog.pg_class AS index_class ON index_class.oid = index_value.indexrelid
INNER JOIN pg_catalog.pg_class AS table_class ON table_class.oid = index_value.indrelid
INNER JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = table_class.relnamespace
WHERE namespace.nspname = 'wanwork_policy_control'
ORDER BY index_class.relname`)
	if err != nil {
		return approvalPolicyControlStoreCatalogManifest{}, fmt.Errorf("read indexes: %w", err)
	}
	for rows.Next() {
		var index approvalPolicyControlStoreCatalogIndex
		if err := rows.Scan(
			&index.Name,
			&index.Table,
			&index.Unique,
			&index.Primary,
			&index.Valid,
			&index.Ready,
			&index.Live,
			&index.Replica,
			&index.Clustered,
			&index.Definition,
		); err != nil {
			rows.Close()
			return approvalPolicyControlStoreCatalogManifest{}, err
		}
		manifest.Indexes = append(manifest.Indexes, index)
	}
	if err := rows.Err(); err != nil {
		return approvalPolicyControlStoreCatalogManifest{}, err
	}

	rows, err = query.Query(ctx, `
SELECT relation.relname,
       relation.relkind::text,
       relation.relpersistence::text,
       relation.relrowsecurity,
       relation.relforcerowsecurity,
       relation.relreplident::text,
       COALESCE(access_method.amname, ''),
       relation.reltablespace = 0,
       COALESCE(pg_catalog.array_to_string(relation.reloptions, E'\\n'), '')
FROM pg_catalog.pg_class AS relation
INNER JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
LEFT JOIN pg_catalog.pg_am AS access_method ON access_method.oid = relation.relam
WHERE namespace.nspname = 'wanwork_policy_control'
ORDER BY relation.relkind, relation.relname`)
	if err != nil {
		return approvalPolicyControlStoreCatalogManifest{}, fmt.Errorf("read relations: %w", err)
	}
	for rows.Next() {
		var relation approvalPolicyControlStoreCatalogRelation
		if err := rows.Scan(
			&relation.Name,
			&relation.Kind,
			&relation.Persistence,
			&relation.RowSecurity,
			&relation.ForceRowSecurity,
			&relation.ReplicaIdentity,
			&relation.AccessMethod,
			&relation.UsesDefaultStorage,
			&relation.Options,
		); err != nil {
			rows.Close()
			return approvalPolicyControlStoreCatalogManifest{}, err
		}
		manifest.Relations = append(manifest.Relations, relation)
	}
	if err := rows.Err(); err != nil {
		return approvalPolicyControlStoreCatalogManifest{}, err
	}

	err = query.QueryRow(ctx, `
SELECT
    (SELECT pg_catalog.count(*)
       FROM pg_catalog.pg_default_acl AS default_acl
       INNER JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = default_acl.defaclnamespace
      WHERE namespace.nspname = 'wanwork_policy_control'),
    (SELECT pg_catalog.count(*)
       FROM pg_catalog.pg_policy AS policy_value
       INNER JOIN pg_catalog.pg_class AS relation ON relation.oid = policy_value.polrelid
       INNER JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
      WHERE namespace.nspname = 'wanwork_policy_control'),
    (SELECT pg_catalog.count(*)
       FROM pg_catalog.pg_publication_rel AS publication
       INNER JOIN pg_catalog.pg_class AS relation ON relation.oid = publication.prrelid
       INNER JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
      WHERE namespace.nspname = 'wanwork_policy_control'),
    (SELECT pg_catalog.count(*)
       FROM pg_catalog.pg_rewrite AS rule
       INNER JOIN pg_catalog.pg_class AS relation ON relation.oid = rule.ev_class
       INNER JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
      WHERE namespace.nspname = 'wanwork_policy_control'),
    (SELECT pg_catalog.count(*)
       FROM pg_catalog.pg_type AS type_value
       INNER JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = type_value.typnamespace
      WHERE namespace.nspname = 'wanwork_policy_control'
        AND type_value.typrelid = 0),
    (SELECT pg_catalog.count(*)
       FROM pg_catalog.pg_trigger AS trigger_value
       INNER JOIN pg_catalog.pg_class AS relation ON relation.oid = trigger_value.tgrelid
       INNER JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
      WHERE namespace.nspname = 'wanwork_policy_control'
        AND NOT trigger_value.tgisinternal)`).Scan(
		&manifest.Surface.DefaultACLs,
		&manifest.Surface.Policies,
		&manifest.Surface.Publications,
		&manifest.Surface.Rules,
		&manifest.Surface.StandaloneTypes,
		&manifest.Surface.Triggers,
	)
	if err != nil {
		return approvalPolicyControlStoreCatalogManifest{}, fmt.Errorf("read surface: %w", err)
	}
	return manifest, nil
}
