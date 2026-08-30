package fake

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/immetadata"
)

func TestProviderProvisionGroupAndMemberEffectsAreIdempotent(t *testing.T) {
	t.Parallel()
	realm := mustRealm(t, "rlm_fake")
	provider, err := New(Options{Realm: realm, Now: func() time.Time { return time.Unix(1700000000, 0).UTC() }})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	alice := mustActor(t, "usr_alice")
	bob := mustActor(t, "usr_bob")
	agent := mustActor(t, "agt_research")
	aliceInfo := mustUserInfo(t, im.SubjectHuman, alice)
	identity, firstReceipt, err := provider.ProvisionUser(ctx, im.ProviderUserProvision{
		Actor: alice, DisplayName: "Alice", ExtInfo: aliceInfo, IdempotencyKey: "user/alice",
	})
	if err != nil {
		t.Fatalf("ProvisionUser(alice): %v", err)
	}
	if identity.SubjectID() != alice.String() || firstReceipt.Status != im.ProviderEffectCommitted {
		t.Fatalf("unexpected provision result: %#v %#v", identity, firstReceipt)
	}
	_, replayReceipt, err := provider.ProvisionUser(ctx, im.ProviderUserProvision{
		Actor: alice, DisplayName: "Alice", ExtInfo: aliceInfo, IdempotencyKey: "user/alice",
	})
	if err != nil || replayReceipt.Status != im.ProviderEffectReplayed {
		t.Fatalf("idempotent provision replay = %#v, %v", replayReceipt, err)
	}
	if _, _, err := provider.ProvisionUser(ctx, im.ProviderUserProvision{
		Actor: alice, DisplayName: "Mallory", ExtInfo: aliceInfo, IdempotencyKey: "user/alice",
	}); !errors.Is(err, im.ErrProviderConflict) {
		t.Fatalf("provision conflict = %v", err)
	}
	if _, _, err := provider.ProvisionUser(ctx, im.ProviderUserProvision{
		Actor: agent, DisplayName: "Research", ExtInfo: mustUserInfo(t, im.SubjectAgent, agent),
		IdempotencyKey: "user/agent",
	}); err != nil {
		t.Fatalf("agent must use normal user provision path: %v", err)
	}
	if _, _, err := provider.ProvisionUser(ctx, im.ProviderUserProvision{
		Actor: bob, DisplayName: "Bob", ExtInfo: mustUserInfo(t, im.SubjectHuman, bob),
		IdempotencyKey: "user/bob",
	}); err != nil {
		t.Fatalf("ProvisionUser(bob): %v", err)
	}

	tenant := mustTenant(t, "ten_acme")
	conversationID := mustConversation(t, "cnv_room")
	conversation := mustConversationRef(t, tenant, conversationID)
	groupInfo := mustGroupInfo(t, conversationID)
	providerConversation, groupReceipt, err := provider.CreateGroup(ctx, im.ProviderGroupCreate{
		Conversation: conversation, ExtInfo: groupInfo,
		MemberActors: []im.ActorID{alice, agent}, IdempotencyKey: "group/room",
	})
	if err != nil {
		t.Fatalf("CreateGroup(): %v", err)
	}
	if providerConversation.SubjectID() != conversationID.String() || groupReceipt.Status != im.ProviderEffectCommitted {
		t.Fatalf("unexpected group result: %#v %#v", providerConversation, groupReceipt)
	}
	_, groupReplay, err := provider.CreateGroup(ctx, im.ProviderGroupCreate{
		Conversation: conversation, ExtInfo: groupInfo,
		MemberActors: []im.ActorID{alice, agent}, IdempotencyKey: "group/room",
	})
	if err != nil || groupReplay.Status != im.ProviderEffectReplayed {
		t.Fatalf("group replay = %#v, %v", groupReplay, err)
	}
	memberReceipt, err := provider.AddMembers(ctx, im.ProviderMemberUpdate{
		Conversation: providerConversation, MemberActors: []im.ActorID{bob}, IdempotencyKey: "members/room-bob",
	})
	if err != nil || memberReceipt.Status != im.ProviderEffectCommitted {
		t.Fatalf("AddMembers() = %#v, %v", memberReceipt, err)
	}
	memberReplay, err := provider.AddMembers(ctx, im.ProviderMemberUpdate{
		Conversation: providerConversation, MemberActors: []im.ActorID{bob}, IdempotencyKey: "members/room-bob",
	})
	if err != nil || memberReplay.Status != im.ProviderEffectReplayed {
		t.Fatalf("AddMembers replay = %#v, %v", memberReplay, err)
	}
	otherRealm := mustRealm(t, "rlm_other")
	otherConversation := mustProviderConversation(t, otherRealm, conversationID.String())
	if _, err := provider.AddMembers(ctx, im.ProviderMemberUpdate{
		Conversation: otherConversation, MemberActors: []im.ActorID{bob}, IdempotencyKey: "members/other",
	}); !errors.Is(err, im.ErrInvalidProviderRequest) {
		t.Fatalf("cross-realm AddMembers() = %v", err)
	}
}

