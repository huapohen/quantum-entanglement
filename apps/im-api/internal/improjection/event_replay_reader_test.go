package improjection

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/events"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
	store "github.com/huapohen/quantum-entanglement/apps/im-api/internal/imstore"
)

func TestEventReplayMessageReaderPaginatesStableDurableStreamAndRejectsDrift(t *testing.T) {
	t.Parallel()
	reference := replayMessageReference(t)
	now := time.Date(2026, 8, 30, 12, 0, 0, 0, time.UTC)
	eventStore, err := events.NewVolatileMemoryStore(
		"message-replay-reader", func(context.Context) time.Time { return now },
	)
	if err != nil {
		t.Fatal(err)
	}
	createdOne := projectionEvent(
		t, reference, 1, "evt_created_1", "message.created", "usr_alice",
		`{"clientMessageId":"msg_client_1","conversationId":"cnv_room","messageId":"msg_1","messageType":"text","text":"one"}`,
	)
	createdTwo := projectionEvent(
		t, reference, 2, "evt_created_2", "message.created", "usr_alice",
		`{"clientMessageId":"msg_client_2","conversationId":"cnv_room","messageId":"msg_2","messageType":"text","text":"two"}`,
	)
	if _, err := eventStore.AppendBatch(context.Background(), events.AppendBatch{
		TenantID: reference.TenantID().String(), StreamID: reference.ConversationID().String(),
		Events: []events.EventToAppend{createdOne.EventToAppend, createdTwo.EventToAppend},
	}); err != nil {
		t.Fatal(err)
	}
	reader, err := NewEventReplayMessageReader(eventStore)
	if err != nil {
		t.Fatal(err)
	}
	query := store.MessageReadPageQuery{
		Conversation: reference, Limit: 1, ConversationRevision: 7, AccessRevision: 9,
	}
	first, err := reader.ReadPage(context.Background(), query)
	if err != nil || len(first.Messages) != 1 || first.Messages[0].Text() != "one" ||
		!first.HasMore || first.NextCursor == "" || first.ProjectionRevision != 2 {
		t.Fatalf("first page=%#v error=%v", first, err)
	}
	query.AfterCursor = first.NextCursor
	second, err := reader.ReadPage(context.Background(), query)
	if err != nil || len(second.Messages) != 1 || second.Messages[0].Text() != "two" ||
		second.HasMore || second.NextCursor != "" || second.ProjectionRevision != 2 {
		t.Fatalf("second page=%#v error=%v", second, err)
	}
	createdThree := projectionEvent(
		t, reference, 3, "evt_created_3", "message.created", "usr_alice",
		`{"clientMessageId":"msg_client_3","conversationId":"cnv_room","messageId":"msg_3","messageType":"text","text":"three"}`,
	)
	if _, err := eventStore.AppendBatch(context.Background(), events.AppendBatch{
		TenantID: reference.TenantID().String(), StreamID: reference.ConversationID().String(),
		ExpectedVersion: 2, Events: []events.EventToAppend{createdThree.EventToAppend},
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := reader.ReadPage(context.Background(), query); !errors.Is(err, store.ErrRevisionConflict) {
		t.Fatalf("stale cursor error=%v, want %v", err, store.ErrRevisionConflict)
	}
}

func TestEventReplayMessageReaderRejectsMalformedCursorAndSupportsEmptyStream(t *testing.T) {
	t.Parallel()
	reference := replayMessageReference(t)
	eventStore, err := events.NewVolatileMemoryStore(
		"empty-message-replay",
		func(context.Context) time.Time {
			return time.Date(2026, 8, 30, 12, 0, 0, 0, time.UTC)
		},
	)
	if err != nil {
		t.Fatal(err)
	}
	reader, err := NewEventReplayMessageReader(eventStore)
	if err != nil {
		t.Fatal(err)
	}
	query := store.MessageReadPageQuery{
		Conversation: reference, Limit: 20, ConversationRevision: 1, AccessRevision: 1,
	}
	page, err := reader.ReadPage(context.Background(), query)
	if err != nil || len(page.Messages) != 0 || page.ProjectionRevision != 0 || page.HasMore {
		t.Fatalf("empty page=%#v error=%v", page, err)
	}
	query.AfterCursor = "not-a-cursor"
	if _, err := reader.ReadPage(context.Background(), query); !errors.Is(err, store.ErrInvalidRequest) {
		t.Fatalf("invalid cursor error=%v", err)
	}
	if _, err := NewEventReplayMessageReader(nil); !errors.Is(err, store.ErrInvalidRequest) {
		t.Fatalf("nil store error=%v", err)
	}
}

func replayMessageReference(t *testing.T) im.ConversationRef {
	t.Helper()
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
	return reference
}
