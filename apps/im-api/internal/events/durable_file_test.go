package events

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"
)

func TestDurableFileStoreRoundTripsAcrossReopen(t *testing.T) {
	t.Parallel()

	path := filepath.Join(t.TempDir(), "events.log")
	recordedAt := time.Date(2026, 8, 29, 12, 30, 0, 123456789, time.UTC)
	clock := func(context.Context) time.Time { return recordedAt }
	store, err := OpenDurableFileStore(context.Background(), path, "file-reopen-v1", clock)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	batch := validBatch(t, 0, validEvent(t, "evt-file-1", "file-key-1"), validEvent(t, "evt-file-2", "file-key-2"))
	first, err := store.AppendBatch(context.Background(), batch)
	if err != nil || first.Replayed || len(first.Events) != 2 {
		t.Fatalf("first append = (%#v, %v)", first, err)
	}
	page, err := store.ReadStreamPage(context.Background(), StreamQuery{
		TenantID: "tenant-acme", WorkspaceID: stringPointer("workspace-acme"),
		StreamID: "task:task-1", Limit: 10,
	})
	if err != nil || len(page.Events) != 2 {
		t.Fatalf("first page = (%#v, %v)", page, err)
	}
	if page.HasMore {
		t.Fatalf("complete page unexpectedly has more events")
	}
	if err := store.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}

	reopened, err := OpenDurableFileStore(context.Background(), path, "file-reopen-v1", clock)
	if err != nil {
		t.Fatalf("reopen: %v", err)
	}
	t.Cleanup(func() { _ = reopened.Close() })
	replayed, err := reopened.AppendBatch(context.Background(), batch)
	if err != nil || !replayed.Replayed || len(replayed.Events) != 2 {
		t.Fatalf("replay after reopen = (%#v, %v)", replayed, err)
	}
	if replayed.Events[0].Sequence != 1 || replayed.Events[1].Sequence != 2 ||
		replayed.Events[0].GlobalPosition != 1 || replayed.Events[1].GlobalPosition != 2 {
		t.Fatalf("replayed positions = %#v", replayed.Events)
	}
	global, err := reopened.ReadGlobalPage(context.Background(), GlobalQuery{
		TenantID: "tenant-acme", WorkspaceID: stringPointer("workspace-acme"), Limit: 10,
	})
	if err != nil || len(global.Events) != 2 || global.Events[0].EventID != "evt-file-1" {
		t.Fatalf("global after reopen = (%#v, %v)", global, err)
	}
}

func TestDurableFileStoreUsesExactRetryAndRejectsDrift(t *testing.T) {
	t.Parallel()

	path := filepath.Join(t.TempDir(), "events.log")
	store, err := OpenDurableFileStore(
		context.Background(), path, "file-retry-v1", func(context.Context) time.Time { return contractTime },
	)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	t.Cleanup(func() { _ = store.Close() })
	batch := validBatch(t, 0, validEvent(t, "evt-file-retry", "file-retry-key"))
	if _, err := store.AppendBatch(context.Background(), batch); err != nil {
		t.Fatalf("append: %v", err)
	}
	drift := snapshotBatch(batch)
	drift.Events[0].ActorID = "actor-drift"
	if _, err := store.AppendBatch(context.Background(), drift); !errors.Is(err, ErrIdempotencyConflict) {
		t.Fatalf("drift error = %v, want %v", err, ErrIdempotencyConflict)
	}
	if _, err := store.AppendBatch(context.Background(), batch); err != nil {
		t.Fatalf("exact retry: %v", err)
	}
	if err := store.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}

	file, err := os.OpenFile(path, os.O_WRONLY|os.O_APPEND, 0)
	if err != nil {
		t.Fatalf("open tail: %v", err)
	}
	if _, err := file.WriteString(`{"format":"quantum-entanglement.events-file/1"}`); err != nil {
		t.Fatalf("write interrupted tail: %v", err)
	}
	if err := file.Close(); err != nil {
		t.Fatalf("close tail: %v", err)
	}
	reopened, err := OpenDurableFileStore(
		context.Background(), path, "file-retry-v1", func(context.Context) time.Time { return contractTime },
	)
	if err != nil {
		t.Fatalf("reopen after interrupted tail: %v", err)
	}
	t.Cleanup(func() { _ = reopened.Close() })
	page, err := reopened.ReadStreamPage(context.Background(), StreamQuery{
		TenantID: "tenant-acme", WorkspaceID: stringPointer("workspace-acme"),
		StreamID: "task:task-1", Limit: 10,
	})
	if err != nil || len(page.Events) != 1 {
		t.Fatalf("recovered page = (%#v, %v)", page, err)
	}
}

