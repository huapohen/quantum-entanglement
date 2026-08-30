package events

// DurableProjectionCheckpointFileStore is a single-process, append-only local
// checkpoint adapter for recovery exercises. It is deliberately separate from
// the projection handler and EventStore: a checkpoint is only a resume point,
// never proof that a projection side effect is durable or idempotent.

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"sync"
)

const durableProjectionCheckpointFileFormat = "quantum-entanglement.event-projection-checkpoint/1"

var (
	ErrDurableProjectionCheckpointFileClosed = errors.New("durable projection checkpoint file store is closed")
	ErrDurableProjectionCheckpointFileLog    = errors.New("durable projection checkpoint file log is corrupt")
)

// DurableProjectionCheckpointFileStore persists the latest checkpoint per
// tenant/workspace/projection scope as newline-delimited JSON. A compare-and-set
// commit is fsynced before the in-memory value changes. It does not provide
// multi-process locking, tamper evidence, replication, retention or backup.
type DurableProjectionCheckpointFileStore struct {
	mu      sync.RWMutex
	path    string
	file    *os.File
	entries map[projectionCheckpointFileKey]ProjectionCheckpoint
	closed  bool
}

var _ ProjectionCheckpointStore = (*DurableProjectionCheckpointFileStore)(nil)

type projectionCheckpointFileKey struct {
	tenantID    string
	workspaceID string
	projection  string
}

// OpenDurableProjectionCheckpointFileStore opens or creates a local checkpoint
// log. The parent directory must already exist and the file is owner-readable
// only.
func OpenDurableProjectionCheckpointFileStore(
	ctx context.Context,
	path string,
) (*DurableProjectionCheckpointFileStore, error) {
	if err := durableProjectionContextError(ctx); err != nil {
		return nil, err
	}
	if path == "" || filepath.Clean(path) == "." || !filepath.IsAbs(path) {
		return nil, ErrInvalidStore
	}
	parent := filepath.Dir(path)
	if info, err := os.Stat(parent); err != nil || !info.IsDir() {
		return nil, ErrInvalidStore
	}
	file, err := os.OpenFile(path, os.O_RDWR|os.O_CREATE|os.O_APPEND, 0o600)
	if err != nil {
		return nil, ErrProjectionStoreUnavailable
	}
	if err := file.Chmod(0o600); err != nil {
		_ = file.Close()
		return nil, ErrProjectionStoreUnavailable
	}
	store := &DurableProjectionCheckpointFileStore{
		path: path, file: file,
		entries: make(map[projectionCheckpointFileKey]ProjectionCheckpoint),
	}
	if err := store.load(ctx); err != nil {
		_ = file.Close()
		return nil, err
	}
	return store, nil
}

// LoadProjectionCheckpoint returns the exact durable checkpoint or the zero
// checkpoint for a valid scope that has not committed yet.
func (store *DurableProjectionCheckpointFileStore) LoadProjectionCheckpoint(
	ctx context.Context,
	scope ProjectionScope,
) (ProjectionCheckpoint, error) {
	if err := durableProjectionContextError(ctx); err != nil {
		return ProjectionCheckpoint{}, err
	}
	if store == nil || !validProjectionScope(scope) {
		return ProjectionCheckpoint{}, ErrProjectionInvalidCheckpoint
	}
	store.mu.RLock()
	defer store.mu.RUnlock()
	if store.closed || store.file == nil {
		return ProjectionCheckpoint{}, ErrDurableProjectionCheckpointFileClosed
	}
	checkpoint, ok := store.entries[newProjectionCheckpointFileKey(scope)]
	if !ok {
		return zeroProjectionCheckpoint(scope), nil
	}
	return cloneProjectionCheckpoint(checkpoint), nil
}

