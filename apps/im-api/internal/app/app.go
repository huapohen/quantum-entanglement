package app

import (
	"github.com/gofiber/fiber/v3"
	"github.com/gofiber/fiber/v3/middleware/recover"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/adapters/httpapi"
)

// New constructs the zero-network WanWork IM HTTP composition. External providers are not
// registered here; the first runnable slice is deliberately limited to local health checks.
func New() *fiber.App {
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
	httpapi.RegisterSystemRoutes(server)

	return server
}