func TestProviderOffboardingEffectsAreIdempotentAndRevokeIdentity(t *testing.T) {
	t.Parallel()
	realm := mustRealm(t, "rlm_offboard")
	provider, err := New(Options{Realm: realm, AllowOutbound: true})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	human := mustActor(t, "usr_offboard")
	agent := mustActor(t, "agt_offboard")
	for _, item := range []struct {
		actor im.ActorID
		type_ im.SubjectType
	}{{human, im.SubjectHuman}, {agent, im.SubjectAgent}} {
		if _, _, err := provider.ProvisionUser(ctx, im.ProviderUserProvision{
			Actor: item.actor, DisplayName: item.actor.String(), ExtInfo: mustUserInfo(t, item.type_, item.actor),
			IdempotencyKey: "user/" + item.actor.String(),
		}); err != nil {
			t.Fatalf("provision %s: %v", item.actor, err)
		}
	}
	conversationID := mustConversation(t, "cnv_offboard")
	conversation, _, err := provider.CreateGroup(ctx, im.ProviderGroupCreate{
		Conversation: mustConversationRef(t, mustTenant(t, "ten_offboard"), conversationID),
		ExtInfo:      mustGroupInfo(t, conversationID), MemberActors: []im.ActorID{human, agent}, IdempotencyKey: "group/offboard",
	})
	if err != nil {
		t.Fatal(err)
	}
	removed, err := provider.RemoveMembers(ctx, im.ProviderMemberUpdate{
		Conversation: conversation, MemberActors: []im.ActorID{agent}, IdempotencyKey: "members/offboard",
	})
	if err != nil || removed.Status != im.ProviderEffectCommitted {
		t.Fatalf("remove member = %#v, %v", removed, err)
	}
	replay, err := provider.RemoveMembers(ctx, im.ProviderMemberUpdate{
		Conversation: conversation, MemberActors: []im.ActorID{agent}, IdempotencyKey: "members/offboard",
	})
	if err != nil || replay.Status != im.ProviderEffectReplayed {
		t.Fatalf("remove member replay = %#v, %v", replay, err)
	}
	revoked, err := provider.RevokeUser(ctx, im.ProviderUserRevoke{Actor: agent, IdempotencyKey: "user-revoke/offboard"})
	if err != nil || revoked.Status != im.ProviderEffectCommitted {
		t.Fatalf("revoke user = %#v, %v", revoked, err)
	}
	revokeReplay, err := provider.RevokeUser(ctx, im.ProviderUserRevoke{Actor: agent, IdempotencyKey: "user-revoke/offboard"})
	if err != nil || revokeReplay.Status != im.ProviderEffectReplayed {
		t.Fatalf("revoke replay = %#v, %v", revokeReplay, err)
	}
	if _, err := provider.AddMembers(ctx, im.ProviderMemberUpdate{
		Conversation: conversation, MemberActors: []im.ActorID{agent}, IdempotencyKey: "members/re-add-revoked",
	}); !errors.Is(err, ErrUserMissing) {
		t.Fatalf("re-add revoked member = %v, want ErrUserMissing", err)
	}
}

