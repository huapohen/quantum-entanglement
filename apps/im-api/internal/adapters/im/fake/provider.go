// Package fake provides a deterministic, zero-network RongCloud-shaped provider for local
// contract and vertical-slice tests. It stores no credentials and never represents a successful
// fake effect as proof of production provider delivery.
package fake

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/immetadata"
)

var (
	ErrClosed         = errors.New("fake IM provider is closed")
	ErrGroupMissing   = errors.New("fake IM provider group not found")
	ErrMessageMissing = errors.New("fake IM provider message not found")
	ErrUserMissing    = errors.New("fake IM provider user not provisioned")
)

type Clock func() time.Time

type Options struct {
	Realm         im.ProviderRealmID
	AllowOutbound bool
	Now           Clock
}

type user struct {
	actor       im.ActorID
	name        string
	extInfo     string
	requestHash string
	revoked     bool
}

type group struct {
	conversation im.ConversationRef
	extInfo      string
	members      map[im.ActorID]struct{}
	requestHash  string
}

type sentMessage struct {
	request  im.ProviderTextMessage
	receipt  im.ProviderEffectReceipt
	recalled bool
}

type Provider struct {
	mu            sync.Mutex
	profile       im.ProviderProfile
	allowOutbound bool
	now           Clock
	closed        bool
	users         map[im.ActorID]user
	groups        map[string]group
	effects       map[string]im.ProviderEffectReceipt
	effectHashes  map[string]string
	inbound       []im.InboundMessage
	sent          []sentMessage
	nextMessage   uint64
}

func New(options Options) (*Provider, error) {
	if options.Realm.IsZero() {
		return nil, im.ErrInvalidProviderRequest
	}
	clock := options.Now
	if clock == nil {
		clock = func() time.Time { return time.Now().UTC() }
	}
	capabilities := []im.ProviderCapability{
		im.ProviderCapabilityHealth,
		im.ProviderCapabilityInboundRead,
		im.ProviderCapabilityCursorResume,
		im.ProviderCapabilityUserProvision,
		im.ProviderCapabilityUserRevoke,
		im.ProviderCapabilityGroupCreate,
		im.ProviderCapabilityMemberWrite,
	}
	if options.AllowOutbound {
		capabilities = append(capabilities,
			im.ProviderCapabilityTextSend,
			im.ProviderCapabilityTextEdit,
			im.ProviderCapabilityMessageRecall,
		)
	}
	profile, err := im.NewProviderProfile(
		im.IdentityProviderRongCloud,
		options.Realm,
		capabilities,
		im.ProviderMaxTextBytes,
		im.ProviderMaxTextBytes,
		1024,
	)
	if err != nil {
		return nil, err
	}
	return &Provider{
		profile: profile, allowOutbound: options.AllowOutbound, now: clock,
		users: make(map[im.ActorID]user), groups: make(map[string]group),
		effects:      make(map[string]im.ProviderEffectReceipt),
		effectHashes: make(map[string]string),
	}, nil
}

func (provider *Provider) Profile() im.ProviderProfile {
	if provider == nil {
		return im.ProviderProfile{}
	}
	provider.mu.Lock()
	defer provider.mu.Unlock()
	profile := provider.profile
	profile.Capabilities = append([]im.ProviderCapability(nil), profile.Capabilities...)
	return profile
}

