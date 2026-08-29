package eventstore

import (
	"errors"
	"math"
	"strings"
	"testing"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/events"
)

func TestProjectionCheckpointAdapterValidationIsScopeAndCoordinateBound(t *testing.T) {
	t.Parallel()

	workspace := "wsp_acme"
	scope := events.ProjectionScope{TenantID: "ten_acme", WorkspaceID: &workspace, ProjectionID: "messages-v1"}
	zero := events.ProjectionCheckpoint{Scope: scope}
	if !validPostgresProjectionCheckpoint(zero, scope) || !isZeroPostgresProjectionCheckpoint(zero, scope) {
		t.Fatalf("valid zero checkpoint rejected: %#v", zero)
	}
	valid := events.ProjectionCheckpoint{Scope: scope, Position: 1, Cursor: "cursor-1", LastEventID: "evt-1"}
	if !validPostgresProjectionCheckpoint(valid, scope) {
		t.Fatalf("valid checkpoint rejected: %#v", valid)
	}
	for name, candidate := range map[string]events.ProjectionCheckpoint{
		"tenant drift":          {Scope: events.ProjectionScope{TenantID: "ten_other", WorkspaceID: &workspace, ProjectionID: "messages-v1"}, Position: 1, Cursor: "cursor-1", LastEventID: "evt-1"},
		"workspace drift":       {Scope: events.ProjectionScope{TenantID: "ten_acme", WorkspaceID: stringPointer("wsp_other"), ProjectionID: "messages-v1"}, Position: 1, Cursor: "cursor-1", LastEventID: "evt-1"},
		"zero with cursor":      {Scope: scope, Cursor: "cursor-1"},
		"nonzero missing event": {Scope: scope, Position: 1, Cursor: "cursor-1"},
		"nonzero control event": {Scope: scope, Position: 1, Cursor: "cursor-1", LastEventID: "evt\n1"},
		"position overflow":     {Scope: scope, Position: math.MaxUint64, Cursor: "cursor-1", LastEventID: "evt-1"},
		"cursor too long":       {Scope: scope, Position: 1, Cursor: events.Cursor(strings.Repeat("c", maxProjectionCheckpointCursorBytes+1)), LastEventID: "evt-1"},
	} {
		if validPostgresProjectionCheckpoint(candidate, scope) {
			t.Errorf("%s checkpoint unexpectedly accepted: %#v", name, candidate)
		}
	}
	if got := projectionPositionValue(math.MaxUint64); got != -1 {
		t.Fatalf("overflow position conversion = %d, want -1", got)
	}
}

func TestNewProjectionCheckpointStoreRejectsNilPool(t *testing.T) {
	t.Parallel()

	if _, err := NewProjectionCheckpointStore(nil); !errors.Is(err, events.ErrInvalidStore) {
		t.Fatalf("nil pool error = %v, want %v", err, events.ErrInvalidStore)
	}
}
