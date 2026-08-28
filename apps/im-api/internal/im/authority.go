package im

import "errors"

var ErrInvalidAuthority = errors.New("invalid IM authority value")

// ExternalIdentityBindingStatus is platform-owned mapping state. A provider subject or ext_info
// value never changes this state by itself.
type ExternalIdentityBindingStatus string

const (
	ExternalIdentityBindingActive  ExternalIdentityBindingStatus = "active"
	ExternalIdentityBindingRevoked ExternalIdentityBindingStatus = "revoked"
)

func (status ExternalIdentityBindingStatus) Valid() bool {
	return status == ExternalIdentityBindingActive || status == ExternalIdentityBindingRevoked
}

// ExternalIdentityBinding is one immutable revision of the tenant-scoped mapping from an
// authenticated provider subject to a stable ActorRef. It is not proof that the provider
// authenticated the current request; adapters must verify that before resolving this value.
type ExternalIdentityBinding struct {
	externalRef ExternalIdentityRef
	actorRef    ActorRef
	status      ExternalIdentityBindingStatus
	revision    uint64
}

func NewExternalIdentityBinding(
	externalRef ExternalIdentityRef,
	actorRef ActorRef,
	status ExternalIdentityBindingStatus,
	revision uint64,
) (ExternalIdentityBinding, error) {
	if externalRef.IsZero() || actorRef.IsZero() || !status.Valid() || revision == 0 {
		return ExternalIdentityBinding{}, ErrInvalidAuthority
	}
	// RongCloud V1 registers a human or Agent under its platform Actor ID. The persisted binding
	// rejects a syntactically valid projection that points at a different Actor.
	if externalRef.Provider() == IdentityProviderRongCloud &&
		externalRef.SubjectID() != actorRef.ActorID().String() {
		return ExternalIdentityBinding{}, ErrInvalidAuthority
	}
	return ExternalIdentityBinding{
		externalRef: externalRef,
		actorRef:    actorRef,
		status:      status,
		revision:    revision,
	}, nil
}

func (binding ExternalIdentityBinding) ExternalRef() ExternalIdentityRef {
	return binding.externalRef
}

func (binding ExternalIdentityBinding) ActorRef() ActorRef {
	return binding.actorRef
}

func (binding ExternalIdentityBinding) Status() ExternalIdentityBindingStatus {
	return binding.status
}

func (binding ExternalIdentityBinding) Revision() uint64 { return binding.revision }

func (binding ExternalIdentityBinding) IsZero() bool {
	return binding.externalRef.IsZero() && binding.actorRef.IsZero() && binding.status == "" &&
		binding.revision == 0
}

type ConversationMembershipStatus string

const (
	ConversationMembershipActive  ConversationMembershipStatus = "active"
	ConversationMembershipRemoved ConversationMembershipStatus = "removed"
)

func (status ConversationMembershipStatus) Valid() bool {
	return status == ConversationMembershipActive || status == ConversationMembershipRemoved
}

// ConversationMembershipSnapshot states whether an Actor is a member of one Conversation at one
// revision. Membership is deliberately separate from topology and access permissions.
type ConversationMembershipSnapshot struct {
	conversationRef ConversationRef
	actorRef        ActorRef
	status          ConversationMembershipStatus
	revision        uint64
}

func NewConversationMembershipSnapshot(
	conversationRef ConversationRef,
	actorRef ActorRef,
	status ConversationMembershipStatus,
	revision uint64,
) (ConversationMembershipSnapshot, error) {
	if conversationRef.IsZero() || actorRef.IsZero() || !status.Valid() || revision == 0 ||
		conversationRef.TenantID() != actorRef.TenantID() {
		return ConversationMembershipSnapshot{}, ErrInvalidAuthority
	}
	return ConversationMembershipSnapshot{
		conversationRef: conversationRef,
		actorRef:        actorRef,
		status:          status,
		revision:        revision,
	}, nil
}

func (snapshot ConversationMembershipSnapshot) ConversationRef() ConversationRef {
	return snapshot.conversationRef
}

