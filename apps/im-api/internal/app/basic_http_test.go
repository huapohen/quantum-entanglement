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
	messageID := decodeMessageID(t, sent.Raw)
	search := localDemoRequest(t, server, http.MethodGet,
		"/api/v1/demo/im/conversations/"+conversationID+"/messages/search?q=hello", "", "Bearer "+localdemo.LocalBearerToken)
	if search.Code != httpapi.CodeOK || !strings.Contains(search.Raw, `"text":"hello from HTTP"`) {
		t.Fatalf("message search response = %s", search.Raw)
	}
	edited := localDemoRequest(t, server, http.MethodPatch,
		"/api/v1/demo/im/conversations/"+conversationID+"/messages/"+messageID,
		`{"text":"edited from HTTP"}`, "Bearer "+localdemo.LocalBearerToken)
	if edited.Code != httpapi.CodeOK || !strings.Contains(edited.Raw, `"status":"edited"`) ||
		!strings.Contains(edited.Raw, `"text":"edited from HTTP"`) ||
		!strings.Contains(edited.Raw, `"replayed":false`) {
		t.Fatalf("edit response = %s", edited.Raw)
	}
	editedReplay := localDemoRequest(t, server, http.MethodPatch,
		"/api/v1/demo/im/conversations/"+conversationID+"/messages/"+messageID,
		`{"text":"edited from HTTP"}`, "Bearer "+localdemo.LocalBearerToken)
	if editedReplay.Code != httpapi.CodeOK || !strings.Contains(editedReplay.Raw, `"replayed":true`) {
		t.Fatalf("edit replay response = %s", editedReplay.Raw)
	}
	recalled := localDemoRequest(t, server, http.MethodPost,
		"/api/v1/demo/im/conversations/"+conversationID+"/messages/"+messageID+"/recall",
		`{}`, "Bearer "+localdemo.LocalBearerToken)
	if recalled.Code != httpapi.CodeOK || !strings.Contains(recalled.Raw, `"status":"recalled"`) ||
		!strings.Contains(recalled.Raw, `"replayed":false`) {
		t.Fatalf("recall response = %s", recalled.Raw)
	}
	recalledReplay := localDemoRequest(t, server, http.MethodPost,
		"/api/v1/demo/im/conversations/"+conversationID+"/messages/"+messageID+"/recall",
		`{}`, "Bearer "+localdemo.LocalBearerToken)
	if recalledReplay.Code != httpapi.CodeOK || !strings.Contains(recalledReplay.Raw, `"replayed":true`) {
		t.Fatalf("recall replay response = %s", recalledReplay.Raw)
	}

	conversations := localDemoRequest(t, server, http.MethodGet,
		"/api/v1/demo/im/conversations?limit=20", "", "Bearer "+localdemo.LocalBearerToken)
	if conversations.Code != httpapi.CodeOK || !strings.Contains(conversations.Raw, `"id":"`+conversationID+`"`) ||
		!strings.Contains(conversations.Raw, `"hasMore":false`) {
		t.Fatalf("conversation page = %s", conversations.Raw)
	}

	messages := localDemoRequest(t, server, http.MethodGet,
		"/api/v1/demo/im/conversations/"+conversationID+"/messages?limit=1", "", "Bearer "+localdemo.LocalBearerToken)
	if messages.Code != httpapi.CodeOK || !strings.Contains(messages.Raw, `"status":"recalled"`) ||
		!strings.Contains(messages.Raw, `"text":""`) || !strings.Contains(messages.Raw, `"hasMore":false`) {
		t.Fatalf("message page = %s", messages.Raw)
	}
	memberGroup := localDemoRequest(t, server, http.MethodPost, "/api/v1/demo/im/conversations", `{
		"type":"group",
		"name":"HTTP 成员动作群",
		"memberActorIds":[],
		"idempotencyKey":"http/create/members/1"
	}`, "Bearer "+localdemo.LocalBearerToken)
	if memberGroup.Code != httpapi.CodeOK {
		t.Fatalf("member group create response = %s", memberGroup.Raw)
	}
	memberConversationID := decodeConversationID(t, memberGroup.Raw)
	added := localDemoRequest(t, server, http.MethodPost,
		"/api/v1/demo/im/conversations/"+memberConversationID+"/members",
		`{"memberActorIds":["agt_local_research"],"idempotencyKey":"http/members/add/1"}`,
		"Bearer "+localdemo.LocalBearerToken)
	if added.Code != httpapi.CodeOK || !strings.Contains(added.Raw, `"addedActorIds":["agt_local_research"]`) ||
		!strings.Contains(added.Raw, `"memberActorIds":["agt_local_research","usr_local_demo"]`) {
		t.Fatalf("member add response = %s", added.Raw)
	}
	addedReplay := localDemoRequest(t, server, http.MethodPost,
		"/api/v1/demo/im/conversations/"+memberConversationID+"/members",
		`{"memberActorIds":["agt_local_research"],"idempotencyKey":"http/members/add/1"}`,
		"Bearer "+localdemo.LocalBearerToken)
	if addedReplay.Code != httpapi.CodeOK || !strings.Contains(addedReplay.Raw, `"replayed":true`) {
		t.Fatalf("member add replay response = %s", addedReplay.Raw)
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

func decodeMessageID(t *testing.T, raw string) string {
	t.Helper()
	var envelope struct {
		Data struct {
			Message struct {
				ID string `json:"id"`
			} `json:"message"`
		} `json:"data"`
	}
	if err := json.Unmarshal([]byte(raw), &envelope); err != nil || envelope.Data.Message.ID == "" {
		t.Fatalf("decode message = %q (%v)", envelope.Data.Message.ID, err)
	}
	return envelope.Data.Message.ID
}
