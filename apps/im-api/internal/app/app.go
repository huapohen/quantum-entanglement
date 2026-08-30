package app

import (
	"context"
	"errors"
	"net/http"
	"strings"
	"time"

	"github.com/gofiber/fiber/v3"
	"github.com/gofiber/fiber/v3/middleware/recover"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/adapters/httpapi"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/auth"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/events"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
	store "github.com/huapohen/quantum-entanglement/apps/im-api/internal/imstore"
)

var ErrInvalidRuntimeDependencies = errors.New("invalid IM runtime dependencies")

type ReadinessProbe interface {
	Ready(context.Context) error
}

type RuntimeDependencies struct {
	Database    ReadinessProbe
	Persistence store.TenantUnitOfWork
	Verifier    auth.Verifier
	// EventStore is optional while the PostgreSQL composition is still being wired to one
	// transaction snapshot. When absent, the authenticated event route fails closed with a
	// dependency-unavailable envelope rather than silently returning an empty page.
	EventStore events.EventStore
	// Messages is the future durable projection read port. It is optional during the contract
	// phase so an uncomposed runtime fails closed instead of presenting synthetic empty history.
	Messages store.MessageReadRepository
	// MessageShadow is an optional, default-off equality canary. When present, the message route
	// runs it before returning the first page; any mismatch is an internal failure and never a
	// best-effort fallback. The callback owns independent opaque cursors for both readers.
	MessageShadow func(context.Context, store.MessageReadPageQuery) error
	// Now is injected by tests and controlled compositions. Production defaults to UTC wall clock;
	// request context resolution never accepts a non-UTC or zero timestamp.
	Now func() time.Time
}

// New constructs the zero-network WanWork IM HTTP composition. External providers are not
// registered here; the first runnable slice is deliberately limited to local health checks.
func New() *fiber.App {
	return newServer(nil)
}

// NewRuntime constructs the database-backed composition. Business routes are registered behind
// an action-time database readiness barrier; liveness remains independent so an orchestrator can
// distinguish a running process from one that is safe to receive work.
func NewRuntime(dependencies RuntimeDependencies) (*fiber.App, error) {
	if dependencies.Database == nil || dependencies.Persistence == nil || dependencies.Verifier == nil {
		return nil, ErrInvalidRuntimeDependencies
	}
	if dependencies.Now == nil {
		dependencies.Now = func() time.Time { return time.Now().UTC() }
	}
	return newServer(&dependencies), nil
}

func newServer(runtime *RuntimeDependencies) *fiber.App {
	server := fiber.New(fiber.Config{
		AppName:      "WanWork IM API",
		ErrorHandler: httpapi.ErrorHandler,
		Immutable:    true,
	})
	server.Use(httpapi.RequestIDMiddleware())
	server.Use(recover.New())

	server.Get("/health/live", func(ctx fiber.Ctx) error {
		return ctx.JSON(fiber.Map{"status": "ok"})
	})
	if runtime != nil {
		server.Get("/health/ready", func(ctx fiber.Ctx) error {
			if runtime.Database.Ready(ctx.Context()) != nil {
				return ctx.Status(http.StatusServiceUnavailable).JSON(fiber.Map{
					"status": "unavailable",
				})
			}
			return ctx.JSON(fiber.Map{"status": "ok"})
		})
		server.Use("/api/v1", func(ctx fiber.Ctx) error {
			if runtime.Database.Ready(ctx.Context()) != nil {
				return httpapi.NewAppError(httpapi.CodeDependencyUnavailable, errors.New(
					"runtime database readiness gate is closed",
				))
			}
			return ctx.Next()
		})
		server.Use("/api/v1", httpapi.BearerAuthMiddleware(runtime.Verifier))
		server.Use("/api/v1", trustedRequestContextMiddleware(*runtime))
	}
	httpapi.RegisterSystemRoutes(server)
	if runtime != nil {
		registerAuthenticatedContextRoute(server)
		registerAuthenticatedConversationRoute(server, *runtime)
		registerAuthenticatedEventRoute(server, *runtime)
		registerAuthenticatedMessageRoute(server, *runtime)
	}

	return server
}

