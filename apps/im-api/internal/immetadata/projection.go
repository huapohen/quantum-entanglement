// Package immetadata owns the untrusted provider metadata projection boundary. Its values are
// display and reconciliation hints only; callers must resolve authorization from platform state.
package immetadata

import (
	"errors"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
)

const SchemaVersion = 1

var (
	ErrInvalidProviderMetadata  = errors.New("invalid IM provider metadata")
	ErrProviderMetadataTooLarge = errors.New("IM provider metadata too large")
)

// UserProjection is the minimum identity projection stored in a RongCloud user's ext_info. It
// deliberately excludes tenant, membership, credentials, capabilities, and authorization facts.
type UserProjection struct {
	subjectType     im.SubjectType
	platformActorID im.ActorID
	agentDefinition im.AgentDefinitionID
	agentVersion    im.AgentVersion
}

func NewUserProjection(
	subjectType im.SubjectType,
	platformActorID im.ActorID,
	agentDefinitionID im.AgentDefinitionID,
	agentVersion im.AgentVersion,
) (UserProjection, error) {
	inferredType, ok := platformActorID.SubjectType()
	if !ok || inferredType != subjectType {
		return UserProjection{}, ErrInvalidProviderMetadata
	}

	switch subjectType {
	case im.SubjectHuman:
		if !agentDefinitionID.IsZero() || !agentVersion.IsZero() {
			return UserProjection{}, ErrInvalidProviderMetadata
		}
	case im.SubjectAgent:
		if agentDefinitionID.IsZero() || agentVersion.IsZero() {
			return UserProjection{}, ErrInvalidProviderMetadata
		}
	default:
		return UserProjection{}, ErrInvalidProviderMetadata
	}

	return UserProjection{
		subjectType:     subjectType,
		platformActorID: platformActorID,
		agentDefinition: agentDefinitionID,
		agentVersion:    agentVersion,
	}, nil
}

func (projection UserProjection) SubjectType() im.SubjectType {
	return projection.subjectType
}
func (projection UserProjection) PlatformActorID() im.ActorID {
	return projection.platformActorID
}
func (projection UserProjection) AgentDefinitionID() im.AgentDefinitionID {
	return projection.agentDefinition
}
func (projection UserProjection) AgentVersion() im.AgentVersion {
	return projection.agentVersion
}
func (projection UserProjection) IsZero() bool {
	return projection.subjectType == "" && projection.platformActorID.IsZero() &&
		projection.agentDefinition.IsZero() && projection.agentVersion.IsZero()
}

// ConversationProjection is the topology projection stored in a RongCloud group's ext_info. It
// is not proof of tenant scope, membership, parent-child authorization, or task ownership.
type ConversationProjection struct {
	conversationType     im.ConversationType
	platformConversation im.ConversationID
	parentConversation   im.ConversationID
	rootMessage          im.MessageID
	agentInvocation      im.InvocationID
}

func NewConversationProjection(
	conversationType im.ConversationType,
	platformConversationID im.ConversationID,
	parentConversationID im.ConversationID,
	rootMessageID im.MessageID,
	agentInvocationID im.InvocationID,
) (ConversationProjection, error) {
	if platformConversationID.IsZero() {
		return ConversationProjection{}, ErrInvalidProviderMetadata
	}

	hasParent := !parentConversationID.IsZero()
	hasRoot := !rootMessageID.IsZero()
	hasInvocation := !agentInvocationID.IsZero()
	switch conversationType {
	case im.ConversationGroup:
		if hasParent || hasRoot || hasInvocation {
			return ConversationProjection{}, ErrInvalidProviderMetadata
		}
	case im.ConversationAgentThread:
		if !hasParent || !hasRoot || !hasInvocation ||
			parentConversationID == platformConversationID {
			return ConversationProjection{}, ErrInvalidProviderMetadata
		}
	default:
		return ConversationProjection{}, ErrInvalidProviderMetadata
	}

	return ConversationProjection{
		conversationType:     conversationType,
		platformConversation: platformConversationID,
		parentConversation:   parentConversationID,
		rootMessage:          rootMessageID,
		agentInvocation:      agentInvocationID,
	}, nil
}

func (projection ConversationProjection) ConversationType() im.ConversationType {
	return projection.conversationType
}
func (projection ConversationProjection) PlatformConversationID() im.ConversationID {
	return projection.platformConversation
}
func (projection ConversationProjection) ParentConversationID() im.ConversationID {
	return projection.parentConversation
}
func (projection ConversationProjection) RootMessageID() im.MessageID {
	return projection.rootMessage
}
func (projection ConversationProjection) AgentInvocationID() im.InvocationID {
	return projection.agentInvocation
}
func (projection ConversationProjection) IsZero() bool {
	return projection.conversationType == "" && projection.platformConversation.IsZero() &&
		projection.parentConversation.IsZero() && projection.rootMessage.IsZero() &&
		projection.agentInvocation.IsZero()
}
