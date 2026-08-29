// Package agentthread owns the @Agent -> child conversation boundary. Parent topology never
// grants child access: every child membership and permission snapshot is created explicitly.
package agentthread

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"strings"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/agentstore"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/immetadata"
)

var (
	ErrInvalidMention      = errors.New("invalid Agent mention")
	ErrMentionUnauthorized = errors.New("Agent mention unauthorized")
	ErrThreadConflict      = errors.New("Agent thread idempotency conflict")
	ErrReplyOutsideThread  = errors.New("Agent reply target is not its child thread")
)

const contractVersion = "wanwork.agent-thread/v1"

type MentionCommand struct {
	Parent            im.ConversationSnapshot
	RequestingActor   im.ActorRef
	RequestingAccess  im.ConversationAccessSnapshot
	RootMessage       im.MessageID
	AgentInstallation agentstore.InstallationSnapshot
	ProviderProfile   im.ProviderProfile
}

type ThreadPlan struct {
	dedupeKey       string
	requestDigest   [sha256.Size]byte
	child           im.ConversationSnapshot
	invocationID    im.InvocationID
	humanMembership im.ConversationMembershipSnapshot
	agentMembership im.ConversationMembershipSnapshot
	humanAccess     im.ConversationAccessSnapshot
	agentAccess     im.ConversationAccessSnapshot
	providerGroup   im.ProviderGroupCreate
	parentCard      ParentWorkCard
}

func PlanMention(command MentionCommand) (ThreadPlan, error) {
	if err := validateMentionCommand(command); err != nil {
		return ThreadPlan{}, err
	}
	workspace, _ := command.Parent.WorkspaceID()
	digest := mentionDigest(command, workspace)
	digestText := hex.EncodeToString(digest[:])
	childID, err := im.ParseConversationID("cnv_at_" + digestText[:32])
	if err != nil {
		return ThreadPlan{}, ErrInvalidMention
	}
	invocationID, err := im.ParseInvocationID("inv_at_" + digestText[32:64])
	if err != nil {
		return ThreadPlan{}, ErrInvalidMention
	}
	childRef, err := im.NewConversationRef(command.Parent.Ref().TenantID(), childID)
	if err != nil {
		return ThreadPlan{}, ErrInvalidMention
	}
	child, err := im.NewConversationSnapshot(
		childRef, &workspace, im.ConversationAgentThread, im.ConversationActive,
		command.Parent.Ref().ConversationID(), command.RootMessage, invocationID, 1,
	)
	if err != nil {
		return ThreadPlan{}, ErrInvalidMention
	}
	agentRef, err := im.NewActorRef(command.Parent.Ref().TenantID(), command.AgentInstallation.AgentActor())
	if err != nil {
		return ThreadPlan{}, ErrInvalidMention
	}
	humanMembership, err := im.NewConversationMembershipSnapshot(
		childRef, command.RequestingActor, im.ConversationMembershipOwner,
		im.ConversationMembershipActive, 1,
	)
	if err != nil {
		return ThreadPlan{}, ErrInvalidMention
	}
	agentMembership, err := im.NewConversationMembershipSnapshot(
		childRef, agentRef, im.ConversationMembershipMember,
		im.ConversationMembershipActive, 1,
	)
	if err != nil {
		return ThreadPlan{}, ErrInvalidMention
	}
	humanAccess, err := im.NewConversationAccessSnapshot(
		childRef, command.RequestingActor,
		[]im.ConversationPermission{
			im.ConversationPermissionRead, im.ConversationPermissionSendMessage,
			im.ConversationPermissionManageMembers, im.ConversationPermissionManageConversation,
		}, 1,
	)
	if err != nil {
		return ThreadPlan{}, ErrInvalidMention
	}
	agentAccess, err := im.NewConversationAccessSnapshot(
		childRef, agentRef,
		[]im.ConversationPermission{
			im.ConversationPermissionRead, im.ConversationPermissionSendMessage,
			im.ConversationPermissionPublishArtifactReference,
		}, 1,
	)
	if err != nil {
		return ThreadPlan{}, ErrInvalidMention
	}
	projection, err := immetadata.NewConversationProjection(
		im.ConversationAgentThread, childID, command.Parent.Ref().ConversationID(),
		command.RootMessage, invocationID,
	)
	if err != nil {
		return ThreadPlan{}, ErrInvalidMention
	}
	extInfo, err := immetadata.EncodeConversationProjection(projection)
	if err != nil {
		return ThreadPlan{}, ErrInvalidMention
	}
	providerGroup := im.ProviderGroupCreate{
		Conversation: childRef, ExtInfo: extInfo,
		MemberActors:   []im.ActorID{command.RequestingActor.ActorID(), command.AgentInstallation.AgentActor()},
		IdempotencyKey: "agent-thread/group/" + digestText[:32],
	}
	if err := providerGroup.Validate(command.ProviderProfile); err != nil {
		return ThreadPlan{}, ErrInvalidMention
	}
	card, err := NewParentWorkCard(
		command.Parent.Ref(), childRef, invocationID, command.AgentInstallation.AgentActor(),
		WorkCardStarted,
	)
	if err != nil {
		return ThreadPlan{}, ErrInvalidMention
	}
	return ThreadPlan{
		dedupeKey: "agent-thread:" + digestText, requestDigest: digest,
		child: child, invocationID: invocationID,
		humanMembership: humanMembership, agentMembership: agentMembership,
		humanAccess: humanAccess, agentAccess: agentAccess,
		providerGroup: providerGroup, parentCard: card,
	}, nil
}

