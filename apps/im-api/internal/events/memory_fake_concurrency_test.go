package events

import (
	"context"
	"errors"
	"reflect"
	"sort"
	"strconv"
	"sync"
	"testing"
	"time"
)

func TestVolatileMemoryStoreConcurrentExpectedRevisionHasOneOwner(t *testing.T) {
	t.Parallel()

	const contenders = 64
	clock := &scriptedStoreClock{values: []time.Time{contractTime}}
	store, err := NewVolatileMemoryStore("concurrent-revision", clock.Now)
	if err != nil {
		t.Fatalf("new store: %v", err)
	}
	batches := make([]AppendBatch, 0, contenders)
	for index := 0; index < contenders; index++ {
		event := validEvent(t, eventIDForIndex("evt", index), eventIDForIndex("key", index))
		batches = append(batches, validBatch(t, 0, event))
	}

	start := make(chan struct{})
	results := make(chan error, contenders)
	var workers sync.WaitGroup
	workers.Add(contenders)
	for _, batch := range batches {
		batch := batch
		go func() {
			defer workers.Done()
			<-start
			_, appendErr := store.AppendBatch(context.Background(), batch)
			results <- appendErr
		}()
	}
	close(start)
	workers.Wait()
	close(results)

	successes := 0
	conflicts := 0
	for result := range results {
		switch {
		case result == nil:
			successes++
		case errors.Is(result, ErrRevisionConflict):
			conflicts++
		default:
			t.Fatalf("unexpected append error: %v", result)
		}
	}
	if successes != 1 || conflicts != contenders-1 || clock.Calls() != 1 {
		t.Fatalf("successes=%d conflicts=%d clockCalls=%d", successes, conflicts, clock.Calls())
	}
	page, err := store.ReadStreamPage(context.Background(), StreamQuery{
		TenantID: "tenant-acme", WorkspaceID: stringPointer("workspace-acme"),
		StreamID: "task:task-1", Limit: maxPageEvents,
	})
	if err != nil || len(page.Events) != 1 || page.Events[0].Sequence != 1 || page.Events[0].GlobalPosition != 1 {
		t.Fatalf("winner page = %#v, err=%v", page, err)
	}
}

func TestVolatileMemoryStoreConcurrentExactRetryStoresOnce(t *testing.T) {
	t.Parallel()

	const contenders = 64
	clock := &scriptedStoreClock{values: []time.Time{contractTime}}
	store, err := NewVolatileMemoryStore("concurrent-replay", clock.Now)
	if err != nil {
		t.Fatalf("new store: %v", err)
	}
	batch := validBatch(t, 0,
		validEvent(t, "evt-1", "key-1"),
		validEvent(t, "evt-2", "key-2"),
	)

	start := make(chan struct{})
	results := make(chan AppendResult, contenders)
	errorsFound := make(chan error, contenders)
	var workers sync.WaitGroup
	workers.Add(contenders)
	for index := 0; index < contenders; index++ {
		go func() {
			defer workers.Done()
			<-start
			result, appendErr := store.AppendBatch(context.Background(), batch)
			if appendErr != nil {
				errorsFound <- appendErr
				return
			}
			results <- result
		}()
	}
	close(start)
	workers.Wait()
	close(results)
	close(errorsFound)
	for appendErr := range errorsFound {
		t.Fatalf("exact retry error: %v", appendErr)
	}

	fresh := 0
	replayed := 0
	var facts []StoredEvent
	for result := range results {
		if facts == nil {
			facts = cloneStoredEvents(result.Events)
		} else if !reflect.DeepEqual(facts, result.Events) {
			t.Fatalf("concurrent retry facts drifted: first=%#v next=%#v", facts, result.Events)
		}
		if result.Replayed {
			replayed++
		} else {
			fresh++
		}
	}
	if fresh != 1 || replayed != contenders-1 || clock.Calls() != 1 {
		t.Fatalf("fresh=%d replayed=%d clockCalls=%d", fresh, replayed, clock.Calls())
	}
}

func TestVolatileMemoryStoreConcurrentIdentityDriftConflicts(t *testing.T) {
	t.Parallel()

	clock := &scriptedStoreClock{values: []time.Time{contractTime}}
	store, err := NewVolatileMemoryStore("concurrent-drift", clock.Now)
	if err != nil {
		t.Fatalf("new store: %v", err)
	}
	first := validBatch(t, 0, validEvent(t, "evt-1", "key-1"))
	second := snapshotBatch(first)
	second.Events[0].ActorID = "actor-drift"
	batches := []AppendBatch{first, second}

	start := make(chan struct{})
	results := make(chan error, len(batches))
	var workers sync.WaitGroup
	workers.Add(len(batches))
	for _, batch := range batches {
		batch := batch
		go func() {
			defer workers.Done()
			<-start
			_, appendErr := store.AppendBatch(context.Background(), batch)
			results <- appendErr
		}()
	}
	close(start)
	workers.Wait()
	close(results)

	successes := 0
	conflicts := 0
	for result := range results {
		if result == nil {
			successes++
		} else if errors.Is(result, ErrIdempotencyConflict) {
			conflicts++
		} else {
			t.Fatalf("unexpected append error: %v", result)
		}
	}
	if successes != 1 || conflicts != 1 || clock.Calls() != 1 {
		t.Fatalf("successes=%d conflicts=%d clockCalls=%d", successes, conflicts, clock.Calls())
	}
}

