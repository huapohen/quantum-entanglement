package im

import (
	"errors"
	"strings"
	"testing"
)

func TestConversationIdentityFreezesOrdinaryAndAgentThreadTopology(t *testing.T) {
	t.Parallel()

	tenantID := mustTenantID(t, "ten_acme")
	workspaceID := mustWorkspaceID(t, "wsp_product")
	parentID := mustConversationID(t, "cnv_parent")
	rootMessageID := mustMessageID(t, "msg_root")
	invocationID := mustInvocationID(t, "inv_finance")

	for _, test := range []struct {
		name             string
		workspaceID      *WorkspaceID
		conversationID   ConversationID
		conversationType ConversationType
		parentID         ConversationID
		rootMessageID    MessageID
		invocationID     InvocationID
		wantWorkspace    WorkspaceID
		wantWorkspaceSet bool
	}{
		{
			name:             "direct conversation may be tenant scoped",
			conversationID:   mustConversationID(t, "cnv_direct_alice_bob"),
			conversationType: ConversationDirect,
		},
		{
			name:             "group conversation may be workspace scoped",
			workspaceID:      &workspaceID,
			conversationID:   mustConversationID(t, "cnv_product_group"),
			conversationType: ConversationGroup,
			wantWorkspace:    workspaceID,
			wantWorkspaceSet: true,
		},
		{
			name:             "agent thread binds parent root message and invocation",
			workspaceID:      &workspaceID,
			conversationID:   mustConversationID(t, "cnv_agent_thread"),
			conversationType: ConversationAgentThread,
			parentID:         parentID,
			rootMessageID:    rootMessageID,
			invocationID:     invocationID,
			wantWorkspace:    workspaceID,
			wantWorkspaceSet: true,
		},
	} {
		test := test
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()
			identity, err := NewConversationIdentity(
				tenantID,
				test.workspaceID,
				test.conversationID,
				test.conversationType,
				test.parentID,
				test.rootMessageID,
				test.invocationID,
				11,
			)
			if err != nil {
				t.Fatalf("NewConversationIdentity() error = %v", err)
			}
			workspace, workspaceSet := identity.WorkspaceID()
			if identity.TenantID() != tenantID || identity.ConversationID() != test.conversationID ||
				identity.ConversationType() != test.conversationType ||
				identity.ParentConversationID() != test.parentID ||
				identity.RootMessageID() != test.rootMessageID ||
				identity.AgentInvocationID() != test.invocationID || identity.Revision() != 11 ||
				workspace != test.wantWorkspace || workspaceSet != test.wantWorkspaceSet ||
				identity.IsZero() {
				t.Fatalf("unexpected conversation identity: %#v", identity)
			}
		})
	}
}

