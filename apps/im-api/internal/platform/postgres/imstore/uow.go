package imstore

import (
	"context"
	"crypto/sha256"
	"encoding/binary"
	"errors"
	"fmt"
	"time"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
	store "github.com/huapohen/quantum-entanglement/apps/im-api/internal/imstore"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

const transactionCleanupTimeout = 5 * time.Second

type UnitOfWork struct {
	pool       *pgxpool.Pool
	commitHook func(context.Context, pgx.Tx) error
}

func NewUnitOfWork(pool *pgxpool.Pool) (*UnitOfWork, error) {
	if pool == nil {
		return nil, store.ErrInvalidRequest
	}
	return &UnitOfWork{pool: pool, commitHook: commitTransaction}, nil
}

func (unit *UnitOfWork) Read(
	ctx context.Context,
	tenantID im.TenantID,
	operation store.ReadOperation,
) error {
	if err := unit.validRequest(ctx, tenantID); err != nil || operation == nil {
		return store.ErrInvalidRequest
	}
	connection, err := unit.pool.Acquire(ctx)
	if err != nil {
		return mapStoreError(err, store.ErrStoreUnavailable)
	}
	defer connection.Release()
	transaction, err := connection.BeginTx(ctx, pgx.TxOptions{
		IsoLevel:   pgx.RepeatableRead,
		AccessMode: pgx.ReadOnly,
	})
	if err != nil {
		return mapStoreError(err, store.ErrStoreUnavailable)
	}
	defer rollbackTransaction(transaction)
	if err := bindTenantTransaction(ctx, transaction, tenantID); err != nil {
		return err
	}
	repositories := newTenantRepositories(transaction, tenantID)
	var operationErr error
	func() {
		defer repositories.deactivate()
		operationErr = operation(ctx, repositories)
	}()
	if operationErr != nil {
		return operationErr
	}
	if repositoryErr := repositories.recordedFailure(); repositoryErr != nil {
		return repositoryErr
	}
	if err := transaction.Commit(ctx); err != nil {
		return mapStoreError(err, store.ErrStoreUnavailable)
	}
	return nil
}

func (unit *UnitOfWork) Execute(
	ctx context.Context,
	tenantID im.TenantID,
	command store.CommandIdentity,
	operation store.ExecuteOperation,
) (store.CommitReceipt, error) {
	if err := unit.validRequest(ctx, tenantID); err != nil || command.IsZero() || operation == nil ||
		unit.commitHook == nil {
		return store.CommitReceipt{}, store.ErrInvalidRequest
	}
	connection, err := unit.pool.Acquire(ctx)
	if err != nil {
		return store.CommitReceipt{}, mapStoreError(err, store.ErrStoreUnavailable)
	}
	released := false
	release := func() {
		if !released {
			connection.Release()
			released = true
		}
	}
	defer release()
	lockKey := commandLockKey(tenantID, command)
	if err := acquireCommandLock(ctx, connection.Conn(), lockKey); err != nil {
		quarantinePooledConnection(connection)
		released = true
		return store.CommitReceipt{}, err
	}
	lockHeld := true
	defer func() {
		if lockHeld {
			if err := releaseCommandLock(connection.Conn(), lockKey); err != nil {
				quarantinePooledConnection(connection)
				released = true
			}
		}
	}()

	transaction, err := connection.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.Serializable})
	if err != nil {
		return store.CommitReceipt{}, mapStoreError(err, store.ErrStoreUnavailable)
	}
	defer rollbackTransaction(transaction)
	if err := bindTenantTransaction(ctx, transaction, tenantID); err != nil {
		return store.CommitReceipt{}, err
	}
	existing, err := readReceipt(ctx, transaction, tenantID, command, true, false)
	if err == nil {
		rollbackTransaction(transaction)
		return existing, nil
	}
	if !errors.Is(err, store.ErrNotFound) {
		return store.CommitReceipt{}, err
	}

	repositories := newTenantRepositories(transaction, tenantID)
	var resultDigest store.SHA256Digest
	var operationErr error
	func() {
		defer repositories.deactivate()
		resultDigest, operationErr = operation(ctx, repositories)
	}()
	if operationErr != nil {
		return store.CommitReceipt{}, operationErr
	}
	if repositoryErr := repositories.recordedFailure(); repositoryErr != nil {
		return store.CommitReceipt{}, repositoryErr
	}
	committedAt, err := finalizeReceipt(ctx, transaction, tenantID, command, resultDigest)
	if err != nil {
		if errors.Is(err, store.ErrIdempotencyConflict) {
			rollbackTransaction(transaction)
			if err := releaseCommandLock(connection.Conn(), lockKey); err != nil {
				lockHeld = false
				quarantinePooledConnection(connection)
				released = true
				return store.CommitReceipt{}, err
			}
			lockHeld = false
			release()
			return unit.Resolve(ctx, tenantID, command)
		}
		return store.CommitReceipt{}, err
	}

	if err := unit.commitHook(ctx, transaction); err != nil {
		if definiteRollback(err) {
			return store.CommitReceipt{}, mapStoreError(err, store.ErrStoreUnavailable)
		}
		lockHeld = false
		quarantinePooledConnection(connection)
		released = true
		resolved, resolveErr := unit.Resolve(ctx, tenantID, command)
		if resolveErr == nil {
			receipt, receiptErr := store.NewCommitReceipt(
				resolved.Command(),
				resolved.ResultDigest(),
				resolved.CommittedAt(),
				true,
				true,
			)
			if receiptErr != nil {
				return store.CommitReceipt{}, store.ErrIntegrity
			}
			return receipt, nil
		}
		if errors.Is(resolveErr, store.ErrIdempotencyConflict) {
			return store.CommitReceipt{}, resolveErr
		}
		return store.CommitReceipt{}, fmt.Errorf(
			"%w: receipt readback failed",
			store.ErrCommitOutcomeUnknown,
		)
	}
	receipt, err := store.NewCommitReceipt(command, resultDigest, committedAt, false, false)
	if err != nil {
		return store.CommitReceipt{}, store.ErrIntegrity
	}
	return receipt, nil
}