const tenantIDHeader = "X-WanWork-Tenant-ID"

func trustedRequestContextMiddleware(runtime RuntimeDependencies) fiber.Handler {
	return func(ctx fiber.Ctx) error {
		identity, ok := httpapi.VerifiedIdentityFromContext(ctx.Context())
		if !ok {
			return httpapi.NewAppError(httpapi.CodeUnauthenticated, auth.ErrInvalidToken)
		}
		tenantID, err := tenantIDFromHeader(ctx)
		if err != nil {
			return httpapi.NewAppError(httpapi.CodeMalformedRequest, err)
		}
		var resolved auth.TrustedRequestContext
		readErr := runtime.Persistence.Read(ctx.Context(), tenantID, func(
			readContext context.Context,
			repositories store.TenantRepositories,
		) error {
			if repositories == nil || repositories.Identity() == nil {
				return auth.ErrContextUnavailable
			}
			var resolveErr error
			resolved, resolveErr = auth.ResolveTrustedRequestContext(
				readContext,
				runtime.Verifier.Profile(),
				identity,
				tenantID,
				repositories.Identity(),
				runtime.Now(),
			)
			return resolveErr
		})
		if readErr != nil {
			return mapTrustedContextError(readErr)
		}
		if resolved.IsZero() {
			return httpapi.NewAppError(httpapi.CodeInternal, auth.ErrContextIntegrity)
		}
		ctx.SetContext(auth.WithTrustedRequestContext(ctx.Context(), resolved))
		return ctx.Next()
	}
}

func registerAuthenticatedContextRoute(server *fiber.App) {
	server.Get("/api/v1/auth/context", func(ctx fiber.Ctx) error {
		request, ok := auth.TrustedRequestContextFromContext(ctx.Context())
		if !ok {
			return httpapi.NewAppError(httpapi.CodeUnauthenticated, auth.ErrInvalidContext)
		}
		return httpapi.WriteSuccess(ctx, fiber.Map{
			"provider":        string(request.Identity().ExternalRef.Provider()),
			"externalSubject": request.Identity().ExternalRef.SubjectID(),
			"principalId":     request.PrincipalID().String(),
			"tenantId":        request.TenantID().String(),
			"actorId":         request.ActorRef().ActorID().String(),
			"membershipRole":  string(request.Membership().Role()),
			"revisions": fiber.Map{
				"principal":  request.Principal().Revision(),
				"membership": request.Membership().Revision(),
				"actor":      request.Actor().Revision(),
			},
		})
	})
}

