package events

import (
	"bytes"
	"errors"
	"strings"
	"testing"
	"time"
)

var contractTime = time.Date(2026, 8, 28, 12, 0, 0, 123456789, time.FixedZone("CST", 8*60*60))

func TestInlinePayloadCanonicalizesStrictIntegerJSONAndReturnsCopies(t *testing.T) {
	t.Parallel()

	payload, err := NewInlinePayload([]byte(` { "z": [true, null, 2], "a": {"value": "<safe>"} } `))
	if err != nil {
		t.Fatalf("new inline payload: %v", err)
	}
	want := []byte(`{"a":{"value":"<safe>"},"z":[true,null,2]}`)
	if got := payload.InlineJSON(); !bytes.Equal(got, want) {
		t.Fatalf("canonical payload = %s", got)
	}
	first := payload.InlineJSON()
	first[0] ^= 0xff
	if !bytes.Equal(payload.InlineJSON(), want) {
		t.Fatal("payload bytes mutated through accessor")
	}
	if !sha256DigestPattern.MatchString(string(payload.Digest())) {
		t.Fatalf("digest = %q", payload.Digest())
	}
	if got, wantDigest := payload.Digest(), digestRawBytes(want); got != wantDigest {
		t.Fatalf("payload digest = %q, want raw canonical digest %q", got, wantDigest)
	}
}

func TestInlinePayloadRejectsAmbiguousOrUnboundedJSON(t *testing.T) {
	t.Parallel()

	deep := strings.Repeat(`{"x":`, maxJSONDepth+2) + `0` + strings.Repeat(`}`, maxJSONDepth+2)
	testCases := [][]byte{
		[]byte(`{"duplicate":1,"duplicate":2}`),
		[]byte(`{"float":1.5}`),
		[]byte(`{"exponent":1e2}`),
		[]byte(`[1,2,3]`),
		[]byte(`{"ok":1} trailing`),
		[]byte(deep),
		{0xff, 0xfe},
	}
	for _, raw := range testCases {
		if _, err := NewInlinePayload(raw); !errors.Is(err, ErrInvalidPayload) {
			t.Fatalf("payload %q error = %v, want %v", raw, err, ErrInvalidPayload)
		}
	}
	tooLarge := bytes.Repeat([]byte("x"), maxInlinePayloadBytes+1)
	if _, err := NewInlinePayload(tooLarge); !errors.Is(err, ErrPayloadTooLarge) {
		t.Fatalf("large payload error = %v, want %v", err, ErrPayloadTooLarge)
	}
}

func TestReferencedPayloadIsOpaqueAndImmutable(t *testing.T) {
	t.Parallel()

	reference := OpaquePayloadRef{Storage: "object-store", ReferenceID: "tenant/object/123", ByteLength: 42}
	payload, err := NewReferencedPayload(reference, "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
	if err != nil {
		t.Fatalf("new reference payload: %v", err)
	}
	returned := payload.Reference()
	returned.ReferenceID = "mutated"
	if payload.Reference().ReferenceID != reference.ReferenceID || len(payload.InlineJSON()) != 0 {
		t.Fatal("referenced payload mutated or exposed inline bytes")
	}
	if _, err := NewReferencedPayload(reference, "not-a-digest"); !errors.Is(err, ErrInvalidPayload) {
		t.Fatalf("invalid digest error = %v, want %v", err, ErrInvalidPayload)
	}
}

func TestEventDigestCoversEveryImmutableHeaderAndNormalizesTime(t *testing.T) {
	t.Parallel()

	event := validEvent(t, "evt-1", "key-1")
	first, err := DigestEventToAppend(event)
	if err != nil {
		t.Fatalf("digest event: %v", err)
	}
	equivalent := event
	equivalent.OccurredAt = event.OccurredAt.UTC()
	second, err := DigestEventToAppend(equivalent)
	if err != nil || first != second {
		t.Fatalf("normalized digest mismatch: %s != %s, err=%v", first, second, err)
	}
	changed := event
	changed.ActorID = "actor-2"
	third, err := DigestEventToAppend(changed)
	if err != nil || first == third {
		t.Fatalf("changed event digest = %s, original = %s, err=%v", third, first, err)
	}
}

func TestTraceparentRejectsForbiddenVersion(t *testing.T) {
	t.Parallel()

	event := validEvent(t, "evt-1", "key-1")
	forbidden := "ff-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
	event.Traceparent = &forbidden
	if err := ValidateEventToAppend(event); !errors.Is(err, ErrInvalidEvent) {
		t.Fatalf("forbidden traceparent version error = %v, want %v", err, ErrInvalidEvent)
	}
}

func TestAppendBatchRequiresOneExactScopeAndUniqueRetryIdentity(t *testing.T) {
	t.Parallel()

	first := validEvent(t, "evt-1", "key-1")
	second := validEvent(t, "evt-2", "key-2")
	batch := AppendBatch{
		TenantID: first.TenantID, WorkspaceID: cloneStringPointer(first.WorkspaceID),
		StreamID: first.StreamID, ExpectedVersion: 0, Events: []EventToAppend{first, second},
	}
	if err := ValidateAppendBatch(batch); err != nil {
		t.Fatalf("validate batch: %v", err)
	}
	drift := batch
	drift.Events = append([]EventToAppend(nil), batch.Events...)
	drift.Events[1].TenantID = "tenant-other"
	if err := ValidateAppendBatch(drift); !errors.Is(err, ErrInvalidBatch) {
		t.Fatalf("scope drift error = %v, want %v", err, ErrInvalidBatch)
	}
	duplicate := batch
	duplicate.Events = append([]EventToAppend(nil), batch.Events...)
	duplicate.Events[1].IdempotencyKey = cloneStringPointer(batch.Events[0].IdempotencyKey)
	if err := ValidateAppendBatch(duplicate); !errors.Is(err, ErrInvalidBatch) {
		t.Fatalf("duplicate key error = %v, want %v", err, ErrInvalidBatch)
	}
}

func validEvent(t *testing.T, eventID string, idempotencyKey string) EventToAppend {
	t.Helper()
	payload, err := NewInlinePayload([]byte(`{"value":1}`))
	if err != nil {
		t.Fatalf("new payload: %v", err)
	}
	workspace := "workspace-acme"
	causation := "evt-cause"
	traceparent := "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
	return EventToAppend{
		SchemaVersion: 1,
		EventID:       eventID, StreamID: "task:task-1", EventType: "task.created.v1",
		TenantID: "tenant-acme", WorkspaceID: &workspace, ActorID: "actor-1",
		OccurredAt: contractTime, CorrelationID: "correlation-1", CausationID: &causation,
		IdempotencyKey: &idempotencyKey, Traceparent: &traceparent, Payload: payload,
	}
}
