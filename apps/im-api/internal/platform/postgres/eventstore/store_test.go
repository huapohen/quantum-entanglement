package eventstore

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"math"
	"strings"
	"testing"
	"time"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/events"
)

func TestNewRequiresAttestedRuntimePool(t *testing.T) {
	if _, err := New(nil); !errors.Is(err, events.ErrInvalidStore) {
		t.Fatalf("nil pool error = %v, want %v", err, events.ErrInvalidStore)
	}
	var store *Store
	if got := store.Characteristics(); got.Durability != events.StoreDurabilityDurable ||
		!got.PersistsAcrossRestart || got.TamperEvident {
		t.Fatalf("nil store characteristics = %#v", got)
	}
	if _, err := store.AppendBatch(context.Background(), events.AppendBatch{}); !errors.Is(err, events.ErrInvalidBatch) {
		t.Fatalf("nil store append error = %v, want %v", err, events.ErrInvalidBatch)
	}
}

func TestCursorIsStrictlyBoundAndCanonical(t *testing.T) {
	binding := cursorBinding{
		Kind: "stream", TenantID: "ten_acme", WorkspaceID: "wsp_acme", WorkspaceSet: true,
		StreamID: "task:one",
	}
	cursor, err := encodeCursor(binding, 3)
	if err != nil {
		t.Fatalf("encode cursor: %v", err)
	}
	if got, err := decodeCursor(cursor, binding); err != nil || got != 3 {
		t.Fatalf("decode cursor = %d/%v, want 3/nil", got, err)
	}
	if _, err := decodeCursor(cursor, cursorBinding{Kind: "global", TenantID: binding.TenantID}); !errors.Is(err, events.ErrInvalidCursor) {
		t.Fatalf("scope drift error = %v, want %v", err, events.ErrInvalidCursor)
	}
	zeroCursor, err := encodeCursor(binding, 0)
	if err != nil {
		t.Fatalf("encode zero cursor: %v", err)
	}
	if _, err := decodeCursor(zeroCursor, binding); !errors.Is(err, events.ErrInvalidCursor) {
		t.Fatalf("zero-position cursor error = %v, want %v", err, events.ErrInvalidCursor)
	}

	raw, err := base64.RawURLEncoding.Strict().DecodeString(string(cursor))
	if err != nil {
		t.Fatalf("decode issued cursor: %v", err)
	}
	var envelope cursorEnvelope
	if err := json.Unmarshal(raw, &envelope); err != nil {
		t.Fatalf("unmarshal issued cursor: %v", err)
	}
	content, err := json.Marshal(envelope.Content)
	if err != nil {
		t.Fatalf("marshal content: %v", err)
	}
	duplicate := []byte(`{"content":` + string(content) + `,"content":` + string(content) + `,"digest":"` + envelope.Digest + `"}`)
	duplicateCursor := events.Cursor(base64.RawURLEncoding.EncodeToString(duplicate))
	if _, err := decodeCursor(duplicateCursor, binding); !errors.Is(err, events.ErrInvalidCursor) {
		t.Fatalf("duplicate cursor error = %v, want %v", err, events.ErrInvalidCursor)
	}
	withPadding := events.Cursor(string(cursor) + "=")
	if _, err := decodeCursor(withPadding, binding); !errors.Is(err, events.ErrInvalidCursor) {
		t.Fatalf("padded cursor error = %v, want %v", err, events.ErrInvalidCursor)
	}
	if _, err := encodeCursor(binding, math.MaxInt64+1); !errors.Is(err, events.ErrInvalidCursor) {
		t.Fatalf("overflow cursor error = %v, want %v", err, events.ErrInvalidCursor)
	}
}

