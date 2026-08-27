package events

import (
	"context"
	"errors"
	"time"
)

var (
	ErrInvalidEvent          = errors.New("invalid event")
	ErrInvalidPayload        = errors.New("invalid event payload")
	ErrPayloadTooLarge       = errors.New("event payload is too large")
	ErrInvalidBatch          = errors.New("invalid event batch")
	ErrRevisionConflict      = errors.New("event stream revision conflict")
	ErrIdempotencyConflict   = errors.New("event idempotency conflict")
	ErrInvalidQuery          = errors.New("invalid event query")
	ErrInvalidCursor         = errors.New("invalid event cursor")
	ErrStoreClock            = errors.New("event store clock is invalid")
	ErrProjectionUnsupported = errors.New("event projection schema is unsupported")
)

type SHA256Digest string

type OpaquePayloadRef struct {
	Storage     string `json:"storage"`
	ReferenceID string `json:"referenceId"`
	ByteLength  uint64 `json:"byteLength"`
}

type PayloadKind string

const (
	PayloadInline    PayloadKind = "inline"
	PayloadReference PayloadKind = "reference"
)

// Payload is immutable outside this package. Inline bytes and references are available only
// through copy-returning accessors, so an admitted event cannot drift after validation.
type Payload struct {
	kind      PayloadKind
	inline    []byte
	reference *OpaquePayloadRef
	digest    SHA256Digest
}

func (payload Payload) Kind() PayloadKind {
	return payload.kind
}

func (payload Payload) InlineJSON() []byte {
	return cloneBytes(payload.inline)
}

func (payload Payload) Reference() *OpaquePayloadRef {
	return clonePayloadReference(payload.reference)
}

func (payload Payload) Digest() SHA256Digest {
	return payload.digest
}

type EventToAppend struct {
	SchemaVersion  uint32
	EventID        string
	StreamID       string
	EventType      string
	TenantID       string
	WorkspaceID    *string
	ActorID        string
	OccurredAt     time.Time
	CorrelationID  string
	CausationID    *string
	IdempotencyKey *string
	Traceparent    *string
	Payload        Payload
}

// StoredEvent adds only store-owned facts. Callers never provide sequence, global position, or
// recorded time to AppendBatch.
type StoredEvent struct {
	EventToAppend
	Sequence       uint64
	GlobalPosition uint64
	RecordedAt     time.Time
}

type AppendBatch struct {
	TenantID        string
	WorkspaceID     *string
	StreamID        string
	ExpectedVersion uint64
	Events          []EventToAppend
}

type AppendResult struct {
	Events   []StoredEvent
	Replayed bool
}

type Cursor string

type StreamQuery struct {
	TenantID    string
	WorkspaceID *string
	StreamID    string
	After       Cursor
	Limit       uint32
}

type GlobalQuery struct {
	TenantID    string
	WorkspaceID *string
	After       Cursor
	Limit       uint32
}

type StreamPage struct {
	Events  []StoredEvent
	Next    Cursor
	HasMore bool
}

type GlobalPage struct {
	Events  []StoredEvent
	Next    Cursor
	HasMore bool
}

type EventStore interface {
	AppendBatch(context.Context, AppendBatch) (AppendResult, error)
	ReadStreamPage(context.Context, StreamQuery) (StreamPage, error)
	ReadGlobalPage(context.Context, GlobalQuery) (GlobalPage, error)
}

func cloneBytes(value []byte) []byte {
	if value == nil {
		return nil
	}
	cloned := make([]byte, len(value))
	copy(cloned, value)
	return cloned
}

func clonePayloadReference(value *OpaquePayloadRef) *OpaquePayloadRef {
	if value == nil {
		return nil
	}
	cloned := *value
	return &cloned
}
