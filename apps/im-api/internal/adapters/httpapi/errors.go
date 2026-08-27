package httpapi

import (
	"errors"

	"github.com/gofiber/fiber/v3"
)

// AppError carries an internal cause without exposing it on the wire.
type AppError struct {
	Code  BusinessCode
	Cause error
}

func (err *AppError) Error() string {
	return publicMessage(err.Code)
}

func (err *AppError) Unwrap() error {
	return err.Cause
}

func NewAppError(code BusinessCode, cause error) *AppError {
	return &AppError{Code: code, Cause: cause}
}

func ErrorHandler(ctx fiber.Ctx, handlerError error) error {
	code := CodeInternal
	var appError *AppError
	if errors.As(handlerError, &appError) && isKnownCode(appError.Code) {
		code = appError.Code
	}

	return writeEnvelope(ctx, Envelope{
		Code:      code,
		Data:      nil,
		Message:   publicMessage(code),
		RequestID: RequestID(ctx),
	})
}

func isKnownCode(code BusinessCode) bool {
	switch code {
	case CodeOK,
		CodeMalformedRequest,
		CodeUnauthenticated,
		CodeForbidden,
		CodeNotFound,
		CodeRevisionConflict,
		CodeIdempotencyConflict,
		CodePayloadTooLarge,
		CodeValidationFailed,
		CodeRateLimited,
		CodeInternal,
		CodeDependencyUnavailable:
		return true
	default:
		return false
	}
}

func publicMessage(code BusinessCode) string {
	switch code {
	case CodeOK:
		return "ok"
	case CodeMalformedRequest:
		return "malformed request"
	case CodeUnauthenticated:
		return "authentication required"
	case CodeForbidden:
		return "operation forbidden"
	case CodeNotFound:
		return "resource not found"
	case CodeRevisionConflict:
		return "revision conflict"
	case CodeIdempotencyConflict:
		return "idempotency conflict"
	case CodePayloadTooLarge:
		return "payload too large"
	case CodeValidationFailed:
		return "validation failed"
	case CodeRateLimited:
		return "rate limited"
	case CodeDependencyUnavailable:
		return "dependency unavailable"
	default:
		return "internal error"
	}
}