func TestProviderInboundCursorPreservesDuplicatesAndOutboundIsExplicit(t *testing.T) {
	t.Parallel()
	realm := mustRealm(t, "rlm_fake")
	provider, err := New(Options{Realm: realm})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	alice := mustActor(t, "usr_alice")
	info := mustUserInfo(t, im.SubjectHuman, alice)
	identity, _, err := provider.ProvisionUser(ctx, im.ProviderUserProvision{
		Actor: alice, DisplayName: "Alice", ExtInfo: info, IdempotencyKey: "user/alice",
	})
	if err != nil {
		t.Fatal(err)
	}
	tenant := mustTenant(t, "ten_acme")
	conversationID := mustConversation(t, "cnv_room")
	conversation := mustConversationRef(t, tenant, conversationID)
	providerConversation, _, err := provider.CreateGroup(ctx, im.ProviderGroupCreate{
		Conversation: conversation, ExtInfo: mustGroupInfo(t, conversationID),
		MemberActors: []im.ActorID{alice}, IdempotencyKey: "group/room",
	})
	if err != nil {
		t.Fatal(err)
	}
	event := im.InboundMessage{
		EventID: "evt_duplicate", Conversation: providerConversation, Sender: identity,
		MessageType: "text", Text: "hello", ProviderCursor: "provider/1",
		ObservedAt: time.Unix(1700000001, 0).UTC(),
	}
	if err := provider.InjectInbound(event); err != nil {
		t.Fatal(err)
	}
	if err := provider.InjectInbound(event); err != nil {
		t.Fatal(err)
	}
	page, err := provider.ReadInbound(ctx, "", 1)
	if err != nil || len(page.Messages) != 1 || !page.HasMore || page.NextCursor != "cursor/1" {
		t.Fatalf("first inbound page = %#v, %v", page, err)
	}
	page, err = provider.ReadInbound(ctx, page.NextCursor, 1)
	if err != nil || len(page.Messages) != 1 || page.Messages[0].EventID != event.EventID || page.HasMore {
		t.Fatalf("resumed inbound page = %#v, %v", page, err)
	}
	if _, err := provider.SendText(ctx, im.ProviderTextMessage{
		Conversation: providerConversation, Sender: alice, ClientMessage: mustMessage(t, "msg_1"),
		Text: "outbound", IdempotencyKey: "send/1",
	}); !errors.Is(err, im.ErrProviderOutboundDisabled) {
		t.Fatalf("disabled SendText() = %v", err)
	}
	if _, err := provider.ReadInbound(ctx, "cursor/99", 1); !errors.Is(err, im.ErrInvalidProviderRequest) {
		t.Fatalf("invalid cursor = %v", err)
	}

	outbound, err := New(Options{Realm: realm, AllowOutbound: true})
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := outbound.ProvisionUser(ctx, im.ProviderUserProvision{
		Actor: alice, DisplayName: "Alice", ExtInfo: info, IdempotencyKey: "user/alice",
	}); err != nil {
		t.Fatal(err)
	}
	outboundConversation, _, err := outbound.CreateGroup(ctx, im.ProviderGroupCreate{
		Conversation: conversation, ExtInfo: mustGroupInfo(t, conversationID),
		MemberActors: []im.ActorID{alice}, IdempotencyKey: "group/room",
	})
	if err != nil {
		t.Fatal(err)
	}
	message := im.ProviderTextMessage{
		Conversation: outboundConversation, Sender: alice, ClientMessage: mustMessage(t, "msg_1"),
		Text: "outbound", IdempotencyKey: "send/1",
	}
	receipt, err := outbound.SendText(ctx, message)
	if err != nil || receipt.Status != im.ProviderEffectCommitted {
		t.Fatalf("SendText() = %#v, %v", receipt, err)
	}
	replay, err := outbound.SendText(ctx, message)
	if err != nil || replay.Status != im.ProviderEffectReplayed {
		t.Fatalf("SendText replay = %#v, %v", replay, err)
	}
	if len(outbound.SentMessages()) != 1 {
		t.Fatalf("replayed send must not duplicate provider effect: %#v", outbound.SentMessages())
	}
	if _, err := outbound.SendText(ctx, im.ProviderTextMessage{
		Conversation: outboundConversation, Sender: alice, ClientMessage: mustMessage(t, "msg_2"),
		Text: "different", IdempotencyKey: "send/1",
	}); !errors.Is(err, im.ErrProviderConflict) {
		t.Fatalf("send conflict = %v", err)
	}
}

