package app

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"strings"

	"github.com/gofiber/fiber/v3"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/adapters/httpapi"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/localdemo"
)

const maxLocalDemoRequestBytes = 8 * 1024

// NewLocalDemo constructs the loopback-only, credential-free IM acceptance surface. The fake
// Clerk and RongCloud-shaped adapters make zero network calls.
func NewLocalDemo() (*fiber.App, error) {
	demo, err := localdemo.New()
	if err != nil {
		return nil, err
	}
	server := newServer(nil)
	registerLocalDemoRoutes(server, demo)
	return server, nil
}

func registerLocalDemoRoutes(server *fiber.App, demo *localdemo.Service) {
	server.Get("/api/v1/demo/im", func(ctx fiber.Ctx) error {
		return httpapi.WriteSuccess(ctx, demo.Snapshot())
	})
	server.Post("/api/v1/demo/im/mentions", func(ctx fiber.Ctx) error {
		var input localdemo.MentionInput
		if err := decodeLocalDemoRequest(ctx.Body(), &input); err != nil {
			return httpapi.NewAppError(httpapi.CodeMalformedRequest, err)
		}
		result, err := demo.Mention(ctx.Context(), bearerToken(ctx), input)
		if err != nil {
			switch {
			case errors.Is(err, localdemo.ErrUnauthenticated):
				return httpapi.NewAppError(httpapi.CodeUnauthenticated, err)
			case errors.Is(err, localdemo.ErrConflict):
				return httpapi.NewAppError(httpapi.CodeIdempotencyConflict, err)
			case errors.Is(err, localdemo.ErrInvalidInput):
				return httpapi.NewAppError(httpapi.CodeValidationFailed, err)
			default:
				return err
			}
		}
		return httpapi.WriteSuccess(ctx, result)
	})
}

func decodeLocalDemoRequest(body []byte, destination any) error {
	if len(body) == 0 || len(body) > maxLocalDemoRequestBytes {
		return localdemo.ErrInvalidInput
	}
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(destination); err != nil {
		return localdemo.ErrInvalidInput
	}
	var trailing any
	if err := decoder.Decode(&trailing); !errors.Is(err, io.EOF) {
		return localdemo.ErrInvalidInput
	}
	return nil
}

func bearerToken(ctx fiber.Ctx) string {
	value := ctx.Get(fiber.HeaderAuthorization)
	if !strings.HasPrefix(value, "Bearer ") || strings.Count(value, " ") != 1 {
		return ""
	}
	return strings.TrimPrefix(value, "Bearer ")
}
