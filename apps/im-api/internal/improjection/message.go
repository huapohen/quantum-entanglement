// Package improjection contains provider-neutral reducers for IM projections. It depends only on
// the platform IM value objects and the durable event port; it does not own storage, authority,
// provider delivery, or Agent execution.
package improjection

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"sort"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/events"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
)

var (
	ErrInvalidProjection  = errors.New("invalid IM message projection")
	ErrProjectionScope    = errors.New("IM message event is outside projection scope")
	ErrProjectionOrder    = errors.New("IM message event order is invalid")
	ErrProjectionConflict = errors.New("IM message projection conflict")
)

const (
	messageCreatedEvent  = "message.created"
	messageEditedEvent   = "message.edited"
	messageRecalledEvent = "message.recalled"
)

// MessageProjection is a deterministic, replay-safe reducer for one platform conversation.
// It is intentionally volatile: persistence and checkpoint ownership belong to the caller's
// events.Projector and durable projection store.
type MessageProjection struct {
	conversationRef im.ConversationRef
	messages        map[im.MessageID]im.MessageSnapshot
	seenEvents      map[string]struct{}
	lastSequence    uint64
}

func NewMessageProjection(reference im.ConversationRef) (*MessageProjection, error) {
	if reference.IsZero() {
		return nil, ErrInvalidProjection
	}
	return &MessageProjection{
		conversationRef: reference,
		messages:        make(map[im.MessageID]im.MessageSnapshot),
		seenEvents:      make(map[string]struct{}),
	}, nil
}

func (projection *MessageProjection) Apply(ctx context.Context, event events.StoredEvent) error {
	if err := projectionContextError(ctx); err != nil {
		return err
	}
	if projection == nil || projection.conversationRef.IsZero() || projection.messages == nil ||
		projection.seenEvents == nil {
		return ErrInvalidProjection
	}
	if err := validateScopedEvent(projection.conversationRef, event); err != nil {
		return err
	}
	if _, replayed := projection.seenEvents[event.EventID]; replayed {
		return nil
	}
	if event.Sequence <= projection.lastSequence {
		return ErrProjectionOrder
	}

	payload, err := inlinePayload(event)
	if err != nil {
		return err
	}
	var next im.MessageSnapshot
	switch event.EventType {
	case messageCreatedEvent:
		next, err = projection.created(event, payload)
	case messageEditedEvent:
		next, err = projection.edited(event, payload)
	case messageRecalledEvent:
		next, err = projection.recalled(event, payload)
	default:
		return ErrInvalidProjection
	}
	if err != nil {
		return err
	}
	projection.messages[next.Ref().MessageID()] = next
	projection.seenEvents[event.EventID] = struct{}{}
	projection.lastSequence = event.Sequence
	return nil
}

func (projection *MessageProjection) created(
	event events.StoredEvent,
	payload []byte,
) (im.MessageSnapshot, error) {
	var value struct {
		ConversationID  string
		MessageID       string
		ClientMessageID string
		MessageType     string
		Text            string
		ExtInfo         string
	}
	if err := decodeExactObject(payload, map[string]fieldDecoder{
		"conversationId":  stringDecoder(&value.ConversationID),
		"messageId":       stringDecoder(&value.MessageID),
		"clientMessageId": stringDecoder(&value.ClientMessageID),
		"messageType":     stringDecoder(&value.MessageType),
		"text":            stringDecoder(&value.Text),
		"extInfo":         stringDecoder(&value.ExtInfo),
	}, "conversationId", "messageId", "clientMessageId", "messageType", "text"); err != nil {
		return im.MessageSnapshot{}, err
	}
	if value.ConversationID != projection.conversationRef.ConversationID().String() {
		return im.MessageSnapshot{}, ErrProjectionScope
	}
	messageID, err := im.ParseMessageID(value.MessageID)
	if err != nil {
		return im.MessageSnapshot{}, ErrInvalidProjection
	}
	clientMessageID, err := im.ParseMessageID(value.ClientMessageID)
	if err != nil {
		return im.MessageSnapshot{}, ErrInvalidProjection
	}
	if _, exists := projection.messages[messageID]; exists {
		return im.MessageSnapshot{}, ErrProjectionConflict
	}
	actorID, err := im.ParseActorID(event.ActorID)
	if err != nil {
		return im.MessageSnapshot{}, ErrInvalidProjection
	}
	actor, err := im.NewActorRef(projection.conversationRef.TenantID(), actorID)
	if err != nil {
		return im.MessageSnapshot{}, ErrInvalidProjection
	}
	reference, err := im.NewMessageRef(projection.conversationRef, messageID)
	if err != nil {
		return im.MessageSnapshot{}, ErrInvalidProjection
	}
	return im.NewMessageSnapshot(
		reference, actor, clientMessageID, im.MessageType(value.MessageType), im.MessageStatusActive,
		value.Text, value.ExtInfo, event.OccurredAt.UTC(), 1,
	)
}

