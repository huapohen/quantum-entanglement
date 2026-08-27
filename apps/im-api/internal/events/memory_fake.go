package events

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"sync"
	"time"
)

const (
	maxPageEvents       = 256
	maxEncodedCursor    = 4096
	appendDigestDomain  = "wanwork.im/volatile-memory-event-store/append/1\n"
	cursorDigestDomain  = "wanwork.im/volatile-memory-event-store/cursor/1\n"
	cursorSchemaVersion = 1
)

type StoreDurability string

const (
	StoreDurabilityVolatile StoreDurability = "volatile"
)

// StoreCharacteristics prevents a contract fake from being mistaken for a production store.
// VolatileMemoryStore executes no Agent code, survives no process restart, and supplies neither
// tamper evidence nor durable Action receipts.
type StoreCharacteristics struct {
	Durability                       StoreDurability
	DeterministicGivenInputsAndClock bool
	PersistsAcrossRestart            bool
	TamperEvident                    bool
	ProvidesActionReceipts           bool
}

type StoreClock func() time.Time

type eventScope struct {
	tenant       string
	workspaceSet bool
	workspace    string
	stream       string
}

type retryIdentity struct {
	scope eventScope
	kind  string
	value string
}

type appendRecord struct {
	digest SHA256Digest
	events []StoredEvent
}

// VolatileMemoryStore is a deterministic contract fake. Its data disappears when this value or
// its process is discarded. It must never satisfy a production durability or audit requirement.
type VolatileMemoryStore struct {
	mu sync.RWMutex

	clock StoreClock

	streams        map[eventScope][]StoredEvent
	global         []StoredEvent
	retries        map[retryIdentity]*appendRecord
	globalPosition uint64
	lastRecordedAt time.Time
}

var _ EventStore = (*VolatileMemoryStore)(nil)

func NewVolatileMemoryStore(clock StoreClock) (*VolatileMemoryStore, error) {
	if clock == nil {
		return nil, ErrStoreClock
	}
	return &VolatileMemoryStore{
		clock:   clock,
		streams: make(map[eventScope][]StoredEvent),
		retries: make(map[retryIdentity]*appendRecord),
	}, nil
}

func (store *VolatileMemoryStore) Characteristics() StoreCharacteristics {
	return StoreCharacteristics{
		Durability:                       StoreDurabilityVolatile,
		DeterministicGivenInputsAndClock: true,
		PersistsAcrossRestart:            false,
		TamperEvident:                    false,
		ProvidesActionReceipts:           false,
	}
}

func (store *VolatileMemoryStore) AppendBatch(ctx context.Context, batch AppendBatch) (AppendResult, error) {
	if err := contextError(ctx); err != nil {
		return AppendResult{}, err
	}
	if err := ValidateAppendBatch(batch); err != nil {
		return AppendResult{}, err
	}

	request, digest, identities, err := snapshotAppendRequest(batch)
	if err != nil {
		return AppendResult{}, err
	}
	scope := scopeFromBatch(request)

	store.mu.Lock()
	defer store.mu.Unlock()
	if err := contextError(ctx); err != nil {
		return AppendResult{}, err
	}

	if record, replayed, conflict := store.findRetry(identities, digest); conflict {
		return AppendResult{}, ErrIdempotencyConflict
	} else if replayed {
		return AppendResult{Events: cloneStoredEvents(record.events), Replayed: true}, nil
	}

	currentVersion := uint64(len(store.streams[scope]))
	if request.ExpectedVersion != currentVersion {
		return AppendResult{}, ErrRevisionConflict
	}
	recordedAt, err := store.readClock()
	if err != nil {
		return AppendResult{}, err
	}
	if err := contextError(ctx); err != nil {
		return AppendResult{}, err
	}

	stored := make([]StoredEvent, 0, len(request.Events))
	for index, event := range request.Events {
		stored = append(stored, StoredEvent{
			EventToAppend:  snapshotEvent(event),
			Sequence:       currentVersion + uint64(index) + 1,
			GlobalPosition: store.globalPosition + uint64(index) + 1,
			RecordedAt:     recordedAt,
		})
	}
	record := &appendRecord{digest: digest, events: cloneStoredEvents(stored)}
	store.streams[scope] = append(store.streams[scope], cloneStoredEvents(stored)...)
	store.global = append(store.global, cloneStoredEvents(stored)...)
	for _, identity := range identities {
		store.retries[identity] = record
	}
	store.globalPosition += uint64(len(stored))
	store.lastRecordedAt = recordedAt

	return AppendResult{Events: cloneStoredEvents(stored)}, nil
}

