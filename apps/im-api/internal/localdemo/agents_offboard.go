package localdemo

import (
	"context"
	"errors"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/agentstore"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
)

// AgentStoreOffboardInput makes cleanup policy explicit. The local demo accepts the same
// retention choices as the domain contract, while all provider/membership/credential cleanup
// flags are fixed to true so a partial offboard cannot be represented as a successful action.
type AgentStoreOffboardInput struct {
	IdempotencyKey  string `json:"idempotencyKey"`
	DataDisposition string `json:"dataDisposition"`
}

type AgentStoreOffboardResult struct {
	Agent                  AgentStoreView `json:"agent"`
	RemovedConversationIDs []string       `json:"removedConversationIds"`
	Replayed               bool           `json:"replayed"`
}

type agentOffboardRecord struct {
	digest agentstore.SHA256Digest
	result AgentStoreOffboardResult
}

func (service *Service) OffboardAgent(
	ctx context.Context,
	bearerToken string,
	definitionIDValue string,
	input AgentStoreOffboardInput,
) (AgentStoreOffboardResult, error) {
	if service == nil || ctx == nil || !validLocalID(input.IdempotencyKey) {
		return AgentStoreOffboardResult{}, ErrInvalidInput
	}
	disposition, err := parseDataDisposition(input.DataDisposition)
	if err != nil {
		return AgentStoreOffboardResult{}, err
	}
	if err := service.verifyRequester(ctx, bearerToken); err != nil {
		return AgentStoreOffboardResult{}, err
	}
	definitionID, err := im.ParseAgentDefinitionID(definitionIDValue)
	if err != nil {
		return AgentStoreOffboardResult{}, ErrNotFound
	}

	service.mu.Lock()
	defer service.mu.Unlock()
	targetIndex := -1
	for index, record := range service.agentCatalog {
		if !record.passport.IsZero() && record.passport.Definition().ID() == definitionID {
			targetIndex = index
			break
		}
	}
	if targetIndex < 0 {
		return AgentStoreOffboardResult{}, ErrNotFound
	}
	target := service.agentCatalog[targetIndex]
	if target.installation.IsZero() {
		return AgentStoreOffboardResult{}, ErrConflict
	}
	requestKey := definitionIDValue + "\x00" + input.IdempotencyKey
	digest := agentstore.DigestBytes([]byte("wanwork.local-demo-agent-offboard/1\x00" +
		definitionIDValue + "\x00" + target.installation.ID().String() + "\x00" + input.DataDisposition))
	if existing, ok := service.agentOffboardRequests[requestKey]; ok {
		if existing.digest != digest {
			return AgentStoreOffboardResult{}, ErrConflict
		}
		replayed := existing.result
		replayed.Replayed = true
		return replayed, nil
	}
	if target.installation.Status() == agentstore.InstallationOffboarded {
		return AgentStoreOffboardResult{}, ErrConflict
	}
	if !service.provider.Profile().Supports(im.ProviderCapabilityUserRevoke) {
		return AgentStoreOffboardResult{}, ErrProvider
	}
	memberProvider := im.MemberRemovalProvider(service.provider)
	request, err := agentstore.NewOffboardingRequest(
		target.installation, target.installation.InstalledBy(), service.nowUTC(),
		true, true, true, true, disposition, digest,
	)
	if err != nil {
		return AgentStoreOffboardResult{}, ErrIntegrity
	}
	actorID := target.installation.AgentActor()
	removedConversationIDs := make([]string, 0)
	for _, conversationID := range service.conversationOrder {
		conversation, exists := service.conversations[conversationID]
		if !exists {
			return AgentStoreOffboardResult{}, ErrIntegrity
		}
		if _, member := conversation.members[actorID]; !member {
			continue
		}
		if conversation.providerBound {
			receipt, providerErr := memberProvider.RemoveMembers(ctx, im.ProviderMemberUpdate{
				Conversation: conversation.providerRef, MemberActors: []im.ActorID{actorID},
				IdempotencyKey: "demo/store/offboard-members/" + request.InstallationID().String() + "/" + conversationID.String(),
			})
			if providerErr != nil || receipt.Validate() != nil ||
				(receipt.Status != im.ProviderEffectCommitted && receipt.Status != im.ProviderEffectReplayed) {
				if providerErr != nil {
					return AgentStoreOffboardResult{}, errors.Join(ErrProvider, providerErr)
				}
				return AgentStoreOffboardResult{}, ErrProvider
			}
		}
		removedConversationIDs = append(removedConversationIDs, conversationID.String())
	}
	userProvider := im.UserLifecycleProvider(service.provider)
	revokeReceipt, providerErr := userProvider.RevokeUser(ctx, im.ProviderUserRevoke{
		Actor: actorID, IdempotencyKey: "demo/store/offboard-user/" + request.InstallationID().String(),
	})
	if providerErr != nil || revokeReceipt.Validate() != nil ||
		(revokeReceipt.Status != im.ProviderEffectCommitted && revokeReceipt.Status != im.ProviderEffectReplayed) {
		if providerErr != nil {
			return AgentStoreOffboardResult{}, errors.Join(ErrProvider, providerErr)
		}
		return AgentStoreOffboardResult{}, ErrProvider
	}
	transitioned, err := agentstore.TransitionInstallation(
		target.installation, agentstore.InstallationOffboarded, service.nowUTC(), target.installation.Revision()+1,
	)
	if err != nil {
		return AgentStoreOffboardResult{}, ErrIntegrity
	}
	for _, conversationID := range service.conversationOrder {
		conversation := service.conversations[conversationID]
		delete(conversation.members, actorID)
		delete(conversation.access, actorID)
	}
	service.agentCatalog[targetIndex].installation = transitioned
	if service.installation.ID() == target.installation.ID() {
		service.installation = transitioned
	}
	if !service.hasActiveInstallation() {
		service.removeInvokePermission()
	}
	result := AgentStoreOffboardResult{
		Agent:                  service.agentStoreView(service.agentCatalog[targetIndex]),
		RemovedConversationIDs: append([]string(nil), removedConversationIDs...),
	}
	service.agentOffboardRequests[requestKey] = agentOffboardRecord{digest: digest, result: result}
	return result, nil
}

func parseDataDisposition(value string) (agentstore.DataDisposition, error) {
	disposition := agentstore.DataDisposition(value)
	if !disposition.Valid() {
		return "", ErrInvalidInput
	}
	return disposition, nil
}

func (service *Service) hasActiveInstallation() bool {
	for _, record := range service.agentCatalog {
		if !record.installation.IsZero() && record.installation.Status() == agentstore.InstallationActive {
			return true
		}
	}
	return false
}

func (service *Service) removeInvokePermission() {
	for _, conversation := range service.conversations {
		access, ok := conversation.access[service.requester.ActorID()]
		if !ok || !access.HasPermission(im.ConversationPermissionInvokeAgent) {
			continue
		}
		permissions := make([]im.ConversationPermission, 0, len(access.Permissions()))
		for _, permission := range access.Permissions() {
			if permission != im.ConversationPermissionInvokeAgent {
				permissions = append(permissions, permission)
			}
		}
		updated, err := im.NewConversationAccessSnapshot(
			conversation.snapshot.Ref(), service.requester, permissions, access.Revision()+1,
		)
		if err == nil {
			conversation.access[service.requester.ActorID()] = updated
		}
	}
}
