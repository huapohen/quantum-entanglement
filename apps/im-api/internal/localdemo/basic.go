package localdemo

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/auth"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/immetadata"
	"golang.org/x/text/unicode/norm"
)

var (
	ErrNotFound      = errors.New("local IM resource not found")
	ErrForbidden     = errors.New("local IM operation forbidden")
	ErrInvalidCursor = errors.New("local IM cursor is invalid")
	ErrProvider      = errors.New("local IM provider effect failed")
	ErrIntegrity     = errors.New("local IM state integrity failure")

	localIDPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,126}[A-Za-z0-9]$`)
)

const (
	conversationNameMaxBytes = 256
	pageLimitDefault         = 20
	pageLimitMax             = 100
	cursorVersion            = 1
	cursorDomain             = "wanwork.local-demo-cursor/1\n"
)

type ConversationView struct {
	ID                   string   `json:"id"`
	Type                 string   `json:"type"`
	Status               string   `json:"status"`
	Name                 string   `json:"name"`
	WorkspaceID          string   `json:"workspaceId,omitempty"`
	ParentConversationID string   `json:"parentConversationId,omitempty"`
	MemberActorIDs       []string `json:"memberActorIds"`
	ProviderStatus       string   `json:"providerStatus"`
	CreatedAt            string   `json:"createdAt"`
}

type ConversationPage struct {
	Conversations []ConversationView `json:"conversations"`
	NextCursor    string             `json:"nextCursor,omitempty"`
	HasMore       bool               `json:"hasMore"`
}

type CreateConversationInput struct {
	Type           string   `json:"type"`
	Name           string   `json:"name"`
	MemberActorIDs []string `json:"memberActorIds"`
	IdempotencyKey string   `json:"idempotencyKey"`
}

type ConversationResult struct {
	Conversation ConversationView `json:"conversation"`
	Replayed     bool             `json:"replayed"`
}

type MessageView struct {
	ID                string `json:"id"`
	ClientMessageID   string `json:"clientMessageId"`
	ConversationID    string `json:"conversationId"`
	SenderActorID     string `json:"senderActorId"`
	Type              string `json:"type"`
	Status            string `json:"status"`
	Text              string `json:"text"`
	ExtInfo           string `json:"extInfo,omitempty"`
	ProviderMessageID string `json:"providerMessageId,omitempty"`
	ProviderStatus    string `json:"providerStatus"`
	CreatedAt         string `json:"createdAt"`
}

type MessagePage struct {
	Messages   []MessageView `json:"messages"`
	NextCursor string        `json:"nextCursor,omitempty"`
	HasMore    bool          `json:"hasMore"`
}

type SendTextInput struct {
	ClientMessageID string `json:"clientMessageId"`
	Text            string `json:"text"`
	ExtInfo         string `json:"extInfo"`
}

type SendMessageResult struct {
	Message  MessageView `json:"message"`
	Replayed bool        `json:"replayed"`
}

type EditTextInput struct {
	Text string `json:"text"`
}

type RecallMessageInput struct{}

type MutateMessageResult struct {
	Message  MessageView `json:"message"`
	Replayed bool        `json:"replayed"`
}

type localConversation struct {
	snapshot       im.ConversationSnapshot
	name           string
	members        map[im.ActorID]im.ConversationMembershipSnapshot
	access         map[im.ActorID]im.ConversationAccessSnapshot
	providerRef    im.ProviderConversationRef
	providerBound  bool
	providerStatus string
	createdAt      time.Time
	messages       []localMessage
	byClient       map[im.MessageID]int
}

type localMessage struct {
	snapshot          im.MessageSnapshot
	providerMessageID string
	providerStatus    string
}

type createRecord struct {
	digest       [sha256.Size]byte
	conversation im.ConversationID
}

type localCursorWire struct {
	Kind      string `json:"kind"`
	Namespace string `json:"namespace"`
	Position  int    `json:"position"`
	Scope     string `json:"scope"`
	Version   int    `json:"version"`
}

type localCursorEnvelope struct {
	Body   localCursorWire `json:"body"`
	Digest string          `json:"digest"`
}

func (service *Service) CreateConversation(
	ctx context.Context,
	bearerToken string,
	input CreateConversationInput,
) (ConversationResult, error) {
	if service == nil || ctx == nil || !validConversationName(input.Name) ||
		!validLocalID(input.IdempotencyKey) {
		return ConversationResult{}, ErrInvalidInput
	}
	if err := service.verifyRequester(ctx, bearerToken); err != nil {
		return ConversationResult{}, err
	}
	conversationType := im.ConversationType(input.Type)
	if conversationType != im.ConversationDirect && conversationType != im.ConversationGroup {
		return ConversationResult{}, ErrInvalidInput
	}
	members, memberIDs, err := service.parseMembers(input.MemberActorIDs, conversationType)
	if err != nil {
		return ConversationResult{}, err
	}
	agentIncluded := containsActorID(members, service.installation.AgentActor())
	digest := createConversationDigest(conversationType, input.Name, memberIDs)

	service.mu.Lock()
	defer service.mu.Unlock()
	if existing, ok := service.conversationCreates[input.IdempotencyKey]; ok {
		if existing.digest != digest {
			return ConversationResult{}, ErrConflict
		}
		conversation, ok := service.conversations[existing.conversation]
		if !ok {
			return ConversationResult{}, ErrIntegrity
		}
		return ConversationResult{Conversation: service.conversationView(conversation), Replayed: true}, nil
	}

	idBytes := sha256.Sum256(append([]byte("wanwork.local-demo-conversation/1\x00"), []byte(
		service.parent.Ref().TenantID().String()+"\x00"+string(conversationType)+"\x00"+input.IdempotencyKey,
	)...))
	conversationID, err := im.ParseConversationID("cnv_local_" + hex.EncodeToString(idBytes[:16]))
	if err != nil {
		return ConversationResult{}, ErrInvalidInput
	}
	conversationRef, err := im.NewConversationRef(service.parent.Ref().TenantID(), conversationID)
	if err != nil {
		return ConversationResult{}, ErrInvalidInput
	}
	workspace, hasWorkspace := service.parent.WorkspaceID()
	if !hasWorkspace {
		return ConversationResult{}, ErrInvalidInput
	}
	snapshot, err := im.NewConversationSnapshot(
		conversationRef, &workspace, conversationType, im.ConversationActive,
		im.ConversationID{}, im.MessageID{}, im.InvocationID{}, 1,
	)
	if err != nil {
		return ConversationResult{}, ErrInvalidInput
	}
	now := service.nowUTC()
	record := &localConversation{
		snapshot: snapshot, name: input.Name, members: make(map[im.ActorID]im.ConversationMembershipSnapshot),
		access: make(map[im.ActorID]im.ConversationAccessSnapshot), createdAt: now,
		byClient: make(map[im.MessageID]int), providerStatus: "local-only",
	}
	for index, actorID := range members {
		actorRef := service.knownActors[actorID]
		role := im.ConversationMembershipMember
		if actorID == service.requester.ActorID() {
			role = im.ConversationMembershipOwner
		}
		membership, membershipErr := im.NewConversationMembershipSnapshot(
			conversationRef, actorRef, role, im.ConversationMembershipActive, 1,
		)
		if membershipErr != nil {
			return ConversationResult{}, ErrInvalidInput
		}
		permissions := []im.ConversationPermission{
			im.ConversationPermissionRead, im.ConversationPermissionSendMessage,
		}
		if index == 0 && actorID == service.requester.ActorID() {
			permissions = append(permissions,
				im.ConversationPermissionManageMembers,
				im.ConversationPermissionManageConversation,
			)
			if agentIncluded {
				permissions = append(permissions, im.ConversationPermissionInvokeAgent)
			}
		}
		access, accessErr := im.NewConversationAccessSnapshot(conversationRef, actorRef, permissions, 1)
		if accessErr != nil {
			return ConversationResult{}, ErrInvalidInput
		}
		record.members[actorID] = membership
		record.access[actorID] = access
	}
	if conversationType == im.ConversationGroup {
		projection, projectionErr := immetadata.NewConversationProjection(
			im.ConversationGroup, conversationID, im.ConversationID{}, im.MessageID{}, im.InvocationID{},
		)
		if projectionErr != nil {
			return ConversationResult{}, ErrInvalidInput
		}
		extInfo, encodeErr := immetadata.EncodeConversationProjection(projection)
		if encodeErr != nil {
			return ConversationResult{}, ErrInvalidInput
		}
		providerConversation, receipt, providerErr := service.provider.CreateGroup(ctx, im.ProviderGroupCreate{
			Conversation: conversationRef, ExtInfo: extInfo, MemberActors: append([]im.ActorID(nil), members...),
			IdempotencyKey: "demo/basic/group/" + input.IdempotencyKey,
		})
		if providerErr != nil {
			return ConversationResult{}, errors.Join(ErrProvider, providerErr)
		}
		if receipt.Validate() != nil || providerConversation.IsZero() {
			return ConversationResult{}, ErrProvider
		}
		record.providerRef, record.providerBound, record.providerStatus = providerConversation, true, string(receipt.Status)
	}
	service.conversations[conversationID] = record
	service.conversationOrder = append(service.conversationOrder, conversationID)
	service.conversationCreates[input.IdempotencyKey] = createRecord{digest: digest, conversation: conversationID}
	return ConversationResult{Conversation: service.conversationView(record)}, nil
}

func (service *Service) ListConversations(
	ctx context.Context,
	bearerToken string,
	after string,
	limit int,
) (ConversationPage, error) {
	if service == nil || ctx == nil {
		return ConversationPage{}, ErrInvalidInput
	}
	if err := service.verifyRequester(ctx, bearerToken); err != nil {
		return ConversationPage{}, err
	}
	limit, err := normalizePageLimit(limit)
	if err != nil {
		return ConversationPage{}, err
	}
	service.mu.Lock()
	defer service.mu.Unlock()
	position, err := service.decodeCursor(after, "conversations", service.requester.ActorID().String())
	if err != nil {
		return ConversationPage{}, err
	}
	if position > len(service.conversationOrder) {
		return ConversationPage{}, ErrInvalidCursor
	}
	end := position + limit
	if end > len(service.conversationOrder) {
		end = len(service.conversationOrder)
	}
	page := ConversationPage{Conversations: make([]ConversationView, 0, end-position), HasMore: end < len(service.conversationOrder)}
	for _, conversationID := range service.conversationOrder[position:end] {
		conversation, ok := service.conversations[conversationID]
		if !ok {
			return ConversationPage{}, ErrIntegrity
		}
		page.Conversations = append(page.Conversations, service.conversationView(conversation))
	}
	if end > position {
		page.NextCursor, err = service.encodeCursor("conversations", service.requester.ActorID().String(), end)
		if err != nil {
			return ConversationPage{}, err
		}
	}
	return page, nil
}

func (service *Service) ListMessages(
	ctx context.Context,
	bearerToken string,
	conversationIDValue string,
	after string,
	limit int,
) (MessagePage, error) {
	if service == nil || ctx == nil {
		return MessagePage{}, ErrInvalidInput
	}
	if err := service.verifyRequester(ctx, bearerToken); err != nil {
		return MessagePage{}, err
	}
	conversationID, err := im.ParseConversationID(conversationIDValue)
	if err != nil {
		return MessagePage{}, ErrNotFound
	}
	limit, err = normalizePageLimit(limit)
	if err != nil {
		return MessagePage{}, err
	}
	service.mu.Lock()
	defer service.mu.Unlock()
	conversation, ok := service.conversations[conversationID]
	if !ok {
		return MessagePage{}, ErrNotFound
	}
	if !service.canRead(conversation) {
		return MessagePage{}, ErrForbidden
	}
	position, err := service.decodeCursor(after, "messages", conversationID.String())
	if err != nil {
		return MessagePage{}, err
	}
	if position > len(conversation.messages) {
		return MessagePage{}, ErrInvalidCursor
	}
	end := position + limit
	if end > len(conversation.messages) {
		end = len(conversation.messages)
	}
	page := MessagePage{Messages: make([]MessageView, 0, end-position), HasMore: end < len(conversation.messages)}
	for _, message := range conversation.messages[position:end] {
		page.Messages = append(page.Messages, messageView(message))
	}
	if end > position {
		page.NextCursor, err = service.encodeCursor("messages", conversationID.String(), end)
		if err != nil {
			return MessagePage{}, err
		}
	}
	return page, nil
}

func (service *Service) SendText(
	ctx context.Context,
	bearerToken string,
	conversationIDValue string,
	input SendTextInput,
) (SendMessageResult, error) {
	if service == nil || ctx == nil || !validMessageInput(input) {
		return SendMessageResult{}, ErrInvalidInput
	}
	if err := service.verifyRequester(ctx, bearerToken); err != nil {
		return SendMessageResult{}, err
	}
	conversationID, err := im.ParseConversationID(conversationIDValue)
	if err != nil {
		return SendMessageResult{}, ErrNotFound
	}
	clientMessageID, err := im.ParseMessageID(input.ClientMessageID)
	if err != nil {
		return SendMessageResult{}, ErrInvalidInput
	}
	digest := sha256.Sum256([]byte("wanwork.local-demo-message/1\x00" + conversationID.String() + "\x00" +
		input.ClientMessageID + "\x00" + input.Text + "\x00" + input.ExtInfo))

	service.mu.Lock()
	defer service.mu.Unlock()
	conversation, ok := service.conversations[conversationID]
	if !ok {
		return SendMessageResult{}, ErrNotFound
	}
	if !service.canSend(conversation) {
		return SendMessageResult{}, ErrForbidden
	}
	if existingIndex, exists := conversation.byClient[clientMessageID]; exists {
		existing := conversation.messages[existingIndex]
		existingDigest := sha256.Sum256([]byte("wanwork.local-demo-message/1\x00" + conversationID.String() + "\x00" +
			existing.snapshot.ClientMessageID().String() + "\x00" + existing.snapshot.Text() + "\x00" + existing.snapshot.ExtInfo()))
		if existingDigest != digest {
			return SendMessageResult{}, ErrConflict
		}
		return SendMessageResult{Message: messageView(existing), Replayed: true}, nil
	}
	platformDigest := sha256.Sum256([]byte("wanwork.local-demo-platform-message/1\x00" + conversationID.String() + "\x00" + input.ClientMessageID))
	platformMessageID, err := im.ParseMessageID("msg_local_" + hex.EncodeToString(platformDigest[:16]))
	if err != nil {
		return SendMessageResult{}, ErrInvalidInput
	}
	messageRef, err := im.NewMessageRef(conversation.snapshot.Ref(), platformMessageID)
	if err != nil {
		return SendMessageResult{}, ErrInvalidInput
	}
	now := service.nowUTC()
	snapshot, err := im.NewMessageSnapshot(
		messageRef, service.requester, clientMessageID, im.MessageTypeText, im.MessageStatusActive,
		input.Text, input.ExtInfo, now, uint64(len(conversation.messages)+1),
	)
	if err != nil {
		return SendMessageResult{}, ErrInvalidInput
	}
	record := localMessage{snapshot: snapshot, providerStatus: "local-only"}
	if conversation.providerBound {
		receipt, providerErr := service.provider.SendText(ctx, im.ProviderTextMessage{
			Conversation: conversation.providerRef, Sender: service.requester.ActorID(),
			ClientMessage: clientMessageID, Text: input.Text, ExtInfo: input.ExtInfo,
			IdempotencyKey: "demo/basic/message/" + conversationID.String() + "/" + input.ClientMessageID,
		})
		if providerErr != nil {
			return SendMessageResult{}, errors.Join(ErrProvider, providerErr)
		}
		if receipt.Validate() != nil {
			return SendMessageResult{}, ErrProvider
		}
		record.providerMessageID, record.providerStatus = receipt.ExternalID, string(receipt.Status)
	}
	conversation.byClient[clientMessageID] = len(conversation.messages)
	conversation.messages = append(conversation.messages, record)
	return SendMessageResult{Message: messageView(record)}, nil
}

// EditText creates a new platform message revision. Provider mutation is attempted only after
// the target and sender checks pass; a provider receipt never replaces the platform snapshot.
func (service *Service) EditText(
	ctx context.Context,
	bearerToken string,
	conversationIDValue string,
	messageIDValue string,
	input EditTextInput,
) (MutateMessageResult, error) {
	if service == nil || ctx == nil || !validMessageText(input.Text) {
		return MutateMessageResult{}, ErrInvalidInput
	}
	if err := service.verifyRequester(ctx, bearerToken); err != nil {
		return MutateMessageResult{}, err
	}
	conversationID, err := im.ParseConversationID(conversationIDValue)
	if err != nil {
		return MutateMessageResult{}, ErrNotFound
	}
	messageID, err := im.ParseMessageID(messageIDValue)
	if err != nil {
		return MutateMessageResult{}, ErrNotFound
	}
	service.mu.Lock()
	defer service.mu.Unlock()
	conversation, ok := service.conversations[conversationID]
	if !ok {
		return MutateMessageResult{}, ErrNotFound
	}
	if !service.canSend(conversation) {
		return MutateMessageResult{}, ErrForbidden
	}
	index := findMessageIndex(conversation.messages, messageID)
	if index < 0 {
		return MutateMessageResult{}, ErrNotFound
	}
	record := conversation.messages[index]
	if record.snapshot.Sender().ActorID() != service.requester.ActorID() {
		return MutateMessageResult{}, ErrForbidden
	}
	if record.snapshot.Status() == im.MessageStatusRecalled {
		return MutateMessageResult{}, ErrConflict
	}
	if record.snapshot.Text() == input.Text {
		return MutateMessageResult{Message: messageView(record), Replayed: true}, nil
	}
	if conversation.providerBound {
		mutator, supported := any(service.provider).(im.MessageMutationProvider)
		if !supported {
			return MutateMessageResult{}, errors.Join(ErrProvider, im.ErrProviderCapabilityUnsupported)
		}
		receipt, providerErr := mutator.EditText(ctx, im.ProviderTextEdit{
			Conversation: conversation.providerRef, Sender: service.requester.ActorID(),
			ClientMessage: record.snapshot.ClientMessageID(), Text: input.Text, ExtInfo: record.snapshot.ExtInfo(),
			IdempotencyKey: "demo/basic/edit/" + messageID.String() + "/" + strconv.FormatUint(record.snapshot.Revision()+1, 10),
		})
		if providerErr != nil {
			return MutateMessageResult{}, errors.Join(ErrProvider, providerErr)
		}
		if receipt.Validate() != nil {
			return MutateMessageResult{}, ErrProvider
		}
		record.providerStatus = string(receipt.Status)
	}
	snapshot, err := im.NewMessageSnapshot(
		record.snapshot.Ref(), record.snapshot.Sender(), record.snapshot.ClientMessageID(),
		record.snapshot.MessageType(), im.MessageStatusEdited, input.Text, record.snapshot.ExtInfo(),
		record.snapshot.CreatedAt(), record.snapshot.Revision()+1,
	)
	if err != nil {
		return MutateMessageResult{}, ErrIntegrity
	}
	record.snapshot = snapshot
	conversation.messages[index] = record
	return MutateMessageResult{Message: messageView(record)}, nil
}

// RecallMessage creates a tombstone-like platform revision while retaining the immutable message
// identity and creation timestamp. The provider adapter is optional and must return an explicit
// capability error when it cannot perform the transport mutation.
func (service *Service) RecallMessage(
	ctx context.Context,
	bearerToken string,
	conversationIDValue string,
	messageIDValue string,
	_ RecallMessageInput,
) (MutateMessageResult, error) {
	if service == nil || ctx == nil {
		return MutateMessageResult{}, ErrInvalidInput
	}
	if err := service.verifyRequester(ctx, bearerToken); err != nil {
		return MutateMessageResult{}, err
	}
	conversationID, err := im.ParseConversationID(conversationIDValue)
	if err != nil {
		return MutateMessageResult{}, ErrNotFound
	}
	messageID, err := im.ParseMessageID(messageIDValue)
	if err != nil {
		return MutateMessageResult{}, ErrNotFound
	}
	service.mu.Lock()
	defer service.mu.Unlock()
	conversation, ok := service.conversations[conversationID]
	if !ok {
		return MutateMessageResult{}, ErrNotFound
	}
	if !service.canSend(conversation) {
		return MutateMessageResult{}, ErrForbidden
	}
	index := findMessageIndex(conversation.messages, messageID)
	if index < 0 {
		return MutateMessageResult{}, ErrNotFound
	}
	record := conversation.messages[index]
	if record.snapshot.Sender().ActorID() != service.requester.ActorID() {
		return MutateMessageResult{}, ErrForbidden
	}
	if record.snapshot.Status() == im.MessageStatusRecalled {
		return MutateMessageResult{Message: messageView(record), Replayed: true}, nil
	}
	if conversation.providerBound {
		mutator, supported := any(service.provider).(im.MessageMutationProvider)
		if !supported {
			return MutateMessageResult{}, errors.Join(ErrProvider, im.ErrProviderCapabilityUnsupported)
		}
		receipt, providerErr := mutator.RecallMessage(ctx, im.ProviderMessageRecall{
			Conversation: conversation.providerRef, Sender: service.requester.ActorID(),
			ClientMessage:  record.snapshot.ClientMessageID(),
			IdempotencyKey: "demo/basic/recall/" + messageID.String() + "/" + strconv.FormatUint(record.snapshot.Revision()+1, 10),
		})
		if providerErr != nil {
			return MutateMessageResult{}, errors.Join(ErrProvider, providerErr)
		}
		if receipt.Validate() != nil {
			return MutateMessageResult{}, ErrProvider
		}
		record.providerStatus = string(receipt.Status)
	}
	snapshot, err := im.NewMessageSnapshot(
		record.snapshot.Ref(), record.snapshot.Sender(), record.snapshot.ClientMessageID(),
		record.snapshot.MessageType(), im.MessageStatusRecalled, "", record.snapshot.ExtInfo(),
		record.snapshot.CreatedAt(), record.snapshot.Revision()+1,
	)
	if err != nil {
		return MutateMessageResult{}, ErrIntegrity
	}
	record.snapshot = snapshot
	conversation.messages[index] = record
	return MutateMessageResult{Message: messageView(record)}, nil
}

func findMessageIndex(messages []localMessage, messageID im.MessageID) int {
	for index, message := range messages {
		if message.snapshot.Ref().MessageID() == messageID {
			return index
		}
	}
	return -1
}

func (service *Service) verifyRequester(ctx context.Context, bearerToken string) error {
	identity, err := service.authVerifier.Verify(ctx, authVerifyRequest(bearerToken))
	if err != nil || identity.ExternalRef.SubjectID() != LocalExternalSubject {
		return ErrUnauthenticated
	}
	return nil
}

func authVerifyRequest(token string) auth.VerifyRequest {
	return auth.VerifyRequest{BearerToken: token}
}

func (service *Service) parseMembers(values []string, conversationType im.ConversationType) ([]im.ActorID, []string, error) {
	seen := make(map[im.ActorID]struct{})
	actors := make([]im.ActorID, 0, len(values)+1)
	actors = append(actors, service.requester.ActorID())
	seen[service.requester.ActorID()] = struct{}{}
	for _, value := range values {
		actorID, err := im.ParseActorID(value)
		if err != nil {
			return nil, nil, ErrInvalidInput
		}
		if _, exists := seen[actorID]; exists {
			continue
		}
		if _, known := service.knownActors[actorID]; !known {
			return nil, nil, ErrInvalidInput
		}
		seen[actorID] = struct{}{}
		actors = append(actors, actorID)
	}
	if conversationType == im.ConversationDirect && len(actors) != 2 {
		return nil, nil, ErrInvalidInput
	}
	if conversationType == im.ConversationGroup && len(actors) == 0 {
		return nil, nil, ErrInvalidInput
	}
	memberIDs := make([]string, 0, len(actors))
	for _, actor := range actors {
		memberIDs = append(memberIDs, actor.String())
	}
	sort.Strings(memberIDs)
	return actors, memberIDs, nil
}

func containsActorID(values []im.ActorID, target im.ActorID) bool {
	for _, value := range values {
		if value == target {
			return true
		}
	}
	return false
}

func (service *Service) canRead(conversation *localConversation) bool {
	access, ok := conversation.access[service.requester.ActorID()]
	return ok && access.HasPermission(im.ConversationPermissionRead) &&
		conversation.members[service.requester.ActorID()].Status() == im.ConversationMembershipActive
}

func (service *Service) canSend(conversation *localConversation) bool {
	access, ok := conversation.access[service.requester.ActorID()]
	return ok && access.HasPermission(im.ConversationPermissionSendMessage) && service.canRead(conversation)
}

func (service *Service) conversationView(conversation *localConversation) ConversationView {
	memberIDs := make([]string, 0, len(conversation.members))
	for actorID, membership := range conversation.members {
		if membership.Status() == im.ConversationMembershipActive {
			memberIDs = append(memberIDs, actorID.String())
		}
	}
	sort.Strings(memberIDs)
	workspaceID := ""
	if workspace, ok := conversation.snapshot.WorkspaceID(); ok {
		workspaceID = workspace.String()
	}
	return ConversationView{
		ID: conversation.snapshot.Ref().ConversationID().String(), Type: string(conversation.snapshot.ConversationType()),
		Status: string(conversation.snapshot.Status()), Name: conversation.name, WorkspaceID: workspaceID,
		ParentConversationID: conversation.snapshot.ParentConversationID().String(), MemberActorIDs: memberIDs,
		ProviderStatus: conversation.providerStatus, CreatedAt: conversation.createdAt.Format(time.RFC3339Nano),
	}
}

func messageView(message localMessage) MessageView {
	snapshot := message.snapshot
	return MessageView{
		ID: snapshot.Ref().MessageID().String(), ClientMessageID: snapshot.ClientMessageID().String(),
		ConversationID: snapshot.Ref().ConversationRef().ConversationID().String(), SenderActorID: snapshot.Sender().ActorID().String(),
		Type: string(snapshot.MessageType()), Status: string(snapshot.Status()), Text: snapshot.Text(), ExtInfo: snapshot.ExtInfo(),
		ProviderMessageID: message.providerMessageID, ProviderStatus: message.providerStatus,
		CreatedAt: snapshot.CreatedAt().Format(time.RFC3339Nano),
	}
}

func (service *Service) encodeCursor(kind, scope string, position int) (string, error) {
	body := localCursorWire{Kind: kind, Namespace: service.cursorNamespaceHex, Position: position, Scope: scope, Version: cursorVersion}
	encodedBody, err := json.Marshal(body)
	if err != nil {
		return "", ErrInvalidCursor
	}
	digest := sha256.Sum256(append([]byte(cursorDomain), encodedBody...))
	envelope, err := json.Marshal(localCursorEnvelope{Body: body, Digest: hex.EncodeToString(digest[:])})
	if err != nil {
		return "", ErrInvalidCursor
	}
	return base64.RawURLEncoding.EncodeToString(envelope), nil
}

func (service *Service) decodeCursor(raw, kind, scope string) (int, error) {
	if raw == "" {
		return 0, nil
	}
	if len(raw) > 4096 {
		return 0, ErrInvalidCursor
	}
	decoded, err := base64.RawURLEncoding.Strict().DecodeString(raw)
	if err != nil {
		return 0, ErrInvalidCursor
	}
	decoder := json.NewDecoder(bytes.NewReader(decoded))
	decoder.DisallowUnknownFields()
	var envelope localCursorEnvelope
	if err := decoder.Decode(&envelope); err != nil {
		return 0, ErrInvalidCursor
	}
	var trailing any
	if err := decoder.Decode(&trailing); !errors.Is(err, io.EOF) {
		return 0, ErrInvalidCursor
	}
	canonical, err := json.Marshal(envelope)
	if err != nil || string(canonical) != string(decoded) {
		return 0, ErrInvalidCursor
	}
	body := envelope.Body
	if body.Version != cursorVersion || body.Kind != kind || body.Scope != scope || body.Namespace != service.cursorNamespaceHex || body.Position < 0 {
		return 0, ErrInvalidCursor
	}
	encodedBody, err := json.Marshal(body)
	if err != nil {
		return 0, ErrInvalidCursor
	}
	digest := sha256.Sum256(append([]byte(cursorDomain), encodedBody...))
	if envelope.Digest != hex.EncodeToString(digest[:]) {
		return 0, ErrInvalidCursor
	}
	return body.Position, nil
}

func createConversationDigest(conversationType im.ConversationType, name string, memberIDs []string) [sha256.Size]byte {
	return sha256.Sum256([]byte("wanwork.local-demo-conversation-request/1\x00" + string(conversationType) + "\x00" + name + "\x00" + strings.Join(memberIDs, "\x00")))
}

func normalizePageLimit(value int) (int, error) {
	if value == 0 {
		return pageLimitDefault, nil
	}
	if value < 1 || value > pageLimitMax {
		return 0, ErrInvalidInput
	}
	return value, nil
}

func validConversationName(value string) bool {
	if value == "" || len(value) > conversationNameMaxBytes || !utf8.ValidString(value) ||
		!norm.NFC.IsNormalString(value) || strings.TrimSpace(value) != value {
		return false
	}
	for _, character := range value {
		if unicode.IsControl(character) {
			return false
		}
	}
	return true
}

func validLocalID(value string) bool {
	return value != "" && len(value) <= 128 && localIDPattern.MatchString(value) && !strings.Contains(value, "..")
}

func validMessageInput(input SendTextInput) bool {
	if input.ExtInfo != "" && !validMessageExtInfo(input.ExtInfo) {
		return false
	}
	if input.ClientMessageID == "" || len(input.ClientMessageID) > 128 ||
		!validLocalID(input.ClientMessageID) || !validMessageText(input.Text) {
		return false
	}
	return true
}

func validMessageText(value string) bool {
	if value == "" || len(value) > im.MessageTextMaxBytes || !utf8.ValidString(value) ||
		!norm.NFC.IsNormalString(value) || strings.TrimSpace(value) != value {
		return false
	}
	for _, character := range value {
		if unicode.IsControl(character) && character != '\n' && character != '\t' && character != '\r' {
			return false
		}
	}
	return true
}

func validMessageExtInfo(value string) bool {
	if value == "" || len(value) > im.MessageExtInfoMaxBytes || !utf8.ValidString(value) ||
		!norm.NFC.IsNormalString(value) || strings.TrimSpace(value) != value {
		return false
	}
	decoder := json.NewDecoder(strings.NewReader(value))
	decoder.UseNumber()
	var object map[string]json.RawMessage
	if err := decoder.Decode(&object); err != nil || object == nil {
		return false
	}
	var trailing any
	if err := decoder.Decode(&trailing); !errors.Is(err, io.EOF) {
		return false
	}
	canonical, err := json.Marshal(object)
	return err == nil && string(canonical) == value
}

func (service *Service) nowUTC() time.Time {
	return time.Now().UTC().Round(0)
}
