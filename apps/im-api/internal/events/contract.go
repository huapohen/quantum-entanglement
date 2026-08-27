package events

import (
	"bytes"
	"encoding/json"
	"fmt"
	"regexp"
	"slices"
	"time"
)

const (
	maxBatchEvents      = 128
	maxIdentifierBytes  = 256
	maxEventTypeBytes   = 192
	maxTraceparentBytes = 128
	eventDigestDomain   = "wanwork.im/event-to-append/1\n"
)

var (
	eventTypePattern   = regexp.MustCompile(`^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$`)
	traceparentPattern = regexp.MustCompile(`^[0-9a-f]{2}-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$`)
)

type canonicalEventPayload struct {
	Kind      PayloadKind       `json:"kind"`
	Inline    json.RawMessage   `json:"inline,omitempty"`
	Reference *OpaquePayloadRef `json:"reference,omitempty"`
	Digest    SHA256Digest      `json:"digest"`
}

func ValidateEventToAppend(event EventToAppend) error {
	if event.SchemaVersion == 0 ||
		!validOpaqueText(event.EventID, maxIdentifierBytes) ||
		!validOpaqueText(event.StreamID, maxIdentifierBytes) ||
		!eventTypePattern.MatchString(event.EventType) || len(event.EventType) > maxEventTypeBytes ||
		!validOpaqueText(event.TenantID, maxIdentifierBytes) ||
		!validOptionalIdentifier(event.WorkspaceID) ||
		!validOpaqueText(event.ActorID, maxIdentifierBytes) ||
		!validEventTime(event.OccurredAt) ||
		!validOpaqueText(event.CorrelationID, maxIdentifierBytes) ||
		!validOptionalIdentifier(event.CausationID) ||
		!validOptionalIdentifier(event.IdempotencyKey) ||
		!validTraceparent(event.Traceparent) ||
		validatePayload(event.Payload) != nil {
		return ErrInvalidEvent
	}
	return nil
}

func ValidateAppendBatch(batch AppendBatch) error {
	if !validOpaqueText(batch.TenantID, maxIdentifierBytes) ||
		!validOptionalIdentifier(batch.WorkspaceID) ||
		!validOpaqueText(batch.StreamID, maxIdentifierBytes) ||
		len(batch.Events) == 0 || len(batch.Events) > maxBatchEvents {
		return ErrInvalidBatch
	}
	eventIDs := make(map[string]struct{}, len(batch.Events))
	idempotencyKeys := make(map[string]struct{}, len(batch.Events))
	for _, event := range batch.Events {
		if ValidateEventToAppend(event) != nil ||
			event.TenantID != batch.TenantID ||
			!optionalStringsEqual(event.WorkspaceID, batch.WorkspaceID) ||
			event.StreamID != batch.StreamID {
			return ErrInvalidBatch
		}
		if _, exists := eventIDs[event.EventID]; exists {
			return ErrInvalidBatch
		}
		eventIDs[event.EventID] = struct{}{}
		if event.IdempotencyKey != nil {
			if _, exists := idempotencyKeys[*event.IdempotencyKey]; exists {
				return ErrInvalidBatch
			}
			idempotencyKeys[*event.IdempotencyKey] = struct{}{}
		}
	}
	return nil
}

