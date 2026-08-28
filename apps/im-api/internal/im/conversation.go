package im

import "errors"

var ErrInvalidConversation = errors.New("invalid IM conversation")

type ConversationType string

const (
	ConversationDirect      ConversationType = "direct"
	ConversationGroup       ConversationType = "group"
	ConversationAgentThread ConversationType = "agent_thread"
)

func (conversationType ConversationType) Valid() bool {
	switch conversationType {
	case ConversationDirect, ConversationGroup, ConversationAgentThread:
		return true
	default:
		return false
	}
}

type ConversationID struct{ value string }

func ParseConversationID(value string) (ConversationID, error) {
	if !validPrefixedPlatformID(value, conversationIDPrefix) {
		return ConversationID{}, ErrInvalidConversation
	}
	return ConversationID{value: value}, nil
}

func (value ConversationID) String() string { return value.value }
func (value ConversationID) IsZero() bool   { return value.value == "" }

type MessageID struct{ value string }

func ParseMessageID(value string) (MessageID, error) {
	if !validPrefixedPlatformID(value, messageIDPrefix) {
		return MessageID{}, ErrInvalidConversation
	}
	return MessageID{value: value}, nil
}

func (value MessageID) String() string { return value.value }
func (value MessageID) IsZero() bool   { return value.value == "" }

type InvocationID struct{ value string }

func ParseInvocationID(value string) (InvocationID, error) {
	if !validPrefixedPlatformID(value, invocationIDPrefix) {
		return InvocationID{}, ErrInvalidConversation
	}
	return InvocationID{value: value}, nil
}

func (value InvocationID) String() string { return value.value }
func (value InvocationID) IsZero() bool   { return value.value == "" }

// ConversationIdentity is a stable collaboration-space identity and topology projection. It is
// not a task, membership grant, capability, or proof that parent participants may access a child.
type ConversationIdentity struct {
	tenantID           TenantID
	workspaceID        WorkspaceID
	hasWorkspace       bool
	conversationID     ConversationID
	conversationType   ConversationType
	parentConversation ConversationID
	rootMessageID      MessageID
	agentInvocationID  InvocationID
	revision           uint64
}

func NewConversationIdentity(
	tenantID TenantID,
	workspaceID *WorkspaceID,
	conversationID ConversationID,
	conversationType ConversationType,
	parentConversationID ConversationID,
	rootMessageID MessageID,
	agentInvocationID InvocationID,
	revision uint64,
) (ConversationIdentity, error) {
	if tenantID.IsZero() || conversationID.IsZero() || !conversationType.Valid() || revision == 0 {
		return ConversationIdentity{}, ErrInvalidConversation
	}

	var workspace WorkspaceID
	hasWorkspace := workspaceID != nil
	if hasWorkspace {
		if workspaceID.IsZero() {
			return ConversationIdentity{}, ErrInvalidConversation
		}
		workspace = *workspaceID
	}

	hasParent := !parentConversationID.IsZero()
	hasRoot := !rootMessageID.IsZero()
	hasInvocation := !agentInvocationID.IsZero()
	switch conversationType {
	case ConversationAgentThread:
		if !hasParent || !hasRoot || !hasInvocation || parentConversationID == conversationID {
			return ConversationIdentity{}, ErrInvalidConversation
		}
	case ConversationDirect, ConversationGroup:
		if hasParent || hasRoot || hasInvocation {
			return ConversationIdentity{}, ErrInvalidConversation
		}
	default:
		return ConversationIdentity{}, ErrInvalidConversation
	}

	return ConversationIdentity{
		tenantID:           tenantID,
		workspaceID:        workspace,
		hasWorkspace:       hasWorkspace,
		conversationID:     conversationID,
		conversationType:   conversationType,
		parentConversation: parentConversationID,
		rootMessageID:      rootMessageID,
		agentInvocationID:  agentInvocationID,
		revision:           revision,
	}, nil
}

func (identity ConversationIdentity) TenantID() TenantID { return identity.tenantID }
func (identity ConversationIdentity) WorkspaceID() (WorkspaceID, bool) {
	return identity.workspaceID, identity.hasWorkspace
}
func (identity ConversationIdentity) ConversationID() ConversationID {
	return identity.conversationID
}
func (identity ConversationIdentity) ConversationType() ConversationType {
	return identity.conversationType
}
func (identity ConversationIdentity) ParentConversationID() ConversationID {
	return identity.parentConversation
}
func (identity ConversationIdentity) RootMessageID() MessageID {
	return identity.rootMessageID
}
func (identity ConversationIdentity) AgentInvocationID() InvocationID {
	return identity.agentInvocationID
}
func (identity ConversationIdentity) Revision() uint64 { return identity.revision }
func (identity ConversationIdentity) IsZero() bool {
	return identity.tenantID.IsZero() && identity.workspaceID.IsZero() && !identity.hasWorkspace &&
		identity.conversationID.IsZero() && identity.conversationType == "" &&
		identity.parentConversation.IsZero() && identity.rootMessageID.IsZero() &&
		identity.agentInvocationID.IsZero() && identity.revision == 0
}