func TestProviderOptionalMessageMutationsAreCapabilityBoundAndIdempotent(t *testing.T) {
	t.Parallel()

	realm := mustRealm(t, "rlm_mutation")
	provider, err := New(Options{
		Realm:         realm,
		AllowOutbound: true,
		Now:           func() time.Time { return time.Unix(1700000000, 0).UTC() },
	})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	alice := mustActor(t, "usr_alice")
	if _, _, err := provider.ProvisionUser(ctx, im.ProviderUserProvision{
		Actor: alice, DisplayName: "Alice", ExtInfo: mustUserInfo(t, im.SubjectHuman, alice),
		IdempotencyKey: "user/alice",
	}); err != nil {
		t.Fatalf("provision: %v", err)
	}
	tenant := mustTenant(t, "ten_mutation")
	conversationID := mustConversation(t, "cnv_mutation")
	conversation, err := im.NewConversationRef(tenant, conversationID)
	if err != nil {
		t.Fatal(err)
	}
	providerConversation, _, err := provider.CreateGroup(ctx, im.ProviderGroupCreate{
		Conversation: conversation, ExtInfo: mustGroupInfo(t, conversationID),
		MemberActors: []im.ActorID{alice}, IdempotencyKey: "group/mutation",
	})
	if err != nil {
		t.Fatalf("group: %v", err)
	}
	clientMessage := mustMessage(t, "msg_mutation")
	sent, err := provider.SendText(ctx, im.ProviderTextMessage{
		Conversation: providerConversation, Sender: alice, ClientMessage: clientMessage,
		Text: "before", IdempotencyKey: "send/mutation",
	})
	if err != nil {
		t.Fatalf("send: %v", err)
	}
	edited, err := provider.EditText(ctx, im.ProviderTextEdit{
		Conversation: providerConversation, Sender: alice, ClientMessage: clientMessage,
		Text: "after", IdempotencyKey: "edit/mutation",
	})
	if err != nil || edited.Status != im.ProviderEffectCommitted || edited.ExternalID != sent.ExternalID {
		t.Fatalf("edit = %#v, %v", edited, err)
	}
	editedReplay, err := provider.EditText(ctx, im.ProviderTextEdit{
		Conversation: providerConversation, Sender: alice, ClientMessage: clientMessage,
		Text: "after", IdempotencyKey: "edit/mutation",
	})
	if err != nil || editedReplay.Status != im.ProviderEffectReplayed {
		t.Fatalf("edit replay = %#v, %v", editedReplay, err)
	}
	recalled, err := provider.RecallMessage(ctx, im.ProviderMessageRecall{
		Conversation: providerConversation, Sender: alice, ClientMessage: clientMessage,
		IdempotencyKey: "recall/mutation",
	})
	if err != nil || recalled.Status != im.ProviderEffectCommitted || recalled.ExternalID != sent.ExternalID {
		t.Fatalf("recall = %#v, %v", recalled, err)
	}
	recalledReplay, err := provider.RecallMessage(ctx, im.ProviderMessageRecall{
		Conversation: providerConversation, Sender: alice, ClientMessage: clientMessage,
		IdempotencyKey: "recall/mutation",
	})
	if err != nil || recalledReplay.Status != im.ProviderEffectReplayed {
		t.Fatalf("recall replay = %#v, %v", recalledReplay, err)
	}
	if _, err := provider.EditText(ctx, im.ProviderTextEdit{
		Conversation: providerConversation, Sender: alice, ClientMessage: clientMessage,
		Text: "too late", IdempotencyKey: "edit/after-recall",
	}); !errors.Is(err, ErrMessageMissing) {
		t.Fatalf("edit after recall = %v, want %v", err, ErrMessageMissing)
	}

	disabled, err := New(Options{Realm: realm})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := disabled.EditText(ctx, im.ProviderTextEdit{
		Conversation: providerConversation, Sender: alice, ClientMessage: clientMessage,
		Text: "no", IdempotencyKey: "edit/disabled",
	}); !errors.Is(err, im.ErrProviderCapabilityUnsupported) {
		t.Fatalf("disabled edit = %v, want %v", err, im.ErrProviderCapabilityUnsupported)
	}
}

