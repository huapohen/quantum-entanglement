package improjection

import (
	"context"
	"encoding/base64"
	"errors"
	"strconv"
	"strings"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/events"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
	store "github.com/huapohen/quantum-entanglement/apps/im-api/internal/imstore"
)

const (
	messageReplayEventPageSize = 256
	maxMessageReplayEvents     = 4096
	messageCursorVersion       = "wanwork-message-replay-v1"
)

// EventReplayMessageReader exposes a durable read model directly from the event source while the
// materialized PostgreSQL message-head migration is still being introduced. It is bounded and
// fails closed on stream drift: an opaque page cursor is valid only for the exact stream version
// from which it was issued. It never writes a checkpoint and never treats a cursor as authority.
type EventReplayMessageReader struct {
	events events.EventStore
}

var _ store.MessageReadRepository = (*EventReplayMessageReader)(nil)

func NewEventReplayMessageReader(eventStore events.EventStore) (*EventReplayMessageReader, error) {
	if eventStore == nil {
		return nil, store.ErrInvalidRequest
	}
	return &EventReplayMessageReader{events: eventStore}, nil
}

func (reader *EventReplayMessageReader) ReadPage(
	ctx context.Context,
	query store.MessageReadPageQuery,
) (store.MessageReadPage, error) {
	if ctx == nil || ctx.Err() != nil || reader == nil || reader.events == nil ||
		query.Conversation.IsZero() || query.Limit == 0 || query.Limit > 256 ||
		query.ConversationRevision == 0 || query.AccessRevision == 0 {
		return store.MessageReadPage{}, store.ErrInvalidRequest
	}
	binding := messageCursorBinding{
		tenantID:       query.Conversation.TenantID().String(),
		conversationID: query.Conversation.ConversationID().String(),
		workspaceID:    messageWorkspaceValue(query.WorkspaceID),
		workspaceSet:   query.WorkspaceID != nil,
	}
	cursor, err := decodeMessageCursor(query.AfterCursor, binding)
	if err != nil {
		return store.MessageReadPage{}, err
	}
	projection, err := NewMessageProjection(query.Conversation)
	if err != nil {
		return store.MessageReadPage{}, store.ErrIntegrity
	}
	streamVersion, err := reader.replay(ctx, query, projection)
	if err != nil {
		return store.MessageReadPage{}, err
	}
	messages := projection.Messages()
	if query.AfterCursor != "" && cursor.streamVersion != streamVersion {
		return store.MessageReadPage{}, store.ErrRevisionConflict
	}
	if cursor.offset > uint64(len(messages)) {
		return store.MessageReadPage{}, store.ErrInvalidRequest
	}
	end := cursor.offset + uint64(query.Limit)
	if end > uint64(len(messages)) {
		end = uint64(len(messages))
	}
	pageMessages := append([]im.MessageSnapshot(nil), messages[cursor.offset:end]...)
	hasMore := end < uint64(len(messages))
	nextCursor := ""
	if hasMore {
		nextCursor, err = encodeMessageCursor(binding, messagePageCursor{
			streamVersion: streamVersion,
			offset:        end,
		})
		if err != nil {
			return store.MessageReadPage{}, store.ErrIntegrity
		}
	}
	return store.MessageReadPage{
		Conversation: query.Conversation, Messages: pageMessages,
		NextCursor: nextCursor, HasMore: hasMore,
		ConversationRevision: query.ConversationRevision,
		ProjectionRevision:   streamVersion,
	}, nil
}

