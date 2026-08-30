package app

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/adapters/httpapi"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/events"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
	store "github.com/huapohen/quantum-entanglement/apps/im-api/internal/imstore"
)

func TestRuntimeAuthenticatedEventReadUsesConversationAuthorityAndBoundCursor(t *testing.T) {
	t.Parallel()
	now := time.Date(2026, 8, 29, 12, 0, 0, 12_000, time.UTC)
	tenant, err := im.ParseTenantID("ten_alpha")
	if err != nil {
		t.Fatal(err)
	}
	conversationID, err := im.ParseConversationID("cnv_room")
	if err != nil {
		t.Fatal(err)
	}
	workspace, err := im.ParseWorkspaceID("wsp_alpha")
	if err != nil {
		t.Fatal(err)
	}
	reference, err := im.NewConversationRef(tenant, conversationID)
	if err != nil {
		t.Fatal(err)
	}
	conversation, err := im.NewConversationSnapshot(
		reference, &workspace, im.ConversationGroup, im.ConversationActive,
		im.ConversationID{}, im.MessageID{}, im.InvocationID{}, 7,
	)
	if err != nil {
		t.Fatal(err)
	}
	identity := newAppIdentityAuthority(t, tenant)
	membership, err := im.NewConversationMembershipSnapshot(
		reference, identity.actor.Ref(), im.ConversationMembershipMember,
		im.ConversationMembershipActive, 8,
	)
	if err != nil {
		t.Fatal(err)
	}
	access, err := im.NewConversationAccessSnapshot(
		reference, identity.actor.Ref(),
		[]im.ConversationPermission{im.ConversationPermissionRead}, 9,
	)
	if err != nil {
		t.Fatal(err)
	}
	eventStore, err := events.NewVolatileMemoryStore("app-event-read", func(context.Context) time.Time { return now })
	if err != nil {
		t.Fatal(err)
	}
	workspaceValue := workspace.String()
	payload, err := events.NewInlinePayload([]byte(`{"conversationId":"cnv_room","text":"hello"}`))
	if err != nil {
		t.Fatal(err)
	}
	_, err = eventStore.AppendBatch(context.Background(), events.AppendBatch{
		TenantID: tenant.String(), WorkspaceID: &workspaceValue, StreamID: conversationID.String(),
		Events: []events.EventToAppend{{
			SchemaVersion: 1, EventID: "evt_message_1", StreamID: conversationID.String(),
			EventType: "message.created", TenantID: tenant.String(), WorkspaceID: &workspaceValue,
			ActorID: identity.actor.Ref().ActorID().String(), OccurredAt: now,
			CorrelationID: "corr_message_1", Payload: payload,
		}},
	})
	if err != nil {
		t.Fatal(err)
	}
	conversationRepository := &appConversationRepository{snapshot: conversation}
	authorityRepository := &appConversationAuthority{membership: membership, access: access}
	server, err := NewRuntime(RuntimeDependencies{
		Database: &fakeReadinessProbe{},
		Persistence: fakeTenantUnitOfWork{read: func(
			ctx context.Context, requestedTenant im.TenantID, operation store.ReadOperation,
		) error {
			if requestedTenant != tenant {
				t.Fatalf("requested tenant = %s, want %s", requestedTenant.String(), tenant.String())
			}
			return operation(ctx, fakeTenantRepositories{
				identity: identity, conversations: conversationRepository, authority: authorityRepository,
			})
		}},
		Verifier:   testVerifier(t),
		EventStore: eventStore,
		Now:        func() time.Time { return now },
	})
	if err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest(
		http.MethodGet,
		"/api/v1/tenants/ten_alpha/conversations/cnv_room/events?limit=1",
		nil,
	)
	request.Header.Set("Authorization", "Bearer header.payload.signature")
	request.Header.Set(tenantIDHeader, tenant.String())
	response, err := server.Test(request)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	var envelope struct {
		Code int `json:"code"`
		Data struct {
			TenantID       string `json:"tenantId"`
			ConversationID string `json:"conversationId"`
			Events         []struct {
				EventID     string         `json:"eventId"`
				DedupeKey   string         `json:"dedupeKey"`
				Sequence    uint64         `json:"sequence"`
				PayloadKind string         `json:"payloadKind"`
				Payload     map[string]any `json:"payload"`
			} `json:"events"`
			NextCursor string `json:"nextCursor"`
			HasMore    bool   `json:"hasMore"`
			Snapshot   struct {
				ConversationRevision uint64 `json:"conversationRevision"`
				MembershipRevision   uint64 `json:"membershipRevision"`
				AccessRevision       uint64 `json:"accessRevision"`
			} `json:"snapshot"`
		} `json:"data"`
	}
	if err := json.NewDecoder(response.Body).Decode(&envelope); err != nil {
		t.Fatal(err)
	}
	if response.StatusCode != http.StatusOK || envelope.Code != int(httpapi.CodeOK) ||
		envelope.Data.TenantID != tenant.String() || envelope.Data.ConversationID != conversationID.String() ||
		len(envelope.Data.Events) != 1 || envelope.Data.Events[0].EventID != "evt_message_1" ||
		envelope.Data.Events[0].DedupeKey != "evt_message_1" || envelope.Data.Events[0].Sequence != 1 ||
		envelope.Data.Events[0].PayloadKind != "inline" || envelope.Data.Events[0].Payload["text"] != "hello" ||
		envelope.Data.NextCursor == "" || envelope.Data.HasMore ||
		envelope.Data.Snapshot.ConversationRevision != 7 || envelope.Data.Snapshot.MembershipRevision != 8 ||
		envelope.Data.Snapshot.AccessRevision != 9 {
		t.Fatalf("event page envelope = %#v", envelope)
	}

	secondRequest := httptest.NewRequest(
		http.MethodGet,
		"/api/v1/tenants/ten_alpha/conversations/cnv_room/events?limit=1&after="+envelope.Data.NextCursor,
		nil,
	)
	secondRequest.Header.Set("Authorization", "Bearer header.payload.signature")
	secondRequest.Header.Set(tenantIDHeader, tenant.String())
	secondResponse, err := server.Test(secondRequest)
	if err != nil {
		t.Fatal(err)
	}
	defer secondResponse.Body.Close()
	var secondEnvelope struct {
		Code int `json:"code"`
		Data struct {
			Events  []json.RawMessage `json:"events"`
			HasMore bool              `json:"hasMore"`
		} `json:"data"`
	}
	if err := json.NewDecoder(secondResponse.Body).Decode(&secondEnvelope); err != nil {
		t.Fatal(err)
	}
	if secondEnvelope.Code != int(httpapi.CodeOK) || len(secondEnvelope.Data.Events) != 0 || secondEnvelope.Data.HasMore {
		t.Fatalf("empty tail envelope = %#v", secondEnvelope)
	}

	invalidCursorRequest := httptest.NewRequest(
		http.MethodGet,
		"/api/v1/tenants/ten_alpha/conversations/cnv_room/events?after=not-a-cursor",
		nil,
	)
	invalidCursorRequest.Header.Set("Authorization", "Bearer header.payload.signature")
	invalidCursorRequest.Header.Set(tenantIDHeader, tenant.String())
	invalidCursorResponse, err := server.Test(invalidCursorRequest)
	if err != nil {
		t.Fatal(err)
	}
	defer invalidCursorResponse.Body.Close()
	var invalidCursorEnvelope struct {
		Code int `json:"code"`
	}
	if err := json.NewDecoder(invalidCursorResponse.Body).Decode(&invalidCursorEnvelope); err != nil {
		t.Fatal(err)
	}
	if invalidCursorEnvelope.Code != int(httpapi.CodeValidationFailed) {
		t.Fatalf("invalid cursor code = %d, want %d", invalidCursorEnvelope.Code, httpapi.CodeValidationFailed)
	}
}

