package migrations

import (
	"context"

	"github.com/jackc/pgx/v5"
)

type authorityRelation struct {
	currentUser       string
	relationOwner     string
	relationKind      string
	persistence       string
	replicaIdentity   string
	rowSecurity       bool
	forceRowSecurity  bool
	partition         bool
	defaultOptions    bool
	noPublicTableACL  bool
	noPublicColumnACL bool
	noUserTriggers    bool
	noRewriteRules    bool
	noPublications    bool
	onlyPrimaryIndex  bool
}

type authorityPolicy struct {
	name       string
	command    string
	permissive bool
	roles      string
	usingSQL   string
	checkSQL   string
}

type authorityTableSpec struct {
	name             string
	rowSecurity      bool
	forceRowSecurity bool
	columns          []ledgerColumn
	constraints      []ledgerConstraint
	policies         []authorityPolicy
}

func validateAppliedPostconditions(
	ctx context.Context,
	connection *pgx.Conn,
	applied []AppliedMigration,
) error {
	if len(applied) == 0 {
		return nil
	}
	transaction, err := connection.BeginTx(ctx, pgx.TxOptions{
		IsoLevel:   pgx.RepeatableRead,
		AccessMode: pgx.ReadOnly,
	})
	if err != nil {
		return ErrMigrationFailed
	}
	defer func() { _ = transaction.Rollback(context.Background()) }()
	for _, migration := range applied {
		if err := validateMigrationPostcondition(ctx, transaction, migration.Version); err != nil {
			return err
		}
	}
	if err := transaction.Commit(ctx); err != nil {
		return ErrMigrationFailed
	}
	return nil
}

func validateMigrationPostcondition(ctx context.Context, transaction pgx.Tx, version int64) error {
	switch version {
	case 1:
		return validateAuthorityRoots(ctx, transaction)
	case 2:
		return validateIdentityAuthority(ctx, transaction)
	default:
		return ErrMigrationSchema
	}
}

func validateIdentityAuthority(ctx context.Context, transaction pgx.Tx) error {
	digest, err := tableSchemaDigest(ctx, transaction, identityAuthorityTableNames)
	if err != nil || digest != identityAuthoritySchemaDigest {
		return ErrMigrationSchema
	}
	return nil
}

func validateAuthorityRoots(ctx context.Context, transaction pgx.Tx) error {
	if !validAuthoritySchema(ctx, transaction) {
		return ErrMigrationSchema
	}
	for _, spec := range authorityRootSpecs() {
		if err := validateAuthorityTable(ctx, transaction, spec); err != nil {
			return err
		}
	}
	return nil
}

func validAuthoritySchema(ctx context.Context, transaction pgx.Tx) bool {
	var currentUser string
	var owner string
	var noPublicACL bool
	err := transaction.QueryRow(ctx, `
SELECT current_user,
       owner.rolname,
       NOT EXISTS (
           SELECT 1
           FROM pg_catalog.aclexplode(
               COALESCE(
                   namespace.nspacl,
                   pg_catalog.acldefault('n', namespace.nspowner)
               )
           ) AS acl
           WHERE acl.grantee = 0
       )
FROM pg_catalog.pg_namespace AS namespace
JOIN pg_catalog.pg_roles AS owner ON owner.oid = namespace.nspowner
WHERE namespace.nspname = 'wanwork_im'`).Scan(&currentUser, &owner, &noPublicACL)
	return err == nil && currentUser != "" && currentUser == owner && noPublicACL
}

func validateAuthorityTable(
	ctx context.Context,
	transaction pgx.Tx,
	spec authorityTableSpec,
) error {
	relation, err := readAuthorityRelation(ctx, transaction, spec.name)
	if err != nil || !exactAuthorityRelation(relation, spec) {
		return ErrMigrationSchema
	}
	columns, err := readAuthorityColumns(ctx, transaction, spec.name)
	if err != nil || !exactColumns(columns, spec.columns) {
		return ErrMigrationSchema
	}
	constraints, err := readAuthorityConstraints(ctx, transaction, spec.name)
	if err != nil || !exactConstraints(constraints, spec.constraints) {
		return ErrMigrationSchema
	}
	policies, err := readAuthorityPolicies(ctx, transaction, spec.name)
	if err != nil || !exactAuthorityPolicies(policies, spec.policies) {
		return ErrMigrationSchema
	}
	return nil
}

