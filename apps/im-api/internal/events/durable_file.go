package events

// DurableFileStore is a single-process, append-only durability adapter for local recovery
// exercises. It intentionally does not claim multi-process locking, tamper evidence, replication,
// or production authorization. The production EventStore port remains provider-neutral so this
// adapter can be replaced by the PostgreSQL implementation without changing domain contracts.

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"sync"
	"time"
)

const durableFileFormat = "quantum-entanglement.events-file/1"

var (
	ErrDurableFileClosed = errors.New("durable event file store is closed")
	ErrDurableFileLog    = errors.New("durable event file log is corrupt")
)

// DurableFileStore persists each accepted append as one newline-delimited, checksummed JSON
// record. A complete record is written and fsynced before it becomes visible through the in-memory
// projection. A truncated final record is treated as an interrupted write and safely discarded;
// corruption before the final record fails closed.
type DurableFileStore struct {
	mu     sync.RWMutex
	path   string
	file   *os.File
	memory *VolatileMemoryStore
	closed bool
}

var _ EventStore = (*DurableFileStore)(nil)

// OpenDurableFileStore opens or creates a local event log. The parent directory must already
// exist; this avoids silently creating an application data root with an unexpected owner or mode.
func OpenDurableFileStore(
	ctx context.Context,
	path string,
	cursorNamespaceID string,
	clock StoreClock,
) (*DurableFileStore, error) {
	if err := contextError(ctx); err != nil {
		return nil, err
	}
	if path == "" || filepath.Clean(path) == "." || filepath.IsAbs(path) == false {
		return nil, ErrInvalidStore
	}
	parent := filepath.Dir(path)
	if info, err := os.Stat(parent); err != nil || !info.IsDir() {
		return nil, ErrInvalidStore
	}
	memory, err := NewVolatileMemoryStore(cursorNamespaceID, clock)
	if err != nil {
		return nil, err
	}
	file, err := os.OpenFile(path, os.O_RDWR|os.O_CREATE|os.O_APPEND, 0o600)
	if err != nil {
		return nil, ErrStoreUnavailable
	}
	store := &DurableFileStore{path: path, file: file, memory: memory}
	if err := store.load(ctx); err != nil {
		_ = file.Close()
		return nil, err
	}
	return store, nil
}

func (store *DurableFileStore) Characteristics() StoreCharacteristics {
	return StoreCharacteristics{
		Durability:                               StoreDurabilityDurable,
		DeterministicGivenInputsClockAndSchedule: false,
		PersistsAcrossRestart:                    true,
		TamperEvident:                            false,
	}
}