func (store *VolatileMemoryStore) ReadStreamPage(ctx context.Context, query StreamQuery) (StreamPage, error) {
	if err := contextError(ctx); err != nil {
		return StreamPage{}, err
	}
	if !validStreamQuery(query) {
		return StreamPage{}, ErrInvalidQuery
	}

	store.mu.RLock()
	defer store.mu.RUnlock()
	if err := contextError(ctx); err != nil {
		return StreamPage{}, err
	}
	scope := scopeFromStreamQuery(query)
	after, err := decodeBoundCursor(query.After, cursorBindingFromScope("stream", scope))
	if err != nil {
		return StreamPage{}, err
	}
	events := store.streams[scope]
	if after > uint64(len(events)) {
		return StreamPage{}, ErrInvalidCursor
	}
	start := int(after)
	end := start + int(query.Limit)
	if end > len(events) {
		end = len(events)
	}
	pageEvents := cloneStoredEvents(events[start:end])
	page := StreamPage{Events: pageEvents, HasMore: end < len(events), Next: query.After}
	if len(pageEvents) > 0 {
		page.Next, err = encodeCursor(cursorBindingFromScope("stream", scope), pageEvents[len(pageEvents)-1].Sequence)
		if err != nil {
			return StreamPage{}, err
		}
	}
	return page, nil
}

func (store *VolatileMemoryStore) ReadGlobalPage(ctx context.Context, query GlobalQuery) (GlobalPage, error) {
	if err := contextError(ctx); err != nil {
		return GlobalPage{}, err
	}
	if !validGlobalQuery(query) {
		return GlobalPage{}, ErrInvalidQuery
	}

	store.mu.RLock()
	defer store.mu.RUnlock()
	if err := contextError(ctx); err != nil {
		return GlobalPage{}, err
	}
	binding := cursorBindingFromGlobalQuery(query)
	after, err := decodeBoundCursor(query.After, binding)
	if err != nil {
		return GlobalPage{}, err
	}
	if after > store.globalPosition {
		return GlobalPage{}, ErrInvalidCursor
	}

	pageEvents := make([]StoredEvent, 0, query.Limit)
	hasMore := false
	for _, event := range store.global {
		if event.GlobalPosition <= after || !eventMatchesGlobalQuery(event, query) {
			continue
		}
		if len(pageEvents) == int(query.Limit) {
			hasMore = true
			break
		}
		pageEvents = append(pageEvents, snapshotStoredEvent(event))
	}
	page := GlobalPage{Events: pageEvents, HasMore: hasMore, Next: query.After}
	if len(pageEvents) > 0 {
		page.Next, err = encodeCursor(binding, pageEvents[len(pageEvents)-1].GlobalPosition)
		if err != nil {
			return GlobalPage{}, err
		}
	}
	return page, nil
}

func (store *VolatileMemoryStore) findRetry(
	identities []retryIdentity,
	digest SHA256Digest,
) (*appendRecord, bool, bool) {
	var matched *appendRecord
	matchedCount := 0
	for _, identity := range identities {
		record, exists := store.retries[identity]
		if !exists {
			continue
		}
		matchedCount++
		if matched == nil {
			matched = record
			continue
		}
		if matched != record {
			return nil, false, true
		}
	}
	if matchedCount == 0 {
		return nil, false, false
	}
	if matchedCount != len(identities) || matched.digest != digest {
		return nil, false, true
	}
	return matched, true, false
}