func TestVolatileMemoryStoreConcurrentStreamsShareOneGlobalOrder(t *testing.T) {
	t.Parallel()

	const streamCount = 128
	clock := &scriptedStoreClock{values: []time.Time{contractTime}}
	store, err := NewVolatileMemoryStore("concurrent-streams", clock.Now)
	if err != nil {
		t.Fatalf("new store: %v", err)
	}
	batches := make([]AppendBatch, 0, streamCount)
	workspace := stringPointer("workspace-acme")
	for index := 0; index < streamCount; index++ {
		event := eventForScope(
			t,
			eventIDForIndex("evt", index),
			eventIDForIndex("key", index),
			"tenant-acme",
			workspace,
			eventIDForIndex("task", index),
		)
		batches = append(batches, validBatch(t, 0, event))
	}

	start := make(chan struct{})
	errorsFound := make(chan error, streamCount)
	var workers sync.WaitGroup
	workers.Add(streamCount)
	for _, batch := range batches {
		batch := batch
		go func() {
			defer workers.Done()
			<-start
			result, appendErr := store.AppendBatch(context.Background(), batch)
			if appendErr == nil && (len(result.Events) != 1 || result.Events[0].Sequence != 1) {
				appendErr = errors.New("stream did not start at sequence one")
			}
			errorsFound <- appendErr
		}()
	}
	close(start)
	workers.Wait()
	close(errorsFound)
	for appendErr := range errorsFound {
		if appendErr != nil {
			t.Fatalf("concurrent stream append: %v", appendErr)
		}
	}

	page, err := store.ReadGlobalPage(context.Background(), GlobalQuery{
		TenantID: "tenant-acme", WorkspaceID: workspace, Limit: maxPageEvents,
	})
	if err != nil || len(page.Events) != streamCount || page.HasMore {
		t.Fatalf("global page len=%d hasMore=%t err=%v", len(page.Events), page.HasMore, err)
	}
	positions := make([]int, 0, len(page.Events))
	for _, event := range page.Events {
		positions = append(positions, int(event.GlobalPosition))
		if event.Sequence != 1 {
			t.Fatalf("stream sequence = %d, want 1", event.Sequence)
		}
	}
	sort.Ints(positions)
	for index, position := range positions {
		if position != index+1 {
			t.Fatalf("global positions = %v", positions)
		}
	}
}

func TestVolatileMemoryStoreReadersObserveWholeBatchAndCanceledWaiterWritesNothing(t *testing.T) {
	t.Parallel()

	clockEntered := make(chan struct{})
	clockRelease := make(chan struct{})
	var enterOnce sync.Once
	store, err := NewVolatileMemoryStore("atomic-visibility", func(context.Context) time.Time {
		enterOnce.Do(func() { close(clockEntered) })
		<-clockRelease
		return contractTime
	})
	if err != nil {
		t.Fatalf("new store: %v", err)
	}
	query := StreamQuery{
		TenantID: "tenant-acme", WorkspaceID: stringPointer("workspace-acme"),
		StreamID: "task:task-1", Limit: maxPageEvents,
	}
	before, err := store.ReadStreamPage(context.Background(), query)
	if err != nil || len(before.Events) != 0 {
		t.Fatalf("before page = %#v, err=%v", before, err)
	}
	batch := validBatch(t, 0,
		validEvent(t, "evt-1", "key-1"),
		validEvent(t, "evt-2", "key-2"),
		validEvent(t, "evt-3", "key-3"),
	)
	appendDone := make(chan error, 1)
	go func() {
		_, appendErr := store.AppendBatch(context.Background(), batch)
		appendDone <- appendErr
	}()
	select {
	case <-clockEntered:
	case <-time.After(5 * time.Second):
		close(clockRelease)
		t.Fatal("append did not reach clock")
	}

	const readers = 32
	readResults := make(chan StreamPage, readers)
	readErrors := make(chan error, readers)
	var readerWorkers sync.WaitGroup
	readerWorkers.Add(readers)
	for index := 0; index < readers; index++ {
		go func() {
			defer readerWorkers.Done()
			page, readErr := store.ReadStreamPage(context.Background(), query)
			readResults <- page
			readErrors <- readErr
		}()
	}

	cancelled, cancel := context.WithCancel(context.Background())
	cancelledBatch := validBatch(t, 0, validEvent(t, "evt-cancel", "key-cancel"))
	cancelledDone := make(chan error, 1)
	go func() {
		_, appendErr := store.AppendBatch(cancelled, cancelledBatch)
		cancelledDone <- appendErr
	}()
	cancel()
	close(clockRelease)
	if err := <-appendDone; err != nil {
		t.Fatalf("batch append: %v", err)
	}
	readerWorkers.Wait()
	close(readResults)
	close(readErrors)
	for readErr := range readErrors {
		if readErr != nil {
			t.Fatalf("concurrent read: %v", readErr)
		}
	}
	for page := range readResults {
		if got, want := eventIDs(page.Events), []string{"evt-1", "evt-2", "evt-3"}; !reflect.DeepEqual(got, want) {
			t.Fatalf("reader observed partial batch: %v", got)
		}
	}
	if err := <-cancelledDone; !errors.Is(err, context.Canceled) {
		t.Fatalf("cancelled waiter error = %v, want %v", err, context.Canceled)
	}
	after, err := store.ReadStreamPage(context.Background(), query)
	if err != nil || len(after.Events) != 3 {
		t.Fatalf("after page = %#v, err=%v", after, err)
	}
}

func eventIDForIndex(prefix string, index int) string {
	return prefix + "-" + strconv.Itoa(index)
}
