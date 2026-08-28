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

type HumanPrincipalStatus string

const (
	HumanPrincipalActive    HumanPrincipalStatus = "active"
	HumanPrincipalSuspended HumanPrincipalStatus = "suspended"
	HumanPrincipalClosed    HumanPrincipalStatus = "closed"
)

func (status HumanPrincipalStatus) Valid() bool {
	switch status {
	case HumanPrincipalActive, HumanPrincipalSuspended, HumanPrincipalClosed:
		return true
	default:
		return false
	}
}

// HumanPrincipalSnapshot is one immutable revision of a global natural person. It carries no
// tenant role, visible Actor identity, provider proof, or conversation access.
type HumanPrincipalSnapshot struct {
	principalID HumanPrincipalID
	status      HumanPrincipalStatus
	revision    uint64
}

func NewHumanPrincipalSnapshot(
	principalID HumanPrincipalID,
	status HumanPrincipalStatus,
	revision uint64,
) (HumanPrincipalSnapshot, error) {
	if principalID.IsZero() || !status.Valid() || !validPersistentRevision(revision) {
		return HumanPrincipalSnapshot{}, ErrInvalidAuthority
	}
	return HumanPrincipalSnapshot{
		principalID: principalID,
		status:      status,
		revision:    revision,
	}, nil
}

func (snapshot HumanPrincipalSnapshot) PrincipalID() HumanPrincipalID {
	return snapshot.principalID
}

func (snapshot HumanPrincipalSnapshot) Status() HumanPrincipalStatus { return snapshot.status }
func (snapshot HumanPrincipalSnapshot) Revision() uint64             { return snapshot.revision }
func (snapshot HumanPrincipalSnapshot) IsZero() bool {
	return snapshot.principalID.IsZero() && snapshot.status == "" && snapshot.revision == 0
}

// HumanExternalIdentityBinding is one immutable revision of the global Clerk mapping from an
// authenticated provider subject to a HumanPrincipalID. Tenant membership and visible Actor
// mapping remain separate. It is not proof that Clerk authenticated the current request; the
// adapter must verify that before resolving this value.
type HumanExternalIdentityBinding struct {
	externalRef ExternalIdentityRef
	principalID HumanPrincipalID
	status      ExternalIdentityBindingStatus
	revision    uint64
}

func NewHumanExternalIdentityBinding(
	externalRef ExternalIdentityRef,
	principalID HumanPrincipalID,
	status ExternalIdentityBindingStatus,
	revision uint64,
) (HumanExternalIdentityBinding, error) {
	if externalRef.IsZero() || externalRef.Provider() != IdentityProviderClerk ||
		principalID.IsZero() || !status.Valid() || !validPersistentRevision(revision) {
		return HumanExternalIdentityBinding{}, ErrInvalidAuthority
	}
	return HumanExternalIdentityBinding{
		externalRef: externalRef,
		principalID: principalID,
		status:      status,
		revision:    revision,
	}, nil
}

func (binding HumanExternalIdentityBinding) ExternalRef() ExternalIdentityRef {
	return binding.externalRef
}

func (binding HumanExternalIdentityBinding) PrincipalID() HumanPrincipalID {
	return binding.principalID
}

func (binding HumanExternalIdentityBinding) Status() ExternalIdentityBindingStatus {
	return binding.status
}

func (binding HumanExternalIdentityBinding) Revision() uint64 { return binding.revision }

func (binding HumanExternalIdentityBinding) IsZero() bool {
	return binding.externalRef.IsZero() && binding.principalID.IsZero() && binding.status == "" &&
		binding.revision == 0
}

// ProviderActorBinding is one immutable revision of the tenant-scoped RongCloud user mapping to
// a visible human or Agent Actor. RongCloud V1 registers that Actor under its platform Actor ID.
// This mapping is still not provider authentication, tenant membership, Agent installation, or
// conversation authorization.
type ProviderActorBinding struct {
	externalRef ExternalIdentityRef
	actorRef    ActorRef
	status      ExternalIdentityBindingStatus
	revision    uint64
}

func NewProviderActorBinding(
	externalRef ExternalIdentityRef,
	actorRef ActorRef,
	status ExternalIdentityBindingStatus,
	revision uint64,
) (ProviderActorBinding, error) {
	actorType, hasActorType := actorRef.ActorID().SubjectType()
	if externalRef.IsZero() || externalRef.Provider() != IdentityProviderRongCloud ||
		actorRef.IsZero() || !status.Valid() || !validPersistentRevision(revision) ||
		!hasActorType || (actorType != SubjectHuman && actorType != SubjectAgent) ||
		externalRef.SubjectID() != actorRef.ActorID().String() {
		return ProviderActorBinding{}, ErrInvalidAuthority
	}
	return ProviderActorBinding{
		externalRef: externalRef,
		actorRef:    actorRef,
		status:      status,
		revision:    revision,
	}, nil
}

func (binding ProviderActorBinding) ExternalRef() ExternalIdentityRef {
	return binding.externalRef
}

func (binding ProviderActorBinding) ActorRef() ActorRef { return binding.actorRef }
func (binding ProviderActorBinding) Status() ExternalIdentityBindingStatus {
	return binding.status
}

func (binding ProviderActorBinding) Revision() uint64 { return binding.revision }

func (binding ProviderActorBinding) IsZero() bool {
	return binding.externalRef.IsZero() && binding.actorRef.IsZero() && binding.status == "" &&
		binding.revision == 0
}

type TenantMembershipRole string

const (
	TenantMembershipOwner  TenantMembershipRole = "owner"
	TenantMembershipAdmin  TenantMembershipRole = "admin"
	TenantMembershipMember TenantMembershipRole = "member"
	TenantMembershipGuest  TenantMembershipRole = "guest"
)

