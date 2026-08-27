package httpapi

import "github.com/gofiber/fiber/v3"

func RegisterSystemRoutes(server *fiber.App) {
	server.Get("/api/v1/system/ping", func(ctx fiber.Ctx) error {
		return WriteSuccess(ctx, fiber.Map{"status": "ok"})
	})
}
