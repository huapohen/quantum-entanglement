package improjection

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/imstore"
)

type shadowPageReader struct {
	page  imstore.MessageReadPage
	calls int
}

func (reader *shadowPageReader) ReadPage(_ context.Context, _ imstore.MessageReadPageQuery) (imstore.MessageReadPage, error) {
	reader.calls++
	return reader.page, nil
}

func TestCompareMessageReadersAcceptsEqualRowsAndIgnoresProjectionRevision(t *testing.T) {
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
	actorID, err := im.ParseActorID("usr_alice")
	if err != nil {
		t.Fatal(err)
	}
	actor, err := im.NewActorRef(tenant, actorID)
	if err != nil {
		t.Fatal(err)
	}
	messageRef, err := im.NewMessageRef(reference, mustShadowMessageID(t, "msg_1"))
	if err != nil {
		t.Fatal(err)
	}
	message, err := im.NewMessageSnapshot(messageRef, actor, mustShadowMessageID(t, "msg_client_1"),
		im.MessageTypeText, im.MessageStatusActive, "hello", "", time.Date(2026, 8, 30, 12, 0, 0, 0, time.UTC), 1)
	if err != nil {
		t.Fatal(err)
	}
	page := imstore.MessageReadPage{Conversation: reference, Messages: []im.MessageSnapshot{message},
		ConversationRevision: 7, ProjectionRevision: 11}
	replay := &shadowPageReader{page: page}
	materialized := &shadowPageReader{page: func() imstore.MessageReadPage {
		copy := page
		copy.ProjectionRevision = 99
		return copy
	}()}
	result, err := CompareMessageReaders(context.Background(), replay, materialized, imstore.MessageReadPageQuery{
		Conversation: reference, Limit: 10, ConversationRevision: 7, AccessRevision: 9,
	})
	if err != nil || result.Pages != 1 || result.Messages != 1 || replay.calls != 1 || materialized.calls != 1 {
		t.Fatalf("comparison=%#v error=%v calls=(%d,%d)", result, err, replay.calls, materialized.calls)
	}
}

func TestCompareMessageReadersRejectsMismatchAndNonEmptyCursor(t *testing.T) {
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
	query := imstore.MessageReadPageQuery{Conversation: reference, Limit: 10, ConversationRevision: 1, AccessRevision: 1}
	page := imstore.MessageReadPage{Conversation: reference, ConversationRevision: 1}
	_, err = CompareMessageReaders(context.Background(), &shadowPageReader{page: page}, &shadowPageReader{page: page}, func() imstore.MessageReadPageQuery {
		copy := query
		copy.AfterCursor = "opaque"
		return copy
	}())
	if !errors.Is(err, ErrShadowInvalid) {
		t.Fatalf("non-empty cursor error=%v", err)
	}
	wrong := page
	wrong.ConversationRevision = 2
	_, err = CompareMessageReaders(context.Background(), &shadowPageReader{page: page}, &shadowPageReader{page: wrong}, query)
	if !errors.Is(err, ErrShadowMismatch) {
		t.Fatalf("metadata mismatch error=%v", err)
	}
}

func mustShadowMessageID(t *testing.T, value string) im.MessageID {
	t.Helper()
	parsed, err := im.ParseMessageID(value)
	if err != nil {
		t.Fatal(err)
	}
	return parsed
}