func (provider *Provider) Health(ctx context.Context) error {
	if ctx == nil {
		return im.ErrInvalidProviderRequest
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	provider.mu.Lock()
	defer provider.mu.Unlock()
	if provider.closed {
		return ErrClosed
	}
	return nil
}

func (provider *Provider) Close() {
	if provider == nil {
		return
	}
	provider.mu.Lock()
	provider.closed = true
	provider.mu.Unlock()
}

func (provider *Provider) ProvisionUser(
	ctx context.Context,
	request im.ProviderUserProvision,
) (im.ExternalIdentityRef, im.ProviderEffectReceipt, error) {
	if err := provider.checkContext(ctx); err != nil {
		return im.ExternalIdentityRef{}, im.ProviderEffectReceipt{}, err
	}
	if err := request.Validate(provider.Profile()); err != nil {
		return im.ExternalIdentityRef{}, im.ProviderEffectReceipt{}, err
	}
	projection, err := immetadata.DecodeUserProjection(request.ExtInfo)
	if err != nil || projection.PlatformActorID() != request.Actor {
		return im.ExternalIdentityRef{}, im.ProviderEffectReceipt{}, im.ErrInvalidProviderRequest
	}
	hash := requestHash("user", request.Actor.String(), request.DisplayName, request.ExtInfo)
	provider.mu.Lock()
	defer provider.mu.Unlock()
	if existing, ok := provider.effects[request.IdempotencyKey]; ok {
		if provider.effectHashes[request.IdempotencyKey] != hash {
			return im.ExternalIdentityRef{}, im.ProviderEffectReceipt{}, im.ErrProviderConflict
		}
		identity, identityErr := im.NewExternalIdentityRef(
			im.IdentityProviderRongCloud, provider.profile.Realm, request.Actor.String(),
		)
		if identityErr != nil {
			return im.ExternalIdentityRef{}, im.ProviderEffectReceipt{}, im.ErrInvalidProviderRequest
		}
		existing.Status = im.ProviderEffectReplayed
		return identity, existing, nil
	}
	provider.users[request.Actor] = user{
		actor: request.Actor, name: request.DisplayName, extInfo: request.ExtInfo,
		requestHash: hash,
	}
	receipt := provider.receiptLocked(request.IdempotencyKey, request.Actor.String(), im.ProviderEffectCommitted)
	provider.effects[request.IdempotencyKey] = receipt
	provider.effectHashes[request.IdempotencyKey] = hash
	identity, err := im.NewExternalIdentityRef(
		im.IdentityProviderRongCloud, provider.profile.Realm, request.Actor.String(),
	)
	if err != nil {
		return im.ExternalIdentityRef{}, im.ProviderEffectReceipt{}, im.ErrInvalidProviderRequest
	}
	return identity, receipt, nil
}

// RevokeUser is a deterministic fake provider-side identity revocation. Revoke is explicit and
// idempotent; a platform installation must not transition to offboarded unless this receipt is
// committed. A later explicit ProvisionUser call creates a new provider generation for tests.
func (provider *Provider) RevokeUser(
	ctx context.Context,
	request im.ProviderUserRevoke,
) (im.ProviderEffectReceipt, error) {
	if err := provider.checkContext(ctx); err != nil {
		return im.ProviderEffectReceipt{}, err
	}
	profile := provider.Profile()
	if err := request.Validate(profile); err != nil {
		return im.ProviderEffectReceipt{}, err
	}
	if !provider.allowOutbound {
		return im.ProviderEffectReceipt{}, im.ErrProviderOutboundDisabled
	}
	hash := requestHash("revoke-user", request.Actor.String())
	provider.mu.Lock()
	defer provider.mu.Unlock()
	if existing, ok := provider.effects[request.IdempotencyKey]; ok {
		if provider.effectHashes[request.IdempotencyKey] != hash {
			return im.ProviderEffectReceipt{}, im.ErrProviderConflict
		}
		existing.Status = im.ProviderEffectReplayed
		return existing, nil
	}
	identity, exists := provider.users[request.Actor]
	if !exists {
		return im.ProviderEffectReceipt{}, ErrUserMissing
	}
	identity.revoked = true
	provider.users[request.Actor] = identity
	receipt := provider.receiptLocked(request.IdempotencyKey, request.Actor.String(), im.ProviderEffectCommitted)
	provider.effects[request.IdempotencyKey] = receipt
	provider.effectHashes[request.IdempotencyKey] = hash
	return receipt, nil
}

func (provider *Provider) CreateGroup(
	ctx context.Context,
	request im.ProviderGroupCreate,
) (im.ProviderConversationRef, im.ProviderEffectReceipt, error) {
	if err := provider.checkContext(ctx); err != nil {
		return im.ProviderConversationRef{}, im.ProviderEffectReceipt{}, err
	}
	if err := request.Validate(provider.Profile()); err != nil {
		return im.ProviderConversationRef{}, im.ProviderEffectReceipt{}, err
	}
	projection, err := immetadata.DecodeConversationProjection(request.ExtInfo)
	if err != nil || projection.PlatformConversationID() != request.Conversation.ConversationID() {
		return im.ProviderConversationRef{}, im.ProviderEffectReceipt{}, im.ErrInvalidProviderRequest
	}
	hashParts := []string{"group", request.Conversation.ConversationID().String(), request.ExtInfo}
	for _, actor := range request.MemberActors {
		hashParts = append(hashParts, actor.String())
	}
	hash := requestHash(hashParts...)
	provider.mu.Lock()
	defer provider.mu.Unlock()
	if existing, ok := provider.effects[request.IdempotencyKey]; ok {
		if provider.effectHashes[request.IdempotencyKey] != hash {
			return im.ProviderConversationRef{}, im.ProviderEffectReceipt{}, im.ErrProviderConflict
		}
		reference, referenceErr := im.NewProviderConversationRef(
			im.IdentityProviderRongCloud, provider.profile.Realm,
			request.Conversation.ConversationID().String(),
		)
		if referenceErr != nil {
			return im.ProviderConversationRef{}, im.ProviderEffectReceipt{}, im.ErrInvalidProviderRequest
		}
		existing.Status = im.ProviderEffectReplayed
		return reference, existing, nil
	}
	members := make(map[im.ActorID]struct{}, len(request.MemberActors))
	for _, actor := range request.MemberActors {
		user, exists := provider.users[actor]
		if !exists || user.revoked {
			return im.ProviderConversationRef{}, im.ProviderEffectReceipt{}, ErrUserMissing
		}
		members[actor] = struct{}{}
	}
	provider.groups[request.Conversation.ConversationID().String()] = group{
		conversation: request.Conversation, extInfo: request.ExtInfo,
		members: members, requestHash: hash,
	}
	receipt := provider.receiptLocked(
		request.IdempotencyKey, request.Conversation.ConversationID().String(),
		im.ProviderEffectCommitted,
	)
	provider.effects[request.IdempotencyKey] = receipt
	provider.effectHashes[request.IdempotencyKey] = hash
	reference, err := im.NewProviderConversationRef(
		im.IdentityProviderRongCloud, provider.profile.Realm,
		request.Conversation.ConversationID().String(),
	)
	if err != nil {
		return im.ProviderConversationRef{}, im.ProviderEffectReceipt{}, im.ErrInvalidProviderRequest
	}
	return reference, receipt, nil
}

func (provider *Provider) AddMembers(
	ctx context.Context,
	request im.ProviderMemberUpdate,
) (im.ProviderEffectReceipt, error) {
	if err := provider.checkContext(ctx); err != nil {
		return im.ProviderEffectReceipt{}, err
	}
	if err := request.ValidateForProfile(provider.Profile()); err != nil {
		return im.ProviderEffectReceipt{}, err
	}
	hashParts := []string{"members", request.Conversation.SubjectID()}
	for _, actor := range request.MemberActors {
		hashParts = append(hashParts, actor.String())
	}
	hash := requestHash(hashParts...)
	provider.mu.Lock()
	defer provider.mu.Unlock()
	if existing, ok := provider.effects[request.IdempotencyKey]; ok {
		if provider.effectHashes[request.IdempotencyKey] != hash {
			return im.ProviderEffectReceipt{}, im.ErrProviderConflict
		}
		existing.Status = im.ProviderEffectReplayed
		return existing, nil
	}
	key := request.Conversation.SubjectID()
	current, ok := provider.groups[key]
	if !ok {
		return im.ProviderEffectReceipt{}, ErrGroupMissing
	}
	for _, actor := range request.MemberActors {
		user, exists := provider.users[actor]
		if !exists || user.revoked {
			return im.ProviderEffectReceipt{}, ErrUserMissing
		}
		current.members[actor] = struct{}{}
	}
	provider.groups[key] = current
	receipt := provider.receiptLocked(request.IdempotencyKey, key, im.ProviderEffectCommitted)
	provider.effects[request.IdempotencyKey] = receipt
	provider.effectHashes[request.IdempotencyKey] = hash
	return receipt, nil
}

// RemoveMembers is the provider-side half of Agent offboarding. Removing an already absent
// member is a committed no-op, while replay/conflict semantics remain bound to the exact request.
func (provider *Provider) RemoveMembers(
	ctx context.Context,
	request im.ProviderMemberUpdate,
) (im.ProviderEffectReceipt, error) {
	if err := provider.checkContext(ctx); err != nil {
		return im.ProviderEffectReceipt{}, err
	}
	if err := request.ValidateForProfile(provider.Profile()); err != nil {
		return im.ProviderEffectReceipt{}, err
	}
	hashParts := []string{"remove-members", request.Conversation.SubjectID()}
	for _, actor := range request.MemberActors {
		hashParts = append(hashParts, actor.String())
	}
	hash := requestHash(hashParts...)
	provider.mu.Lock()
	defer provider.mu.Unlock()
	if existing, ok := provider.effects[request.IdempotencyKey]; ok {
		if provider.effectHashes[request.IdempotencyKey] != hash {
			return im.ProviderEffectReceipt{}, im.ErrProviderConflict
		}
		existing.Status = im.ProviderEffectReplayed
		return existing, nil
	}
	key := request.Conversation.SubjectID()
	current, ok := provider.groups[key]
	if !ok {
		return im.ProviderEffectReceipt{}, ErrGroupMissing
	}
	for _, actor := range request.MemberActors {
		delete(current.members, actor)
	}
	provider.groups[key] = current
	receipt := provider.receiptLocked(request.IdempotencyKey, key, im.ProviderEffectCommitted)
	provider.effects[request.IdempotencyKey] = receipt
	provider.effectHashes[request.IdempotencyKey] = hash
	return receipt, nil
}

func (provider *Provider) ReadInbound(
	ctx context.Context,
	cursor string,
	limit int,
) (im.InboundPage, error) {
	if err := provider.checkContext(ctx); err != nil {
		return im.InboundPage{}, err
	}
	if limit <= 0 || limit > 100 || len(cursor) > im.ProviderMaxCursorBytes {
		return im.InboundPage{}, im.ErrInvalidProviderRequest
	}
	start, err := parseCursor(cursor)
	if err != nil {
		return im.InboundPage{}, im.ErrInvalidProviderRequest
	}
	provider.mu.Lock()
	defer provider.mu.Unlock()
	if start > len(provider.inbound) {
		return im.InboundPage{}, im.ErrInvalidProviderRequest
	}
	end := start + limit
	if end > len(provider.inbound) {
		end = len(provider.inbound)
	}
	page := im.InboundPage{Messages: cloneMessages(provider.inbound[start:end])}
	if end < len(provider.inbound) {
		page.HasMore = true
		page.NextCursor = "cursor/" + strconv.Itoa(end)
	}
	if err := page.Validate(provider.profile); err != nil {
		return im.InboundPage{}, err
	}
	return page, nil
}

// InjectInbound appends an untrusted provider-shaped event for a local test. Duplicates are kept
// intentionally: platform inbox deduplication, not this transport fake, owns event uniqueness.
func (provider *Provider) InjectInbound(message im.InboundMessage) error {
	if provider == nil {
		return ErrClosed
	}
	if err := message.Validate(provider.Profile()); err != nil {
		return err
	}
	provider.mu.Lock()
	defer provider.mu.Unlock()
	if provider.closed {
		return ErrClosed
	}
	provider.inbound = append(provider.inbound, cloneMessage(message))
	return nil
}

func (provider *Provider) SendText(
	ctx context.Context,
	request im.ProviderTextMessage,
) (im.ProviderEffectReceipt, error) {
	if err := provider.checkContext(ctx); err != nil {
		return im.ProviderEffectReceipt{}, err
	}
	if err := request.Validate(provider.Profile()); err != nil {
		return im.ProviderEffectReceipt{}, err
	}
	if !provider.allowOutbound {
		return im.ProviderEffectReceipt{}, im.ErrProviderOutboundDisabled
	}
	hash := requestHash(
		"send", request.Conversation.SubjectID(), request.Sender.String(),
		request.ClientMessage.String(), request.Text, request.ExtInfo,
	)
	provider.mu.Lock()
	defer provider.mu.Unlock()
	if existing, ok := provider.effects[request.IdempotencyKey]; ok {
		if provider.effectHashes[request.IdempotencyKey] != hash {
			return im.ProviderEffectReceipt{}, im.ErrProviderConflict
		}
		existing.Status = im.ProviderEffectReplayed
		return existing, nil
	}
	if _, exists := provider.groups[request.Conversation.SubjectID()]; !exists {
		return im.ProviderEffectReceipt{}, ErrGroupMissing
	}
	user, exists := provider.users[request.Sender]
	if !exists || user.revoked {
		return im.ProviderEffectReceipt{}, ErrUserMissing
	}
	provider.nextMessage++
	externalID := fmt.Sprintf("msg_fake_%016x", provider.nextMessage)
	receipt := provider.receiptLocked(request.IdempotencyKey, externalID, im.ProviderEffectCommitted)
	provider.effects[request.IdempotencyKey] = receipt
	provider.effectHashes[request.IdempotencyKey] = hash
	provider.sent = append(provider.sent, sentMessage{request: request, receipt: receipt})
	return receipt, nil
}

// EditText is a deterministic fake implementation of the optional message mutation port. It
// only mutates a previously accepted fake outbound message and never represents provider state
// as platform authorization.
func (provider *Provider) EditText(
	ctx context.Context,
	request im.ProviderTextEdit,
) (im.ProviderEffectReceipt, error) {
	if err := provider.checkContext(ctx); err != nil {
		return im.ProviderEffectReceipt{}, err
	}
	profile := provider.Profile()
	if err := request.Validate(profile); err != nil {
		return im.ProviderEffectReceipt{}, err
	}
	if !profile.Supports(im.ProviderCapabilityTextEdit) || !provider.allowOutbound {
		return im.ProviderEffectReceipt{}, im.ErrProviderCapabilityUnsupported
	}
	hash := requestHash(
		"edit", request.Conversation.SubjectID(), request.Sender.String(),
		request.ClientMessage.String(), request.Text, request.ExtInfo,
	)
	provider.mu.Lock()
	defer provider.mu.Unlock()
	if existing, ok := provider.effects[request.IdempotencyKey]; ok {
		if provider.effectHashes[request.IdempotencyKey] != hash {
			return im.ProviderEffectReceipt{}, im.ErrProviderConflict
		}
		existing.Status = im.ProviderEffectReplayed
		return existing, nil
	}
	for index := range provider.sent {
		message := &provider.sent[index]
		if message.request.Conversation.SubjectID() == request.Conversation.SubjectID() &&
			message.request.ClientMessage == request.ClientMessage {
			if message.request.Sender != request.Sender || message.recalled {
				return im.ProviderEffectReceipt{}, ErrMessageMissing
			}
			message.request.Text = request.Text
			message.request.ExtInfo = request.ExtInfo
			receipt := provider.receiptLocked(
				request.IdempotencyKey, message.receipt.ExternalID, im.ProviderEffectCommitted,
			)
			provider.effects[request.IdempotencyKey] = receipt
			provider.effectHashes[request.IdempotencyKey] = hash
			return receipt, nil
		}
	}
	return im.ProviderEffectReceipt{}, ErrMessageMissing
}

// RecallMessage records a provider-side recall effect against the matching fake message. The
// original outbound record remains available for deterministic test inspection; callers must use
// the platform's recalled message projection as the business source of truth.
func (provider *Provider) RecallMessage(
	ctx context.Context,
	request im.ProviderMessageRecall,
) (im.ProviderEffectReceipt, error) {
	if err := provider.checkContext(ctx); err != nil {
		return im.ProviderEffectReceipt{}, err
	}
	profile := provider.Profile()
	if err := request.Validate(profile); err != nil {
		return im.ProviderEffectReceipt{}, err
	}
	if !profile.Supports(im.ProviderCapabilityMessageRecall) || !provider.allowOutbound {
		return im.ProviderEffectReceipt{}, im.ErrProviderCapabilityUnsupported
	}
	hash := requestHash(
		"recall", request.Conversation.SubjectID(), request.Sender.String(), request.ClientMessage.String(),
	)
	provider.mu.Lock()
	defer provider.mu.Unlock()
	if existing, ok := provider.effects[request.IdempotencyKey]; ok {
		if provider.effectHashes[request.IdempotencyKey] != hash {
			return im.ProviderEffectReceipt{}, im.ErrProviderConflict
		}
		existing.Status = im.ProviderEffectReplayed
		return existing, nil
	}
	for index := range provider.sent {
		message := &provider.sent[index]
		if message.request.Conversation.SubjectID() == request.Conversation.SubjectID() &&
			message.request.ClientMessage == request.ClientMessage {
			if message.request.Sender != request.Sender || message.recalled {
				return im.ProviderEffectReceipt{}, ErrMessageMissing
			}
			message.recalled = true
			receipt := provider.receiptLocked(
				request.IdempotencyKey, message.receipt.ExternalID, im.ProviderEffectCommitted,
			)
			provider.effects[request.IdempotencyKey] = receipt
			provider.effectHashes[request.IdempotencyKey] = hash
			return receipt, nil
		}
	}
	return im.ProviderEffectReceipt{}, ErrMessageMissing
}

func (provider *Provider) SentMessages() []im.ProviderTextMessage {
	if provider == nil {
		return nil
	}
	provider.mu.Lock()
	defer provider.mu.Unlock()
	output := make([]im.ProviderTextMessage, 0, len(provider.sent))
	for _, message := range provider.sent {
		output = append(output, message.request)
	}
	return output
}

func (provider *Provider) checkContext(ctx context.Context) error {
	if provider == nil || ctx == nil {
		return im.ErrInvalidProviderRequest
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	provider.mu.Lock()
	closed := provider.closed
	provider.mu.Unlock()
	if closed {
		return ErrClosed
	}
	return nil
}

func (provider *Provider) receiptLocked(
	operationKey string,
	externalID string,
	status im.ProviderEffectStatus,
) im.ProviderEffectReceipt {
	return im.ProviderEffectReceipt{
		OperationKey: operationKey,
		ExternalID:   externalID,
		Status:       status,
		ObservedAt:   provider.now().UTC(),
	}
}

func parseCursor(cursor string) (int, error) {
	if cursor == "" {
		return 0, nil
	}
	if !strings.HasPrefix(cursor, "cursor/") {
		return 0, errors.New("invalid fake cursor")
	}
	value, err := strconv.Atoi(strings.TrimPrefix(cursor, "cursor/"))
	if err != nil || value < 0 || strconv.Itoa(value) != strings.TrimPrefix(cursor, "cursor/") {
		return 0, errors.New("invalid fake cursor")
	}
	return value, nil
}

func requestHash(parts ...string) string {
	digest := sha256.Sum256([]byte(strings.Join(parts, "\x00")))
	return hex.EncodeToString(digest[:])
}

func cloneMessages(messages []im.InboundMessage) []im.InboundMessage {
	output := make([]im.InboundMessage, 0, len(messages))
	for _, message := range messages {
		output = append(output, cloneMessage(message))
	}
	return output
}

func cloneMessage(message im.InboundMessage) im.InboundMessage {
	message.MentionedActors = append([]im.ActorID(nil), message.MentionedActors...)
	return message
}
