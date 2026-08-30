package localdemo

import (
	"context"
	"errors"
	"strings"
	"testing"
)

func TestBasicConversationAndMessageLifecycle(t *testing.T) {
	t.Parallel()
	service, err := New()
	if err != nil {
		t.Fatal(err)
	}
	snapshot := service.Snapshot()
	page, err := service.ListConversations(context.Background(), LocalBearerToken, "", 20)
	if err != nil {
		t.Fatal(err)
	}
	if len(page.Conversations) != 1 || page.Conversations[0].ID != snapshot.ParentConversationID ||
		page.Conversations[0].ProviderStatus != "committed" {
		t.Fatalf("initial conversation page = %#v", page)
	}

	created, err := service.CreateConversation(context.Background(), LocalBearerToken, CreateConversationInput{
		Type: imConversationGroup, Name: "项目协同", MemberActorIDs: []string{snapshot.AgentActorID},
		IdempotencyKey: "create/basic/group/1",
	})
	if err != nil {
		t.Fatal(err)
	}
	if created.Replayed || created.Conversation.Type != imConversationGroup ||
		created.Conversation.ProviderStatus != "committed" || len(created.Conversation.MemberActorIDs) != 2 {
		t.Fatalf("created group = %#v", created)
	}
	replay, err := service.CreateConversation(context.Background(), LocalBearerToken, CreateConversationInput{
		Type: imConversationGroup, Name: "项目协同", MemberActorIDs: []string{snapshot.AgentActorID},
		IdempotencyKey: "create/basic/group/1",
	})
	if err != nil || !replay.Replayed || replay.Conversation.ID != created.Conversation.ID {
		t.Fatalf("group replay = %#v, %v", replay, err)
	}
	if _, err := service.CreateConversation(context.Background(), LocalBearerToken, CreateConversationInput{
		Type: imConversationGroup, Name: "改名冲突", MemberActorIDs: []string{snapshot.AgentActorID},
		IdempotencyKey: "create/basic/group/1",
	}); !errors.Is(err, ErrConflict) {
		t.Fatalf("group idempotency drift = %v", err)
	}

	sent, err := service.SendText(context.Background(), LocalBearerToken, created.Conversation.ID, SendTextInput{
		ClientMessageID: "msg_client_basic_1", Text: "第一条消息", ExtInfo: `{"messageType":"text"}`,
	})
	if err != nil {
		t.Fatal(err)
	}
	if sent.Replayed || sent.Message.ConversationID != created.Conversation.ID ||
		sent.Message.ProviderStatus != "committed" || sent.Message.ProviderMessageID == "" {
		t.Fatalf("sent group message = %#v", sent)
	}
	sentReplay, err := service.SendText(context.Background(), LocalBearerToken, created.Conversation.ID, SendTextInput{
		ClientMessageID: "msg_client_basic_1", Text: "第一条消息", ExtInfo: `{"messageType":"text"}`,
	})
	if err != nil || !sentReplay.Replayed || sentReplay.Message.ID != sent.Message.ID {
		t.Fatalf("message replay = %#v, %v", sentReplay, err)
	}
	if _, err := service.SendText(context.Background(), LocalBearerToken, created.Conversation.ID, SendTextInput{
		ClientMessageID: "msg_client_basic_1", Text: "正文漂移", ExtInfo: `{"messageType":"text"}`,
	}); !errors.Is(err, ErrConflict) {
		t.Fatalf("message idempotency drift = %v", err)
	}
	edited, err := service.EditText(context.Background(), LocalBearerToken, created.Conversation.ID, sent.Message.ID, EditTextInput{
		Text: "编辑后的消息",
	})
	if err != nil || edited.Replayed || edited.Message.Status != "edited" || edited.Message.Text != "编辑后的消息" ||
		edited.Message.ProviderStatus != "committed" {
		t.Fatalf("edited message = %#v, %v", edited, err)
	}
	editedReplay, err := service.EditText(context.Background(), LocalBearerToken, created.Conversation.ID, sent.Message.ID, EditTextInput{
		Text: "编辑后的消息",
	})
	if err != nil || !editedReplay.Replayed || editedReplay.Message.Status != "edited" {
		t.Fatalf("edited replay = %#v, %v", editedReplay, err)
	}
	recalled, err := service.RecallMessage(context.Background(), LocalBearerToken, created.Conversation.ID, sent.Message.ID, RecallMessageInput{})
	if err != nil || recalled.Replayed || recalled.Message.Status != "recalled" || recalled.Message.Text != "" ||
		recalled.Message.ProviderStatus != "committed" {
		t.Fatalf("recalled message = %#v, %v", recalled, err)
	}
	recalledReplay, err := service.RecallMessage(context.Background(), LocalBearerToken, created.Conversation.ID, sent.Message.ID, RecallMessageInput{})
	if err != nil || !recalledReplay.Replayed || recalledReplay.Message.Status != "recalled" {
		t.Fatalf("recalled replay = %#v, %v", recalledReplay, err)
	}
	if _, err := service.EditText(context.Background(), LocalBearerToken, created.Conversation.ID, sent.Message.ID, EditTextInput{
		Text: "撤回后禁止编辑",
	}); !errors.Is(err, ErrConflict) {
		t.Fatalf("edit after recall = %v, want %v", err, ErrConflict)
	}

	messages, err := service.ListMessages(context.Background(), LocalBearerToken, created.Conversation.ID, "", 1)
	if err != nil || len(messages.Messages) != 1 || messages.Messages[0].Status != "recalled" || messages.Messages[0].Text != "" || messages.HasMore {
		t.Fatalf("message page = %#v, %v", messages, err)
	}
	if _, err := service.ListMessages(context.Background(), LocalBearerToken, created.Conversation.ID, messages.NextCursor+"x", 1); !errors.Is(err, ErrInvalidCursor) {
		t.Fatalf("tampered cursor = %v", err)
	}
}

