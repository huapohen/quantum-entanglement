package app

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	authfake "github.com/huapohen/quantum-entanglement/apps/im-api/internal/adapters/auth/fake"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/adapters/httpapi"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
	store "github.com/huapohen/quantum-entanglement/apps/im-api/internal/imstore"
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

func TestRuntimeReadinessAndBusinessRouteBarrier(t *testing.T) {
	t.Parallel()
	probe := &fakeReadinessProbe{}
	server, err := NewRuntime(RuntimeDependencies{
		Database:    probe,
		Persistence: fakeTenantUnitOfWork{},
		Verifier:    testVerifier(t),
	})
	if err != nil {
		t.Fatalf("construct runtime server: %v", err)
	}

	ready, err := server.Test(httptest.NewRequest(http.MethodGet, "/health/ready", nil))
	if err != nil {
		t.Fatalf("request healthy readiness: %v", err)
	}
	defer ready.Body.Close()
	if ready.StatusCode != http.StatusOK {
		t.Fatalf("healthy readiness status = %d, want %d", ready.StatusCode, http.StatusOK)
	}

	probe.err = errors.New("database drift canary")
	unready, err := server.Test(httptest.NewRequest(http.MethodGet, "/health/ready", nil))
	if err != nil {
		t.Fatalf("request unhealthy readiness: %v", err)
	}
	defer unready.Body.Close()
	if unready.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("unhealthy readiness status = %d, want %d", unready.StatusCode, http.StatusServiceUnavailable)
	}
	var readinessPayload struct {
		Status string `json:"status"`
	}
	if err := json.NewDecoder(unready.Body).Decode(&readinessPayload); err != nil ||
		readinessPayload.Status != "unavailable" {
		t.Fatalf("unhealthy readiness payload=%#v error=%v", readinessPayload, err)
	}

	blocked, err := server.Test(httptest.NewRequest(http.MethodGet, "/api/v1/system/ping", nil))
	if err != nil {
		t.Fatalf("request blocked business route: %v", err)
	}
	defer blocked.Body.Close()
	if blocked.StatusCode != http.StatusOK {
		t.Fatalf("business envelope HTTP status = %d, want %d", blocked.StatusCode, http.StatusOK)
	}
	var envelope struct {
		Code    int    `json:"code"`
		Message string `json:"message"`
	}
	if err := json.NewDecoder(blocked.Body).Decode(&envelope); err != nil {
		t.Fatalf("decode blocked business envelope: %v", err)
	}
	if envelope.Code != int(httpapi.CodeDependencyUnavailable) || envelope.Message != "dependency unavailable" {
		t.Fatalf("blocked business envelope = %#v", envelope)
	}
	if probe.calls != 3 {
		t.Fatalf("readiness probe calls = %d, want 3", probe.calls)
	}
}

func testVerifier(t *testing.T) *authfake.Verifier {
	t.Helper()
	realm, err := im.ParseProviderRealmID("rlm_app_test")
	if err != nil {
		t.Fatal(err)
	}
	now := time.Date(2026, 8, 29, 12, 0, 0, 0, time.UTC)
	verifier, err := authfake.New(authfake.Options{
		Realm: realm, Issuer: "clerk.test", Audience: "wanwork-test",
		Now: func() time.Time { return now },
		Tokens: map[string]authfake.TokenFixture{
			"header.payload.signature": {
				ExternalSubject: "user_alice", SessionID: "sess_app_test",
				IssuedAt: now.Add(-time.Minute), ExpiresAt: now.Add(time.Hour),
			},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	return verifier
}

func TestNewRuntimeRejectsMissingDependencies(t *testing.T) {
	t.Parallel()
	for _, dependencies := range []RuntimeDependencies{
		{},
		{Database: &fakeReadinessProbe{}},
		{Persistence: fakeTenantUnitOfWork{}},
	} {
		if _, err := NewRuntime(dependencies); !errors.Is(err, ErrInvalidRuntimeDependencies) {
			t.Fatalf("invalid runtime dependencies error = %v, want %v", err, ErrInvalidRuntimeDependencies)
		}
	}
}

type fakeReadinessProbe struct {
	err   error
	calls int
}

func (probe *fakeReadinessProbe) Ready(context.Context) error {
	probe.calls++
	return probe.err
}

type fakeTenantUnitOfWork struct{}

func (fakeTenantUnitOfWork) Read(context.Context, im.TenantID, store.ReadOperation) error {
	return nil
}

func (fakeTenantUnitOfWork) Execute(
	context.Context,
	im.TenantID,
	store.CommandIdentity,
	store.ExecuteOperation,
) (store.CommitReceipt, error) {
	return store.CommitReceipt{}, nil
}

func (fakeTenantUnitOfWork) Resolve(
	context.Context,
	im.TenantID,
	store.CommandIdentity,
) (store.CommitReceipt, error) {
	return store.CommitReceipt{}, nil
}
