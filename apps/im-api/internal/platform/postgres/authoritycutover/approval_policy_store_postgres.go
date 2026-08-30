package authoritycutover

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/platform/postgres/migrations"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

const (
	ApprovalPolicyControlStoreSchemaFormat           = "wanwork.im.postgres-approval-policy-control-store/1"
	ApprovalPolicyControlStoreSchemaFormatV2         = "wanwork.im.postgres-approval-policy-control-store/2"
	approvalPolicyControlStoreContractDigestDomain   = "wanwork.im/postgres-approval-policy-control-store-contract/1\n"
	approvalPolicyControlStoreContractDigestDomainV2 = "wanwork.im/postgres-approval-policy-control-store-contract/2\n"
	approvalPolicyControlStoreSchemaName             = "wanwork_policy_control"
	approvalPolicyControlStoreIdentityFunction       = "read_store_identity"
	approvalPolicyControlStoreReadFunction           = "read_approval_policy_state"
	approvalPolicyControlStoreActivateFunction       = "compare_and_activate_approval_policy"
	approvalPolicyControlStoreFenceReadFunction      = "read_approval_execution_fence"
	approvalPolicyControlStoreFenceOpenFunction      = "compare_and_open_approval_execution_fence"
	approvalPolicyControlStoreAttemptReadFunction    = "read_approval_execution_attempt"
	approvalPolicyControlStoreAttemptIssueFunction   = "compare_and_issue_approval_execution_attempt"
	approvalPolicyControlStoreAdmissionFunction      = "approval_execution_admission_is_valid"
	approvalPolicyControlStoreTargetLockSeed         = int64(7318470027)
	approvalPolicyControlStoreCleanupTimeout         = 5 * time.Second
)

var (
	ErrInvalidPostgresApprovalPolicyStore   = errors.New("invalid PostgreSQL approval policy control store")
	ErrUntrustedPostgresApprovalPolicyStore = errors.New("untrusted PostgreSQL approval policy control store")
)

type ApprovalPolicyControlStoreExpectation struct {
	ControlActivatorRole          string
	ControlAttemptIssuerRole      string
	ControlDatabase               string
	ControlFencerRole             string
	ControlLoginRole              string
	ControlOwnerRole              string
	ControlReaderRole             string
	ControlPostgreSQLMajor        int
	ControlServerIdentity         string
	ControlSystemIdentifierDigest string
	ControlTLS                    TLSProfile
	PolicyID                      string
	PolicyTarget                  ApprovalPolicyTarget
}

type approvalPolicyControlStoreTransportVerifier func(
	*pgx.Conn,
	PostgreSQLClusterProbeExpectation,
) bool

type approvalPolicyControlStoreSchemaVersion uint8

type approvalPolicyControlStoreAccess uint8

const (
	approvalPolicyControlStoreSchemaVersionV1       approvalPolicyControlStoreSchemaVersion = 1
	approvalPolicyControlStoreSchemaVersionV2       approvalPolicyControlStoreSchemaVersion = 2
	approvalPolicyControlStoreAccessV1Activator     approvalPolicyControlStoreAccess        = 1
	approvalPolicyControlStoreAccessV2Activator     approvalPolicyControlStoreAccess        = 2
	approvalPolicyControlStoreAccessV2Fencer        approvalPolicyControlStoreAccess        = 3
	approvalPolicyControlStoreAccessV2AttemptIssuer approvalPolicyControlStoreAccess        = 4
)

// PostgresApprovalPolicyActivationStore persists one policy namespace in a physically separate
// PostgreSQL control cluster. It calls only fixed, schema-qualified functions and never creates or
// migrates schema at runtime. The deployment login must have EXECUTE only; direct table writes are
// outside this API and rejected by the accompanying IaC contract.
type PostgresApprovalPolicyActivationStore struct {
	access          approvalPolicyControlStoreAccess
	expectation     ApprovalPolicyControlStoreExpectation
	namespace       ApprovalPolicyNamespace
	pool            *pgxpool.Pool
	schemaVersion   approvalPolicyControlStoreSchemaVersion
	verifyTransport approvalPolicyControlStoreTransportVerifier
}

func NewPostgresApprovalPolicyActivationStore(
	pool *pgxpool.Pool,
	expectation ApprovalPolicyControlStoreExpectation,
) (*PostgresApprovalPolicyActivationStore, error) {
	return newPostgresApprovalPolicyActivationStore(pool, expectation, verifyClusterTLSTransport)
}

