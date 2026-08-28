package authoritycutover

import (
	"context"
	"errors"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// PostgresApprovalExecutionFenceStore opens execution fences through the dedicated schema-v2
// fencer role. It never accepts an activator credential and never reads control tables directly.
type PostgresApprovalExecutionFenceStore struct {
	expectation     ApprovalPolicyControlStoreExpectation
	namespace       ApprovalPolicyNamespace
	pool            *pgxpool.Pool
	verifyTransport approvalPolicyControlStoreTransportVerifier
}

func NewPostgresApprovalExecutionFenceStore(
	pool *pgxpool.Pool,
	expectation ApprovalPolicyControlStoreExpectation,
) (*PostgresApprovalExecutionFenceStore, error) {
	return newPostgresApprovalExecutionFenceStore(
		pool,
		expectation,
		verifyClusterTLSTransport,
	)
}

func newPostgresApprovalExecutionFenceStore(
	pool *pgxpool.Pool,
	expectation ApprovalPolicyControlStoreExpectation,
	verifyTransport approvalPolicyControlStoreTransportVerifier,
) (*PostgresApprovalExecutionFenceStore, error) {
	if pool == nil || verifyTransport == nil ||
		!validApprovalPolicyControlStoreAccess(
			expectation,
			approvalPolicyControlStoreAccessV2Fencer,
		) {
		return nil, ErrInvalidPostgresApprovalPolicyStore
	}
	return &PostgresApprovalExecutionFenceStore{
		expectation: expectation,
		namespace: ApprovalPolicyNamespace{
			PolicyID:     expectation.PolicyID,
			TargetDigest: digestApprovalPolicyTarget(expectation.PolicyTarget),
		},
		pool:            pool,
		verifyTransport: verifyTransport,
	}, nil
}

func (store *PostgresApprovalExecutionFenceStore) Load(
	ctx context.Context,
	namespace ApprovalPolicyNamespace,
	operationID string,
) (ApprovalExecutionFenceStoredState, error) {
	if ctx == nil || store == nil || store.pool == nil || namespace != store.namespace ||
		!canonicalIdentity(operationID) ||
		!strings.HasPrefix(operationID, "approval-operation/") {
		return ApprovalExecutionFenceStoredState{}, ErrInvalidApprovalExecutionState
	}
	connection, err := store.pool.Acquire(ctx)
	if err != nil {
		return ApprovalExecutionFenceStoredState{}, ErrApprovalExecutionStoreUnavailable
	}
	defer connection.Release()
	if !verifyApprovalPolicyControlStoreConnection(
		ctx,
		connection.Conn(),
		store.expectation,
		approvalPolicyControlStoreAccessV2Fencer,
		store.verifyTransport,
	) {
		return ApprovalExecutionFenceStoredState{}, ErrInvalidApprovalExecutionState
	}
	var (
		canonicalAdmission []byte
		fenceEpoch         *int64
		openedAt           *time.Time
		stateStatus        string
		tokenDigest        *string
	)
	err = connection.QueryRow(ctx, `
SELECT fence.state_status,
       fence.fence_epoch,
       fence.opened_at,
       fence.token_digest,
       fence.canonical_admission
FROM wanwork_policy_control.read_approval_execution_fence($1, $2, $3) AS fence`,
		namespace.PolicyID,
		namespace.TargetDigest,
		operationID,
	).Scan(
		&stateStatus,
		&fenceEpoch,
		&openedAt,
		&tokenDigest,
		&canonicalAdmission,
	)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return ApprovalExecutionFenceStoredState{}, ErrInvalidApprovalExecutionState
		}
		return ApprovalExecutionFenceStoredState{}, ErrApprovalExecutionStoreUnavailable
	}
	switch stateStatus {
	case "missing":
		return ApprovalExecutionFenceStoredState{}, ErrApprovalExecutionFenceNotFound
	case "corrupt":
		return ApprovalExecutionFenceStoredState{}, ErrInvalidApprovalExecutionState
	case "present":
	default:
		return ApprovalExecutionFenceStoredState{}, ErrInvalidApprovalExecutionState
	}
	if fenceEpoch == nil || *fenceEpoch <= 0 || openedAt == nil || tokenDigest == nil {
		return ApprovalExecutionFenceStoredState{}, ErrInvalidApprovalExecutionState
	}
	candidate, err := decodeApprovalExecutionAdmission(canonicalAdmission)
	if err != nil || candidate.record.ApprovalPolicyID != namespace.PolicyID ||
		candidate.record.ApprovalPolicyTargetDigest != namespace.TargetDigest ||
		candidate.record.OperationID != operationID {
		return ApprovalExecutionFenceStoredState{}, ErrInvalidApprovalExecutionState
	}
	record, err := sealApprovalExecutionFenceRecord(
		candidate,
		uint64(*fenceEpoch),
		openedAt.UTC(),
		*tokenDigest,
	)
	if err != nil {
		return ApprovalExecutionFenceStoredState{}, ErrInvalidApprovalExecutionState
	}
	return ApprovalExecutionFenceStoredState{Record: record}, nil
}

