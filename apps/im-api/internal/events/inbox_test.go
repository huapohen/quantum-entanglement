package events

import (
	"context"
	"errors"
	"sync"
	"testing"
)

func TestMemoryInboxStoreAdmitsAndReplaysByExactScopeAndDigest(t *testing.T) {
	store := NewMemoryInboxStore()
	envelope := validInboxEnvelope(t)
	first, err := store.Admit(t.Context(), envelope)
	if err != nil || first.Status != InboxInserted || first.Receipt.DeliveryCount != 1 {
		t.Fatalf("first admission = %#v, %v", first, err)
	}
	second, err := store.Admit(t.Context(), envelope)
	if err != nil || second.Status != InboxReplayed || second.Receipt.DeliveryCount != 2 {
		t.Fatalf("replay admission = %#v, %v", second, err)
	}
	if second.Receipt.Envelope.VerificationID != envelope.VerificationID {
		t.Fatalf("replay changed verified identity: %#v", second.Receipt.Envelope)
	}
	loaded, err := store.Load(t.Context(), envelope.Scope, envelope.EventID)
	if err != nil || loaded.DeliveryCount != 2 {
		t.Fatalf("load = %#v, %v", loaded, err)
	}
	if loaded.Envelope.Payload.Digest() != envelope.Payload.Digest() {
		t.Fatalf("payload digest = %q, want %q", loaded.Envelope.Payload.Digest(), envelope.Payload.Digest())
	}
}

func TestMemoryInboxStoreRejectsDigestAndPayloadDriftWithoutOverwrite(t *testing.T) {
	store := NewMemoryInboxStore()
	envelope := validInboxEnvelope(t)
	if _, err := store.Admit(t.Context(), envelope); err != nil {
		t.Fatalf("seed admission: %v", err)
	}
	changedDigest := envelope
	changedDigest.EventDigest = SHA256Digest("sha256:" + "b" + "111111111111111111111111111111111111111111111111111111111111111")
	if _, err := store.Admit(t.Context(), changedDigest); !errors.Is(err, ErrInboxDigestConflict) {
		t.Fatalf("changed event digest error = %v, want %v", err, ErrInboxDigestConflict)
	}
	changedPayload, err := NewInlinePayload([]byte(`{"text":"changed"}`))
	if err != nil {
		t.Fatalf("changed payload: %v", err)
	}
	changedEnvelope := envelope
	changedEnvelope.Payload = changedPayload
	if _, err := store.Admit(t.Context(), changedEnvelope); !errors.Is(err, ErrInboxDigestConflict) {
		t.Fatalf("changed payload error = %v, want %v", err, ErrInboxDigestConflict)
	}
	loaded, err := store.Load(t.Context(), envelope.Scope, envelope.EventID)
	if err != nil || string(loaded.Envelope.Payload.InlineJSON()) != `{"text":"hello"}` {
		t.Fatalf("stored payload after conflict = %#v, %v", loaded.Envelope.Payload.InlineJSON(), err)
	}
}

func TestMemoryInboxStoreScopesAreNotWildcardsAndConcurrentAdmissionHasOneInsert(t *testing.T) {
	store := NewMemoryInboxStore()
	envelope := validInboxEnvelope(t)
	otherWorkspace := "wsp_other"
	other := envelope
	other.Scope.WorkspaceID = &otherWorkspace
	if _, err := store.Load(t.Context(), other.Scope, envelope.EventID); !errors.Is(err, ErrInboxNotFound) {
		t.Fatalf("other workspace load = %v, want %v", err, ErrInboxNotFound)
	}
	const workers = 32
	statuses := make([]InboxAdmissionStatus, workers)
	errorsByWorker := make([]error, workers)
	var wait sync.WaitGroup
	wait.Add(workers)
	for index := range workers {
		go func(index int) {
			defer wait.Done()
			admission, err := store.Admit(context.Background(), envelope)
			statuses[index], errorsByWorker[index] = admission.Status, err
		}(index)
	}
	wait.Wait()
	inserted := 0
	for index := range workers {
		if errorsByWorker[index] != nil {
			t.Fatalf("worker %d error: %v", index, errorsByWorker[index])
		}
		if statuses[index] == InboxInserted {
			inserted++
		} else if statuses[index] != InboxReplayed {
			t.Fatalf("worker %d status = %q", index, statuses[index])
		}
	}
	if inserted != 1 {
		t.Fatalf("inserted admissions = %d, want 1", inserted)
	}
}

func TestInboxValidationRejectsInvalidScopeAndEnvelope(t *testing.T) {
	envelope := validInboxEnvelope(t)
	fixtures := []struct {
		name  string
		value InboxEnvelope
	}{
		{name: "empty tenant", value: func() InboxEnvelope { value := envelope; value.Scope.TenantID = ""; return value }()},
		{name: "empty provider", value: func() InboxEnvelope { value := envelope; value.Scope.Provider = ""; return value }()},
		{name: "empty event", value: func() InboxEnvelope { value := envelope; value.EventID = ""; return value }()},
		{name: "bad digest", value: func() InboxEnvelope { value := envelope; value.EventDigest = "sha256:not-a-digest"; return value }()},
		{name: "empty verification", value: func() InboxEnvelope { value := envelope; value.VerificationID = ""; return value }()},
	}
	for _, fixture := range fixtures {
		t.Run(fixture.name, func(t *testing.T) {
			if err := ValidateInboxEnvelope(fixture.value); !errors.Is(err, ErrInvalidInboxEnvelope) {
				t.Fatalf("validation error = %v, want %v", err, ErrInvalidInboxEnvelope)
			}
		})
	}
}

func validInboxEnvelope(t *testing.T) InboxEnvelope {
	t.Helper()
	payload, err := NewInlinePayload([]byte(`{"text":"hello"}`))
	if err != nil {
		t.Fatalf("payload: %v", err)
	}
	return InboxEnvelope{
		Scope: InboxScope{
			TenantID: "ten_alpha", WorkspaceID: nil, Provider: "rongcloud", ChannelID: "ch_alpha",
		},
		EventID: "evt_alpha", EventDigest: SHA256Digest("sha256:" + "a" + "111111111111111111111111111111111111111111111111111111111111111"),
		VerificationID: "verify_alpha", Payload: payload,
	}
}