func (snapshot ConversationMembershipSnapshot) ActorRef() ActorRef { return snapshot.actorRef }

func (snapshot ConversationMembershipSnapshot) Status() ConversationMembershipStatus {
	return snapshot.status
}

func (snapshot ConversationMembershipSnapshot) Revision() uint64 { return snapshot.revision }

func (snapshot ConversationMembershipSnapshot) IsZero() bool {
	return snapshot.conversationRef.IsZero() && snapshot.actorRef.IsZero() &&
		snapshot.status == "" && snapshot.revision == 0
}

type ConversationPermission string

const (
	ConversationPermissionRead                     ConversationPermission = "read"
	ConversationPermissionSendMessage              ConversationPermission = "send_message"
	ConversationPermissionManageMembers            ConversationPermission = "manage_members"
	ConversationPermissionManageConversation       ConversationPermission = "manage_conversation"
	ConversationPermissionInvokeAgent              ConversationPermission = "invoke_agent"
	ConversationPermissionPublishArtifactReference ConversationPermission = "publish_artifact_reference"
)

var conversationPermissionOrder = [...]ConversationPermission{
	ConversationPermissionRead,
	ConversationPermissionSendMessage,
	ConversationPermissionManageMembers,
	ConversationPermissionManageConversation,
	ConversationPermissionInvokeAgent,
	ConversationPermissionPublishArtifactReference,
}

func (permission ConversationPermission) Valid() bool {
	for _, candidate := range conversationPermissionOrder {
		if permission == candidate {
			return true
		}
	}
	return false
}

// ConversationAccessSnapshot is an explicit permission projection for one member. An empty set is
// a valid revocation revision. A parent Conversation, membership row, role label, provider profile,
// or ext_info value never creates permissions implicitly.
type ConversationAccessSnapshot struct {
	conversationRef ConversationRef
	actorRef        ActorRef
	permissions     []ConversationPermission
	revision        uint64
}

func NewConversationAccessSnapshot(
	conversationRef ConversationRef,
	actorRef ActorRef,
	permissions []ConversationPermission,
	revision uint64,
) (ConversationAccessSnapshot, error) {
	if conversationRef.IsZero() || actorRef.IsZero() || revision == 0 ||
		conversationRef.TenantID() != actorRef.TenantID() {
		return ConversationAccessSnapshot{}, ErrInvalidAuthority
	}
	seen := make(map[ConversationPermission]struct{}, len(permissions))
	for _, permission := range permissions {
		if !permission.Valid() {
			return ConversationAccessSnapshot{}, ErrInvalidAuthority
		}
		if _, exists := seen[permission]; exists {
			return ConversationAccessSnapshot{}, ErrInvalidAuthority
		}
		seen[permission] = struct{}{}
	}
	canonical := make([]ConversationPermission, 0, len(seen))
	for _, permission := range conversationPermissionOrder {
		if _, exists := seen[permission]; exists {
			canonical = append(canonical, permission)
		}
	}
	return ConversationAccessSnapshot{
		conversationRef: conversationRef,
		actorRef:        actorRef,
		permissions:     canonical,
		revision:        revision,
	}, nil
}

func (snapshot ConversationAccessSnapshot) ConversationRef() ConversationRef {
	return snapshot.conversationRef
}

func (snapshot ConversationAccessSnapshot) ActorRef() ActorRef { return snapshot.actorRef }

func (snapshot ConversationAccessSnapshot) Permissions() []ConversationPermission {
	return append([]ConversationPermission(nil), snapshot.permissions...)
}

func (snapshot ConversationAccessSnapshot) HasPermission(permission ConversationPermission) bool {
	if !permission.Valid() {
		return false
	}
	for _, candidate := range snapshot.permissions {
		if candidate == permission {
			return true
		}
	}
	return false
}

func (snapshot ConversationAccessSnapshot) Revision() uint64 { return snapshot.revision }

func (snapshot ConversationAccessSnapshot) IsZero() bool {
	return snapshot.conversationRef.IsZero() && snapshot.actorRef.IsZero() &&
		len(snapshot.permissions) == 0 && snapshot.revision == 0
}