func TestPayloadPartsAndMaterializeRejectCorruption(t *testing.T) {
	event := testEvent(t, "evt_one", "key_one", "ten_acme", "wsp_acme", "task:one")
	digest, err := events.DigestEventToAppend(event)
	if err != nil {
		t.Fatalf("digest event: %v", err)
	}
	parts, err := payloadParts(event.Payload)
	if err != nil || parts.kind != "inline" || parts.byteLength != -1 || parts.inline == "" {
		t.Fatalf("inline parts = %#v/%v", parts, err)
	}
	stored, err := materialize(
		event.TenantID, "wsp_acme", event.StreamID, 1, 9, event.EventID, int64(event.SchemaVersion),
		event.EventType, event.ActorID, event.OccurredAt, event.CorrelationID, "", "key_one", "",
		parts.kind, stringPointer(parts.inline), "", "", parts.byteLength, string(parts.digest), string(digest),
		time.Date(2026, time.August, 29, 1, 2, 3, 0, time.UTC),
	)
	if err != nil || stored.EventID != event.EventID || stored.Sequence != 1 || stored.GlobalPosition != 9 {
		t.Fatalf("materialize = %#v/%v", stored, err)
	}
	if _, err := materialize(
		event.TenantID, "wsp_acme", event.StreamID, 1, 9, event.EventID, int64(event.SchemaVersion),
		event.EventType, event.ActorID, event.OccurredAt, event.CorrelationID, "", "key_one", "",
		parts.kind, stringPointer(parts.inline), "", "", parts.byteLength, string(parts.digest), "sha256:"+strings.Repeat("0", 64),
		time.Date(2026, time.August, 29, 1, 2, 3, 0, time.UTC),
	); !errors.Is(err, errEventIntegrity) {
		t.Fatalf("corrupt append digest error = %v, want %v", err, errEventIntegrity)
	}
	if _, err := materialize(
		event.TenantID, "wsp_acme", event.StreamID, 1, 9, event.EventID, int64(event.SchemaVersion),
		event.EventType, event.ActorID, event.OccurredAt, event.CorrelationID, "", "key_one", "",
		parts.kind, stringPointer(parts.inline), "", "", parts.byteLength, string(parts.digest), string(digest), time.Time{},
	); !errors.Is(err, errEventIntegrity) {
		t.Fatalf("zero recordedAt error = %v, want %v", err, errEventIntegrity)
	}
	reference, err := events.NewReferencedPayload(events.OpaquePayloadRef{
		Storage: "s3", ReferenceID: "blob-1", ByteLength: 12,
	}, events.SHA256Digest("sha256:"+strings.Repeat("a", 64)))
	if err != nil {
		t.Fatalf("reference payload: %v", err)
	}
	referenceParts, err := payloadParts(reference)
	if err != nil || referenceParts.kind != "reference" || referenceParts.byteLength != 12 {
		t.Fatalf("reference parts = %#v/%v", referenceParts, err)
	}
	tooLarge, err := events.NewReferencedPayload(events.OpaquePayloadRef{
		Storage: "s3", ReferenceID: "blob-large", ByteLength: math.MaxUint64,
	}, events.SHA256Digest("sha256:"+strings.Repeat("b", 64)))
	if err != nil {
		t.Fatalf("large reference payload: %v", err)
	}
	if _, err := payloadParts(tooLarge); !errors.Is(err, events.ErrPayloadTooLarge) {
		t.Fatalf("large reference error = %v, want %v", err, events.ErrPayloadTooLarge)
	}
}

func TestDatabaseAdmissionMatchesMigrationIdentityShape(t *testing.T) {
	valid := testEvent(t, "evt_one", "key_one", "ten_acme", "wsp_acme", "task:one")
	base := events.AppendBatch{
		TenantID: valid.TenantID, WorkspaceID: valid.WorkspaceID, StreamID: valid.StreamID,
		Events: []events.EventToAppend{valid},
	}
	if !validBatch(base) {
		t.Fatal("valid database event batch was rejected")
	}
	for _, mutate := range []func(*events.AppendBatch){
		func(batch *events.AppendBatch) { batch.TenantID = "tenant_acme" },
		func(batch *events.AppendBatch) { batch.WorkspaceID = stringPointer("workspace_acme") },
		func(batch *events.AppendBatch) { batch.ExpectedVersion = math.MaxInt64 },
	} {
		candidate := base
		candidate.Events = append([]events.EventToAppend(nil), base.Events...)
		mutate(&candidate)
		if validBatch(candidate) {
			t.Fatalf("invalid database batch accepted: %#v", candidate)
		}
	}
}

func testEvent(t *testing.T, eventID, key, tenant, workspace, stream string) events.EventToAppend {
	t.Helper()
	payload, err := events.NewInlinePayload([]byte(`{"value":1}`))
	if err != nil {
		t.Fatalf("payload: %v", err)
	}
	workspaceCopy := workspace
	keyCopy := key
	return events.EventToAppend{
		SchemaVersion: 1, EventID: eventID, StreamID: stream, EventType: "task.created.v1",
		TenantID: tenant, WorkspaceID: &workspaceCopy, ActorID: "act_user",
		OccurredAt:    time.Date(2026, time.August, 29, 0, 0, 0, 0, time.UTC),
		CorrelationID: "corr_one", IdempotencyKey: &keyCopy, Payload: payload,
	}
}
