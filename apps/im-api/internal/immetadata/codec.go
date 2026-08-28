package immetadata

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"unicode/utf8"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
	"golang.org/x/text/unicode/norm"
)

const maxProviderMetadataBytes = 1024

type userProjectionWire struct {
	SchemaVersion     int    `json:"schemaVersion"`
	SubjectType       string `json:"subjectType"`
	PlatformActorID   string `json:"platformActorId"`
	AgentDefinitionID string `json:"agentDefinitionId,omitempty"`
	AgentVersion      string `json:"agentVersion,omitempty"`
}

type conversationProjectionWire struct {
	SchemaVersion          int    `json:"schemaVersion"`
	ConversationType       string `json:"conversationType"`
	PlatformConversationID string `json:"platformConversationId"`
	ParentConversationID   string `json:"parentConversationId,omitempty"`
	RootMessageID          string `json:"rootMessageId,omitempty"`
	AgentInvocationID      string `json:"agentInvocationId,omitempty"`
}

func EncodeUserProjection(projection UserProjection) (string, error) {
	if _, err := NewUserProjection(
		projection.subjectType,
		projection.platformActorID,
		projection.agentDefinition,
		projection.agentVersion,
	); err != nil {
		return "", ErrInvalidProviderMetadata
	}

	wire := userProjectionWire{
		SchemaVersion:     SchemaVersion,
		SubjectType:       string(projection.subjectType),
		PlatformActorID:   projection.platformActorID.String(),
		AgentDefinitionID: projection.agentDefinition.String(),
		AgentVersion:      projection.agentVersion.String(),
	}
	return encodeCanonical(wire)
}

func DecodeUserProjection(raw string) (UserProjection, error) {
	var wire userProjectionWire
	if err := decodeCanonical(raw, &wire); err != nil {
		return UserProjection{}, err
	}
	if wire.SchemaVersion != SchemaVersion {
		return UserProjection{}, ErrInvalidProviderMetadata
	}

	actorID, err := im.ParseActorID(wire.PlatformActorID)
	if err != nil {
		return UserProjection{}, ErrInvalidProviderMetadata
	}

	var agentDefinitionID im.AgentDefinitionID
	var agentVersion im.AgentVersion
	subjectType := im.SubjectType(wire.SubjectType)
	if subjectType == im.SubjectAgent {
		agentDefinitionID, err = im.ParseAgentDefinitionID(wire.AgentDefinitionID)
		if err != nil {
			return UserProjection{}, ErrInvalidProviderMetadata
		}
		agentVersion, err = im.ParseAgentVersion(wire.AgentVersion)
		if err != nil {
			return UserProjection{}, ErrInvalidProviderMetadata
		}
	}

	projection, err := NewUserProjection(
		subjectType,
		actorID,
		agentDefinitionID,
		agentVersion,
	)
	if err != nil {
		return UserProjection{}, ErrInvalidProviderMetadata
	}
	return projection, nil
}

func EncodeConversationProjection(projection ConversationProjection) (string, error) {
	if _, err := NewConversationProjection(
		projection.conversationType,
		projection.platformConversation,
		projection.parentConversation,
		projection.rootMessage,
		projection.agentInvocation,
	); err != nil {
		return "", ErrInvalidProviderMetadata
	}

	wire := conversationProjectionWire{
		SchemaVersion:          SchemaVersion,
		ConversationType:       string(projection.conversationType),
		PlatformConversationID: projection.platformConversation.String(),
		ParentConversationID:   projection.parentConversation.String(),
		RootMessageID:          projection.rootMessage.String(),
		AgentInvocationID:      projection.agentInvocation.String(),
	}
	return encodeCanonical(wire)
}

func DecodeConversationProjection(raw string) (ConversationProjection, error) {
	var wire conversationProjectionWire
	if err := decodeCanonical(raw, &wire); err != nil {
		return ConversationProjection{}, err
	}
	if wire.SchemaVersion != SchemaVersion {
		return ConversationProjection{}, ErrInvalidProviderMetadata
	}

	conversationID, err := im.ParseConversationID(wire.PlatformConversationID)
	if err != nil {
		return ConversationProjection{}, ErrInvalidProviderMetadata
	}

	var parentConversationID im.ConversationID
	if wire.ParentConversationID != "" {
		parentConversationID, err = im.ParseConversationID(wire.ParentConversationID)
		if err != nil {
			return ConversationProjection{}, ErrInvalidProviderMetadata
		}
	}
	var rootMessageID im.MessageID
	if wire.RootMessageID != "" {
		rootMessageID, err = im.ParseMessageID(wire.RootMessageID)
		if err != nil {
			return ConversationProjection{}, ErrInvalidProviderMetadata
		}
	}
	var agentInvocationID im.InvocationID
	if wire.AgentInvocationID != "" {
		agentInvocationID, err = im.ParseInvocationID(wire.AgentInvocationID)
		if err != nil {
			return ConversationProjection{}, ErrInvalidProviderMetadata
		}
	}

	projection, err := NewConversationProjection(
		im.ConversationType(wire.ConversationType),
		conversationID,
		parentConversationID,
		rootMessageID,
		agentInvocationID,
	)
	if err != nil {
		return ConversationProjection{}, ErrInvalidProviderMetadata
	}
	return projection, nil
}

func encodeCanonical(wire any) (string, error) {
	encoded, err := json.Marshal(wire)
	if err != nil {
		return "", ErrInvalidProviderMetadata
	}
	if len(encoded) > maxProviderMetadataBytes {
		return "", ErrProviderMetadataTooLarge
	}
	return string(encoded), nil
}

func decodeCanonical(raw string, destination any) error {
	if raw == "" {
		return ErrInvalidProviderMetadata
	}
	if len(raw) > maxProviderMetadataBytes {
		return ErrProviderMetadataTooLarge
	}
	if !utf8.ValidString(raw) || !norm.NFC.IsNormalString(raw) {
		return ErrInvalidProviderMetadata
	}

	decoder := json.NewDecoder(bytes.NewBufferString(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(destination); err != nil {
		return ErrInvalidProviderMetadata
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return ErrInvalidProviderMetadata
	}

	canonical, err := json.Marshal(destination)
	if err != nil || string(canonical) != raw {
		return ErrInvalidProviderMetadata
	}
	return nil
}
