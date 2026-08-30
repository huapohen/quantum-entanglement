package im

import (
	"errors"
	"strings"
	"testing"
)

func TestConversationSnapshotFreezesOrdinaryAndAgentThreadTopology(t *testing.T) {
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
		status           ConversationStatus
		wantWorkspace    WorkspaceID
		wantWorkspaceSet bool
	}{
		{
			name:             "direct conversation may be tenant scoped",
			conversationID:   mustConversationID(t, "cnv_direct_alice_bob"),
			conversationType: ConversationDirect,
			status:           ConversationActive,
		},
		{
			name:             "group conversation may be workspace scoped",
			workspaceID:      &workspaceID,
			conversationID:   mustConversationID(t, "cnv_product_group"),
			conversationType: ConversationGroup,
			status:           ConversationActive,
			wantWorkspace:    workspaceID,
			wantWorkspaceSet: true,
		},
		{
			name:             "agent thread binds parent root message and invocation",
			workspaceID:      &workspaceID,
			conversationID:   mustConversationID(t, "cnv_agent_thread"),
			conversationType: ConversationAgentThread,
			status:           ConversationActive,
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
			reference, err := NewConversationRef(tenantID, test.conversationID)
			if err != nil {
				t.Fatalf("NewConversationRef() error = %v", err)
			}
			snapshot, err := NewConversationSnapshot(
				reference,
				test.workspaceID,
				test.conversationType,
				test.status,
				test.parentID,
				test.rootMessageID,
				test.invocationID,
				11,
			)
			if err != nil {
				t.Fatalf("NewConversationSnapshot() error = %v", err)
			}
			workspace, workspaceSet := snapshot.WorkspaceID()
			if snapshot.Ref() != reference || reference.TenantID() != tenantID ||
				reference.ConversationID() != test.conversationID ||
				snapshot.ConversationType() != test.conversationType ||
				snapshot.Status() != test.status ||
				snapshot.ParentConversationID() != test.parentID ||
				snapshot.RootMessageID() != test.rootMessageID ||
				snapshot.AgentInvocationID() != test.invocationID || snapshot.Revision() != 11 ||
				workspace != test.wantWorkspace || workspaceSet != test.wantWorkspaceSet ||
				reference.IsZero() || snapshot.IsZero() {
				t.Fatalf("unexpected conversation reference/snapshot: %#v %#v", reference, snapshot)
			}
		})
	}
}

func TestConversationReferenceRemainsStableAcrossSnapshotRevisions(t *testing.T) {
	t.Parallel()

	reference, err := NewConversationRef(
		mustTenantID(t, "ten_acme"),
		mustConversationID(t, "cnv_product"),
	)
	if err != nil {
		t.Fatalf("NewConversationRef() error = %v", err)
	}
	first, err := NewConversationSnapshot(reference, nil, ConversationGroup, ConversationActive, ConversationID{}, MessageID{}, InvocationID{}, 1)
	if err != nil {
		t.Fatalf("NewConversationSnapshot(first) error = %v", err)
	}
	second, err := NewConversationSnapshot(reference, nil, ConversationGroup, ConversationArchived, ConversationID{}, MessageID{}, InvocationID{}, 2)
	if err != nil {
		t.Fatalf("NewConversationSnapshot(second) error = %v", err)
	}
	if first == second || first.Ref() != second.Ref() {
		t.Fatalf("snapshots must differ while stable refs match: %#v %#v", first, second)
	}
}

func TestConversationReferenceRejectsIncompleteScope(t *testing.T) {
	t.Parallel()

	tenantID := mustTenantID(t, "ten_acme")
	conversationID := mustConversationID(t, "cnv_product")
	for _, test := range []struct {
		name           string
		tenantID       TenantID
		conversationID ConversationID
	}{
		{name: "missing tenant", conversationID: conversationID},
		{name: "missing conversation", tenantID: tenantID},
	} {
		test := test
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()
			reference, err := NewConversationRef(test.tenantID, test.conversationID)
			if !errors.Is(err, ErrInvalidConversation) || !reference.IsZero() {
				t.Fatalf("NewConversationRef() = (%#v, %v), want zero and ErrInvalidConversation", reference, err)
			}
		})
	}
}

