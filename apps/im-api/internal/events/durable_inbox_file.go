package events

// DurableInboxFileStore is a single-process, append-only durability adapter for
// local native-IM recovery exercises. It intentionally does not claim multi-process
// locking, tamper evidence, replication, retention, or production authorization.
// The InboxStore port remains provider-neutral so a PostgreSQL adapter can own the
// production inbox boundary.

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"math"
	"os"
	"path/filepath"
	"sync"
	"time"
)

const durableInboxFileFormat = "quantum-entanglement.native-im-inbox/1"

var (
	// ErrDurableInboxFileClosed is returned after Close. A closed store never
	// silently reopens or accepts a new admission.
	ErrDurableInboxFileClosed = errors.New("durable inbox file store is closed")
	// ErrDurableInboxFileLog means a complete record is malformed or contradicts
	// an earlier durable receipt. Recovery fails closed for such corruption.
	ErrDurableInboxFileLog = errors.New("durable inbox file log is corrupt")
)

// DurableInboxFileStore persists the latest receipt for each scoped provider
// event as newline-delimited JSON. Every admission, including a replay, is
// written and fsynced before its new receipt becomes visible in memory.
//
// A process may die after writing a partial final line; Open discards only that
// incomplete tail. Any complete malformed line fails closed instead.
type DurableInboxFileStore struct {
	mu             sync.RWMutex
	path           string
	file           *os.File
	clock          StoreClock
	entries        map[memoryInboxKey]InboxReceipt
	lastReceivedAt time.Time
	closed         bool
}

var _ InboxStore = (*DurableInboxFileStore)(nil)
var _ InboxAdmissionReconciler = (*DurableInboxFileStore)(nil)

// OpenDurableInboxFileStore opens or creates a local inbox log. The parent
// directory must already exist, preventing an unexpected application data root
// from being created with a different owner or mode.
func OpenDurableInboxFileStore(
	ctx context.Context,
	path string,
	clock StoreClock,
) (*DurableInboxFileStore, error) {
	if err := inboxContextError(ctx); err != nil {
		return nil, err
	}
	if path == "" || filepath.Clean(path) == "." || !filepath.IsAbs(path) || clock == nil {
		if clock == nil {
			return nil, ErrStoreClock
		}
		return nil, ErrInvalidStore
	}
	parent := filepath.Dir(path)
	if info, err := os.Stat(parent); err != nil || !info.IsDir() {
		return nil, ErrInvalidStore
	}
	file, err := os.OpenFile(path, os.O_RDWR|os.O_CREATE|os.O_APPEND, 0o600)
	if err != nil {
		return nil, ErrStoreUnavailable
	}
	store := &DurableInboxFileStore{
		path: path, file: file, clock: clock,
		entries: make(map[memoryInboxKey]InboxReceipt),
	}
	if err := store.load(ctx); err != nil {
		_ = file.Close()
		return nil, err
	}
	return store, nil
}

