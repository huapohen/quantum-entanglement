package im

import (
	"errors"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	"golang.org/x/text/unicode/norm"
)

var ErrInvalidMessage = errors.New("invalid IM message")

const (
	MessageTextMaxBytes    = 64 * 1024
	MessageExtInfoMaxBytes = 64 * 1024
)

type MessageType string

const (
	MessageTypeText   MessageType = "text"
	MessageTypeSystem MessageType = "system"
)

func (messageType MessageType) Valid() bool {
	return messageType == MessageTypeText || messageType == MessageTypeSystem
}

type MessageStatus string

const (
	MessageStatusActive   MessageStatus = "active"
	MessageStatusEdited   MessageStatus = "edited"
	MessageStatusRecalled MessageStatus = "recalled"
)

func (status MessageStatus) Valid() bool {
	return status == MessageStatusActive || status == MessageStatusEdited || status == MessageStatusRecalled
}

// MessageRef is the stable platform identity of one message in one tenant-scoped conversation.
// It is deliberately separate from provider message IDs and transport receipts.
type MessageRef struct {
	conversationRef ConversationRef
	messageID       MessageID
}

func NewMessageRef(conversationRef ConversationRef, messageID MessageID) (MessageRef, error) {
	if conversationRef.IsZero() || messageID.IsZero() {
		return MessageRef{}, ErrInvalidMessage
	}
	return MessageRef{conversationRef: conversationRef, messageID: messageID}, nil
}

func (reference MessageRef) ConversationRef() ConversationRef { return reference.conversationRef }
func (reference MessageRef) MessageID() MessageID             { return reference.messageID }
func (reference MessageRef) IsZero() bool {
	return reference.conversationRef.IsZero() && reference.messageID.IsZero()
}

// MessageSnapshot is an immutable platform message revision. ClientMessageID is retained so a
// local client retry can be reconciled with the platform MessageID; provider IDs remain adapter
// mappings and never become business authorization facts.
type MessageSnapshot struct {
	reference       MessageRef
	sender          ActorRef
	clientMessageID MessageID
	messageType     MessageType
	status          MessageStatus
	text            string
	extInfo         string
	createdAt       time.Time
	revision        uint64
}

func NewMessageSnapshot(
	reference MessageRef,
	sender ActorRef,
	clientMessageID MessageID,
	messageType MessageType,
	status MessageStatus,
	text string,
	extInfo string,
	createdAt time.Time,
	revision uint64,
) (MessageSnapshot, error) {
	if reference.IsZero() || sender.IsZero() || sender.TenantID() != reference.ConversationRef().TenantID() ||
		clientMessageID.IsZero() || !messageType.Valid() || !status.Valid() ||
		!validMessageText(messageType, status, text) || !validMessageExtInfo(extInfo) ||
		createdAt.IsZero() || createdAt.Location() != time.UTC || !validPersistentRevision(revision) {
		return MessageSnapshot{}, ErrInvalidMessage
	}
	return MessageSnapshot{
		reference: reference, sender: sender, clientMessageID: clientMessageID,
		messageType: messageType, status: status, text: text, extInfo: extInfo,
		createdAt: createdAt.Round(0), revision: revision,
	}, nil
}

func (snapshot MessageSnapshot) Ref() MessageRef            { return snapshot.reference }
func (snapshot MessageSnapshot) Sender() ActorRef           { return snapshot.sender }
func (snapshot MessageSnapshot) ClientMessageID() MessageID { return snapshot.clientMessageID }
func (snapshot MessageSnapshot) MessageType() MessageType   { return snapshot.messageType }
func (snapshot MessageSnapshot) Status() MessageStatus      { return snapshot.status }
func (snapshot MessageSnapshot) Text() string               { return snapshot.text }
func (snapshot MessageSnapshot) ExtInfo() string            { return snapshot.extInfo }
func (snapshot MessageSnapshot) CreatedAt() time.Time       { return snapshot.createdAt }
func (snapshot MessageSnapshot) Revision() uint64           { return snapshot.revision }
func (snapshot MessageSnapshot) IsZero() bool {
	return snapshot.reference.IsZero() && snapshot.sender.IsZero() && snapshot.clientMessageID.IsZero() &&
		snapshot.messageType == "" && snapshot.status == "" && snapshot.text == "" && snapshot.extInfo == "" &&
		snapshot.createdAt.IsZero() && snapshot.revision == 0
}

func validMessageText(messageType MessageType, status MessageStatus, text string) bool {
	if status == MessageStatusRecalled && text == "" {
		return true
	}
	if text == "" || len(text) > MessageTextMaxBytes || !utf8.ValidString(text) ||
		!norm.NFC.IsNormalString(text) || strings.TrimSpace(text) != text {
		return false
	}
	for _, character := range text {
		if unicode.IsControl(character) && character != '\n' && character != '\t' && character != '\r' {
			return false
		}
	}
	if status == MessageStatusRecalled && messageType == MessageTypeSystem {
		return false
	}
	return true
}

func validMessageExtInfo(value string) bool {
	return len(value) <= MessageExtInfoMaxBytes && utf8.ValidString(value) &&
		(value == "" || (norm.NFC.IsNormalString(value) && strings.TrimSpace(value) == value))
}
