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
	agents := localDemoRequest(t, server, http.MethodGet, "/api/v1/demo/im/agents", "", "Bearer "+localdemo.LocalBearerToken)
	if agents.Code != httpapi.CodeOK || !strings.Contains(agents.Raw, `"name":"v0版研究 Agent"`) ||
		!strings.Contains(agents.Raw, `"installationStatus":"active"`) ||
		!strings.Contains(agents.Raw, `"attestations":["data_routes_reviewed","publisher_verified","security_reviewed"]`) {
		t.Fatalf("agent store response = %s", agents.Raw)
	}
	availableAgents := localDemoRequest(t, server, http.MethodGet, "/api/v1/demo/im/agents", "", "Bearer "+localdemo.LocalBearerToken)
	if !strings.Contains(availableAgents.Raw, `"definitionId":"agd_local_planner"`) ||
		!strings.Contains(availableAgents.Raw, `"canInstall":true`) {
		t.Fatalf("available Agent Store response = %s", availableAgents.Raw)
	}
	install := localDemoRequest(t, server, http.MethodPost, "/api/v1/demo/im/agents/agd_local_planner/install",
		`{"idempotencyKey":"http/store/install/planner"}`, "Bearer "+localdemo.LocalBearerToken)
	if install.Code != httpapi.CodeOK || !strings.Contains(install.Raw, `"agentActorId":"agt_local_planner"`) ||
		!strings.Contains(install.Raw, `"installationStatus":"active"`) || strings.Contains(install.Raw, `"replayed":true`) {
		t.Fatalf("agent install response = %s", install.Raw)
	}
	installReplay := localDemoRequest(t, server, http.MethodPost, "/api/v1/demo/im/agents/agd_local_planner/install",
		`{"idempotencyKey":"http/store/install/planner"}`, "Bearer "+localdemo.LocalBearerToken)
	if installReplay.Code != httpapi.CodeOK || !strings.Contains(installReplay.Raw, `"replayed":true`) {
		t.Fatalf("agent install replay response = %s", installReplay.Raw)
	}
	unauthenticatedAgents := localDemoRequest(t, server, http.MethodGet, "/api/v1/demo/im/agents", "", "Bearer wrong.local.token")
	if unauthenticatedAgents.Code != httpapi.CodeUnauthenticated || strings.Contains(unauthenticatedAgents.Raw, "wrong.local.token") {
		t.Fatalf("unauthenticated agent store response = %s", unauthenticatedAgents.Raw)
	}
	body := `{"messageId":"msg_http_1","instruction":"调研竞品并输出证据表"}`
	result := localDemoRequest(
		t, server, http.MethodPost, "/api/v1/demo/im/mentions", body, "Bearer "+localdemo.LocalBearerToken,
	)
	if result.Code != httpapi.CodeOK || !strings.Contains(result.Raw, `"childConversationId":"cnv_at_`) ||
		!strings.Contains(result.Raw, `"providerStatus":"committed"`) ||
		!strings.Contains(result.Raw, `"taskId":"task_local_`) ||
		!strings.Contains(result.Raw, `"artifactId":"artifact_local_`) ||
		!strings.Contains(result.Raw, `"needsYouId":"needs_local_`) ||
		strings.Contains(result.Raw, localdemo.LocalBearerToken) {
		t.Fatalf("mention response = %s", result.Raw)
	}
	tasks := localDemoRequest(t, server, http.MethodGet, "/api/v1/demo/im/tasks", "", "Bearer "+localdemo.LocalBearerToken)
	if tasks.Code != httpapi.CodeOK || !strings.Contains(tasks.Raw, `"status":"waiting_for_review"`) {
		t.Fatalf("tasks response = %s", tasks.Raw)
	}
	artifacts := localDemoRequest(t, server, http.MethodGet, "/api/v1/demo/im/artifacts", "", "Bearer "+localdemo.LocalBearerToken)
	if artifacts.Code != httpapi.CodeOK || !strings.Contains(artifacts.Raw, `"status":"draft"`) {
		t.Fatalf("artifacts response = %s", artifacts.Raw)
	}
	needsYou := localDemoRequest(t, server, http.MethodGet, "/api/v1/demo/im/needs-you", "", "Bearer "+localdemo.LocalBearerToken)
	if needsYou.Code != httpapi.CodeOK || !strings.Contains(needsYou.Raw, `"status":"open"`) {
		t.Fatalf("needs-you response = %s", needsYou.Raw)
	}
	var needsPayload struct {
		Data struct {
			NeedsYou []struct {
				ID string `json:"id"`
			} `json:"needsYou"`
		} `json:"data"`
	}
	if err := json.Unmarshal([]byte(needsYou.Raw), &needsPayload); err != nil || len(needsPayload.Data.NeedsYou) != 1 {
		t.Fatalf("decode needs-you response = %v, %s", err, needsYou.Raw)
	}
	resolved := localDemoRequest(t, server, http.MethodPost, "/api/v1/demo/im/needs-you/"+needsPayload.Data.NeedsYou[0].ID+"/resolve", `{"decision":"accept"}`, "Bearer "+localdemo.LocalBearerToken)
	if resolved.Code != httpapi.CodeOK || !strings.Contains(resolved.Raw, `"status":"accepted"`) || !strings.Contains(resolved.Raw, `"status":"completed"`) {
		t.Fatalf("resolve response = %s", resolved.Raw)
	}
	resolvedReplay := localDemoRequest(t, server, http.MethodPost, "/api/v1/demo/im/needs-you/"+needsPayload.Data.NeedsYou[0].ID+"/resolve", `{"decision":"accept"}`, "Bearer "+localdemo.LocalBearerToken)
	if resolvedReplay.Code != httpapi.CodeOK || !strings.Contains(resolvedReplay.Raw, `"replayed":true`) {
		t.Fatalf("resolve replay response = %s", resolvedReplay.Raw)
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
	created := localDemoRequest(
		t, server, http.MethodPost, "/api/v1/demo/im/conversations",
		`{"type":"group","name":"没有 Agent 的群","memberActorIds":[],"idempotencyKey":"http/no-agent"}`,
		"Bearer "+localdemo.LocalBearerToken,
	)
	if created.Code != httpapi.CodeOK {
		t.Fatalf("create no-agent group response = %s", created.Raw)
	}
	var createdPayload struct {
		Data struct {
			Conversation struct {
				ID string `json:"id"`
			} `json:"conversation"`
		} `json:"data"`
	}
	if err := json.Unmarshal([]byte(created.Raw), &createdPayload); err != nil || createdPayload.Data.Conversation.ID == "" {
		t.Fatalf("decode no-agent group response = %v, %s", err, created.Raw)
	}
	forbidden := localDemoRequest(
		t, server, http.MethodPost, "/api/v1/demo/im/mentions",
		`{"conversationId":"`+createdPayload.Data.Conversation.ID+`","messageId":"msg_forbidden","instruction":"不能执行"}`,
		"Bearer "+localdemo.LocalBearerToken,
	)
	if forbidden.Code != httpapi.CodeForbidden || strings.Contains(forbidden.Raw, "不能执行") {
		t.Fatalf("forbidden mention response = %s", forbidden.Raw)
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
