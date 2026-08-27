package events

import (
	"context"
	"errors"
	"math"
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

	if _, err := NewVolatileMemoryStore("", func() time.Time { return contractTime }); !errors.Is(err, ErrInvalidStore) {
		t.Fatalf("empty instance error = %v, want %v", err, ErrInvalidStore)
	}
	if _, err := NewVolatileMemoryStore("test-instance", nil); !errors.Is(err, ErrStoreClock) {
		t.Fatalf("nil clock error = %v, want %v", err, ErrStoreClock)
	}
	store := newVolatileStore(t, contractTime)
	got := store.Characteristics()
	want := StoreCharacteristics{
		Durability:                               StoreDurabilityVolatile,
		DeterministicGivenInputsClockAndSchedule: true,
		PersistsAcrossRestart:                    false,
		TamperEvident:                            false,
		ProvidesActionReceipts:                   false,
	}
	if got != want {
		t.Fatalf("characteristics = %#v, want %#v", got, want)
	}
	if err := ValidateStoreRequirements(store, StoreRequirements{Durability: StoreDurabilityVolatile}); err != nil {
		t.Fatalf("volatile requirement: %v", err)
	}
	production := StoreRequirements{
		Durability: StoreDurabilityDurable, PersistsAcrossRestart: true,
		TamperEvident: true, ProvidesActionReceipts: true,
	}
	if err := ValidateStoreRequirements(store, production); !errors.Is(err, ErrStoreRequirements) {
		t.Fatalf("production requirement error = %v, want %v", err, ErrStoreRequirements)
	}
}

