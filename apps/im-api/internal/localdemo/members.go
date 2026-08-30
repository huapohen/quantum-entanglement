package localdemo

import (
	"context"
	"crypto/sha256"
	"errors"
	"sort"
	"strings"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
)

const maxMemberUpdates = 64

type AddMembersInput struct {
	MemberActorIDs []string `json:"memberActorIds"`
	IdempotencyKey string   `json:"idempotencyKey"`
}

type AddMembersResult struct {
	Conversation  ConversationView `json:"conversation"`
	AddedActorIDs []string         `json:"addedActorIds"`
	Replayed      bool             `json:"replayed"`
}

type memberUpdateRecord struct {
	digest       [sha256.Size]byte
	conversation im.ConversationID
	added        []string
}

func (service *Service) AddMembers(
	ctx context.Context,
	bearerToken string,
	conversationIDValue string,
	input AddMembersInput,
) (AddMembersResult, error) {
	if service == nil || ctx == nil || !validLocalID(input.IdempotencyKey) ||
		len(input.MemberActorIDs) == 0 || len(input.MemberActorIDs) > maxMemberUpdates {
		return AddMembersResult{}, ErrInvalidInput
	}
	if err := service.verifyRequester(ctx, bearerToken); err != nil {
		return AddMembersResult{}, err
	}
	conversationID, err := im.ParseConversationID(conversationIDValue)
	if err != nil {
		return AddMembersResult{}, ErrNotFound
	}
	memberIDs, err := service.parseMemberIDs(input.MemberActorIDs)
	if err != nil {
		return AddMembersResult{}, err
	}
	digest := sha256.Sum256([]byte("wanwork.local-demo-member-update/1\x00" +
		conversationID.String() + "\x00" + strings.Join(memberIDs, "\x00")))

	service.mu.Lock()
	defer service.mu.Unlock()
	conversation, ok := service.conversations[conversationID]
	if !ok {
		return AddMembersResult{}, ErrNotFound
	}
	if !service.canManageMembers(conversation) {
		return AddMembersResult{}, ErrForbidden
	}
	updateKey := conversationID.String() + "\x00" + input.IdempotencyKey
	if existing, ok := service.memberUpdates[updateKey]; ok {
		if existing.digest != digest {
			return AddMembersResult{}, ErrConflict
		}
		return AddMembersResult{
			Conversation:  service.conversationView(conversation),
			AddedActorIDs: append([]string(nil), existing.added...), Replayed: true,
		}, nil
	}

	added := make([]im.ActorID, 0, len(memberIDs))
	addedStrings := make([]string, 0, len(memberIDs))
	for _, value := range memberIDs {
		actorID, parseErr := im.ParseActorID(value)
		if parseErr != nil {
			return AddMembersResult{}, ErrInvalidInput
		}
		if _, exists := conversation.members[actorID]; exists {
			continue
		}
		added = append(added, actorID)
		addedStrings = append(addedStrings, value)
	}
	if len(added) > 0 && conversation.providerBound {
		receipt, providerErr := service.provider.AddMembers(ctx, im.ProviderMemberUpdate{
			Conversation: conversation.providerRef, MemberActors: append([]im.ActorID(nil), added...),
			IdempotencyKey: "demo/basic/members/" + conversationID.String() + "/" + input.IdempotencyKey,
		})
		if providerErr != nil {
			return AddMembersResult{}, errors.Join(ErrProvider, providerErr)
		}
		if im.RequireCommittedProviderEffect(receipt) != nil {
			return AddMembersResult{}, ErrProvider
		}
		conversation.providerStatus = string(receipt.Status)
	}
	for _, actorID := range added {
		actorRef, exists := service.knownActors[actorID]
		if !exists {
			return AddMembersResult{}, ErrInvalidInput
		}
		membership, membershipErr := im.NewConversationMembershipSnapshot(
			conversation.snapshot.Ref(), actorRef, im.ConversationMembershipMember,
			im.ConversationMembershipActive, 1,
		)
		if membershipErr != nil {
			return AddMembersResult{}, ErrInvalidInput
		}
		access, accessErr := im.NewConversationAccessSnapshot(
			conversation.snapshot.Ref(), actorRef,
			[]im.ConversationPermission{im.ConversationPermissionRead, im.ConversationPermissionSendMessage}, 1,
		)
		if accessErr != nil {
			return AddMembersResult{}, ErrInvalidInput
		}
		conversation.members[actorID] = membership
		conversation.access[actorID] = access
	}
	if containsActorID(added, service.installation.AgentActor()) {
		requesterAccess := conversation.access[service.requester.ActorID()]
		if !requesterAccess.HasPermission(im.ConversationPermissionInvokeAgent) {
			permissions := requesterAccess.Permissions()
			permissions = append(permissions, im.ConversationPermissionInvokeAgent)
			updatedAccess, accessErr := im.NewConversationAccessSnapshot(
				conversation.snapshot.Ref(), service.requester, permissions, requesterAccess.Revision()+1,
			)
			if accessErr != nil {
				return AddMembersResult{}, ErrIntegrity
			}
			conversation.access[service.requester.ActorID()] = updatedAccess
		}
	}
	service.memberUpdates[updateKey] = memberUpdateRecord{
		digest: digest, conversation: conversationID, added: append([]string(nil), addedStrings...),
	}
	return AddMembersResult{
		Conversation: service.conversationView(conversation), AddedActorIDs: addedStrings,
	}, nil
}

func (service *Service) parseMemberIDs(values []string) ([]string, error) {
	seen := make(map[string]struct{}, len(values))
	memberIDs := make([]string, 0, len(values))
	for _, value := range values {
		actorID, err := im.ParseActorID(value)
		if err != nil || actorID == service.requester.ActorID() {
			return nil, ErrInvalidInput
		}
		if _, exists := seen[value]; exists {
			return nil, ErrInvalidInput
		}
		if _, known := service.knownActors[actorID]; !known {
			return nil, ErrInvalidInput
		}
		seen[value] = struct{}{}
		memberIDs = append(memberIDs, value)
	}
	sort.Strings(memberIDs)
	return memberIDs, nil
}

func (service *Service) canManageMembers(conversation *localConversation) bool {
	access, ok := conversation.access[service.requester.ActorID()]
	return ok && access.HasPermission(im.ConversationPermissionManageMembers) && service.canRead(conversation)
}
