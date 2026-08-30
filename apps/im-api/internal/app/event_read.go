package app

import (
	"context"
	"encoding/json"
	"errors"
	"strconv"
	"strings"

	"github.com/gofiber/fiber/v3"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/adapters/httpapi"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/auth"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/events"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
	store "github.com/huapohen/quantum-entanglement/apps/im-api/internal/imstore"
)

const (
	defaultEventPageLimit uint32 = 50
	maxEventPageLimit     uint32 = 256
)

// registerAuthenticatedEventRoute exposes only a tenant-authorized, read-only event stream.
// The conversation stream ID is deliberately the platform ConversationID; provider IDs and
// transport channels never select a stream. EventStore remains an optional composition dependency
// until the PostgreSQL implementation can read through the same UoW snapshot as the authority
// query. Missing composition is therefore an explicit 503, never an empty successful page.
func registerAuthenticatedEventRoute(server *fiber.App, runtime RuntimeDependencies) {
	server.Get("/api/v1/tenants/:tenantId/conversations/:conversationId/events", func(ctx fiber.Ctx) error {
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
		if runtime.EventStore == nil {
			return httpapi.NewAppError(httpapi.CodeDependencyUnavailable, events.ErrStoreUnavailable)
		}
		workspace, hasWorkspace := conversation.WorkspaceID()
		query := events.StreamQuery{
			TenantID: pathTenant.String(), StreamID: conversationID.String(),
			After: events.Cursor(ctx.Query("after")), Limit: limit,
		}
		if hasWorkspace {
			workspaceValue := workspace.String()
			query.WorkspaceID = &workspaceValue
		}
		page, err := runtime.EventStore.ReadStreamPage(ctx.Context(), query)
		if err != nil {
			return mapEventPageError(err)
		}
		if err := validateAuthorizedEventPage(page, query); err != nil {
			return httpapi.NewAppError(httpapi.CodeInternal, err)
		}

		values := make([]fiber.Map, 0, len(page.Events))
		for _, event := range page.Events {
			value, marshalErr := eventJSONValue(event)
			if marshalErr != nil {
				return httpapi.NewAppError(httpapi.CodeInternal, marshalErr)
			}
			values = append(values, value)
		}
		return httpapi.WriteSuccess(ctx, fiber.Map{
			"tenantId":       pathTenant.String(),
			"conversationId": conversationID.String(),
			"events":         values,
			"nextCursor":     string(page.Next),
			"hasMore":        page.HasMore,
			"snapshot": fiber.Map{
				"conversationRevision": conversation.Revision(),
				"membershipRevision":   membership.Revision(),
				"accessRevision":       access.Revision(),
				"afterCursor":          string(query.After),
				"nextCursor":           string(page.Next),
			},
		})
	})
}

func parseConversationRouteParams(
	ctx fiber.Ctx,
	request auth.TrustedRequestContext,
) (im.TenantID, im.ConversationID, im.ConversationRef, error) {
	pathTenant, err := im.ParseTenantID(ctx.Params("tenantId"))
	if err != nil {
		return im.TenantID{}, im.ConversationID{}, im.ConversationRef{},
			httpapi.NewAppError(httpapi.CodeMalformedRequest, err)
	}
	if pathTenant != request.TenantID() {
		return im.TenantID{}, im.ConversationID{}, im.ConversationRef{},
			httpapi.NewAppError(httpapi.CodeForbidden, auth.ErrContextUnauthorized)
	}
	conversationID, err := im.ParseConversationID(ctx.Params("conversationId"))
	if err != nil {
		return im.TenantID{}, im.ConversationID{}, im.ConversationRef{},
			httpapi.NewAppError(httpapi.CodeMalformedRequest, err)
	}
	reference, err := im.NewConversationRef(pathTenant, conversationID)
	if err != nil {
		return im.TenantID{}, im.ConversationID{}, im.ConversationRef{},
			httpapi.NewAppError(httpapi.CodeMalformedRequest, err)
	}
	return pathTenant, conversationID, reference, nil
}

func eventPageLimit(raw string) (uint32, error) {
	if raw == "" {
		return defaultEventPageLimit, nil
	}
	if strings.TrimSpace(raw) != raw {
		return 0, events.ErrInvalidQuery
	}
	value, err := strconv.ParseUint(raw, 10, 32)
	if err != nil || value == 0 || value > uint64(maxEventPageLimit) {
		return 0, events.ErrInvalidQuery
	}
	return uint32(value), nil
}

