package httpapi

import (
	"encoding/json"
	"net/http"

	"github.com/gofiber/fiber/v3"
)

// BusinessCode is stable application-level status. It is deliberately independent from
// transport, provider delivery, Attempt, Artifact acceptance, and Task closure states.
type BusinessCode int

const (
	CodeOK                    BusinessCode = 200
	CodeMalformedRequest      BusinessCode = 40001
	CodeUnauthenticated       BusinessCode = 40101
	CodeForbidden             BusinessCode = 40301
	CodeNotFound              BusinessCode = 40401
	CodeRevisionConflict      BusinessCode = 40901
	CodeIdempotencyConflict   BusinessCode = 40902
	CodePayloadTooLarge       BusinessCode = 41301
	CodeValidationFailed      BusinessCode = 42201
	CodeRateLimited           BusinessCode = 42901
	CodeInternal              BusinessCode = 50001
	CodeDependencyUnavailable BusinessCode = 50301
)

type Envelope struct {
	Code      BusinessCode `json:"code"`
	Data      any          `json:"data"`
	Message   string       `json:"message"`
	RequestID string       `json:"requestId"`
}

func WriteSuccess(ctx fiber.Ctx, data any) error {
	return writeEnvelope(ctx, Envelope{
		Code:      CodeOK,
		Data:      data,
		Message:   "ok",
		RequestID: RequestID(ctx),
	})
}

func writeEnvelope(ctx fiber.Ctx, envelope Envelope) error {
	payload, err := json.Marshal(envelope)
	if err != nil {
		payload = mustMarshalInternalError(RequestID(ctx))
	}

	ctx.Set(fiber.HeaderContentType, fiber.MIMEApplicationJSONCharsetUTF8)
	return ctx.Status(http.StatusOK).Send(payload)
}

func mustMarshalInternalError(requestID string) []byte {
	payload, err := json.Marshal(Envelope{
		Code:      CodeInternal,
		Data:      nil,
		Message:   publicMessage(CodeInternal),
		RequestID: requestID,
	})
	if err != nil {
		panic("static business envelope is not JSON encodable")
	}
	return payload
}
