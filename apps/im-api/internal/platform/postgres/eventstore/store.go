// Package eventstore contains the PostgreSQL implementation of the low-level EventStore port.
// It is deliberately separate from the IM projection: events are the durable source, while
// message/conversation projections may be rebuilt from this stream later.
package eventstore

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"math"
	"regexp"
	"time"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/events"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/platform/postgres/runtimepool"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

const (
	maxPageEvents    = 256
	maxEncodedCursor = 4096
	cursorDomain     = "wanwork.im/postgres-event-store/cursor/1\n"
	cursorVersion    = 1
)

var (
	tenantIDPattern    = regexp.MustCompile(`^ten_[A-Za-z0-9]([A-Za-z0-9_-]{0,122}[A-Za-z0-9])?$`)
	workspaceIDPattern = regexp.MustCompile(`^wsp_[A-Za-z0-9]([A-Za-z0-9_-]{0,122}[A-Za-z0-9])?$`)
	streamIDPattern    = regexp.MustCompile("^[^\\x00-\\x20\\x7f]{1,256}$")
)

// These sentinels are adapter-internal: the public events port intentionally exposes only
// outcomes that callers can recover from. A malformed row is an integrity failure, while a
// missing event is only used during the exact-replay probe.
var (
	errEventNotFound  = errors.New("event not found")
	errEventIntegrity = errors.New("event store integrity failure")
)

// Store is durable across process restarts when backed by the PostgreSQL migration 0006
// event_store. The runtime pool remains the only accepted dependency; owner/migrator pools are
// rejected by construction.
type Store struct {
	pool *runtimepool.Pool
}

var _ events.EventStore = (*Store)(nil)

func New(pool *runtimepool.Pool) (*Store, error) {
	if pool == nil {
		return nil, events.ErrInvalidStore
	}
	return &Store{pool: pool}, nil
}

func (store *Store) Characteristics() events.StoreCharacteristics {
	return events.StoreCharacteristics{
		Durability:                               events.StoreDurabilityDurable,
		DeterministicGivenInputsClockAndSchedule: false,
		PersistsAcrossRestart:                    true,
		TamperEvident:                            false,
	}
}

