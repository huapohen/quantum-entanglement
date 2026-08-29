package app

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gofiber/fiber/v3"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/adapters/httpapi"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/localdemo"
)

func TestLocalDemoHTTPVerticalSlice(t *testing.T) {
	t.Parallel()
	server, err := NewLocalDemo()
	if err != nil {
		t.Fatal(err)
	}
	page, err := server.Test(httptest.NewRequest(http.MethodGet, "/demo/im", nil))
	if err != nil {
		t.Fatal(err)
	}
	pageBody, err := io.ReadAll(page.Body)
	page.Body.Close()
	if err != nil || page.StatusCode != http.StatusOK ||
		!strings.Contains(string(pageBody), "人和 Agent 共生协同办公") ||
		page.Header.Get("Content-Security-Policy") == "" {
		t.Fatalf("demo HTML response status=%d error=%v", page.StatusCode, err)
	}
	snapshot := localDemoRequest(t, server, http.MethodGet, "/api/v1/demo/im", "", "")
	if snapshot.Code != httpapi.CodeOK || !strings.Contains(snapshot.Raw, `"networkCalls":0`) ||
		!strings.Contains(snapshot.Raw, `"mode":"zero-network-fake"`) {
		t.Fatalf("snapshot response = %s", snapshot.Raw)
	}
	body := `{"messageId":"msg_http_1","instruction":"调研竞品并输出证据表"}`
	result := localDemoRequest(
		t, server, http.MethodPost, "/api/v1/demo/im/mentions", body, "Bearer "+localdemo.LocalBearerToken,
	)
	if result.Code != httpapi.CodeOK || !strings.Contains(result.Raw, `"childConversationId":"cnv_at_`) ||
		!strings.Contains(result.Raw, `"providerStatus":"committed"`) ||
		strings.Contains(result.Raw, localdemo.LocalBearerToken) {
		t.Fatalf("mention response = %s", result.Raw)
	}
	replay := localDemoRequest(
		t, server, http.MethodPost, "/api/v1/demo/im/mentions", body, "Bearer "+localdemo.LocalBearerToken,
	)
	if replay.Code != httpapi.CodeOK || !strings.Contains(replay.Raw, `"replayed":true`) {
		t.Fatalf("replay response = %s", replay.Raw)
	}
}

func TestLocalDemoBusinessFailuresRemainHTTP200AndRedacted(t *testing.T) {
	t.Parallel()
	server, err := NewLocalDemo()
	if err != nil {
		t.Fatal(err)
	}
	body := `{"messageId":"msg_http_1","instruction":"first"}`
	unauthenticated := localDemoRequest(
		t, server, http.MethodPost, "/api/v1/demo/im/mentions", body, "Bearer wrong.local.token",
	)
	if unauthenticated.Code != httpapi.CodeUnauthenticated || strings.Contains(unauthenticated.Raw, "wrong.local.token") {
		t.Fatalf("unauthenticated response = %s", unauthenticated.Raw)
	}
	malformed := localDemoRequest(
		t, server, http.MethodPost, "/api/v1/demo/im/mentions",
		`{"messageId":"msg_http_1","instruction":"first","secret":"canary"}`,
		"Bearer "+localdemo.LocalBearerToken,
	)
	if malformed.Code != httpapi.CodeMalformedRequest || strings.Contains(malformed.Raw, "canary") {
		t.Fatalf("malformed response = %s", malformed.Raw)
	}
	first := localDemoRequest(
		t, server, http.MethodPost, "/api/v1/demo/im/mentions", body,
		"Bearer "+localdemo.LocalBearerToken,
	)
	if first.Code != httpapi.CodeOK {
		t.Fatalf("first response = %s", first.Raw)
	}
	conflict := localDemoRequest(
		t, server, http.MethodPost, "/api/v1/demo/im/mentions",
		`{"messageId":"msg_http_1","instruction":"changed"}`,
		"Bearer "+localdemo.LocalBearerToken,
	)
	if conflict.Code != httpapi.CodeIdempotencyConflict || strings.Contains(conflict.Raw, "changed") {
		t.Fatalf("conflict response = %s", conflict.Raw)
	}
}

type localDemoEnvelope struct {
	Code httpapi.BusinessCode
	Raw  string
}

func localDemoRequest(
	t *testing.T,
	server *fiber.App,
	method string,
	path string,
	body string,
	authorization string,
) localDemoEnvelope {
	t.Helper()
	request := httptest.NewRequest(method, path, bytes.NewBufferString(body))
	if body != "" {
		request.Header.Set(http.CanonicalHeaderKey("Content-Type"), "application/json")
	}
	if authorization != "" {
		request.Header.Set(http.CanonicalHeaderKey("Authorization"), authorization)
	}
	response, err := server.Test(request)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		t.Fatalf("HTTP status = %d", response.StatusCode)
	}
	payload, err := io.ReadAll(response.Body)
	if err != nil {
		t.Fatal(err)
	}
	var envelope struct {
		Code httpapi.BusinessCode `json:"code"`
	}
	if err := json.Unmarshal(payload, &envelope); err != nil {
		t.Fatalf("decode %s: %v", payload, err)
	}
	return localDemoEnvelope{Code: envelope.Code, Raw: string(payload)}
}
