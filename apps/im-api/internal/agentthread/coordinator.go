package agentthread

import (
	"context"
	"sync"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/agentstore"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
)

// ThreadResult separates the platform topology plan from provider transport evidence. The receipt
// does not grant child membership or advance an invocation by itself.
type ThreadResult struct {
	plan                 ThreadPlan
	providerConversation im.ProviderConversationRef
	providerReceipt      im.ProviderEffectReceipt
	replayed             bool
}

func (result ThreadResult) Plan() ThreadPlan { return result.plan }
func (result ThreadResult) ProviderConversation() im.ProviderConversationRef {
	return result.providerConversation
}
func (result ThreadResult) ProviderReceipt() im.ProviderEffectReceipt { return result.providerReceipt }
func (result ThreadResult) Replayed() bool                            { return result.replayed }

// LocalCoordinator is a zero-network vertical-slice coordinator. Its registry is process-local
// test state; production must persist the plan, ACL snapshots, command receipt, and provider
// reconciliation through the durable store before enabling real outbound traffic.
type LocalCoordinator struct {
	mu       sync.Mutex
	provider im.Provider
	opened   map[string]ThreadResult
}

func NewLocalCoordinator(provider im.Provider) (*LocalCoordinator, error) {
	if provider == nil {
		return nil, ErrInvalidMention
	}
	profile := provider.Profile()
	if profile.IsZero() || !profile.Supports(im.ProviderCapabilityGroupCreate) ||
		!profile.Supports(im.ProviderCapabilityTextSend) {
		return nil, ErrInvalidMention
	}
	return &LocalCoordinator{provider: provider, opened: make(map[string]ThreadResult)}, nil
}

func (coordinator *LocalCoordinator) Open(
	ctx context.Context,
	command MentionCommand,
) (ThreadResult, error) {
	if coordinator == nil || ctx == nil {
		return ThreadResult{}, ErrInvalidMention
	}
	command.ProviderProfile = coordinator.provider.Profile()
	plan, err := PlanMention(command)
	if err != nil {
		return ThreadResult{}, err
	}
	coordinator.mu.Lock()
	if existing, ok := coordinator.opened[plan.DedupeKey()]; ok {
		coordinator.mu.Unlock()
		existing.replayed = true
		return existing, nil
	}
	coordinator.mu.Unlock()

	providerConversation, receipt, err := coordinator.provider.CreateGroup(ctx, plan.ProviderGroup())
	if err != nil {
		return ThreadResult{}, err
	}
	if im.RequireCommittedProviderEffect(receipt) != nil || providerConversation.SubjectID() != plan.Child().Ref().ConversationID().String() {
		return ThreadResult{}, im.ErrProviderEffectUnknown
	}
	result := ThreadResult{
		plan: plan, providerConversation: providerConversation, providerReceipt: receipt,
	}
	coordinator.mu.Lock()
	if existing, ok := coordinator.opened[plan.DedupeKey()]; ok {
		coordinator.mu.Unlock()
		existing.replayed = true
		return existing, nil
	}
	coordinator.opened[plan.DedupeKey()] = result
	coordinator.mu.Unlock()
	return result, nil
}

func (coordinator *LocalCoordinator) SendAgentReply(
	ctx context.Context,
	thread ThreadResult,
	installation agentstore.InstallationSnapshot,
	clientMessage im.MessageID,
	text string,
	idempotencyKey string,
) (im.ProviderEffectReceipt, error) {
	if coordinator == nil || ctx == nil || thread.plan.IsZero() || thread.providerConversation.IsZero() {
		return im.ProviderEffectReceipt{}, ErrReplyOutsideThread
	}
	request, err := BuildAgentReply(
		thread.plan, installation, thread.providerConversation, clientMessage, text,
		coordinator.provider.Profile(), idempotencyKey,
	)
	if err != nil {
		return im.ProviderEffectReceipt{}, err
	}
	return coordinator.provider.SendText(ctx, request)
}
