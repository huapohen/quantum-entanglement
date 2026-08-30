package eventstore

import (
	"context"
	"errors"
	"math"
	"regexp"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/events"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/platform/postgres/runtimepool"
	"github.com/jackc/pgx/v5"
)

const (
	maxProjectionCheckpointCursorBytes = 4096
	maxProjectionIdentifierBytes       = 256
)

var projectionIdentifierPattern = regexp.MustCompile(`^[^\x00-\x20\x7f]{1,256}$`)

// CheckpointStore is the PostgreSQL implementation of the projection checkpoint port. It only
// accepts an attested runtime pool; migration/owner connections cannot be used for projection
// progress writes.
type CheckpointStore struct {
	pool *runtimepool.Pool
}

var _ events.ProjectionCheckpointStore = (*CheckpointStore)(nil)

// NewProjectionCheckpointStore constructs the durable checkpoint adapter.
func NewProjectionCheckpointStore(pool *runtimepool.Pool) (*CheckpointStore, error) {
	if pool == nil {
		return nil, events.ErrInvalidStore
	}
	return &CheckpointStore{pool: pool}, nil
}

func (store *CheckpointStore) LoadProjectionCheckpoint(
	ctx context.Context,
	scope events.ProjectionScope,
) (events.ProjectionCheckpoint, error) {
	if err := projectionCheckpointContextError(ctx); err != nil {
		return events.ProjectionCheckpoint{}, err
	}
	if store == nil || store.pool == nil || !validProjectionScopeForPostgres(scope) {
		return events.ProjectionCheckpoint{}, events.ErrProjectionInvalidCheckpoint
	}
	connection, err := store.pool.Acquire(ctx)
	if err != nil {
		return events.ProjectionCheckpoint{}, events.ErrProjectionStoreUnavailable
	}
	defer connection.Release()
	transaction, err := connection.BeginTx(ctx, pgx.TxOptions{
		IsoLevel: pgx.RepeatableRead, AccessMode: pgx.ReadOnly,
	})
	if err != nil {
		return events.ProjectionCheckpoint{}, mapProjectionCheckpointError(ctx, err)
	}
	defer rollbackCheckpointTransaction(transaction)
	if err := bindProjectionTenant(ctx, transaction, scope.TenantID); err != nil {
		return events.ProjectionCheckpoint{}, err
	}
	var position int64
	var cursor, lastEventID string
	err = transaction.QueryRow(ctx, `
SELECT global_position, cursor, last_event_id
FROM wanwork_im.event_projection_checkpoints
WHERE tenant_id = $1 AND workspace_id = $2 AND projection_id = $3`,
		scope.TenantID, projectionWorkspaceValue(scope.WorkspaceID), scope.ProjectionID,
	).Scan(&position, &cursor, &lastEventID)
	if errors.Is(err, pgx.ErrNoRows) {
		if err := transaction.Commit(ctx); err != nil {
			return events.ProjectionCheckpoint{}, mapProjectionCheckpointError(ctx, err)
		}
		return zeroPostgresProjectionCheckpoint(scope), nil
	}
	if err != nil {
		return events.ProjectionCheckpoint{}, mapProjectionCheckpointError(ctx, err)
	}
	if position < 0 {
		return events.ProjectionCheckpoint{}, events.ErrProjectionInvalidCheckpoint
	}
	checkpoint := events.ProjectionCheckpoint{
		Scope:       clonePostgresProjectionScope(scope),
		Position:    uint64(position),
		Cursor:      events.Cursor(cursor),
		LastEventID: lastEventID,
	}
	if !validPostgresProjectionCheckpoint(checkpoint, scope) {
		return events.ProjectionCheckpoint{}, events.ErrProjectionInvalidCheckpoint
	}
	if err := transaction.Commit(ctx); err != nil {
		return events.ProjectionCheckpoint{}, mapProjectionCheckpointError(ctx, err)
	}
	return checkpoint, nil
}

func (store *CheckpointStore) CommitProjectionCheckpoint(
	ctx context.Context,
	previous events.ProjectionCheckpoint,
	next events.ProjectionCheckpoint,
) error {
	if err := projectionCheckpointContextError(ctx); err != nil {
		return err
	}
	if store == nil || store.pool == nil || !validProjectionScopeForPostgres(next.Scope) ||
		!validPostgresProjectionCheckpoint(previous, next.Scope) ||
		!validPostgresProjectionCheckpoint(next, next.Scope) ||
		!samePostgresProjectionScope(previous.Scope, next.Scope) ||
		next.Position > math.MaxInt64 {
		return events.ErrProjectionInvalidCheckpoint
	}
	if next.Position < previous.Position {
		return events.ErrProjectionInvalidCheckpoint
	}
	connection, err := store.pool.Acquire(ctx)
	if err != nil {
		return events.ErrProjectionStoreUnavailable
	}
	defer connection.Release()
	transaction, err := connection.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.Serializable})
	if err != nil {
		return mapProjectionCheckpointError(ctx, err)
	}
	defer rollbackCheckpointTransaction(transaction)
	if err := bindProjectionTenant(ctx, transaction, next.Scope.TenantID); err != nil {
		return err
	}
	workspace := projectionWorkspaceValue(next.Scope.WorkspaceID)
	var currentPosition int64
	var currentCursor, currentLastEventID string
	err = transaction.QueryRow(ctx, `
SELECT global_position, cursor, last_event_id
FROM wanwork_im.event_projection_checkpoints
WHERE tenant_id = $1 AND workspace_id = $2 AND projection_id = $3
	`, next.Scope.TenantID, workspace, next.Scope.ProjectionID).
		Scan(&currentPosition, &currentCursor, &currentLastEventID)
	if errors.Is(err, pgx.ErrNoRows) {
		if !isZeroPostgresProjectionCheckpoint(previous, next.Scope) {
			return events.ErrProjectionCheckpointConflict
		}
	} else {
		if err != nil {
			return mapProjectionCheckpointError(ctx, err)
		}
		if currentPosition < 0 || !samePostgresCheckpointValues(previous,
			uint64(currentPosition), events.Cursor(currentCursor), currentLastEventID) {
			return events.ErrProjectionCheckpointConflict
		}
	}
	var written bool
	if err := transaction.QueryRow(ctx, `
SELECT wanwork_im.write_projection_checkpoint(
    $1, $2, $3, $4, $5, $6, $7, $8, $9
)`,
		next.Scope.TenantID, workspace, next.Scope.ProjectionID,
		projectionPositionValue(previous.Position), string(previous.Cursor), previous.LastEventID,
		int64(next.Position), string(next.Cursor), next.LastEventID,
	).Scan(&written); err != nil {
		return mapProjectionCheckpointError(ctx, err)
	}
	if !written {
		return events.ErrProjectionCheckpointConflict
	}
	if err := transaction.Commit(ctx); err != nil {
		return mapProjectionCheckpointError(ctx, err)
	}
	return nil
}

