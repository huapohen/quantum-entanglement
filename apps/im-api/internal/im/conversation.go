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

// ConversationRef is the stable tenant-scoped collaboration-space reference. It is not a task,
// membership grant, capability, or provider group binding.
type ConversationRef struct {
	tenantID       TenantID
	conversationID ConversationID
}

func NewConversationRef(
	tenantID TenantID,
	conversationID ConversationID,
) (ConversationRef, error) {
	if tenantID.IsZero() || conversationID.IsZero() {
		return ConversationRef{}, ErrInvalidConversation
	}
	return ConversationRef{tenantID: tenantID, conversationID: conversationID}, nil
}

func (reference ConversationRef) TenantID() TenantID { return reference.tenantID }
func (reference ConversationRef) ConversationID() ConversationID {
	return reference.conversationID
}
func (reference ConversationRef) IsZero() bool {
	return reference.tenantID.IsZero() && reference.conversationID.IsZero()
}

// ConversationSnapshot describes one immutable revision and topology projection of a stable
// ConversationRef. Parent lineage never grants access to a child conversation.
type ConversationSnapshot struct {
	reference          ConversationRef
	workspaceID        WorkspaceID
	hasWorkspace       bool
	conversationType   ConversationType
	parentConversation ConversationID
	rootMessageID      MessageID
	agentInvocationID  InvocationID
	revision           uint64
}

func NewConversationSnapshot(
	reference ConversationRef,
	workspaceID *WorkspaceID,
	conversationType ConversationType,
	parentConversationID ConversationID,
	rootMessageID MessageID,
	agentInvocationID InvocationID,
	revision uint64,
) (ConversationSnapshot, error) {
	if reference.IsZero() || !conversationType.Valid() || !validPersistentRevision(revision) {
		return ConversationSnapshot{}, ErrInvalidConversation
	}

	var workspace WorkspaceID
	hasWorkspace := workspaceID != nil
	if hasWorkspace {
		if workspaceID.IsZero() {
			return ConversationSnapshot{}, ErrInvalidConversation
		}
		workspace = *workspaceID
	}

	hasParent := !parentConversationID.IsZero()
	hasRoot := !rootMessageID.IsZero()
	hasInvocation := !agentInvocationID.IsZero()
	switch conversationType {
	case ConversationAgentThread:
		if !hasParent || !hasRoot || !hasInvocation ||
			parentConversationID == reference.conversationID {
			return ConversationSnapshot{}, ErrInvalidConversation
		}
	case ConversationDirect, ConversationGroup:
		if hasParent || hasRoot || hasInvocation {
			return ConversationSnapshot{}, ErrInvalidConversation
		}
	default:
		return ConversationSnapshot{}, ErrInvalidConversation
	}

	return ConversationSnapshot{
		reference:          reference,
		workspaceID:        workspace,
		hasWorkspace:       hasWorkspace,
		conversationType:   conversationType,
		parentConversation: parentConversationID,
		rootMessageID:      rootMessageID,
		agentInvocationID:  agentInvocationID,
		revision:           revision,
	}, nil
}

func (snapshot ConversationSnapshot) Ref() ConversationRef { return snapshot.reference }
func (snapshot ConversationSnapshot) WorkspaceID() (WorkspaceID, bool) {
	return snapshot.workspaceID, snapshot.hasWorkspace
}
func (snapshot ConversationSnapshot) ConversationType() ConversationType {
	return snapshot.conversationType
}
func (snapshot ConversationSnapshot) ParentConversationID() ConversationID {
	return snapshot.parentConversation
}
func (snapshot ConversationSnapshot) RootMessageID() MessageID {
	return snapshot.rootMessageID
}
func (snapshot ConversationSnapshot) AgentInvocationID() InvocationID {
	return snapshot.agentInvocationID
}
func (snapshot ConversationSnapshot) Revision() uint64 { return snapshot.revision }
func (snapshot ConversationSnapshot) IsZero() bool {
	return snapshot.reference.IsZero() && snapshot.workspaceID.IsZero() && !snapshot.hasWorkspace &&
		snapshot.conversationType == "" && snapshot.parentConversation.IsZero() &&
		snapshot.rootMessageID.IsZero() && snapshot.agentInvocationID.IsZero() &&
		snapshot.revision == 0
}