func (projection *MessageProjection) edited(
	event events.StoredEvent,
	payload []byte,
) (im.MessageSnapshot, error) {
	var value struct {
		ConversationID string
		MessageID      string
		Text           string
	}
	if err := decodeExactObject(payload, map[string]fieldDecoder{
		"conversationId": stringDecoder(&value.ConversationID),
		"messageId":      stringDecoder(&value.MessageID),
		"text":           stringDecoder(&value.Text),
	}, "conversationId", "messageId", "text"); err != nil {
		return im.MessageSnapshot{}, err
	}
	if value.ConversationID != projection.conversationRef.ConversationID().String() {
		return im.MessageSnapshot{}, ErrProjectionScope
	}
	messageID, err := im.ParseMessageID(value.MessageID)
	if err != nil {
		return im.MessageSnapshot{}, ErrInvalidProjection
	}
	previous, exists := projection.messages[messageID]
	if !exists || previous.Status() == im.MessageStatusRecalled {
		return im.MessageSnapshot{}, ErrProjectionConflict
	}
	return im.NewMessageSnapshot(
		previous.Ref(), previous.Sender(), previous.ClientMessageID(), previous.MessageType(),
		im.MessageStatusEdited, value.Text, previous.ExtInfo(), previous.CreatedAt(), previous.Revision()+1,
	)
}

func (projection *MessageProjection) recalled(
	event events.StoredEvent,
	payload []byte,
) (im.MessageSnapshot, error) {
	var value struct {
		ConversationID string
		MessageID      string
	}
	if err := decodeExactObject(payload, map[string]fieldDecoder{
		"conversationId": stringDecoder(&value.ConversationID),
		"messageId":      stringDecoder(&value.MessageID),
	}, "conversationId", "messageId"); err != nil {
		return im.MessageSnapshot{}, err
	}
	if value.ConversationID != projection.conversationRef.ConversationID().String() {
		return im.MessageSnapshot{}, ErrProjectionScope
	}
	messageID, err := im.ParseMessageID(value.MessageID)
	if err != nil {
		return im.MessageSnapshot{}, ErrInvalidProjection
	}
	previous, exists := projection.messages[messageID]
	if !exists || previous.Status() == im.MessageStatusRecalled {
		return im.MessageSnapshot{}, ErrProjectionConflict
	}
	return im.NewMessageSnapshot(
		previous.Ref(), previous.Sender(), previous.ClientMessageID(), previous.MessageType(),
		im.MessageStatusRecalled, "", previous.ExtInfo(), previous.CreatedAt(), previous.Revision()+1,
	)
}

// Messages returns independent, deterministic snapshots sorted by creation time and message ID.
// It is a read snapshot, not a durable checkpoint or authorization decision.
func (projection *MessageProjection) Messages() []im.MessageSnapshot {
	if projection == nil {
		return nil
	}
	values := make([]im.MessageSnapshot, 0, len(projection.messages))
	for _, message := range projection.messages {
		values = append(values, message)
	}
	sort.Slice(values, func(left, right int) bool {
		if values[left].CreatedAt().Equal(values[right].CreatedAt()) {
			return values[left].Ref().MessageID().String() < values[right].Ref().MessageID().String()
		}
		return values[left].CreatedAt().Before(values[right].CreatedAt())
	})
	return values
}

func (projection *MessageProjection) LastSequence() uint64 {
	if projection == nil {
		return 0
	}
	return projection.lastSequence
}

func validateScopedEvent(reference im.ConversationRef, event events.StoredEvent) error {
	if event.Sequence == 0 || event.GlobalPosition == 0 || event.TenantID != reference.TenantID().String() ||
		event.StreamID != reference.ConversationID().String() || events.ValidateEventToAppend(event.EventToAppend) != nil {
		return ErrProjectionScope
	}
	return nil
}

func inlinePayload(event events.StoredEvent) ([]byte, error) {
	if event.Payload.Kind() != events.PayloadInline {
		return nil, ErrInvalidProjection
	}
	raw := event.Payload.InlineJSON()
	if len(raw) == 0 || !json.Valid(raw) {
		return nil, ErrInvalidProjection
	}
	return raw, nil
}

type fieldDecoder func(json.RawMessage) error

func stringDecoder(target *string) fieldDecoder {
	return func(raw json.RawMessage) error {
		var value string
		decoder := json.NewDecoder(bytes.NewReader(raw))
		if err := decoder.Decode(&value); err != nil {
			return ErrInvalidProjection
		}
		if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
			return ErrInvalidProjection
		}
		*target = value
		return nil
	}
}

func decodeExactObject(raw []byte, fields map[string]fieldDecoder, required ...string) error {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	var object map[string]json.RawMessage
	if err := decoder.Decode(&object); err != nil || object == nil {
		return ErrInvalidProjection
	}
	if _, err := decoder.Token(); !errors.Is(err, io.EOF) {
		return ErrInvalidProjection
	}
	for key, value := range object {
		decode, ok := fields[key]
		if !ok || decode(value) != nil {
			return ErrInvalidProjection
		}
	}
	for _, key := range required {
		if _, ok := object[key]; !ok {
			return ErrInvalidProjection
		}
	}
	return nil
}

func projectionContextError(ctx context.Context) error {
	if ctx == nil {
		return ErrInvalidProjection
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	return nil
}