func TestBasicDirectConversationIsExplicitlyLocalOnly(t *testing.T) {
	t.Parallel()
	service, err := New()
	if err != nil {
		t.Fatal(err)
	}
	direct, err := service.CreateConversation(context.Background(), LocalBearerToken, CreateConversationInput{
		Type: imConversationDirect, Name: "一对一", MemberActorIDs: []string{service.Snapshot().AgentActorID},
		IdempotencyKey: "create/basic/direct/1",
	})
	if err != nil {
		t.Fatal(err)
	}
	if direct.Conversation.ProviderStatus != "local-only" || len(direct.Conversation.MemberActorIDs) != 2 {
		t.Fatalf("direct conversation = %#v", direct)
	}
	sent, err := service.SendText(context.Background(), LocalBearerToken, direct.Conversation.ID, SendTextInput{
		ClientMessageID: "msg_client_direct_1", Text: "仅平台内的本地消息",
	})
	if err != nil || sent.Message.ProviderStatus != "local-only" || sent.Message.ProviderMessageID != "" {
		t.Fatalf("direct message = %#v, %v", sent, err)
	}
}

func TestBasicConversationRejectsUnknownMemberAndInvalidContent(t *testing.T) {
	t.Parallel()
	service, err := New()
	if err != nil {
		t.Fatal(err)
	}
	for name, input := range map[string]CreateConversationInput{
		"unknown member":     {Type: imConversationGroup, Name: "群", MemberActorIDs: []string{"usr_unknown"}, IdempotencyKey: "create/basic/unknown"},
		"unsupported type":   {Type: "channel", Name: "频道", IdempotencyKey: "create/basic/channel"},
		"non canonical name": {Type: imConversationGroup, Name: " e\u0301", IdempotencyKey: "create/basic/name"},
	} {
		input := input
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			if _, err := service.CreateConversation(context.Background(), LocalBearerToken, input); !errors.Is(err, ErrInvalidInput) {
				t.Fatalf("CreateConversation() error = %v", err)
			}
		})
	}
	if _, err := service.SendText(context.Background(), LocalBearerToken, service.Snapshot().ParentConversationID, SendTextInput{
		ClientMessageID: "msg_client_invalid", Text: "hello", ExtInfo: strings.Repeat("a", 100),
	}); !errors.Is(err, ErrInvalidInput) {
		t.Fatalf("invalid JSON ext_info = %v", err)
	}
}

const (
	imConversationDirect = "direct"
	imConversationGroup  = "group"
)
