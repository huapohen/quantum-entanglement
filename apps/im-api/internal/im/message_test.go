package im

import (
	"errors"
	"strings"
	"testing"
	"time"
)

func TestMessageSnapshotKeepsPlatformAndClientIdentitySeparate(t *testing.T) {
	t.Parallel()
	tenant := mustMessageTenantID(t, "ten_message")
	conversationID := mustConversationID(t, "cnv_message_room")
	conversation, err := NewConversationRef(tenant, conversationID)
	if err != nil {
		t.Fatal(err)
	}
	messageID := mustMessageID(t, "msg_platform_1")
	clientID := mustMessageID(t, "msg_client_1")
	senderID := mustMessageActorID(t, "usr_message")
	sender, err := NewActorRef(tenant, senderID)
	if err != nil {
		t.Fatal(err)
	}
	reference, err := NewMessageRef(conversation, messageID)
	if err != nil {
		t.Fatal(err)
	}
	createdAt := time.Date(2026, 8, 29, 1, 2, 3, 4, time.UTC)
	snapshot, err := NewMessageSnapshot(
		reference, sender, clientID, MessageTypeText, MessageStatusActive,
		"hello\nworld", `{"messageType":"text"}`, createdAt, 1,
	)
	if err != nil {
		t.Fatal(err)
	}
	if snapshot.Ref() != reference || snapshot.ClientMessageID() != clientID ||
		snapshot.Sender() != sender || snapshot.Text() != "hello\nworld" ||
		snapshot.CreatedAt() != createdAt || snapshot.IsZero() {
		t.Fatalf("unexpected message snapshot: %#v", snapshot)
	}
}

func TestMessageSnapshotRejectsCrossTenantAndMalformedContent(t *testing.T) {
	t.Parallel()
	tenant := mustMessageTenantID(t, "ten_message")
	otherTenant := mustMessageTenantID(t, "ten_other")
	conversation, err := NewConversationRef(tenant, mustConversationID(t, "cnv_message_room"))
	if err != nil {
		t.Fatal(err)
	}
	messageRef, err := NewMessageRef(conversation, mustMessageID(t, "msg_platform_1"))
	if err != nil {
		t.Fatal(err)
	}
	otherSender, err := NewActorRef(otherTenant, mustMessageActorID(t, "usr_message"))
	if err != nil {
		t.Fatal(err)
	}
	createdAt := time.Date(2026, 8, 29, 1, 2, 3, 4, time.UTC)
	for _, test := range []struct {
		name   string
		sender ActorRef
		kind   MessageType
		state  MessageStatus
		text   string
		extra  string
		at     time.Time
	}{
		{name: "cross tenant", sender: otherSender, kind: MessageTypeText, state: MessageStatusActive, text: "hello", at: createdAt},
		{name: "unknown type", sender: mustMessageActorRef(t, tenant, "usr_message"), kind: MessageType("markdown"), state: MessageStatusActive, text: "hello", at: createdAt},
		{name: "empty active text", sender: mustMessageActorRef(t, tenant, "usr_message"), kind: MessageTypeText, state: MessageStatusActive, at: createdAt},
		{name: "control text", sender: mustMessageActorRef(t, tenant, "usr_message"), kind: MessageTypeText, state: MessageStatusActive, text: "hello\x00world", at: createdAt},
		{name: "non NFC text", sender: mustMessageActorRef(t, tenant, "usr_message"), kind: MessageTypeText, state: MessageStatusActive, text: "e\u0301", at: createdAt},
		{name: "whitespace ext info", sender: mustMessageActorRef(t, tenant, "usr_message"), kind: MessageTypeText, state: MessageStatusActive, text: "hello", extra: " {\"a\":1}", at: createdAt},
		{name: "non UTC", sender: mustMessageActorRef(t, tenant, "usr_message"), kind: MessageTypeText, state: MessageStatusActive, text: "hello", at: createdAt.In(time.FixedZone("test", 3600))},
		{name: "zero revision", sender: mustMessageActorRef(t, tenant, "usr_message"), kind: MessageTypeText, state: MessageStatusActive, text: "hello", at: createdAt},
	} {
		test := test
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()
			revision := uint64(1)
			if test.name == "zero revision" {
				revision = 0
			}
			value, err := NewMessageSnapshot(
				messageRef, test.sender, mustMessageID(t, "msg_client_1"), test.kind, test.state,
				test.text, test.extra, test.at, revision,
			)
			if !errors.Is(err, ErrInvalidMessage) || !value.IsZero() {
				t.Fatalf("NewMessageSnapshot() = (%#v, %v), want zero and ErrInvalidMessage", value, err)
			}
		})
	}
	if value, err := NewMessageSnapshot(
		messageRef, mustMessageActorRef(t, tenant, "usr_message"), mustMessageID(t, "msg_client_1"),
		MessageTypeText, MessageStatusActive, strings.Repeat("a", MessageTextMaxBytes+1), "", createdAt, 1,
	); !errors.Is(err, ErrInvalidMessage) || !value.IsZero() {
		t.Fatalf("oversize message = (%#v, %v), want invalid", value, err)
	}
}

func mustMessageTenantID(t *testing.T, value string) TenantID {
	t.Helper()
	identifier, err := ParseTenantID(value)
	if err != nil {
		t.Fatal(err)
	}
	return identifier
}

func mustMessageActorID(t *testing.T, value string) ActorID {
	t.Helper()
	identifier, err := ParseActorID(value)
	if err != nil {
		t.Fatal(err)
	}
	return identifier
}

func mustMessageActorRef(t *testing.T, tenant TenantID, actor string) ActorRef {
	t.Helper()
	reference, err := NewActorRef(tenant, mustMessageActorID(t, actor))
	if err != nil {
		t.Fatal(err)
	}
	return reference
}