func readAuthorityRelation(
	ctx context.Context,
	transaction pgx.Tx,
	tableName string,
) (authorityRelation, error) {
	var relation authorityRelation
	err := transaction.QueryRow(ctx, `
SELECT current_user,
       relation_owner.rolname,
       relation.relkind::text,
       relation.relpersistence::text,
       relation.relreplident::text,
       relation.relrowsecurity,
       relation.relforcerowsecurity,
       relation.relispartition,
       relation.reloptions IS NULL,
       NOT EXISTS (
           SELECT 1
           FROM pg_catalog.aclexplode(
               COALESCE(
                   relation.relacl,
                   pg_catalog.acldefault('r', relation.relowner)
               )
           ) AS acl
           WHERE acl.grantee = 0
       ),
       NOT EXISTS (
           SELECT 1
           FROM pg_catalog.pg_attribute AS protected_attribute
           WHERE protected_attribute.attrelid = relation.oid
             AND protected_attribute.attnum > 0
             AND NOT protected_attribute.attisdropped
             AND protected_attribute.attacl IS NOT NULL
             AND EXISTS (
                 SELECT 1
                 FROM pg_catalog.aclexplode(protected_attribute.attacl) AS acl
                 WHERE acl.grantee = 0
             )
       ),
       NOT EXISTS (
           SELECT 1
           FROM pg_catalog.pg_trigger AS trigger_value
           WHERE trigger_value.tgrelid = relation.oid
             AND NOT trigger_value.tgisinternal
       ),
       NOT EXISTS (
           SELECT 1
           FROM pg_catalog.pg_rewrite AS rewrite_value
           WHERE rewrite_value.ev_class = relation.oid
       ),
       NOT EXISTS (
           SELECT 1
           FROM pg_catalog.pg_publication_rel AS publication_value
           WHERE publication_value.prrelid = relation.oid
       ) AND NOT EXISTS (
           SELECT 1
           FROM pg_catalog.pg_publication AS publication_value
           WHERE publication_value.puballtables
       ),
       (
           SELECT count(*) = 1
              AND bool_and(index_value.indisprimary)
              AND bool_and(index_value.indisunique)
              AND bool_and(index_value.indisvalid)
              AND bool_and(index_value.indisready)
              AND bool_and(index_value.indislive)
              AND bool_and(index_value.indexprs IS NULL)
              AND bool_and(index_value.indpred IS NULL)
           FROM pg_catalog.pg_index AS index_value
           WHERE index_value.indrelid = relation.oid
       )
FROM pg_catalog.pg_class AS relation
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
JOIN pg_catalog.pg_roles AS relation_owner ON relation_owner.oid = relation.relowner
WHERE namespace.nspname = 'wanwork_im'
  AND relation.relname = $1`, tableName).Scan(
		&relation.currentUser,
		&relation.relationOwner,
		&relation.relationKind,
		&relation.persistence,
		&relation.replicaIdentity,
		&relation.rowSecurity,
		&relation.forceRowSecurity,
		&relation.partition,
		&relation.defaultOptions,
		&relation.noPublicTableACL,
		&relation.noPublicColumnACL,
		&relation.noUserTriggers,
		&relation.noRewriteRules,
		&relation.noPublications,
		&relation.onlyPrimaryIndex,
	)
	return relation, err
}

