package app

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestLiveHealth(t *testing.T) {
	t.Parallel()

	response, err := New().Test(httptest.NewRequest(http.MethodGet, "/health/live", nil))
	if err != nil {
		t.Fatalf("request live health: %v", err)
	}
	defer response.Body.Close()

	if response.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want %d", response.StatusCode, http.StatusOK)
	}

	var payload struct {
		Status string `json:"status"`
	}
	if err := json.NewDecoder(response.Body).Decode(&payload); err != nil {
		t.Fatalf("decode live health: %v", err)
	}
	if payload.Status != "ok" {
		t.Fatalf("status payload = %q, want %q", payload.Status, "ok")
	}
}
