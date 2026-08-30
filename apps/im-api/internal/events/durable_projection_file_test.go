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

func TestDurableProjectionCheckpointFileStoreRoundTripsAndCAS(t *testing.T) {
	t.Parallel()

	path := filepath.Join(t.TempDir(), "projection.log")
	store, err := OpenDurableProjectionCheckpointFileStore(t.Context(), path)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	scope := testProjectionScope()
	zero, err := store.LoadProjectionCheckpoint(t.Context(), scope)
	if err != nil || !isZeroProjectionCheckpoint(zero, scope) {
		t.Fatalf("initial checkpoint = %#v, %v", zero, err)
	}
	first := testProjectionCheckpoint(scope, 1, "cursor-1", "evt-1")
	if err := store.CommitProjectionCheckpoint(t.Context(), zero, first); err != nil {
		t.Fatalf("first commit: %v", err)
	}
	loaded, err := store.LoadProjectionCheckpoint(t.Context(), scope)
	if err != nil || !sameProjectionCheckpoint(loaded, first) {
		t.Fatalf("loaded first checkpoint = %#v, %v", loaded, err)
	}
	if err := store.CommitProjectionCheckpoint(t.Context(), zero, first); !errors.Is(err, ErrProjectionCheckpointConflict) {
		t.Fatalf("stale commit = %v, want %v", err, ErrProjectionCheckpointConflict)
	}
	second := testProjectionCheckpoint(scope, 2, "cursor-2", "evt-2")
	if err := store.CommitProjectionCheckpoint(t.Context(), first, second); err != nil {
		t.Fatalf("second commit: %v", err)
	}
	if err := store.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read log: %v", err)
	}
	if strings.Count(string(raw), "\n") != 2 || strings.Contains(string(raw), `"TenantID"`) || !strings.Contains(string(raw), `"tenantId"`) {
		t.Fatalf("unexpected checkpoint log = %s", raw)
	}
	reopened, err := OpenDurableProjectionCheckpointFileStore(t.Context(), path)
	if err != nil {
		t.Fatalf("reopen: %v", err)
	}
	t.Cleanup(func() { _ = reopened.Close() })
	loaded, err = reopened.LoadProjectionCheckpoint(t.Context(), scope)
	if err != nil || !sameProjectionCheckpoint(loaded, second) {
		t.Fatalf("loaded after reopen = %#v, %v", loaded, err)
	}
}

func TestDurableProjectionCheckpointFileStoreConcurrentCASHasOneWinner(t *testing.T) {
	t.Parallel()

	store, err := OpenDurableProjectionCheckpointFileStore(t.Context(), filepath.Join(t.TempDir(), "concurrent.log"))
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	t.Cleanup(func() { _ = store.Close() })
	scope := testProjectionScope()
	zero := zeroProjectionCheckpoint(scope)
	next := testProjectionCheckpoint(scope, 1, "cursor-1", "evt-1")
	const workers = 24
	results := make(chan error, workers)
	var wait sync.WaitGroup
	wait.Add(workers)
	for range workers {
		go func() {
			defer wait.Done()
			results <- store.CommitProjectionCheckpoint(context.Background(), zero, next)
		}()
	}
	wait.Wait()
	close(results)
	winners, conflicts := 0, 0
	for err := range results {
		switch {
		case err == nil:
			winners++
		case errors.Is(err, ErrProjectionCheckpointConflict):
			conflicts++
		default:
			t.Fatalf("concurrent commit error = %v", err)
		}
	}
	if winners != 1 || conflicts != workers-1 {
		t.Fatalf("winners=%d conflicts=%d, want 1/%d", winners, conflicts, workers-1)
	}
	loaded, err := store.LoadProjectionCheckpoint(t.Context(), scope)
	if err != nil || !sameProjectionCheckpoint(loaded, next) {
		t.Fatalf("winner checkpoint = %#v, %v", loaded, err)
	}
}

func TestDurableProjectionCheckpointFileStoreDiscardsTailAndRejectsCorruption(t *testing.T) {
	t.Parallel()

	directory := t.TempDir()
	path := filepath.Join(directory, "tail.log")
	store, err := OpenDurableProjectionCheckpointFileStore(t.Context(), path)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	scope := testProjectionScope()
	zero := zeroProjectionCheckpoint(scope)
	next := testProjectionCheckpoint(scope, 1, "cursor-1", "evt-1")
	if err := store.CommitProjectionCheckpoint(t.Context(), zero, next); err != nil {
		t.Fatalf("commit: %v", err)
	}
	if err := store.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		t.Fatalf("open tail: %v", err)
	}
	if _, err := file.WriteString(`{"format":"quantum-entanglement.event-projection-checkpoint/1"}`); err != nil {
		t.Fatalf("write tail: %v", err)
	}
	if err := file.Close(); err != nil {
		t.Fatalf("close tail: %v", err)
	}
	reopened, err := OpenDurableProjectionCheckpointFileStore(t.Context(), path)
	if err != nil {
		t.Fatalf("reopen tail: %v", err)
	}
	loaded, err := reopened.LoadProjectionCheckpoint(t.Context(), scope)
	if err != nil || !sameProjectionCheckpoint(loaded, next) {
		t.Fatalf("loaded after tail = %#v, %v", loaded, err)
	}
	if err := reopened.Close(); err != nil {
		t.Fatalf("close reopened: %v", err)
	}
	file, err = os.OpenFile(path, os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		t.Fatalf("open corruption: %v", err)
	}
	if _, err := file.WriteString("not-json\n"); err != nil {
		t.Fatalf("write corruption: %v", err)
	}
	if err := file.Close(); err != nil {
		t.Fatalf("close corruption: %v", err)
	}
	if _, err := OpenDurableProjectionCheckpointFileStore(t.Context(), path); !errors.Is(err, ErrDurableProjectionCheckpointFileLog) {
		t.Fatalf("corruption error = %v, want %v", err, ErrDurableProjectionCheckpointFileLog)
	}
}

