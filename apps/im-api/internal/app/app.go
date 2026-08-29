package app

import (
	"context"
	"errors"
	"net/http"

	"github.com/gofiber/fiber/v3"
	"github.com/gofiber/fiber/v3/middleware/recover"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/adapters/httpapi"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/auth"
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
	}
	httpapi.RegisterSystemRoutes(server)

	return server
}