func exactAuthorityRelation(relation authorityRelation, spec authorityTableSpec) bool {
	return relation.currentUser != "" &&
		relation.currentUser == relation.relationOwner &&
		relation.relationKind == "r" &&
		relation.persistence == "p" &&
		relation.replicaIdentity == "d" &&
		relation.rowSecurity == spec.rowSecurity &&
		relation.forceRowSecurity == spec.forceRowSecurity &&
		!relation.partition &&
		relation.defaultOptions &&
		relation.noPublicTableACL &&
		relation.noPublicColumnACL &&
		relation.noUserTriggers &&
		relation.noRewriteRules &&
		relation.noPublications &&
		relation.onlyPrimaryIndex
}

func readAuthorityColumns(
	ctx context.Context,
	transaction pgx.Tx,
	tableName string,
) ([]ledgerColumn, error) {
	rows, err := transaction.Query(ctx, `
SELECT attribute.attname,
       pg_catalog.format_type(attribute.atttypid, attribute.atttypmod),
       attribute.attnotnull,
       pg_catalog.pg_get_expr(default_value.adbin, default_value.adrelid),
       collation_namespace.nspname,
       collation_value.collname,
       attribute.attidentity::text,
       attribute.attgenerated::text
FROM pg_catalog.pg_attribute AS attribute
JOIN pg_catalog.pg_class AS relation ON relation.oid = attribute.attrelid
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
LEFT JOIN pg_catalog.pg_attrdef AS default_value
       ON default_value.adrelid = relation.oid AND default_value.adnum = attribute.attnum
LEFT JOIN pg_catalog.pg_collation AS collation_value
       ON collation_value.oid = attribute.attcollation
LEFT JOIN pg_catalog.pg_namespace AS collation_namespace
       ON collation_namespace.oid = collation_value.collnamespace
WHERE namespace.nspname = 'wanwork_im'
  AND relation.relname = $1
  AND attribute.attnum > 0
  AND NOT attribute.attisdropped
ORDER BY attribute.attnum`, tableName)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	columns := make([]ledgerColumn, 0)
	for rows.Next() {
		var column ledgerColumn
		if err := rows.Scan(
			&column.name,
			&column.formatType,
			&column.notNull,
			&column.defaultSQL,
			&column.collationNamespace,
			&column.collationName,
			&column.identityKind,
			&column.generatedKind,
		); err != nil {
			return nil, err
		}
		columns = append(columns, column)
	}
	if rows.Err() != nil {
		return nil, rows.Err()
	}
	return columns, nil
}

func readAuthorityConstraints(
	ctx context.Context,
	transaction pgx.Tx,
	tableName string,
) ([]ledgerConstraint, error) {
	rows, err := transaction.Query(ctx, `
SELECT constraint_value.conname,
       constraint_value.contype::text,
       pg_catalog.pg_get_constraintdef(constraint_value.oid, false),
       constraint_value.convalidated,
       constraint_value.condeferrable,
       constraint_value.condeferred
FROM pg_catalog.pg_constraint AS constraint_value
JOIN pg_catalog.pg_class AS relation ON relation.oid = constraint_value.conrelid
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
WHERE namespace.nspname = 'wanwork_im'
  AND relation.relname = $1
ORDER BY constraint_value.conname`, tableName)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	constraints := make([]ledgerConstraint, 0)
	for rows.Next() {
		var constraint ledgerConstraint
		if err := rows.Scan(
			&constraint.name,
			&constraint.kind,
			&constraint.definition,
			&constraint.validated,
			&constraint.deferrable,
			&constraint.deferred,
		); err != nil {
			return nil, err
		}
		constraints = append(constraints, constraint)
	}
	if rows.Err() != nil {
		return nil, rows.Err()
	}
	return constraints, nil
}