func newPostgresApprovalPolicyActivationStore(
	pool *pgxpool.Pool,
	expectation ApprovalPolicyControlStoreExpectation,
	verifyTransport approvalPolicyControlStoreTransportVerifier,
) (*PostgresApprovalPolicyActivationStore, error) {
	if pool == nil || verifyTransport == nil || !validApprovalPolicyControlStoreExpectation(expectation) {
		return nil, ErrInvalidPostgresApprovalPolicyStore
	}
	return newPostgresApprovalPolicyActivationStoreVersion(
		pool,
		expectation,
		verifyTransport,
		approvalPolicyControlStoreSchemaVersionV1,
	)
}

func NewPostgresApprovalPolicyActivationStoreV2(
	pool *pgxpool.Pool,
	expectation ApprovalPolicyControlStoreExpectation,
) (*PostgresApprovalPolicyActivationStore, error) {
	return newPostgresApprovalPolicyActivationStoreV2(
		pool,
		expectation,
		verifyClusterTLSTransport,
	)
}

func newPostgresApprovalPolicyActivationStoreV2(
	pool *pgxpool.Pool,
	expectation ApprovalPolicyControlStoreExpectation,
	verifyTransport approvalPolicyControlStoreTransportVerifier,
) (*PostgresApprovalPolicyActivationStore, error) {
	if pool == nil || verifyTransport == nil ||
		!validApprovalPolicyControlStoreExpectationV2(expectation) ||
		expectation.ControlLoginRole != expectation.ControlActivatorRole {
		return nil, ErrInvalidPostgresApprovalPolicyStore
	}
	return newPostgresApprovalPolicyActivationStoreVersion(
		pool,
		expectation,
		verifyTransport,
		approvalPolicyControlStoreSchemaVersionV2,
	)
}

func newPostgresApprovalPolicyActivationStoreVersion(
	pool *pgxpool.Pool,
	expectation ApprovalPolicyControlStoreExpectation,
	verifyTransport approvalPolicyControlStoreTransportVerifier,
	schemaVersion approvalPolicyControlStoreSchemaVersion,
) (*PostgresApprovalPolicyActivationStore, error) {
	if schemaVersion != approvalPolicyControlStoreSchemaVersionV1 &&
		schemaVersion != approvalPolicyControlStoreSchemaVersionV2 {
		return nil, ErrInvalidPostgresApprovalPolicyStore
	}
	access := approvalPolicyControlStoreAccessV1Activator
	if schemaVersion == approvalPolicyControlStoreSchemaVersionV2 {
		access = approvalPolicyControlStoreAccessV2Activator
	}
	return &PostgresApprovalPolicyActivationStore{
		access:      access,
		expectation: expectation,
		namespace: ApprovalPolicyNamespace{
			PolicyID:     expectation.PolicyID,
			TargetDigest: digestApprovalPolicyTarget(expectation.PolicyTarget),
		},
		pool:            pool,
		schemaVersion:   schemaVersion,
		verifyTransport: verifyTransport,
	}, nil
}

