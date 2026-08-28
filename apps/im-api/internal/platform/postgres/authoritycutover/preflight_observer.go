package authoritycutover

import (
	"context"
	"errors"
	"slices"
	"time"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/platform/postgres/migrations"
	"github.com/jackc/pgx/v5"
)

var (
	ErrInvalidPreflightObservation     = errors.New("invalid PostgreSQL authority preflight observation")
	ErrUntrustedPreflightObservation   = errors.New("untrusted PostgreSQL authority preflight observation")
	ErrPreflightObservationUnavailable = errors.New("PostgreSQL authority preflight observation unavailable")
)

// PreflightArtifactVerifier is an injected deployment-controller trust boundary. Implementations
// must read authenticated artifact metadata and return the digest actually observed; returning the
// expected input without readback is not verification. Provider errors and panics become unknown
// checks and are never copied into the report.
type PreflightArtifactVerifier interface {
	BackupAttestationDigest(context.Context, string) (string, error)
	ReleaseArtifactDigest(context.Context, string, string) (string, error)
}

type preflightTransportVerifier func(*pgx.Conn, PostgreSQLClusterProbeExpectation) bool

type preflightClusterObservation struct {
	CatalogVersionNo       int    `json:"catalogVersionNo"`
	Database               string `json:"database"`
	InRecovery             bool   `json:"inRecovery"`
	PGControlVersion       int    `json:"pgControlVersion"`
	PostgreSQLMajor        int    `json:"postgresqlMajor"`
	SystemIdentifierDigest string `json:"systemIdentifierDigest"`
}

type preflightRoleObservation struct {
	BypassRLS       bool   `json:"bypassRls"`
	ConnectionLimit int    `json:"connectionLimit"`
	CreateDatabase  bool   `json:"createDatabase"`
	CreateRole      bool   `json:"createRole"`
	Inherit         bool   `json:"inherit"`
	Login           bool   `json:"login"`
	Name            string `json:"name"`
	Replication     bool   `json:"replication"`
	Settings        bool   `json:"settings"`
	Superuser       bool   `json:"superuser"`
	ValidUntil      bool   `json:"validUntil"`
}

type preflightDatabaseInventoryObservation struct {
	ApplicationLedgerExists bool  `json:"applicationLedgerExists"`
	ApplicationLedgerRows   int64 `json:"applicationLedgerRows"`
	DatabaseScopedObjects   int64 `json:"databaseScopedObjects"`
	UserNamespaceObjects    int64 `json:"userNamespaceObjects"`
	UserSchemas             int64 `json:"userSchemas"`
}

type preflightDatabaseObservation struct {
	AllowConnections bool   `json:"allowConnections"`
	ConnectionLimit  int    `json:"connectionLimit"`
	Database         string `json:"database"`
	Owner            string `json:"owner"`
	Template         bool   `json:"template"`
}

type preflightArtifactObservation struct {
	ExpectedDigest string `json:"expectedDigest"`
	ObservedDigest string `json:"observedDigest"`
	Status         string `json:"status"`
}

// ObservePreflightReport performs fixed, read-only PostgreSQL catalog observation and binds it to
// the exact verified approval and plan. A returned report may be block/unknown; only
// ValidatePreflightReport accepts a fresh all-pass report, and even that does not authorize writes.
func ObservePreflightReport(
	ctx context.Context,
	connection *pgx.Conn,
	plan Plan,
	approval VerifiedApproval,
	artifacts PreflightArtifactVerifier,
	observedAt time.Time,
) (PreflightReport, error) {
	return observePreflightReport(
		ctx,
		connection,
		plan,
		approval,
		artifacts,
		observedAt,
		verifyClusterTLSTransport,
	)
}

