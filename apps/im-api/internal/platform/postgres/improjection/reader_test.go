package improjection

import (
	"errors"
	"testing"
	"time"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
	store "github.com/huapohen/quantum-entanglement/apps/im-api/internal/imstore"
)

func TestMaterializedReaderRejectsNilPool(t *testing.T) {
	if _, err := NewReader(nil); !errors.Is(err, store.ErrInvalidRequest) {
		t.Fatalf("nil pool error=%v", err)
	}
}

func TestMaterializedCursorBindsExactScopeAndRevision(t *testing.T) {
	binding := materializedCursorBinding{
		tenantID: "ten_alpha", workspaceID: "wsp_alpha", workspaceSet: true,
		conversationID: "cnv_room",
	}
	cursor, err := encodeCursor(binding, materializedCursor{
		projectionRevision: 3,
		createdAt:          time.Date(2026, 8, 30, 12, 0, 0, 0, time.UTC),
		messageID:          "msg_1",
	})
	if err != nil {
		t.Fatal(err)
	}
	decoded, err := decodeCursor(cursor, binding)
	if err != nil || decoded.projectionRevision != 3 || decoded.messageID != "msg_1" {
		t.Fatalf("decoded cursor=%#v error=%v", decoded, err)
	}
	cases := []materializedCursorBinding{
		{tenantID: "ten_other", workspaceID: "wsp_alpha", workspaceSet: true, conversationID: "cnv_room"},
		{tenantID: "ten_alpha", workspaceID: "", workspaceSet: false, conversationID: "cnv_room"},
		{tenantID: "ten_alpha", workspaceID: "wsp_alpha", workspaceSet: true, conversationID: "cnv_other"},
	}
	for _, wrong := range cases {
		if _, err := decodeCursor(cursor, wrong); !errors.Is(err, store.ErrInvalidRequest) {
			t.Fatalf("wrong binding=%#v error=%v", wrong, err)
		}
	}
	for _, malformed := range []string{"!", cursor + "=", " " + cursor} {
		if _, err := decodeCursor(malformed, binding); !errors.Is(err, store.ErrInvalidRequest) {
			t.Fatalf("malformed cursor=%q error=%v", malformed, err)
		}
	}
}

func TestMaterializedMessageSnapshotRejectsCrossTenantAndMalformedRows(t *testing.T) {
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
	valid := func(sender string) error {
		_, err := materializedMessageSnapshot(
			reference, "msg_1", "msg_client_1", sender, "text", "active", "hello", "",
			time.Date(2026, 8, 30, 12, 0, 0, 0, time.UTC), 1,
		)
		return err
	}
	if err := valid("usr_alice"); err != nil {
		t.Fatalf("valid row rejected=%v", err)
	}
	for _, sender := range []string{"", "not-an-actor"} {
		if err := valid(sender); !errors.Is(err, store.ErrIntegrity) {
			t.Fatalf("sender=%q error=%v", sender, err)
		}
	}
}
