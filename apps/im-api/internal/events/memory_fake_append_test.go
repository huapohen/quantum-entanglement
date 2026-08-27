package events

import (
	"context"
	"errors"
	"reflect"
	"sync"
	"testing"
	"time"
)

type scriptedStoreClock struct {
	mu     sync.Mutex
	values []time.Time
	calls  int
}

func (clock *scriptedStoreClock) Now() time.Time {
	clock.mu.Lock()
	defer clock.mu.Unlock()
	clock.calls++
	if len(clock.values) == 0 {
		return time.Time{}
	}
	value := clock.values[0]
	if len(clock.values) > 1 {
		clock.values = clock.values[1:]
	}
	return value
}

func (clock *scriptedStoreClock) Calls() int {
	clock.mu.Lock()
	defer clock.mu.Unlock()
	return clock.calls
}

func TestVolatileMemoryStoreDeclaresItsNonProductionBoundaries(t *testing.T) {
	t.Parallel()

	if _, err := NewVolatileMemoryStore(nil); !errors.Is(err, ErrStoreClock) {
		t.Fatalf("nil clock error = %v, want %v", err, ErrStoreClock)
	}
	store := newVolatileStore(t, contractTime)
	got := store.Characteristics()
	want := StoreCharacteristics{
		Durability:                       StoreDurabilityVolatile,
		DeterministicGivenInputsAndClock: true,
		PersistsAcrossRestart:            false,
		TamperEvident:                    false,
		ProvidesActionReceipts:           false,
	}
	if got != want {
		t.Fatalf("characteristics = %#v, want %#v", got, want)
	}
}

func TestVolatileMemoryStoreAppendOwnsFactsAndExactRetry(t *testing.T) {
	t.Parallel()

	recordedAt := contractTime.Add(5 * time.Minute)
	clock := &scriptedStoreClock{values: []time.Time{recordedAt}}
	store, err := NewVolatileMemoryStore(clock.Now)
	if err != nil {
		t.Fatalf("new store: %v", err)
	}
	batch := validBatch(t, 0, validEvent(t, "evt-1", "key-1"), validEvent(t, "evt-2", "key-2"))
	original := snapshotBatch(batch)

	first, err := store.AppendBatch(context.Background(), batch)
	if err != nil {
		t.Fatalf("append: %v", err)
	}
	if first.Replayed || len(first.Events) != 2 {
		t.Fatalf("first result = %#v", first)
	}
	for index, event := range first.Events {
		if event.Sequence != uint64(index+1) || event.GlobalPosition != uint64(index+1) {
			t.Fatalf("event %d positions = sequence %d global %d", index, event.Sequence, event.GlobalPosition)
		}
		if got := event.RecordedAt; !got.Equal(recordedAt) || got.Location() != time.UTC {
			t.Fatalf("event %d recordedAt = %s (%s)", index, got, got.Location())
		}
	}

	batch.Events[0].EventID = "caller-mutated-input"
	*batch.Events[0].WorkspaceID = "caller-mutated-workspace"
	first.Events[0].EventID = "caller-mutated-output"
	*first.Events[0].WorkspaceID = "caller-mutated-output-workspace"
	first.Events[0].Payload.inline[0] ^= 0xff

	replayed, err := store.AppendBatch(context.Background(), original)
	if err != nil {
		t.Fatalf("exact replay: %v", err)
	}
	if !replayed.Replayed || clock.Calls() != 1 {
		t.Fatalf("replay = %#v, clock calls = %d", replayed, clock.Calls())
	}
	if replayed.Events[0].EventID != "evt-1" || *replayed.Events[0].WorkspaceID != "workspace-acme" {
		t.Fatalf("stored event was mutated through caller data: %#v", replayed.Events[0])
	}
	wantPayload := []byte(`{"value":1}`)
	if got := replayed.Events[0].Payload.InlineJSON(); !reflect.DeepEqual(got, wantPayload) {
		t.Fatalf("stored payload = %s, want %s", got, wantPayload)
	}
	if !reflect.DeepEqual(replayed.Events, storeSnapshotEvents(firstExpectedEvents(original, recordedAt))) {
		t.Fatalf("replayed events drifted: %#v", replayed.Events)
	}
}

