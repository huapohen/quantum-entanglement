package authoritycutover

import (
	"context"
	"errors"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// PostgresApprovalExecutionAttemptStore is the only database boundary allowed to issue a
// post-preflight execution attempt. Its login must be the dedicated attempt-issuer role; the
// fencer role has no EXECUTE privilege on the issue function and cannot mint a grant.
type PostgresApprovalExecutionAttemptStore struct {
	expectation     ApprovalPolicyControlStoreExpectation
	namespace       ApprovalPolicyNamespace
	pool            *pgxpool.Pool
	verifyTransport approvalPolicyControlStoreTransportVerifier
}

func NewPostgresApprovalExecutionAttemptStore(
	pool *pgxpool.Pool,
	expectation ApprovalPolicyControlStoreExpectation,
) (*PostgresApprovalExecutionAttemptStore, error) {
	return newPostgresApprovalExecutionAttemptStore(pool, expectation, verifyClusterTLSTransport)
}

func newPostgresApprovalExecutionAttemptStore(
	pool *pgxpool.Pool,
	expectation ApprovalPolicyControlStoreExpectation,
	verifyTransport approvalPolicyControlStoreTransportVerifier,
) (*PostgresApprovalExecutionAttemptStore, error) {
	if pool == nil || verifyTransport == nil ||
		!validApprovalPolicyControlStoreAccess(
			expectation,
			approvalPolicyControlStoreAccessV2AttemptIssuer,
		) {
		return nil, ErrInvalidPostgresApprovalPolicyStore
	}
	return &PostgresApprovalExecutionAttemptStore{
		expectation: expectation,
		namespace: ApprovalPolicyNamespace{
			PolicyID:     expectation.PolicyID,
			TargetDigest: digestApprovalPolicyTarget(expectation.PolicyTarget),
		},
		pool:            pool,
		verifyTransport: verifyTransport,
	}, nil
}

func (store *PostgresApprovalExecutionAttemptStore) Load(
	ctx context.Context,
	namespace ApprovalPolicyNamespace,
	issuanceID string,
) (ApprovalExecutionAttemptStoredState, error) {
	if ctx == nil || store == nil || store.pool == nil || namespace != store.namespace ||
		!canonicalIdentity(issuanceID) ||
		!strings.HasPrefix(issuanceID, "execution-attempt-issuance/") {
		return ApprovalExecutionAttemptStoredState{}, ErrInvalidApprovalExecutionAttempt
	}
	connection, err := store.pool.Acquire(ctx)
	if err != nil {
		return ApprovalExecutionAttemptStoredState{}, ErrApprovalExecutionAttemptUnavailable
	}
	defer connection.Release()
	if err := verifyApprovalPolicyControlStoreConnection(
		ctx,
		connection.Conn(),
		store.expectation,
		approvalPolicyControlStoreAccessV2AttemptIssuer,
		store.verifyTransport,
	); err != nil {
		if errors.Is(err, ErrUntrustedPostgresApprovalPolicyStore) {
			return ApprovalExecutionAttemptStoredState{}, ErrInvalidApprovalExecutionAttempt
		}
		return ApprovalExecutionAttemptStoredState{}, ErrApprovalExecutionAttemptUnavailable
	}

	var (
		stateStatus       string
		attemptGeneration *int64
		attemptID         *string
		attemptIssuanceID *string
		attemptReceipt    *string
		createdAt         *time.Time
		canonicalAttempt  []byte
	)
	err = connection.QueryRow(ctx, `
SELECT attempt.state_status,
       attempt.attempt_generation,
       attempt.attempt_id,
       attempt.attempt_issuance_id,
       attempt.attempt_receipt_digest,
       attempt.created_at,
       attempt.canonical_attempt
FROM wanwork_policy_control.read_approval_execution_attempt($1, $2, $3) AS attempt`,
		namespace.PolicyID,
		namespace.TargetDigest,
		issuanceID,
	).Scan(
		&stateStatus,
		&attemptGeneration,
		&attemptID,
		&attemptIssuanceID,
		&attemptReceipt,
		&createdAt,
		&canonicalAttempt,
	)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return ApprovalExecutionAttemptStoredState{}, ErrInvalidApprovalExecutionAttempt
		}
		return ApprovalExecutionAttemptStoredState{}, ErrApprovalExecutionAttemptUnavailable
	}
	switch stateStatus {
	case "missing":
		return ApprovalExecutionAttemptStoredState{}, ErrApprovalExecutionAttemptNotFound
	case "corrupt":
		return ApprovalExecutionAttemptStoredState{}, ErrInvalidApprovalExecutionAttempt
	case "present":
	default:
		return ApprovalExecutionAttemptStoredState{}, ErrInvalidApprovalExecutionAttempt
	}
	if attemptGeneration == nil || *attemptGeneration <= 0 || attemptID == nil ||
		attemptIssuanceID == nil || attemptReceipt == nil || createdAt == nil {
		return ApprovalExecutionAttemptStoredState{}, ErrInvalidApprovalExecutionAttempt
	}
	record, err := decodeApprovalExecutionAttemptRecord(canonicalAttempt)
	if err != nil ||
		record.ExpectedPolicyHead.PolicyID != namespace.PolicyID ||
		record.ExpectedPolicyHead.TargetDigest != namespace.TargetDigest ||
		record.AttemptIssuanceID != issuanceID ||
		uint64(*attemptGeneration) != record.AttemptGeneration ||
		*attemptID != record.AttemptID ||
		*attemptIssuanceID != record.AttemptIssuanceID ||
		*attemptReceipt != record.AttemptReceiptDigest ||
		!record.CreatedAt.Equal(createdAt.UTC()) {
		return ApprovalExecutionAttemptStoredState{}, ErrInvalidApprovalExecutionAttempt
	}
	return ApprovalExecutionAttemptStoredState{Record: record}, nil
}

