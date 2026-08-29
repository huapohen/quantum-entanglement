package events

import (
	"context"
	"errors"
	"reflect"
)

const defaultProjectionPageSize uint32 = 64

var (
	ErrProjectionInvalidCheckpoint  = errors.New("invalid event projection checkpoint")
	ErrProjectionCheckpointConflict = errors.New("event projection checkpoint conflict")
	ErrProjectionStoreUnavailable   = errors.New("event projection checkpoint store unavailable")
)

// ProjectionScope is the exact namespace a projection consumes. A nil workspace is the root
// workspace, not a wildcard. Projection IDs are application-owned names and are part of the
// checkpoint identity so two projections cannot advance one another's cursor.
type ProjectionScope struct {
	TenantID     string
	WorkspaceID  *string
	ProjectionID string
}

// ProjectionCheckpoint is the durable resume point for one projection scope. Cursor is opaque
// and must be issued by the EventStore; Position and LastEventID are retained as an independent
// readback invariant. The zero value is the initial checkpoint.
type ProjectionCheckpoint struct {
	Scope       ProjectionScope
	Position    uint64
	Cursor      Cursor
	LastEventID string
}

// ProjectionCheckpointStore atomically compares the complete previous checkpoint and commits the
// next one. A crash after Apply and before Commit is intentionally at-least-once: projection
// handlers must make event IDs idempotent, while the checkpoint store prevents two consumers from
// silently advancing the same projection from different cursors.
type ProjectionCheckpointStore interface {
	LoadProjectionCheckpoint(context.Context, ProjectionScope) (ProjectionCheckpoint, error)
	CommitProjectionCheckpoint(context.Context, ProjectionCheckpoint, ProjectionCheckpoint) error
}

// ProjectionApplyFunc applies one immutable stored event to a projection. It must not mutate the
// supplied event and must tolerate a replay of the same event after an acknowledgement loss.
type ProjectionApplyFunc func(context.Context, StoredEvent) error

// Projector consumes one exact global scope and advances its checkpoint only after the whole page
// has been applied. A page-level commit bounds duplicate replay after a crash; callers wanting a
// tighter acknowledgement window can configure PageSize=1.
type Projector struct {
	eventStore  EventStore
	checkpoints ProjectionCheckpointStore
	apply       ProjectionApplyFunc
	pageSize    uint32
}

// NewProjector constructs a provider-neutral replay loop. It does not claim that either supplied
// store is durable; production composition must separately require durable EventStore and
// checkpoint implementations.
func NewProjector(
	eventStore EventStore,
	checkpoints ProjectionCheckpointStore,
	apply ProjectionApplyFunc,
	pageSize uint32,
) (*Projector, error) {
	if eventStoreIsNil(eventStore) || projectionCheckpointStoreIsNil(checkpoints) || apply == nil ||
		(pageSize != 0 && pageSize > maxPageEvents) {
		return nil, ErrInvalidStore
	}
	if pageSize == 0 {
		pageSize = defaultProjectionPageSize
	}
	return &Projector{eventStore: eventStore, checkpoints: checkpoints, apply: apply, pageSize: pageSize}, nil
}

type ProjectionRunResult struct {
	Checkpoint ProjectionCheckpoint
	Processed  uint64
}

