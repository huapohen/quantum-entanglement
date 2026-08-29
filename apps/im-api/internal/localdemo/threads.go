package localdemo

import (
	"crypto/sha256"
	"encoding/hex"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/agentthread"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
)

func (service *Service) canInvoke(conversation *localConversation) bool {
	if conversation == nil {
		return false
	}
	access, ok := conversation.access[service.requester.ActorID()]
	if !ok || !access.HasPermission(im.ConversationPermissionInvokeAgent) || !service.canRead(conversation) {
		return false
	}
	agentMembership, ok := conversation.members[service.installation.AgentActor()]
	return ok && agentMembership.Status() == im.ConversationMembershipActive
}

// materializeThread writes the provider-neutral child and parent-card projections after the
// provider group and Agent reply have been accepted. The local demo keeps this in memory; a
// production composition must commit the same records through the tenant-bound UoW before any
// external provider effect is considered durable.
func (service *Service) materializeThread(
	thread agentthread.ThreadResult,
	replyMessageID im.MessageID,
	replyText string,
	receipt im.ProviderEffectReceipt,
) error {
	if service == nil || thread.Plan().IsZero() || replyMessageID.IsZero() || replyText == "" || receipt.Validate() != nil {
		return ErrIntegrity
	}
	plan := thread.Plan()
	child := plan.Child()
	parentID := child.ParentConversationID()
	service.mu.Lock()
	defer service.mu.Unlock()
	if _, exists := service.conversations[child.Ref().ConversationID()]; exists {
		return nil
	}
	parentRecord, exists := service.conversations[parentID]
	if !exists {
		return ErrIntegrity
	}
	agentRef, exists := service.knownActors[service.installation.AgentActor()]
	if !exists {
		return ErrIntegrity
	}
	childRecord := &localConversation{
		snapshot: child,
		name:     "Agent · " + parentRecord.name,
		members: map[im.ActorID]im.ConversationMembershipSnapshot{
			service.requester.ActorID():       plan.HumanMembership(),
			service.installation.AgentActor(): plan.AgentMembership(),
		},
		access: map[im.ActorID]im.ConversationAccessSnapshot{
			service.requester.ActorID():       plan.HumanAccess(),
			service.installation.AgentActor(): plan.AgentAccess(),
		},
		providerRef: thread.ProviderConversation(), providerBound: true,
		providerStatus: string(receipt.Status), createdAt: service.nowUTC(),
		messages: make([]localMessage, 0, 1), byClient: make(map[im.MessageID]int),
	}
	messageRef, err := im.NewMessageRef(child.Ref(), replyMessageID)
	if err != nil {
		return ErrIntegrity
	}
	replySnapshot, err := im.NewMessageSnapshot(
		messageRef, agentRef, replyMessageID, im.MessageTypeText, im.MessageStatusActive,
		replyText, `{"messageType":"agent_reply"}`, childRecord.createdAt, 1,
	)
	if err != nil {
		return ErrIntegrity
	}
	childRecord.byClient[replyMessageID] = 0
	childRecord.messages = append(childRecord.messages, localMessage{
		snapshot: replySnapshot, providerMessageID: receipt.ExternalID, providerStatus: string(receipt.Status),
	})
	service.conversations[child.Ref().ConversationID()] = childRecord
	service.conversationOrder = append(service.conversationOrder, child.Ref().ConversationID())

	workCard, err := agentthread.EncodeParentWorkCard(plan.ParentCard())
	if err != nil {
		return ErrIntegrity
	}
	cardDigest := sha256.Sum256([]byte("wanwork.local-demo-parent-card/1\x00" + child.Ref().ConversationID().String()))
	cardMessageID, err := im.ParseMessageID("msg_card_" + hex.EncodeToString(cardDigest[:12]))
	if err != nil {
		return ErrIntegrity
	}
	cardRef, err := im.NewMessageRef(parentRecord.snapshot.Ref(), cardMessageID)
	if err != nil {
		return ErrIntegrity
	}
	cardText := "Agent 工作卡已创建：子群 " + child.Ref().ConversationID().String()
	cardSnapshot, err := im.NewMessageSnapshot(
		cardRef, service.requester, cardMessageID, im.MessageTypeText, im.MessageStatusActive,
		cardText, workCard, parentRecord.createdAt, uint64(len(parentRecord.messages)+1),
	)
	if err != nil {
		return ErrIntegrity
	}
	parentRecord.byClient[cardMessageID] = len(parentRecord.messages)
	parentRecord.messages = append(parentRecord.messages, localMessage{
		snapshot: cardSnapshot, providerStatus: "local-only",
	})
	return nil
}