func (store *PostgresApprovalPolicyActivationStore) Load(
	ctx context.Context,
	namespace ApprovalPolicyNamespace,
) (ApprovalPolicyStoredState, error) {
	if ctx == nil || store == nil || store.pool == nil || namespace != store.namespace {
		return ApprovalPolicyStoredState{}, ErrInvalidPostgresApprovalPolicyStore
	}
	connection, err := store.pool.Acquire(ctx)
	if err != nil {
		return ApprovalPolicyStoredState{}, ErrApprovalPolicyStoreUnavailable
	}
	defer connection.Release()
	if err := store.verifyConnection(ctx, connection.Conn()); err != nil {
		if errors.Is(err, ErrUntrustedPostgresApprovalPolicyStore) {
			return ApprovalPolicyStoredState{}, ErrUntrustedPostgresApprovalPolicyStore
		}
		return ApprovalPolicyStoredState{}, ErrApprovalPolicyStoreUnavailable
	}
	var (
		activationRecordDigest *string
		canonicalPolicy        []byte
		canonicalRecord        []byte
		policyDigest           *string
		revision               *int64
		stateStatus            string
	)
	err = connection.QueryRow(ctx, `
SELECT state.state_status,
       state.activation_record_digest,
       state.policy_digest,
       state.revision,
       state.canonical_policy,
       state.canonical_record
FROM wanwork_policy_control.read_approval_policy_state($1, $2) AS state`,
		namespace.PolicyID,
		namespace.TargetDigest,
	).Scan(
		&stateStatus,
		&activationRecordDigest,
		&policyDigest,
		&revision,
		&canonicalPolicy,
		&canonicalRecord,
	)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return ApprovalPolicyStoredState{}, ErrInvalidApprovalPolicyStoreState
		}
		return ApprovalPolicyStoredState{}, ErrApprovalPolicyStoreUnavailable
	}
	if stateStatus == "empty" {
		return ApprovalPolicyStoredState{}, ErrApprovalPolicyStoreEmpty
	}
	if stateStatus != "present" || activationRecordDigest == nil || policyDigest == nil || revision == nil {
		return ApprovalPolicyStoredState{}, ErrInvalidApprovalPolicyStoreState
	}
	if *revision <= 0 || uint64(*revision) > maximumApprovalPolicyRevision {
		return ApprovalPolicyStoredState{}, ErrInvalidApprovalPolicyStoreState
	}
	record, err := decodeApprovalPolicyActivationRecord(canonicalRecord)
	if err != nil || record.ActivationRecordDigest != *activationRecordDigest ||
		record.PolicyDigest != *policyDigest || record.Revision != uint64(*revision) ||
		record.PolicyID != namespace.PolicyID || record.TargetDigest != namespace.TargetDigest {
		return ApprovalPolicyStoredState{}, ErrInvalidApprovalPolicyStoreState
	}
	policy, policyCanonical, err := decodeApprovalPolicy(canonicalPolicy)
	if err != nil || !bytes.Equal(policyCanonical, canonicalPolicy) ||
		policy.PolicyID != record.PolicyID || policy.Revision != record.Revision ||
		policy.PolicyDigest != record.PolicyDigest ||
		policy.PreviousPolicyDigest != record.PreviousPolicyDigest ||
		digestApprovalPolicyEnvelope(canonicalPolicy) != record.PolicyEnvelopeDigest ||
		digestApprovalPolicyTarget(policy.Target) != record.TargetDigest {
		return ApprovalPolicyStoredState{}, ErrInvalidApprovalPolicyStoreState
	}
	return ApprovalPolicyStoredState{
		CanonicalPolicy: slices.Clone(canonicalPolicy),
		Head:            record.Head(),
		Record:          cloneApprovalPolicyActivationRecord(record),
	}, nil
}

func (store *PostgresApprovalPolicyActivationStore) CompareAndActivate(
	ctx context.Context,
	namespace ApprovalPolicyNamespace,
	expected ApprovalPolicyHead,
	record ApprovalPolicyActivationRecord,
	canonicalPolicy []byte,
) error {
	if ctx == nil || store == nil || store.pool == nil || namespace != store.namespace ||
		!validApprovalPolicyExpectedHead(expected, namespace) ||
		!validApprovalPolicyActivationRecord(record) || record.PolicyID != namespace.PolicyID ||
		record.TargetDigest != namespace.TargetDigest || record.Revision != expected.Revision+1 ||
		(expected.Revision == 0 && record.PreviousPolicyDigest != "") ||
		(expected.Revision > 0 && record.PreviousPolicyDigest != expected.PolicyDigest) {
		return ErrInvalidPostgresApprovalPolicyStore
	}
	policy, policyCanonical, err := decodeApprovalPolicy(canonicalPolicy)
	if err != nil || !bytes.Equal(policyCanonical, canonicalPolicy) ||
		policy.PolicyID != record.PolicyID || policy.Revision != record.Revision ||
		policy.PolicyDigest != record.PolicyDigest ||
		policy.PreviousPolicyDigest != record.PreviousPolicyDigest ||
		digestApprovalPolicyEnvelope(canonicalPolicy) != record.PolicyEnvelopeDigest ||
		digestApprovalPolicyTarget(policy.Target) != record.TargetDigest {
		return ErrInvalidPostgresApprovalPolicyStore
	}
	canonicalRecord, err := marshalApprovalPolicyActivationRecordCanonical(record)
	if err != nil || len(canonicalRecord) > maximumApprovalPolicyActivationRecordBytes {
		return ErrInvalidPostgresApprovalPolicyStore
	}
	connection, err := store.pool.Acquire(ctx)
	if err != nil {
		return ErrApprovalPolicyStoreUnavailable
	}
	released := false
	release := func() {
		if !released {
			connection.Release()
			released = true
		}
	}
	defer release()
	if err := store.verifyConnection(ctx, connection.Conn()); err != nil {
		if errors.Is(err, ErrUntrustedPostgresApprovalPolicyStore) {
			return ErrUntrustedPostgresApprovalPolicyStore
		}
		return ErrApprovalPolicyStoreUnavailable
	}
	var outcome string
	err = connection.QueryRow(ctx, `
SELECT wanwork_policy_control.compare_and_activate_approval_policy(
    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10
)`,
		namespace.PolicyID,
		namespace.TargetDigest,
		int64(expected.Revision),
		expected.PolicyDigest,
		expected.ActivationRecordDigest,
		int64(record.Revision),
		record.PolicyDigest,
		record.ActivationRecordDigest,
		canonicalPolicy,
		canonicalRecord,
	).Scan(&outcome)
	if err != nil {
		// The single function call is the transaction boundary. A transport, cancellation, or
		// server error cannot prove whether commit became durable. Quarantine this session so
		// authoritative readback cannot accidentally reuse a connection with unread results or
		// an unknown transaction state.
		quarantineApprovalPolicyControlConnection(connection)
		released = true
		return ErrApprovalPolicyCommitUncertain
	}
	switch outcome {
	case "committed":
		return nil
	case "conflict", "fence_open":
		return ErrApprovalPolicyActivationConflict
	case "corrupt":
		return ErrInvalidApprovalPolicyStoreState
	case "rejected":
		return ErrInvalidPostgresApprovalPolicyStore
	default:
		quarantineApprovalPolicyControlConnection(connection)
		released = true
		return ErrApprovalPolicyCommitUncertain
	}
}

