package events

import (
	"context"
	"errors"
	"sync"
	"time"
)

// InboxScope is the exact transport namespace for an inbound provider event. A nil workspace
// means the tenant root; it is not a wildcard. Provider and channel are part of the key so an
// event ID issued by one connector can never suppress an event from another connector.
type InboxScope struct {
	TenantID    string
	WorkspaceID *string
	Provider    string
	ChannelID   string
}

// InboxEnvelope is the verified, canonical payload handed to durable admission. The envelope
// digest is computed by the provider-neutral codec before this port is called; the store binds it
// to the immutable payload digest and never trusts a caller-supplied timestamp or delivery count.
type InboxEnvelope struct {
	Scope          InboxScope
	EventID        string
	EventDigest    SHA256Digest
	VerificationID string
	Payload        Payload
}

type InboxReceipt struct {
	Envelope        InboxEnvelope
	FirstReceivedAt time.Time
	LastReceivedAt  time.Time
	DeliveryCount   uint64
}

type InboxAdmissionStatus string

const (
	InboxInserted InboxAdmissionStatus = "inserted"
	InboxReplayed InboxAdmissionStatus = "replayed"
)

type InboxAdmission struct {
	Status  InboxAdmissionStatus
	Receipt InboxReceipt
}

var (
	ErrInvalidInboxScope     = errors.New("invalid inbound inbox scope")
	ErrInvalidInboxEnvelope  = errors.New("invalid inbound inbox envelope")
	ErrInboxDigestConflict   = errors.New("inbound inbox event digest conflict")
	ErrInboxNotFound         = errors.New("inbound inbox event not found")
	ErrInboxStoreUnavailable = errors.New("inbound inbox store unavailable")
)

// InboxStore admits one verified event exactly once per scope and event ID. A retry with the
// same event digest returns the original receipt; a retry with a different digest must fail
// closed and must not invoke downstream routing.
type InboxStore interface {
	Admit(context.Context, InboxEnvelope) (InboxAdmission, error)
	Load(context.Context, InboxScope, string) (InboxReceipt, error)
}

func ValidateInboxScope(scope InboxScope) error {
	if !validInboxScope(scope) {
		return ErrInvalidInboxScope
	}
	return nil
}

func ValidateInboxEnvelope(envelope InboxEnvelope) error {
	if !validInboxScope(envelope.Scope) ||
		!validOpaqueText(envelope.EventID, maxIdentifierBytes) ||
		!sha256DigestPattern.MatchString(string(envelope.EventDigest)) ||
		!validOpaqueText(envelope.VerificationID, maxIdentifierBytes) ||
		validatePayload(envelope.Payload) != nil {
		return ErrInvalidInboxEnvelope
	}
	return nil
}

func validInboxScope(scope InboxScope) bool {
	return validOpaqueText(scope.TenantID, maxIdentifierBytes) &&
		validOptionalIdentifier(scope.WorkspaceID) &&
		validOpaqueText(scope.Provider, 64) &&
		validOpaqueText(scope.ChannelID, maxIdentifierBytes)
}

func sameInboxScope(left, right InboxScope) bool {
	return left.TenantID == right.TenantID && left.Provider == right.Provider &&
		left.ChannelID == right.ChannelID && optionalStringsEqual(left.WorkspaceID, right.WorkspaceID)
}

func cloneInboxScope(scope InboxScope) InboxScope {
	scope.WorkspaceID = cloneStringPointer(scope.WorkspaceID)
	return scope
}

func cloneInboxEnvelope(envelope InboxEnvelope) InboxEnvelope {
	envelope.Scope = cloneInboxScope(envelope.Scope)
	envelope.Payload = clonePayload(envelope.Payload)
	return envelope
}

func cloneInboxReceipt(receipt InboxReceipt) InboxReceipt {
	receipt.Envelope = cloneInboxEnvelope(receipt.Envelope)
	return receipt
}

type memoryInboxKey struct {
	tenantID    string
	workspaceID string
	provider    string
	channelID   string
	eventID     string
}

// MemoryInboxStore is a deterministic contract fake for unit tests. It is explicitly volatile
// and must not be used as the production IM admission store.
type MemoryInboxStore struct {
	mu      sync.Mutex
	entries map[memoryInboxKey]InboxReceipt
}

func NewMemoryInboxStore() *MemoryInboxStore {
	return &MemoryInboxStore{entries: make(map[memoryInboxKey]InboxReceipt)}
}

func (store *MemoryInboxStore) Admit(ctx context.Context, envelope InboxEnvelope) (InboxAdmission, error) {
	if err := inboxContextError(ctx); err != nil {
		return InboxAdmission{}, err
	}
	if store == nil || envelope.Validate() != nil {
		return InboxAdmission{}, ErrInvalidInboxEnvelope
	}
	key := newMemoryInboxKey(envelope)
	store.mu.Lock()
	defer store.mu.Unlock()
	now := time.Now().UTC()
	if existing, ok := store.entries[key]; ok {
		if existing.Envelope.EventDigest != envelope.EventDigest ||
			existing.Envelope.Payload.Digest() != envelope.Payload.Digest() {
			return InboxAdmission{}, ErrInboxDigestConflict
		}
		existing.LastReceivedAt = now
		existing.DeliveryCount++
		store.entries[key] = existing
		return InboxAdmission{Status: InboxReplayed, Receipt: cloneInboxReceipt(existing)}, nil
	}
	receipt := InboxReceipt{
		Envelope:        cloneInboxEnvelope(envelope),
		FirstReceivedAt: now,
		LastReceivedAt:  now,
		DeliveryCount:   1,
	}
	store.entries[key] = receipt
	return InboxAdmission{Status: InboxInserted, Receipt: cloneInboxReceipt(receipt)}, nil
}

func (store *MemoryInboxStore) Load(ctx context.Context, scope InboxScope, eventID string) (InboxReceipt, error) {
	if err := inboxContextError(ctx); err != nil {
		return InboxReceipt{}, err
	}
	if store == nil || ValidateInboxScope(scope) != nil || !validOpaqueText(eventID, maxIdentifierBytes) {
		return InboxReceipt{}, ErrInvalidInboxScope
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	receipt, ok := store.entries[memoryInboxKey{
		tenantID: scope.TenantID, workspaceID: inboxWorkspaceValue(scope.WorkspaceID),
		provider: scope.Provider, channelID: scope.ChannelID, eventID: eventID,
	}]
	if !ok {
		return InboxReceipt{}, ErrInboxNotFound
	}
	return cloneInboxReceipt(receipt), nil
}

func (envelope InboxEnvelope) Validate() error {
	return ValidateInboxEnvelope(envelope)
}

func newMemoryInboxKey(envelope InboxEnvelope) memoryInboxKey {
	return memoryInboxKey{
		tenantID: envelope.Scope.TenantID, workspaceID: inboxWorkspaceValue(envelope.Scope.WorkspaceID),
		provider: envelope.Scope.Provider, channelID: envelope.Scope.ChannelID, eventID: envelope.EventID,
	}
}

func inboxWorkspaceValue(value *string) string {
	if value == nil {
		return ""
	}
	return *value
}

func inboxContextError(ctx context.Context) error {
	if ctx == nil {
		return context.Canceled
	}
	return ctx.Err()
}