func TestVolatileMemoryStoreRejectsRetryDriftAndLeavesNoPartialWrite(t *testing.T) {
	t.Parallel()

	clock := &scriptedStoreClock{values: []time.Time{contractTime, contractTime.Add(time.Second)}}
	store, err := NewVolatileMemoryStore(clock.Now)
	if err != nil {
		t.Fatalf("new store: %v", err)
	}
	initial := validBatch(t, 0, validEvent(t, "evt-1", "key-1"), validEvent(t, "evt-2", "key-2"))
	if _, err := store.AppendBatch(context.Background(), initial); err != nil {
		t.Fatalf("initial append: %v", err)
	}

	testCases := []struct {
		name  string
		batch AppendBatch
	}{
		{
			name: "event content drift",
			batch: func() AppendBatch {
				batch := snapshotBatch(initial)
				batch.Events[0].ActorID = "actor-drift"
				return batch
			}(),
		},
		{
			name:  "idempotency key reused by another event",
			batch: validBatch(t, 2, validEvent(t, "evt-3", "key-1")),
		},
		{
			name:  "partial overlap",
			batch: validBatch(t, 2, validEvent(t, "evt-2", "key-2"), validEvent(t, "evt-3", "key-3")),
		},
		{
			name: "expected revision drift",
			batch: func() AppendBatch {
				batch := snapshotBatch(initial)
				batch.ExpectedVersion = 2
				return batch
			}(),
		},
	}
	for _, testCase := range testCases {
		t.Run(testCase.name, func(t *testing.T) {
			if _, err := store.AppendBatch(context.Background(), testCase.batch); !errors.Is(err, ErrIdempotencyConflict) {
				t.Fatalf("error = %v, want %v", err, ErrIdempotencyConflict)
			}
		})
	}
	if clock.Calls() != 1 {
		t.Fatalf("conflicts consumed clock or wrote state; calls = %d", clock.Calls())
	}

	third := validBatch(t, 2, validEvent(t, "evt-3", "key-3"))
	result, err := store.AppendBatch(context.Background(), third)
	if err != nil {
		t.Fatalf("append after conflicts: %v", err)
	}
	if result.Events[0].Sequence != 3 || result.Events[0].GlobalPosition != 3 {
		t.Fatalf("partial conflict advanced positions: %#v", result.Events[0])
	}
}

func TestVolatileMemoryStoreRevisionContextAndClockFailuresWriteNothing(t *testing.T) {
	t.Parallel()

	clock := &scriptedStoreClock{values: []time.Time{
		contractTime,
		time.Time{},
		contractTime.Add(-time.Second),
		contractTime.Add(time.Second),
	}}
	store, err := NewVolatileMemoryStore(clock.Now)
	if err != nil {
		t.Fatalf("new store: %v", err)
	}
	if _, err := store.AppendBatch(context.Background(), validBatch(t, 0, validEvent(t, "evt-1", "key-1"))); err != nil {
		t.Fatalf("initial append: %v", err)
	}

	stale := validBatch(t, 0, validEvent(t, "evt-stale", "key-stale"))
	if _, err := store.AppendBatch(context.Background(), stale); !errors.Is(err, ErrRevisionConflict) {
		t.Fatalf("stale append error = %v, want %v", err, ErrRevisionConflict)
	}
	cancelled, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := store.AppendBatch(cancelled, validBatch(t, 1, validEvent(t, "evt-cancel", "key-cancel"))); !errors.Is(err, context.Canceled) {
		t.Fatalf("cancelled append error = %v, want %v", err, context.Canceled)
	}
	if clock.Calls() != 1 {
		t.Fatalf("rejected requests consumed clock; calls = %d", clock.Calls())
	}

	if _, err := store.AppendBatch(context.Background(), validBatch(t, 1, validEvent(t, "evt-zero", "key-zero"))); !errors.Is(err, ErrStoreClock) {
		t.Fatalf("zero clock error = %v, want %v", err, ErrStoreClock)
	}
	if _, err := store.AppendBatch(context.Background(), validBatch(t, 1, validEvent(t, "evt-back", "key-back"))); !errors.Is(err, ErrStoreClock) {
		t.Fatalf("backward clock error = %v, want %v", err, ErrStoreClock)
	}
	result, err := store.AppendBatch(context.Background(), validBatch(t, 1, validEvent(t, "evt-2", "key-2")))
	if err != nil {
		t.Fatalf("append after clock failures: %v", err)
	}
	if result.Events[0].Sequence != 2 || result.Events[0].GlobalPosition != 2 {
		t.Fatalf("clock failure advanced store: %#v", result.Events[0])
	}
}