func readAuthorityPolicies(
	ctx context.Context,
	transaction pgx.Tx,
	tableName string,
) ([]authorityPolicy, error) {
	rows, err := transaction.Query(ctx, `
SELECT policy_value.polname,
       policy_value.polcmd::text,
       policy_value.polpermissive,
       policy_value.polroles::text,
       COALESCE(pg_catalog.pg_get_expr(policy_value.polqual, policy_value.polrelid), ''),
       COALESCE(pg_catalog.pg_get_expr(policy_value.polwithcheck, policy_value.polrelid), '')
FROM pg_catalog.pg_policy AS policy_value
JOIN pg_catalog.pg_class AS relation ON relation.oid = policy_value.polrelid
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
WHERE namespace.nspname = 'wanwork_im'
  AND relation.relname = $1
ORDER BY policy_value.polname`, tableName)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	policies := make([]authorityPolicy, 0)
	for rows.Next() {
		var policy authorityPolicy
		if err := rows.Scan(
			&policy.name,
			&policy.command,
			&policy.permissive,
			&policy.roles,
			&policy.usingSQL,
			&policy.checkSQL,
		); err != nil {
			return nil, err
		}
		policies = append(policies, policy)
	}
	if rows.Err() != nil {
		return nil, rows.Err()
	}
	return policies, nil
}

func exactAuthorityPolicies(got, expected []authorityPolicy) bool {
	if len(got) != len(expected) {
		return false
	}
	for index := range expected {
		if got[index] != expected[index] {
			return false
		}
	}
	return true
}

func authorityRootSpecs() []authorityTableSpec {
	return []authorityTableSpec{
		providerRealmsSpec(),
		tenantsSpec(),
		workspacesSpec(),
	}
}

func providerRealmsSpec() authorityTableSpec {
	return authorityTableSpec{
		name: "provider_realms",
		columns: []ledgerColumn{
			textAuthorityColumn("provider"),
			textAuthorityColumn("realm_id"),
			textAuthorityColumn("status"),
			bigintAuthorityColumn("revision"),
			recordedAtAuthorityColumn(),
		},
		constraints: []ledgerConstraint{
			validConstraint("provider_realms_pkey", "p", "PRIMARY KEY (provider, realm_id)"),
			validConstraint(
				"provider_realms_provider_check",
				"c",
				"CHECK ((provider = ANY (ARRAY['clerk'::text, 'rongcloud'::text])))",
			),
			validConstraint("provider_realms_provider_not_null", "n", "NOT NULL provider"),
			validConstraint(
				"provider_realms_realm_id_check",
				"c",
				"CHECK ((((octet_length(realm_id) >= 5) AND (octet_length(realm_id) <= 128)) AND (realm_id ~ '^rlm_[A-Za-z0-9]([A-Za-z0-9_-]{0,122}[A-Za-z0-9])?$'::text)))",
			),
			validConstraint("provider_realms_realm_id_not_null", "n", "NOT NULL realm_id"),
			validConstraint("provider_realms_recorded_at_not_null", "n", "NOT NULL recorded_at"),
			validConstraint(
				"provider_realms_revision_check",
				"c",
				"CHECK (((revision >= 1) AND (revision <= '9223372036854775807'::bigint)))",
			),
			validConstraint("provider_realms_revision_not_null", "n", "NOT NULL revision"),
			validConstraint(
				"provider_realms_status_check",
				"c",
				"CHECK ((status = ANY (ARRAY['active'::text, 'disabled'::text])))",
			),
			validConstraint("provider_realms_status_not_null", "n", "NOT NULL status"),
		},
	}
}

func tenantsSpec() authorityTableSpec {
	return authorityTableSpec{
		name:             "tenants",
		rowSecurity:      true,
		forceRowSecurity: true,
		columns: []ledgerColumn{
			textAuthorityColumn("tenant_id"),
			textAuthorityColumn("status"),
			bigintAuthorityColumn("revision"),
			recordedAtAuthorityColumn(),
		},
		constraints: []ledgerConstraint{
			validConstraint("tenants_pkey", "p", "PRIMARY KEY (tenant_id)"),
			validConstraint("tenants_recorded_at_not_null", "n", "NOT NULL recorded_at"),
			validConstraint(
				"tenants_revision_check",
				"c",
				"CHECK (((revision >= 1) AND (revision <= '9223372036854775807'::bigint)))",
			),
			validConstraint("tenants_revision_not_null", "n", "NOT NULL revision"),
			validConstraint(
				"tenants_status_check",
				"c",
				"CHECK ((status = ANY (ARRAY['active'::text, 'suspended'::text, 'closed'::text])))",
			),
			validConstraint("tenants_status_not_null", "n", "NOT NULL status"),
			validConstraint(
				"tenants_tenant_id_check",
				"c",
				"CHECK ((((octet_length(tenant_id) >= 5) AND (octet_length(tenant_id) <= 128)) AND (tenant_id ~ '^ten_[A-Za-z0-9]([A-Za-z0-9_-]{0,122}[A-Za-z0-9])?$'::text)))",
			),
			validConstraint("tenants_tenant_id_not_null", "n", "NOT NULL tenant_id"),
		},
		policies: []authorityPolicy{exactTenantPolicy("tenants_exact_tenant")},
	}
}