func TestDurableProjectionCheckpointFileStoreIntegratesWithDurableEventReplay(t *testing.T) {
	t.Parallel()

	directory := t.TempDir()
	eventPath := filepath.Join(directory, "events.log")
	checkpointPath := filepath.Join(directory, "checkpoints.log")
	clock := func(context.Context) time.Time { return contractTime }
	eventStore, err := OpenDurableFileStore(t.Context(), eventPath, "durable-projection-v1", clock)
	if err != nil {
		t.Fatalf("open event store: %v", err)
	}
	scope := testProjectionScope()
	batch := validBatch(t, 0, eventForScope(t, "evt-projection", "key-projection", scope.TenantID, scope.WorkspaceID, "task:projection"))
	if _, err := eventStore.AppendBatch(t.Context(), batch); err != nil {
		t.Fatalf("append event: %v", err)
	}
	checkpointStore, err := OpenDurableProjectionCheckpointFileStore(t.Context(), checkpointPath)
	if err != nil {
		t.Fatalf("open checkpoint store: %v", err)
	}
	projector, err := NewProjector(eventStore, checkpointStore, func(context.Context, StoredEvent) error { return nil }, 1)
	if err != nil {
		t.Fatalf("new projector: %v", err)
	}
	result, err := projector.Run(t.Context(), scope)
	if err != nil || result.Processed != 1 || result.Checkpoint.Position != 1 {
		t.Fatalf("first projection = %#v, %v", result, err)
	}
	if err := checkpointStore.Close(); err != nil {
		t.Fatalf("close checkpoint: %v", err)
	}
	if err := eventStore.Close(); err != nil {
		t.Fatalf("close event: %v", err)
	}
	reopenedEvents, err := OpenDurableFileStore(t.Context(), eventPath, "durable-projection-v1", clock)
	if err != nil {
		t.Fatalf("reopen event: %v", err)
	}
	t.Cleanup(func() { _ = reopenedEvents.Close() })
	reopenedCheckpoints, err := OpenDurableProjectionCheckpointFileStore(t.Context(), checkpointPath)
	if err != nil {
		t.Fatalf("reopen checkpoint: %v", err)
	}
	t.Cleanup(func() { _ = reopenedCheckpoints.Close() })
	reopenedProjector, err := NewProjector(reopenedEvents, reopenedCheckpoints, func(context.Context, StoredEvent) error {
		t.Fatal("replayed an event after durable checkpoint")
		return nil
	}, 1)
	if err != nil {
		t.Fatalf("new reopened projector: %v", err)
	}
	result, err = reopenedProjector.Run(t.Context(), scope)
	if err != nil || result.Processed != 0 || result.Checkpoint.Position != 1 {
		t.Fatalf("reopened projection = %#v, %v", result, err)
	}
}

func TestDurableProjectionCheckpointFileStoreClosedAndInvalidContracts(t *testing.T) {
	t.Parallel()

	path := filepath.Join(t.TempDir(), "closed.log")
	store, err := OpenDurableProjectionCheckpointFileStore(t.Context(), path)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	scope := testProjectionScope()
	if err := store.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}
	if _, err := store.LoadProjectionCheckpoint(t.Context(), scope); !errors.Is(err, ErrDurableProjectionCheckpointFileClosed) {
		t.Fatalf("closed load = %v, want %v", err, ErrDurableProjectionCheckpointFileClosed)
	}
	if err := store.CommitProjectionCheckpoint(t.Context(), zeroProjectionCheckpoint(scope), testProjectionCheckpoint(scope, 1, "cursor-1", "evt-1")); !errors.Is(err, ErrDurableProjectionCheckpointFileClosed) {
		t.Fatalf("closed commit = %v, want %v", err, ErrDurableProjectionCheckpointFileClosed)
	}
	invalidScope := ProjectionScope{TenantID: "", ProjectionID: "tasks-v1"}
	if _, err := store.LoadProjectionCheckpoint(t.Context(), invalidScope); !errors.Is(err, ErrProjectionInvalidCheckpoint) {
		t.Fatalf("invalid scope = %v, want %v", err, ErrProjectionInvalidCheckpoint)
	}
	if _, err := OpenDurableProjectionCheckpointFileStore(t.Context(), "relative.log"); !errors.Is(err, ErrInvalidStore) {
		t.Fatalf("relative path = %v, want %v", err, ErrInvalidStore)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := OpenDurableProjectionCheckpointFileStore(ctx, filepath.Join(t.TempDir(), "cancel.log")); !errors.Is(err, context.Canceled) {
		t.Fatalf("canceled open = %v, want %v", err, context.Canceled)
	}
}

func testProjectionScope() ProjectionScope {
	return ProjectionScope{TenantID: "tenant-acme", WorkspaceID: stringPointer("workspace-acme"), ProjectionID: "tasks-v1"}
}

func testProjectionCheckpoint(scope ProjectionScope, position uint64, cursor Cursor, eventID string) ProjectionCheckpoint {
	return ProjectionCheckpoint{Scope: cloneProjectionScope(scope), Position: position, Cursor: cursor, LastEventID: eventID}
}
