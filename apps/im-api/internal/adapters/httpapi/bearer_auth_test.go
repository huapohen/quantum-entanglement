package httpapi

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gofiber/fiber/v3"
	authfake "github.com/huapohen/quantum-entanglement/apps/im-api/internal/adapters/auth/fake"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/auth"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
)

func TestBearerAuthMiddlewareInjectsVerifiedIdentityOnly(t *testing.T) {
	verifier := newHTTPAuthTestVerifier(t, false)
	server := newTestServer()
	server.Use(BearerAuthMiddleware(verifier))
	server.Get("/protected", func(ctx fiber.Ctx) error {
		identity, ok := VerifiedIdentityFromContext(ctx.Context())
		if !ok || identity.ExternalRef.SubjectID() != "user_alice" || identity.SessionID != "sess_http" {
			return errors.New("verified identity missing")
		}
		if strings.Contains(ctx.Context().Value(verifiedIdentityContextKey{}).(auth.VerifiedIdentity).SessionID, "token") {
			return errors.New("unexpected token material")
		}
		return WriteSuccess(ctx, fiber.Map{"subject": identity.ExternalRef.SubjectID()})
	})

	request := httptest.NewRequest(http.MethodGet, "/protected", nil)
	request.Header.Set("Authorization", "Bearer header.payload.signature")
	response, err := server.Test(request)
	if err != nil {
		t.Fatalf("protected request: %v", err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		t.Fatalf("HTTP status = %d, want 200", response.StatusCode)
	}
	var envelope struct {
		Code BusinessCode `json:"code"`
		Data struct {
			Subject string `json:"subject"`
		} `json:"data"`
	}
	if err := json.NewDecoder(response.Body).Decode(&envelope); err != nil {
		t.Fatal(err)
	}
	if envelope.Code != CodeOK || envelope.Data.Subject != "user_alice" {
		t.Fatalf("envelope = %#v", envelope)
	}
}

func TestBearerAuthMiddlewareRejectsAmbiguousOrQueryCredentials(t *testing.T) {
	verifier := newHTTPAuthTestVerifier(t, false)
	server := newTestServer()
	server.Use(BearerAuthMiddleware(verifier))
	server.Get("/protected", func(ctx fiber.Ctx) error {
		return WriteSuccess(ctx, nil)
	})
	tests := []struct {
		name      string
		path      string
		authorize []string
		wantCode  BusinessCode
	}{
		{name: "missing", path: "/protected", wantCode: CodeUnauthenticated},
		{name: "query access token", path: "/protected?access_token=header.payload.signature", wantCode: CodeUnauthenticated},
		{name: "query token", path: "/protected?token=header.payload.signature", wantCode: CodeUnauthenticated},
		{name: "wrong scheme", path: "/protected", authorize: []string{"Basic header.payload.signature"}, wantCode: CodeUnauthenticated},
		{name: "extra whitespace", path: "/protected", authorize: []string{"Bearer  header.payload.signature"}, wantCode: CodeUnauthenticated},
		{name: "duplicate header", path: "/protected", authorize: []string{"Bearer header.payload.signature", "Bearer header.payload.signature"}, wantCode: CodeUnauthenticated},
		{name: "unknown token", path: "/protected", authorize: []string{"Bearer unknown.payload.signature"}, wantCode: CodeUnauthenticated},
	}
	for _, testCase := range tests {
		t.Run(testCase.name, func(t *testing.T) {
			request := httptest.NewRequest(http.MethodGet, testCase.path, nil)
			for _, value := range testCase.authorize {
				request.Header.Add("Authorization", value)
			}
			response, err := server.Test(request)
			if err != nil {
				t.Fatal(err)
			}
			defer response.Body.Close()
			var envelope struct {
				Code BusinessCode `json:"code"`
			}
			if err := json.NewDecoder(response.Body).Decode(&envelope); err != nil {
				t.Fatal(err)
			}
			if response.StatusCode != http.StatusOK || envelope.Code != testCase.wantCode {
				t.Fatalf("status=%d code=%d want status=200 code=%d", response.StatusCode, envelope.Code, testCase.wantCode)
			}
		})
	}
}

func TestParseBearerHeaderRejectsNonCanonicalWhitespace(t *testing.T) {
	for _, value := range []string{
		" Bearer header.payload.signature",
		"Bearer header.payload.signature ",
		"Bearer\theader.payload.signature",
		"Bearer header.payload.signature\tmore",
	} {
		if token, ok := parseBearerHeader(value); ok || token != "" {
			t.Fatalf("parseBearerHeader(%q) = (%q, %v), want rejection", value, token, ok)
		}
	}
}

func TestBearerAuthMiddlewareMapsProviderFailureWithoutLeakingCause(t *testing.T) {
	verifier := newHTTPAuthTestVerifier(t, true)
	server := newTestServer()
	server.Use(BearerAuthMiddleware(verifier))
	server.Get("/protected", func(ctx fiber.Ctx) error { return WriteSuccess(ctx, nil) })
	request := httptest.NewRequest(http.MethodGet, "/protected", nil)
	request.Header.Set("Authorization", "Bearer header.payload.signature")
	response, err := server.Test(request)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	var envelope struct {
		Code    BusinessCode `json:"code"`
		Message string       `json:"message"`
	}
	if err := json.NewDecoder(response.Body).Decode(&envelope); err != nil {
		t.Fatal(err)
	}
	if response.StatusCode != http.StatusOK || envelope.Code != CodeDependencyUnavailable ||
		envelope.Message != "dependency unavailable" {
		t.Fatalf("envelope = %#v", envelope)
	}
}

func newHTTPAuthTestVerifier(t *testing.T, closed bool) *authfake.Verifier {
	t.Helper()
	realm, err := im.ParseProviderRealmID("rlm_http_auth")
	if err != nil {
		t.Fatal(err)
	}
	now := time.Date(2026, 8, 29, 12, 0, 0, 0, time.UTC)
	verifier, err := authfake.New(authfake.Options{
		Realm: realm, Issuer: "clerk.http-test", Audience: "wanwork-http-test",
		Now: func() time.Time { return now },
		Tokens: map[string]authfake.TokenFixture{
			"header.payload.signature": {
				ExternalSubject: "user_alice", SessionID: "sess_http",
				IssuedAt: now.Add(-time.Minute), ExpiresAt: now.Add(time.Hour),
			},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	if closed {
		verifier.Close()
	}
	return verifier
}
