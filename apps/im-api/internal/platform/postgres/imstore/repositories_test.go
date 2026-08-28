package imstore

import (
	"errors"
	"testing"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
	store "github.com/huapohen/quantum-entanglement/apps/im-api/internal/imstore"
	"github.com/jackc/pgx/v5/pgconn"
)

func TestValidCASRevisionRequiresCreateOrExactSuccessor(t *testing.T) {
	for _, fixture := range []struct {
		name     string
		expected uint64
		next     uint64
		want     error
	}{
		{name: "create", expected: 0, next: 1},
		{name: "update", expected: 41, next: 42},
		{name: "create gap", expected: 0, next: 2, want: store.ErrRevisionConflict},
		{name: "update gap", expected: 41, next: 43, want: store.ErrRevisionConflict},
		{name: "rewind", expected: 41, next: 40, want: store.ErrRevisionConflict},
		{name: "reuse", expected: 41, next: 41, want: store.ErrRevisionConflict},
		{name: "PostgreSQL maximum", expected: 1<<63 - 2, next: 1<<63 - 1},
		{name: "PostgreSQL maximum has no successor", expected: 1<<63 - 1, next: 1 << 63, want: store.ErrRevisionConflict},
		{name: "above PostgreSQL maximum", expected: 1 << 63, next: 1<<63 + 1, want: store.ErrRevisionConflict},
		{name: "overflow", expected: ^uint64(0), next: 0, want: store.ErrRevisionConflict},
	} {
		t.Run(fixture.name, func(t *testing.T) {
			err := validCASRevision(fixture.expected, fixture.next)
			if !errors.Is(err, fixture.want) || fixture.want == nil && err != nil {
				t.Fatalf("validCASRevision(%d, %d) = %v, want %v", fixture.expected, fixture.next, err, fixture.want)
			}
		})
	}
}

func TestConversationPersistenceRejectsUnsupportedTopologyAndTenantDrift(t *testing.T) {
	tenant := mustTenantID(t, "ten_alpha")
	repositories := &tenantRepositories{tenantID: tenant}
	reference := mustConversationRef(t, tenant, "cnv_alpha")
	ordinary, err := im.NewConversationSnapshot(
		reference,
		nil,
		im.ConversationGroup,
		im.ConversationActive,
		im.ConversationID{},
		im.MessageID{},
		im.InvocationID{},
		1,
	)
	if err != nil {
		t.Fatalf("create ordinary snapshot: %v", err)
	}
	if err := repositories.validateConversationCAS(0, ordinary); err != nil {
		t.Fatalf("ordinary create validation: %v", err)
	}

	parentID, err := im.ParseConversationID("cnv_parent")
	if err != nil {
		t.Fatalf("parse parent: %v", err)
	}
	messageID, err := im.ParseMessageID("msg_root")
	if err != nil {
		t.Fatalf("parse root message: %v", err)
	}
	invocationID, err := im.ParseInvocationID("inv_agent")
	if err != nil {
		t.Fatalf("parse invocation: %v", err)
	}
	thread, err := im.NewConversationSnapshot(
		reference,
		nil,
		im.ConversationAgentThread,
		im.ConversationActive,
		parentID,
		messageID,
		invocationID,
		1,
	)
	if err != nil {
		t.Fatalf("create thread snapshot: %v", err)
	}
	if err := repositories.validateConversationCAS(0, thread); !errors.Is(
		err,
		store.ErrPersistenceUnsupported,
	) {
		t.Fatalf("thread persistence validation = %v", err)
	}

	otherTenant := mustTenantID(t, "ten_beta")
	other := mustConversationRef(t, otherTenant, "cnv_alpha")
	crossTenant, err := im.NewConversationSnapshot(
		other,
		nil,
		im.ConversationGroup,
		im.ConversationActive,
		im.ConversationID{},
		im.MessageID{},
		im.InvocationID{},
		1,
	)
	if err != nil {
		t.Fatalf("create cross-tenant snapshot: %v", err)
	}
	if err := repositories.validateConversationCAS(0, crossTenant); !errors.Is(
		err,
		store.ErrInvalidRequest,
	) {
		t.Fatalf("cross-tenant persistence validation = %v", err)
	}
}

func TestPostgresErrorsMapToStableStoreErrors(t *testing.T) {
	for _, fixture := range []struct {
		code string
		want error
	}{
		{code: "23505", want: store.ErrRevisionConflict},
		{code: "23503", want: store.ErrIntegrity},
		{code: "23514", want: store.ErrIntegrity},
		{code: "40001", want: store.ErrStoreUnavailable},
		{code: "42501", want: store.ErrStoreUnavailable},
	} {
		err := mapWriteError(
			&pgconn.PgError{Code: fixture.code, ConstraintName: "canary"},
			store.ErrRevisionConflict,
		)
		if !errors.Is(err, fixture.want) {
			t.Fatalf("mapWriteError(%s) = %v, want %v", fixture.code, err, fixture.want)
		}
	}
}

func mustTenantID(t *testing.T, value string) im.TenantID {
	t.Helper()
	tenantID, err := im.ParseTenantID(value)
	if err != nil {
		t.Fatalf("parse tenant %q: %v", value, err)
	}
	return tenantID
}

func mustConversationRef(
	t *testing.T,
	tenantID im.TenantID,
	value string,
) im.ConversationRef {
	t.Helper()
	conversationID, err := im.ParseConversationID(value)
	if err != nil {
		t.Fatalf("parse conversation %q: %v", value, err)
	}
	reference, err := im.NewConversationRef(tenantID, conversationID)
	if err != nil {
		t.Fatalf("create conversation reference: %v", err)
	}
	return reference
}