func (store *VolatileMemoryStore) readClock() (value time.Time, err error) {
	defer func() {
		if recovered := recover(); recovered != nil {
			value = time.Time{}
			err = ErrStoreClock
		}
	}()
	value = normalizeEventTime(store.clock())
	if !validEventTime(value) || (!store.lastRecordedAt.IsZero() && value.Before(store.lastRecordedAt)) {
		return time.Time{}, ErrStoreClock
	}
	return value, nil
}

func snapshotAppendRequest(batch AppendBatch) (AppendBatch, SHA256Digest, []retryIdentity, error) {
	snapshot := AppendBatch{
		TenantID: batch.TenantID, WorkspaceID: cloneStringPointer(batch.WorkspaceID),
		StreamID: batch.StreamID, ExpectedVersion: batch.ExpectedVersion,
		Events: make([]EventToAppend, 0, len(batch.Events)),
	}
	scope := scopeFromBatch(batch)
	identities := make([]retryIdentity, 0, len(batch.Events)*2)
	type canonicalEvent struct {
		EventID        string       `json:"eventId"`
		IdempotencyKey *string      `json:"idempotencyKey"`
		Digest         SHA256Digest `json:"digest"`
	}
	canonicalEvents := make([]canonicalEvent, 0, len(batch.Events))
	for _, event := range batch.Events {
		eventSnapshot := snapshotEvent(event)
		eventDigest, err := DigestEventToAppend(eventSnapshot)
		if err != nil {
			return AppendBatch{}, "", nil, err
		}
		snapshot.Events = append(snapshot.Events, eventSnapshot)
		canonicalEvents = append(canonicalEvents, canonicalEvent{
			EventID: eventSnapshot.EventID, IdempotencyKey: cloneStringPointer(eventSnapshot.IdempotencyKey),
			Digest: eventDigest,
		})
		identities = append(identities, retryIdentity{scope: scope, kind: "event", value: eventSnapshot.EventID})
		if eventSnapshot.IdempotencyKey != nil {
			identities = append(identities, retryIdentity{
				scope: scope, kind: "idempotency", value: *eventSnapshot.IdempotencyKey,
			})
		}
	}
	canonical := struct {
		TenantID        string           `json:"tenantId"`
		WorkspaceID     *string          `json:"workspaceId"`
		StreamID        string           `json:"streamId"`
		ExpectedVersion uint64           `json:"expectedVersion"`
		Events          []canonicalEvent `json:"events"`
	}{
		TenantID: snapshot.TenantID, WorkspaceID: cloneStringPointer(snapshot.WorkspaceID),
		StreamID: snapshot.StreamID, ExpectedVersion: snapshot.ExpectedVersion, Events: canonicalEvents,
	}
	encoded, err := json.Marshal(canonical)
	if err != nil {
		return AppendBatch{}, "", nil, fmt.Errorf("%w: append digest", ErrInvalidBatch)
	}
	return snapshot, digestBytes(appendDigestDomain, encoded), identities, nil
}

type cursorBinding struct {
	kind         string
	tenant       string
	workspaceSet bool
	workspace    string
	stream       string
}

type cursorContent struct {
	Version      uint32 `json:"version"`
	Kind         string `json:"kind"`
	Tenant       string `json:"tenant"`
	WorkspaceSet bool   `json:"workspaceSet"`
	Workspace    string `json:"workspace"`
	Stream       string `json:"stream"`
	Position     uint64 `json:"position"`
}

type cursorEnvelope struct {
	Content cursorContent `json:"content"`
	Digest  SHA256Digest  `json:"digest"`
}

