package events

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"strings"
	"testing"
	"time"
)

func TestVolatileMemoryStorePagesExactScopesWithoutGapOrDuplicate(t *testing.T) {
	t.Parallel()

	store := newVolatileStore(t, contractTime)
	workspace := stringPointer("workspace-acme")
	appendScopeBatch(t, store, 0,
		eventForScope(t, "evt-a1", "key-a1", "tenant-acme", workspace, "task:a"),
		eventForScope(t, "evt-a2", "key-a2", "tenant-acme", workspace, "task:a"),
	)
	appendScopeBatch(t, store, 0,
		eventForScope(t, "evt-other-tenant", "key-other-tenant", "tenant-other", workspace, "task:a"),
	)
	appendScopeBatch(t, store, 0,
		eventForScope(t, "evt-b1", "key-b1", "tenant-acme", workspace, "task:b"),
	)
	appendScopeBatch(t, store, 2,
		eventForScope(t, "evt-a3", "key-a3", "tenant-acme", workspace, "task:a"),
	)
	appendScopeBatch(t, store, 0,
		eventForScope(t, "evt-root", "key-root", "tenant-acme", nil, "task:root"),
	)

	streamQuery := StreamQuery{
		TenantID: "tenant-acme", WorkspaceID: workspace, StreamID: "task:a", Limit: 1,
	}
	var streamEvents []StoredEvent
	for {
		page, err := store.ReadStreamPage(context.Background(), streamQuery)
		if err != nil {
			t.Fatalf("read stream page: %v", err)
		}
		streamEvents = append(streamEvents, page.Events...)
		if !page.HasMore {
			streamQuery.After = page.Next
			break
		}
		if page.Next == "" || page.Next == streamQuery.After {
			t.Fatalf("stream cursor did not advance: %#v", page)
		}
		streamQuery.After = page.Next
		streamQuery.Limit = 2
	}
	if got, want := eventIDs(streamEvents), []string{"evt-a1", "evt-a2", "evt-a3"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("stream ids = %v, want %v", got, want)
	}
	for index, event := range streamEvents {
		if event.Sequence != uint64(index+1) {
			t.Fatalf("stream sequence %d = %d", index, event.Sequence)
		}
	}

	emptyTail, err := store.ReadStreamPage(context.Background(), streamQuery)
	if err != nil {
		t.Fatalf("read stream tail: %v", err)
	}
	if len(emptyTail.Events) != 0 || emptyTail.HasMore || emptyTail.Next != streamQuery.After {
		t.Fatalf("empty tail = %#v", emptyTail)
	}
	appendScopeBatch(t, store, 3,
		eventForScope(t, "evt-a4", "key-a4", "tenant-acme", workspace, "task:a"),
	)
	afterTail, err := store.ReadStreamPage(context.Background(), streamQuery)
	if err != nil || len(afterTail.Events) != 1 || afterTail.Events[0].EventID != "evt-a4" {
		t.Fatalf("tail poll = %#v, err=%v", afterTail, err)
	}

	globalQuery := GlobalQuery{TenantID: "tenant-acme", WorkspaceID: workspace, Limit: 2}
	var globalEvents []StoredEvent
	for {
		page, err := store.ReadGlobalPage(context.Background(), globalQuery)
		if err != nil {
			t.Fatalf("read global page: %v", err)
		}
		globalEvents = append(globalEvents, page.Events...)
		if !page.HasMore {
			break
		}
		globalQuery.After = page.Next
		globalQuery.Limit = 1
	}
	if got, want := eventIDs(globalEvents), []string{"evt-a1", "evt-a2", "evt-b1", "evt-a3", "evt-a4"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("global ids = %v, want %v", got, want)
	}
	for index := 1; index < len(globalEvents); index++ {
		if globalEvents[index].GlobalPosition <= globalEvents[index-1].GlobalPosition {
			t.Fatalf("global positions not increasing: %#v", globalEvents)
		}
	}

	root, err := store.ReadGlobalPage(context.Background(), GlobalQuery{
		TenantID: "tenant-acme", WorkspaceID: nil, Limit: maxPageEvents,
	})
	if err != nil {
		t.Fatalf("read tenant root: %v", err)
	}
	if got, want := eventIDs(root.Events), []string{"evt-root"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("nil workspace acted as wildcard: %v", got)
	}
}

