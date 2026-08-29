package eventstore

import (
	"context"
	"errors"
	"math"
	"strings"
	"testing"
	"time"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/events"
	"github.com/jackc/pgx/v5"
)

func TestNewNativeIMInboxStoreRequiresRuntimePool(t *testing.T) {
	if _, err := NewNativeIMInboxStore(nil); !errors.Is(err, events.ErrInvalidStore) {
		t.Fatalf("nil pool error = %v, want %v", err, events.ErrInvalidStore)
	}
	var store *NativeIMInboxStore
	if _, err := store.Admit(context.Background(), events.InboxEnvelope{}); !errors.Is(err, events.ErrInvalidInboxEnvelope) {
		t.Fatalf("nil store admission error = %v, want %v", err, events.ErrInvalidInboxEnvelope)
	}
	if _, err := store.Load(context.Background(), events.InboxScope{}, "event"); !errors.Is(err, events.ErrInvalidInboxScope) {
		t.Fatalf("nil store load error = %v, want %v", err, events.ErrInvalidInboxScope)
	}
}

func TestNewNativeIMAtomicStoreRequiresRuntimePool(t *testing.T) {
	if _, err := NewNativeIMAtomicStore(nil); !errors.Is(err, events.ErrInvalidStore) {
		t.Fatalf("nil pool error = %v, want %v", err, events.ErrInvalidStore)
	}
	var store *NativeIMAtomicStore
	if _, err := store.AdmitAndAppend(context.Background(), events.InboxEventProjection{}); !errors.Is(err, events.ErrInvalidStore) {
		t.Fatalf("nil store error = %v, want %v", err, events.ErrInvalidStore)
	}
}

func TestAtomicProjectionRejectsInvalidProjectionBeforeDatabaseAccess(t *testing.T) {
	payload, err := events.NewInlinePayload([]byte(`{"message":"hello"}`))
	if err != nil {
		t.Fatalf("payload: %v", err)
	}
	projection := events.InboxEventProjection{
		Envelope: events.InboxEnvelope{
			Scope:   events.InboxScope{TenantID: "ten_alpha", Provider: "rongcloud", ChannelID: "channel_alpha"},
			EventID: "provider-event-1", EventDigest: events.SHA256Digest("sha256:" + strings.Repeat("a", 64)),
			VerificationID: "verification-1", Payload: payload,
		},
		SchemaVersion: 1, StreamID: "task:inbound", EventType: "message.received.v1",
		ActorID: "act_user", OccurredAt: time.Date(2026, time.August, 29, 0, 0, 0, 0, time.UTC),
		CorrelationID: "corr-1", ExpectedVersion: math.MaxUint64,
	}
	if _, err := projection.EventBatch(); !errors.Is(err, events.ErrInvalidInboxEvent) {
		t.Fatalf("invalid projection digest error = %v, want %v", err, events.ErrInvalidInboxEvent)
	}
}

func TestNativeIMInboxValidationBindsProviderScopeAndPayload(t *testing.T) {
	payload, err := events.NewInlinePayload([]byte(`{"message":"hello"}`))
	if err != nil {
		t.Fatalf("payload: %v", err)
	}
	envelope := events.InboxEnvelope{
		Scope:   events.InboxScope{TenantID: "ten_alpha", Provider: "rongcloud", ChannelID: "channel_alpha"},
		EventID: "event_alpha", EventDigest: events.SHA256Digest("sha256:" + strings.Repeat("a", 64)),
		VerificationID: "verification_alpha", Payload: payload,
	}
	if !validNativeIMInboxEnvelope(envelope) {
		t.Fatal("valid native IM inbox envelope rejected")
	}
	for name, mutate := range map[string]func(*events.InboxEnvelope){
		"tenant shape":    func(value *events.InboxEnvelope) { value.Scope.TenantID = "tenant_alpha" },
		"provider shape":  func(value *events.InboxEnvelope) { value.Scope.Provider = "RongCloud" },
		"channel control": func(value *events.InboxEnvelope) { value.Scope.ChannelID = "channel\nalpha" },
		"event digest": func(value *events.InboxEnvelope) {
			value.EventDigest = events.SHA256Digest("sha256:" + strings.Repeat("A", 64))
		},
		"verification empty": func(value *events.InboxEnvelope) { value.VerificationID = "" },
	} {
		t.Run(name, func(t *testing.T) {
			changed := envelope
			mutate(&changed)
			if validNativeIMInboxEnvelope(changed) {
				t.Fatalf("invalid envelope accepted: %#v", changed)
			}
		})
	}
}

func TestMaterializeInboxPayloadRejectsStorageAndDigestDrift(t *testing.T) {
	payload, err := events.NewInlinePayload([]byte(`{"value":1}`))
	if err != nil {
		t.Fatalf("payload: %v", err)
	}
	if got, err := materializeInboxPayload("inline", stringPointer(`{"value":1}`), "", "", -1, string(payload.Digest())); err != nil || got.Digest() != payload.Digest() {
		t.Fatalf("materialize inline = %#v/%v", got, err)
	}
	if _, err := materializeInboxPayload("inline", stringPointer(`{"value":1}`), "", "", -1, "sha256:"+strings.Repeat("b", 64)); !errors.Is(err, events.ErrInvalidPayload) {
		t.Fatalf("digest drift error = %v, want %v", err, events.ErrInvalidPayload)
	}
	if _, err := materializeInboxPayload("reference", nil, "s3", "blob-1", math.MaxInt64, "sha256:"+strings.Repeat("c", 64)); err != nil {
		t.Fatalf("reference materialize: %v", err)
	}
	if _, err := materializeInboxPayload("reference", nil, "s3", "blob-1", -1, "sha256:"+strings.Repeat("c", 64)); !errors.Is(err, events.ErrInvalidPayload) {
		t.Fatalf("negative reference length error = %v, want %v", err, events.ErrInvalidPayload)
	}
}

func TestMapInboxStoreErrorPreservesCancellationOnly(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if !errors.Is(mapInboxStoreError(ctx, errors.New("database")), context.Canceled) {
		t.Fatal("cancellation was not preserved")
	}
	if !errors.Is(mapInboxStoreError(context.Background(), errors.New("database")), events.ErrInboxStoreUnavailable) {
		t.Fatal("database failure was not sanitized")
	}
	if !errors.Is(mapInboxStoreError(context.Background(), events.ErrInboxDigestConflict), events.ErrInboxDigestConflict) {
		t.Fatal("public conflict was not preserved")
	}
	if got := inboxStoreContextError(nil); !errors.Is(got, context.Canceled) {
		t.Fatalf("nil context error = %v", got)
	}
}

func TestDefiniteInboxRollbackSeparatesKnownRollbackFromUnknownOutcome(t *testing.T) {
	if !definiteInboxRollback(pgx.ErrTxCommitRollback) {
		t.Fatal("pgx commit rollback was not classified as definite rollback")
	}
	if definiteInboxRollback(errors.New("synthetic acknowledgement loss")) {
		t.Fatal("generic commit error was incorrectly classified as definite rollback")
	}
}