func (store *PostgresApprovalPolicyActivationStore) verifyConnection(
	ctx context.Context,
	connection *pgx.Conn,
) error {
	if store == nil {
		return ErrUntrustedPostgresApprovalPolicyStore
	}
	return verifyApprovalPolicyControlStoreConnection(
		ctx,
		connection,
		store.expectation,
		store.access,
		store.verifyTransport,
	)
}

func verifyApprovalPolicyControlStoreConnection(
	ctx context.Context,
	connection *pgx.Conn,
	expectation ApprovalPolicyControlStoreExpectation,
	access approvalPolicyControlStoreAccess,
	verifyTransport approvalPolicyControlStoreTransportVerifier,
) error {
	if connection == nil || connection.IsClosed() {
		return ErrApprovalPolicyStoreUnavailable
	}
	if verifyTransport == nil || !validApprovalPolicyControlStoreAccess(expectation, access) {
		return ErrUntrustedPostgresApprovalPolicyStore
	}
	var (
		expectedSchemaDigest string
		expectedSchemaFormat string
		verifyCatalog        func(context.Context, *pgx.Conn, ApprovalPolicyControlStoreExpectation) bool
	)
	schemaVersion, ok := approvalPolicyControlStoreAccessSchemaVersion(access)
	if !ok {
		return ErrUntrustedPostgresApprovalPolicyStore
	}
	switch schemaVersion {
	case approvalPolicyControlStoreSchemaVersionV1:
		expectedSchemaDigest = CurrentApprovalPolicyControlStoreSchemaDigest()
		expectedSchemaFormat = ApprovalPolicyControlStoreSchemaFormat
		verifyCatalog = verifyApprovalPolicyControlStoreCatalog
	case approvalPolicyControlStoreSchemaVersionV2:
		expectedSchemaDigest = CurrentApprovalPolicyControlStoreSchemaDigestV2()
		expectedSchemaFormat = ApprovalPolicyControlStoreSchemaFormatV2
		verifyCatalog = verifyApprovalPolicyControlStoreCatalogV2
	default:
		return ErrUntrustedPostgresApprovalPolicyStore
	}
	probeExpectation := PostgreSQLClusterProbeExpectation{
		Database:        expectation.ControlDatabase,
		LoginRole:       expectation.ControlLoginRole,
		PostgreSQLMajor: expectation.ControlPostgreSQLMajor,
		ServerIdentity:  expectation.ControlServerIdentity,
		TLS:             expectation.ControlTLS,
	}
	if !verifyTransport(connection, probeExpectation) {
		return ErrUntrustedPostgresApprovalPolicyStore
	}
	var (
		currentRole         string
		readOnlyTransaction bool
		sessionRole         string
	)
	if err := connection.QueryRow(ctx, `
SELECT session_user::text,
       current_user::text,
       pg_catalog.current_setting('transaction_read_only')::boolean`).Scan(
		&sessionRole,
		&currentRole,
		&readOnlyTransaction,
	); err != nil {
		return approvalPolicyControlStoreVerificationQueryError(ctx, connection)
	}
	if sessionRole != expectation.ControlLoginRole ||
		currentRole != expectation.ControlLoginRole || readOnlyTransaction {
		return ErrUntrustedPostgresApprovalPolicyStore
	}
	var (
		database         string
		inRecovery       bool
		loginRole        string
		ownerRole        string
		schemaDigest     string
		schemaFormat     string
		serverVersion    int
		systemIdentifier string
	)
	err := connection.QueryRow(ctx, `
SELECT identity.login_role,
       identity.owner_role,
       identity.database_name,
       identity.server_version_num,
       identity.in_recovery,
       identity.system_identifier,
       identity.schema_format,
       identity.schema_digest
FROM wanwork_policy_control.read_store_identity() AS identity`).Scan(
		&loginRole,
		&ownerRole,
		&database,
		&serverVersion,
		&inRecovery,
		&systemIdentifier,
		&schemaFormat,
		&schemaDigest,
	)
	if err != nil {
		return approvalPolicyControlStoreVerificationQueryError(ctx, connection)
	}
	if loginRole != expectation.ControlLoginRole || ownerRole != expectation.ControlOwnerRole ||
		database != expectation.ControlDatabase || serverVersion/10000 != expectation.ControlPostgreSQLMajor ||
		inRecovery || schemaFormat != expectedSchemaFormat ||
		schemaDigest != expectedSchemaDigest ||
		!canonicalPostgreSQLSystemIdentifier.MatchString(systemIdentifier) {
		return ErrUntrustedPostgresApprovalPolicyStore
	}
	if _, err := strconv.ParseUint(systemIdentifier, 10, 64); err != nil {
		return ErrUntrustedPostgresApprovalPolicyStore
	}
	if digestPostgreSQLSystemIdentifier(systemIdentifier) !=
		expectation.ControlSystemIdentifierDigest {
		return ErrUntrustedPostgresApprovalPolicyStore
	}
	if !verifyCatalog(ctx, connection, expectation) {
		return approvalPolicyControlStoreVerificationQueryError(ctx, connection)
	}
	return nil
}

