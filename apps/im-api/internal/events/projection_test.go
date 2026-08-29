package events

import (
	"context"
	"errors"
	"reflect"
	"testing"
)

type memoryProjectionCheckpointStore struct {
	checkpoint ProjectionCheckpoint
	loads      int
	commits    int
	conflict   bool
}

func (store *memoryProjectionCheckpointStore) LoadProjectionCheckpoint(
	_ context.Context,
	scope ProjectionScope,
) (ProjectionCheckpoint, error) {
	store.loads++
	if store.checkpoint.Scope.ProjectionID == "" {
		return zeroProjectionCheckpoint(scope), nil
	}
	return cloneProjectionCheckpoint(store.checkpoint), nil
}

func (store *memoryProjectionCheckpointStore) CommitProjectionCheckpoint(
	_ context.Context,
	previous ProjectionCheckpoint,
	next ProjectionCheckpoint,
) error {
	store.commits++
	if store.conflict {
		return ErrProjectionCheckpointConflict
	}
	current := store.checkpoint
	if current.Scope.ProjectionID == "" {
		current = zeroProjectionCheckpoint(previous.Scope)
	}
	if !reflect.DeepEqual(current, previous) {
		return ErrProjectionCheckpointConflict
	}
	store.checkpoint = cloneProjectionCheckpoint(next)
	return nil
}

func TestProjectorBackfillsAndCommitsAtPageBoundaries(t *testing.T) {
	t.Parallel()

	store := newVolatileStore(t, contractTime)
	workspace := stringPointer("workspace-acme")
	appendScopeBatch(t, store, 0,
		eventForScope(t, "evt-p1", "key-p1", "tenant-acme", workspace, "task:a"),
		eventForScope(t, "evt-p2", "key-p2", "tenant-acme", workspace, "task:a"),
	)
	appendScopeBatch(t, store, 0,
		eventForScope(t, "evt-p3", "key-p3", "tenant-acme", workspace, "task:b"),
	)
	checkpoints := &memoryProjectionCheckpointStore{}
	scope := ProjectionScope{TenantID: "tenant-acme", WorkspaceID: workspace, ProjectionID: "tasks-v1"}
	var applied []string
	projector, err := NewProjector(store, checkpoints, func(_ context.Context, event StoredEvent) error {
		applied = append(applied, event.EventID)
		return nil
	}, 1)
	if err != nil {
		t.Fatalf("new projector: %v", err)
	}

	result, err := projector.Run(context.Background(), scope)
	if err != nil {
		t.Fatalf("run projector: %v", err)
	}
	if !reflect.DeepEqual(applied, []string{"evt-p1", "evt-p2", "evt-p3"}) {
		t.Fatalf("applied IDs = %#v", applied)
	}
	if result.Processed != 3 || result.Checkpoint.Position != 3 ||
		result.Checkpoint.LastEventID != "evt-p3" || result.Checkpoint.Cursor == "" {
		t.Fatalf("result = %#v", result)
	}
	if checkpoints.commits != 3 {
		t.Fatalf("checkpoint commits = %d, want 3", checkpoints.commits)
	}

	second, err := projector.Run(context.Background(), scope)
	if err != nil {
		t.Fatalf("rerun caught-up projector: %v", err)
	}
	if second.Processed != 0 || len(applied) != 3 {
		t.Fatalf("caught-up rerun = %#v, applied=%#v", second, applied)
	}
}

func TestProjectorApplyFailureDoesNotAdvanceCheckpointAndReplayIsAtLeastOnce(t *testing.T) {
	t.Parallel()

	store := newVolatileStore(t, contractTime)
	workspace := stringPointer("workspace-acme")
	appendScopeBatch(t, store, 0,
		eventForScope(t, "evt-r1", "key-r1", "tenant-acme", workspace, "task:a"),
		eventForScope(t, "evt-r2", "key-r2", "tenant-acme", workspace, "task:a"),
	)
	checkpoints := &memoryProjectionCheckpointStore{}
	scope := ProjectionScope{TenantID: "tenant-acme", WorkspaceID: workspace, ProjectionID: "tasks-v1"}
	var applied []string
	fail := true
	projector, err := NewProjector(store, checkpoints, func(_ context.Context, event StoredEvent) error {
		applied = append(applied, event.EventID)
		if fail && event.EventID == "evt-r2" {
			return errors.New("projection handler unavailable")
		}
		return nil
	}, 2)
	if err != nil {
		t.Fatalf("new projector: %v", err)
	}
	if _, err := projector.Run(context.Background(), scope); err == nil || err.Error() != "projection handler unavailable" {
		t.Fatalf("failed run error = %v", err)
	}
	if checkpoints.commits != 0 || checkpoints.checkpoint.Scope.ProjectionID != "" {
		t.Fatalf("checkpoint advanced after failed apply: %#v", checkpoints.checkpoint)
	}

	fail = false
	result, err := projector.Run(context.Background(), scope)
	if err != nil {
		t.Fatalf("replay run: %v", err)
	}
	if result.Processed != 2 || result.Checkpoint.Position != 2 {
		t.Fatalf("replay result = %#v", result)
	}
	if !reflect.DeepEqual(applied, []string{"evt-r1", "evt-r2", "evt-r1", "evt-r2"}) {
		t.Fatalf("at-least-once applied IDs = %#v", applied)
	}
}

