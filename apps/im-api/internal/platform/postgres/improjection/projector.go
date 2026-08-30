package improjection

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"math"
	"strings"
	"time"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/events"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
	coreprojection "github.com/huapohen/quantum-entanglement/apps/im-api/internal/improjection"
	store "github.com/huapohen/quantum-entanglement/apps/im-api/internal/imstore"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/platform/postgres/eventstore"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/platform/postgres/runtimepool"
	"github.com/jackc/pgx/v5"
)

const (
	messageProjectionID       = "messages-v1"
	defaultProjectorPageSize  = uint32(64)
	maximumProjectorPageSize  = uint32(256)
	maximumProjectorStateRows = 100000
)

var (
	ErrProjectorInvalid   = errors.New("invalid PostgreSQL message projector request")
	ErrProjectorConflict  = errors.New("PostgreSQL message projection CAS conflict")
	ErrProjectorIntegrity = errors.New("PostgreSQL message projection integrity failure")
)

// Projector applies the provider-neutral message reducer to a PostgreSQL event stream. Each
// page is read, row/head CAS-applied and checkpointed in one Serializable transaction. The
// runtime pool exposes only SELECT and execute privileges; all projection writes go through the
// owner-defined migration-12 functions.
type Projector struct {
	pool *runtimepool.Pool
}

type ProjectorResult struct {
	Checkpoint events.ProjectionCheckpoint
	Processed  uint64
}

func NewProjector(pool *runtimepool.Pool) (*Projector, error) {
	if pool == nil {
		return nil, ErrProjectorInvalid
	}
	return &Projector{pool: pool}, nil
}

// Run drains one tenant/workspace global stream. A nil workspace denotes the root workspace and
// is intentionally distinct from a wildcard. Re-running after a lost commit acknowledgement is
// safe: the write functions accept an exact existing row/head and the checkpoint CAS prevents
// competing projectors from advancing the same scope.
func (projector *Projector) Run(
	ctx context.Context,
	tenant im.TenantID,
	workspace *im.WorkspaceID,
	pageSize uint32,
) (ProjectorResult, error) {
	if ctx == nil || ctx.Err() != nil || projector == nil || projector.pool == nil || tenant.IsZero() {
		return ProjectorResult{}, ErrProjectorInvalid
	}
	if pageSize == 0 {
		pageSize = defaultProjectorPageSize
	}
	if pageSize > maximumProjectorPageSize {
		return ProjectorResult{}, ErrProjectorInvalid
	}
	tenantID := tenant.String()
	workspaceValue := ""
	var workspaceString *string
	if workspace != nil {
		workspaceValue = workspace.String()
		workspaceString = &workspaceValue
	}
	scope := events.ProjectionScope{TenantID: tenantID, WorkspaceID: workspaceString, ProjectionID: messageProjectionID}
	result := ProjectorResult{Checkpoint: events.ProjectionCheckpoint{Scope: scope}}
	for {
		pageResult, done, err := projector.runPage(ctx, scope, pageSize)
		if err != nil {
			return ProjectorResult{}, err
		}
		result.Processed += pageResult.Processed
		result.Checkpoint = pageResult.Checkpoint
		if done {
			return result, nil
		}
	}
}

