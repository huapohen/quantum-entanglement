package events

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestDurableInboxFileStoreRoundTripsAndPersistsReplay(t *testing.T) {
	t.Parallel()

	path := filepath.Join(t.TempDir(), "native-im-inbox.log")
	firstAt := time.Date(2026, 8, 30, 8, 0, 0, 0, time.UTC)
	secondAt := firstAt.Add(3 * time.Second)
	clock := sequenceClock(firstAt, secondAt)
	envelope := validInboxEnvelope(t)
	store, err := OpenDurableInboxFileStore(t.Context(), path, clock)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	first, err := store.Admit(t.Context(), envelope)
	if err != nil || first.Status != InboxInserted || first.Receipt.DeliveryCount != 1 {
		t.Fatalf("first admission = %#v, %v", first, err)
	}
	second, err := store.Admit(t.Context(), envelope)
	if err != nil || second.Status != InboxReplayed || second.Receipt.DeliveryCount != 2 {
		t.Fatalf("replay admission = %#v, %v", second, err)
	}
	if !second.Receipt.FirstReceivedAt.Equal(firstAt) || !second.Receipt.LastReceivedAt.Equal(secondAt) {
		t.Fatalf("receipt times = %#v, want %s/%s", second.Receipt, firstAt, secondAt)
	}
	if err := store.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}

	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read log: %v", err)
	}
	if lines := strings.Count(string(raw), "\n"); lines != 2 {
		t.Fatalf("log lines = %d, want 2", lines)
	}
	if strings.Contains(string(raw), `"TenantID"`) || !strings.Contains(string(raw), `"tenantId"`) {
		t.Fatalf("scope wire names are not stable: %s", raw)
	}

	reopened, err := OpenDurableInboxFileStore(t.Context(), path, func(context.Context) time.Time { return secondAt })
	if err != nil {
		t.Fatalf("reopen: %v", err)
	}
	t.Cleanup(func() { _ = reopened.Close() })
	loaded, err := reopened.Load(t.Context(), envelope.Scope, envelope.EventID)
	if err != nil || loaded.DeliveryCount != 2 {
		t.Fatalf("load after reopen = %#v, %v", loaded, err)
	}
	if loaded.Envelope.EventDigest != envelope.EventDigest ||
		loaded.Envelope.VerificationID != envelope.VerificationID ||
		loaded.Envelope.Payload.Digest() != envelope.Payload.Digest() {
		t.Fatalf("loaded envelope drifted: %#v", loaded.Envelope)
	}
	reconciled, err := reopened.Reconcile(t.Context(), envelope)
	if err != nil || reconciled.Status != InboxReplayed || !reconciled.ResolvedAfterUnknown || reconciled.Receipt.DeliveryCount != 2 {
		t.Fatalf("reconcile = %#v, %v", reconciled, err)
	}
}

func TestDurableInboxFileStoreRejectsDigestDriftWithoutOverwrite(t *testing.T) {
	t.Parallel()

	path := filepath.Join(t.TempDir(), "conflict.log")
	clock := sequenceClock(time.Date(2026, 8, 30, 9, 0, 0, 0, time.UTC))
	store, err := OpenDurableInboxFileStore(t.Context(), path, clock)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	t.Cleanup(func() { _ = store.Close() })
	envelope := validInboxEnvelope(t)
	if _, err := store.Admit(t.Context(), envelope); err != nil {
		t.Fatalf("seed admission: %v", err)
	}
	changedPayload, err := NewInlinePayload([]byte(`{"text":"changed"}`))
	if err != nil {
		t.Fatalf("changed payload: %v", err)
	}
	fixtures := map[string]InboxEnvelope{
		"event digest": func() InboxEnvelope {
			value := envelope
			value.EventDigest = SHA256Digest("sha256:" + "b" + strings.Repeat("1", 63))
			return value
		}(),
		"payload digest": func() InboxEnvelope {
			value := envelope
			value.Payload = changedPayload
			return value
		}(),
		"verification id": func() InboxEnvelope {
			value := envelope
			value.VerificationID = "verify_changed"
			return value
		}(),
	}
	for name, changed := range fixtures {
		t.Run(name, func(t *testing.T) {
			if _, err := store.Admit(t.Context(), changed); !errors.Is(err, ErrInboxDigestConflict) {
				t.Fatalf("conflict error = %v, want %v", err, ErrInboxDigestConflict)
			}
		})
	}
	loaded, err := store.Load(t.Context(), envelope.Scope, envelope.EventID)
	if err != nil || loaded.DeliveryCount != 1 || string(loaded.Envelope.Payload.InlineJSON()) != `{"text":"hello"}` {
		t.Fatalf("stored receipt after conflicts = %#v, %v", loaded, err)
	}
}

