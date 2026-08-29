package agentthread

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"unicode/utf8"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
	"golang.org/x/text/unicode/norm"
)

const (
	workCardSchemaVersion = 1
	workCardMessageType   = "agent_work_card"
	maxWorkCardBytes      = 1024
)

type WorkCardStatus string

const (
	WorkCardStarted   WorkCardStatus = "started"
	WorkCardRunning   WorkCardStatus = "running"
	WorkCardWaiting   WorkCardStatus = "waiting"
	WorkCardCompleted WorkCardStatus = "completed"
	WorkCardFailed    WorkCardStatus = "failed"
	WorkCardCancelled WorkCardStatus = "cancelled"
)

func (status WorkCardStatus) Valid() bool {
	return status == WorkCardStarted || status == WorkCardRunning || status == WorkCardWaiting ||
		status == WorkCardCompleted || status == WorkCardFailed || status == WorkCardCancelled
}

// ParentWorkCard is the deliberately limited parent-room projection. Prompt, response, artifact
// content, credentials, capabilities, and child ACLs are excluded; users open the child room for
// those details.
type ParentWorkCard struct {
	parent       im.ConversationRef
	child        im.ConversationRef
	invocationID im.InvocationID
	agentActor   im.ActorID
	status       WorkCardStatus
}

func NewParentWorkCard(
	parent im.ConversationRef,
	child im.ConversationRef,
	invocationID im.InvocationID,
	agentActor im.ActorID,
	status WorkCardStatus,
) (ParentWorkCard, error) {
	subjectType, hasSubjectType := agentActor.SubjectType()
	if parent.IsZero() || child.IsZero() || parent.TenantID() != child.TenantID() ||
		parent.ConversationID() == child.ConversationID() || invocationID.IsZero() ||
		!hasSubjectType || subjectType != im.SubjectAgent || !status.Valid() {
		return ParentWorkCard{}, ErrInvalidMention
	}
	return ParentWorkCard{
		parent: parent, child: child, invocationID: invocationID,
		agentActor: agentActor, status: status,
	}, nil
}

func (card ParentWorkCard) Parent() im.ConversationRef    { return card.parent }
func (card ParentWorkCard) Child() im.ConversationRef     { return card.child }
func (card ParentWorkCard) InvocationID() im.InvocationID { return card.invocationID }
func (card ParentWorkCard) AgentActor() im.ActorID        { return card.agentActor }
func (card ParentWorkCard) Status() WorkCardStatus        { return card.status }
func (card ParentWorkCard) IsZero() bool {
	return card.parent.IsZero() && card.child.IsZero() && card.invocationID.IsZero() &&
		card.agentActor.IsZero() && card.status == ""
}

type parentWorkCardWire struct {
	AgentActorID         string `json:"agentActorId"`
	ChildConversationID  string `json:"childConversationId"`
	InvocationID         string `json:"invocationId"`
	MessageType          string `json:"messageType"`
	ParentConversationID string `json:"parentConversationId"`
	SchemaVersion        int    `json:"schemaVersion"`
	Status               string `json:"status"`
}

func EncodeParentWorkCard(card ParentWorkCard) (string, error) {
	if _, err := NewParentWorkCard(
		card.parent, card.child, card.invocationID, card.agentActor, card.status,
	); err != nil {
		return "", ErrInvalidMention
	}
	wire := parentWorkCardWire{
		AgentActorID: card.agentActor.String(), ChildConversationID: card.child.ConversationID().String(),
		InvocationID: card.invocationID.String(), MessageType: workCardMessageType,
		ParentConversationID: card.parent.ConversationID().String(),
		SchemaVersion:        workCardSchemaVersion, Status: string(card.status),
	}
	encoded, err := json.Marshal(wire)
	if err != nil || len(encoded) > maxWorkCardBytes {
		return "", ErrInvalidMention
	}
	return string(encoded), nil
}

func DecodeParentWorkCard(raw string, tenant im.TenantID) (ParentWorkCard, error) {
	if raw == "" || len(raw) > maxWorkCardBytes || tenant.IsZero() || !utf8.ValidString(raw) ||
		!norm.NFC.IsNormalString(raw) {
		return ParentWorkCard{}, ErrInvalidMention
	}
	decoder := json.NewDecoder(bytes.NewBufferString(raw))
	decoder.DisallowUnknownFields()
	var wire parentWorkCardWire
	if err := decoder.Decode(&wire); err != nil {
		return ParentWorkCard{}, ErrInvalidMention
	}
	var trailing any
	if err := decoder.Decode(&trailing); !errors.Is(err, io.EOF) {
		return ParentWorkCard{}, ErrInvalidMention
	}
	canonical, err := json.Marshal(wire)
	if err != nil || string(canonical) != raw || wire.SchemaVersion != workCardSchemaVersion ||
		wire.MessageType != workCardMessageType {
		return ParentWorkCard{}, ErrInvalidMention
	}
	parentID, err := im.ParseConversationID(wire.ParentConversationID)
	if err != nil {
		return ParentWorkCard{}, ErrInvalidMention
	}
	childID, err := im.ParseConversationID(wire.ChildConversationID)
	if err != nil {
		return ParentWorkCard{}, ErrInvalidMention
	}
	invocationID, err := im.ParseInvocationID(wire.InvocationID)
	if err != nil {
		return ParentWorkCard{}, ErrInvalidMention
	}
	agentActor, err := im.ParseActorID(wire.AgentActorID)
	if err != nil {
		return ParentWorkCard{}, ErrInvalidMention
	}
	parent, err := im.NewConversationRef(tenant, parentID)
	if err != nil {
		return ParentWorkCard{}, ErrInvalidMention
	}
	child, err := im.NewConversationRef(tenant, childID)
	if err != nil {
		return ParentWorkCard{}, ErrInvalidMention
	}
	return NewParentWorkCard(parent, child, invocationID, agentActor, WorkCardStatus(wire.Status))
}