func registerAuthenticatedConversationRoute(server *fiber.App, runtime RuntimeDependencies) {
	server.Get("/api/v1/tenants/:tenantId/conversations/:conversationId", func(ctx fiber.Ctx) error {
		request, ok := auth.TrustedRequestContextFromContext(ctx.Context())
		if !ok {
			return httpapi.NewAppError(httpapi.CodeUnauthenticated, auth.ErrInvalidContext)
		}
		pathTenant, err := im.ParseTenantID(ctx.Params("tenantId"))
		if err != nil {
			return httpapi.NewAppError(httpapi.CodeMalformedRequest, err)
		}
		if pathTenant != request.TenantID() {
			return httpapi.NewAppError(httpapi.CodeForbidden, auth.ErrContextUnauthorized)
		}
		conversationID, err := im.ParseConversationID(ctx.Params("conversationId"))
		if err != nil {
			return httpapi.NewAppError(httpapi.CodeMalformedRequest, err)
		}
		reference, err := im.NewConversationRef(pathTenant, conversationID)
		if err != nil {
			return httpapi.NewAppError(httpapi.CodeMalformedRequest, err)
		}

		var conversation im.ConversationSnapshot
		var membership im.ConversationMembershipSnapshot
		var access im.ConversationAccessSnapshot
		readErr := runtime.Persistence.Read(ctx.Context(), pathTenant, func(
			readContext context.Context,
			repositories store.TenantRepositories,
		) error {
			if repositories == nil || repositories.Identity() == nil ||
				repositories.Conversations() == nil || repositories.Authority() == nil {
				return auth.ErrContextUnavailable
			}
			// Resolve again inside the business read snapshot. The middleware snapshot is a routing
			// prerequisite; action-time reads must not rely on a potentially stale Actor revision.
			freshRequest, resolveErr := auth.ResolveTrustedRequestContext(
				readContext,
				runtime.Verifier.Profile(), request.Identity(), pathTenant,
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
		if readErr != nil {
			return mapTenantReadError(readErr)
		}
		workspace, hasWorkspace := conversation.WorkspaceID()
		data := fiber.Map{
			"id":       conversation.Ref().ConversationID().String(),
			"tenantId": conversation.Ref().TenantID().String(),
			"type":     string(conversation.ConversationType()),
			"status":   string(conversation.Status()),
			"revision": conversation.Revision(),
			"membership": fiber.Map{
				"role":     string(membership.Role()),
				"revision": membership.Revision(),
			},
			"access": fiber.Map{
				"permissions": conversationPermissions(access),
				"revision":    access.Revision(),
			},
		}
		if hasWorkspace {
			data["workspaceId"] = workspace.String()
		}
		if parent := conversation.ParentConversationID(); !parent.IsZero() {
			data["parentConversationId"] = parent.String()
		}
		return httpapi.WriteSuccess(ctx, data)
	})
}

func conversationPermissions(snapshot im.ConversationAccessSnapshot) []string {
	permissions := snapshot.Permissions()
	values := make([]string, 0, len(permissions))
	for _, permission := range permissions {
		values = append(values, string(permission))
	}
	return values
}

func mapTenantReadError(err error) error {
	switch {
	case errors.Is(err, auth.ErrInvalidToken), errors.Is(err, auth.ErrTokenExpired):
		return httpapi.NewAppError(httpapi.CodeUnauthenticated, err)
	case errors.Is(err, auth.ErrContextUnauthorized), errors.Is(err, auth.ErrContextAuthorityMissing):
		return httpapi.NewAppError(httpapi.CodeForbidden, err)
	case errors.Is(err, store.ErrNotFound):
		return httpapi.NewAppError(httpapi.CodeNotFound, err)
	case errors.Is(err, store.ErrInvalidRequest):
		return httpapi.NewAppError(httpapi.CodeMalformedRequest, err)
	case errors.Is(err, auth.ErrContextUnavailable), errors.Is(err, store.ErrStoreUnavailable):
		return httpapi.NewAppError(httpapi.CodeDependencyUnavailable, err)
	case errors.Is(err, auth.ErrContextIntegrity), errors.Is(err, store.ErrIntegrity):
		return httpapi.NewAppError(httpapi.CodeInternal, err)
	default:
		return httpapi.NewAppError(httpapi.CodeInternal, err)
	}
}

func tenantIDFromHeader(ctx fiber.Ctx) (im.TenantID, error) {
	values := make([]string, 0, 1)
	for key, candidates := range ctx.GetReqHeaders() {
		if strings.EqualFold(key, tenantIDHeader) {
			values = append(values, candidates...)
		}
	}
	if len(values) != 1 || values[0] == "" || strings.TrimSpace(values[0]) != values[0] {
		return im.TenantID{}, auth.ErrInvalidContext
	}
	tenantID, err := im.ParseTenantID(values[0])
	if err != nil {
		return im.TenantID{}, auth.ErrInvalidContext
	}
	return tenantID, nil
}

func mapTrustedContextError(err error) error {
	switch {
	case errors.Is(err, auth.ErrInvalidToken), errors.Is(err, auth.ErrTokenExpired):
		return httpapi.NewAppError(httpapi.CodeUnauthenticated, err)
	case errors.Is(err, auth.ErrContextUnauthorized), errors.Is(err, auth.ErrContextAuthorityMissing):
		return httpapi.NewAppError(httpapi.CodeForbidden, err)
	case errors.Is(err, auth.ErrContextUnavailable), errors.Is(err, store.ErrStoreUnavailable):
		return httpapi.NewAppError(httpapi.CodeDependencyUnavailable, err)
	case errors.Is(err, auth.ErrContextIntegrity):
		return httpapi.NewAppError(httpapi.CodeInternal, err)
	default:
		return httpapi.NewAppError(httpapi.CodeInternal, err)
	}
}
