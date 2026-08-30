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
