package im

import (
	"errors"
	"strings"
	"testing"
	"time"
)

func TestProviderProfileValidatesCapabilitiesAndCopiesInput(t *testing.T) {
	t.Parallel()
	realm := mustProviderRealm(t, "rlm_test")
	caps := []ProviderCapability{ProviderCapabilityHealth, ProviderCapabilityInboundRead}
	profile, err := NewProviderProfile(
		IdentityProviderRongCloud, realm, caps, 1024, 2048, 512,
	)
	if err != nil {
		t.Fatalf("NewProviderProfile() error = %v", err)
	}
	caps[0] = ProviderCapabilityTextSend
	if !profile.Supports(ProviderCapabilityHealth) || profile.Supports(ProviderCapabilityTextSend) {
		t.Fatalf("profile capabilities were not copied: %#v", profile.Capabilities)
	}
	if profile.MetadataSchemaVersion != ProviderMetadataSchemaVersion || profile.Realm != realm {
		t.Fatalf("unexpected profile: %#v", profile)
	}
	for _, test := range []struct {
		name string
		call func() (ProviderProfile, error)
	}{
		{name: "clerk is not an IM provider", call: func() (ProviderProfile, error) {
			return NewProviderProfile(IdentityProviderClerk, realm, caps, 1, 1, 1)
		}},
		{name: "duplicate capability", call: func() (ProviderProfile, error) {
			return NewProviderProfile(IdentityProviderRongCloud, realm,
				[]ProviderCapability{ProviderCapabilityHealth, ProviderCapabilityHealth}, 1, 1, 1)
		}},
		{name: "unknown capability", call: func() (ProviderProfile, error) {
			return NewProviderProfile(IdentityProviderRongCloud, realm,
				[]ProviderCapability{"typing"}, 1, 1, 1)
		}},
		{name: "oversized limits", call: func() (ProviderProfile, error) {
			return NewProviderProfile(IdentityProviderRongCloud, realm, caps,
				ProviderMaxTextBytes+1, 1, 1)
		}},
	} {
		test := test
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()
			profile, err := test.call()
			if !errors.Is(err, ErrInvalidProviderRequest) || !profile.IsZero() {
				t.Fatalf("profile = %#v, error = %v; want zero and ErrInvalidProviderRequest", profile, err)
			}
		})
	}
}

func TestProviderRequestValidationBindsRealmAndRejectsUnsafeValues(t *testing.T) {
	t.Parallel()
	realm := mustProviderRealm(t, "rlm_test")
	otherRealm := mustProviderRealm(t, "rlm_other")
	profile, err := NewProviderProfile(
		IdentityProviderRongCloud, realm,
		[]ProviderCapability{ProviderCapabilityHealth, ProviderCapabilityMemberWrite, ProviderCapabilityTextSend}, 64, 64, 128,
	)
	if err != nil {
		t.Fatal(err)
	}
	conversation := mustProviderConversation(t, realm, "cnv_room")
	otherConversation := mustProviderConversation(t, otherRealm, "cnv_room")
	actor := mustActor(t, "usr_alice")
	messageID := mustMessage(t, "msg_1")

	if err := (ProviderMemberUpdate{
		Conversation: conversation, MemberActors: []ActorID{actor}, IdempotencyKey: "members/1",
	}).ValidateForProfile(profile); err != nil {
		t.Fatalf("valid member update rejected: %v", err)
	}
	if err := (ProviderMemberUpdate{
		Conversation: otherConversation, MemberActors: []ActorID{actor}, IdempotencyKey: "members/2",
	}).ValidateForProfile(profile); !errors.Is(err, ErrInvalidProviderRequest) {
		t.Fatalf("cross-realm member update error = %v", err)
	}
	withoutMemberWrite, err := NewProviderProfile(
		IdentityProviderRongCloud, realm,
		[]ProviderCapability{ProviderCapabilityHealth, ProviderCapabilityTextSend}, 64, 64, 128,
	)
	if err != nil {
		t.Fatal(err)
	}
	if err := (ProviderMemberUpdate{
		Conversation: conversation, MemberActors: []ActorID{actor}, IdempotencyKey: "members/no-capability",
	}).ValidateForProfile(withoutMemberWrite); !errors.Is(err, ErrInvalidProviderRequest) {
		t.Fatalf("member update without reviewed capability = %v, want ErrInvalidProviderRequest", err)
	}
	if err := (ProviderTextMessage{
		Conversation: conversation, Sender: actor, ClientMessage: messageID,
		Text: "hello", IdempotencyKey: "send/1",
	}).Validate(profile); err != nil {
		t.Fatalf("valid text request rejected: %v", err)
	}
	if err := (ProviderTextMessage{
		Conversation: conversation, Sender: actor, ClientMessage: messageID,
		Text: strings.Repeat("x", 65), IdempotencyKey: "send/2",
	}).Validate(profile); !errors.Is(err, ErrInvalidProviderRequest) {
		t.Fatalf("oversized text error = %v", err)
	}
	if err := (ProviderTextMessage{
		Conversation: conversation, Sender: actor, ClientMessage: messageID,
		Text: "hello", IdempotencyKey: "../unsafe",
	}).Validate(profile); !errors.Is(err, ErrInvalidProviderRequest) {
		t.Fatalf("unsafe idempotency key error = %v", err)
	}
}

