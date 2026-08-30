package app

import (
	"net/http"
	"strings"
	"testing"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/adapters/httpapi"
)

func TestLocalDemoAgentStoreCommandStatusEnvelope(t *testing.T) {
	t.Parallel()
	server, err := NewLocalDemo()
	if err != nil {
		t.Fatal(err)
	}
	auth := "Bearer demo.local.signature"
	firstInstall := localDemoRequest(t, server, http.MethodPost, "/api/v1/demo/im/agents/agd_local_planner/install",
		`{"idempotencyKey":"http/store/status/install"}`, auth)
	if firstInstall.Code != httpapi.CodeOK || !strings.Contains(firstInstall.Raw, `"commandStatus":"committed"`) || strings.Contains(firstInstall.Raw, `"replayed":true`) {
		t.Fatalf("first install = %#v", firstInstall)
	}
	replayedInstall := localDemoRequest(t, server, http.MethodPost, "/api/v1/demo/im/agents/agd_local_planner/install",
		`{"idempotencyKey":"http/store/status/install"}`, auth)
	if replayedInstall.Code != httpapi.CodeOK || !strings.Contains(replayedInstall.Raw, `"commandStatus":"replayed"`) || !strings.Contains(replayedInstall.Raw, `"replayed":true`) {
		t.Fatalf("replayed install = %#v", replayedInstall)
	}
	firstOffboard := localDemoRequest(t, server, http.MethodPost, "/api/v1/demo/im/agents/agd_local_planner/offboard",
		`{"idempotencyKey":"http/store/status/offboard","dataDisposition":"archive"}`, auth)
	if firstOffboard.Code != httpapi.CodeOK || !strings.Contains(firstOffboard.Raw, `"commandStatus":"committed"`) || strings.Contains(firstOffboard.Raw, `"replayed":true`) {
		t.Fatalf("first offboard = %#v", firstOffboard)
	}
	replayedOffboard := localDemoRequest(t, server, http.MethodPost, "/api/v1/demo/im/agents/agd_local_planner/offboard",
		`{"idempotencyKey":"http/store/status/offboard","dataDisposition":"archive"}`, auth)
	if replayedOffboard.Code != httpapi.CodeOK || !strings.Contains(replayedOffboard.Raw, `"commandStatus":"replayed"`) || !strings.Contains(replayedOffboard.Raw, `"replayed":true`) {
		t.Fatalf("replayed offboard = %#v", replayedOffboard)
	}
}
