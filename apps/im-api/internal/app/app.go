package app

import "github.com/gofiber/fiber/v3"

// New constructs the zero-network WanWork IM HTTP composition. External providers are not
// registered here; the first runnable slice is deliberately limited to local health checks.
func New() *fiber.App {
	server := fiber.New(fiber.Config{
		AppName:   "WanWork IM API",
		Immutable: true,
	})

	server.Get("/health/live", func(ctx fiber.Ctx) error {
		return ctx.JSON(fiber.Map{"status": "ok"})
	})

	return server
}