func (reader *EventReplayMessageReader) replay(
	ctx context.Context,
	query store.MessageReadPageQuery,
	projection *MessageProjection,
) (uint64, error) {
	var after events.Cursor
	var streamVersion uint64
	count := 0
	workspace := messageWorkspaceString(query.WorkspaceID)
	for {
		page, err := reader.events.ReadStreamPage(ctx, events.StreamQuery{
			TenantID: query.Conversation.TenantID().String(), WorkspaceID: workspace,
			StreamID: query.Conversation.ConversationID().String(), After: after,
			Limit: messageReplayEventPageSize,
		})
		if err != nil {
			return 0, mapMessageReplayStoreError(err)
		}
		if page.HasMore && (len(page.Events) == 0 || page.Next == after) {
			return 0, store.ErrIntegrity
		}
		for _, event := range page.Events {
			count++
			if count > maxMessageReplayEvents || event.Sequence <= streamVersion {
				return 0, store.ErrStoreUnavailable
			}
			streamVersion = event.Sequence
			if !isMessageProjectionEvent(event.EventType) {
				continue
			}
			if err := projection.Apply(ctx, event); err != nil {
				return 0, store.ErrIntegrity
			}
		}
		if !page.HasMore {
			return streamVersion, nil
		}
		after = page.Next
	}
}

func isMessageProjectionEvent(eventType string) bool {
	return eventType == messageCreatedEvent || eventType == messageEditedEvent ||
		eventType == messageRecalledEvent
}

func mapMessageReplayStoreError(err error) error {
	switch {
	case errors.Is(err, events.ErrInvalidCursor), errors.Is(err, events.ErrInvalidQuery):
		return store.ErrInvalidRequest
	case errors.Is(err, events.ErrStoreUnavailable), errors.Is(err, events.ErrStoreCapacity):
		return store.ErrStoreUnavailable
	default:
		return store.ErrIntegrity
	}
}

type messageCursorBinding struct {
	tenantID       string
	workspaceID    string
	workspaceSet   bool
	conversationID string
}

type messagePageCursor struct {
	streamVersion uint64
	offset        uint64
}

func encodeMessageCursor(binding messageCursorBinding, cursor messagePageCursor) (string, error) {
	if binding.tenantID == "" || binding.conversationID == "" || cursor.streamVersion == 0 ||
		cursor.offset == 0 {
		return "", store.ErrIntegrity
	}
	workspaceSet := "0"
	if binding.workspaceSet {
		workspaceSet = "1"
	}
	raw := strings.Join([]string{
		messageCursorVersion, binding.tenantID, workspaceSet, binding.workspaceID,
		binding.conversationID, strconv.FormatUint(cursor.streamVersion, 10),
		strconv.FormatUint(cursor.offset, 10),
	}, "\n")
	return base64.RawURLEncoding.EncodeToString([]byte(raw)), nil
}

func decodeMessageCursor(raw string, binding messageCursorBinding) (messagePageCursor, error) {
	if raw == "" {
		return messagePageCursor{}, nil
	}
	if len(raw) > 2048 || strings.TrimSpace(raw) != raw {
		return messagePageCursor{}, store.ErrInvalidRequest
	}
	decoded, err := base64.RawURLEncoding.Strict().DecodeString(raw)
	if err != nil {
		return messagePageCursor{}, store.ErrInvalidRequest
	}
	parts := strings.Split(string(decoded), "\n")
	if len(parts) != 7 || parts[0] != messageCursorVersion || parts[1] != binding.tenantID ||
		parts[3] != binding.workspaceID || parts[4] != binding.conversationID ||
		(parts[2] == "1") != binding.workspaceSet || (parts[2] != "0" && parts[2] != "1") {
		return messagePageCursor{}, store.ErrInvalidRequest
	}
	version, err := strconv.ParseUint(parts[5], 10, 64)
	if err != nil || version == 0 || strconv.FormatUint(version, 10) != parts[5] {
		return messagePageCursor{}, store.ErrInvalidRequest
	}
	offset, err := strconv.ParseUint(parts[6], 10, 64)
	if err != nil || offset == 0 || strconv.FormatUint(offset, 10) != parts[6] {
		return messagePageCursor{}, store.ErrInvalidRequest
	}
	return messagePageCursor{streamVersion: version, offset: offset}, nil
}

func messageWorkspaceValue(workspace *im.WorkspaceID) string {
	if workspace == nil {
		return ""
	}
	return workspace.String()
}

func messageWorkspaceString(workspace *im.WorkspaceID) *string {
	if workspace == nil {
		return nil
	}
	value := workspace.String()
	return &value
}