func TestVolatileMemoryStoreCursorBindsKindScopeAndIncarnation(t *testing.T) {
	t.Parallel()

	workspace := stringPointer("workspace-acme")
	store := newVolatileStore(t, contractTime)
	event := eventForScope(t, "evt-1", "key-1", "tenant-acme", workspace, "task:a")
	appendScopeBatch(t, store, 0, event)
	streamQuery := StreamQuery{
		TenantID: event.TenantID, WorkspaceID: cloneStringPointer(event.WorkspaceID),
		StreamID: event.StreamID, Limit: 1,
	}
	streamPage, err := store.ReadStreamPage(context.Background(), streamQuery)
	if err != nil || streamPage.Next == "" {
		t.Fatalf("read seed page: %#v, err=%v", streamPage, err)
	}
	globalPage, err := store.ReadGlobalPage(context.Background(), GlobalQuery{
		TenantID: event.TenantID, WorkspaceID: cloneStringPointer(event.WorkspaceID), Limit: 1,
	})
	if err != nil || globalPage.Next == "" {
		t.Fatalf("read global seed: %#v, err=%v", globalPage, err)
	}

	streamCases := []struct {
		name  string
		query StreamQuery
	}{
		{name: "tenant", query: StreamQuery{TenantID: "tenant-other", WorkspaceID: workspace, StreamID: "task:a", After: streamPage.Next, Limit: 1}},
		{name: "workspace", query: StreamQuery{TenantID: "tenant-acme", WorkspaceID: stringPointer("workspace-other"), StreamID: "task:a", After: streamPage.Next, Limit: 1}},
		{name: "workspace presence", query: StreamQuery{TenantID: "tenant-acme", WorkspaceID: nil, StreamID: "task:a", After: streamPage.Next, Limit: 1}},
		{name: "stream", query: StreamQuery{TenantID: "tenant-acme", WorkspaceID: workspace, StreamID: "task:b", After: streamPage.Next, Limit: 1}},
		{name: "global cursor", query: StreamQuery{TenantID: "tenant-acme", WorkspaceID: workspace, StreamID: "task:a", After: globalPage.Next, Limit: 1}},
	}
	for _, testCase := range streamCases {
		t.Run(testCase.name, func(t *testing.T) {
			if _, err := store.ReadStreamPage(context.Background(), testCase.query); !errors.Is(err, ErrInvalidCursor) {
				t.Fatalf("error = %v, want %v", err, ErrInvalidCursor)
			}
		})
	}
	if _, err := store.ReadGlobalPage(context.Background(), GlobalQuery{
		TenantID: "tenant-acme", WorkspaceID: workspace, After: streamPage.Next, Limit: 1,
	}); !errors.Is(err, ErrInvalidCursor) {
		t.Fatalf("stream cursor in global query error = %v, want %v", err, ErrInvalidCursor)
	}

	malformed := []Cursor{
		"not/base64",
		streamPage.Next[:len(streamPage.Next)-1],
		streamPage.Next + "A",
		Cursor(strings.Repeat("a", maxEncodedCursor+1)),
	}
	mutated := []byte(streamPage.Next)
	if mutated[len(mutated)/2] == 'a' {
		mutated[len(mutated)/2] = 'b'
	} else {
		mutated[len(mutated)/2] = 'a'
	}
	malformed = append(malformed, Cursor(mutated))
	for _, cursor := range malformed {
		query := streamQuery
		query.After = cursor
		if _, err := store.ReadStreamPage(context.Background(), query); !errors.Is(err, ErrInvalidCursor) {
			t.Fatalf("malformed cursor %q error = %v, want %v", cursor, err, ErrInvalidCursor)
		}
	}

	future, err := encodeCursor(store.cursorBindingFromScope("stream", scopeFromStreamQuery(streamQuery)), 2)
	if err != nil {
		t.Fatalf("encode future cursor: %v", err)
	}
	futureQuery := streamQuery
	futureQuery.After = future
	if _, err := store.ReadStreamPage(context.Background(), futureQuery); !errors.Is(err, ErrInvalidCursor) {
		t.Fatalf("future cursor error = %v, want %v", err, ErrInvalidCursor)
	}

	decoded, err := base64.RawURLEncoding.DecodeString(string(streamPage.Next))
	if err != nil {
		t.Fatalf("decode issued cursor: %v", err)
	}
	var raw map[string]any
	if err := json.Unmarshal(decoded, &raw); err != nil {
		t.Fatalf("unmarshal issued cursor: %v", err)
	}
	raw["unknown"] = true
	withUnknown, err := json.Marshal(raw)
	if err != nil {
		t.Fatalf("marshal unknown cursor: %v", err)
	}
	unknownQuery := streamQuery
	unknownQuery.After = Cursor(base64.RawURLEncoding.EncodeToString(withUnknown))
	if _, err := store.ReadStreamPage(context.Background(), unknownQuery); !errors.Is(err, ErrInvalidCursor) {
		t.Fatalf("unknown-field cursor error = %v, want %v", err, ErrInvalidCursor)
	}
	var issued cursorEnvelope
	if err := json.Unmarshal(decoded, &issued); err != nil {
		t.Fatalf("unmarshal issued envelope: %v", err)
	}
	contentJSON, err := json.Marshal(issued.Content)
	if err != nil {
		t.Fatalf("marshal issued content: %v", err)
	}
	duplicateJSON := []byte(fmt.Sprintf(
		`{"content":%s,"content":%s,"digest":%q}`,
		contentJSON, contentJSON, issued.Digest,
	))
	duplicateQuery := streamQuery
	duplicateQuery.After = Cursor(base64.RawURLEncoding.EncodeToString(duplicateJSON))
	if _, err := store.ReadStreamPage(context.Background(), duplicateQuery); !errors.Is(err, ErrInvalidCursor) {
		t.Fatalf("duplicate-field cursor error = %v, want %v", err, ErrInvalidCursor)
	}

	otherStore, err := NewVolatileMemoryStore("other-instance", func() time.Time { return contractTime })
	if err != nil {
		t.Fatalf("new other store: %v", err)
	}
	appendScopeBatch(t, otherStore, 0, event)
	oldInstanceQuery := streamQuery
	oldInstanceQuery.After = streamPage.Next
	if _, err := otherStore.ReadStreamPage(context.Background(), oldInstanceQuery); !errors.Is(err, ErrInvalidCursor) {
		t.Fatalf("old-instance cursor error = %v, want %v", err, ErrInvalidCursor)
	}
}

