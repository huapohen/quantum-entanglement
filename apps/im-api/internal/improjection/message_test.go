package improjection

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/events"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
)

func TestMessageProjectionReplaysCreateEditAndRecallWithoutDuplicateState(t *testing.T) {
	t.Parallel()
	tenant, err := im.ParseTenantID("ten_alpha")
	if err != nil {
		t.Fatal(err)
	}
	conversationID, err := im.ParseConversationID("cnv_room")
	if err != nil {
		t.Fatal(err)
	}
	reference, err := im.NewConversationRef(tenant, conversationID)
	if err != nil {
		t.Fatal(err)
	}
	projection, err := NewMessageProjection(reference)
	if err != nil {
		t.Fatal(err)
	}
	created := projectionEvent(t, reference, 1, "evt_created", "message.created", "usr_alice", "{\"clientMessageId\":\"msg_client_1\",\"conversationId\":\"cnv_room\",\"extInfo\":\"\",\"messageId\":\"msg_1\",\"messageType\":\"text\",\"text\":\"hello\"}")
	edited := projectionEvent(t, reference, 2, "evt_edited", "message.edited", "usr_alice", "{\"conversationId\":\"cnv_room\",\"messageId\":\"msg_1\",\"text\":\"hello again\"}")
	recalled := projectionEvent(t, reference, 3, "evt_recalled", "message.recalled", "usr_alice", "{\"conversationId\":\"cnv_room\",\"messageId\":\"msg_1\"}")
	for _, event := range []events.StoredEvent{created, edited, recalled} {
		if err := projection.Apply(context.Background(), event); err != nil {
			t.Fatalf("apply %s: %v", event.EventType, err)
		}
	}
	if err := projection.Apply(context.Background(), created); err != nil {
		t.Fatalf("replay create: %v", err)
	}
	messages := projection.Messages()
	if len(messages) != 1 || messages[0].Ref().MessageID().String() != "msg_1" ||
		messages[0].Status() != im.MessageStatusRecalled || messages[0].Text() != "" ||
		messages[0].Revision() != 3 || projection.LastSequence() != 3 {
		t.Fatalf("projection state = %#v, last sequence=%d", messages, projection.LastSequence())
	}
}

func TestMessageProjectionRejectsScopeOrderPayloadAndMutationConflicts(t *testing.T) {
	t.Parallel()
	tenant, err := im.ParseTenantID("ten_alpha")
	if err != nil {
		t.Fatal(err)
	}
	conversationID, err := im.ParseConversationID("cnv_room")
	if err != nil {
		t.Fatal(err)
	}
	reference, err := im.NewConversationRef(tenant, conversationID)
	if err != nil {
		t.Fatal(err)
	}
	projection, err := NewMessageProjection(reference)
	if err != nil {
		t.Fatal(err)
	}
	created := projectionEvent(t, reference, 1, "evt_created", "message.created", "usr_alice", "{\"clientMessageId\":\"msg_client_1\",\"conversationId\":\"cnv_room\",\"messageId\":\"msg_1\",\"messageType\":\"text\",\"text\":\"hello\"}")
	if err := projection.Apply(context.Background(), created); err != nil {
		t.Fatal(err)
	}
	cases := []struct {
		name  string
		event events.StoredEvent
		want  error
	}{
		{
			name:  "out of order",
			event: projectionEvent(t, reference, 1, "evt_old", "message.edited", "usr_alice", "{\"conversationId\":\"cnv_room\",\"messageId\":\"msg_1\",\"text\":\"old\"}"),
			want:  ErrProjectionOrder,
		},
		{
			name:  "duplicate created message",
			event: projectionEvent(t, reference, 2, "evt_second_create", "message.created", "usr_alice", "{\"clientMessageId\":\"msg_client_2\",\"conversationId\":\"cnv_room\",\"messageId\":\"msg_1\",\"messageType\":\"text\",\"text\":\"other\"}"),
			want:  ErrProjectionConflict,
		},
		{
			name:  "unknown field",
			event: projectionEvent(t, reference, 2, "evt_unknown", "message.edited", "usr_alice", "{\"conversationId\":\"cnv_room\",\"messageId\":\"msg_1\",\"text\":\"next\",\"role\":\"owner\"}"),
			want:  ErrInvalidProjection,
		},
		{
			name:  "reference payload",
			event: projectionReferenceEvent(reference, 2, "evt_reference", "message.edited"),
			want:  ErrInvalidProjection,
		},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			if err := projection.Apply(context.Background(), testCase.event); !errors.Is(err, testCase.want) {
				t.Fatalf("error = %v, want %v", err, testCase.want)
			}
		})
	}
	wrongTenant, err := im.ParseTenantID("ten_other")
	if err != nil {
		t.Fatal(err)
	}
	wrongReference, err := im.NewConversationRef(wrongTenant, conversationID)
	if err != nil {
		t.Fatal(err)
	}
	wrongScope := projectionEvent(t, wrongReference, 2, "evt_wrong_scope", "message.edited", "usr_alice", "{\"conversationId\":\"cnv_room\",\"messageId\":\"msg_1\",\"text\":\"cross\"}")
	if err := projection.Apply(context.Background(), wrongScope); !errors.Is(err, ErrProjectionScope) {
		t.Fatalf("wrong scope error = %v, want %v", err, ErrProjectionScope)
	}
	missing := projectionEvent(t, reference, 2, "evt_missing", "message.recalled", "usr_alice", "{\"conversationId\":\"cnv_room\"}")
	if err := projection.Apply(context.Background(), missing); !errors.Is(err, ErrInvalidProjection) {
		t.Fatalf("missing field error = %v, want %v", err, ErrInvalidProjection)
	}
}