func approvalPolicyControlStoreVerificationQueryError(
	ctx context.Context,
	connection *pgx.Conn,
) error {
	if ctx == nil || ctx.Err() != nil || connection == nil || connection.IsClosed() {
		return ErrApprovalPolicyStoreUnavailable
	}
	return ErrUntrustedPostgresApprovalPolicyStore
}

func approvalPolicyControlStoreAccessSchemaVersion(
	access approvalPolicyControlStoreAccess,
) (approvalPolicyControlStoreSchemaVersion, bool) {
	switch access {
	case approvalPolicyControlStoreAccessV1Activator:
		return approvalPolicyControlStoreSchemaVersionV1, true
	case approvalPolicyControlStoreAccessV2Activator, approvalPolicyControlStoreAccessV2Fencer,
		approvalPolicyControlStoreAccessV2AttemptIssuer:
		return approvalPolicyControlStoreSchemaVersionV2, true
	default:
		return 0, false
	}
}

func validApprovalPolicyControlStoreAccess(
	expectation ApprovalPolicyControlStoreExpectation,
	access approvalPolicyControlStoreAccess,
) bool {
	switch access {
	case approvalPolicyControlStoreAccessV1Activator:
		return validApprovalPolicyControlStoreExpectation(expectation)
	case approvalPolicyControlStoreAccessV2Activator:
		return validApprovalPolicyControlStoreExpectationV2(expectation) &&
			expectation.ControlLoginRole == expectation.ControlActivatorRole
	case approvalPolicyControlStoreAccessV2Fencer:
		return validApprovalPolicyControlStoreExpectationV2(expectation) &&
			expectation.ControlLoginRole == expectation.ControlFencerRole
	case approvalPolicyControlStoreAccessV2AttemptIssuer:
		return validApprovalPolicyControlStoreExpectationV2(expectation) &&
			expectation.ControlLoginRole == expectation.ControlAttemptIssuerRole
	default:
		return false
	}
}

func validApprovalPolicyControlStoreExpectation(
	expectation ApprovalPolicyControlStoreExpectation,
) bool {
	return expectation.ControlActivatorRole == "" &&
		expectation.ControlAttemptIssuerRole == "" &&
		expectation.ControlFencerRole == "" &&
		validApprovalPolicyControlStoreExpectationCommon(expectation) &&
		canonicalIdentity(expectation.ControlOwnerRole) &&
		canonicalIdentity(expectation.ControlReaderRole) &&
		expectation.ControlOwnerRole != expectation.ControlLoginRole &&
		expectation.ControlReaderRole != expectation.ControlLoginRole &&
		expectation.ControlReaderRole != expectation.ControlOwnerRole
}

