package improjection

import (
	"context"
	"errors"
	"testing"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/events"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
)

func TestProjectorRejectsNilPoolAndInvalidRunInputs(t *testing.T) {
	if _, err := NewProjector(nil); !errors.Is(err, ErrProjectorInvalid) {
		t.Fatalf("nil pool error=%v", err)
	}
	projector := &Projector{}
	tenant, err := im.ParseTenantID("ten_alpha")
	if err != nil {
		t.Fatal(err)
	}
	for _, pageSize := range []uint32{1, maximumProjectorPageSize + 1} {
		if _, err := projector.Run(context.Background(), tenant, nil, pageSize); !errors.Is(err, ErrProjectorInvalid) {
			t.Fatalf("page size %d error=%v", pageSize, err)
		}
	}
	if _, err := projector.Run(context.Background(), im.TenantID{}, nil, 1); !errors.Is(err, ErrProjectorInvalid) {
		t.Fatalf("zero tenant error=%v", err)
	}
}

func TestProjectorRecognizesOnlyMessageVocabularyAndSkipsOtherStreams(t *testing.T) {
	if !isMessageProjectionEvent("message.created") || !isMessageProjectionEvent("message.edited") ||
		!isMessageProjectionEvent("message.recalled") {
		t.Fatal("message event vocabulary not recognized")
	}
	if isMessageProjectionEvent("conversation.updated") || isMessageProjectionEvent("task.created.v1") {
		t.Fatal("non-message event recognized")
	}
}

func TestMessageIDExtractionAcceptsFullCreatedPayloadAndRejectsTrailingJSON(t *testing.T) {
	payload, err := events.NewInlinePayload([]byte(`{"conversationId":"cnv_room","messageId":"msg_1","messageType":"text","text":"hello"}`))
	if err != nil {
		t.Fatal(err)
	}
	event := events.StoredEvent{EventToAppend: events.EventToAppend{EventType: "message.created", Payload: payload}}
	messageID, err := messageIDFromProjectionEvent(event)
	if err != nil || messageID != "msg_1" {
		t.Fatalf("message id=%q error=%v", messageID, err)
	}
	missing, err := events.NewInlinePayload([]byte(`{"conversationId":"cnv_room"}`))
	if err != nil {
		t.Fatal(err)
	}
	event.Payload = missing
	if _, err := messageIDFromProjectionEvent(event); !errors.Is(err, ErrProjectorIntegrity) {
		t.Fatalf("missing message id error=%v", err)
	}
}