func (store *PostgresApprovalExecutionFenceStore) CompareAndOpen(
	ctx context.Context,
	namespace ApprovalPolicyNamespace,
	expected ApprovalPolicyHead,
	candidate approvalExecutionFenceCandidate,
	tokenDigest string,
) error {
	if ctx == nil || store == nil || store.pool == nil || namespace != store.namespace ||
		!validApprovalPolicyExpectedHead(expected, namespace) || expected.Revision == 0 ||
		!validApprovalExecutionFenceCandidate(candidate) ||
		candidate.record.ExpectedPolicyHead != expected ||
		candidate.record.ApprovalPolicyID != namespace.PolicyID ||
		candidate.record.ApprovalPolicyTargetDigest != namespace.TargetDigest ||
		!canonicalDigest.MatchString(tokenDigest) {
		return ErrInvalidApprovalExecutionState
	}
	canonicalAdmission, err := marshalApprovalExecutionAdmissionCanonical(candidate)
	if err != nil {
		return ErrInvalidApprovalExecutionState
	}
	connection, err := store.pool.Acquire(ctx)
	if err != nil {
		return ErrApprovalExecutionStoreUnavailable
	}
	released := false
	release := func() {
		if !released {
			connection.Release()
			released = true
		}
	}
	defer release()
	if !verifyApprovalPolicyControlStoreConnection(
		ctx,
		connection.Conn(),
		store.expectation,
		approvalPolicyControlStoreAccessV2Fencer,
		store.verifyTransport,
	) {
		return ErrInvalidApprovalExecutionState
	}
	var outcome string
	err = connection.QueryRow(ctx, `
SELECT wanwork_policy_control.compare_and_open_approval_execution_fence(
    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12
)`,
		namespace.PolicyID,
		namespace.TargetDigest,
		int64(expected.Revision),
		expected.PolicyDigest,
		expected.ActivationRecordDigest,
		candidate.record.AdmissionDigest,
		candidate.record.OperationID,
		candidate.record.ConsumptionID,
		candidate.record.ApprovalDigest,
		tokenDigest,
		candidate.record.MutationNotAfter,
		canonicalAdmission,
	).Scan(&outcome)
	if err != nil {
		quarantineApprovalPolicyControlConnection(connection)
		released = true
		return ErrApprovalExecutionCommitUncertain
	}
	switch outcome {
	case "committed":
		return nil
	case "conflict", "fence_open", "policy_conflict":
		return ErrApprovalExecutionConflict
	case "expired":
		return ErrApprovalExecutionExpired
	case "corrupt", "rejected":
		return ErrInvalidApprovalExecutionState
	default:
		quarantineApprovalPolicyControlConnection(connection)
		released = true
		return ErrApprovalExecutionCommitUncertain
	}
}