func TestVolatileMemoryStoreContainsClockPanic(t *testing.T) {
	t.Parallel()

	store, err := NewVolatileMemoryStore(func() time.Time { panic("clock secret must not escape") })
	if err != nil {
		t.Fatalf("new store: %v", err)
	}
	if _, err := store.AppendBatch(context.Background(), validBatch(t, 0, validEvent(t, "evt-1", "key-1"))); !errors.Is(err, ErrStoreClock) {
		t.Fatalf("panic clock error = %v, want %v", err, ErrStoreClock)
	}
	page, err := store.ReadStreamPage(context.Background(), StreamQuery{
		TenantID: "tenant-acme", WorkspaceID: stringPointer("workspace-acme"),
		StreamID: "task:task-1", Limit: 1,
	})
	if err != nil {
		t.Fatalf("read after panic: %v", err)
	}
	if len(page.Events) != 0 {
		t.Fatalf("panic clock wrote events: %#v", page.Events)
	}
}

func validBatch(t *testing.T, expectedVersion uint64, events ...EventToAppend) AppendBatch {
	t.Helper()
	if len(events) == 0 {
		t.Fatal("validBatch requires events")
	}
	return AppendBatch{
		TenantID: events[0].TenantID, WorkspaceID: cloneStringPointer(events[0].WorkspaceID),
		StreamID: events[0].StreamID, ExpectedVersion: expectedVersion, Events: events,
	}
}

func snapshotBatch(batch AppendBatch) AppendBatch {
	result := AppendBatch{
		TenantID: batch.TenantID, WorkspaceID: cloneStringPointer(batch.WorkspaceID),
		StreamID: batch.StreamID, ExpectedVersion: batch.ExpectedVersion,
		Events: make([]EventToAppend, 0, len(batch.Events)),
	}
	for _, event := range batch.Events {
		result.Events = append(result.Events, snapshotEvent(event))
	}
	return result
}

func firstExpectedEvents(batch AppendBatch, recordedAt time.Time) []StoredEvent {
	events := make([]StoredEvent, 0, len(batch.Events))
	for index, event := range batch.Events {
		events = append(events, StoredEvent{
			EventToAppend: snapshotEvent(event), Sequence: uint64(index + 1),
			GlobalPosition: uint64(index + 1), RecordedAt: normalizeEventTime(recordedAt),
		})
	}
	return events
}

func storeSnapshotEvents(events []StoredEvent) []StoredEvent {
	return cloneStoredEvents(events)
}

func newVolatileStore(t *testing.T, values ...time.Time) *VolatileMemoryStore {
	t.Helper()
	clock := &scriptedStoreClock{values: values}
	store, err := NewVolatileMemoryStore(clock.Now)
	if err != nil {
		t.Fatalf("new volatile store: %v", err)
	}
	return store
}

func stringPointer(value string) *string {
	return &value
}
