package httpapi

import (
	"context"
	"errors"
	"strings"

	"github.com/gofiber/fiber/v3"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/auth"
)

// BearerAuthMiddleware authenticates an API request without making the token itself available to
// handlers. Only one RFC 6750 Authorization header is accepted; query-string token transport and
// ambiguous whitespace are rejected to prevent proxy/cache and duplicate-header confusion.
func BearerAuthMiddleware(verifier auth.Verifier) fiber.Handler {
	return func(ctx fiber.Ctx) error {
		if verifier == nil {
			return NewAppError(CodeDependencyUnavailable, auth.ErrProviderUnavailable)
		}
		if hasQueryToken(ctx) {
			return NewAppError(CodeUnauthenticated, auth.ErrInvalidRequest)
		}
		header, ok := singleAuthorizationHeader(ctx)
		if !ok {
			return NewAppError(CodeUnauthenticated, auth.ErrInvalidRequest)
		}
		token, ok := parseBearerHeader(header)
		if !ok {
			return NewAppError(CodeUnauthenticated, auth.ErrInvalidRequest)
		}
		identity, err := verifier.Verify(ctx.Context(), auth.VerifyRequest{BearerToken: token})
		if err != nil {
			return mapVerifierError(err)
		}
		requestContext := context.WithValue(ctx.Context(), verifiedIdentityContextKey{}, identity)
		ctx.SetContext(requestContext)
		return ctx.Next()
	}
}

// VerifiedIdentityFromContext returns the identity installed by BearerAuthMiddleware. The
// context key is private to this package, so a client cannot forge it by supplying a header or
// query parameter. It is a value snapshot and contains no bearer token.
func VerifiedIdentityFromContext(ctx context.Context) (auth.VerifiedIdentity, bool) {
	if ctx == nil {
		return auth.VerifiedIdentity{}, false
	}
	identity, ok := ctx.Value(verifiedIdentityContextKey{}).(auth.VerifiedIdentity)
	return identity, ok && !identity.ExternalRef.IsZero()
}

type verifiedIdentityContextKey struct{}

func singleAuthorizationHeader(ctx fiber.Ctx) (string, bool) {
	values := make([]string, 0, 1)
	for key, candidates := range ctx.GetReqHeaders() {
		if strings.EqualFold(key, fiber.HeaderAuthorization) {
			values = append(values, candidates...)
		}
	}
	if len(values) != 1 || values[0] == "" {
		return "", false
	}
	return values[0], true
}

func parseBearerHeader(value string) (string, bool) {
	if !strings.HasPrefix(value, "Bearer ") || strings.Count(value, " ") != 1 {
		return "", false
	}
	token := strings.TrimPrefix(value, "Bearer ")
	if token == "" || strings.TrimSpace(token) != token || strings.ContainsAny(token, "\r\n\t") {
		return "", false
	}
	return token, true
}

func hasQueryToken(ctx fiber.Ctx) bool {
	query := ctx.Request().URI().QueryArgs()
	for _, key := range []string{"access_token", "token", "bearer_token", "authorization"} {
		if query.Has(key) {
			return true
		}
	}
	return false
}

func mapVerifierError(err error) error {
	switch {
	case errors.Is(err, auth.ErrInvalidRequest), errors.Is(err, auth.ErrInvalidToken),
		errors.Is(err, auth.ErrTokenExpired):
		return NewAppError(CodeUnauthenticated, err)
	case errors.Is(err, auth.ErrProviderUnavailable), errors.Is(err, auth.ErrProviderClosed):
		return NewAppError(CodeDependencyUnavailable, err)
	default:
		return NewAppError(CodeInternal, err)
	}
}