func TestRuntimeAuthenticatedEventReadFailsClosedForACLAndComposition(t *testing.T) {
	t.Parallel()
	tenant, err := im.ParseTenantID("ten_alpha")
	if err != nil {
		t.Fatal(err)
	}
	conversationID, err := im.ParseConversationID("cnv_room")
	if err != nil {
		t.Fatal(err)
	}
	reference, err := im.NewConversationRef(tenant, conversationID)
	if err != nil {
		t.Fatal(err)
	}
	conversation, err := im.NewConversationSnapshot(
		reference, nil, im.ConversationGroup, im.ConversationActive,
		im.ConversationID{}, im.MessageID{}, im.InvocationID{}, 1,
	)
	if err != nil {
		t.Fatal(err)
	}
	identity := newAppIdentityAuthority(t, tenant)
	membership, err := im.NewConversationMembershipSnapshot(
		reference, identity.actor.Ref(), im.ConversationMembershipMember,
		im.ConversationMembershipActive, 1,
	)
	if err != nil {
		t.Fatal(err)
	}
	access, err := im.NewConversationAccessSnapshot(
		reference, identity.actor.Ref(), []im.ConversationPermission{}, 1,
	)
	if err != nil {
		t.Fatal(err)
	}
	server, err := NewRuntime(RuntimeDependencies{
		Database: &fakeReadinessProbe{},
		Persistence: fakeTenantUnitOfWork{read: func(
			ctx context.Context, _ im.TenantID, operation store.ReadOperation,
		) error {
			return operation(ctx, fakeTenantRepositories{
				identity:      identity,
				conversations: &appConversationRepository{snapshot: conversation},
				authority:     &appConversationAuthority{membership: membership, access: access},
			})
		}},
		Verifier: testVerifier(t),
		Now:      func() time.Time { return time.Date(2026, 8, 29, 12, 0, 0, 0, time.UTC) },
	})
	if err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest(http.MethodGet, "/api/v1/tenants/ten_alpha/conversations/cnv_room/events", nil)
	request.Header.Set("Authorization", "Bearer header.payload.signature")
	request.Header.Set(tenantIDHeader, tenant.String())
	response, err := server.Test(request)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	var envelope struct {
		Code int `json:"code"`
	}
	if err := json.NewDecoder(response.Body).Decode(&envelope); err != nil {
		t.Fatal(err)
	}
	if envelope.Code != int(httpapi.CodeForbidden) {
		t.Fatalf("ACL-denied code = %d, want %d", envelope.Code, httpapi.CodeForbidden)
	}

	if mapped := mapEventPageError(events.ErrInvalidCursor); !errors.Is(mapped, events.ErrInvalidCursor) {
		t.Fatalf("mapped invalid cursor lost cause: %v", mapped)
	}
}