func (unit *UnitOfWork) Resolve(
	ctx context.Context,
	tenantID im.TenantID,
	command store.CommandIdentity,
) (store.CommitReceipt, error) {
	if err := unit.validRequest(ctx, tenantID); err != nil || command.IsZero() {
		return store.CommitReceipt{}, store.ErrInvalidRequest
	}
	connection, err := unit.pool.Acquire(ctx)
	if err != nil {
		return store.CommitReceipt{}, mapStoreError(err, store.ErrStoreUnavailable)
	}
	defer connection.Release()
	transaction, err := connection.BeginTx(ctx, pgx.TxOptions{
		IsoLevel:   pgx.RepeatableRead,
		AccessMode: pgx.ReadOnly,
	})
	if err != nil {
		return store.CommitReceipt{}, mapStoreError(err, store.ErrStoreUnavailable)
	}
	defer rollbackTransaction(transaction)
	if err := bindTenantTransaction(ctx, transaction, tenantID); err != nil {
		return store.CommitReceipt{}, err
	}
	receipt, err := readReceipt(ctx, transaction, tenantID, command, true, false)
	if err != nil {
		return store.CommitReceipt{}, err
	}
	if err := transaction.Commit(ctx); err != nil {
		return store.CommitReceipt{}, mapStoreError(err, store.ErrStoreUnavailable)
	}
	return receipt, nil
}

func (unit *UnitOfWork) validRequest(ctx context.Context, tenantID im.TenantID) error {
	if unit == nil || unit.pool == nil || ctx == nil || ctx.Err() != nil || tenantID.IsZero() {
		return store.ErrInvalidRequest
	}
	return nil
}

func bindTenantTransaction(ctx context.Context, transaction pgx.Tx, tenantID im.TenantID) error {
	if _, err := transaction.Exec(ctx, "SET LOCAL search_path = pg_catalog"); err != nil {
		return mapStoreError(err, store.ErrStoreUnavailable)
	}
	var recorded string
	if err := transaction.QueryRow(
		ctx,
		"SELECT pg_catalog.set_config('wanwork.tenant_id', $1, true)",
		tenantID.String(),
	).Scan(&recorded); err != nil {
		return mapStoreError(err, store.ErrStoreUnavailable)
	}
	if recorded != tenantID.String() {
		return store.ErrIntegrity
	}
	return nil
}