func TestMessageProjectionRejectsEditingRecalledAndUnknownMessages(t *testing.T) {
	t.Parallel()
	tenant, err := im.ParseTenantID("ten_alpha")
	if err != nil {
		t.Fatal(err)
	}
	conversationID, err := im.ParseConversationID("cnv_room")
	if err != nil {
		t.Fatal(err)
	}
	reference, err := im.NewConversationRef(tenant, conversationID)
	if err != nil {
		t.Fatal(err)
	}
	projection, err := NewMessageProjection(reference)
	if err != nil {
		t.Fatal(err)
	}
	created := projectionEvent(t, reference, 1, "evt_created", "message.created", "usr_alice", "{\"clientMessageId\":\"msg_client_1\",\"conversationId\":\"cnv_room\",\"messageId\":\"msg_1\",\"messageType\":\"text\",\"text\":\"hello\"}")
	if err := projection.Apply(context.Background(), created); err != nil {
		t.Fatal(err)
	}
	recalled := projectionEvent(t, reference, 2, "evt_recalled", "message.recalled", "usr_alice", "{\"conversationId\":\"cnv_room\",\"messageId\":\"msg_1\"}")
	if err := projection.Apply(context.Background(), recalled); err != nil {
		t.Fatal(err)
	}
	editRecalled := projectionEvent(t, reference, 3, "evt_edit_recalled", "message.edited", "usr_alice", "{\"conversationId\":\"cnv_room\",\"messageId\":\"msg_1\",\"text\":\"again\"}")
	if err := projection.Apply(context.Background(), editRecalled); !errors.Is(err, ErrProjectionConflict) {
		t.Fatalf("edit recalled error = %v, want %v", err, ErrProjectionConflict)
	}
	editMissing := projectionEvent(t, reference, 3, "evt_edit_missing", "message.edited", "usr_alice", "{\"conversationId\":\"cnv_room\",\"messageId\":\"msg_missing\",\"text\":\"again\"}")
	if err := projection.Apply(context.Background(), editMissing); !errors.Is(err, ErrProjectionConflict) {
		t.Fatalf("edit missing error = %v, want %v", err, ErrProjectionConflict)
	}
	unknown := projectionEvent(t, reference, 3, "evt_unknown_type", "message.deleted", "usr_alice", "{\"conversationId\":\"cnv_room\",\"messageId\":\"msg_1\"}")
	if err := projection.Apply(context.Background(), unknown); !errors.Is(err, ErrInvalidProjection) {
		t.Fatalf("unknown type error = %v, want %v", err, ErrInvalidProjection)
	}
	if projection.LastSequence() != 2 || len(projection.Messages()) != 1 {
		t.Fatalf("failed events mutated projection: last=%d messages=%d", projection.LastSequence(), len(projection.Messages()))
	}
}

func projectionEvent(
	t *testing.T,
	reference im.ConversationRef,
	sequence uint64,
	eventID, eventType, actorID, raw string,
) events.StoredEvent {
	t.Helper()
	payload, err := events.NewInlinePayload([]byte(raw))
	if err != nil {
		t.Fatalf("payload: %v", err)
	}
	return events.StoredEvent{
		EventToAppend: events.EventToAppend{
			SchemaVersion: 1, EventID: eventID, StreamID: reference.ConversationID().String(),
			EventType: eventType, TenantID: reference.TenantID().String(), ActorID: actorID,
			OccurredAt:    time.Date(2026, 8, 29, 12, 0, int(sequence), 0, time.UTC),
			CorrelationID: "corr_" + eventID, Payload: payload,
		},
		Sequence: sequence, GlobalPosition: sequence,
		RecordedAt: time.Date(2026, 8, 29, 12, 0, int(sequence), 0, time.UTC),
	}
}

func projectionReferenceEvent(
	reference im.ConversationRef,
	sequence uint64,
	eventID, eventType string,
) events.StoredEvent {
	return events.StoredEvent{
		EventToAppend: events.EventToAppend{
			SchemaVersion: 1, EventID: eventID, StreamID: reference.ConversationID().String(),
			EventType: eventType, TenantID: reference.TenantID().String(), ActorID: "usr_alice",
			OccurredAt:    time.Date(2026, 8, 29, 12, 0, int(sequence), 0, time.UTC),
			CorrelationID: "corr_" + eventID,
			Payload:       mustReferencePayload(),
		},
		Sequence: sequence, GlobalPosition: sequence,
		RecordedAt: time.Date(2026, 8, 29, 12, 0, int(sequence), 0, time.UTC),
	}
}

func mustReferencePayload() events.Payload {
	payload, err := events.NewReferencedPayload(
		events.OpaquePayloadRef{Storage: "blob", ReferenceID: "ref_1", ByteLength: 10},
		events.SHA256Digest("sha256:0000000000000000000000000000000000000000000000000000000000000000"),
	)
	if err != nil {
		panic(err)
	}
	return payload
}