func validApprovalPolicyControlStoreExpectationV2(
	expectation ApprovalPolicyControlStoreExpectation,
) bool {
	if !validApprovalPolicyControlStoreExpectationCommon(expectation) ||
		!canonicalIdentity(expectation.ControlOwnerRole) ||
		!canonicalIdentity(expectation.ControlReaderRole) ||
		!canonicalIdentity(expectation.ControlActivatorRole) ||
		!canonicalIdentity(expectation.ControlAttemptIssuerRole) ||
		!canonicalIdentity(expectation.ControlFencerRole) {
		return false
	}
	roles := []string{
		expectation.ControlOwnerRole,
		expectation.ControlReaderRole,
		expectation.ControlActivatorRole,
		expectation.ControlAttemptIssuerRole,
		expectation.ControlFencerRole,
	}
	for left := range roles {
		for right := left + 1; right < len(roles); right++ {
			if roles[left] == roles[right] {
				return false
			}
		}
	}
	return expectation.ControlLoginRole == expectation.ControlReaderRole ||
		expectation.ControlLoginRole == expectation.ControlActivatorRole ||
		expectation.ControlLoginRole == expectation.ControlFencerRole
}

func validApprovalPolicyControlStoreExpectationCommon(
	expectation ApprovalPolicyControlStoreExpectation,
) bool {
	return canonicalIdentity(expectation.ControlDatabase) &&
		canonicalIdentity(expectation.ControlLoginRole) &&
		expectation.ControlPostgreSQLMajor == migrations.AuthorityAccessPostgreSQLMajor &&
		canonicalIdentity(expectation.ControlServerIdentity) &&
		canonicalDigest.MatchString(expectation.ControlSystemIdentifierDigest) &&
		validTLS(expectation.ControlTLS) &&
		expectation.ControlTLS.ServerName == expectation.ControlServerIdentity &&
		canonicalIdentity(expectation.PolicyID) && strings.HasPrefix(expectation.PolicyID, "approval-policy/") &&
		validApprovalPolicyTarget(expectation.PolicyTarget) &&
		expectation.ControlSystemIdentifierDigest != expectation.PolicyTarget.SystemIdentifierDigest
}

func validApprovalPolicyExpectedHead(
	head ApprovalPolicyHead,
	namespace ApprovalPolicyNamespace,
) bool {
	if head.PolicyID != namespace.PolicyID || head.TargetDigest != namespace.TargetDigest ||
		head.Revision > maximumApprovalPolicyRevision {
		return false
	}
	if head.Revision == 0 {
		return head.PolicyDigest == "" && head.ActivationRecordDigest == ""
	}
	return canonicalDigest.MatchString(head.PolicyDigest) &&
		canonicalDigest.MatchString(head.ActivationRecordDigest)
}

type approvalPolicyControlStoreContract struct {
	ActivateFunction            string   `json:"activateFunction"`
	ActivateFunctionArguments   []string `json:"activateFunctionArguments"`
	ActivationRecordFormat      string   `json:"activationRecordFormat"`
	ActivationRecordTable       string   `json:"activationRecordTable"`
	ActivatorFunctions          []string `json:"activatorFunctions"`
	ArchiveTable                string   `json:"archiveTable"`
	CanonicalPolicyMaximumBytes int      `json:"canonicalPolicyMaximumBytes"`
	CanonicalRecordMaximumBytes int      `json:"canonicalRecordMaximumBytes"`
	HeadTable                   string   `json:"headTable"`
	IdentityFunction            string   `json:"identityFunction"`
	IdentityFunctionArguments   []string `json:"identityFunctionArguments"`
	ReadFunction                string   `json:"readFunction"`
	ReadFunctionArguments       []string `json:"readFunctionArguments"`
	ReaderFunctions             []string `json:"readerFunctions"`
	Schema                      string   `json:"schema"`
	SchemaFormat                string   `json:"schemaFormat"`
}