func commandLockKey(tenantID im.TenantID, command store.CommandIdentity) int64 {
	digest := sha256.Sum256([]byte(
		"wanwork.im/idempotency-lock/1\n" + tenantID.String() + "\x00" + command.Kind() +
			"\x00" + command.IdempotencyKey(),
	))
	return int64(binary.BigEndian.Uint64(digest[:8]))
}

func acquireCommandLock(ctx context.Context, connection *pgx.Conn, lockKey int64) error {
	if _, err := connection.Exec(
		ctx,
		"SELECT pg_catalog.pg_advisory_lock($1)",
		lockKey,
	); err != nil {
		return mapStoreError(err, store.ErrStoreUnavailable)
	}
	return nil
}

func releaseCommandLock(connection *pgx.Conn, lockKey int64) error {
	unlockContext, cancel := context.WithTimeout(context.Background(), transactionCleanupTimeout)
	defer cancel()
	var unlocked bool
	if err := connection.QueryRow(
		unlockContext,
		"SELECT pg_catalog.pg_advisory_unlock($1)",
		lockKey,
	).Scan(&unlocked); err != nil || !unlocked {
		return store.ErrStoreUnavailable
	}
	return nil
}

func finalizeReceipt(
	ctx context.Context,
	transaction pgx.Tx,
	tenantID im.TenantID,
	command store.CommandIdentity,
	resultDigest store.SHA256Digest,
) (time.Time, error) {
	var committedAt time.Time
	err := transaction.QueryRow(ctx, `
SELECT wanwork_im.write_tenant_command_receipt($1, $2, $3, $4, $5)`,
		tenantID.String(),
		command.Kind(),
		command.IdempotencyKey(),
		command.RequestDigest().Hex(),
		resultDigest.Hex(),
	).Scan(&committedAt)
	if err != nil {
		return time.Time{}, mapWriteError(err, store.ErrIdempotencyConflict)
	}
	return committedAt, nil
}

func readReceipt(
	ctx context.Context,
	transaction pgx.Tx,
	tenantID im.TenantID,
	command store.CommandIdentity,
	replayed bool,
	resolved bool,
) (store.CommitReceipt, error) {
	var requestValue string
	var resultValue string
	var committedAt time.Time
	err := transaction.QueryRow(ctx, `
SELECT request_sha256, result_sha256, committed_at
FROM wanwork_im.tenant_command_receipts
WHERE tenant_id = $1
  AND command_kind = $2
  AND idempotency_key = $3`,
		tenantID.String(),
		command.Kind(),
		command.IdempotencyKey(),
	).Scan(&requestValue, &resultValue, &committedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return store.CommitReceipt{}, store.ErrNotFound
	}
	if err != nil {
		return store.CommitReceipt{}, mapReadError(err)
	}
	requestDigest, err := store.ParseSHA256Digest(requestValue)
	if err != nil {
		return store.CommitReceipt{}, store.ErrIntegrity
	}
	if requestDigest != command.RequestDigest() {
		return store.CommitReceipt{}, store.ErrIdempotencyConflict
	}
	resultDigest, err := store.ParseSHA256Digest(resultValue)
	if err != nil {
		return store.CommitReceipt{}, store.ErrIntegrity
	}
	receipt, err := store.NewCommitReceipt(
		command,
		resultDigest,
		committedAt,
		replayed,
		resolved,
	)
	if err != nil {
		return store.CommitReceipt{}, store.ErrIntegrity
	}
	return receipt, nil
}

func definiteRollback(err error) bool {
	if errors.Is(err, pgx.ErrTxCommitRollback) {
		return true
	}
	var postgresError *pgconn.PgError
	return errors.As(err, &postgresError) &&
		(postgresError.Code == "40001" || postgresError.Code == "40P01")
}

func commitTransaction(ctx context.Context, transaction pgx.Tx) error {
	return transaction.Commit(ctx)
}

func rollbackTransaction(transaction pgx.Tx) {
	rollbackContext, cancel := context.WithTimeout(context.Background(), transactionCleanupTimeout)
	defer cancel()
	_ = transaction.Rollback(rollbackContext)
}

func quarantinePooledConnection(connection *pgxpool.Conn) {
	rawConnection := connection.Hijack()
	closeContext, cancel := context.WithTimeout(context.Background(), transactionCleanupTimeout)
	defer cancel()
	_ = rawConnection.Close(closeContext)
}

var _ store.TenantUnitOfWork = (*UnitOfWork)(nil)