func encodeCursor(binding cursorBinding, position uint64) (Cursor, error) {
	content := cursorContent{
		Version: cursorSchemaVersion, Kind: binding.kind, Tenant: binding.tenant,
		WorkspaceSet: binding.workspaceSet, Workspace: binding.workspace,
		Stream: binding.stream, Position: position,
	}
	encodedContent, err := json.Marshal(content)
	if err != nil {
		return "", ErrInvalidCursor
	}
	envelope := cursorEnvelope{Content: content, Digest: digestBytes(cursorDigestDomain, encodedContent)}
	encodedEnvelope, err := json.Marshal(envelope)
	if err != nil {
		return "", ErrInvalidCursor
	}
	return Cursor(base64.RawURLEncoding.EncodeToString(encodedEnvelope)), nil
}

func decodeBoundCursor(cursor Cursor, binding cursorBinding) (uint64, error) {
	if cursor == "" {
		return 0, nil
	}
	if len(cursor) > maxEncodedCursor {
		return 0, ErrInvalidCursor
	}
	decoded, err := base64.RawURLEncoding.Strict().DecodeString(string(cursor))
	if err != nil {
		return 0, ErrInvalidCursor
	}
	decoder := json.NewDecoder(bytes.NewReader(decoded))
	decoder.DisallowUnknownFields()
	var envelope cursorEnvelope
	if err := decoder.Decode(&envelope); err != nil {
		return 0, ErrInvalidCursor
	}
	var trailing any
	if err := decoder.Decode(&trailing); !errors.Is(err, io.EOF) {
		return 0, ErrInvalidCursor
	}
	encodedContent, err := json.Marshal(envelope.Content)
	if err != nil || envelope.Digest != digestBytes(cursorDigestDomain, encodedContent) {
		return 0, ErrInvalidCursor
	}
	content := envelope.Content
	if content.Version != cursorSchemaVersion || content.Kind != binding.kind ||
		content.Tenant != binding.tenant || content.WorkspaceSet != binding.workspaceSet ||
		content.Workspace != binding.workspace || content.Stream != binding.stream || content.Position == 0 {
		return 0, ErrInvalidCursor
	}
	return content.Position, nil
}

func validStreamQuery(query StreamQuery) bool {
	return validOpaqueText(query.TenantID, maxIdentifierBytes) &&
		validOptionalIdentifier(query.WorkspaceID) &&
		validOpaqueText(query.StreamID, maxIdentifierBytes) &&
		query.Limit > 0 && query.Limit <= maxPageEvents
}

func validGlobalQuery(query GlobalQuery) bool {
	return validOpaqueText(query.TenantID, maxIdentifierBytes) &&
		validOptionalIdentifier(query.WorkspaceID) &&
		query.Limit > 0 && query.Limit <= maxPageEvents
}

func scopeFromBatch(batch AppendBatch) eventScope {
	return newEventScope(batch.TenantID, batch.WorkspaceID, batch.StreamID)
}

func scopeFromStreamQuery(query StreamQuery) eventScope {
	return newEventScope(query.TenantID, query.WorkspaceID, query.StreamID)
}

func newEventScope(tenant string, workspace *string, stream string) eventScope {
	scope := eventScope{tenant: tenant, stream: stream}
	if workspace != nil {
		scope.workspaceSet = true
		scope.workspace = *workspace
	}
	return scope
}

func cursorBindingFromScope(kind string, scope eventScope) cursorBinding {
	return cursorBinding{
		kind: kind, tenant: scope.tenant, workspaceSet: scope.workspaceSet,
		workspace: scope.workspace, stream: scope.stream,
	}
}

func cursorBindingFromGlobalQuery(query GlobalQuery) cursorBinding {
	scope := newEventScope(query.TenantID, query.WorkspaceID, "")
	return cursorBindingFromScope("global", scope)
}

func eventMatchesGlobalQuery(event StoredEvent, query GlobalQuery) bool {
	return event.TenantID == query.TenantID && optionalStringsEqual(event.WorkspaceID, query.WorkspaceID)
}

func contextError(ctx context.Context) error {
	if ctx == nil {
		return context.Canceled
	}
	return ctx.Err()
}