func TestVolatileMemoryStoreRetryIdentityScopesAreExact(t *testing.T) {
	t.Parallel()

	store := newVolatileStore(t, contractTime)
	workspace := stringPointer("workspace-acme")
	appendScopeBatch(t, store, 0,
		eventForScope(t, "evt-shared", "key-shared", "tenant-acme", workspace, "task:a"),
	)
	appendScopeBatch(t, store, 0,
		eventForScope(t, "evt-shared", "key-shared", "tenant-other", workspace, "task:a"),
	)
	appendScopeBatch(t, store, 0,
		eventForScope(t, "evt-shared", "key-shared", "tenant-acme", stringPointer("workspace-other"), "task:a"),
	)
	appendScopeBatch(t, store, 0,
		eventForScope(t, "evt-b", "key-shared", "tenant-acme", workspace, "task:b"),
	)

	crossStreamEventID := validBatch(t, 0,
		eventForScope(t, "evt-shared", "key-c", "tenant-acme", workspace, "task:c"),
	)
	if _, err := store.AppendBatch(context.Background(), crossStreamEventID); !errors.Is(err, ErrIdempotencyConflict) {
		t.Fatalf("cross-stream EventID error = %v, want %v", err, ErrIdempotencyConflict)
	}
}

func TestVolatileMemoryStoreRebuildIsDeterministicButFreshStoreIsEmpty(t *testing.T) {
	t.Parallel()

	workspace := stringPointer("workspace-acme")
	events := []EventToAppend{
		eventForScope(t, "evt-1", "key-1", "tenant-acme", workspace, "task:a"),
		eventForScope(t, "evt-2", "key-2", "tenant-acme", workspace, "task:a"),
	}
	first, err := NewVolatileMemoryStore("rebuild-fixture", func() time.Time { return contractTime })
	if err != nil {
		t.Fatalf("new first store: %v", err)
	}
	second, err := NewVolatileMemoryStore("rebuild-fixture", func() time.Time { return contractTime })
	if err != nil {
		t.Fatalf("new second store: %v", err)
	}
	empty, err := second.ReadStreamPage(context.Background(), StreamQuery{
		TenantID: "tenant-acme", WorkspaceID: workspace, StreamID: "task:a", Limit: 1,
	})
	if err != nil || len(empty.Events) != 0 {
		t.Fatalf("fresh store page = %#v, err=%v", empty, err)
	}
	firstResult := appendScopeBatch(t, first, 0, events...)
	secondResult := appendScopeBatch(t, second, 0, events...)
	if !reflect.DeepEqual(firstResult, secondResult) {
		t.Fatalf("rebuilt results differ:\nfirst=%#v\nsecond=%#v", firstResult, secondResult)
	}
	query := StreamQuery{TenantID: "tenant-acme", WorkspaceID: workspace, StreamID: "task:a", Limit: 1}
	firstPage, firstErr := first.ReadStreamPage(context.Background(), query)
	secondPage, secondErr := second.ReadStreamPage(context.Background(), query)
	if firstErr != nil || secondErr != nil || !reflect.DeepEqual(firstPage, secondPage) {
		t.Fatalf("rebuilt pages differ: first=%#v/%v second=%#v/%v", firstPage, firstErr, secondPage, secondErr)
	}
}

