package events

import (
	"errors"
	"strings"
	"testing"
	"time"
)

func TestInboxEventProjectionBindsCompleteCanonicalEventDigest(t *testing.T) {
	payload, err := NewInlinePayload([]byte(`{"conversationId":"cnv_group","text":"hello"}`))
	if err != nil {
		t.Fatalf("payload: %v", err)
	}
	workspace := "wsp_acme"
	projection := InboxEventProjection{
		Envelope: InboxEnvelope{
			Scope:   InboxScope{TenantID: "ten_acme", WorkspaceID: &workspace, Provider: "rongcloud", ChannelID: "channel_main"},
			EventID: "evt_message_1", VerificationID: "verify_1", Payload: payload,
		},
		SchemaVersion: 1, StreamID: "cnv_group", EventType: "message.created", ActorID: "usr_alice",
		OccurredAt: time.Date(2026, 8, 29, 10, 11, 12, 13, time.UTC), CorrelationID: "corr_1",
		ExpectedVersion: 0,
	}
	event, err := projection.Event()
	if err == nil {
		t.Fatal("projection without bound event digest was accepted")
	}
	digest, err := DigestEventToAppend(EventToAppend{
		SchemaVersion: projection.SchemaVersion, EventID: projection.Envelope.EventID,
		StreamID: projection.StreamID, EventType: projection.EventType,
		TenantID: projection.Envelope.Scope.TenantID, WorkspaceID: &workspace,
		ActorID: projection.ActorID, OccurredAt: projection.OccurredAt,
		CorrelationID: projection.CorrelationID, Payload: payload,
	})
	if err != nil {
		t.Fatalf("event digest: %v", err)
	}
	projection.Envelope.EventDigest = digest
	event, err = projection.Event()
	if err != nil || event.EventID != "evt_message_1" || event.StreamID != projection.StreamID {
		t.Fatalf("bound projection = %#v/%v", event, err)
	}
	batch, err := projection.EventBatch()
	if err != nil || len(batch.Events) != 1 || batch.Events[0].EventID != event.EventID ||
		batch.ExpectedVersion != 0 || batch.WorkspaceID == nil || *batch.WorkspaceID != workspace {
		t.Fatalf("event batch = %#v/%v", batch, err)
	}
}

func TestInboxEventProjectionRejectsEveryImmutableDigestMutation(t *testing.T) {
	payload, err := NewInlinePayload([]byte(`{"text":"hello"}`))
	if err != nil {
		t.Fatalf("payload: %v", err)
	}
	base := InboxEventProjection{
		Envelope: InboxEnvelope{
			Scope:   InboxScope{TenantID: "ten_acme", Provider: "rongcloud", ChannelID: "channel_main"},
			EventID: "evt_message_2", VerificationID: "verify_2", Payload: payload,
		},
		SchemaVersion: 1, StreamID: "cnv_group", EventType: "message.created", ActorID: "usr_alice",
		OccurredAt: time.Date(2026, 8, 29, 10, 11, 12, 13, time.UTC), CorrelationID: "corr_2",
	}
	canonical := EventToAppend{
		SchemaVersion: base.SchemaVersion, EventID: base.Envelope.EventID, StreamID: base.StreamID,
		EventType: base.EventType, TenantID: base.Envelope.Scope.TenantID, ActorID: base.ActorID,
		OccurredAt: base.OccurredAt, CorrelationID: base.CorrelationID, Payload: payload,
	}
	base.Envelope.EventDigest, err = DigestEventToAppend(canonical)
	if err != nil {
		t.Fatalf("event digest: %v", err)
	}
	if _, err := base.Event(); err != nil {
		t.Fatalf("base projection: %v", err)
	}
	mutations := map[string]func(*InboxEventProjection){
		"stream":      func(value *InboxEventProjection) { value.StreamID = "cnv_other" },
		"event type":  func(value *InboxEventProjection) { value.EventType = "message.edited" },
		"actor":       func(value *InboxEventProjection) { value.ActorID = "usr_bob" },
		"time":        func(value *InboxEventProjection) { value.OccurredAt = value.OccurredAt.Add(time.Second) },
		"correlation": func(value *InboxEventProjection) { value.CorrelationID = "corr_other" },
		"payload": func(value *InboxEventProjection) {
			changed, payloadErr := NewInlinePayload([]byte(`{"text":"changed"}`))
			if payloadErr != nil {
				t.Fatalf("changed payload: %v", payloadErr)
			}
			value.Envelope.Payload = changed
		},
		"workspace": func(value *InboxEventProjection) {
			workspace := "wsp_other"
			value.Envelope.Scope.WorkspaceID = &workspace
		},
	}
	for name, mutate := range mutations {
		t.Run(name, func(t *testing.T) {
			changed := base
			mutate(&changed)
			if _, err := changed.Event(); !errors.Is(err, ErrInvalidInboxEvent) {
				t.Fatalf("mutation accepted: %v", err)
			}
		})
	}
	badDigest := base
	badDigest.Envelope.EventDigest = SHA256Digest("sha256:" + strings.Repeat("f", 64))
	if _, err := badDigest.Event(); !errors.Is(err, ErrInvalidInboxEvent) {
		t.Fatalf("digest mutation accepted: %v", err)
	}
}