// Run resumes from the checkpoint store and returns only after the current scope is caught up.
// No external effect is retried here; the apply function owns its idempotency and any provider
// reconciliation belongs to a separate outbox/action plane.
func (projector *Projector) Run(ctx context.Context, scope ProjectionScope) (ProjectionRunResult, error) {
	if err := projectionContextError(ctx); err != nil {
		return ProjectionRunResult{}, err
	}
	if projector == nil || projector.eventStore == nil || projector.checkpoints == nil || projector.apply == nil ||
		!validProjectionScope(scope) {
		return ProjectionRunResult{}, ErrInvalidStore
	}
	checkpoint, err := projector.checkpoints.LoadProjectionCheckpoint(ctx, scope)
	if err != nil {
		return ProjectionRunResult{}, mapProjectionStoreError(ctx, err)
	}
	if checkpoint.Scope.ProjectionID == "" {
		checkpoint = zeroProjectionCheckpoint(scope)
	}
	if !validProjectionCheckpoint(checkpoint, scope) {
		return ProjectionRunResult{}, ErrProjectionInvalidCheckpoint
	}
	result := ProjectionRunResult{Checkpoint: cloneProjectionCheckpoint(checkpoint)}
	for {
		if err := projectionContextError(ctx); err != nil {
			return ProjectionRunResult{}, err
		}
		page, err := projector.eventStore.ReadGlobalPage(ctx, GlobalQuery{
			TenantID: scope.TenantID, WorkspaceID: cloneProjectionString(scope.WorkspaceID),
			After: checkpoint.Cursor, Limit: projector.pageSize,
		})
		if err != nil {
			return ProjectionRunResult{}, err
		}
		if len(page.Events) == 0 {
			if page.HasMore || page.Next != checkpoint.Cursor {
				return ProjectionRunResult{}, ErrProjectionInvalidCheckpoint
			}
			result.Checkpoint = cloneProjectionCheckpoint(checkpoint)
			return result, nil
		}
		if page.Next == "" || (page.HasMore && page.Next == checkpoint.Cursor) {
			return ProjectionRunResult{}, ErrProjectionInvalidCheckpoint
		}
		previous := checkpoint
		for index, event := range page.Events {
			if err := projectionContextError(ctx); err != nil {
				return ProjectionRunResult{}, err
			}
			if !projectionEventMatchesScope(event, scope) || event.GlobalPosition <= checkpoint.Position ||
				(index > 0 && event.GlobalPosition <= page.Events[index-1].GlobalPosition) {
				return ProjectionRunResult{}, ErrProjectionInvalidCheckpoint
			}
			if err := projector.apply(ctx, event); err != nil {
				return ProjectionRunResult{}, err
			}
		}
		last := page.Events[len(page.Events)-1]
		next := ProjectionCheckpoint{
			Scope:       cloneProjectionScope(scope),
			Position:    last.GlobalPosition,
			Cursor:      page.Next,
			LastEventID: last.EventID,
		}
		if err := validateProjectionCheckpoint(next, scope); err != nil {
			return ProjectionRunResult{}, err
		}
		if err := projector.checkpoints.CommitProjectionCheckpoint(ctx, previous, next); err != nil {
			return ProjectionRunResult{}, mapProjectionStoreError(ctx, err)
		}
		result.Processed += uint64(len(page.Events))
		checkpoint = next
		result.Checkpoint = cloneProjectionCheckpoint(checkpoint)
		if !page.HasMore {
			return result, nil
		}
	}
}

func validProjectionScope(scope ProjectionScope) bool {
	return validOpaqueText(scope.TenantID, maxIdentifierBytes) &&
		validOptionalIdentifier(scope.WorkspaceID) &&
		validOpaqueText(scope.ProjectionID, maxIdentifierBytes)
}

func validProjectionCheckpoint(checkpoint ProjectionCheckpoint, scope ProjectionScope) bool {
	if !validProjectionScope(checkpoint.Scope) || !sameProjectionScope(checkpoint.Scope, scope) {
		return false
	}
	if checkpoint.Position == 0 {
		return checkpoint.Cursor == "" && checkpoint.LastEventID == ""
	}
	return checkpoint.Cursor != "" && validOpaqueText(checkpoint.LastEventID, maxIdentifierBytes)
}

func validateProjectionCheckpoint(checkpoint ProjectionCheckpoint, scope ProjectionScope) error {
	if !validProjectionCheckpoint(checkpoint, scope) {
		return ErrProjectionInvalidCheckpoint
	}
	return nil
}

func zeroProjectionCheckpoint(scope ProjectionScope) ProjectionCheckpoint {
	return ProjectionCheckpoint{Scope: cloneProjectionScope(scope)}
}

func projectionEventMatchesScope(event StoredEvent, scope ProjectionScope) bool {
	return event.TenantID == scope.TenantID && optionalStringsEqual(event.WorkspaceID, scope.WorkspaceID)
}

func sameProjectionScope(left, right ProjectionScope) bool {
	return left.TenantID == right.TenantID && left.ProjectionID == right.ProjectionID &&
		optionalStringsEqual(left.WorkspaceID, right.WorkspaceID)
}

func cloneProjectionString(value *string) *string {
	if value == nil {
		return nil
	}
	cloned := *value
	return &cloned
}

func cloneProjectionScope(scope ProjectionScope) ProjectionScope {
	scope.WorkspaceID = cloneProjectionString(scope.WorkspaceID)
	return scope
}

func cloneProjectionCheckpoint(checkpoint ProjectionCheckpoint) ProjectionCheckpoint {
	checkpoint.Scope = cloneProjectionScope(checkpoint.Scope)
	return checkpoint
}

func projectionCheckpointStoreIsNil(store ProjectionCheckpointStore) bool {
	if store == nil {
		return true
	}
	value := reflect.ValueOf(store)
	switch value.Kind() {
	case reflect.Chan, reflect.Func, reflect.Interface, reflect.Map, reflect.Pointer, reflect.Slice:
		return value.IsNil()
	default:
		return false
	}
}

func projectionContextError(ctx context.Context) error {
	if ctx == nil {
		return context.Canceled
	}
	return ctx.Err()
}

func mapProjectionStoreError(ctx context.Context, err error) error {
	if err == nil {
		return nil
	}
	if contextErr := projectionContextError(ctx); contextErr != nil {
		return contextErr
	}
	if errors.Is(err, ErrProjectionCheckpointConflict) || errors.Is(err, ErrProjectionInvalidCheckpoint) {
		return err
	}
	return ErrProjectionStoreUnavailable
}
