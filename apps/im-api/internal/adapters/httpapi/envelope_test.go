package httpapi

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"regexp"
	"strings"
	"testing"

	"github.com/gofiber/fiber/v3"
	"github.com/gofiber/fiber/v3/middleware/recover"
)

var requestIDPattern = regexp.MustCompile(`^req_[0-9a-f]{32}$`)

func TestBusinessErrorsUseStableHTTP200Envelope(t *testing.T) {
	t.Parallel()

	testCases := []struct {
		name string
		code BusinessCode
	}{
		{name: "malformed", code: CodeMalformedRequest},
		{name: "unauthenticated", code: CodeUnauthenticated},
		{name: "forbidden", code: CodeForbidden},
		{name: "not found", code: CodeNotFound},
		{name: "revision conflict", code: CodeRevisionConflict},
		{name: "idempotency conflict", code: CodeIdempotencyConflict},
		{name: "payload too large", code: CodePayloadTooLarge},
		{name: "validation", code: CodeValidationFailed},
		{name: "rate limited", code: CodeRateLimited},
		{name: "dependency unavailable", code: CodeDependencyUnavailable},
	}

	for _, testCase := range testCases {
		t.Run(testCase.name, func(t *testing.T) {
			t.Parallel()

			server := newTestServer()
			server.Get("/failure", func(fiber.Ctx) error {
				return NewAppError(testCase.code, errors.New("provider secret canary"))
			})

			response := performRequest(t, server, "/failure")
			if response.Code != testCase.code {
				t.Fatalf("business code = %d, want %d", response.Code, testCase.code)
			}
			if strings.Contains(response.Raw, "provider secret canary") {
				t.Fatal("internal cause leaked into response")
			}
		})
	}
}

func TestUnknownErrorAndPanicAreRedacted(t *testing.T) {
	t.Parallel()

	for _, testCase := range []struct {
		name    string
		handler fiber.Handler
	}{
		{
			name: "unknown error",
			handler: func(fiber.Ctx) error {
				return errors.New("database password canary")
			},
		},
		{
			name: "panic",
			handler: func(fiber.Ctx) error {
				panic("panic token canary")
			},
		},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			t.Parallel()

			server := newTestServer()
			server.Get("/failure", testCase.handler)
			response := performRequest(t, server, "/failure")
			if response.Code != CodeInternal {
				t.Fatalf("business code = %d, want %d", response.Code, CodeInternal)
			}
			if strings.Contains(response.Raw, "canary") {
				t.Fatal("unknown internal failure leaked into response")
			}
		})
	}
}

func TestMarshalFailureReplacesWholeResponseWithInternalError(t *testing.T) {
	t.Parallel()

	server := newTestServer()
	server.Get("/unencodable", func(ctx fiber.Ctx) error {
		return WriteSuccess(ctx, make(chan struct{}))
	})

	response := performRequest(t, server, "/unencodable")
	if response.Code != CodeInternal {
		t.Fatalf("business code = %d, want %d", response.Code, CodeInternal)
	}
	if strings.Contains(response.Raw, `"code":200`) {
		t.Fatal("partial success envelope was emitted")
	}
}

func TestRequestIDIsServerGeneratedAndConsistent(t *testing.T) {
	t.Parallel()

	server := newTestServer()
	server.Get("/success", func(ctx fiber.Ctx) error {
		return WriteSuccess(ctx, fiber.Map{"accepted": true})
	})

	request := httptest.NewRequest(http.MethodGet, "/success", nil)
	request.Header.Set(requestIDHeader, "attacker-controlled")
	response, err := server.Test(request)
	if err != nil {
		t.Fatalf("perform request: %v", err)
	}
	defer response.Body.Close()

	decoded := decodeResponse(t, response)
	if !requestIDPattern.MatchString(decoded.RequestID) {
		t.Fatalf("request ID = %q, want server-generated ID", decoded.RequestID)
	}
	if decoded.RequestID != response.Header.Get(requestIDHeader) {
		t.Fatal("response header and envelope request IDs differ")
	}
}

type decodedEnvelope struct {
	Code      BusinessCode
	RequestID string
	Raw       string
}

func newTestServer() *fiber.App {
	server := fiber.New(fiber.Config{ErrorHandler: ErrorHandler})
	server.Use(RequestIDMiddleware())
	server.Use(recover.New())
	return server
}

func performRequest(t *testing.T, server *fiber.App, path string) decodedEnvelope {
	t.Helper()

	response, err := server.Test(httptest.NewRequest(http.MethodGet, path, nil))
	if err != nil {
		t.Fatalf("perform request: %v", err)
	}
	defer response.Body.Close()
	return decodeResponse(t, response)
}

func decodeResponse(t *testing.T, response *http.Response) decodedEnvelope {
	t.Helper()

	if response.StatusCode != http.StatusOK {
		t.Fatalf("HTTP status = %d, want %d", response.StatusCode, http.StatusOK)
	}
	body, err := io.ReadAll(response.Body)
	if err != nil {
		t.Fatalf("read response: %v", err)
	}

	var envelope struct {
		Code      BusinessCode `json:"code"`
		RequestID string       `json:"requestId"`
	}
	if err := json.Unmarshal(body, &envelope); err != nil {
		t.Fatalf("decode response %q: %v", string(body), err)
	}
	return decodedEnvelope{Code: envelope.Code, RequestID: envelope.RequestID, Raw: string(body)}
}
