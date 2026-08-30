package app

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/gofiber/fiber/v3"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/adapters/httpapi"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
	store "github.com/huapohen/quantum-entanglement/apps/im-api/internal/imstore"
)

func TestRuntimeAuthenticatedMessageReadReturnsScopedPage(t *testing.T) {
	t.Parallel()
	now := time.Date(2026, 8, 29, 12, 0, 0, 0, time.UTC)
	server, tenant, _, message, reader := newMessageReadTestRuntime(t, now, true, true)
	request := httptest.NewRequest(
		http.MethodGet,
		"/api/v1/tenants/ten_alpha/conversations/cnv_room/messages?limit=1",
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
			Messages       []struct {
				ID       string `json:"id"`
				Text     string `json:"text"`
				Revision uint64 `json:"revision"`
			} `json:"messages"`
			HasMore  bool `json:"hasMore"`
			Snapshot struct {
				ConversationRevision uint64 `json:"conversationRevision"`
				MembershipRevision   uint64 `json:"membershipRevision"`
				AccessRevision       uint64 `json:"accessRevision"`
				ProjectionRevision   uint64 `json:"projectionRevision"`
			} `json:"snapshot"`
		} `json:"data"`
	}
	if err := json.NewDecoder(response.Body).Decode(&envelope); err != nil {
		t.Fatal(err)
	}
	if response.StatusCode != http.StatusOK || envelope.Code != int(httpapi.CodeOK) ||
		envelope.Data.TenantID != tenant.String() || envelope.Data.ConversationID != "cnv_room" ||
		len(envelope.Data.Messages) != 1 || envelope.Data.Messages[0].ID != message.Ref().MessageID().String() ||
		envelope.Data.Messages[0].Text != "hello" || envelope.Data.Messages[0].Revision != 1 ||
		envelope.Data.HasMore || envelope.Data.Snapshot.ConversationRevision != 7 ||
		envelope.Data.Snapshot.MembershipRevision != 8 || envelope.Data.Snapshot.AccessRevision != 9 ||
		envelope.Data.Snapshot.ProjectionRevision != 11 || reader.calls != 1 {
		t.Fatalf("message page envelope=%#v calls=%d", envelope, reader.calls)
	}
}

func TestRuntimeAuthenticatedMessageReadChecksACLBeforeRepository(t *testing.T) {
	t.Parallel()
	server, tenant, _, _, reader := newMessageReadTestRuntime(
		t, time.Date(2026, 8, 29, 12, 0, 0, 0, time.UTC), true, false,
	)
	request := httptest.NewRequest(
		http.MethodGet,
		"/api/v1/tenants/ten_alpha/conversations/cnv_room/messages",
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
	}
	if err := json.NewDecoder(response.Body).Decode(&envelope); err != nil {
		t.Fatal(err)
	}
	if envelope.Code != int(httpapi.CodeForbidden) || reader.calls != 0 {
		t.Fatalf("ACL envelope=%#v repository calls=%d", envelope, reader.calls)
	}
}

func TestRuntimeAuthenticatedMessageReadFailsClosedWhenRepositoryMissing(t *testing.T) {
	t.Parallel()
	server, tenant, _, _, _ := newMessageReadTestRuntime(
		t, time.Date(2026, 8, 29, 12, 0, 0, 0, time.UTC), false, true,
	)
	request := httptest.NewRequest(
		http.MethodGet,
		"/api/v1/tenants/ten_alpha/conversations/cnv_room/messages",
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
	}
	if err := json.NewDecoder(response.Body).Decode(&envelope); err != nil {
		t.Fatal(err)
	}
	if envelope.Code != int(httpapi.CodeDependencyUnavailable) {
		t.Fatalf("missing repository code=%d", envelope.Code)
	}
}

func TestRuntimeAuthenticatedMessageReadBlocksOnShadowMismatch(t *testing.T) {
	t.Parallel()
	shadowCalls := 0
	server, tenant, _, _, reader := newMessageReadTestRuntime(
		t, time.Date(2026, 8, 29, 12, 0, 0, 0, time.UTC), true, true,
		func(context.Context, store.MessageReadPageQuery) error {
			shadowCalls++
			return errors.New("shadow mismatch canary")
		},
	)
	request := httptest.NewRequest(http.MethodGet,
		"/api/v1/tenants/ten_alpha/conversations/cnv_room/messages?limit=1", nil)
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
	if response.StatusCode != http.StatusOK || envelope.Code != int(httpapi.CodeInternal) ||
		shadowCalls != 1 || reader.calls != 0 {
		t.Fatalf("shadow block status=%d envelope=%#v shadowCalls=%d readerCalls=%d",
			response.StatusCode, envelope, shadowCalls, reader.calls)
	}
}