func (plan ThreadPlan) DedupeKey() string              { return plan.dedupeKey }
func (plan ThreadPlan) RequestDigestHex() string       { return hex.EncodeToString(plan.requestDigest[:]) }
func (plan ThreadPlan) Child() im.ConversationSnapshot { return plan.child }
func (plan ThreadPlan) InvocationID() im.InvocationID  { return plan.invocationID }
func (plan ThreadPlan) HumanMembership() im.ConversationMembershipSnapshot {
	return plan.humanMembership
}
func (plan ThreadPlan) AgentMembership() im.ConversationMembershipSnapshot {
	return plan.agentMembership
}
func (plan ThreadPlan) HumanAccess() im.ConversationAccessSnapshot { return plan.humanAccess }
func (plan ThreadPlan) AgentAccess() im.ConversationAccessSnapshot { return plan.agentAccess }
func (plan ThreadPlan) ProviderGroup() im.ProviderGroupCreate {
	value := plan.providerGroup
	value.MemberActors = append([]im.ActorID(nil), plan.providerGroup.MemberActors...)
	return value
}
func (plan ThreadPlan) ParentCard() ParentWorkCard { return plan.parentCard }
func (plan ThreadPlan) IsZero() bool {
	return plan.dedupeKey == "" && plan.requestDigest == [sha256.Size]byte{} &&
		plan.child.IsZero() && plan.invocationID.IsZero() && plan.humanMembership.IsZero() &&
		plan.agentMembership.IsZero() && plan.humanAccess.IsZero() && plan.agentAccess.IsZero() &&
		plan.providerGroup.Conversation.IsZero() && plan.parentCard.IsZero()
}

func BuildAgentReply(
	plan ThreadPlan,
	installation agentstore.InstallationSnapshot,
	providerConversation im.ProviderConversationRef,
	clientMessage im.MessageID,
	text string,
	profile im.ProviderProfile,
	idempotencyKey string,
) (im.ProviderTextMessage, error) {
	if plan.IsZero() || installation.IsZero() || installation.Status() != agentstore.InstallationActive ||
		installation.AgentActor() != plan.agentAccess.ActorRef().ActorID() ||
		plan.agentAccess.ConversationRef() != plan.child.Ref() ||
		!plan.agentAccess.HasPermission(im.ConversationPermissionSendMessage) ||
		providerConversation.SubjectID() != plan.child.Ref().ConversationID().String() {
		return im.ProviderTextMessage{}, ErrReplyOutsideThread
	}
	request := im.ProviderTextMessage{
		Conversation: providerConversation, Sender: installation.AgentActor(),
		ClientMessage: clientMessage, Text: text, IdempotencyKey: idempotencyKey,
	}
	if err := request.Validate(profile); err != nil {
		return im.ProviderTextMessage{}, ErrInvalidMention
	}
	return request, nil
}

func validateMentionCommand(command MentionCommand) error {
	workspace, hasWorkspace := command.Parent.WorkspaceID()
	installation := command.AgentInstallation
	if command.Parent.IsZero() || command.Parent.ConversationType() != im.ConversationGroup ||
		command.Parent.Status() != im.ConversationActive || !hasWorkspace || workspace.IsZero() ||
		command.RequestingActor.IsZero() || command.RequestingActor.TenantID() != command.Parent.Ref().TenantID() ||
		command.RequestingAccess.IsZero() || command.RequestingAccess.ActorRef() != command.RequestingActor ||
		command.RequestingAccess.ConversationRef() != command.Parent.Ref() || command.RootMessage.IsZero() ||
		installation.IsZero() || installation.Status() != agentstore.InstallationActive ||
		installation.TenantID() != command.Parent.Ref().TenantID() || installation.WorkspaceID() != workspace ||
		command.ProviderProfile.Provider != im.IdentityProviderRongCloud ||
		!command.ProviderProfile.Supports(im.ProviderCapabilityGroupCreate) {
		return ErrInvalidMention
	}
	if !command.RequestingAccess.HasPermission(im.ConversationPermissionRead) ||
		!command.RequestingAccess.HasPermission(im.ConversationPermissionSendMessage) ||
		!command.RequestingAccess.HasPermission(im.ConversationPermissionInvokeAgent) {
		return ErrMentionUnauthorized
	}
	conversationRead, err := agentstore.ParseCapability("conversation.read")
	if err != nil || !installation.CanInvoke(conversationRead) {
		return ErrMentionUnauthorized
	}
	return nil
}

func mentionDigest(command MentionCommand, workspace im.WorkspaceID) [sha256.Size]byte {
	parts := []string{
		contractVersion,
		command.Parent.Ref().TenantID().String(), workspace.String(),
		command.Parent.Ref().ConversationID().String(), command.RootMessage.String(),
		command.RequestingActor.ActorID().String(), command.AgentInstallation.ID().String(),
		command.AgentInstallation.ReleaseID().String(), command.AgentInstallation.AgentActor().String(),
	}
	return sha256.Sum256([]byte(strings.Join(parts, "\x00")))
}