func TestConversationSnapshotRejectsIncompleteOrForgedTopology(t *testing.T) {
	t.Parallel()

	tenantID := mustTenantID(t, "ten_acme")
	workspaceID := mustWorkspaceID(t, "wsp_product")
	conversationID := mustConversationID(t, "cnv_agent_thread")
	parentID := mustConversationID(t, "cnv_parent")
	rootMessageID := mustMessageID(t, "msg_root")
	invocationID := mustInvocationID(t, "inv_finance")
	reference, err := NewConversationRef(tenantID, conversationID)
	if err != nil {
		t.Fatalf("NewConversationRef() error = %v", err)
	}

	for _, test := range []struct {
		name             string
		reference        ConversationRef
		workspaceID      *WorkspaceID
		conversationType ConversationType
		status           ConversationStatus
		parentID         ConversationID
		rootMessageID    MessageID
		invocationID     InvocationID
		revision         uint64
	}{
		{name: "missing reference", conversationType: ConversationGroup, status: ConversationActive, revision: 1},
		{name: "zero workspace value is not absence", reference: reference, workspaceID: &WorkspaceID{}, conversationType: ConversationGroup, status: ConversationActive, revision: 1},
		{name: "unknown conversation type", reference: reference, conversationType: ConversationType("channel"), status: ConversationActive, revision: 1},
		{name: "unknown status", reference: reference, conversationType: ConversationGroup, status: ConversationStatus("ready"), revision: 1},
		{name: "zero revision", reference: reference, conversationType: ConversationGroup, status: ConversationActive},
		{name: "revision exceeds PostgreSQL bigint", reference: reference, conversationType: ConversationGroup, status: ConversationActive, revision: maxPersistentRevision + 1},
		{name: "direct cannot claim parent", reference: reference, conversationType: ConversationDirect, status: ConversationActive, parentID: parentID, revision: 1},
		{name: "group cannot claim invocation", reference: reference, conversationType: ConversationGroup, status: ConversationActive, invocationID: invocationID, revision: 1},
		{name: "thread missing parent", reference: reference, workspaceID: &workspaceID, conversationType: ConversationAgentThread, status: ConversationActive, rootMessageID: rootMessageID, invocationID: invocationID, revision: 1},
		{name: "thread missing root message", reference: reference, conversationType: ConversationAgentThread, status: ConversationActive, parentID: parentID, invocationID: invocationID, revision: 1},
		{name: "thread missing invocation", reference: reference, conversationType: ConversationAgentThread, status: ConversationActive, parentID: parentID, rootMessageID: rootMessageID, revision: 1},
		{name: "thread cannot parent itself", reference: reference, conversationType: ConversationAgentThread, status: ConversationActive, parentID: conversationID, rootMessageID: rootMessageID, invocationID: invocationID, revision: 1},
	} {
		test := test
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()
			snapshot, err := NewConversationSnapshot(
				test.reference,
				test.workspaceID,
				test.conversationType,
				test.status,
				test.parentID,
				test.rootMessageID,
				test.invocationID,
				test.revision,
			)
			if !errors.Is(err, ErrInvalidConversation) || !snapshot.IsZero() {
				t.Fatalf("NewConversationSnapshot() = (%#v, %v), want zero and ErrInvalidConversation", snapshot, err)
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

func TestProviderConversationReferenceIsRealmScopedMappingMetadata(t *testing.T) {
	t.Parallel()
	realm := mustProviderRealmID(t, "rlm_prod")
	reference, err := NewProviderConversationRef(
		IdentityProviderRongCloud,
		realm,
		"cnv_product",
	)
	if err != nil {
		t.Fatalf("NewProviderConversationRef() error = %v", err)
	}
	if reference.Provider() != IdentityProviderRongCloud || reference.RealmID() != realm ||
		reference.SubjectID() != "cnv_product" || reference.IsZero() {
		t.Fatalf("unexpected provider conversation ref: %#v", reference)
	}

	for _, fixture := range []struct {
		name      string
		provider  IdentityProvider
		realm     ProviderRealmID
		subjectID string
	}{
		{name: "auth provider", provider: IdentityProviderClerk, realm: realm, subjectID: "cnv_product"},
		{name: "missing realm", provider: IdentityProviderRongCloud, subjectID: "cnv_product"},
		{name: "not platform conversation", provider: IdentityProviderRongCloud, realm: realm, subjectID: "group_product"},
	} {
		t.Run(fixture.name, func(t *testing.T) {
			t.Parallel()
			value, err := NewProviderConversationRef(
				fixture.provider,
				fixture.realm,
				fixture.subjectID,
			)
			if !errors.Is(err, ErrInvalidConversation) || !value.IsZero() {
				t.Fatalf("expected zero value and fixed error, got %#v, %v", value, err)
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