func DigestEventToAppend(event EventToAppend) (SHA256Digest, error) {
	if err := ValidateEventToAppend(event); err != nil {
		return "", err
	}
	payload := canonicalEventPayload{
		Kind:      event.Payload.Kind(),
		Reference: event.Payload.Reference(),
		Digest:    event.Payload.Digest(),
	}
	if event.Payload.Kind() == PayloadInline {
		payload.Inline = json.RawMessage(event.Payload.InlineJSON())
	}
	canonical := struct {
		SchemaVersion  uint32                `json:"schemaVersion"`
		EventID        string                `json:"eventId"`
		StreamID       string                `json:"streamId"`
		EventType      string                `json:"eventType"`
		TenantID       string                `json:"tenantId"`
		WorkspaceID    *string               `json:"workspaceId"`
		ActorID        string                `json:"actorId"`
		OccurredAt     string                `json:"occurredAt"`
		CorrelationID  string                `json:"correlationId"`
		CausationID    *string               `json:"causationId"`
		IdempotencyKey *string               `json:"idempotencyKey"`
		Traceparent    *string               `json:"traceparent"`
		Payload        canonicalEventPayload `json:"payload"`
	}{
		SchemaVersion: event.SchemaVersion,
		EventID:       event.EventID, StreamID: event.StreamID, EventType: event.EventType,
		TenantID: event.TenantID, WorkspaceID: cloneStringPointer(event.WorkspaceID),
		ActorID: event.ActorID, OccurredAt: normalizeEventTime(event.OccurredAt).Format(time.RFC3339Nano),
		CorrelationID: event.CorrelationID, CausationID: cloneStringPointer(event.CausationID),
		IdempotencyKey: cloneStringPointer(event.IdempotencyKey), Traceparent: cloneStringPointer(event.Traceparent),
		Payload: payload,
	}
	var output bytes.Buffer
	encoder := json.NewEncoder(&output)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(canonical); err != nil {
		return "", fmt.Errorf("%w: canonical encoding", ErrInvalidEvent)
	}
	return digestBytes(eventDigestDomain, bytes.TrimSuffix(output.Bytes(), []byte("\n"))), nil
}

func snapshotEvent(event EventToAppend) EventToAppend {
	return EventToAppend{
		SchemaVersion: event.SchemaVersion,
		EventID:       event.EventID, StreamID: event.StreamID, EventType: event.EventType,
		TenantID: event.TenantID, WorkspaceID: cloneStringPointer(event.WorkspaceID),
		ActorID: event.ActorID, OccurredAt: normalizeEventTime(event.OccurredAt),
		CorrelationID: event.CorrelationID, CausationID: cloneStringPointer(event.CausationID),
		IdempotencyKey: cloneStringPointer(event.IdempotencyKey), Traceparent: cloneStringPointer(event.Traceparent),
		Payload: clonePayload(event.Payload),
	}
}

func snapshotStoredEvent(event StoredEvent) StoredEvent {
	return StoredEvent{
		EventToAppend: snapshotEvent(event.EventToAppend),
		Sequence:      event.Sequence, GlobalPosition: event.GlobalPosition,
		RecordedAt: normalizeEventTime(event.RecordedAt),
	}
}

func clonePayload(payload Payload) Payload {
	return Payload{
		kind: payload.kind, inline: cloneBytes(payload.inline),
		reference: clonePayloadReference(payload.reference), digest: payload.digest,
	}
}

func cloneStoredEvents(events []StoredEvent) []StoredEvent {
	cloned := make([]StoredEvent, 0, len(events))
	for _, event := range events {
		cloned = append(cloned, snapshotStoredEvent(event))
	}
	return cloned
}

func cloneStringPointer(value *string) *string {
	if value == nil {
		return nil
	}
	cloned := *value
	return &cloned
}

func validOptionalIdentifier(value *string) bool {
	return value == nil || validOpaqueText(*value, maxIdentifierBytes)
}

func optionalStringsEqual(left, right *string) bool {
	if left == nil || right == nil {
		return left == nil && right == nil
	}
	return *left == *right
}

func validEventTime(value time.Time) bool {
	return !value.IsZero() && value.Year() >= 1 && value.Year() <= 9999
}

func normalizeEventTime(value time.Time) time.Time {
	return value.Round(0).UTC()
}

func validTraceparent(value *string) bool {
	if value == nil {
		return true
	}
	if len(*value) > maxTraceparentBytes || !traceparentPattern.MatchString(*value) {
		return false
	}
	parts := regexp.MustCompile(`-`).Split(*value, -1)
	return len(parts) == 4 && parts[0] != "ff" && parts[1] != stringsOf('0', 32) && parts[2] != stringsOf('0', 16)
}

func stringsOf(value byte, count int) string {
	return string(slices.Repeat([]byte{value}, count))
}