func TestVolatileMemoryStoreAppendOwnsFactsAndExactRetry(t *testing.T) {
	t.Parallel()

	recordedAt := contractTime.Add(5 * time.Minute)
	clock := &scriptedStoreClock{values: []time.Time{recordedAt}}
	store, err := NewVolatileMemoryStore("exact-retry", clock.Now)
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
	store, err := NewVolatileMemoryStore("retry-conflict", clock.Now)
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
	store, err := NewVolatileMemoryStore("failure-atomicity", clock.Now)
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

	store, err := NewVolatileMemoryStore("panic-clock", func() time.Time { panic("clock secret must not escape") })
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

func TestVolatileMemoryStoreRejectsRetrySubsetSupersetAndReorder(t *testing.T) {
	t.Parallel()

	clock := &scriptedStoreClock{values: []time.Time{contractTime}}
	store, err := NewVolatileMemoryStore("retry-shapes", clock.Now)
	if err != nil {
		t.Fatalf("new store: %v", err)
	}
	first := validEvent(t, "evt-1", "key-1")
	second := validEvent(t, "evt-2", "key-2")
	third := validEvent(t, "evt-3", "key-3")
	if _, err := store.AppendBatch(context.Background(), validBatch(t, 0, first, second)); err != nil {
		t.Fatalf("initial append: %v", err)
	}
	testCases := []struct {
		name  string
		batch AppendBatch
	}{
		{name: "subset", batch: validBatch(t, 0, snapshotEvent(first))},
		{name: "superset", batch: validBatch(t, 0, snapshotEvent(first), snapshotEvent(second), third)},
		{name: "reorder", batch: validBatch(t, 0, snapshotEvent(second), snapshotEvent(first))},
		{
			name: "event id with changed idempotency key",
			batch: func() AppendBatch {
				event := snapshotEvent(first)
				event.IdempotencyKey = stringPointer("key-drift")
				return validBatch(t, 0, event)
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
		t.Fatalf("retry shape conflict consumed clock: %d", clock.Calls())
	}
	page, err := store.ReadStreamPage(context.Background(), StreamQuery{
		TenantID: first.TenantID, WorkspaceID: cloneStringPointer(first.WorkspaceID),
		StreamID: first.StreamID, Limit: maxPageEvents,
	})
	if err != nil || !reflect.DeepEqual(eventIDs(page.Events), []string{"evt-1", "evt-2"}) {
		t.Fatalf("retry shape conflict changed store: %#v, err=%v", page, err)
	}
}

func TestVolatileMemoryStoreInvalidBatchAndCapacityAreAtomic(t *testing.T) {
	t.Parallel()

	clock := &scriptedStoreClock{values: []time.Time{contractTime}}
	store, err := NewVolatileMemoryStore("validation-atomicity", clock.Now)
	if err != nil {
		t.Fatalf("new store: %v", err)
	}
	first := validEvent(t, "evt-1", "key-1")
	invalid := validEvent(t, "evt-invalid", "key-invalid")
	invalid.EventType = "INVALID EVENT TYPE"
	if _, err := store.AppendBatch(context.Background(), validBatch(t, 0, first, invalid)); !errors.Is(err, ErrInvalidBatch) {
		t.Fatalf("invalid batch error = %v, want %v", err, ErrInvalidBatch)
	}
	if clock.Calls() != 0 {
		t.Fatalf("invalid batch consumed clock: %d", clock.Calls())
	}
	result, err := store.AppendBatch(context.Background(), validBatch(t, 0, first))
	if err != nil || result.Events[0].Sequence != 1 || result.Events[0].GlobalPosition != 1 {
		t.Fatalf("append after invalid batch = %#v, err=%v", result, err)
	}

	overflowClock := &scriptedStoreClock{values: []time.Time{contractTime}}
	overflow, err := NewVolatileMemoryStore("capacity-atomicity", overflowClock.Now)
	if err != nil {
		t.Fatalf("new overflow store: %v", err)
	}
	overflow.globalPosition = math.MaxUint64 - 1
	if _, err := overflow.AppendBatch(context.Background(), validBatch(
		t, 0, validEvent(t, "evt-a", "key-a"), validEvent(t, "evt-b", "key-b"),
	)); !errors.Is(err, ErrStoreCapacity) {
		t.Fatalf("overflow error = %v, want %v", err, ErrStoreCapacity)
	}
	if overflowClock.Calls() != 0 || len(overflow.global) != 0 || len(overflow.streams) != 0 ||
		len(overflow.retries) != 0 || !overflow.lastRecordedAt.IsZero() || overflow.globalPosition != math.MaxUint64-1 {
		t.Fatalf("overflow mutated store: %#v", overflow)
	}
}

func TestVolatileMemoryStoreAcceptsEqualClockButRejectsOutOfRangeYear(t *testing.T) {
	t.Parallel()

	clock := &scriptedStoreClock{values: []time.Time{
		contractTime,
		contractTime,
		time.Date(10000, time.January, 1, 0, 0, 0, 0, time.UTC),
	}}
	store, err := NewVolatileMemoryStore("clock-boundaries", clock.Now)
	if err != nil {
		t.Fatalf("new store: %v", err)
	}
	if _, err := store.AppendBatch(context.Background(), validBatch(t, 0, validEvent(t, "evt-1", "key-1"))); err != nil {
		t.Fatalf("first append: %v", err)
	}
	second, err := store.AppendBatch(context.Background(), validBatch(t, 1, validEvent(t, "evt-2", "key-2")))
	if err != nil || !second.Events[0].RecordedAt.Equal(contractTime) {
		t.Fatalf("equal clock append = %#v, err=%v", second, err)
	}
	if _, err := store.AppendBatch(context.Background(), validBatch(t, 2, validEvent(t, "evt-3", "key-3"))); !errors.Is(err, ErrStoreClock) {
		t.Fatalf("out-of-range clock error = %v, want %v", err, ErrStoreClock)
	}
	page, err := store.ReadStreamPage(context.Background(), StreamQuery{
		TenantID: "tenant-acme", WorkspaceID: stringPointer("workspace-acme"),
		StreamID: "task:task-1", Limit: maxPageEvents,
	})
	if err != nil || len(page.Events) != 2 {
		t.Fatalf("clock boundary page = %#v, err=%v", page, err)
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
	store, err := NewVolatileMemoryStore("test-instance", clock.Now)
	if err != nil {
		t.Fatalf("new volatile store: %v", err)
	}
	return store
}

func stringPointer(value string) *string {
	return &value
}