func (role TenantMembershipRole) Valid() bool {
	switch role {
	case TenantMembershipOwner, TenantMembershipAdmin, TenantMembershipMember,
		TenantMembershipGuest:
		return true
	default:
		return false
	}
}

type TenantMembershipStatus string

const (
	TenantMembershipActive    TenantMembershipStatus = "active"
	TenantMembershipSuspended TenantMembershipStatus = "suspended"
	TenantMembershipRemoved   TenantMembershipStatus = "removed"
)

func (status TenantMembershipStatus) Valid() bool {
	switch status {
	case TenantMembershipActive, TenantMembershipSuspended, TenantMembershipRemoved:
		return true
	default:
		return false
	}
}

// TenantMembershipSnapshot maps one global natural person to one tenant-local human Actor at one
// revision. A provider binding does not create this membership, and roles never cross tenants.
type TenantMembershipSnapshot struct {
	tenantID    TenantID
	principalID HumanPrincipalID
	actorRef    ActorRef
	role        TenantMembershipRole
	status      TenantMembershipStatus
	revision    uint64
}

func NewTenantMembershipSnapshot(
	tenantID TenantID,
	principalID HumanPrincipalID,
	actorRef ActorRef,
	role TenantMembershipRole,
	status TenantMembershipStatus,
	revision uint64,
) (TenantMembershipSnapshot, error) {
	actorType, hasActorType := actorRef.ActorID().SubjectType()
	if tenantID.IsZero() || principalID.IsZero() || actorRef.IsZero() || !role.Valid() ||
		!status.Valid() || !validPersistentRevision(revision) || actorRef.TenantID() != tenantID ||
		!hasActorType ||
		actorType != SubjectHuman {
		return TenantMembershipSnapshot{}, ErrInvalidAuthority
	}
	return TenantMembershipSnapshot{
		tenantID:    tenantID,
		principalID: principalID,
		actorRef:    actorRef,
		role:        role,
		status:      status,
		revision:    revision,
	}, nil
}

func (snapshot TenantMembershipSnapshot) TenantID() TenantID {
	return snapshot.tenantID
}

func (snapshot TenantMembershipSnapshot) PrincipalID() HumanPrincipalID {
	return snapshot.principalID
}

func (snapshot TenantMembershipSnapshot) ActorRef() ActorRef { return snapshot.actorRef }
func (snapshot TenantMembershipSnapshot) Role() TenantMembershipRole {
	return snapshot.role
}

func (snapshot TenantMembershipSnapshot) Status() TenantMembershipStatus {
	return snapshot.status
}

func (snapshot TenantMembershipSnapshot) Revision() uint64 { return snapshot.revision }

func (snapshot TenantMembershipSnapshot) IsZero() bool {
	return snapshot.tenantID.IsZero() && snapshot.principalID.IsZero() &&
		snapshot.actorRef.IsZero() && snapshot.role == "" && snapshot.status == "" &&
		snapshot.revision == 0
}

type ConversationMembershipStatus string

const (
	ConversationMembershipActive  ConversationMembershipStatus = "active"
	ConversationMembershipRemoved ConversationMembershipStatus = "removed"
)

func (status ConversationMembershipStatus) Valid() bool {
	return status == ConversationMembershipActive || status == ConversationMembershipRemoved
}

type ConversationMembershipRole string

const (
	ConversationMembershipOwner   ConversationMembershipRole = "owner"
	ConversationMembershipManager ConversationMembershipRole = "manager"
	ConversationMembershipMember  ConversationMembershipRole = "member"
)

func (role ConversationMembershipRole) Valid() bool {
	switch role {
	case ConversationMembershipOwner, ConversationMembershipManager,
		ConversationMembershipMember:
		return true
	default:
		return false
	}
}

// ConversationMembershipSnapshot states whether an Actor is a member of one Conversation at one
// revision. Membership is deliberately separate from topology and access permissions.
type ConversationMembershipSnapshot struct {
	conversationRef ConversationRef
	actorRef        ActorRef
	role            ConversationMembershipRole
	status          ConversationMembershipStatus
	revision        uint64
}

func NewConversationMembershipSnapshot(
	conversationRef ConversationRef,
	actorRef ActorRef,
	role ConversationMembershipRole,
	status ConversationMembershipStatus,
	revision uint64,
) (ConversationMembershipSnapshot, error) {
	if conversationRef.IsZero() || actorRef.IsZero() || !role.Valid() || !status.Valid() ||
		!validPersistentRevision(revision) ||
		conversationRef.TenantID() != actorRef.TenantID() {
		return ConversationMembershipSnapshot{}, ErrInvalidAuthority
	}
	return ConversationMembershipSnapshot{
		conversationRef: conversationRef,
		actorRef:        actorRef,
		role:            role,
		status:          status,
		revision:        revision,
	}, nil
}

func (snapshot ConversationMembershipSnapshot) ConversationRef() ConversationRef {
	return snapshot.conversationRef
}

func (snapshot ConversationMembershipSnapshot) ActorRef() ActorRef { return snapshot.actorRef }

func (snapshot ConversationMembershipSnapshot) Role() ConversationMembershipRole {
	return snapshot.role
}

func (snapshot ConversationMembershipSnapshot) Status() ConversationMembershipStatus {
	return snapshot.status
}

func (snapshot ConversationMembershipSnapshot) Revision() uint64 { return snapshot.revision }

func (snapshot ConversationMembershipSnapshot) IsZero() bool {
	return snapshot.conversationRef.IsZero() && snapshot.actorRef.IsZero() &&
		snapshot.role == "" && snapshot.status == "" && snapshot.revision == 0
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
	if conversationRef.IsZero() || actorRef.IsZero() || !validPersistentRevision(revision) ||
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