func TestProviderContextAndCloseBoundaries(t *testing.T) {
	t.Parallel()
	provider, err := New(Options{Realm: mustRealm(t, "rlm_fake")})
	if err != nil {
		t.Fatal(err)
	}
	canceled, cancel := context.WithCancel(context.Background())
	cancel()
	if err := provider.Health(canceled); !errors.Is(err, context.Canceled) {
		t.Fatalf("canceled health = %v", err)
	}
	provider.Close()
	if err := provider.Health(context.Background()); !errors.Is(err, ErrClosed) {
		t.Fatalf("closed health = %v", err)
	}
}

func mustRealm(t *testing.T, value string) im.ProviderRealmID {
	t.Helper()
	parsed, err := im.ParseProviderRealmID(value)
	if err != nil {
		t.Fatalf("ParseProviderRealmID(%q): %v", value, err)
	}
	return parsed
}

func mustActor(t *testing.T, value string) im.ActorID {
	t.Helper()
	parsed, err := im.ParseActorID(value)
	if err != nil {
		t.Fatalf("ParseActorID(%q): %v", value, err)
	}
	return parsed
}

func mustTenant(t *testing.T, value string) im.TenantID {
	t.Helper()
	parsed, err := im.ParseTenantID(value)
	if err != nil {
		t.Fatalf("ParseTenantID(%q): %v", value, err)
	}
	return parsed
}

func mustConversation(t *testing.T, value string) im.ConversationID {
	t.Helper()
	parsed, err := im.ParseConversationID(value)
	if err != nil {
		t.Fatalf("ParseConversationID(%q): %v", value, err)
	}
	return parsed
}

func mustMessage(t *testing.T, value string) im.MessageID {
	t.Helper()
	parsed, err := im.ParseMessageID(value)
	if err != nil {
		t.Fatalf("ParseMessageID(%q): %v", value, err)
	}
	return parsed
}

func mustConversationRef(t *testing.T, tenant im.TenantID, conversation im.ConversationID) im.ConversationRef {
	t.Helper()
	parsed, err := im.NewConversationRef(tenant, conversation)
	if err != nil {
		t.Fatalf("NewConversationRef(): %v", err)
	}
	return parsed
}

func mustProviderConversation(t *testing.T, realm im.ProviderRealmID, value string) im.ProviderConversationRef {
	t.Helper()
	parsed, err := im.NewProviderConversationRef(im.IdentityProviderRongCloud, realm, value)
	if err != nil {
		t.Fatalf("NewProviderConversationRef(): %v", err)
	}
	return parsed
}

func mustUserInfo(t *testing.T, subjectType im.SubjectType, actor im.ActorID) string {
	t.Helper()
	var definition im.AgentDefinitionID
	var version im.AgentVersion
	if subjectType == im.SubjectAgent {
		var err error
		definition, err = im.ParseAgentDefinitionID("agd_research")
		if err != nil {
			t.Fatal(err)
		}
		version, err = im.ParseAgentVersion("1.0.0")
		if err != nil {
			t.Fatal(err)
		}
	}
	projection, err := immetadata.NewUserProjection(subjectType, actor, definition, version)
	if err != nil {
		t.Fatal(err)
	}
	encoded, err := immetadata.EncodeUserProjection(projection)
	if err != nil {
		t.Fatal(err)
	}
	return encoded
}

func mustGroupInfo(t *testing.T, conversation im.ConversationID) string {
	t.Helper()
	projection, err := immetadata.NewConversationProjection(
		im.ConversationGroup, conversation, im.ConversationID{}, im.MessageID{}, im.InvocationID{},
	)
	if err != nil {
		t.Fatal(err)
	}
	encoded, err := immetadata.EncodeConversationProjection(projection)
	if err != nil {
		t.Fatal(err)
	}
	return encoded
}