func observePreflightReport(
	ctx context.Context,
	connection *pgx.Conn,
	plan Plan,
	approval VerifiedApproval,
	artifacts PreflightArtifactVerifier,
	observedAt time.Time,
	verifyTransport preflightTransportVerifier,
) (PreflightReport, error) {
	if ctx == nil || connection == nil || connection.IsClosed() || verifyTransport == nil {
		return PreflightReport{}, ErrInvalidPreflightObservation
	}
	if !validPlanSnapshot(plan.snapshot, true) ||
		!verifiedApprovalBindsPlanAt(approval, plan, observedAt) {
		return PreflightReport{}, ErrUntrustedPreflightObservation
	}
	snapshot := plan.Snapshot()
	provisionerLogin, uniqueProvisioner := provisionerLoginRole(snapshot.Credentials)
	if !uniqueProvisioner {
		return PreflightReport{}, ErrInvalidPreflightObservation
	}
	expectation := PostgreSQLClusterProbeExpectation{
		Database:        snapshot.Target.Database,
		LoginRole:       provisionerLogin,
		PostgreSQLMajor: snapshot.Target.PostgreSQLMajor,
		ServerIdentity:  snapshot.Target.ServerIdentity,
		TLS:             snapshot.Target.TLS,
	}
	if !validClusterProbeExpectation(expectation) || !verifyTransport(connection, expectation) {
		return PreflightReport{}, ErrUntrustedPreflightObservation
	}

	transaction, err := connection.BeginTx(ctx, pgx.TxOptions{
		IsoLevel:   pgx.RepeatableRead,
		AccessMode: pgx.ReadOnly,
	})
	if err != nil {
		return PreflightReport{}, ErrPreflightObservationUnavailable
	}
	defer func() { _ = transaction.Rollback(context.Background()) }()

	observations, err := observePreflightDatabase(ctx, transaction, snapshot, provisionerLogin)
	if err != nil {
		return PreflightReport{}, err
	}
	if err := transaction.Commit(ctx); err != nil {
		return PreflightReport{}, ErrPreflightObservationUnavailable
	}

	observations["backup/attestation"] = observeArtifactDigest(
		ctx,
		artifacts,
		snapshot.Backup.AttestationDigest,
		func(verifier PreflightArtifactVerifier) (string, error) {
			return verifier.BackupAttestationDigest(ctx, snapshot.Backup.ArtifactReference)
		},
	)
	observations["source/release-artifact"] = observeArtifactDigest(
		ctx,
		artifacts,
		snapshot.Source.ReleaseArtifactDigest,
		func(verifier PreflightArtifactVerifier) (string, error) {
			return verifier.ReleaseArtifactDigest(ctx, snapshot.Source.Commit, snapshot.Source.Tree)
		},
	)
	return buildPreflightReport(plan, approval, observedAt, observations)
}