func (projector *Projector) runPage(
	ctx context.Context,
	scope events.ProjectionScope,
	pageSize uint32,
) (ProjectorResult, bool, error) {
	connection, err := projector.pool.Acquire(ctx)
	if err != nil {
		return ProjectorResult{}, false, store.ErrStoreUnavailable
	}
	defer connection.Release()
	transaction, err := connection.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.Serializable})
	if err != nil {
		return ProjectorResult{}, false, store.ErrStoreUnavailable
	}
	defer rollbackProjectorTransaction(transaction)
	if err := bindMaterializedTenant(ctx, transaction, scope.TenantID); err != nil {
		return ProjectorResult{}, false, err
	}
	checkpoint, err := loadProjectorCheckpoint(ctx, transaction, scope)
	if err != nil {
		return ProjectorResult{}, false, err
	}
	page, err := eventstore.ReadGlobalPageTx(ctx, transaction, events.GlobalQuery{
		TenantID: scope.TenantID, WorkspaceID: cloneWorkspace(scope.WorkspaceID),
		After: checkpoint.Cursor, Limit: pageSize,
	})
	if err != nil {
		return ProjectorResult{}, false, mapProjectorEventError(err)
	}
	if len(page.Events) == 0 {
		if page.HasMore || page.Next != checkpoint.Cursor {
			return ProjectorResult{}, false, ErrProjectorIntegrity
		}
		if err := transaction.Commit(ctx); err != nil {
			return ProjectorResult{}, false, store.ErrStoreUnavailable
		}
		return ProjectorResult{Checkpoint: checkpoint}, true, nil
	}
	if page.Next == "" || (page.HasMore && page.Next == checkpoint.Cursor) {
		return ProjectorResult{}, false, ErrProjectorIntegrity
	}
	states := make(map[string]*conversationState)
	for _, event := range page.Events {
		if err := projector.applyEvent(ctx, transaction, scope, states, event); err != nil {
			return ProjectorResult{}, false, err
		}
	}
	last := page.Events[len(page.Events)-1]
	next := events.ProjectionCheckpoint{
		Scope:       cloneProjectionScope(scope),
		Position:    last.GlobalPosition,
		Cursor:      page.Next,
		LastEventID: last.EventID,
	}
	if err := commitProjectorCheckpoint(ctx, transaction, checkpoint, next); err != nil {
		return ProjectorResult{}, false, err
	}
	if err := transaction.Commit(ctx); err != nil {
		return ProjectorResult{}, false, store.ErrStoreUnavailable
	}
	return ProjectorResult{Checkpoint: next, Processed: uint64(len(page.Events))}, !page.HasMore, nil
}

type conversationState struct {
	reference  im.ConversationRef
	projection *coreprojection.MessageProjection
	sequence   uint64
	position   uint64
	revision   uint64
}

func (projector *Projector) applyEvent(
	ctx context.Context,
	transaction pgx.Tx,
	scope events.ProjectionScope,
	states map[string]*conversationState,
	event events.StoredEvent,
) error {
	if event.TenantID != scope.TenantID || workspaceText(event.WorkspaceID) != workspaceText(scope.WorkspaceID) ||
		event.GlobalPosition == 0 || event.Sequence == 0 {
		return ErrProjectorIntegrity
	}
	// The global EventStore contains workflow, inbox and other non-conversation streams.
	// They still advance the global projection checkpoint, but do not belong to this
	// conversation message projection and must not be parsed as conversation IDs.
	if !strings.HasPrefix(event.StreamID, "cnv_") {
		return nil
	}
	tenant, err := im.ParseTenantID(scope.TenantID)
	if err != nil {
		return ErrProjectorIntegrity
	}
	conversationID, err := im.ParseConversationID(event.StreamID)
	if err != nil {
		return ErrProjectorIntegrity
	}
	reference, err := im.NewConversationRef(tenant, conversationID)
	if err != nil {
		return ErrProjectorIntegrity
	}
	key := conversationID.String()
	state := states[key]
	if state == nil {
		state, err = loadConversationState(ctx, transaction, reference, workspaceText(scope.WorkspaceID))
		if err != nil {
			return err
		}
		states[key] = state
	}
	if event.Sequence <= state.sequence {
		return nil
	}
	if event.Sequence != state.sequence+1 {
		return ErrProjectorIntegrity
	}
	expectedSequence, expectedPosition, expectedRevision := state.sequence, state.position, state.revision
	nextRevision := expectedRevision + 1
	if nextRevision > math.MaxInt64 || event.Sequence > math.MaxInt64 || event.GlobalPosition > math.MaxInt64 {
		return ErrProjectorIntegrity
	}
	var message im.MessageSnapshot
	var messageID string
	if isMessageProjectionEvent(event.EventType) {
		if err := state.projection.Apply(ctx, event); err != nil {
			return ErrProjectorIntegrity
		}
		messageID, err = messageIDFromProjectionEvent(event)
		if err != nil {
			return ErrProjectorIntegrity
		}
		parsedMessageID, parseErr := im.ParseMessageID(messageID)
		if parseErr != nil {
			return ErrProjectorIntegrity
		}
		var ok bool
		message, ok = state.projection.Snapshot(parsedMessageID)
		if !ok {
			return ErrProjectorIntegrity
		}
		var written bool
		if err := transaction.QueryRow(ctx, `
SELECT wanwork_im.write_message_projection(
    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
    $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22
)`,
			scope.TenantID, workspaceText(scope.WorkspaceID), reference.ConversationID().String(), messageProjectionID,
			int64(expectedSequence), int64(expectedPosition), int64(expectedRevision),
			int64(event.Sequence), int64(event.GlobalPosition), int64(nextRevision),
			message.Ref().MessageID().String(), message.ClientMessageID().String(), message.Sender().ActorID().String(),
			string(message.MessageType()), string(message.Status()), message.Text(), message.ExtInfo(), message.CreatedAt().UTC(),
			int64(message.Revision()), int64(event.Sequence), int64(event.GlobalPosition), int64(nextRevision),
		).Scan(&written); err != nil {
			return mapProjectorWriteError(ctx, err)
		}
		if !written {
			return ErrProjectorConflict
		}
	} else {
		if err := state.projection.ObserveSequence(ctx, event); err != nil {
			return ErrProjectorIntegrity
		}
		var advanced bool
		if err := transaction.QueryRow(ctx, `
SELECT wanwork_im.advance_message_projection_head(
    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10
)`,
			scope.TenantID, workspaceText(scope.WorkspaceID), reference.ConversationID().String(), messageProjectionID,
			int64(expectedSequence), int64(expectedPosition), int64(expectedRevision),
			int64(event.Sequence), int64(event.GlobalPosition), int64(nextRevision),
		).Scan(&advanced); err != nil {
			return mapProjectorWriteError(ctx, err)
		}
		if !advanced {
			return ErrProjectorConflict
		}
	}
	state.sequence, state.position, state.revision = event.Sequence, event.GlobalPosition, nextRevision
	return nil
}