func TestDurableFileStoreRejectsCompleteCorruptionAndClosedUse(t *testing.T) {
	t.Parallel()

	path := filepath.Join(t.TempDir(), "events.log")
	if _, err := OpenDurableFileStore(
		context.Background(), path, "file-corrupt-v1", func(context.Context) time.Time { return contractTime },
	); err != nil {
		t.Fatalf("open empty: %v", err)
	}
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_APPEND, 0)
	if err != nil {
		t.Fatalf("open corrupt log: %v", err)
	}
	_, _ = file.WriteString("not-json\n")
	_ = file.Close()
	if _, err := OpenDurableFileStore(
		context.Background(), path, "file-corrupt-v1", func(context.Context) time.Time { return contractTime },
	); !errors.Is(err, ErrDurableFileLog) {
		t.Fatalf("complete corruption error = %v, want %v", err, ErrDurableFileLog)
	}

	closedPath := filepath.Join(t.TempDir(), "closed.log")
	store, err := OpenDurableFileStore(
		context.Background(), closedPath, "file-closed-v1", func(context.Context) time.Time { return contractTime },
	)
	if err != nil {
		t.Fatalf("open closed fixture: %v", err)
	}
	if err := store.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}
	if _, err := store.AppendBatch(context.Background(), validBatch(t, 0, validEvent(t, "evt-closed", "key-closed"))); !errors.Is(err, ErrDurableFileClosed) {
		t.Fatalf("closed append error = %v, want %v", err, ErrDurableFileClosed)
	}
	if _, err := store.ReadGlobalPage(context.Background(), GlobalQuery{TenantID: "tenant-acme", Limit: 1}); !errors.Is(err, ErrDurableFileClosed) {
		t.Fatalf("closed read error = %v, want %v", err, ErrDurableFileClosed)
	}
}

func TestDurableFileStoreConcurrentExactRetryHasOneCommittedRecord(t *testing.T) {
	t.Parallel()

	path := filepath.Join(t.TempDir(), "events.log")
	store, err := OpenDurableFileStore(
		context.Background(), path, "file-concurrency-v1", func(context.Context) time.Time { return contractTime },
	)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	t.Cleanup(func() { _ = store.Close() })
	batch := validBatch(t, 0, validEvent(t, "evt-file-concurrent", "file-concurrent-key"))
	const attempts = 12
	results := make(chan AppendResult, attempts)
	errorsCh := make(chan error, attempts)
	var group sync.WaitGroup
	for range attempts {
		group.Add(1)
		go func() {
			defer group.Done()
			result, appendErr := store.AppendBatch(context.Background(), batch)
			results <- result
			errorsCh <- appendErr
		}()
	}
	group.Wait()
	close(results)
	close(errorsCh)
	fresh, replayed := 0, 0
	for err := range errorsCh {
		if err != nil {
			t.Fatalf("concurrent append error = %v", err)
		}
	}
	for result := range results {
		if result.Replayed {
			replayed++
		} else {
			fresh++
		}
	}
	if fresh != 1 || replayed != attempts-1 {
		t.Fatalf("fresh=%d replayed=%d, want 1/%d", fresh, replayed, attempts-1)
	}
	page, err := store.ReadStreamPage(context.Background(), StreamQuery{
		TenantID: "tenant-acme", WorkspaceID: stringPointer("workspace-acme"),
		StreamID: "task:task-1", Limit: 10,
	})
	if err != nil || len(page.Events) != 1 {
		t.Fatalf("concurrent page = (%#v, %v)", page, err)
	}
}

func TestDurableFileStoreRequirementsExposeTamperEvidenceBoundary(t *testing.T) {
	t.Parallel()

	path := filepath.Join(t.TempDir(), "requirements.log")
	store, err := OpenDurableFileStore(
		context.Background(), path, "file-requirements-v1", func(context.Context) time.Time { return contractTime },
	)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	t.Cleanup(func() { _ = store.Close() })
	if err := ValidateStoreRequirements(store, StoreRequirements{
		Durability: StoreDurabilityDurable, PersistsAcrossRestart: true,
	}); err != nil {
		t.Fatalf("durable non-tamper requirement: %v", err)
	}
	if err := ValidateStoreRequirements(store, StoreRequirements{
		Durability: StoreDurabilityDurable, PersistsAcrossRestart: true, TamperEvident: true,
	}); !errors.Is(err, ErrStoreRequirements) {
		t.Fatalf("tamper-evident requirement = %v, want %v", err, ErrStoreRequirements)
	}
}