func TestVolatileMemoryStoreRejectsInvalidPageLimits(t *testing.T) {
	t.Parallel()

	store := newVolatileStore(t, contractTime)
	for _, limit := range []uint32{0, maxPageEvents + 1} {
		if _, err := store.ReadStreamPage(context.Background(), StreamQuery{
			TenantID: "tenant-acme", WorkspaceID: stringPointer("workspace-acme"),
			StreamID: "task:a", Limit: limit,
		}); !errors.Is(err, ErrInvalidQuery) {
			t.Fatalf("stream limit %d error = %v, want %v", limit, err, ErrInvalidQuery)
		}
		if _, err := store.ReadGlobalPage(context.Background(), GlobalQuery{
			TenantID: "tenant-acme", WorkspaceID: stringPointer("workspace-acme"), Limit: limit,
		}); !errors.Is(err, ErrInvalidQuery) {
			t.Fatalf("global limit %d error = %v, want %v", limit, err, ErrInvalidQuery)
		}
	}
}

func TestVolatileMemoryStoreReturnsIndependentPageSnapshots(t *testing.T) {
	t.Parallel()

	store := newVolatileStore(t, contractTime)
	workspace := stringPointer("workspace-acme")
	event := eventForScope(t, "evt-1", "key-1", "tenant-acme", workspace, "task:a")
	event.SchemaVersion = 99
	event.EventType = "future.event.v99"
	appendScopeBatch(t, store, 0, event)
	streamQuery := StreamQuery{
		TenantID: event.TenantID, WorkspaceID: cloneStringPointer(event.WorkspaceID),
		StreamID: event.StreamID, Limit: 1,
	}
	first, err := store.ReadStreamPage(context.Background(), streamQuery)
	if err != nil {
		t.Fatalf("first stream read: %v", err)
	}
	first.Events[0].EventID = "mutated-stream-page"
	*first.Events[0].WorkspaceID = "mutated-stream-workspace"
	first.Events[0].Payload.inline[0] ^= 0xff
	second, err := store.ReadStreamPage(context.Background(), streamQuery)
	if err != nil {
		t.Fatalf("second stream read: %v", err)
	}
	if second.Events[0].EventID != "evt-1" || second.Events[0].SchemaVersion != 99 ||
		second.Events[0].EventType != "future.event.v99" || *second.Events[0].WorkspaceID != "workspace-acme" ||
		string(second.Events[0].Payload.InlineJSON()) != `{"value":1}` {
		t.Fatalf("stream page mutated store or rewrote unknown schema: %#v", second.Events[0])
	}

	globalQuery := GlobalQuery{TenantID: event.TenantID, WorkspaceID: workspace, Limit: 1}
	global, err := store.ReadGlobalPage(context.Background(), globalQuery)
	if err != nil {
		t.Fatalf("first global read: %v", err)
	}
	global.Events[0].EventID = "mutated-global-page"
	globalAgain, err := store.ReadGlobalPage(context.Background(), globalQuery)
	if err != nil {
		t.Fatalf("second global read: %v", err)
	}
	if globalAgain.Events[0].EventID != "evt-1" {
		t.Fatalf("global page mutated store: %#v", globalAgain.Events[0])
	}

	cancelled, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := store.ReadStreamPage(cancelled, streamQuery); !errors.Is(err, context.Canceled) {
		t.Fatalf("cancelled stream read error = %v, want %v", err, context.Canceled)
	}
	if _, err := store.ReadGlobalPage(cancelled, globalQuery); !errors.Is(err, context.Canceled) {
		t.Fatalf("cancelled global read error = %v, want %v", err, context.Canceled)
	}
}

func appendScopeBatch(
	t *testing.T,
	store *VolatileMemoryStore,
	expectedVersion uint64,
	events ...EventToAppend,
) AppendResult {
	t.Helper()
	result, err := store.AppendBatch(context.Background(), validBatch(t, expectedVersion, events...))
	if err != nil {
		t.Fatalf("append scope batch: %v", err)
	}
	return result
}

func eventForScope(
	t *testing.T,
	eventID string,
	idempotencyKey string,
	tenant string,
	workspace *string,
	stream string,
) EventToAppend {
	t.Helper()
	event := validEvent(t, eventID, idempotencyKey)
	event.TenantID = tenant
	event.WorkspaceID = cloneStringPointer(workspace)
	event.StreamID = stream
	return event
}

func eventIDs(events []StoredEvent) []string {
	result := make([]string, 0, len(events))
	for _, event := range events {
		result = append(result, event.EventID)
	}
	return result
}