func (store *DurableFileStore) AppendBatch(
	ctx context.Context,
	batch AppendBatch,
) (AppendResult, error) {
	if err := contextError(ctx); err != nil {
		return AppendResult{}, err
	}
	if err := ValidateAppendBatch(batch); err != nil {
		return AppendResult{}, err
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	if store.closed || store.file == nil || store.memory == nil {
		return AppendResult{}, ErrDurableFileClosed
	}
	store.memory.mu.Lock()
	defer store.memory.mu.Unlock()
	if err := contextError(ctx); err != nil {
		return AppendResult{}, err
	}

	request, digest, identities, err := snapshotAppendRequest(ctx, batch)
	if err != nil {
		return AppendResult{}, err
	}
	if record, replayed, conflict := store.memory.findRetry(identities, digest); conflict {
		return AppendResult{}, ErrIdempotencyConflict
	} else if replayed {
		return AppendResult{Events: cloneStoredEvents(record.events), Replayed: true}, nil
	}

	scope := scopeFromBatch(request)
	currentVersion := uint64(len(store.memory.streams[scope]))
	if request.ExpectedVersion != currentVersion {
		return AppendResult{}, ErrRevisionConflict
	}
	batchSize := uint64(len(request.Events))
	if currentVersion > uint64(maxInt64)-batchSize ||
		store.memory.globalPosition > uint64(maxInt64)-batchSize {
		return AppendResult{}, ErrStoreCapacity
	}
	recordedAt, err := store.memory.readClock(ctx)
	if err != nil {
		return AppendResult{}, err
	}
	stored := make([]StoredEvent, 0, len(request.Events))
	for index, event := range request.Events {
		stored = append(stored, StoredEvent{
			EventToAppend:  snapshotEvent(event),
			Sequence:       currentVersion + uint64(index) + 1,
			GlobalPosition: store.memory.globalPosition + uint64(index) + 1,
			RecordedAt:     recordedAt,
		})
	}
	record := &appendRecord{digest: digest, events: cloneStoredEvents(stored)}
	disk := newDurableFileBatch(request, digest, stored, recordedAt)
	encoded, err := json.Marshal(disk)
	if err != nil {
		return AppendResult{}, ErrDurableFileLog
	}
	encoded = append(encoded, '\n')
	if err := store.appendAndSync(encoded); err != nil {
		return AppendResult{}, err
	}
	if err := store.applyStoredBatchLocked(request, digest, identities, record); err != nil {
		// This should be unreachable after the in-memory preflight above. The durable record is
		// retained and will fail closed on reopen rather than silently diverging projections.
		return AppendResult{}, ErrDurableFileLog
	}
	return AppendResult{Events: cloneStoredEvents(stored)}, nil
}

func (store *DurableFileStore) ReadStreamPage(ctx context.Context, query StreamQuery) (StreamPage, error) {
	store.mu.RLock()
	defer store.mu.RUnlock()
	if store.closed || store.memory == nil {
		return StreamPage{}, ErrDurableFileClosed
	}
	return store.memory.ReadStreamPage(ctx, query)
}

func (store *DurableFileStore) ReadGlobalPage(ctx context.Context, query GlobalQuery) (GlobalPage, error) {
	store.mu.RLock()
	defer store.mu.RUnlock()
	if store.closed || store.memory == nil {
		return GlobalPage{}, ErrDurableFileClosed
	}
	return store.memory.ReadGlobalPage(ctx, query)
}

// Close flushes the file and makes all subsequent operations fail closed. It is idempotent.
func (store *DurableFileStore) Close() error {
	if store == nil {
		return nil
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	if store.closed {
		return nil
	}
	store.closed = true
	if store.file == nil {
		return nil
	}
	err := store.file.Sync()
	closeErr := store.file.Close()
	store.file = nil
	if err != nil || closeErr != nil {
		return ErrStoreUnavailable
	}
	return nil
}

func (store *DurableFileStore) appendAndSync(encoded []byte) error {
	if len(encoded) == 0 {
		return ErrDurableFileLog
	}
	start, err := store.file.Seek(0, io.SeekEnd)
	if err != nil {
		return ErrStoreUnavailable
	}
	written := 0
	for written < len(encoded) {
		count, writeErr := store.file.Write(encoded[written:])
		if writeErr != nil || count <= 0 {
			_ = store.file.Truncate(start)
			return ErrStoreUnavailable
		}
		written += count
	}
	if err := store.file.Sync(); err != nil {
		_ = store.file.Truncate(start)
		return ErrStoreUnavailable
	}
	return nil
}

func (store *DurableFileStore) applyStoredBatchLocked(
	request AppendBatch,
	digest SHA256Digest,
	identities []retryIdentity,
	record *appendRecord,
) error {
	if record == nil || len(record.events) != len(request.Events) {
		return ErrDurableFileLog
	}
	scope := scopeFromBatch(request)
	stream := store.memory.streams[scope]
	if uint64(len(stream)) != request.ExpectedVersion {
		return ErrRevisionConflict
	}
	if len(record.events) == 0 || record.digest != digest {
		return ErrDurableFileLog
	}
	for index, event := range record.events {
		if event.Sequence != request.ExpectedVersion+uint64(index)+1 ||
			event.GlobalPosition != store.memory.globalPosition+uint64(index)+1 {
			return ErrDurableFileLog
		}
		if err := ValidateEventToAppend(event.EventToAppend); err != nil {
			return ErrDurableFileLog
		}
	}
	store.memory.streams[scope] = append(stream, cloneStoredEvents(record.events)...)
	store.memory.global = append(store.memory.global, cloneStoredEvents(record.events)...)
	for _, identity := range identities {
		store.memory.retries[identity] = record
	}
	store.memory.globalPosition = record.events[len(record.events)-1].GlobalPosition
	store.memory.lastRecordedAt = record.events[len(record.events)-1].RecordedAt
	return nil
}

const maxInt64 = int64(^uint64(0) >> 1)

type durableFileBatch struct {
	Format          string             `json:"format"`
	TenantID        string             `json:"tenantId"`
	WorkspaceID     *string            `json:"workspaceId,omitempty"`
	StreamID        string             `json:"streamId"`
	ExpectedVersion uint64             `json:"expectedVersion"`
	AppendDigest    SHA256Digest       `json:"appendDigest"`
	RecordedAt      time.Time          `json:"recordedAt"`
	Events          []durableFileEvent `json:"events"`
}

type durableFileEvent struct {
	SchemaVersion  uint32             `json:"schemaVersion"`
	EventID        string             `json:"eventId"`
	StreamID       string             `json:"streamId"`
	EventType      string             `json:"eventType"`
	TenantID       string             `json:"tenantId"`
	WorkspaceID    *string            `json:"workspaceId,omitempty"`
	ActorID        string             `json:"actorId"`
	OccurredAt     time.Time          `json:"occurredAt"`
	CorrelationID  string             `json:"correlationId"`
	CausationID    *string            `json:"causationId,omitempty"`
	IdempotencyKey *string            `json:"idempotencyKey,omitempty"`
	Traceparent    *string            `json:"traceparent,omitempty"`
	Payload        durableFilePayload `json:"payload"`
	Sequence       uint64             `json:"sequence"`
	GlobalPosition uint64             `json:"globalPosition"`
	RecordedAt     time.Time          `json:"recordedAt"`
}

type durableFilePayload struct {
	Kind      PayloadKind       `json:"kind"`
	Inline    json.RawMessage   `json:"inline,omitempty"`
	Reference *OpaquePayloadRef `json:"reference,omitempty"`
	Digest    SHA256Digest      `json:"digest"`
}

func newDurableFileBatch(
	request AppendBatch,
	digest SHA256Digest,
	stored []StoredEvent,
	recordedAt time.Time,
) durableFileBatch {
	wire := durableFileBatch{
		Format: durableFileFormat, TenantID: request.TenantID,
		WorkspaceID: cloneStringPointer(request.WorkspaceID), StreamID: request.StreamID,
		ExpectedVersion: request.ExpectedVersion, AppendDigest: digest,
		RecordedAt: recordedAt, Events: make([]durableFileEvent, 0, len(stored)),
	}
	for _, event := range stored {
		payload := event.Payload
		wire.Events = append(wire.Events, durableFileEvent{
			SchemaVersion: event.SchemaVersion, EventID: event.EventID, StreamID: event.StreamID,
			EventType: event.EventType, TenantID: event.TenantID,
			WorkspaceID: cloneStringPointer(event.WorkspaceID), ActorID: event.ActorID,
			OccurredAt: event.OccurredAt, CorrelationID: event.CorrelationID,
			CausationID:    cloneStringPointer(event.CausationID),
			IdempotencyKey: cloneStringPointer(event.IdempotencyKey),
			Traceparent:    cloneStringPointer(event.Traceparent),
			Payload: durableFilePayload{
				Kind: payload.Kind(), Inline: payload.InlineJSON(), Reference: payload.Reference(),
				Digest: payload.Digest(),
			},
			Sequence: event.Sequence, GlobalPosition: event.GlobalPosition, RecordedAt: event.RecordedAt,
		})
	}
	return wire
}

func (store *DurableFileStore) load(ctx context.Context) error {
	if err := contextError(ctx); err != nil {
		return err
	}
	data, err := os.ReadFile(store.path)
	if err != nil {
		return ErrStoreUnavailable
	}
	if len(data) == 0 {
		return nil
	}
	lastGood := 0
	for start := 0; start < len(data); {
		if err := contextError(ctx); err != nil {
			return err
		}
		relativeEnd := bytes.IndexByte(data[start:], '\n')
		if relativeEnd < 0 {
			// A process can die between write and newline. Drop only this final tail.
			if len(bytes.TrimSpace(data[start:])) > 0 {
				if err := store.file.Truncate(int64(lastGood)); err != nil {
					return ErrStoreUnavailable
				}
			}
			return nil
		}
		end := start + relativeEnd
		line := data[start:end]
		if len(bytes.TrimSpace(line)) == 0 {
			return ErrDurableFileLog
		}
		record, err := decodeDurableFileBatch(line)
		if err != nil {
			return ErrDurableFileLog
		}
		request, digest, identities, err := snapshotAppendRequest(ctx, record.request())
		if err != nil || digest != record.AppendDigest || request.ExpectedVersion != record.ExpectedVersion {
			return ErrDurableFileLog
		}
		store.memory.mu.Lock()
		if err := store.applyStoredBatchLocked(request, digest, identities, &appendRecord{
			digest: digest, events: cloneStoredEvents(record.events),
		}); err != nil {
			store.memory.mu.Unlock()
			return ErrDurableFileLog
		}
		store.memory.mu.Unlock()
		lastGood = end + 1
		start = end + 1
	}
	return nil
}

type decodedDurableFileBatch struct {
	durableFileBatch
	events []StoredEvent
}

func decodeDurableFileBatch(raw []byte) (decodedDurableFileBatch, error) {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	var wire durableFileBatch
	if err := decoder.Decode(&wire); err != nil {
		return decodedDurableFileBatch{}, ErrDurableFileLog
	}
	var trailing any
	if err := decoder.Decode(&trailing); !errors.Is(err, io.EOF) || wire.Format != durableFileFormat ||
		wire.RecordedAt.IsZero() || wire.RecordedAt.Location() != time.UTC || len(wire.Events) == 0 {
		return decodedDurableFileBatch{}, ErrDurableFileLog
	}
	request := wire.request()
	if ValidateAppendBatch(request) != nil {
		return decodedDurableFileBatch{}, ErrDurableFileLog
	}
	events := make([]StoredEvent, 0, len(wire.Events))
	for _, event := range wire.Events {
		if event.RecordedAt.IsZero() || event.RecordedAt.Location() != time.UTC || event.RecordedAt != wire.RecordedAt {
			return decodedDurableFileBatch{}, ErrDurableFileLog
		}
		payload, err := decodeDurableFilePayload(event.Payload)
		if err != nil {
			return decodedDurableFileBatch{}, err
		}
		storedInput := EventToAppend{
			SchemaVersion: event.SchemaVersion, EventID: event.EventID, StreamID: event.StreamID,
			EventType: event.EventType, TenantID: event.TenantID,
			WorkspaceID: cloneStringPointer(event.WorkspaceID), ActorID: event.ActorID,
			OccurredAt: event.OccurredAt, CorrelationID: event.CorrelationID,
			CausationID:    cloneStringPointer(event.CausationID),
			IdempotencyKey: cloneStringPointer(event.IdempotencyKey),
			Traceparent:    cloneStringPointer(event.Traceparent), Payload: payload,
		}
		if ValidateEventToAppend(storedInput) != nil {
			return decodedDurableFileBatch{}, ErrDurableFileLog
		}
		events = append(events, StoredEvent{
			EventToAppend: storedInput, Sequence: event.Sequence,
			GlobalPosition: event.GlobalPosition, RecordedAt: event.RecordedAt,
		})
	}
	return decodedDurableFileBatch{durableFileBatch: wire, events: events}, nil
}

func (wire durableFileBatch) request() AppendBatch {
	events := make([]EventToAppend, 0, len(wire.Events))
	for _, event := range wire.Events {
		payload, _ := decodeDurableFilePayload(event.Payload)
		events = append(events, EventToAppend{
			SchemaVersion: event.SchemaVersion, EventID: event.EventID, StreamID: event.StreamID,
			EventType: event.EventType, TenantID: event.TenantID,
			WorkspaceID: cloneStringPointer(event.WorkspaceID), ActorID: event.ActorID,
			OccurredAt: event.OccurredAt, CorrelationID: event.CorrelationID,
			CausationID:    cloneStringPointer(event.CausationID),
			IdempotencyKey: cloneStringPointer(event.IdempotencyKey),
			Traceparent:    cloneStringPointer(event.Traceparent), Payload: payload,
		})
	}
	return AppendBatch{
		TenantID: wire.TenantID, WorkspaceID: cloneStringPointer(wire.WorkspaceID),
		StreamID: wire.StreamID, ExpectedVersion: wire.ExpectedVersion, Events: events,
	}
}

func decodeDurableFilePayload(wire durableFilePayload) (Payload, error) {
	switch wire.Kind {
	case PayloadInline:
		payload, err := NewInlinePayload(wire.Inline)
		if err != nil || payload.Digest() != wire.Digest {
			return Payload{}, ErrDurableFileLog
		}
		return payload, nil
	case PayloadReference:
		if wire.Reference == nil {
			return Payload{}, ErrDurableFileLog
		}
		payload, err := NewReferencedPayload(*wire.Reference, wire.Digest)
		if err != nil {
			return Payload{}, ErrDurableFileLog
		}
		return payload, nil
	default:
		return Payload{}, ErrDurableFileLog
	}
}
