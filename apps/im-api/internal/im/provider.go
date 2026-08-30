package im

import (
	"context"
	"errors"
	"regexp"
	"strings"
	"time"
)

// Provider ports deliberately contain platform values rather than SDK clients. A provider
// adapter may translate these values to RongCloud (or a fake), but it cannot turn provider
// metadata or an acknowledgement into platform authorization.
var (
	ErrInvalidProviderRequest        = errors.New("invalid IM provider request")
	ErrProviderUnavailable           = errors.New("IM provider unavailable")
	ErrProviderNotReady              = errors.New("IM provider is not ready")
	ErrProviderConflict              = errors.New("IM provider idempotency conflict")
	ErrProviderCapabilityUnsupported = errors.New("IM provider capability is unsupported")
	ErrProviderEffectUnknown         = errors.New("IM provider effect outcome is unknown")
	ErrProviderOutboundDisabled      = errors.New("IM provider outbound is disabled")
)

const (
	ProviderMetadataSchemaVersion = 1
	ProviderMaxTextBytes          = 64 * 1024
	ProviderMaxExternalIDBytes    = 256
	ProviderMaxCursorBytes        = 1024
	ProviderMaxIdempotencyBytes   = 128
)

var providerOpaqueIDPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$`)

type ProviderCapability string

const (
	ProviderCapabilityHealth        ProviderCapability = "health"
	ProviderCapabilityInboundRead   ProviderCapability = "inbound_read"
	ProviderCapabilityCursorResume  ProviderCapability = "cursor_resume"
	ProviderCapabilityUserProvision ProviderCapability = "user_provision"
	ProviderCapabilityGroupCreate   ProviderCapability = "group_create"
	ProviderCapabilityMemberWrite   ProviderCapability = "member_write"
	ProviderCapabilityTextSend      ProviderCapability = "text_send"
	ProviderCapabilityTextEdit      ProviderCapability = "text_edit"
	ProviderCapabilityMessageRecall ProviderCapability = "message_recall"
)

func (capability ProviderCapability) Valid() bool {
	switch capability {
	case ProviderCapabilityHealth, ProviderCapabilityInboundRead,
		ProviderCapabilityCursorResume, ProviderCapabilityUserProvision,
		ProviderCapabilityGroupCreate, ProviderCapabilityMemberWrite,
		ProviderCapabilityTextSend, ProviderCapabilityTextEdit, ProviderCapabilityMessageRecall:
		return true
	default:
		return false
	}
}

// ProviderProfile is a reviewed capability declaration, not proof that the provider currently
// supports every operation. The adapter must still verify callback authenticity and readback.
type ProviderProfile struct {
	Provider                 IdentityProvider
	Realm                    ProviderRealmID
	Capabilities             []ProviderCapability
	MetadataSchemaVersion    int
	MaxInboundTextBytes      int
	MaxOutboundTextBytes     int
	MaxProviderMetadataBytes int
}

func NewProviderProfile(
	provider IdentityProvider,
	realm ProviderRealmID,
	capabilities []ProviderCapability,
	maxInboundTextBytes int,
	maxOutboundTextBytes int,
	maxProviderMetadataBytes int,
) (ProviderProfile, error) {
	if !provider.Valid() || provider != IdentityProviderRongCloud || realm.IsZero() ||
		len(capabilities) == 0 || maxInboundTextBytes <= 0 ||
		maxInboundTextBytes > ProviderMaxTextBytes || maxOutboundTextBytes <= 0 ||
		maxOutboundTextBytes > ProviderMaxTextBytes || maxProviderMetadataBytes <= 0 ||
		maxProviderMetadataBytes > 64*1024 {
		return ProviderProfile{}, ErrInvalidProviderRequest
	}
	seen := make(map[ProviderCapability]struct{}, len(capabilities))
	for _, capability := range capabilities {
		if !capability.Valid() {
			return ProviderProfile{}, ErrInvalidProviderRequest
		}
		if _, exists := seen[capability]; exists {
			return ProviderProfile{}, ErrInvalidProviderRequest
		}
		seen[capability] = struct{}{}
	}
	return ProviderProfile{
		Provider: provider, Realm: realm,
		Capabilities:             append([]ProviderCapability(nil), capabilities...),
		MetadataSchemaVersion:    ProviderMetadataSchemaVersion,
		MaxInboundTextBytes:      maxInboundTextBytes,
		MaxOutboundTextBytes:     maxOutboundTextBytes,
		MaxProviderMetadataBytes: maxProviderMetadataBytes,
	}, nil
}

func (profile ProviderProfile) Supports(capability ProviderCapability) bool {
	for _, candidate := range profile.Capabilities {
		if candidate == capability {
			return true
		}
	}
	return false
}

func (profile ProviderProfile) IsZero() bool {
	return profile.Provider == "" && profile.Realm.IsZero() && len(profile.Capabilities) == 0
}

// ProviderUserProvision requests a provider account for a platform Actor. Agent actors use this
// same normal-user path; no bot/robot account is represented by this port.
type ProviderUserProvision struct {
	Actor          ActorID
	DisplayName    string
	ExtInfo        string
	IdempotencyKey string
}

func (request ProviderUserProvision) Validate(profile ProviderProfile) error {
	if !request.validate(profile) {
		return ErrInvalidProviderRequest
	}
	return nil
}

func (request ProviderUserProvision) validate(profile ProviderProfile) bool {
	if request.Actor.IsZero() || request.DisplayName == "" || len(request.DisplayName) > 256 ||
		len(request.ExtInfo) == 0 || len(request.ExtInfo) > profile.MaxProviderMetadataBytes ||
		!validProviderIdempotencyKey(request.IdempotencyKey) {
		return false
	}
	subjectType, ok := request.Actor.SubjectType()
	return ok && (subjectType == SubjectHuman || subjectType == SubjectAgent)
}

type ProviderGroupCreate struct {
	Conversation   ConversationRef
	ExtInfo        string
	MemberActors   []ActorID
	IdempotencyKey string
}

func (request ProviderGroupCreate) Validate(profile ProviderProfile) error {
	if !request.validate(profile) {
		return ErrInvalidProviderRequest
	}
	return nil
}

func (request ProviderGroupCreate) validate(profile ProviderProfile) bool {
	if request.Conversation.IsZero() || request.Conversation.ConversationID().IsZero() ||
		len(request.ExtInfo) == 0 || len(request.ExtInfo) > profile.MaxProviderMetadataBytes ||
		!validProviderIdempotencyKey(request.IdempotencyKey) || len(request.MemberActors) == 0 {
		return false
	}
	seen := make(map[ActorID]struct{}, len(request.MemberActors))
	for _, actor := range request.MemberActors {
		if actor.IsZero() {
			return false
		}
		if _, exists := seen[actor]; exists {
			return false
		}
		seen[actor] = struct{}{}
	}
	return true
}

type ProviderMemberUpdate struct {
	Conversation   ProviderConversationRef
	MemberActors   []ActorID
	IdempotencyKey string
}

func (request ProviderMemberUpdate) Validate() error {
	if !request.validate() {
		return ErrInvalidProviderRequest
	}
	return nil
}

// ValidateForProfile additionally binds the provider conversation to the adapter's configured
// provider realm. A syntactically valid RongCloud reference from another realm must not be sent
// through this adapter by accident.
func (request ProviderMemberUpdate) ValidateForProfile(profile ProviderProfile) error {
	if !request.validate() || !validProviderConversationRef(request.Conversation, profile) {
		return ErrInvalidProviderRequest
	}
	return nil
}

func (request ProviderMemberUpdate) validate() bool {
	if request.Conversation.IsZero() || !validProviderIdempotencyKey(request.IdempotencyKey) ||
		len(request.MemberActors) == 0 {
		return false
	}
	seen := make(map[ActorID]struct{}, len(request.MemberActors))
	for _, actor := range request.MemberActors {
		if actor.IsZero() {
			return false
		}
		if _, exists := seen[actor]; exists {
			return false
		}
		seen[actor] = struct{}{}
	}
	return true
}

type ProviderTextMessage struct {
	Conversation   ProviderConversationRef
	Sender         ActorID
	ClientMessage  MessageID
	Text           string
	ExtInfo        string
	IdempotencyKey string
}

// ProviderTextEdit and ProviderMessageRecall are optional transport mutations. Their presence
// never changes platform history by itself; the platform must first commit its own message
// revision and retain the provider receipt. Providers that cannot prove the mutation capability
// must return ErrProviderCapabilityUnsupported instead of simulating success.
type ProviderTextEdit struct {
	Conversation   ProviderConversationRef
	Sender         ActorID
	ClientMessage  MessageID
	Text           string
	ExtInfo        string
	IdempotencyKey string
}

func (request ProviderTextEdit) Validate(profile ProviderProfile) error {
	if !validProviderConversationRef(request.Conversation, profile) || request.Sender.IsZero() ||
		request.ClientMessage.IsZero() || request.Text == "" || len(request.Text) > profile.MaxOutboundTextBytes ||
		len(request.ExtInfo) > profile.MaxProviderMetadataBytes || !validProviderIdempotencyKey(request.IdempotencyKey) {
		return ErrInvalidProviderRequest
	}
	return nil
}

type ProviderMessageRecall struct {
	Conversation   ProviderConversationRef
	Sender         ActorID
	ClientMessage  MessageID
	IdempotencyKey string
}

func (request ProviderMessageRecall) Validate(profile ProviderProfile) error {
	if !validProviderConversationRef(request.Conversation, profile) || request.Sender.IsZero() ||
		request.ClientMessage.IsZero() || !validProviderIdempotencyKey(request.IdempotencyKey) {
		return ErrInvalidProviderRequest
	}
	return nil
}

func (request ProviderTextMessage) Validate(profile ProviderProfile) error {
	if !request.validate(profile) {
		return ErrInvalidProviderRequest
	}
	return nil
}

func (request ProviderTextMessage) validate(profile ProviderProfile) bool {
	return validProviderConversationRef(request.Conversation, profile) && !request.Sender.IsZero() &&
		!request.ClientMessage.IsZero() && request.Text != "" &&
		len(request.Text) <= profile.MaxOutboundTextBytes &&
		len(request.ExtInfo) <= profile.MaxProviderMetadataBytes &&
		validProviderIdempotencyKey(request.IdempotencyKey)
}

type ProviderEffectStatus string

const (
	ProviderEffectCommitted ProviderEffectStatus = "committed"
	ProviderEffectReplayed  ProviderEffectStatus = "replayed"
	ProviderEffectUnknown   ProviderEffectStatus = "unknown"
)

func (status ProviderEffectStatus) Valid() bool {
	return status == ProviderEffectCommitted || status == ProviderEffectReplayed ||
		status == ProviderEffectUnknown
}

// ProviderEffectReceipt is transport evidence only. It never advances a platform Task or
// Acceptance state by itself.
type ProviderEffectReceipt struct {
	OperationKey string
	ExternalID   string
	Status       ProviderEffectStatus
	ObservedAt   time.Time
}

func (receipt ProviderEffectReceipt) Validate() error {
	if !receipt.validate() {
		return ErrInvalidProviderRequest
	}
	return nil
}

func (receipt ProviderEffectReceipt) validate() bool {
	return providerOpaqueIDPattern.MatchString(receipt.OperationKey) &&
		providerOpaqueIDPattern.MatchString(receipt.ExternalID) && receipt.Status.Valid() &&
		!receipt.ObservedAt.IsZero() && receipt.ObservedAt.Location() == time.UTC
}

type InboundMessage struct {
	EventID         string
	Conversation    ProviderConversationRef
	Sender          ExternalIdentityRef
	MessageType     string
	Text            string
	ExtInfo         string
	MentionedActors []ActorID
	ProviderCursor  string
	ObservedAt      time.Time
}

func (message InboundMessage) Validate(profile ProviderProfile) error {
	if !message.validate(profile) {
		return ErrInvalidProviderRequest
	}
	return nil
}

func (message InboundMessage) validate(profile ProviderProfile) bool {
	if !providerOpaqueIDPattern.MatchString(message.EventID) ||
		!validProviderConversationRef(message.Conversation, profile) || message.Sender.IsZero() ||
		message.Sender.Provider() != profile.Provider || message.Sender.RealmID() != profile.Realm ||
		message.MessageType != "text" || message.Text == "" ||
		len(message.Text) > profile.MaxInboundTextBytes ||
		len(message.ExtInfo) > profile.MaxProviderMetadataBytes ||
		len(message.ProviderCursor) > ProviderMaxCursorBytes || message.ObservedAt.IsZero() ||
		message.ObservedAt.Location() != time.UTC {
		return false
	}
	seen := make(map[ActorID]struct{}, len(message.MentionedActors))
	for _, actor := range message.MentionedActors {
		if actor.IsZero() {
			return false
		}
		if _, exists := seen[actor]; exists {
			return false
		}
		seen[actor] = struct{}{}
	}
	return true
}

type InboundPage struct {
	Messages   []InboundMessage
	NextCursor string
	HasMore    bool
}

func (page InboundPage) Validate(profile ProviderProfile) error {
	if len(page.NextCursor) > ProviderMaxCursorBytes || (page.HasMore && page.NextCursor == "") {
		return ErrInvalidProviderRequest
	}
	for _, message := range page.Messages {
		if !message.validate(profile) {
			return ErrInvalidProviderRequest
		}
	}
	return nil
}

// Provider is intentionally narrower than a full SDK. Unsupported capabilities must fail
// explicitly instead of being simulated as successful provider effects.
type Provider interface {
	Profile() ProviderProfile
	Health(context.Context) error
	ProvisionUser(context.Context, ProviderUserProvision) (ExternalIdentityRef, ProviderEffectReceipt, error)
	CreateGroup(context.Context, ProviderGroupCreate) (ProviderConversationRef, ProviderEffectReceipt, error)
	AddMembers(context.Context, ProviderMemberUpdate) (ProviderEffectReceipt, error)
	ReadInbound(context.Context, string, int) (InboundPage, error)
	SendText(context.Context, ProviderTextMessage) (ProviderEffectReceipt, error)
}

// MessageMutationProvider is deliberately optional: Provider implementations may expose only the
// capabilities they have verified. Callers must check the type and profile capability before
// invoking these methods.
type MessageMutationProvider interface {
	EditText(context.Context, ProviderTextEdit) (ProviderEffectReceipt, error)
	RecallMessage(context.Context, ProviderMessageRecall) (ProviderEffectReceipt, error)
}

func validProviderIdempotencyKey(value string) bool {
	return value != "" && len(value) <= ProviderMaxIdempotencyBytes &&
		providerOpaqueIDPattern.MatchString(value) && !strings.Contains(value, "..")
}

func validProviderConversationRef(reference ProviderConversationRef, profile ProviderProfile) bool {
	return !reference.IsZero() && reference.Provider() == profile.Provider &&
		reference.RealmID() == profile.Realm && providerOpaqueIDPattern.MatchString(reference.SubjectID())
}