func observePreflightDatabase(
	ctx context.Context,
	transaction pgx.Tx,
	plan PlanSnapshot,
	provisionerLogin string,
) (map[string]preflightCheckObservation, error) {
	var (
		catalogVersionNo int
		currentDatabase  string
		currentUser      string
		inRecovery       bool
		isolation        string
		pgControlVersion int
		readOnly         bool
		serverVersion    int
		sessionUser      string
		systemIdentifier string
	)
	if err := transaction.QueryRow(ctx, `
SELECT session_user,
       current_user,
       current_database(),
       pg_catalog.current_setting('server_version_num')::integer,
       pg_catalog.current_setting('transaction_isolation'),
       pg_catalog.current_setting('transaction_read_only')::boolean,
       pg_catalog.pg_is_in_recovery(),
       control.system_identifier::text,
       control.pg_control_version,
       control.catalog_version_no
FROM pg_catalog.pg_control_system() AS control`).Scan(
		&sessionUser,
		&currentUser,
		&currentDatabase,
		&serverVersion,
		&isolation,
		&readOnly,
		&inRecovery,
		&systemIdentifier,
		&pgControlVersion,
		&catalogVersionNo,
	); err != nil {
		return nil, ErrPreflightObservationUnavailable
	}
	if sessionUser != provisionerLogin || currentUser != provisionerLogin ||
		currentDatabase != plan.Target.Database || isolation != "repeatable read" || !readOnly {
		return nil, ErrUntrustedPreflightObservation
	}

	cluster := preflightClusterObservation{
		CatalogVersionNo:       catalogVersionNo,
		Database:               currentDatabase,
		InRecovery:             inRecovery,
		PGControlVersion:       pgControlVersion,
		PostgreSQLMajor:        serverVersion / 10000,
		SystemIdentifierDigest: digestPostgreSQLSystemIdentifier(systemIdentifier),
	}
	clusterPass := cluster.CatalogVersionNo == plan.Target.CatalogVersionNo &&
		cluster.PGControlVersion == plan.Target.PGControlVersion &&
		cluster.PostgreSQLMajor == plan.Target.PostgreSQLMajor && !cluster.InRecovery &&
		cluster.SystemIdentifierDigest == plan.Target.SystemIdentifierDigest
	observations := map[string]preflightCheckObservation{
		"cluster/identity": {
			outcome:  preflightOutcome(clusterPass),
			evidence: cluster,
		},
		"tls/transport": {
			outcome: PreflightCheckPass,
			evidence: struct {
				CADigest       string `json:"caDigest"`
				Mode           string `json:"mode"`
				ServerIdentity string `json:"serverIdentity"`
			}{
				CADigest:       plan.Target.TLS.CADigest,
				Mode:           plan.Target.TLS.Mode,
				ServerIdentity: plan.Target.ServerIdentity,
			},
		},
	}

	manifest := migrationsAuthorityManifest(plan.Authority.Manifest)
	cutover, err := migrations.CurrentAuthorityCutoverSpecification(
		manifest,
		provisionerLogin,
		plan.Authority.ProvisionerGrantorRole,
	)
	if err != nil {
		return nil, ErrUntrustedPreflightObservation
	}
	databaseObservation, roles, err := readPreflightRoles(
		ctx,
		transaction,
		plan.Target.Database,
		[]string{cutover.DatabaseOwner.Role, cutover.Provisioner.Name},
	)
	if err != nil {
		return nil, err
	}
	databasePass := databaseObservation.Database == plan.Target.Database &&
		databaseObservation.AllowConnections && databaseObservation.ConnectionLimit == -1 &&
		!databaseObservation.Template
	observations["database/existence"] = preflightCheckObservation{
		outcome:  preflightOutcome(databasePass),
		evidence: databaseObservation,
	}
	ownerPass := databaseObservation.Owner == cutover.DatabaseOwner.Role && len(roles) == 2 &&
		preflightRoleMatchesDatabaseOwner(roles, cutover.DatabaseOwner)
	provisionerPass := len(roles) == 2 &&
		slices.Contains(roles, preflightRoleFromSpecification(cutover.Provisioner))
	observations["authority/database-owner-attributes"] = preflightCheckObservation{
		outcome: preflightOutcome(ownerPass),
		evidence: struct {
			DatabaseOwner string                     `json:"databaseOwner"`
			Roles         []preflightRoleObservation `json:"roles"`
		}{DatabaseOwner: databaseObservation.Owner, Roles: slices.Clone(roles)},
	}
	observations["authority/provisioner-attributes"] = preflightCheckObservation{
		outcome: preflightOutcome(provisionerPass),
		evidence: struct {
			Provisioner string                     `json:"provisioner"`
			Roles       []preflightRoleObservation `json:"roles"`
		}{Provisioner: provisionerLogin, Roles: slices.Clone(roles)},
	}

	memberships, err := readPreflightMemberships(
		ctx,
		transaction,
		[]string{cutover.DatabaseOwner.Role, cutover.Provisioner.Name},
	)
	if err != nil {
		return nil, err
	}
	observations["authority/provisioner-membership"] = preflightCheckObservation{
		outcome:  preflightOutcome(slices.Equal(memberships, []migrations.AuthorityMembershipSpecification{cutover.Membership})),
		evidence: memberships,
	}

	privileges, err := readPreflightDatabasePrivileges(ctx, transaction, plan.Target.Database)
	if err != nil {
		return nil, err
	}
	observations["authority/provisioner-connect"] = preflightCheckObservation{
		outcome:  preflightOutcome(slices.Equal(privileges, []migrations.AuthorityPrivilegeSpecification{cutover.ProvisionerConnect})),
		evidence: privileges,
	}

	inventory, err := readPreflightDatabaseInventory(ctx, transaction)
	if err != nil {
		return nil, err
	}
	empty := inventory.UserSchemas == 0 && inventory.UserNamespaceObjects == 0 &&
		inventory.DatabaseScopedObjects == 0 && !inventory.ApplicationLedgerExists &&
		inventory.ApplicationLedgerRows == 0
	classificationPass := (plan.SchemaTransition.NonEmptyClassification == ClassificationEmpty && empty) ||
		(plan.SchemaTransition.NonEmptyClassification == ClassificationNonEmpty && !empty)
	observations["database/non-empty-classification"] = preflightCheckObservation{
		outcome: preflightOutcome(classificationPass),
		evidence: struct {
			ActualEmpty bool                                  `json:"actualEmpty"`
			Expected    NonEmptyClassification                `json:"expected"`
			Inventory   preflightDatabaseInventoryObservation `json:"inventory"`
		}{
			ActualEmpty: empty,
			Expected:    plan.SchemaTransition.NonEmptyClassification,
			Inventory:   inventory,
		},
	}
	return observations, nil
}