func TestConversationIdentityRejectsIncompleteOrForgedTopology(t *testing.T) {
	t.Parallel()

	tenantID := mustTenantID(t, "ten_acme")
	workspaceID := mustWorkspaceID(t, "wsp_product")
	conversationID := mustConversationID(t, "cnv_agent_thread")
	parentID := mustConversationID(t, "cnv_parent")
	rootMessageID := mustMessageID(t, "msg_root")
	invocationID := mustInvocationID(t, "inv_finance")

	for _, test := range []struct {
		name             string
		tenantID         TenantID
		workspaceID      *WorkspaceID
		conversationID   ConversationID
		conversationType ConversationType
		parentID         ConversationID
		rootMessageID    MessageID
		invocationID     InvocationID
		revision         uint64
	}{
		{name: "missing tenant", conversationID: conversationID, conversationType: ConversationGroup, revision: 1},
		{name: "missing conversation", tenantID: tenantID, conversationType: ConversationGroup, revision: 1},
		{name: "zero workspace value is not absence", tenantID: tenantID, workspaceID: &WorkspaceID{}, conversationID: conversationID, conversationType: ConversationGroup, revision: 1},
		{name: "unknown conversation type", tenantID: tenantID, conversationID: conversationID, conversationType: ConversationType("channel"), revision: 1},
		{name: "zero revision", tenantID: tenantID, conversationID: conversationID, conversationType: ConversationGroup},
		{name: "direct cannot claim parent", tenantID: tenantID, conversationID: conversationID, conversationType: ConversationDirect, parentID: parentID, revision: 1},
		{name: "group cannot claim invocation", tenantID: tenantID, conversationID: conversationID, conversationType: ConversationGroup, invocationID: invocationID, revision: 1},
		{name: "thread missing parent", tenantID: tenantID, workspaceID: &workspaceID, conversationID: conversationID, conversationType: ConversationAgentThread, rootMessageID: rootMessageID, invocationID: invocationID, revision: 1},
		{name: "thread missing root message", tenantID: tenantID, conversationID: conversationID, conversationType: ConversationAgentThread, parentID: parentID, invocationID: invocationID, revision: 1},
		{name: "thread missing invocation", tenantID: tenantID, conversationID: conversationID, conversationType: ConversationAgentThread, parentID: parentID, rootMessageID: rootMessageID, revision: 1},
		{name: "thread cannot parent itself", tenantID: tenantID, conversationID: conversationID, conversationType: ConversationAgentThread, parentID: conversationID, rootMessageID: rootMessageID, invocationID: invocationID, revision: 1},
	} {
		test := test
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()
			identity, err := NewConversationIdentity(
				test.tenantID,
				test.workspaceID,
				test.conversationID,
				test.conversationType,
				test.parentID,
				test.rootMessageID,
				test.invocationID,
				test.revision,
			)
			if !errors.Is(err, ErrInvalidConversation) || !identity.IsZero() {
				t.Fatalf("NewConversationIdentity() = (%#v, %v), want zero and ErrInvalidConversation", identity, err)
			}
		})
	}
}

func TestConversationIdentifiersRejectAmbiguousOrUnboundedText(t *testing.T) {
	t.Parallel()

	for _, test := range []struct {
		name  string
		parse func(string) error
		value string
	}{
		{name: "conversation wrong prefix", parse: conversationParseError, value: "group_product"},
		{name: "message empty suffix", parse: messageParseError, value: "msg_"},
		{name: "invocation whitespace", parse: invocationParseError, value: "inv_finance bot"},
		{name: "conversation control", parse: conversationParseError, value: "cnv_parent\nchild"},
		{name: "message unicode confusable", parse: messageParseError, value: "msg_ｒｏｏｔ"},
		{name: "invocation trailing separator", parse: invocationParseError, value: "inv_finance_"},
		{name: "conversation oversize", parse: conversationParseError, value: "cnv_" + strings.Repeat("a", maxPlatformIDBytes)},
	} {
		test := test
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()
			if err := test.parse(test.value); !errors.Is(err, ErrInvalidConversation) {
				t.Fatalf("parse(%q) error = %v, want ErrInvalidConversation", test.value, err)
			}
		})
	}
}

func mustWorkspaceID(t *testing.T, value string) WorkspaceID {
	t.Helper()
	identifier, err := ParseWorkspaceID(value)
	if err != nil {
		t.Fatalf("ParseWorkspaceID(%q) error = %v", value, err)
	}
	return identifier
}

func mustConversationID(t *testing.T, value string) ConversationID {
	t.Helper()
	identifier, err := ParseConversationID(value)
	if err != nil {
		t.Fatalf("ParseConversationID(%q) error = %v", value, err)
	}
	return identifier
}

func mustMessageID(t *testing.T, value string) MessageID {
	t.Helper()
	identifier, err := ParseMessageID(value)
	if err != nil {
		t.Fatalf("ParseMessageID(%q) error = %v", value, err)
	}
	return identifier
}

func mustInvocationID(t *testing.T, value string) InvocationID {
	t.Helper()
	identifier, err := ParseInvocationID(value)
	if err != nil {
		t.Fatalf("ParseInvocationID(%q) error = %v", value, err)
	}
	return identifier
}

func conversationParseError(value string) error {
	_, err := ParseConversationID(value)
	return err
}

func messageParseError(value string) error {
	_, err := ParseMessageID(value)
	return err
}

func invocationParseError(value string) error {
	_, err := ParseInvocationID(value)
	return err
}
