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

func TestRuntimeAuthenticatedContextResolvesTenantAuthorityBeforeHandler(t *testing.T) {
	t.Parallel()
	const tenantValue = "ten_alpha"
	now := time.Date(2026, 8, 29, 12, 0, 0, 0, time.UTC)
	verifier := testVerifier(t)
	tenant, err := im.ParseTenantID(tenantValue)
	if err != nil {
		t.Fatal(err)
	}
	authority := newAppIdentityAuthority(t, tenant)
	readCalls := 0
	server, err := NewRuntime(RuntimeDependencies{
		Database: &fakeReadinessProbe{},
		Persistence: fakeTenantUnitOfWork{read: func(
			ctx context.Context, requestedTenant im.TenantID, operation store.ReadOperation,
		) error {
			readCalls++
			if requestedTenant != tenant {
				t.Fatalf("requested tenant = %s, want %s", requestedTenant.String(), tenant.String())
			}
			return operation(ctx, fakeTenantRepositories{identity: authority})
		}},
		Verifier: verifier,
		Now:      func() time.Time { return now },
	})
	if err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest(http.MethodGet, "/api/v1/auth/context", nil)
	request.Header.Set("Authorization", "Bearer header.payload.signature")
	request.Header.Set(tenantIDHeader, tenantValue)
	response, err := server.Test(request)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	var envelope struct {
		Code int `json:"code"`
		Data struct {
			Provider        string `json:"provider"`
			ExternalSubject string `json:"externalSubject"`
			PrincipalID     string `json:"principalId"`
			TenantID        string `json:"tenantId"`
			ActorID         string `json:"actorId"`
			MembershipRole  string `json:"membershipRole"`
		} `json:"data"`
	}
	if err := json.NewDecoder(response.Body).Decode(&envelope); err != nil {
		t.Fatal(err)
	}
	if response.StatusCode != http.StatusOK || envelope.Code != int(httpapi.CodeOK) ||
		envelope.Data.Provider != "clerk" || envelope.Data.ExternalSubject != "user_alice" ||
		envelope.Data.PrincipalID != "hpr_alice" || envelope.Data.TenantID != tenantValue ||
		envelope.Data.ActorID != "usr_alice" || envelope.Data.MembershipRole != "member" {
		t.Fatalf("context envelope = %#v", envelope)
	}
	if readCalls != 1 {
		t.Fatalf("identity read calls = %d, want 1", readCalls)
	}
}