// CommitProjectionCheckpoint compares the complete previous value and then
// persists next. A conflict never writes a new record.
func (store *DurableProjectionCheckpointFileStore) CommitProjectionCheckpoint(
	ctx context.Context,
	previous ProjectionCheckpoint,
	next ProjectionCheckpoint,
) error {
	if err := durableProjectionContextError(ctx); err != nil {
		return err
	}
	if store == nil || !validProjectionCheckpoint(previous, next.Scope) ||
		!validProjectionCheckpoint(next, next.Scope) ||
		next.Position < previous.Position {
		return ErrProjectionInvalidCheckpoint
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	if store.closed || store.file == nil {
		return ErrDurableProjectionCheckpointFileClosed
	}
	current, exists := store.entries[newProjectionCheckpointFileKey(next.Scope)]
	if exists {
		if !sameProjectionCheckpoint(current, previous) {
			return ErrProjectionCheckpointConflict
		}
	} else if !isZeroProjectionCheckpoint(previous, next.Scope) {
		return ErrProjectionCheckpointConflict
	}
	encoded, err := json.Marshal(newDurableProjectionCheckpointRecord(next))
	if err != nil {
		return ErrDurableProjectionCheckpointFileLog
	}
	encoded = append(encoded, '\n')
	if err := store.appendAndSync(encoded); err != nil {
		return err
	}
	store.entries[newProjectionCheckpointFileKey(next.Scope)] = cloneProjectionCheckpoint(next)
	return nil
}

// Close flushes the log and rejects subsequent operations. It is idempotent.
func (store *DurableProjectionCheckpointFileStore) Close() error {
	if store == nil {
		return nil
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	if store.closed {
		return nil
	}
	store.closed = true
	if store.file == nil {
		return nil
	}
	err := store.file.Sync()
	closeErr := store.file.Close()
	store.file = nil
	if err != nil || closeErr != nil {
		return ErrProjectionStoreUnavailable
	}
	return nil
}

func (store *DurableProjectionCheckpointFileStore) appendAndSync(encoded []byte) error {
	if len(encoded) == 0 || store.file == nil {
		return ErrDurableProjectionCheckpointFileLog
	}
	start, err := store.file.Seek(0, io.SeekEnd)
	if err != nil {
		return ErrProjectionStoreUnavailable
	}
	written := 0
	for written < len(encoded) {
		count, writeErr := store.file.Write(encoded[written:])
		if writeErr != nil || count <= 0 {
			_ = store.file.Truncate(start)
			_ = store.file.Sync()
			return ErrProjectionStoreUnavailable
		}
		written += count
	}
	if err := store.file.Sync(); err != nil {
		_ = store.file.Truncate(start)
		_ = store.file.Sync()
		return ErrProjectionStoreUnavailable
	}
	return nil
}

func (store *DurableProjectionCheckpointFileStore) load(ctx context.Context) error {
	if err := durableProjectionContextError(ctx); err != nil {
		return err
	}
	data, err := os.ReadFile(store.path)
	if err != nil {
		return ErrProjectionStoreUnavailable
	}
	if len(data) == 0 {
		return nil
	}
	lastGood := 0
	for start := 0; start < len(data); {
		if err := durableProjectionContextError(ctx); err != nil {
			return err
		}
		relativeEnd := bytes.IndexByte(data[start:], '\n')
		if relativeEnd < 0 {
			// A process can die before the final newline. Drop only this tail.
			if len(bytes.TrimSpace(data[start:])) > 0 {
				if err := store.file.Truncate(int64(lastGood)); err != nil {
					return ErrProjectionStoreUnavailable
				}
				if err := store.file.Sync(); err != nil {
					return ErrProjectionStoreUnavailable
				}
			}
			return nil
		}
		end := start + relativeEnd
		line := data[start:end]
		if len(bytes.TrimSpace(line)) == 0 {
			return ErrDurableProjectionCheckpointFileLog
		}
		checkpoint, err := decodeDurableProjectionCheckpointRecord(line)
		if err != nil {
			return ErrDurableProjectionCheckpointFileLog
		}
		key := newProjectionCheckpointFileKey(checkpoint.Scope)
		if existing, ok := store.entries[key]; ok {
			if checkpoint.Position < existing.Position ||
				(checkpoint.Position == existing.Position && !sameProjectionCheckpoint(existing, checkpoint)) {
				return ErrDurableProjectionCheckpointFileLog
			}
		}
		store.entries[key] = checkpoint
		lastGood = end + 1
		start = end + 1
	}
	return nil
}

type durableProjectionCheckpointRecord struct {
	Format      string                 `json:"format"`
	Scope       durableProjectionScope `json:"scope"`
	Position    uint64                 `json:"position"`
	Cursor      Cursor                 `json:"cursor"`
	LastEventID string                 `json:"lastEventId"`
}

type durableProjectionScope struct {
	TenantID     string  `json:"tenantId"`
	WorkspaceID  *string `json:"workspaceId,omitempty"`
	ProjectionID string  `json:"projectionId"`
}

func newDurableProjectionCheckpointRecord(checkpoint ProjectionCheckpoint) durableProjectionCheckpointRecord {
	return durableProjectionCheckpointRecord{
		Format: durableProjectionCheckpointFileFormat,
		Scope: durableProjectionScope{
			TenantID: checkpoint.Scope.TenantID, WorkspaceID: cloneProjectionString(checkpoint.Scope.WorkspaceID),
			ProjectionID: checkpoint.Scope.ProjectionID,
		},
		Position: checkpoint.Position, Cursor: checkpoint.Cursor, LastEventID: checkpoint.LastEventID,
	}
}

func decodeDurableProjectionCheckpointRecord(raw []byte) (ProjectionCheckpoint, error) {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	var wire durableProjectionCheckpointRecord
	if err := decoder.Decode(&wire); err != nil {
		return ProjectionCheckpoint{}, ErrDurableProjectionCheckpointFileLog
	}
	var trailing any
	if err := decoder.Decode(&trailing); !errors.Is(err, io.EOF) || wire.Format != durableProjectionCheckpointFileFormat {
		return ProjectionCheckpoint{}, ErrDurableProjectionCheckpointFileLog
	}
	scope := ProjectionScope{
		TenantID: wire.Scope.TenantID, WorkspaceID: cloneProjectionString(wire.Scope.WorkspaceID),
		ProjectionID: wire.Scope.ProjectionID,
	}
	checkpoint := ProjectionCheckpoint{
		Scope: scope, Position: wire.Position, Cursor: wire.Cursor, LastEventID: wire.LastEventID,
	}
	if !validProjectionCheckpoint(checkpoint, scope) {
		return ProjectionCheckpoint{}, ErrDurableProjectionCheckpointFileLog
	}
	return checkpoint, nil
}

func newProjectionCheckpointFileKey(scope ProjectionScope) projectionCheckpointFileKey {
	return projectionCheckpointFileKey{
		tenantID: scope.TenantID, workspaceID: durableProjectionWorkspaceValue(scope.WorkspaceID), projection: scope.ProjectionID,
	}
}

func sameProjectionCheckpoint(left, right ProjectionCheckpoint) bool {
	return sameProjectionScope(left.Scope, right.Scope) && left.Position == right.Position &&
		left.Cursor == right.Cursor && left.LastEventID == right.LastEventID
}

func isZeroProjectionCheckpoint(checkpoint ProjectionCheckpoint, scope ProjectionScope) bool {
	return sameProjectionScope(checkpoint.Scope, scope) && checkpoint.Position == 0 &&
		checkpoint.Cursor == "" && checkpoint.LastEventID == ""
}

func durableProjectionContextError(ctx context.Context) error {
	if ctx == nil {
		return context.Canceled
	}
	return ctx.Err()
}

func durableProjectionWorkspaceValue(workspace *string) string {
	if workspace == nil {
		return ""
	}
	return *workspace
}
