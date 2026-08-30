package httpapi

import (
	"crypto/rand"
	"encoding/hex"

	"github.com/gofiber/fiber/v3"
)

const (
	requestIDHeader = "X-Request-ID"
	requestIDLocal  = "wanwork.request_id"
)

func RequestIDMiddleware() fiber.Handler {
	return func(ctx fiber.Ctx) error {
		requestID := newRequestID()
		ctx.Locals(requestIDLocal, requestID)
		ctx.Set(requestIDHeader, requestID)
		return ctx.Next()
	}
}

func RequestID(ctx fiber.Ctx) string {
	if requestID, ok := ctx.Locals(requestIDLocal).(string); ok && requestID != "" {
		return requestID
	}
	requestID := newRequestID()
	ctx.Locals(requestIDLocal, requestID)
	ctx.Set(requestIDHeader, requestID)
	return requestID
}

func newRequestID() string {
	random := make([]byte, 16)
	if _, err := rand.Read(random); err != nil {
		panic("cryptographic request ID generation failed")
	}
	return "req_" + hex.EncodeToString(random)
}