func TestProjectorMapsCheckpointConflictAndUnavailableErrors(t *testing.T) {
	t.Parallel()

	store := newVolatileStore(t, contractTime)
	workspace := stringPointer("workspace-acme")
	appendScopeBatch(t, store, 0, eventForScope(t, "evt-c1", "key-c1", "tenant-acme", workspace, "task:a"))
	scope := ProjectionScope{TenantID: "tenant-acme", WorkspaceID: workspace, ProjectionID: "tasks-v1"}

	conflicting := &memoryProjectionCheckpointStore{conflict: true}
	projector, err := NewProjector(store, conflicting, func(context.Context, StoredEvent) error { return nil }, 1)
	if err != nil {
		t.Fatalf("new conflicting projector: %v", err)
	}
	if _, err := projector.Run(context.Background(), scope); !errors.Is(err, ErrProjectionCheckpointConflict) {
		t.Fatalf("conflict error = %v, want %v", err, ErrProjectionCheckpointConflict)
	}

	unavailable := &failingProjectionCheckpointStore{err: errors.New("database offline")}
	projector, err = NewProjector(store, unavailable, func(context.Context, StoredEvent) error { return nil }, 1)
	if err != nil {
		t.Fatalf("new unavailable projector: %v", err)
	}
	if _, err := projector.Run(context.Background(), scope); !errors.Is(err, ErrProjectionStoreUnavailable) {
		t.Fatalf("unavailable error = %v, want %v", err, ErrProjectionStoreUnavailable)
	}
}

type failingProjectionCheckpointStore struct{ err error }

func (store *failingProjectionCheckpointStore) LoadProjectionCheckpoint(context.Context, ProjectionScope) (ProjectionCheckpoint, error) {
	return ProjectionCheckpoint{}, store.err
}

func (store *failingProjectionCheckpointStore) CommitProjectionCheckpoint(context.Context, ProjectionCheckpoint, ProjectionCheckpoint) error {
	return store.err
}

func TestProjectorRejectsInvalidCheckpointBeforeReadingEvents(t *testing.T) {
	t.Parallel()

	store := newVolatileStore(t, contractTime)
	workspace := stringPointer("workspace-acme")
	appendScopeBatch(t, store, 0, eventForScope(t, "evt-i1", "key-i1", "tenant-acme", workspace, "task:a"))
	scope := ProjectionScope{TenantID: "tenant-acme", WorkspaceID: workspace, ProjectionID: "tasks-v1"}

	page, err := store.ReadGlobalPage(context.Background(), GlobalQuery{
		TenantID: "tenant-acme", WorkspaceID: workspace, Limit: 1,
	})
	if err != nil {
		t.Fatalf("read fixture page: %v", err)
	}
	validCursor := page.Next
	tests := []ProjectionCheckpoint{
		{Scope: ProjectionScope{TenantID: "tenant-other", WorkspaceID: workspace, ProjectionID: "tasks-v1"}, Position: 1, Cursor: validCursor, LastEventID: "evt-i1"},
		{Scope: scope, Position: 0, Cursor: validCursor},
		{Scope: scope, Position: 1, Cursor: "", LastEventID: "evt-i1"},
		{Scope: scope, Position: 1, Cursor: validCursor},
	}
	for index, checkpoint := range tests {
		checkpoint := checkpoint
		t.Run(string(rune('a'+index)), func(t *testing.T) {
			checkpoints := &memoryProjectionCheckpointStore{checkpoint: checkpoint}
			projector, err := NewProjector(store, checkpoints, func(context.Context, StoredEvent) error {
				t.Fatal("apply called for invalid checkpoint")
				return nil
			}, 1)
			if err != nil {
				t.Fatalf("new projector: %v", err)
			}
			if _, err := projector.Run(context.Background(), scope); !errors.Is(err, ErrProjectionInvalidCheckpoint) {
				t.Fatalf("error = %v, want %v", err, ErrProjectionInvalidCheckpoint)
			}
		})
	}
}

func TestProjectorHonorsCancellationAndScopeValidation(t *testing.T) {
	t.Parallel()

	store := newVolatileStore(t, contractTime)
	checkpoints := &memoryProjectionCheckpointStore{}
	projector, err := NewProjector(store, checkpoints, func(context.Context, StoredEvent) error { return nil }, 0)
	if err != nil {
		t.Fatalf("new projector: %v", err)
	}
	cancelled, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := projector.Run(cancelled, ProjectionScope{TenantID: "tenant-acme", ProjectionID: "tasks-v1"}); !errors.Is(err, context.Canceled) {
		t.Fatalf("cancelled run error = %v, want %v", err, context.Canceled)
	}
	if _, err := projector.Run(context.Background(), ProjectionScope{TenantID: "", ProjectionID: "tasks-v1"}); !errors.Is(err, ErrInvalidStore) {
		t.Fatalf("invalid scope error = %v, want %v", err, ErrInvalidStore)
	}
	if _, err := NewProjector(store, checkpoints, func(context.Context, StoredEvent) error { return nil }, maxPageEvents+1); !errors.Is(err, ErrInvalidStore) {
		t.Fatalf("invalid page size error = %v, want %v", err, ErrInvalidStore)
	}
}