func (store *Store) AppendBatch(ctx context.Context, batch events.AppendBatch) (events.AppendResult, error) {
	if err := contextError(ctx); err != nil {
		return events.AppendResult{}, err
	}
	if store == nil || store.pool == nil || !validBatch(batch) {
		return events.AppendResult{}, events.ErrInvalidBatch
	}

	workspace := workspaceValue(batch.WorkspaceID)
	snapshot := snapshotBatch(batch)
	digests := make([]events.SHA256Digest, len(snapshot.Events))
	for index, event := range snapshot.Events {
		digest, err := events.DigestEventToAppend(event)
		if err != nil {
			return events.AppendResult{}, err
		}
		digests[index] = digest
	}

	connection, err := store.pool.Acquire(ctx)
	if err != nil {
		return events.AppendResult{}, events.ErrStoreUnavailable
	}
	defer connection.Release()
	transaction, err := connection.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.Serializable})
	if err != nil {
		return events.AppendResult{}, mapError(ctx, err)
	}
	defer rollback(transaction)
	if err := bindTenant(ctx, transaction, snapshot.TenantID); err != nil {
		return events.AppendResult{}, err
	}

	existing := make([]events.StoredEvent, len(snapshot.Events))
	found := 0
	for index, event := range snapshot.Events {
		stored, readErr := readEvent(ctx, transaction, snapshot.TenantID, workspace, event.EventID)
		if errors.Is(readErr, errEventNotFound) {
			continue
		}
		if readErr != nil {
			return events.AppendResult{}, mapEventReadError(readErr)
		}
		existing[index] = stored
		found++
	}
	if found != 0 {
		if found != len(snapshot.Events) {
			return events.AppendResult{}, events.ErrIdempotencyConflict
		}
		for index, event := range snapshot.Events {
			stored := existing[index]
			if !sameStoredEvent(stored, event, digests[index], snapshot.StreamID, workspace,
				snapshot.ExpectedVersion+uint64(index)+1) {
				return events.AppendResult{}, events.ErrIdempotencyConflict
			}
		}
		if err := transaction.Commit(ctx); err != nil {
			return events.AppendResult{}, mapError(ctx, err)
		}
		return events.AppendResult{Events: existing, Replayed: true}, nil
	}

	stored := make([]events.StoredEvent, 0, len(snapshot.Events))
	for index, event := range snapshot.Events {
		parts, err := payloadParts(event.Payload)
		if err != nil {
			return events.AppendResult{}, mapEventReadError(err)
		}
		var written bool
		err = transaction.QueryRow(ctx, `
SELECT wanwork_im.write_event(
    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
    $11, $12, $13, $14, $15, $16, $17, $18, $19, $20
)
`,
			snapshot.TenantID,
			workspace,
			snapshot.StreamID,
			int64(snapshot.ExpectedVersion)+int64(index),
			event.EventID,
			int64(event.SchemaVersion),
			event.EventType,
			event.ActorID,
			event.OccurredAt.UTC(),
			event.CorrelationID,
			optionalValue(event.CausationID),
			optionalValue(event.IdempotencyKey),
			optionalValue(event.Traceparent),
			parts.kind,
			parts.inline,
			parts.storage,
			parts.referenceID,
			parts.byteLength,
			parts.digest,
			digests[index],
		).Scan(&written)
		if err != nil {
			return events.AppendResult{}, mapError(ctx, err)
		}
		if !written {
			return events.AppendResult{}, events.ErrRevisionConflict
		}
		value, err := readEvent(ctx, transaction, snapshot.TenantID, workspace, event.EventID)
		if err != nil {
			return events.AppendResult{}, err
		}
		if !sameStoredEvent(value, event, digests[index], snapshot.StreamID, workspace,
			snapshot.ExpectedVersion+uint64(index)+1) {
			return events.AppendResult{}, errEventIntegrity
		}
		stored = append(stored, value)
	}
	if err := transaction.Commit(ctx); err != nil {
		return events.AppendResult{}, mapError(ctx, err)
	}
	return events.AppendResult{Events: stored}, nil
}

func (store *Store) ReadStreamPage(ctx context.Context, query events.StreamQuery) (events.StreamPage, error) {
	if err := contextError(ctx); err != nil {
		return events.StreamPage{}, err
	}
	if store == nil || store.pool == nil || !validStreamQuery(query) {
		return events.StreamPage{}, events.ErrInvalidQuery
	}
	binding := cursorBinding{
		Kind: "stream", TenantID: query.TenantID, WorkspaceID: workspaceValue(query.WorkspaceID),
		WorkspaceSet: query.WorkspaceID != nil, StreamID: query.StreamID,
	}
	after, err := decodeCursor(query.After, binding)
	if err != nil {
		return events.StreamPage{}, mapEventReadError(err)
	}
	connection, err := store.pool.Acquire(ctx)
	if err != nil {
		return events.StreamPage{}, events.ErrStoreUnavailable
	}
	defer connection.Release()
	transaction, err := connection.BeginTx(ctx, pgx.TxOptions{
		IsoLevel: pgx.RepeatableRead, AccessMode: pgx.ReadOnly,
	})
	if err != nil {
		return events.StreamPage{}, mapError(ctx, err)
	}
	defer rollback(transaction)
	if err := bindTenant(ctx, transaction, query.TenantID); err != nil {
		return events.StreamPage{}, err
	}
	var maximum uint64
	if err := transaction.QueryRow(ctx, `
SELECT COALESCE(MAX(sequence), 0)
FROM wanwork_im.event_log
WHERE tenant_id = $1 AND workspace_id = $2 AND stream_id = $3
`,
		query.TenantID, workspaceValue(query.WorkspaceID), query.StreamID,
	).Scan(&maximum); err != nil {
		return events.StreamPage{}, mapError(ctx, err)
	}
	if after > maximum {
		return events.StreamPage{}, events.ErrInvalidCursor
	}
	rows, err := transaction.Query(ctx, `
SELECT workspace_id, stream_id, sequence, global_position, event_id,
       schema_version, event_type, actor_id, occurred_at, correlation_id,
       causation_id, idempotency_key, traceparent, payload_kind, payload_inline,
       payload_storage, payload_reference_id, payload_byte_length, payload_digest,
       append_digest, recorded_at
FROM wanwork_im.event_log
WHERE tenant_id = $1 AND workspace_id = $2 AND stream_id = $3 AND sequence > $4
ORDER BY sequence
LIMIT $5
`,
		query.TenantID, workspaceValue(query.WorkspaceID), query.StreamID, int64(after),
		int64(query.Limit)+1,
	)
	if err != nil {
		return events.StreamPage{}, mapError(ctx, err)
	}
	values, err := scanRows(ctx, rows, query.TenantID)
	if err != nil {
		return events.StreamPage{}, err
	}
	hasMore := len(values) > int(query.Limit)
	if hasMore {
		values = values[:query.Limit]
	}
	next := query.After
	if len(values) != 0 {
		next, err = encodeCursor(binding, values[len(values)-1].Sequence)
		if err != nil {
			return events.StreamPage{}, err
		}
	}
	if err := transaction.Commit(ctx); err != nil {
		return events.StreamPage{}, mapError(ctx, err)
	}
	return events.StreamPage{Events: values, Next: next, HasMore: hasMore}, nil
}