type approvalPolicyControlStoreContractV2 struct {
	ActivateFunction              string              `json:"activateFunction"`
	ActivateFunctionArguments     []string            `json:"activateFunctionArguments"`
	ActivateFunctionResult        string              `json:"activateFunctionResult"`
	ActivationRecordFormat        string              `json:"activationRecordFormat"`
	AdmissionFunction             string              `json:"admissionFunction"`
	AdmissionFunctionArguments    []string            `json:"admissionFunctionArguments"`
	AdmissionFunctionResult       string              `json:"admissionFunctionResult"`
	AdmissionMaximumBytes         int                 `json:"admissionMaximumBytes"`
	AttemptIssueFunction          string              `json:"attemptIssueFunction"`
	AttemptIssueFunctionArguments []string            `json:"attemptIssueFunctionArguments"`
	AttemptIssueFunctionResult    []string            `json:"attemptIssueFunctionResult"`
	AttemptReadFunction           string              `json:"attemptReadFunction"`
	AttemptReadFunctionArguments  []string            `json:"attemptReadFunctionArguments"`
	AttemptReadFunctionResult     []string            `json:"attemptReadFunctionResult"`
	AttemptRecordFormat           string              `json:"attemptRecordFormat"`
	CanonicalPolicyMaximumBytes   int                 `json:"canonicalPolicyMaximumBytes"`
	CanonicalRecordMaximumBytes   int                 `json:"canonicalRecordMaximumBytes"`
	FenceOpenFunction             string              `json:"fenceOpenFunction"`
	FenceOpenFunctionArguments    []string            `json:"fenceOpenFunctionArguments"`
	FenceOpenFunctionResult       string              `json:"fenceOpenFunctionResult"`
	FenceReadFunction             string              `json:"fenceReadFunction"`
	FenceReadFunctionArguments    []string            `json:"fenceReadFunctionArguments"`
	FenceReadFunctionResult       []string            `json:"fenceReadFunctionResult"`
	FenceRecordFormat             string              `json:"fenceRecordFormat"`
	IdentityFunction              string              `json:"identityFunction"`
	IdentityFunctionArguments     []string            `json:"identityFunctionArguments"`
	IdentityFunctionResult        []string            `json:"identityFunctionResult"`
	PolicyReadFunction            string              `json:"policyReadFunction"`
	PolicyReadFunctionArguments   []string            `json:"policyReadFunctionArguments"`
	PolicyReadFunctionResult      []string            `json:"policyReadFunctionResult"`
	RoleFunctions                 map[string][]string `json:"roleFunctions"`
	Schema                        string              `json:"schema"`
	SchemaFormat                  string              `json:"schemaFormat"`
	Tables                        []string            `json:"tables"`
	TargetLockScope               string              `json:"targetLockScope"`
	TargetLockSeed                int64               `json:"targetLockSeed"`
}

func CurrentApprovalPolicyControlStoreSchemaDigest() string {
	contract := approvalPolicyControlStoreContract{
		ActivateFunction: approvalPolicyControlStoreActivateFunction,
		ActivateFunctionArguments: []string{
			"text", "text", "bigint", "text", "text", "bigint", "text", "text", "bytea", "bytea",
		},
		ActivationRecordFormat: ApprovalPolicyActivationRecordFormat,
		ActivationRecordTable:  "approval_policy_activation_record",
		ActivatorFunctions: []string{
			approvalPolicyControlStoreActivateFunction,
			approvalPolicyControlStoreIdentityFunction,
			approvalPolicyControlStoreReadFunction,
		},
		ArchiveTable:                "approval_policy_archive",
		CanonicalPolicyMaximumBytes: maximumApprovalPolicyBytes,
		CanonicalRecordMaximumBytes: maximumApprovalPolicyActivationRecordBytes,
		HeadTable:                   "approval_policy_head",
		IdentityFunction:            approvalPolicyControlStoreIdentityFunction,
		IdentityFunctionArguments:   []string{},
		ReadFunction:                approvalPolicyControlStoreReadFunction,
		ReadFunctionArguments:       []string{"text", "text"},
		ReaderFunctions: []string{
			approvalPolicyControlStoreIdentityFunction,
			approvalPolicyControlStoreReadFunction,
		},
		Schema:       approvalPolicyControlStoreSchemaName,
		SchemaFormat: ApprovalPolicyControlStoreSchemaFormat,
	}
	var output bytes.Buffer
	encoder := json.NewEncoder(&output)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(contract); err != nil {
		return ""
	}
	return domainSeparatedDigest(
		approvalPolicyControlStoreContractDigestDomain,
		bytes.TrimSuffix(output.Bytes(), []byte("\n")),
	)
}