func TestDurableInboxFileStoreRoundTripsReferencePayload(t *testing.T) {
	t.Parallel()

	path := filepath.Join(t.TempDir(), "reference.log")
	clock := sequenceClock(time.Date(2026, 8, 30, 10, 0, 0, 0, time.UTC))
	store, err := OpenDurableInboxFileStore(t.Context(), path, clock)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	envelope := validInboxEnvelope(t)
	payload, err := NewReferencedPayload(OpaquePayloadRef{
		Storage: "s3", ReferenceID: "blob-1", ByteLength: 42,
	}, SHA256Digest("sha256:"+strings.Repeat("c", 64)))
	if err != nil {
		t.Fatalf("reference payload: %v", err)
	}
	envelope.EventID = "evt_reference"
	envelope.Payload = payload
	if _, err := store.Admit(t.Context(), envelope); err != nil {
		t.Fatalf("reference admission: %v", err)
	}
	if err := store.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}
	reopened, err := OpenDurableInboxFileStore(t.Context(), path, clock)
	if err != nil {
		t.Fatalf("reopen: %v", err)
	}
	t.Cleanup(func() { _ = reopened.Close() })
	loaded, err := reopened.Load(t.Context(), envelope.Scope, envelope.EventID)
	if err != nil || loaded.Envelope.Payload.Kind() != PayloadReference {
		t.Fatalf("reference load = %#v, %v", loaded, err)
	}
	ref := loaded.Envelope.Payload.Reference()
	if ref == nil || ref.Storage != "s3" || ref.ReferenceID != "blob-1" || ref.ByteLength != 42 || loaded.Envelope.Payload.Digest() != payload.Digest() {
		t.Fatalf("reference payload drifted: %#v", loaded.Envelope.Payload)
	}
}

func TestDurableInboxFileStoreDiscardsOnlyInterruptedTail(t *testing.T) {
	t.Parallel()

	directory := t.TempDir()
	path := filepath.Join(directory, "tail.log")
	clock := sequenceClock(time.Date(2026, 8, 30, 11, 0, 0, 0, time.UTC))
	store, err := OpenDurableInboxFileStore(t.Context(), path, clock)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	envelope := validInboxEnvelope(t)
	if _, err := store.Admit(t.Context(), envelope); err != nil {
		t.Fatalf("admit: %v", err)
	}
	if err := store.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		t.Fatalf("open tail: %v", err)
	}
	if _, err := file.WriteString(`{"format":"quantum-entanglement.native-im-inbox/1"}`); err != nil {
		t.Fatalf("write tail: %v", err)
	}
	if err := file.Close(); err != nil {
		t.Fatalf("close tail: %v", err)
	}
	reopened, err := OpenDurableInboxFileStore(t.Context(), path, clock)
	if err != nil {
		t.Fatalf("reopen interrupted tail: %v", err)
	}
	t.Cleanup(func() { _ = reopened.Close() })
	loaded, err := reopened.Load(t.Context(), envelope.Scope, envelope.EventID)
	if err != nil || loaded.DeliveryCount != 1 {
		t.Fatalf("loaded after tail = %#v, %v", loaded, err)
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read truncated log: %v", err)
	}
	if strings.Count(string(raw), "\n") != 1 {
		t.Fatalf("tail was not truncated: %q", raw)
	}
}

func TestDurableInboxFileStoreFailsClosedOnCompleteCorruption(t *testing.T) {
	t.Parallel()

	path := filepath.Join(t.TempDir(), "corrupt.log")
	file, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o600)
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	if _, err := file.WriteString("not-json\n"); err != nil {
		t.Fatalf("write corruption: %v", err)
	}
	if err := file.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}
	if _, err := OpenDurableInboxFileStore(t.Context(), path, sequenceClock(time.Date(2026, 8, 30, 12, 0, 0, 0, time.UTC))); !errors.Is(err, ErrDurableInboxFileLog) {
		t.Fatalf("corruption error = %v, want %v", err, ErrDurableInboxFileLog)
	}
}

