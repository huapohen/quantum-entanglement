package events

import (
	"context"
	"errors"
	"reflect"
	"testing"
)

type fixtureProjection struct {
	EventIDs      []string
	StreamVersion map[string]uint64
	LastPosition  uint64
}

func TestVolatileMemoryStoreBackfillRebuildsFixtureProjectionDeterministically(t *testing.T) {
	t.Parallel()

	store := newVolatileStore(t, contractTime)
	workspace := stringPointer("workspace-acme")
	appendScopeBatch(t, store, 0,
		eventForScope(t, "evt-a1", "key-a1", "tenant-acme", workspace, "task:a"),
		eventForScope(t, "evt-a2", "key-a2", "tenant-acme", workspace, "task:a"),
	)
	appendScopeBatch(t, store, 0,
		eventForScope(t, "evt-b1", "key-b1", "tenant-acme", workspace, "task:b"),
	)
	appendScopeBatch(t, store, 2,
		eventForScope(t, "evt-a3", "key-a3", "tenant-acme", workspace, "task:a"),
	)

	withSingleEventPages, err := rebuildFixtureProjection(
		context.Background(), store, "tenant-acme", workspace, 1,
	)
	if err != nil {
		t.Fatalf("single-event rebuild: %v", err)
	}
	withWidePages, err := rebuildFixtureProjection(
		context.Background(), store, "tenant-acme", workspace, maxPageEvents,
	)
	if err != nil {
		t.Fatalf("wide-page rebuild: %v", err)
	}
	want := fixtureProjection{
		EventIDs:      []string{"evt-a1", "evt-a2", "evt-b1", "evt-a3"},
		StreamVersion: map[string]uint64{"task:a": 3, "task:b": 1},
		LastPosition:  4,
	}
	if !reflect.DeepEqual(withSingleEventPages, want) || !reflect.DeepEqual(withWidePages, want) {
		t.Fatalf("rebuild mismatch:\nsingle=%#v\nwide=%#v\nwant=%#v", withSingleEventPages, withWidePages, want)
	}
}

func TestVolatileMemoryStorePreservesUnsupportedProjectionSourceEvent(t *testing.T) {
	t.Parallel()

	store := newVolatileStore(t, contractTime)
	workspace := stringPointer("workspace-acme")
	known := eventForScope(t, "evt-known", "key-known", "tenant-acme", workspace, "task:a")
	unknown := eventForScope(t, "evt-future", "key-future", "tenant-acme", workspace, "task:a")
	unknown.SchemaVersion = 99
	unknown.EventType = "future.event.v99"
	appendScopeBatch(t, store, 0, known, unknown)

	if _, err := rebuildFixtureProjection(
		context.Background(), store, "tenant-acme", workspace, 1,
	); !errors.Is(err, ErrProjectionUnsupported) {
		t.Fatalf("projection error = %v, want %v", err, ErrProjectionUnsupported)
	}
	page, err := store.ReadStreamPage(context.Background(), StreamQuery{
		TenantID: "tenant-acme", WorkspaceID: workspace, StreamID: "task:a", Limit: maxPageEvents,
	})
	if err != nil {
		t.Fatalf("read preserved source: %v", err)
	}
	if len(page.Events) != 2 || page.Events[1].SchemaVersion != 99 ||
		page.Events[1].EventType != "future.event.v99" || page.Events[1].EventID != "evt-future" {
		t.Fatalf("unsupported source event was dropped or rewritten: %#v", page.Events)
	}
}

func rebuildFixtureProjection(
	ctx context.Context,
	store EventStore,
	tenantID string,
	workspaceID *string,
	limit uint32,
) (fixtureProjection, error) {
	projection := fixtureProjection{StreamVersion: make(map[string]uint64)}
	query := GlobalQuery{
		TenantID: tenantID, WorkspaceID: cloneStringPointer(workspaceID), Limit: limit,
	}
	for {
		page, err := store.ReadGlobalPage(ctx, query)
		if err != nil {
			return fixtureProjection{}, err
		}
		for _, event := range page.Events {
			if event.SchemaVersion != 1 {
				return fixtureProjection{}, ErrProjectionUnsupported
			}
			expected := projection.StreamVersion[event.StreamID] + 1
			if event.Sequence != expected || event.GlobalPosition <= projection.LastPosition {
				return fixtureProjection{}, ErrRevisionConflict
			}
			projection.EventIDs = append(projection.EventIDs, event.EventID)
			projection.StreamVersion[event.StreamID] = event.Sequence
			projection.LastPosition = event.GlobalPosition
		}
		if !page.HasMore {
			return projection, nil
		}
		query.After = page.Next
	}
}