func loadConversationState(
	ctx context.Context,
	transaction pgx.Tx,
	reference im.ConversationRef,
	workspace string,
) (*conversationState, error) {
	var sequence, position, revision int64
	err := transaction.QueryRow(ctx, `
SELECT current_sequence, current_global_position, current_revision
FROM wanwork_im.message_projection_heads
WHERE tenant_id = $1 AND workspace_id = $2 AND conversation_id = $3 AND projection_id = $4`,
		reference.TenantID().String(), workspace, reference.ConversationID().String(), messageProjectionID,
	).Scan(&sequence, &position, &revision)
	if errors.Is(err, pgx.ErrNoRows) {
		sequence, position, revision = 0, 0, 0
	} else if err != nil {
		return nil, store.ErrStoreUnavailable
	}
	if sequence < 0 || position < 0 || revision < 0 ||
		(sequence == 0) != (position == 0) || (sequence == 0) != (revision == 0) {
		return nil, ErrProjectorIntegrity
	}
	rows, err := transaction.Query(ctx, `
SELECT message_id, client_message_id, sender_actor_id, message_type, status,
       text, ext_info, created_at, revision, last_event_sequence, last_event_position,
       projection_revision
FROM wanwork_im.message_snapshots
WHERE tenant_id = $1 AND workspace_id = $2 AND conversation_id = $3
ORDER BY created_at, message_id
LIMIT $4`, reference.TenantID().String(), workspace, reference.ConversationID().String(), maximumProjectorStateRows)
	if err != nil {
		return nil, store.ErrStoreUnavailable
	}
	defer rows.Close()
	messages := make([]im.MessageSnapshot, 0)
	for rows.Next() {
		var messageID, clientMessageID, senderActorID, messageType, status string
		var text, extInfo string
		var createdAt time.Time
		var messageRevision, lastSequence, lastPosition, projectionRevision int64
		if err := rows.Scan(&messageID, &clientMessageID, &senderActorID, &messageType, &status,
			&text, &extInfo, &createdAt, &messageRevision, &lastSequence, &lastPosition, &projectionRevision); err != nil {
			return nil, ErrProjectorIntegrity
		}
		message, err := materializedMessageSnapshot(reference, messageID, clientMessageID, senderActorID,
			messageType, status, text, extInfo, createdAt, messageRevision)
		if err != nil || lastSequence <= 0 || lastPosition <= 0 || projectionRevision != revision ||
			lastSequence > sequence || lastPosition > position {
			return nil, ErrProjectorIntegrity
		}
		messages = append(messages, message)
	}
	if err := rows.Err(); err != nil {
		return nil, store.ErrStoreUnavailable
	}
	projection, err := coreprojection.NewMessageProjectionFromSnapshots(reference, messages, uint64(sequence))
	if err != nil {
		return nil, ErrProjectorIntegrity
	}
	return &conversationState{reference: reference, projection: projection,
		sequence: uint64(sequence), position: uint64(position), revision: uint64(revision)}, nil
}