// Admit implements InboxStore. A duplicate exact envelope is a replay and
// increments DeliveryCount. Any immutable digest drift is rejected without
// changing the durable row.
func (store *DurableInboxFileStore) Admit(
	ctx context.Context,
	envelope InboxEnvelope,
) (InboxAdmission, error) {
	if err := inboxContextError(ctx); err != nil {
		return InboxAdmission{}, err
	}
	if store == nil || envelope.Validate() != nil {
		return InboxAdmission{}, ErrInvalidInboxEnvelope
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	if store.closed || store.file == nil {
		return InboxAdmission{}, ErrDurableInboxFileClosed
	}
	if err := inboxContextError(ctx); err != nil {
		return InboxAdmission{}, err
	}
	key := newMemoryInboxKey(envelope)
	existing, exists := store.entries[key]
	if exists {
		if existing.Envelope.EventDigest != envelope.EventDigest ||
			existing.Envelope.Payload.Digest() != envelope.Payload.Digest() ||
			existing.Envelope.VerificationID != envelope.VerificationID {
			return InboxAdmission{}, ErrInboxDigestConflict
		}
	}
	now, err := store.readClock(ctx)
	if err != nil {
		return InboxAdmission{}, err
	}
	var next InboxReceipt
	status := InboxInserted
	if exists {
		if existing.DeliveryCount == math.MaxUint64 {
			return InboxAdmission{}, ErrStoreCapacity
		}
		status = InboxReplayed
		next = cloneInboxReceipt(existing)
		next.LastReceivedAt = now
		next.DeliveryCount++
	} else {
		next = InboxReceipt{
			Envelope:        cloneInboxEnvelope(envelope),
			FirstReceivedAt: now,
			LastReceivedAt:  now,
			DeliveryCount:   1,
		}
	}
	encoded, err := json.Marshal(newDurableInboxRecord(next))
	if err != nil {
		return InboxAdmission{}, ErrDurableInboxFileLog
	}
	encoded = append(encoded, '\n')
	if err := store.appendAndSync(encoded); err != nil {
		return InboxAdmission{}, err
	}
	store.entries[key] = next
	store.lastReceivedAt = now
	return InboxAdmission{Status: status, Receipt: cloneInboxReceipt(next)}, nil
}

// Load reads a receipt from the exact tenant/workspace/provider/channel scope.
// A nil workspace is the tenant root; it is not a wildcard.
func (store *DurableInboxFileStore) Load(
	ctx context.Context,
	scope InboxScope,
	eventID string,
) (InboxReceipt, error) {
	if err := inboxContextError(ctx); err != nil {
		return InboxReceipt{}, err
	}
	if store == nil || ValidateInboxScope(scope) != nil || !validOpaqueText(eventID, maxIdentifierBytes) {
		return InboxReceipt{}, ErrInvalidInboxScope
	}
	store.mu.RLock()
	defer store.mu.RUnlock()
	if store.closed || store.file == nil {
		return InboxReceipt{}, ErrDurableInboxFileClosed
	}
	receipt, ok := store.entries[memoryInboxKey{
		tenantID: scope.TenantID, workspaceID: inboxWorkspaceValue(scope.WorkspaceID),
		provider: scope.Provider, channelID: scope.ChannelID, eventID: eventID,
	}]
	if !ok {
		return InboxReceipt{}, ErrInboxNotFound
	}
	return cloneInboxReceipt(receipt), nil
}

// Reconcile returns only an observed replay receipt. It never reports a fresh
// insertion, which preserves the commit-acknowledgement-loss contract.
func (store *DurableInboxFileStore) Reconcile(
	ctx context.Context,
	envelope InboxEnvelope,
) (InboxAdmission, error) {
	if err := inboxContextError(ctx); err != nil {
		return InboxAdmission{}, err
	}
	if store == nil || envelope.Validate() != nil {
		return InboxAdmission{}, ErrInvalidInboxEnvelope
	}
	receipt, err := store.Load(ctx, envelope.Scope, envelope.EventID)
	if err != nil {
		return InboxAdmission{}, err
	}
	if receipt.Envelope.EventDigest != envelope.EventDigest ||
		receipt.Envelope.Payload.Digest() != envelope.Payload.Digest() ||
		receipt.Envelope.VerificationID != envelope.VerificationID {
		return InboxAdmission{}, ErrInboxDigestConflict
	}
	return InboxAdmission{
		Status: InboxReplayed, Receipt: receipt, ResolvedAfterUnknown: true,
	}, nil
}

// Close flushes the file and makes all subsequent operations fail closed. It
// is idempotent and safe for a nil receiver.
func (store *DurableInboxFileStore) Close() error {
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

func (store *DurableInboxFileStore) appendAndSync(encoded []byte) error {
	if len(encoded) == 0 || store.file == nil {
		return ErrDurableInboxFileLog
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

func (store *DurableInboxFileStore) readClock(ctx context.Context) (value time.Time, err error) {
	if err := inboxContextError(ctx); err != nil {
		return time.Time{}, err
	}
	defer func() {
		if recovered := recover(); recovered != nil {
			value = time.Time{}
			err = ErrStoreClock
		}
	}()
	value = normalizeEventTime(store.clock(ctx))
	if err := inboxContextError(ctx); err != nil {
		return time.Time{}, err
	}
	if !validEventTime(value) || (!store.lastReceivedAt.IsZero() && value.Before(store.lastReceivedAt)) {
		return time.Time{}, ErrStoreClock
	}
	return value, nil
}

func (store *DurableInboxFileStore) load(ctx context.Context) error {
	if err := inboxContextError(ctx); err != nil {
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
		if err := inboxContextError(ctx); err != nil {
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
			return ErrDurableInboxFileLog
		}
		receipt, err := decodeDurableInboxRecord(line)
		if err != nil {
			return ErrDurableInboxFileLog
		}
		key := newMemoryInboxKey(receipt.Envelope)
		if existing, ok := store.entries[key]; ok {
			if existing.Envelope.EventDigest != receipt.Envelope.EventDigest ||
				existing.Envelope.Payload.Digest() != receipt.Envelope.Payload.Digest() ||
				existing.Envelope.VerificationID != receipt.Envelope.VerificationID ||
				existing.FirstReceivedAt != receipt.FirstReceivedAt ||
				receipt.DeliveryCount <= existing.DeliveryCount ||
				receipt.LastReceivedAt.Before(existing.LastReceivedAt) {
				return ErrDurableInboxFileLog
			}
		}
		if !store.lastReceivedAt.IsZero() && receipt.LastReceivedAt.Before(store.lastReceivedAt) {
			return ErrDurableInboxFileLog
		}
		store.entries[key] = receipt
		store.lastReceivedAt = receipt.LastReceivedAt
		lastGood = end + 1
		start = end + 1
	}
	return nil
}

type durableInboxRecord struct {
	Format          string              `json:"format"`
	Scope           durableInboxScope   `json:"scope"`
	EventID         string              `json:"eventId"`
	EventDigest     SHA256Digest        `json:"eventDigest"`
	VerificationID  string              `json:"verificationId"`
	Payload         durableInboxPayload `json:"payload"`
	FirstReceivedAt time.Time           `json:"firstReceivedAt"`
	LastReceivedAt  time.Time           `json:"lastReceivedAt"`
	DeliveryCount   uint64              `json:"deliveryCount"`
}

// durableInboxScope keeps the on-disk field names stable even though the
// provider-neutral InboxScope intentionally has no wire-format tags.
type durableInboxScope struct {
	TenantID    string  `json:"tenantId"`
	WorkspaceID *string `json:"workspaceId,omitempty"`
	Provider    string  `json:"provider"`
	ChannelID   string  `json:"channelId"`
}

type durableInboxPayload struct {
	Kind      PayloadKind       `json:"kind"`
	Inline    json.RawMessage   `json:"inline,omitempty"`
	Reference *OpaquePayloadRef `json:"reference,omitempty"`
	Digest    SHA256Digest      `json:"digest"`
}

func newDurableInboxRecord(receipt InboxReceipt) durableInboxRecord {
	payload := receipt.Envelope.Payload
	return durableInboxRecord{
		Format: durableInboxFileFormat, Scope: newDurableInboxScope(receipt.Envelope.Scope),
		EventID: receipt.Envelope.EventID, EventDigest: receipt.Envelope.EventDigest,
		VerificationID: receipt.Envelope.VerificationID,
		Payload: durableInboxPayload{Kind: payload.Kind(), Inline: payload.InlineJSON(),
			Reference: payload.Reference(), Digest: payload.Digest()},
		FirstReceivedAt: receipt.FirstReceivedAt, LastReceivedAt: receipt.LastReceivedAt,
		DeliveryCount: receipt.DeliveryCount,
	}
}

func decodeDurableInboxRecord(raw []byte) (InboxReceipt, error) {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	var wire durableInboxRecord
	if err := decoder.Decode(&wire); err != nil {
		return InboxReceipt{}, ErrDurableInboxFileLog
	}
	var trailing any
	if err := decoder.Decode(&trailing); !errors.Is(err, io.EOF) ||
		wire.Format != durableInboxFileFormat || wire.DeliveryCount == 0 ||
		!validInboxTime(wire.FirstReceivedAt) || !validInboxTime(wire.LastReceivedAt) ||
		wire.LastReceivedAt.Before(wire.FirstReceivedAt) {
		return InboxReceipt{}, ErrDurableInboxFileLog
	}
	payload, err := decodeDurableInboxPayload(wire.Payload)
	if err != nil {
		return InboxReceipt{}, err
	}
	envelope := InboxEnvelope{
		Scope: wire.Scope.inboxScope(), EventID: wire.EventID,
		EventDigest: wire.EventDigest, VerificationID: wire.VerificationID, Payload: payload,
	}
	if envelope.Validate() != nil {
		return InboxReceipt{}, ErrDurableInboxFileLog
	}
	return InboxReceipt{
		Envelope: envelope, FirstReceivedAt: wire.FirstReceivedAt,
		LastReceivedAt: wire.LastReceivedAt, DeliveryCount: wire.DeliveryCount,
	}, nil
}

func newDurableInboxScope(scope InboxScope) durableInboxScope {
	return durableInboxScope{
		TenantID: scope.TenantID, WorkspaceID: cloneStringPointer(scope.WorkspaceID),
		Provider: scope.Provider, ChannelID: scope.ChannelID,
	}
}

func (scope durableInboxScope) inboxScope() InboxScope {
	return InboxScope{
		TenantID: scope.TenantID, WorkspaceID: cloneStringPointer(scope.WorkspaceID),
		Provider: scope.Provider, ChannelID: scope.ChannelID,
	}
}

func decodeDurableInboxPayload(wire durableInboxPayload) (Payload, error) {
	switch wire.Kind {
	case PayloadInline:
		if wire.Reference != nil {
			return Payload{}, ErrDurableInboxFileLog
		}
		payload, err := NewInlinePayload(wire.Inline)
		if err != nil || payload.Digest() != wire.Digest {
			return Payload{}, ErrDurableInboxFileLog
		}
		return payload, nil
	case PayloadReference:
		if len(wire.Inline) != 0 || wire.Reference == nil {
			return Payload{}, ErrDurableInboxFileLog
		}
		payload, err := NewReferencedPayload(*wire.Reference, wire.Digest)
		if err != nil {
			return Payload{}, ErrDurableInboxFileLog
		}
		return payload, nil
	default:
		return Payload{}, ErrDurableInboxFileLog
	}
}

func validInboxTime(value time.Time) bool {
	return !value.IsZero() && value.Location() == time.UTC && validEventTime(value)
}
