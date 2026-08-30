package app

import (
	"bytes"
	_ "embed"
	"encoding/json"
	"errors"
	"io"
	"os"
	"strconv"
	"strings"

	"github.com/gofiber/fiber/v3"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/adapters/httpapi"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/localdemo"
)

const maxLocalDemoRequestBytes = 8 * 1024

//go:embed local_demo.html
var localDemoHTML []byte

// NewLocalDemo constructs the loopback-only IM acceptance surface. The default synthetic runtime
// and fake Clerk/RongCloud-shaped adapters make zero network calls; a model runtime is enabled only
// by an explicit, fully configured environment mode.
func NewLocalDemo() (*fiber.App, error) {
	demo, err := localdemo.NewFromEnv(os.LookupEnv)
	if err != nil {
		return nil, err
	}
	server := newServer(nil)
	registerLocalDemoRoutes(server, demo)
	return server, nil
}

func registerLocalDemoRoutes(server *fiber.App, demo *localdemo.Service) {
	server.Get("/demo/im", func(ctx fiber.Ctx) error {
		ctx.Set(fiber.HeaderContentType, fiber.MIMETextHTMLCharsetUTF8)
		ctx.Set("Content-Security-Policy", "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'")
		return ctx.Send(localDemoHTML)
	})
	server.Get("/api/v1/demo/im", func(ctx fiber.Ctx) error {
		return httpapi.WriteSuccess(ctx, demo.Snapshot())
	})
	server.Get("/api/v1/demo/im/agents", func(ctx fiber.Ctx) error {
		page, err := demo.ListAgents(ctx.Context(), bearerToken(ctx))
		if err != nil {
			return localDemoAppError(err)
		}
		return httpapi.WriteSuccess(ctx, page)
	})
	server.Post("/api/v1/demo/im/agents/:definitionId/install", func(ctx fiber.Ctx) error {
		var input localdemo.AgentStoreInstallInput
		if err := decodeLocalDemoRequest(ctx.Body(), &input); err != nil {
			return httpapi.NewAppError(httpapi.CodeMalformedRequest, err)
		}
		result, err := demo.InstallAgent(
			ctx.Context(), bearerToken(ctx), ctx.Params("definitionId"), input,
		)
		if err != nil {
			return localDemoAppError(err)
		}
		return httpapi.WriteSuccess(ctx, result)
	})
	server.Get("/api/v1/demo/im/tasks", func(ctx fiber.Ctx) error {
		page, err := demo.ListTasks(ctx.Context(), bearerToken(ctx))
		if err != nil {
			return localDemoAppError(err)
		}
		return httpapi.WriteSuccess(ctx, page)
	})
	server.Get("/api/v1/demo/im/artifacts", func(ctx fiber.Ctx) error {
		page, err := demo.ListArtifacts(ctx.Context(), bearerToken(ctx))
		if err != nil {
			return localDemoAppError(err)
		}
		return httpapi.WriteSuccess(ctx, page)
	})
	server.Get("/api/v1/demo/im/needs-you", func(ctx fiber.Ctx) error {
		page, err := demo.ListNeedsYou(ctx.Context(), bearerToken(ctx))
		if err != nil {
			return localDemoAppError(err)
		}
		return httpapi.WriteSuccess(ctx, page)
	})
	server.Get("/api/v1/demo/im/conversations", func(ctx fiber.Ctx) error {
		limit, err := localDemoQueryLimit(ctx.Query("limit"))
		if err != nil {
			return httpapi.NewAppError(httpapi.CodeValidationFailed, err)
		}
		page, err := demo.ListConversations(ctx.Context(), bearerToken(ctx), ctx.Query("after"), limit)
		if err != nil {
			return localDemoAppError(err)
		}
		return httpapi.WriteSuccess(ctx, page)
	})
	server.Post("/api/v1/demo/im/conversations", func(ctx fiber.Ctx) error {
		var input localdemo.CreateConversationInput
		if err := decodeLocalDemoRequest(ctx.Body(), &input); err != nil {
			return httpapi.NewAppError(httpapi.CodeMalformedRequest, err)
		}
		result, err := demo.CreateConversation(ctx.Context(), bearerToken(ctx), input)
		if err != nil {
			return localDemoAppError(err)
		}
		return httpapi.WriteSuccess(ctx, result)
	})
	server.Post("/api/v1/demo/im/conversations/:conversationId/members", func(ctx fiber.Ctx) error {
		var input localdemo.AddMembersInput
		if err := decodeLocalDemoRequest(ctx.Body(), &input); err != nil {
			return httpapi.NewAppError(httpapi.CodeMalformedRequest, err)
		}
		result, err := demo.AddMembers(
			ctx.Context(), bearerToken(ctx), ctx.Params("conversationId"), input,
		)
		if err != nil {
			return localDemoAppError(err)
		}
		return httpapi.WriteSuccess(ctx, result)
	})
	server.Get("/api/v1/demo/im/conversations/:conversationId/messages", func(ctx fiber.Ctx) error {
		limit, err := localDemoQueryLimit(ctx.Query("limit"))
		if err != nil {
			return httpapi.NewAppError(httpapi.CodeValidationFailed, err)
		}
		page, err := demo.ListMessages(
			ctx.Context(), bearerToken(ctx), ctx.Params("conversationId"), ctx.Query("after"), limit,
		)
		if err != nil {
			return localDemoAppError(err)
		}
		return httpapi.WriteSuccess(ctx, page)
	})
	server.Get("/api/v1/demo/im/conversations/:conversationId/messages/search", func(ctx fiber.Ctx) error {
		page, err := demo.SearchMessages(ctx.Context(), bearerToken(ctx), ctx.Params("conversationId"), ctx.Query("q"))
		if err != nil {
			return localDemoAppError(err)
		}
		return httpapi.WriteSuccess(ctx, page)
	})
	server.Post("/api/v1/demo/im/conversations/:conversationId/messages", func(ctx fiber.Ctx) error {
		var input localdemo.SendTextInput
		if err := decodeLocalDemoRequest(ctx.Body(), &input); err != nil {
			return httpapi.NewAppError(httpapi.CodeMalformedRequest, err)
		}
		result, err := demo.SendText(
			ctx.Context(), bearerToken(ctx), ctx.Params("conversationId"), input,
		)
		if err != nil {
			return localDemoAppError(err)
		}
		return httpapi.WriteSuccess(ctx, result)
	})
	server.Patch("/api/v1/demo/im/conversations/:conversationId/messages/:messageId", func(ctx fiber.Ctx) error {
		var input localdemo.EditTextInput
		if err := decodeLocalDemoRequest(ctx.Body(), &input); err != nil {
			return httpapi.NewAppError(httpapi.CodeMalformedRequest, err)
		}
		result, err := demo.EditText(
			ctx.Context(), bearerToken(ctx), ctx.Params("conversationId"), ctx.Params("messageId"), input,
		)
		if err != nil {
			return localDemoAppError(err)
		}
		return httpapi.WriteSuccess(ctx, result)
	})
	server.Post("/api/v1/demo/im/conversations/:conversationId/messages/:messageId/recall", func(ctx fiber.Ctx) error {
		var input localdemo.RecallMessageInput
		if err := decodeLocalDemoRequest(ctx.Body(), &input); err != nil {
			return httpapi.NewAppError(httpapi.CodeMalformedRequest, err)
		}
		result, err := demo.RecallMessage(
			ctx.Context(), bearerToken(ctx), ctx.Params("conversationId"), ctx.Params("messageId"), input,
		)
		if err != nil {
			return localDemoAppError(err)
		}
		return httpapi.WriteSuccess(ctx, result)
	})
	server.Post("/api/v1/demo/im/mentions", func(ctx fiber.Ctx) error {
		var input localdemo.MentionInput
		if err := decodeLocalDemoRequest(ctx.Body(), &input); err != nil {
			return httpapi.NewAppError(httpapi.CodeMalformedRequest, err)
		}
		result, err := demo.Mention(ctx.Context(), bearerToken(ctx), input)
		if err != nil {
			return localDemoAppError(err)
		}
		return httpapi.WriteSuccess(ctx, result)
	})
	server.Post("/api/v1/demo/im/needs-you/:needsYouId/resolve", func(ctx fiber.Ctx) error {
		var input localdemo.ResolveNeedsYouInput
		if err := decodeLocalDemoRequest(ctx.Body(), &input); err != nil {
			return httpapi.NewAppError(httpapi.CodeMalformedRequest, err)
		}
		result, err := demo.ResolveNeedsYou(ctx.Context(), bearerToken(ctx), ctx.Params("needsYouId"), input)
		if err != nil {
			return localDemoAppError(err)
		}
		return httpapi.WriteSuccess(ctx, result)
	})
}