func readPreflightRoles(
	ctx context.Context,
	transaction pgx.Tx,
	database string,
	roleNames []string,
) (preflightDatabaseObservation, []preflightRoleObservation, error) {
	var databaseObservation preflightDatabaseObservation
	if err := transaction.QueryRow(ctx, `
SELECT database_value.datname,
       owner.rolname,
       database_value.datallowconn,
       database_value.datconnlimit,
       database_value.datistemplate
FROM pg_catalog.pg_database AS database_value
JOIN pg_catalog.pg_roles AS owner ON owner.oid = database_value.datdba
WHERE database_value.datname = current_database()
  AND database_value.datname = $1`, database).Scan(
		&databaseObservation.Database,
		&databaseObservation.Owner,
		&databaseObservation.AllowConnections,
		&databaseObservation.ConnectionLimit,
		&databaseObservation.Template,
	); err != nil {
		return preflightDatabaseObservation{}, nil, ErrPreflightObservationUnavailable
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
       role_value.rolconfig IS NOT NULL OR EXISTS (
           SELECT 1
           FROM pg_catalog.pg_db_role_setting AS role_setting
           WHERE role_setting.setrole = role_value.oid
       )
FROM pg_catalog.pg_roles AS role_value
WHERE role_value.rolname = ANY($1::text[])
ORDER BY role_value.rolname`, roleNames)
	if err != nil {
		return preflightDatabaseObservation{}, nil, ErrPreflightObservationUnavailable
	}
	defer rows.Close()
	roles := make([]preflightRoleObservation, 0, len(roleNames))
	for rows.Next() {
		var role preflightRoleObservation
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
			return preflightDatabaseObservation{}, nil, ErrPreflightObservationUnavailable
		}
		roles = append(roles, role)
	}
	if rows.Err() != nil {
		return preflightDatabaseObservation{}, nil, ErrPreflightObservationUnavailable
	}
	return databaseObservation, roles, nil
}

func readPreflightMemberships(
	ctx context.Context,
	transaction pgx.Tx,
	roleNames []string,
) ([]migrations.AuthorityMembershipSpecification, error) {
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
ORDER BY granted_role.rolname, member_role.rolname, grantor_role.rolname`, roleNames)
	if err != nil {
		return nil, ErrPreflightObservationUnavailable
	}
	defer rows.Close()
	values := make([]migrations.AuthorityMembershipSpecification, 0)
	for rows.Next() {
		var value migrations.AuthorityMembershipSpecification
		if err := rows.Scan(
			&value.GrantedRole,
			&value.MemberRole,
			&value.GrantorRole,
			&value.AdminOption,
			&value.InheritOption,
			&value.SetOption,
		); err != nil {
			return nil, ErrPreflightObservationUnavailable
		}
		values = append(values, value)
	}
	if rows.Err() != nil {
		return nil, ErrPreflightObservationUnavailable
	}
	return values, nil
}