func (store *PostgresApprovalExecutionAttemptStore) CompareAndIssue(
	ctx context.Context,
	namespace ApprovalPolicyNamespace,
	candidate approvalExecutionAttemptCandidate,
) error {
	if ctx == nil || store == nil || store.pool == nil || namespace != store.namespace ||
		!validApprovalExecutionAttemptCandidate(candidate) ||
		candidate.record.ExpectedPolicyHead.PolicyID != namespace.PolicyID ||
		candidate.record.ExpectedPolicyHead.TargetDigest != namespace.TargetDigest {
		return ErrInvalidApprovalExecutionAttempt
	}
	canonicalCandidate, err := marshalApprovalExecutionAttemptRecordCanonical(candidate.record)
	if err != nil {
		return ErrInvalidApprovalExecutionAttempt
	}
	connection, err := store.pool.Acquire(ctx)
	if err != nil {
		return ErrApprovalExecutionAttemptUnavailable
	}
	released := false
	release := func() {
		if !released {
			connection.Release()
			released = true
		}
	}
	defer release()
	if err := verifyApprovalPolicyControlStoreConnection(
		ctx,
		connection.Conn(),
		store.expectation,
		approvalPolicyControlStoreAccessV2AttemptIssuer,
		store.verifyTransport,
	); err != nil {
		if errors.Is(err, ErrUntrustedPostgresApprovalPolicyStore) {
			return ErrInvalidApprovalExecutionAttempt
		}
		return ErrApprovalExecutionAttemptUnavailable
	}
	var (
		stateStatus       string
		attemptGeneration *int64
		attemptID         *string
		attemptIssuanceID *string
		attemptReceipt    *string
		createdAt         *time.Time
		canonicalAttempt  []byte
	)
	err = connection.QueryRow(ctx, `
SELECT result.state_status,
       result.attempt_generation,
       result.attempt_id,
       result.attempt_issuance_id,
       result.attempt_receipt_digest,
       result.created_at,
       result.canonical_attempt
FROM wanwork_policy_control.compare_and_issue_approval_execution_attempt($1, $2) AS result`,
		candidate.record.AttemptIssuanceID,
		canonicalCandidate,
	).Scan(
		&stateStatus,
		&attemptGeneration,
		&attemptID,
		&attemptIssuanceID,
		&attemptReceipt,
		&createdAt,
		&canonicalAttempt,
	)
	if err != nil {
		quarantineApprovalPolicyControlConnection(connection)
		released = true
		return ErrApprovalExecutionAttemptCommitUncertain
	}
	if stateStatus == "committed" {
		if attemptGeneration == nil || attemptID == nil || attemptIssuanceID == nil ||
			attemptReceipt == nil || createdAt == nil {
			return ErrInvalidApprovalExecutionAttempt
		}
		record, decodeErr := decodeApprovalExecutionAttemptRecord(canonicalAttempt)
		if decodeErr != nil || !approvalExecutionAttemptReadbackMatches(
			record, candidate,
		) || uint64(*attemptGeneration) != record.AttemptGeneration ||
			*attemptID != record.AttemptID || *attemptIssuanceID != record.AttemptIssuanceID ||
			*attemptReceipt != record.AttemptReceiptDigest || !record.CreatedAt.Equal(createdAt.UTC()) {
			return ErrInvalidApprovalExecutionAttempt
		}
		return nil
	}
	switch stateStatus {
	case "expired":
		return ErrApprovalExecutionAttemptExpired
	case "policy_conflict", "conflict":
		return ErrApprovalExecutionAttemptConflict
	case "corrupt", "rejected":
		return ErrInvalidApprovalExecutionAttempt
	default:
		quarantineApprovalPolicyControlConnection(connection)
		released = true
		return ErrApprovalExecutionAttemptCommitUncertain
	}
}