func TestProviderReceiptAndInboundValidationRequireUTCAndProviderRealm(t *testing.T) {
	t.Parallel()
	realm := mustProviderRealm(t, "rlm_test")
	profile, err := NewProviderProfile(
		IdentityProviderRongCloud, realm, []ProviderCapability{ProviderCapabilityInboundRead}, 64, 64, 128,
	)
	if err != nil {
		t.Fatal(err)
	}
	conversation := mustProviderConversation(t, realm, "cnv_room")
	sender, err := NewExternalIdentityRef(IdentityProviderRongCloud, realm, "usr_alice")
	if err != nil {
		t.Fatal(err)
	}
	valid := InboundMessage{
		EventID: "evt_1", Conversation: conversation, Sender: sender,
		MessageType: "text", Text: "hello", ObservedAt: time.Unix(10, 0).UTC(),
	}
	if err := valid.Validate(profile); err != nil {
		t.Fatalf("valid inbound message rejected: %v", err)
	}
	if err := (ProviderEffectReceipt{
		OperationKey: "op/1", ExternalID: "ext_1", Status: ProviderEffectCommitted,
		ObservedAt: time.Unix(10, 0).UTC(),
	}).Validate(); err != nil {
		t.Fatalf("valid receipt rejected: %v", err)
	}
	if err := (ProviderEffectReceipt{
		OperationKey: "op/1", ExternalID: "ext_1", Status: ProviderEffectCommitted,
		ObservedAt: time.Unix(10, 0).In(time.FixedZone("UTC", 0)),
	}).Validate(); !errors.Is(err, ErrInvalidProviderRequest) {
		t.Fatalf("non-canonical UTC receipt error = %v", err)
	}
	otherRealm := mustProviderRealm(t, "rlm_other")
	otherSender, err := NewExternalIdentityRef(IdentityProviderRongCloud, otherRealm, "usr_alice")
	if err != nil {
		t.Fatal(err)
	}
	valid.Sender = otherSender
	if err := valid.Validate(profile); !errors.Is(err, ErrInvalidProviderRequest) {
		t.Fatalf("cross-realm inbound error = %v", err)
	}
	valid.Sender = sender
	valid.MessageType = "image"
	if err := valid.Validate(profile); !errors.Is(err, ErrInvalidProviderRequest) {
		t.Fatalf("non-text inbound error = %v", err)
	}
}

func mustProviderRealm(t *testing.T, value string) ProviderRealmID {
	t.Helper()
	parsed, err := ParseProviderRealmID(value)
	if err != nil {
		t.Fatalf("ParseProviderRealmID(%q): %v", value, err)
	}
	return parsed
}

func mustActor(t *testing.T, value string) ActorID {
	t.Helper()
	parsed, err := ParseActorID(value)
	if err != nil {
		t.Fatalf("ParseActorID(%q): %v", value, err)
	}
	return parsed
}

func mustMessage(t *testing.T, value string) MessageID {
	t.Helper()
	parsed, err := ParseMessageID(value)
	if err != nil {
		t.Fatalf("ParseMessageID(%q): %v", value, err)
	}
	return parsed
}

func mustProviderConversation(t *testing.T, realm ProviderRealmID, value string) ProviderConversationRef {
	t.Helper()
	parsed, err := NewProviderConversationRef(IdentityProviderRongCloud, realm, value)
	if err != nil {
		t.Fatalf("NewProviderConversationRef(%q): %v", value, err)
	}
	return parsed
}