func readPreflightDatabasePrivileges(
	ctx context.Context,
	transaction pgx.Tx,
	database string,
) ([]migrations.AuthorityPrivilegeSpecification, error) {
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
  AND database_value.datname = $1
  AND acl.grantee <> database_value.datdba
ORDER BY grantee.rolname, grantor.rolname, acl.privilege_type`, database)
	if err != nil {
		return nil, ErrPreflightObservationUnavailable
	}
	defer rows.Close()
	values := make([]migrations.AuthorityPrivilegeSpecification, 0)
	for rows.Next() {
		value := migrations.AuthorityPrivilegeSpecification{Scope: migrations.AuthorityPrivilegeDatabase}
		if err := rows.Scan(
			&value.Object,
			&value.GranteeRole,
			&value.GrantorRole,
			&value.Privilege,
			&value.Grantable,
		); err != nil {
			return nil, ErrPreflightObservationUnavailable
		}
		values = append(values, value)
	}
	if rows.Err() != nil {
		return nil, ErrPreflightObservationUnavailable
	}
	return values, nil
}

func readPreflightDatabaseInventory(
	ctx context.Context,
	transaction pgx.Tx,
) (preflightDatabaseInventoryObservation, error) {
	var inventory preflightDatabaseInventoryObservation
	if err := transaction.QueryRow(ctx, `
WITH user_namespaces AS (
    SELECT namespace.oid, namespace.nspname
    FROM pg_catalog.pg_namespace AS namespace
    WHERE namespace.nspname <> 'information_schema'
      AND namespace.nspname NOT LIKE 'pg\_%' ESCAPE '\'
),
user_namespace_objects AS (
    SELECT relation_value.oid FROM pg_catalog.pg_class AS relation_value
    JOIN user_namespaces AS namespace ON namespace.oid = relation_value.relnamespace
    UNION ALL
    SELECT procedure_value.oid FROM pg_catalog.pg_proc AS procedure_value
    JOIN user_namespaces AS namespace ON namespace.oid = procedure_value.pronamespace
    UNION ALL
    SELECT type_value.oid FROM pg_catalog.pg_type AS type_value
    JOIN user_namespaces AS namespace ON namespace.oid = type_value.typnamespace
    UNION ALL
    SELECT collation_value.oid FROM pg_catalog.pg_collation AS collation_value
    JOIN user_namespaces AS namespace ON namespace.oid = collation_value.collnamespace
    UNION ALL
    SELECT conversion_value.oid FROM pg_catalog.pg_conversion AS conversion_value
    JOIN user_namespaces AS namespace ON namespace.oid = conversion_value.connamespace
    UNION ALL
    SELECT operator_value.oid FROM pg_catalog.pg_operator AS operator_value
    JOIN user_namespaces AS namespace ON namespace.oid = operator_value.oprnamespace
    UNION ALL
    SELECT operator_class.oid FROM pg_catalog.pg_opclass AS operator_class
    JOIN user_namespaces AS namespace ON namespace.oid = operator_class.opcnamespace
    UNION ALL
    SELECT operator_family.oid FROM pg_catalog.pg_opfamily AS operator_family
    JOIN user_namespaces AS namespace ON namespace.oid = operator_family.opfnamespace
    UNION ALL
    SELECT configuration_value.oid FROM pg_catalog.pg_ts_config AS configuration_value
    JOIN user_namespaces AS namespace ON namespace.oid = configuration_value.cfgnamespace
    UNION ALL
    SELECT dictionary_value.oid FROM pg_catalog.pg_ts_dict AS dictionary_value
    JOIN user_namespaces AS namespace ON namespace.oid = dictionary_value.dictnamespace
    UNION ALL
    SELECT parser_value.oid FROM pg_catalog.pg_ts_parser AS parser_value
    JOIN user_namespaces AS namespace ON namespace.oid = parser_value.prsnamespace
    UNION ALL
    SELECT template_value.oid FROM pg_catalog.pg_ts_template AS template_value
    JOIN user_namespaces AS namespace ON namespace.oid = template_value.tmplnamespace
),
database_scoped_objects AS (
    SELECT extension_value.oid FROM pg_catalog.pg_extension AS extension_value WHERE extension_value.extname <> 'plpgsql'
    UNION ALL SELECT wrapper.oid FROM pg_catalog.pg_foreign_data_wrapper AS wrapper
    UNION ALL SELECT server_value.oid FROM pg_catalog.pg_foreign_server AS server_value
    UNION ALL SELECT publication_value.oid FROM pg_catalog.pg_publication AS publication_value
    UNION ALL SELECT subscription_value.oid FROM pg_catalog.pg_subscription AS subscription_value
        WHERE subscription_value.subdbid = (SELECT oid FROM pg_catalog.pg_database WHERE datname = current_database())
    UNION ALL SELECT trigger_value.oid FROM pg_catalog.pg_event_trigger AS trigger_value
    UNION ALL SELECT default_acl.oid FROM pg_catalog.pg_default_acl AS default_acl
    UNION ALL SELECT large_object.oid FROM pg_catalog.pg_largeobject_metadata AS large_object
    UNION ALL SELECT cast_value.oid FROM pg_catalog.pg_cast AS cast_value WHERE cast_value.oid >= 16384
    UNION ALL SELECT language_value.oid FROM pg_catalog.pg_language AS language_value
        WHERE language_value.oid >= 16384 AND language_value.lanname <> 'plpgsql'
    UNION ALL SELECT transform_value.oid FROM pg_catalog.pg_transform AS transform_value WHERE transform_value.oid >= 16384
)
SELECT (SELECT count(*) FROM user_namespaces WHERE nspname <> 'public'),
       (SELECT count(*) FROM user_namespace_objects),
       (SELECT count(*) FROM database_scoped_objects),
       pg_catalog.to_regclass('wanwork_meta.schema_migrations') IS NOT NULL`).Scan(
		&inventory.UserSchemas,
		&inventory.UserNamespaceObjects,
		&inventory.DatabaseScopedObjects,
		&inventory.ApplicationLedgerExists,
	); err != nil {
		return preflightDatabaseInventoryObservation{}, ErrPreflightObservationUnavailable
	}
	if inventory.ApplicationLedgerExists {
		if err := transaction.QueryRow(ctx, `
SELECT count(*)
FROM wanwork_meta.schema_migrations`).Scan(&inventory.ApplicationLedgerRows); err != nil {
			return preflightDatabaseInventoryObservation{}, ErrPreflightObservationUnavailable
		}
	}
	return inventory, nil
}

func preflightRoleMatchesDatabaseOwner(
	roles []preflightRoleObservation,
	expected migrations.AuthorityDatabaseOwnerSpecification,
) bool {
	return slices.Contains(roles, preflightRoleObservation{
		BypassRLS:       expected.BypassRLS,
		ConnectionLimit: expected.ConnectionLimit,
		CreateDatabase:  expected.CreateDatabase,
		CreateRole:      expected.CreateRole,
		Inherit:         expected.Inherit,
		Login:           expected.Login,
		Name:            expected.Role,
		Replication:     expected.Replication,
		Settings:        expected.Settings,
		Superuser:       expected.Superuser,
		ValidUntil:      expected.ValidUntil,
	})
}

func preflightRoleFromSpecification(
	expected migrations.AuthorityRoleSpecification,
) preflightRoleObservation {
	return preflightRoleObservation{
		BypassRLS:       expected.BypassRLS,
		ConnectionLimit: expected.ConnectionLimit,
		CreateDatabase:  expected.CreateDatabase,
		CreateRole:      expected.CreateRole,
		Inherit:         expected.Inherit,
		Login:           expected.Login,
		Name:            expected.Name,
		Replication:     expected.Replication,
		Settings:        expected.Settings,
		Superuser:       expected.Superuser,
		ValidUntil:      expected.ValidUntil,
	}
}

func preflightOutcome(pass bool) PreflightCheckOutcome {
	if pass {
		return PreflightCheckPass
	}
	return PreflightCheckBlock
}

func observeArtifactDigest(
	ctx context.Context,
	verifier PreflightArtifactVerifier,
	expected string,
	read func(PreflightArtifactVerifier) (string, error),
) (observation preflightCheckObservation) {
	observation = preflightCheckObservation{
		outcome: PreflightCheckUnknown,
		evidence: preflightArtifactObservation{
			ExpectedDigest: expected,
			Status:         "unavailable",
		},
	}
	if ctx == nil || verifier == nil || read == nil {
		return observation
	}
	defer func() {
		if recover() != nil {
			observation = preflightCheckObservation{
				outcome: PreflightCheckUnknown,
				evidence: preflightArtifactObservation{
					ExpectedDigest: expected,
					Status:         "provider-failed",
				},
			}
		}
	}()
	observed, err := read(verifier)
	if err != nil || !canonicalDigest.MatchString(observed) {
		return observation
	}
	status := "mismatch"
	outcome := PreflightCheckBlock
	if observed == expected {
		status = "verified"
		outcome = PreflightCheckPass
	}
	return preflightCheckObservation{
		outcome: outcome,
		evidence: preflightArtifactObservation{
			ExpectedDigest: expected,
			ObservedDigest: observed,
			Status:         status,
		},
	}
}