func CurrentApprovalPolicyControlStoreSchemaDigestV2() string {
	contract := approvalPolicyControlStoreContractV2{
		ActivateFunction: approvalPolicyControlStoreActivateFunction,
		ActivateFunctionArguments: []string{
			"text", "text", "bigint", "text", "text", "bigint", "text", "text", "bytea", "bytea",
		},
		ActivateFunctionResult: "text",
		ActivationRecordFormat: ApprovalPolicyActivationRecordFormat,
		AdmissionFunction:      approvalPolicyControlStoreAdmissionFunction,
		AdmissionFunctionArguments: []string{
			"bytea", "text", "text", "bigint", "text", "text", "text", "text", "text", "text",
			"text", "text", "text", "text", "timestamptz",
		},
		AdmissionFunctionResult:       "boolean",
		AdmissionMaximumBytes:         maximumApprovalExecutionAdmissionBytes,
		AttemptIssueFunction:          approvalPolicyControlStoreAttemptIssueFunction,
		AttemptIssueFunctionArguments: []string{"text", "bytea"},
		AttemptIssueFunctionResult: []string{
			"state_status text", "attempt_generation bigint", "attempt_id text",
			"attempt_issuance_id text", "attempt_receipt_digest text", "created_at timestamptz",
			"canonical_attempt bytea",
		},
		AttemptReadFunction:          approvalPolicyControlStoreAttemptReadFunction,
		AttemptReadFunctionArguments: []string{"text", "text", "text"},
		AttemptReadFunctionResult: []string{
			"state_status text", "attempt_generation bigint", "attempt_id text",
			"attempt_issuance_id text", "attempt_receipt_digest text", "created_at timestamptz",
			"canonical_attempt bytea",
		},
		AttemptRecordFormat:         ApprovalExecutionAttemptRecordFormat,
		CanonicalPolicyMaximumBytes: maximumApprovalPolicyBytes,
		CanonicalRecordMaximumBytes: maximumApprovalPolicyActivationRecordBytes,
		FenceOpenFunction:           approvalPolicyControlStoreFenceOpenFunction,
		FenceOpenFunctionArguments: []string{
			"text", "text", "bigint", "text", "text", "text", "text", "text", "text", "text",
			"timestamptz", "bytea",
		},
		FenceOpenFunctionResult:    "text",
		FenceReadFunction:          approvalPolicyControlStoreFenceReadFunction,
		FenceReadFunctionArguments: []string{"text", "text", "text"},
		FenceReadFunctionResult: []string{
			"state_status text", "fence_epoch bigint", "opened_at timestamptz",
			"token_digest text", "canonical_admission bytea",
		},
		FenceRecordFormat:         ApprovalExecutionFenceRecordFormat,
		IdentityFunction:          approvalPolicyControlStoreIdentityFunction,
		IdentityFunctionArguments: []string{},
		IdentityFunctionResult: []string{
			"login_role text", "owner_role text", "database_name text", "server_version_num integer",
			"in_recovery boolean", "system_identifier text", "schema_format text", "schema_digest text",
		},
		PolicyReadFunction:          approvalPolicyControlStoreReadFunction,
		PolicyReadFunctionArguments: []string{"text", "text"},
		PolicyReadFunctionResult: []string{
			"state_status text", "activation_record_digest text", "policy_digest text", "revision bigint",
			"canonical_policy bytea", "canonical_record bytea",
		},
		RoleFunctions: map[string][]string{
			"activator": {
				approvalPolicyControlStoreActivateFunction,
				approvalPolicyControlStoreIdentityFunction,
				approvalPolicyControlStoreReadFunction,
			},
			"fencer": {
				approvalPolicyControlStoreFenceOpenFunction,
				approvalPolicyControlStoreFenceReadFunction,
				approvalPolicyControlStoreIdentityFunction,
				approvalPolicyControlStoreReadFunction,
			},
			"attemptIssuer": {
				approvalPolicyControlStoreAttemptIssueFunction,
				approvalPolicyControlStoreAttemptReadFunction,
				approvalPolicyControlStoreIdentityFunction,
				approvalPolicyControlStoreReadFunction,
			},
			"reader": {
				approvalPolicyControlStoreFenceReadFunction,
				approvalPolicyControlStoreIdentityFunction,
				approvalPolicyControlStoreReadFunction,
			},
		},
		Schema:       approvalPolicyControlStoreSchemaName,
		SchemaFormat: ApprovalPolicyControlStoreSchemaFormatV2,
		Tables: []string{
			"approval_execution_attempt_counter",
			"approval_execution_attempt_record",
			"approval_execution_fence_counter",
			"approval_execution_fence_head",
			"approval_execution_fence_record",
			"approval_policy_activation_record",
			"approval_policy_archive",
			"approval_policy_head",
		},
		TargetLockScope: "physical_target_digest",
		TargetLockSeed:  approvalPolicyControlStoreTargetLockSeed,
	}
	var output bytes.Buffer
	encoder := json.NewEncoder(&output)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(contract); err != nil {
		return ""
	}
	return domainSeparatedDigest(
		approvalPolicyControlStoreContractDigestDomainV2,
		bytes.TrimSuffix(output.Bytes(), []byte("\n")),
	)
}

func quarantineApprovalPolicyControlConnection(connection *pgxpool.Conn) {
	rawConnection := connection.Hijack()
	closeContext, cancel := context.WithTimeout(context.Background(), approvalPolicyControlStoreCleanupTimeout)
	defer cancel()
	_ = rawConnection.Close(closeContext)
}
