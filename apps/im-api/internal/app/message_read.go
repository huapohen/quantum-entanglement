package app

import (
	"errors"
	"time"

	"github.com/gofiber/fiber/v3"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/adapters/httpapi"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/auth"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
	store "github.com/huapohen/quantum-entanglement/apps/im-api/internal/imstore"
)

func registerAuthenticatedMessageRoute(server *fiber.App, runtime RuntimeDependencies) {
	server.Get("/api/v1/tenants/:tenantId/conversations/:conversationId/messages", func(ctx fiber.Ctx) error {
		request, ok := auth.TrustedRequestContextFromContext(ctx.Context())
		if !ok {
			return httpapi.NewAppError(httpapi.CodeUnauthenticated, auth.ErrInvalidContext)
		}
		pathTenant, conversationID, reference, err := parseConversationRouteParams(ctx, request)
		if err != nil {
			return err
		}
		limit, err := eventPageLimit(ctx.Query("limit"))
		if err != nil {
			return httpapi.NewAppError(httpapi.CodeValidationFailed, err)
		}
		conversation, membership, access, err := readAuthorizedConversation(
			ctx.Context(), runtime, request, pathTenant, reference,
		)
		if err != nil {
			return mapTenantReadError(err)
		}
		if runtime.Messages == nil {
			return httpapi.NewAppError(httpapi.CodeDependencyUnavailable, store.ErrStoreUnavailable)
		}
		workspaceID, hasWorkspace := conversation.WorkspaceID()
		var workspaceReference *im.WorkspaceID
		if hasWorkspace {
			workspaceReference = &workspaceID
		}
		page, err := runtime.Messages.ReadPage(ctx.Context(), store.MessageReadPageQuery{
			Conversation: reference, AfterCursor: ctx.Query("after"), Limit: limit,
			WorkspaceID:          workspaceReference,
			ConversationRevision: conversation.Revision(), AccessRevision: access.Revision(),
		})
		if err != nil {
			return mapMessageReadError(err)
		}
		if err := validateMessageReadPage(
			page, reference, conversation.Revision(), limit, ctx.Query("after"),
		); err != nil {
			return httpapi.NewAppError(httpapi.CodeInternal, err)
		}
		values := make([]fiber.Map, 0, len(page.Messages))
		for _, message := range page.Messages {
			values = append(values, messageJSONValue(message))
		}
		return httpapi.WriteSuccess(ctx, fiber.Map{
			"tenantId":       pathTenant.String(),
			"conversationId": conversationID.String(),
			"messages":       values,
			"nextCursor":     page.NextCursor,
			"hasMore":        page.HasMore,
			"snapshot": fiber.Map{
				"conversationRevision": conversation.Revision(),
				"membershipRevision":   membership.Revision(),
				"accessRevision":       access.Revision(),
				"projectionRevision":   page.ProjectionRevision,
			},
		})
	})
}

func validateMessageReadPage(
	page store.MessageReadPage,
	reference im.ConversationRef,
	conversationRevision uint64,
	limit uint32,
	after string,
) error {
	if page.Conversation != reference || page.ConversationRevision != conversationRevision ||
		conversationRevision == 0 || len(page.Messages) > int(limit) ||
		(page.HasMore && page.NextCursor == after) || (page.HasMore && page.NextCursor == "") {
		return store.ErrIntegrity
	}
	if page.ConversationRevision == 0 ||
		(page.ProjectionRevision == 0 && (len(page.Messages) != 0 || after != "")) {
		return store.ErrIntegrity
	}
	seen := make(map[im.MessageID]struct{}, len(page.Messages))
	for _, message := range page.Messages {
		if message.IsZero() || message.Ref().ConversationRef() != reference ||
			message.Sender().TenantID() != reference.TenantID() || message.Revision() == 0 ||
			!message.MessageType().Valid() || !message.Status().Valid() ||
			message.CreatedAt().IsZero() || message.CreatedAt().Location() != timeUTC() {
			return store.ErrIntegrity
		}
		if _, exists := seen[message.Ref().MessageID()]; exists {
			return store.ErrIntegrity
		}
		seen[message.Ref().MessageID()] = struct{}{}
	}
	return nil
}

func messageJSONValue(message im.MessageSnapshot) fiber.Map {
	value := fiber.Map{
		"id":              message.Ref().MessageID().String(),
		"clientMessageId": message.ClientMessageID().String(),
		"conversationId":  message.Ref().ConversationRef().ConversationID().String(),
		"senderActorId":   message.Sender().ActorID().String(),
		"type":            string(message.MessageType()),
		"status":          string(message.Status()),
		"text":            message.Text(),
		"extInfo":         message.ExtInfo(),
		"createdAt":       message.CreatedAt().UTC().Format("2006-01-02T15:04:05.999999Z07:00"),
		"revision":        message.Revision(),
	}
	return value
}

func mapMessageReadError(err error) error {
	switch {
	case errors.Is(err, store.ErrInvalidRequest), errors.Is(err, store.ErrRevisionConflict):
		return httpapi.NewAppError(httpapi.CodeValidationFailed, err)
	case errors.Is(err, store.ErrNotFound):
		return httpapi.NewAppError(httpapi.CodeNotFound, err)
	case errors.Is(err, store.ErrStoreUnavailable):
		return httpapi.NewAppError(httpapi.CodeDependencyUnavailable, err)
	case errors.Is(err, store.ErrIntegrity):
		return httpapi.NewAppError(httpapi.CodeInternal, err)
	default:
		return httpapi.NewAppError(httpapi.CodeInternal, err)
	}
}

// timeUTC is kept as a function to make the validation expression explicit and testable without
// accepting a location-equivalent timestamp that is not actually UTC.
func timeUTC() *time.Location { return time.UTC }