func (store *Store) ReadGlobalPage(ctx context.Context, query events.GlobalQuery) (events.GlobalPage, error) {
	if err := contextError(ctx); err != nil {
		return events.GlobalPage{}, err
	}
	if store == nil || store.pool == nil || !validGlobalQuery(query) {
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
	connection, err := store.pool.Acquire(ctx)
	if err != nil {
		return events.GlobalPage{}, events.ErrStoreUnavailable
	}
	defer connection.Release()
	transaction, err := connection.BeginTx(ctx, pgx.TxOptions{
		IsoLevel: pgx.RepeatableRead, AccessMode: pgx.ReadOnly,
	})
	if err != nil {
		return events.GlobalPage{}, mapError(ctx, err)
	}
	defer rollback(transaction)
	if err := bindTenant(ctx, transaction, query.TenantID); err != nil {
		return events.GlobalPage{}, err
	}
	var maximum uint64
	if err := transaction.QueryRow(ctx, `
SELECT COALESCE(MAX(global_position), 0)
FROM wanwork_im.event_log
WHERE tenant_id = $1 AND workspace_id = $2
`,
		query.TenantID, workspaceValue(query.WorkspaceID),
	).Scan(&maximum); err != nil {
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
`,
		query.TenantID, workspaceValue(query.WorkspaceID), int64(after), int64(query.Limit)+1,
	)
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
	if err := transaction.Commit(ctx); err != nil {
		return events.GlobalPage{}, mapError(ctx, err)
	}
	return events.GlobalPage{Events: values, Next: next, HasMore: hasMore}, nil
}

type payloadPartsValue struct {
	kind, inline, storage, referenceID string
	byteLength                         int64
	digest                             events.SHA256Digest
}

func payloadParts(payload events.Payload) (payloadPartsValue, error) {
	switch payload.Kind() {
	case events.PayloadInline:
		return payloadPartsValue{
			kind: "inline", inline: string(payload.InlineJSON()), byteLength: -1, digest: payload.Digest(),
		}, nil
	case events.PayloadReference:
		reference := payload.Reference()
		if reference == nil {
			return payloadPartsValue{}, events.ErrInvalidPayload
		}
		if reference.ByteLength > math.MaxInt64 {
			return payloadPartsValue{}, events.ErrPayloadTooLarge
		}
		return payloadPartsValue{
			kind: "reference", storage: reference.Storage, referenceID: reference.ReferenceID,
			byteLength: int64(reference.ByteLength), digest: payload.Digest(),
		}, nil
	default:
		return payloadPartsValue{}, events.ErrInvalidPayload
	}
}

func readEvent(
	ctx context.Context,
	transaction pgx.Tx,
	tenantID string,
	workspace string,
	eventID string,
) (events.StoredEvent, error) {
	var storedWorkspace, stream, storedEventID, eventType, actor, correlation string
	var causation, idempotency, traceparent, payloadKind string
	var payloadInline *string
	var storage, referenceID string
	var sequence, globalPosition, schemaVersion, byteLength int64
	var occurredAt, recordedAt time.Time
	var payloadDigest, appendDigest string
	err := transaction.QueryRow(ctx, `
SELECT workspace_id, stream_id, sequence, global_position, event_id,
       schema_version, event_type, actor_id, occurred_at, correlation_id,
       causation_id, idempotency_key, traceparent, payload_kind, payload_inline,
       payload_storage, payload_reference_id, payload_byte_length, payload_digest,
       append_digest, recorded_at
FROM wanwork_im.event_log
WHERE tenant_id = $1 AND workspace_id = $2 AND event_id = $3
`,
		tenantID, workspace, eventID,
	).Scan(
		&storedWorkspace, &stream, &sequence, &globalPosition, &storedEventID,
		&schemaVersion, &eventType, &actor, &occurredAt, &correlation,
		&causation, &idempotency, &traceparent, &payloadKind, &payloadInline,
		&storage, &referenceID, &byteLength, &payloadDigest, &appendDigest, &recordedAt,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return events.StoredEvent{}, errEventNotFound
	}
	if err != nil {
		return events.StoredEvent{}, mapError(ctx, err)
	}
	return materialize(
		tenantID, storedWorkspace, stream, sequence, globalPosition, storedEventID, schemaVersion,
		eventType, actor, occurredAt, correlation, causation, idempotency, traceparent,
		payloadKind, payloadInline, storage, referenceID, byteLength, payloadDigest, appendDigest,
		recordedAt,
	)
}

func scanRows(ctx context.Context, rows pgx.Rows, tenantID string) ([]events.StoredEvent, error) {
	defer rows.Close()
	values := make([]events.StoredEvent, 0)
	for rows.Next() {
		var workspace, stream, eventID, eventType, actor, correlation string
		var causation, idempotency, traceparent, payloadKind string
		var payloadInline *string
		var storage, referenceID string
		var sequence, globalPosition, schemaVersion, byteLength int64
		var occurredAt, recordedAt time.Time
		var payloadDigest, appendDigest string
		if err := rows.Scan(
			&workspace, &stream, &sequence, &globalPosition, &eventID,
			&schemaVersion, &eventType, &actor, &occurredAt, &correlation,
			&causation, &idempotency, &traceparent, &payloadKind, &payloadInline,
			&storage, &referenceID, &byteLength, &payloadDigest, &appendDigest, &recordedAt,
		); err != nil {
			return nil, events.ErrStoreUnavailable
		}
		value, err := materialize(
			tenantID, workspace, stream, sequence, globalPosition, eventID, schemaVersion,
			eventType, actor, occurredAt, correlation, causation, idempotency, traceparent,
			payloadKind, payloadInline, storage, referenceID, byteLength, payloadDigest, appendDigest,
			recordedAt,
		)
		if err != nil {
			return nil, mapEventReadError(err)
		}
		values = append(values, value)
	}
	if err := rows.Err(); err != nil {
		return nil, mapError(ctx, err)
	}
	return values, nil
}

func materialize(
	tenantID, workspace, stream string, sequence, globalPosition int64, eventID string, schemaVersion int64,
	eventType, actor string, occurredAt time.Time, correlation, causation, idempotency,
	traceparent, payloadKind string, payloadInline *string, storage, referenceID string,
	byteLength int64, payloadDigest, appendDigest string, recordedAt time.Time,
) (events.StoredEvent, error) {
	if schemaVersion <= 0 || schemaVersion > math.MaxUint32 || sequence <= 0 || globalPosition <= 0 ||
		recordedAt.IsZero() || recordedAt.Year() < 1 || recordedAt.Year() > 9999 {
		return events.StoredEvent{}, errEventIntegrity
	}
	var payload events.Payload
	switch payloadKind {
	case "inline":
		if payloadInline == nil || byteLength != -1 {
			return events.StoredEvent{}, errEventIntegrity
		}
		var err error
		payload, err = events.NewInlinePayload([]byte(*payloadInline))
		if err != nil || payload.Digest() != events.SHA256Digest(payloadDigest) {
			return events.StoredEvent{}, errEventIntegrity
		}
	case "reference":
		if payloadInline != nil || byteLength < 0 {
			return events.StoredEvent{}, errEventIntegrity
		}
		var err error
		payload, err = events.NewReferencedPayload(events.OpaquePayloadRef{
			Storage: storage, ReferenceID: referenceID, ByteLength: uint64(byteLength),
		}, events.SHA256Digest(payloadDigest))
		if err != nil {
			return events.StoredEvent{}, errEventIntegrity
		}
	default:
		return events.StoredEvent{}, errEventIntegrity
	}
	event := events.EventToAppend{
		SchemaVersion: uint32(schemaVersion), EventID: eventID, StreamID: stream,
		EventType: eventType, TenantID: tenantID, ActorID: actor, OccurredAt: occurredAt.UTC(),
		CorrelationID: correlation, Payload: payload,
	}
	if workspace != "" {
		event.WorkspaceID = stringPointer(workspace)
	}
	if causation != "" {
		event.CausationID = stringPointer(causation)
	}
	if idempotency != "" {
		event.IdempotencyKey = stringPointer(idempotency)
	}
	if traceparent != "" {
		event.Traceparent = stringPointer(traceparent)
	}
	if events.ValidateEventToAppend(event) != nil {
		return events.StoredEvent{}, errEventIntegrity
	}
	stored := events.StoredEvent{
		EventToAppend: event, Sequence: uint64(sequence), GlobalPosition: uint64(globalPosition),
		RecordedAt: recordedAt.UTC(),
	}
	actualDigest, err := events.DigestEventToAppend(event)
	if err != nil || actualDigest != events.SHA256Digest(appendDigest) {
		return events.StoredEvent{}, errEventIntegrity
	}
	return stored, nil
}

func sameStoredEvent(
	stored events.StoredEvent,
	event events.EventToAppend,
	digest events.SHA256Digest,
	stream, workspace string,
	sequence uint64,
) bool {
	return stored.EventID == event.EventID && stored.StreamID == stream && stored.Sequence == sequence &&
		stored.TenantID == event.TenantID && stored.GlobalPosition != 0 &&
		workspace == workspaceValue(event.WorkspaceID) && stored.EventType == event.EventType &&
		stored.ActorID == event.ActorID && stored.OccurredAt.UTC().Equal(event.OccurredAt.UTC()) &&
		func() bool {
			storedDigest, err := events.DigestEventToAppend(stored.EventToAppend)
			return err == nil && storedDigest == digest
		}()
}

func snapshotBatch(batch events.AppendBatch) events.AppendBatch {
	snapshot := batch
	snapshot.WorkspaceID = cloneString(batch.WorkspaceID)
	snapshot.Events = make([]events.EventToAppend, len(batch.Events))
	for index, event := range batch.Events {
		snapshot.Events[index] = event
		snapshot.Events[index].WorkspaceID = cloneString(event.WorkspaceID)
		snapshot.Events[index].CausationID = cloneString(event.CausationID)
		snapshot.Events[index].IdempotencyKey = cloneString(event.IdempotencyKey)
		snapshot.Events[index].Traceparent = cloneString(event.Traceparent)
	}
	return snapshot
}

func validBatch(batch events.AppendBatch) bool {
	if events.ValidateAppendBatch(batch) != nil || batch.ExpectedVersion > math.MaxInt64 ||
		uint64(len(batch.Events)) > uint64(math.MaxInt64)-batch.ExpectedVersion {
		return false
	}
	return validTenant(batch.TenantID) && validWorkspace(batch.WorkspaceID) &&
		streamIDPattern.MatchString(batch.StreamID)
}

func validStreamQuery(query events.StreamQuery) bool {
	return validTenant(query.TenantID) && streamIDPattern.MatchString(query.StreamID) &&
		validWorkspace(query.WorkspaceID) && query.Limit > 0 && query.Limit <= maxPageEvents
}

func validGlobalQuery(query events.GlobalQuery) bool {
	return validTenant(query.TenantID) && validWorkspace(query.WorkspaceID) &&
		query.Limit > 0 && query.Limit <= maxPageEvents
}

func validTenant(value string) bool {
	return len(value) >= 5 && len(value) <= 128 && tenantIDPattern.MatchString(value)
}

func validWorkspace(value *string) bool {
	return value == nil || (len(*value) >= 5 && len(*value) <= 128 && workspaceIDPattern.MatchString(*value))
}

func workspaceValue(value *string) string {
	if value == nil {
		return ""
	}
	return *value
}

func optionalValue(value *string) string {
	if value == nil {
		return ""
	}
	return *value
}

func cloneString(value *string) *string {
	if value == nil {
		return nil
	}
	cloned := *value
	return &cloned
}

func stringPointer(value string) *string { return &value }

func contextError(ctx context.Context) error {
	if ctx == nil {
		return context.Canceled
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	return nil
}

func bindTenant(ctx context.Context, transaction pgx.Tx, tenant string) error {
	if _, err := transaction.Exec(ctx, "SET LOCAL search_path = pg_catalog"); err != nil {
		return mapError(ctx, err)
	}
	var recorded string
	if err := transaction.QueryRow(ctx,
		"SELECT pg_catalog.set_config('wanwork.tenant_id', $1, true)", tenant,
	).Scan(&recorded); err != nil {
		return mapError(ctx, err)
	}
	if recorded != tenant {
		return events.ErrStoreUnavailable
	}
	return nil
}

func mapError(ctx context.Context, err error) error {
	if err == nil {
		return nil
	}
	if ctx != nil {
		if contextErr := ctx.Err(); contextErr != nil {
			return contextErr
		}
	}
	var postgresError *pgconn.PgError
	if errors.As(err, &postgresError) {
		switch postgresError.Code {
		case "23505":
			return events.ErrIdempotencyConflict
		case "22003":
			return events.ErrStoreCapacity
		case "40001", "40P01", "42501", "57P01", "57P02", "57P03":
			return events.ErrStoreUnavailable
		}
	}
	if errors.Is(err, pgx.ErrTxClosed) {
		return events.ErrStoreUnavailable
	}
	return events.ErrStoreUnavailable
}

func mapEventReadError(err error) error {
	if errors.Is(err, errEventIntegrity) || errors.Is(err, errEventNotFound) {
		return events.ErrStoreUnavailable
	}
	return err
}

func rollback(transaction pgx.Tx) {
	if transaction == nil {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = transaction.Rollback(ctx)
}

type cursorBinding struct {
	Kind         string
	TenantID     string
	WorkspaceID  string
	WorkspaceSet bool
	StreamID     string
}

type cursorContent struct {
	Version      uint32 `json:"version"`
	Kind         string `json:"kind"`
	TenantID     string `json:"tenantId"`
	WorkspaceID  string `json:"workspaceId"`
	WorkspaceSet bool   `json:"workspaceSet"`
	StreamID     string `json:"streamId,omitempty"`
	Position     uint64 `json:"position"`
}

type cursorEnvelope struct {
	Content cursorContent `json:"content"`
	Digest  string        `json:"digest"`
}

func encodeCursor(binding cursorBinding, position uint64) (events.Cursor, error) {
	if position > math.MaxInt64 {
		return "", events.ErrInvalidCursor
	}
	content := cursorContent{
		Version: cursorVersion, Kind: binding.Kind, TenantID: binding.TenantID,
		WorkspaceID: binding.WorkspaceID, WorkspaceSet: binding.WorkspaceSet,
		StreamID: binding.StreamID, Position: position,
	}
	raw, err := json.Marshal(content)
	if err != nil {
		return "", events.ErrInvalidCursor
	}
	digest := sha256.Sum256(append([]byte(cursorDomain), raw...))
	envelope, err := json.Marshal(cursorEnvelope{
		Content: content, Digest: hex.EncodeToString(digest[:]),
	})
	if err != nil {
		return "", events.ErrInvalidCursor
	}
	return events.Cursor(base64.RawURLEncoding.EncodeToString(envelope)), nil
}

func decodeCursor(cursor events.Cursor, binding cursorBinding) (uint64, error) {
	if cursor == "" {
		return 0, nil
	}
	if len(cursor) > maxEncodedCursor {
		return 0, events.ErrInvalidCursor
	}
	raw, err := base64.RawURLEncoding.Strict().DecodeString(string(cursor))
	if err != nil || len(raw) == 0 {
		return 0, events.ErrInvalidCursor
	}
	if !validStrictJSON(raw) {
		return 0, events.ErrInvalidCursor
	}
	var envelope cursorEnvelope
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&envelope); err != nil {
		return 0, events.ErrInvalidCursor
	}
	var extra any
	if err := decoder.Decode(&extra); !errors.Is(err, io.EOF) {
		return 0, events.ErrInvalidCursor
	}
	contentRaw, err := json.Marshal(envelope.Content)
	if err != nil {
		return 0, events.ErrInvalidCursor
	}
	digest := sha256.Sum256(append([]byte(cursorDomain), contentRaw...))
	if envelope.Digest != hex.EncodeToString(digest[:]) {
		return 0, events.ErrInvalidCursor
	}
	content := envelope.Content
	if content.Version != cursorVersion || content.Kind != binding.Kind ||
		content.TenantID != binding.TenantID || content.WorkspaceID != binding.WorkspaceID ||
		content.WorkspaceSet != binding.WorkspaceSet || content.StreamID != binding.StreamID ||
		content.Position == 0 || content.Position > math.MaxInt64 {
		return 0, events.ErrInvalidCursor
	}
	return content.Position, nil
}

// validStrictJSON rejects duplicate object keys and trailing values before the typed cursor
// decode. encoding/json's DisallowUnknownFields does not reject duplicate keys by itself.
func validStrictJSON(raw []byte) bool {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	if _, err := strictJSONValue(decoder, 0); err != nil {
		return false
	}
	var trailing any
	return errors.Is(decoder.Decode(&trailing), io.EOF)
}

func strictJSONValue(decoder *json.Decoder, depth int) (any, error) {
	if depth > 32 {
		return nil, errors.New("cursor JSON nesting exceeds limit")
	}
	token, err := decoder.Token()
	if err != nil {
		return nil, err
	}
	switch value := token.(type) {
	case json.Delim:
		switch value {
		case '{':
			object := make(map[string]any)
			for decoder.More() {
				keyToken, err := decoder.Token()
				if err != nil {
					return nil, err
				}
				key, ok := keyToken.(string)
				if !ok {
					return nil, errors.New("cursor JSON object key is not a string")
				}
				if _, exists := object[key]; exists {
					return nil, errors.New("cursor JSON contains duplicate key")
				}
				child, err := strictJSONValue(decoder, depth+1)
				if err != nil {
					return nil, err
				}
				object[key] = child
			}
			end, err := decoder.Token()
			if err != nil || end != json.Delim('}') {
				return nil, errors.New("cursor JSON object is not closed")
			}
			return object, nil
		case '[':
			values := make([]any, 0)
			for decoder.More() {
				child, err := strictJSONValue(decoder, depth+1)
				if err != nil {
					return nil, err
				}
				values = append(values, child)
			}
			end, err := decoder.Token()
			if err != nil || end != json.Delim(']') {
				return nil, errors.New("cursor JSON array is not closed")
			}
			return values, nil
		default:
			return nil, errors.New("cursor JSON delimiter is invalid")
		}
	default:
		return value, nil
	}
}