func TestValidateMessageReadPageRejectsDuplicateAndCrossScopeRows(t *testing.T) {
	t.Parallel()
	tenant := mustMessageReadTenant(t, "ten_alpha")
	conversationID := mustMessageReadConversation(t, "cnv_room")
	reference, err := im.NewConversationRef(tenant, conversationID)
	if err != nil {
		t.Fatal(err)
	}
	actorID, err := im.ParseActorID("usr_alice")
	if err != nil {
		t.Fatal(err)
	}
	actor, err := im.NewActorRef(tenant, actorID)
	if err != nil {
		t.Fatal(err)
	}
	messageID := mustMessageReadID(t, "msg_1")
	clientID := mustMessageReadID(t, "msg_client_1")
	messageRef, err := im.NewMessageRef(reference, messageID)
	if err != nil {
		t.Fatal(err)
	}
	message, err := im.NewMessageSnapshot(
		messageRef, actor, clientID, im.MessageTypeText, im.MessageStatusActive,
		"hello", "", time.Date(2026, 8, 30, 12, 0, 0, 0, time.UTC), 1,
	)
	if err != nil {
		t.Fatal(err)
	}
	if err := validateMessageReadPage(store.MessageReadPage{
		Conversation: reference, Messages: []im.MessageSnapshot{message},
		ConversationRevision: 7, ProjectionRevision: 11,
	}, reference, 7, 2, ""); err != nil {
		t.Fatalf("valid page rejected: %v", err)
	}
	for name, page := range map[string]store.MessageReadPage{
		"duplicate":            {Conversation: reference, Messages: []im.MessageSnapshot{message, message}, ConversationRevision: 7, ProjectionRevision: 11},
		"missing conversation": {Messages: []im.MessageSnapshot{message}, ConversationRevision: 7, ProjectionRevision: 11},
	} {
		t.Run(name, func(t *testing.T) {
			if err := validateMessageReadPage(page, reference, 7, 2, ""); !errors.Is(err, store.ErrIntegrity) {
				t.Fatalf("validation error=%v, want %v", err, store.ErrIntegrity)
			}
		})
	}
}

type messageReadTestRepository struct {
	page  store.MessageReadPage
	calls int
}

func (repository *messageReadTestRepository) ReadPage(
	context.Context, store.MessageReadPageQuery,
) (store.MessageReadPage, error) {
	repository.calls++
	return repository.page, nil
}

func newMessageReadTestRuntime(
	t *testing.T, now time.Time, withRepository, canRead bool,
	shadow ...func(context.Context, store.MessageReadPageQuery) error,
) (*fiber.App, im.TenantID, im.ConversationRef, im.MessageSnapshot, *messageReadTestRepository) {
	t.Helper()
	tenant := mustMessageReadTenant(t, "ten_alpha")
	conversationID := mustMessageReadConversation(t, "cnv_room")
	reference, err := im.NewConversationRef(tenant, conversationID)
	if err != nil {
		t.Fatal(err)
	}
	conversation, err := im.NewConversationSnapshot(
		reference, nil, im.ConversationGroup, im.ConversationActive,
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
	permissions := []im.ConversationPermission{}
	if canRead {
		permissions = []im.ConversationPermission{im.ConversationPermissionRead}
	}
	access, err := im.NewConversationAccessSnapshot(
		reference, identity.actor.Ref(), permissions, 9,
	)
	if err != nil {
		t.Fatal(err)
	}
	messageID := mustMessageReadID(t, "msg_1")
	clientID := mustMessageReadID(t, "msg_client_1")
	messageRef, err := im.NewMessageRef(reference, messageID)
	if err != nil {
		t.Fatal(err)
	}
	message, err := im.NewMessageSnapshot(
		messageRef, identity.actor.Ref(), clientID, im.MessageTypeText, im.MessageStatusActive,
		"hello", "", now.UTC(), 1,
	)
	if err != nil {
		t.Fatal(err)
	}
	reader := &messageReadTestRepository{page: store.MessageReadPage{
		Conversation: reference, Messages: []im.MessageSnapshot{message},
		ConversationRevision: 7, ProjectionRevision: 11,
	}}
	dependencies := RuntimeDependencies{
		Database: &fakeReadinessProbe{},
		Persistence: fakeTenantUnitOfWork{read: func(
			ctx context.Context, requestedTenant im.TenantID, operation store.ReadOperation,
		) error {
			if requestedTenant != tenant {
				t.Fatalf("requested tenant=%s", requestedTenant.String())
			}
			return operation(ctx, fakeTenantRepositories{
				identity: identity, conversations: &appConversationRepository{snapshot: conversation},
				authority: &appConversationAuthority{membership: membership, access: access},
			})
		}},
		Verifier: testVerifier(t), Now: func() time.Time { return now },
	}
	if withRepository {
		dependencies.Messages = reader
	}
	if len(shadow) > 0 {
		dependencies.MessageShadow = shadow[0]
	}
	server, err := NewRuntime(dependencies)
	if err != nil {
		t.Fatal(err)
	}
	return server, tenant, reference, message, reader
}

func mustMessageReadTenant(t *testing.T, value string) im.TenantID {
	t.Helper()
	parsed, err := im.ParseTenantID(value)
	if err != nil {
		t.Fatal(err)
	}
	return parsed
}

func mustMessageReadConversation(t *testing.T, value string) im.ConversationID {
	t.Helper()
	parsed, err := im.ParseConversationID(value)
	if err != nil {
		t.Fatal(err)
	}
	return parsed
}

func mustMessageReadID(t *testing.T, value string) im.MessageID {
	t.Helper()
	parsed, err := im.ParseMessageID(value)
	if err != nil {
		t.Fatal(err)
	}
	return parsed
}
