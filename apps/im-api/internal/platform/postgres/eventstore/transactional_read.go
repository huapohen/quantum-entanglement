package eventstore

import (
	"context"
	"errors"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/events"
	"github.com/jackc/pgx/v5"
)

// ReadGlobalPageTx reads one global event page without opening or committing a transaction.
// Callers use it when a projection write and its checkpoint must observe the exact same
// PostgreSQL snapshot. The supplied transaction remains owned by the caller and is never closed
// here.
func ReadGlobalPageTx(
	ctx context.Context,
	transaction pgx.Tx,
	query events.GlobalQuery,
) (events.GlobalPage, error) {
	if err := contextError(ctx); err != nil {
		return events.GlobalPage{}, err
	}
	if transaction == nil || !validGlobalQuery(query) {
		return events.GlobalPage{}, events.ErrInvalidQuery
	}
	binding := cursorBinding{
		Kind: "global", TenantID: query.TenantID, WorkspaceID: workspaceValue(query.WorkspaceID),
		WorkspaceSet: query.WorkspaceID != nil,
	}
	after, err := decodeCursor(query.After, binding)
	if err != nil {
		return events.GlobalPage{}, mapEventReadError(err)
	}
	if err := bindTenant(ctx, transaction, query.TenantID); err != nil {
		return events.GlobalPage{}, err
	}
	var maximum uint64
	if err := transaction.QueryRow(ctx, `
SELECT COALESCE(MAX(global_position), 0)
FROM wanwork_im.event_log
WHERE tenant_id = $1 AND workspace_id = $2
`, query.TenantID, workspaceValue(query.WorkspaceID)).Scan(&maximum); err != nil {
		return events.GlobalPage{}, mapError(ctx, err)
	}
	if after > maximum {
		return events.GlobalPage{}, events.ErrInvalidCursor
	}
	rows, err := transaction.Query(ctx, `
SELECT workspace_id, stream_id, sequence, global_position, event_id,
       schema_version, event_type, actor_id, occurred_at, correlation_id,
       causation_id, idempotency_key, traceparent, payload_kind, payload_inline,
       payload_storage, payload_reference_id, payload_byte_length, payload_digest,
       append_digest, recorded_at
FROM wanwork_im.event_log
WHERE tenant_id = $1 AND workspace_id = $2 AND global_position > $3
ORDER BY global_position
LIMIT $4
`, query.TenantID, workspaceValue(query.WorkspaceID), int64(after), int64(query.Limit)+1)
	if err != nil {
		return events.GlobalPage{}, mapError(ctx, err)
	}
	values, err := scanRows(ctx, rows, query.TenantID)
	if err != nil {
		return events.GlobalPage{}, err
	}
	hasMore := len(values) > int(query.Limit)
	if hasMore {
		values = values[:query.Limit]
	}
	next := query.After
	if len(values) != 0 {
		next, err = encodeCursor(binding, values[len(values)-1].GlobalPosition)
		if err != nil {
			return events.GlobalPage{}, err
		}
	}
	return events.GlobalPage{Events: values, Next: next, HasMore: hasMore}, nil
}

// ValidateGlobalPageTx is a small compile-time/test seam for callers that need to assert that a
// transaction-local read was not silently replaced by an independent EventStore connection.
// It intentionally returns the same public errors as ReadGlobalPageTx.
func ValidateGlobalPageTx(ctx context.Context, transaction pgx.Tx, query events.GlobalQuery) error {
	if transaction == nil {
		return events.ErrInvalidQuery
	}
	_, err := ReadGlobalPageTx(ctx, transaction, query)
	if errors.Is(err, events.ErrInvalidQuery) {
		return events.ErrInvalidQuery
	}
	return err
}