func validProjectionScopeForPostgres(scope events.ProjectionScope) bool {
	return tenantIDPattern.MatchString(scope.TenantID) &&
		(scope.WorkspaceID == nil || workspaceIDPattern.MatchString(*scope.WorkspaceID)) &&
		projectionIdentifierPattern.MatchString(scope.ProjectionID) &&
		len(scope.TenantID) <= maxProjectionIdentifierBytes && len(scope.ProjectionID) <= maxProjectionIdentifierBytes
}

func validPostgresProjectionCheckpoint(checkpoint events.ProjectionCheckpoint, scope events.ProjectionScope) bool {
	if !validProjectionScopeForPostgres(checkpoint.Scope) || !samePostgresProjectionScope(checkpoint.Scope, scope) ||
		checkpoint.Position > math.MaxInt64 || len(checkpoint.Cursor) > maxProjectionCheckpointCursorBytes ||
		len(checkpoint.LastEventID) > maxProjectionIdentifierBytes {
		return false
	}
	if checkpoint.Position == 0 {
		return checkpoint.Cursor == "" && checkpoint.LastEventID == ""
	}
	return checkpoint.Cursor != "" && projectionIdentifierPattern.MatchString(checkpoint.LastEventID)
}

func isZeroPostgresProjectionCheckpoint(checkpoint events.ProjectionCheckpoint, scope events.ProjectionScope) bool {
	return samePostgresProjectionScope(checkpoint.Scope, scope) && checkpoint.Position == 0 && checkpoint.Cursor == "" && checkpoint.LastEventID == ""
}

func samePostgresProjectionScope(left, right events.ProjectionScope) bool {
	return left.TenantID == right.TenantID && left.ProjectionID == right.ProjectionID &&
		projectionOptionalStringsEqual(left.WorkspaceID, right.WorkspaceID)
}

func samePostgresCheckpointValues(previous events.ProjectionCheckpoint, position uint64, cursor events.Cursor, lastEventID string) bool {
	return previous.Position == position && previous.Cursor == cursor && previous.LastEventID == lastEventID
}

func zeroPostgresProjectionCheckpoint(scope events.ProjectionScope) events.ProjectionCheckpoint {
	return events.ProjectionCheckpoint{Scope: clonePostgresProjectionScope(scope)}
}

func clonePostgresProjectionScope(scope events.ProjectionScope) events.ProjectionScope {
	if scope.WorkspaceID != nil {
		workspace := *scope.WorkspaceID
		scope.WorkspaceID = &workspace
	}
	return scope
}

func projectionWorkspaceValue(workspace *string) string {
	if workspace == nil {
		return ""
	}
	return *workspace
}

func projectionOptionalStringsEqual(left, right *string) bool {
	if left == nil || right == nil {
		return left == right
	}
	return *left == *right
}

func bindProjectionTenant(ctx context.Context, transaction pgx.Tx, tenantID string) error {
	if _, err := transaction.Exec(ctx, "SET LOCAL search_path = pg_catalog"); err != nil {
		return mapProjectionCheckpointError(ctx, err)
	}
	var recorded string
	if err := transaction.QueryRow(ctx, `
SELECT pg_catalog.set_config('wanwork.tenant_id', $1, true)`, tenantID).Scan(&recorded); err != nil {
		return mapProjectionCheckpointError(ctx, err)
	}
	if recorded != tenantID {
		return events.ErrProjectionStoreUnavailable
	}
	return nil
}

func projectionCheckpointContextError(ctx context.Context) error {
	if ctx == nil {
		return context.Canceled
	}
	return ctx.Err()
}

func mapProjectionCheckpointError(ctx context.Context, err error) error {
	if err == nil {
		return nil
	}
	if contextErr := projectionCheckpointContextError(ctx); contextErr != nil {
		return contextErr
	}
	if errors.Is(err, events.ErrProjectionCheckpointConflict) || errors.Is(err, events.ErrProjectionInvalidCheckpoint) {
		return err
	}
	return events.ErrProjectionStoreUnavailable
}

func projectionPositionValue(position uint64) int64 {
	if position > math.MaxInt64 {
		return -1
	}
	return int64(position)
}

func rollbackCheckpointTransaction(transaction pgx.Tx) {
	if transaction == nil {
		return
	}
	_ = transaction.Rollback(context.Background())
}