func loadProjectorCheckpoint(
	ctx context.Context,
	transaction pgx.Tx,
	scope events.ProjectionScope,
) (events.ProjectionCheckpoint, error) {
	var position int64
	var cursor, eventID string
	err := transaction.QueryRow(ctx, `
SELECT global_position, cursor, last_event_id
FROM wanwork_im.event_projection_checkpoints
WHERE tenant_id = $1 AND workspace_id = $2 AND projection_id = $3`,
		scope.TenantID, workspaceText(scope.WorkspaceID), scope.ProjectionID,
	).Scan(&position, &cursor, &eventID)
	if errors.Is(err, pgx.ErrNoRows) {
		return events.ProjectionCheckpoint{Scope: cloneProjectionScope(scope)}, nil
	}
	if err != nil || position < 0 {
		return events.ProjectionCheckpoint{}, store.ErrStoreUnavailable
	}
	checkpoint := events.ProjectionCheckpoint{Scope: cloneProjectionScope(scope), Position: uint64(position), Cursor: events.Cursor(cursor), LastEventID: eventID}
	if position == 0 && (cursor != "" || eventID != "") || position > 0 && (cursor == "" || eventID == "") {
		return events.ProjectionCheckpoint{}, ErrProjectorIntegrity
	}
	return checkpoint, nil
}

func commitProjectorCheckpoint(
	ctx context.Context,
	transaction pgx.Tx,
	previous, next events.ProjectionCheckpoint,
) error {
	if next.Position > math.MaxInt64 || previous.Position > math.MaxInt64 {
		return ErrProjectorIntegrity
	}
	var written bool
	if err := transaction.QueryRow(ctx, `
SELECT wanwork_im.write_projection_checkpoint(
    $1, $2, $3, $4, $5, $6, $7, $8, $9
)`,
		next.Scope.TenantID, workspaceText(next.Scope.WorkspaceID), next.Scope.ProjectionID,
		int64(previous.Position), string(previous.Cursor), previous.LastEventID,
		int64(next.Position), string(next.Cursor), next.LastEventID,
	).Scan(&written); err != nil {
		return mapProjectorWriteError(ctx, err)
	}
	if !written {
		return ErrProjectorConflict
	}
	return nil
}

func messageIDFromProjectionEvent(event events.StoredEvent) (string, error) {
	var payload struct {
		MessageID string `json:"messageId"`
	}
	if event.Payload.Kind() != events.PayloadInline {
		return "", ErrProjectorIntegrity
	}
	decoder := json.NewDecoder(bytes.NewReader(event.Payload.InlineJSON()))
	if err := decoder.Decode(&payload); err != nil || payload.MessageID == "" {
		return "", ErrProjectorIntegrity
	}
	var trailing any
	if err := decoder.Decode(&trailing); !errors.Is(err, io.EOF) {
		return "", ErrProjectorIntegrity
	}
	return payload.MessageID, nil
}

func isMessageProjectionEvent(eventType string) bool {
	return eventType == "message.created" || eventType == "message.edited" || eventType == "message.recalled"
}

func cloneWorkspace(value *string) *string {
	if value == nil {
		return nil
	}
	cloned := *value
	return &cloned
}

func cloneProjectionScope(scope events.ProjectionScope) events.ProjectionScope {
	return events.ProjectionScope{TenantID: scope.TenantID, WorkspaceID: cloneWorkspace(scope.WorkspaceID), ProjectionID: scope.ProjectionID}
}

func workspaceText(value *string) string {
	if value == nil {
		return ""
	}
	return *value
}

func mapProjectorEventError(err error) error {
	if errors.Is(err, events.ErrInvalidQuery) || errors.Is(err, events.ErrInvalidCursor) {
		return ErrProjectorIntegrity
	}
	if errors.Is(err, events.ErrStoreUnavailable) || errors.Is(err, events.ErrStoreCapacity) {
		return store.ErrStoreUnavailable
	}
	return ErrProjectorIntegrity
}

func mapProjectorWriteError(ctx context.Context, err error) error {
	if ctx != nil && ctx.Err() != nil {
		return ctx.Err()
	}
	if errors.Is(err, pgx.ErrTxClosed) {
		return store.ErrStoreUnavailable
	}
	return store.ErrStoreUnavailable
}

func rollbackProjectorTransaction(transaction pgx.Tx) {
	if transaction == nil {
		return
	}
	_ = transaction.Rollback(context.Background())
}