func readAuthorizedConversation(
	ctx context.Context,
	runtime RuntimeDependencies,
	request auth.TrustedRequestContext,
	tenant im.TenantID,
	reference im.ConversationRef,
) (im.ConversationSnapshot, im.ConversationMembershipSnapshot, im.ConversationAccessSnapshot, error) {
	var conversation im.ConversationSnapshot
	var membership im.ConversationMembershipSnapshot
	var access im.ConversationAccessSnapshot
	readErr := runtime.Persistence.Read(ctx, tenant, func(
		readContext context.Context,
		repositories store.TenantRepositories,
	) error {
		if repositories == nil || repositories.Identity() == nil ||
			repositories.Conversations() == nil || repositories.Authority() == nil {
			return auth.ErrContextUnavailable
		}
		freshRequest, resolveErr := auth.ResolveTrustedRequestContext(
			readContext,
			runtime.Verifier.Profile(), request.Identity(), tenant,
			repositories.Identity(), runtime.Now(),
		)
		if resolveErr != nil {
			return resolveErr
		}
		if freshRequest.ActorRef() != request.ActorRef() {
			return auth.ErrContextUnauthorized
		}
		var operationErr error
		conversation, operationErr = repositories.Conversations().CurrentConversation(readContext, reference)
		if operationErr != nil {
			return operationErr
		}
		membership, operationErr = repositories.Authority().CurrentMembership(
			readContext, reference, freshRequest.ActorRef(),
		)
		if operationErr != nil {
			return operationErr
		}
		access, operationErr = repositories.Authority().CurrentAccess(
			readContext, reference, freshRequest.ActorRef(),
		)
		if operationErr != nil {
			return operationErr
		}
		if conversation.Status() != im.ConversationActive ||
			membership.Status() != im.ConversationMembershipActive ||
			!access.HasPermission(im.ConversationPermissionRead) {
			return auth.ErrContextUnauthorized
		}
		return nil
	})
	return conversation, membership, access, readErr
}

func validateAuthorizedEventPage(page events.StreamPage, query events.StreamQuery) error {
	if len(page.Events) > int(query.Limit) || (page.HasMore && page.Next == query.After) {
		return events.ErrInvalidQuery
	}
	seen := make(map[string]struct{}, len(page.Events))
	var previousSequence uint64
	for index, event := range page.Events {
		if event.TenantID != query.TenantID || event.StreamID != query.StreamID ||
			!optionalEventWorkspaceEqual(event.WorkspaceID, query.WorkspaceID) ||
			event.Sequence == 0 || (index > 0 && event.Sequence <= previousSequence) ||
			event.GlobalPosition == 0 || event.EventID == "" {
			return events.ErrInvalidQuery
		}
		if _, exists := seen[event.EventID]; exists {
			return events.ErrInvalidQuery
		}
		seen[event.EventID] = struct{}{}
		if err := events.ValidateEventToAppend(event.EventToAppend); err != nil {
			return err
		}
		previousSequence = event.Sequence
	}
	if len(page.Events) == 0 && page.HasMore {
		return events.ErrInvalidQuery
	}
	return nil
}

func optionalEventWorkspaceEqual(left *string, right *string) bool {
	if left == nil || right == nil {
		return left == nil && right == nil
	}
	return *left == *right
}

func eventJSONValue(event events.StoredEvent) (fiber.Map, error) {
	value := fiber.Map{
		"eventId":        event.EventID,
		"streamId":       event.StreamID,
		"eventType":      event.EventType,
		"tenantId":       event.TenantID,
		"actorId":        event.ActorID,
		"schemaVersion":  event.SchemaVersion,
		"sequence":       event.Sequence,
		"globalPosition": event.GlobalPosition,
		"occurredAt":     event.OccurredAt.UTC().Format("2006-01-02T15:04:05.999999Z07:00"),
		"recordedAt":     event.RecordedAt.UTC().Format("2006-01-02T15:04:05.999999Z07:00"),
		"correlationId":  event.CorrelationID,
		"payloadDigest":  string(event.Payload.Digest()),
		"dedupeKey":      event.EventID,
	}
	if event.WorkspaceID != nil {
		value["workspaceId"] = *event.WorkspaceID
	}
	if event.CausationID != nil {
		value["causationId"] = *event.CausationID
	}
	if event.IdempotencyKey != nil {
		value["idempotencyKey"] = *event.IdempotencyKey
	}
	if event.Traceparent != nil {
		value["traceparent"] = *event.Traceparent
	}
	switch event.Payload.Kind() {
	case events.PayloadInline:
		raw := event.Payload.InlineJSON()
		if !json.Valid(raw) {
			return nil, errors.New("event payload is not valid JSON")
		}
		value["payloadKind"] = string(events.PayloadInline)
		value["payload"] = json.RawMessage(raw)
	case events.PayloadReference:
		reference := event.Payload.Reference()
		if reference == nil {
			return nil, errors.New("event payload reference is missing")
		}
		value["payloadKind"] = string(events.PayloadReference)
		value["payload"] = fiber.Map{
			"storage":     reference.Storage,
			"referenceId": reference.ReferenceID,
			"byteLength":  reference.ByteLength,
		}
	default:
		return nil, errors.New("event payload kind is unsupported")
	}
	return value, nil
}

func mapEventPageError(err error) error {
	switch {
	case errors.Is(err, events.ErrInvalidCursor), errors.Is(err, events.ErrInvalidQuery):
		return httpapi.NewAppError(httpapi.CodeValidationFailed, err)
	case errors.Is(err, events.ErrStoreUnavailable):
		return httpapi.NewAppError(httpapi.CodeDependencyUnavailable, err)
	case errors.Is(err, events.ErrInvalidEvent), errors.Is(err, events.ErrInvalidPayload):
		return httpapi.NewAppError(httpapi.CodeInternal, err)
	default:
		return httpapi.NewAppError(httpapi.CodeInternal, err)
	}
}