func TestDurableInboxFileStoreConcurrentExactRetryHasOneInsert(t *testing.T) {
	t.Parallel()

	path := filepath.Join(t.TempDir(), "concurrent.log")
	store, err := OpenDurableInboxFileStore(t.Context(), path, sequenceClock(time.Date(2026, 8, 30, 13, 0, 0, 0, time.UTC)))
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	t.Cleanup(func() { _ = store.Close() })
	envelope := validInboxEnvelope(t)
	const attempts = 24
	statuses := make(chan InboxAdmissionStatus, attempts)
	errorsCh := make(chan error, attempts)
	var wait sync.WaitGroup
	wait.Add(attempts)
	for range attempts {
		go func() {
			defer wait.Done()
			admission, err := store.Admit(context.Background(), envelope)
			statuses <- admission.Status
			errorsCh <- err
		}()
	}
	wait.Wait()
	close(statuses)
	close(errorsCh)
	for err := range errorsCh {
		if err != nil {
			t.Fatalf("concurrent admission error = %v", err)
		}
	}
	inserted, replayed := 0, 0
	for status := range statuses {
		switch status {
		case InboxInserted:
			inserted++
		case InboxReplayed:
			replayed++
		default:
			t.Fatalf("unexpected status %q", status)
		}
	}
	if inserted != 1 || replayed != attempts-1 {
		t.Fatalf("inserted=%d replayed=%d, want 1/%d", inserted, replayed, attempts-1)
	}
	receipt, err := store.Load(t.Context(), envelope.Scope, envelope.EventID)
	if err != nil || receipt.DeliveryCount != attempts {
		t.Fatalf("concurrent receipt = %#v, %v", receipt, err)
	}
}

func TestDurableInboxFileStoreClockAndClosedContracts(t *testing.T) {
	t.Parallel()

	path := filepath.Join(t.TempDir(), "clock.log")
	if _, err := OpenDurableInboxFileStore(t.Context(), path, nil); !errors.Is(err, ErrStoreClock) {
		t.Fatalf("nil clock error = %v, want %v", err, ErrStoreClock)
	}
	store, err := OpenDurableInboxFileStore(t.Context(), path, func(context.Context) time.Time { return time.Time{} })
	if err != nil {
		t.Fatalf("open zero-clock store: %v", err)
	}
	envelope := validInboxEnvelope(t)
	if _, err := store.Admit(t.Context(), envelope); !errors.Is(err, ErrStoreClock) {
		t.Fatalf("zero clock admission = %v, want %v", err, ErrStoreClock)
	}
	if err := store.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}
	if _, err := store.Admit(t.Context(), envelope); !errors.Is(err, ErrDurableInboxFileClosed) {
		t.Fatalf("closed admission = %v, want %v", err, ErrDurableInboxFileClosed)
	}
	if _, err := store.Load(t.Context(), envelope.Scope, envelope.EventID); !errors.Is(err, ErrDurableInboxFileClosed) {
		t.Fatalf("closed load = %v, want %v", err, ErrDurableInboxFileClosed)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := OpenDurableInboxFileStore(ctx, filepath.Join(t.TempDir(), "cancel.log"), sequenceClock(time.Now().UTC())); !errors.Is(err, context.Canceled) {
		t.Fatalf("canceled open = %v, want %v", err, context.Canceled)
	}
}

func TestDurableInboxFileStoreRejectsClockRegressionWithoutWrite(t *testing.T) {
	t.Parallel()

	path := filepath.Join(t.TempDir(), "regression.log")
	firstAt := time.Date(2026, 8, 30, 14, 0, 0, 0, time.UTC)
	secondAt := firstAt.Add(-time.Second)
	var mu sync.Mutex
	values := []time.Time{firstAt, secondAt}
	index := 0
	clock := func(context.Context) time.Time {
		mu.Lock()
		defer mu.Unlock()
		value := values[index]
		if index < len(values)-1 {
			index++
		}
		return value
	}
	store, err := OpenDurableInboxFileStore(t.Context(), path, clock)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	t.Cleanup(func() { _ = store.Close() })
	envelope := validInboxEnvelope(t)
	if _, err := store.Admit(t.Context(), envelope); err != nil {
		t.Fatalf("first admission: %v", err)
	}
	if _, err := store.Admit(t.Context(), envelope); !errors.Is(err, ErrStoreClock) {
		t.Fatalf("regression admission = %v, want %v", err, ErrStoreClock)
	}
	loaded, err := store.Load(t.Context(), envelope.Scope, envelope.EventID)
	if err != nil || loaded.DeliveryCount != 1 || !loaded.LastReceivedAt.Equal(firstAt) {
		t.Fatalf("receipt after regression = %#v, %v", loaded, err)
	}
}

func sequenceClock(values ...time.Time) StoreClock {
	var mu sync.Mutex
	index := 0
	return func(context.Context) time.Time {
		mu.Lock()
		defer mu.Unlock()
		value := values[index]
		if index < len(values)-1 {
			index++
		}
		return value
	}
}