func localDemoQueryLimit(raw string) (int, error) {
	if raw == "" {
		return 0, nil
	}
	value, err := strconv.Atoi(raw)
	if err != nil {
		return 0, localdemo.ErrInvalidInput
	}
	return value, nil
}

func localDemoAppError(err error) error {
	switch {
	case errors.Is(err, localdemo.ErrUnauthenticated):
		return httpapi.NewAppError(httpapi.CodeUnauthenticated, err)
	case errors.Is(err, localdemo.ErrForbidden):
		return httpapi.NewAppError(httpapi.CodeForbidden, err)
	case errors.Is(err, localdemo.ErrNotFound):
		return httpapi.NewAppError(httpapi.CodeNotFound, err)
	case errors.Is(err, localdemo.ErrConflict):
		return httpapi.NewAppError(httpapi.CodeIdempotencyConflict, err)
	case errors.Is(err, localdemo.ErrInvalidCursor), errors.Is(err, localdemo.ErrInvalidInput):
		return httpapi.NewAppError(httpapi.CodeValidationFailed, err)
	case errors.Is(err, localdemo.ErrProvider):
		return httpapi.NewAppError(httpapi.CodeDependencyUnavailable, err)
	case errors.Is(err, localdemo.ErrRuntime):
		return httpapi.NewAppError(httpapi.CodeDependencyUnavailable, err)
	case errors.Is(err, localdemo.ErrIntegrity):
		return httpapi.NewAppError(httpapi.CodeInternal, err)
	default:
		return err
	}
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