func TestRuntimeAuthenticatedContextRejectsMissingTenantAndInactiveMembership(t *testing.T) {
	t.Parallel()
	now := time.Date(2026, 8, 29, 12, 0, 0, 0, time.UTC)
	verifier := testVerifier(t)
	tenant, err := im.ParseTenantID("ten_alpha")
	if err != nil {
		t.Fatal(err)
	}
	authority := newAppIdentityAuthority(t, tenant)
	server, err := NewRuntime(RuntimeDependencies{
		Database: &fakeReadinessProbe{},
		Persistence: fakeTenantUnitOfWork{read: func(
			ctx context.Context, _ im.TenantID, operation store.ReadOperation,
		) error {
			return operation(ctx, fakeTenantRepositories{identity: authority})
		}},
		Verifier: verifier,
		Now:      func() time.Time { return now },
	})
	if err != nil {
		t.Fatal(err)
	}
	missingHeader := httptest.NewRequest(http.MethodGet, "/api/v1/auth/context", nil)
	missingHeader.Header.Set("Authorization", "Bearer header.payload.signature")
	missingResponse, err := server.Test(missingHeader)
	if err != nil {
		t.Fatal(err)
	}
	defer missingResponse.Body.Close()
	var missingEnvelope struct {
		Code int `json:"code"`
	}
	if err := json.NewDecoder(missingResponse.Body).Decode(&missingEnvelope); err != nil {
		t.Fatal(err)
	}
	if missingEnvelope.Code != int(httpapi.CodeMalformedRequest) {
		t.Fatalf("missing tenant code = %d, want %d", missingEnvelope.Code, httpapi.CodeMalformedRequest)
	}

	authority.membership, err = im.NewTenantMembershipSnapshot(
		tenant, authority.principal.PrincipalID(), authority.actor.Ref(), authority.membership.Role(),
		im.TenantMembershipRemoved, authority.membership.Revision(),
	)
	if err != nil {
		t.Fatal(err)
	}
	inactive := httptest.NewRequest(http.MethodGet, "/api/v1/auth/context", nil)
	inactive.Header.Set("Authorization", "Bearer header.payload.signature")
	inactive.Header.Set(tenantIDHeader, tenant.String())
	inactiveResponse, err := server.Test(inactive)
	if err != nil {
		t.Fatal(err)
	}
	defer inactiveResponse.Body.Close()
	var inactiveEnvelope struct {
		Code int `json:"code"`
	}
	if err := json.NewDecoder(inactiveResponse.Body).Decode(&inactiveEnvelope); err != nil {
		t.Fatal(err)
	}
	if inactiveEnvelope.Code != int(httpapi.CodeForbidden) {
		t.Fatalf("inactive membership code = %d, want %d", inactiveEnvelope.Code, httpapi.CodeForbidden)
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

type fakeTenantUnitOfWork struct {
	read func(context.Context, im.TenantID, store.ReadOperation) error
}

func (unit fakeTenantUnitOfWork) Read(ctx context.Context, tenant im.TenantID, operation store.ReadOperation) error {
	if unit.read != nil {
		return unit.read(ctx, tenant, operation)
	}
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

type fakeTenantRepositories struct {
	identity store.IdentityAuthorityRepository
}

func (repositories fakeTenantRepositories) Conversations() store.ConversationRepository { return nil }
func (repositories fakeTenantRepositories) Authority() store.ConversationAuthorityRepository {
	return nil
}
func (repositories fakeTenantRepositories) Identity() store.IdentityAuthorityRepository {
	return repositories.identity
}

type appIdentityAuthority struct {
	binding    im.HumanExternalIdentityBinding
	principal  im.HumanPrincipalSnapshot
	membership im.TenantMembershipSnapshot
	actor      im.ActorSnapshot
}

func newAppIdentityAuthority(t *testing.T, tenant im.TenantID) *appIdentityAuthority {
	t.Helper()
	realm, err := im.ParseProviderRealmID("rlm_app_test")
	if err != nil {
		t.Fatal(err)
	}
	external, err := im.NewExternalIdentityRef(im.IdentityProviderClerk, realm, "user_alice")
	if err != nil {
		t.Fatal(err)
	}
	principalID, err := im.ParseHumanPrincipalID("hpr_alice")
	if err != nil {
		t.Fatal(err)
	}
	principal, err := im.NewHumanPrincipalSnapshot(principalID, im.HumanPrincipalActive, 1)
	if err != nil {
		t.Fatal(err)
	}
	binding, err := im.NewHumanExternalIdentityBinding(external, principalID, im.ExternalIdentityBindingActive, 1)
	if err != nil {
		t.Fatal(err)
	}
	actorID, err := im.ParseActorID("usr_alice")
	if err != nil {
		t.Fatal(err)
	}
	actorRef, err := im.NewActorRef(tenant, actorID)
	if err != nil {
		t.Fatal(err)
	}
	actor, err := im.NewActorSnapshot(actorRef, im.SubjectHuman, im.ActorActive, 1)
	if err != nil {
		t.Fatal(err)
	}
	membership, err := im.NewTenantMembershipSnapshot(
		tenant, principalID, actorRef, im.TenantMembershipMember, im.TenantMembershipActive, 1,
	)
	if err != nil {
		t.Fatal(err)
	}
	return &appIdentityAuthority{binding: binding, principal: principal, membership: membership, actor: actor}
}

func (authority *appIdentityAuthority) CurrentHumanIdentityBinding(context.Context, im.ExternalIdentityRef) (im.HumanExternalIdentityBinding, error) {
	return authority.binding, nil
}
func (authority *appIdentityAuthority) CurrentHumanPrincipal(context.Context, im.HumanPrincipalID) (im.HumanPrincipalSnapshot, error) {
	return authority.principal, nil
}
func (authority *appIdentityAuthority) CurrentTenantMembership(context.Context, im.TenantID, im.HumanPrincipalID) (im.TenantMembershipSnapshot, error) {
	return authority.membership, nil
}
func (authority *appIdentityAuthority) CurrentActor(context.Context, im.ActorRef) (im.ActorSnapshot, error) {
	return authority.actor, nil
}