func workspacesSpec() authorityTableSpec {
	return authorityTableSpec{
		name:             "workspaces",
		rowSecurity:      true,
		forceRowSecurity: true,
		columns: []ledgerColumn{
			textAuthorityColumn("tenant_id"),
			textAuthorityColumn("workspace_id"),
			textAuthorityColumn("status"),
			bigintAuthorityColumn("revision"),
			recordedAtAuthorityColumn(),
		},
		constraints: []ledgerConstraint{
			validConstraint("workspaces_pkey", "p", "PRIMARY KEY (tenant_id, workspace_id)"),
			validConstraint("workspaces_recorded_at_not_null", "n", "NOT NULL recorded_at"),
			validConstraint(
				"workspaces_revision_check",
				"c",
				"CHECK (((revision >= 1) AND (revision <= '9223372036854775807'::bigint)))",
			),
			validConstraint("workspaces_revision_not_null", "n", "NOT NULL revision"),
			validConstraint(
				"workspaces_status_check",
				"c",
				"CHECK ((status = ANY (ARRAY['active'::text, 'archived'::text, 'closed'::text])))",
			),
			validConstraint("workspaces_status_not_null", "n", "NOT NULL status"),
			validConstraint(
				"workspaces_tenant_fk",
				"f",
				"FOREIGN KEY (tenant_id) REFERENCES wanwork_im.tenants(tenant_id) ON DELETE RESTRICT",
			),
			validConstraint("workspaces_tenant_id_not_null", "n", "NOT NULL tenant_id"),
			validConstraint(
				"workspaces_workspace_id_check",
				"c",
				"CHECK ((((octet_length(workspace_id) >= 5) AND (octet_length(workspace_id) <= 128)) AND (workspace_id ~ '^wsp_[A-Za-z0-9]([A-Za-z0-9_-]{0,122}[A-Za-z0-9])?$'::text)))",
			),
			validConstraint("workspaces_workspace_id_not_null", "n", "NOT NULL workspace_id"),
		},
		policies: []authorityPolicy{exactTenantPolicy("workspaces_exact_tenant")},
	}
}

func textAuthorityColumn(name string) ledgerColumn {
	return ledgerColumn{
		name: name, formatType: "text", notNull: true,
		collationNamespace: stringPointer("pg_catalog"), collationName: stringPointer("C"),
	}
}

func bigintAuthorityColumn(name string) ledgerColumn {
	return ledgerColumn{name: name, formatType: "bigint", notNull: true}
}

func recordedAtAuthorityColumn() ledgerColumn {
	return ledgerColumn{
		name: "recorded_at", formatType: "timestamp with time zone", notNull: true,
		defaultSQL: stringPointer("clock_timestamp()"),
	}
}

func validConstraint(name, kind, definition string) ledgerConstraint {
	return ledgerConstraint{name: name, kind: kind, definition: definition, validated: true}
}

func exactTenantPolicy(name string) authorityPolicy {
	expression := "(tenant_id = current_setting('wanwork.tenant_id'::text, true))"
	return authorityPolicy{
		name:       name,
		command:    "*",
		permissive: true,
		roles:      "{0}",
		usingSQL:   expression,
		checkSQL:   expression,
	}
}
