package app

import (
	"encoding/json"
	"net/http"
	"strings"
	"testing"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/adapters/httpapi"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/localdemo"
)

func TestLocalDemoBasicConversationAndMessageHTTPAPI(t *testing.T) {
	t.Parallel()
	server, err := NewLocalDemo()
	if err != nil {
		t.Fatal(err)
	}
	create := localDemoRequest(t, server, http.MethodPost, "/api/v1/demo/im/conversations", `{
		"type":"group",
		"name":"HTTP 验收群",
		"memberActorIds":["agt_local_research"],
		"idempotencyKey":"http/create/group/1"
	}`, "Bearer "+localdemo.LocalBearerToken)
	if create.Code != httpapi.CodeOK || !strings.Contains(create.Raw, `"providerStatus":"committed"`) ||
		!strings.Contains(create.Raw, `"replayed":false`) {
		t.Fatalf("create response = %s", create.Raw)
	}
	conversationID := decodeConversationID(t, create.Raw)

	sent := localDemoRequest(t, server, http.MethodPost,
		"/api/v1/demo/im/conversations/"+conversationID+"/messages",
		`{"clientMessageId":"msg_http_client_1","text":"hello from HTTP","extInfo":"{\"messageType\":\"text\"}"}`,
		"Bearer "+localdemo.LocalBearerToken)
	if sent.Code != httpapi.CodeOK || !strings.Contains(sent.Raw, `"conversationId":"`+conversationID+`"`) ||
		!strings.Contains(sent.Raw, `"providerStatus":"committed"`) {
		t.Fatalf("send response = %s", sent.Raw)
	}

	conversations := localDemoRequest(t, server, http.MethodGet,
		"/api/v1/demo/im/conversations?limit=20", "", "Bearer "+localdemo.LocalBearerToken)
	if conversations.Code != httpapi.CodeOK || !strings.Contains(conversations.Raw, `"id":"`+conversationID+`"`) ||
		!strings.Contains(conversations.Raw, `"hasMore":false`) {
		t.Fatalf("conversation page = %s", conversations.Raw)
	}

	messages := localDemoRequest(t, server, http.MethodGet,
		"/api/v1/demo/im/conversations/"+conversationID+"/messages?limit=1", "", "Bearer "+localdemo.LocalBearerToken)
	if messages.Code != httpapi.CodeOK || !strings.Contains(messages.Raw, `"text":"hello from HTTP"`) ||
		!strings.Contains(messages.Raw, `"hasMore":false`) {
		t.Fatalf("message page = %s", messages.Raw)
	}
	unknown := localDemoRequest(t, server, http.MethodGet,
		"/api/v1/demo/im/conversations/cnv_missing/messages", "", "Bearer "+localdemo.LocalBearerToken)
	if unknown.Code != httpapi.CodeNotFound {
		t.Fatalf("unknown conversation code = %d, body = %s", unknown.Code, unknown.Raw)
	}
	badLimit := localDemoRequest(t, server, http.MethodGet,
		"/api/v1/demo/im/conversations?limit=101", "", "Bearer "+localdemo.LocalBearerToken)
	if badLimit.Code != httpapi.CodeValidationFailed {
		t.Fatalf("bad limit code = %d, body = %s", badLimit.Code, badLimit.Raw)
	}
}

func decodeConversationID(t *testing.T, raw string) string {
	t.Helper()
	var envelope struct {
		Data struct {
			Conversation struct {
				ID string `json:"id"`
			} `json:"conversation"`
		} `json:"data"`
	}
	if err := json.Unmarshal([]byte(raw), &envelope); err != nil || envelope.Data.Conversation.ID == "" {
		t.Fatalf("decode conversation = %q (%v)", envelope.Data.Conversation.ID, err)
	}
	return envelope.Data.Conversation.ID
}
