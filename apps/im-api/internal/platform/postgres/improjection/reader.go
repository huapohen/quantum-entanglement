// Package improjection contains the inactive PostgreSQL materialized message reader. It is kept
// separate from the event-replay bridge so cutover can be shadowed and rolled back without
// changing the public MessageReadRepository contract.
package improjection

import (
	"context"
	"encoding/base64"
	"errors"
	"strconv"
	"strings"
	"time"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
	store "github.com/huapohen/quantum-entanglement/apps/im-api/internal/imstore"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/platform/postgres/runtimepool"
	"github.com/jackc/pgx/v5"
)

const (
	projectionID        = "messages-v1"
	maxPageSize         = 256
	maxCursorBytes      = 2048
	materializedVersion = "wanwork-materialized-message-v1"
)

// Reader reads only rows produced by the future materialized message projector. It does not
// perform writes, fall back to replay, or accept caller-provided projection coordinates.
type Reader struct {
	pool *runtimepool.Pool
}

var _ store.MessageReadRepository = (*Reader)(nil)

func NewReader(pool *runtimepool.Pool) (*Reader, error) {
	if pool == nil {
		return nil, store.ErrInvalidRequest
	}
	return &Reader{pool: pool}, nil
}

func (reader *Reader) ReadPage(
	ctx context.Context,
	query store.MessageReadPageQuery,
) (store.MessageReadPage, error) {
	if ctx == nil || ctx.Err() != nil || reader == nil || reader.pool == nil ||
		query.Conversation.IsZero() || query.ConversationRevision == 0 ||
		query.AccessRevision == 0 || query.Limit == 0 || query.Limit > maxPageSize {
		return store.MessageReadPage{}, store.ErrInvalidRequest
	}
	workspace := materializedWorkspaceValue(query.WorkspaceID)
	cursor, err := decodeCursor(query.AfterCursor, materializedCursorBinding{
		tenantID: query.Conversation.TenantID().String(), workspaceID: workspace,
		workspaceSet: query.WorkspaceID != nil, conversationID: query.Conversation.ConversationID().String(),
	})
	if err != nil {
		return store.MessageReadPage{}, err
	}
	connection, err := reader.pool.Acquire(ctx)
	if err != nil {
		return store.MessageReadPage{}, store.ErrStoreUnavailable
	}
	defer connection.Release()
	transaction, err := connection.BeginTx(ctx, pgx.TxOptions{
		IsoLevel: pgx.RepeatableRead, AccessMode: pgx.ReadOnly,
	})
	if err != nil {
		return store.MessageReadPage{}, store.ErrStoreUnavailable
	}
	defer rollbackMaterializedTransaction(transaction)
	if err := bindMaterializedTenant(ctx, transaction, query.Conversation.TenantID().String()); err != nil {
		return store.MessageReadPage{}, err
	}
	var projectionRevision, sequence int64
	err = transaction.QueryRow(ctx, `
SELECT current_revision, current_sequence
FROM wanwork_im.message_projection_heads
WHERE tenant_id = $1 AND workspace_id = $2 AND conversation_id = $3 AND projection_id = $4`,
		query.Conversation.TenantID().String(), workspace,
		query.Conversation.ConversationID().String(), projectionID,
	).Scan(&projectionRevision, &sequence)
	if errors.Is(err, pgx.ErrNoRows) {
		if query.AfterCursor != "" {
			return store.MessageReadPage{}, store.ErrRevisionConflict
		}
		if err := transaction.Commit(ctx); err != nil {
			return store.MessageReadPage{}, store.ErrStoreUnavailable
		}
		return store.MessageReadPage{
			Conversation: query.Conversation, ConversationRevision: query.ConversationRevision,
		}, nil
	}
	if err != nil || projectionRevision <= 0 || sequence < 0 {
		return store.MessageReadPage{}, store.ErrIntegrity
	}
	if query.AfterCursor != "" && cursor.projectionRevision != uint64(projectionRevision) {
		return store.MessageReadPage{}, store.ErrRevisionConflict
	}
	args := []any{
		query.Conversation.TenantID().String(), workspace,
		query.Conversation.ConversationID().String(),
	}
	whereAfter := ""
	if query.AfterCursor != "" {
		whereAfter = " AND (created_at, message_id) > ($4, $5)"
		args = append(args, cursor.createdAt, cursor.messageID)
	}
	args = append(args, int64(query.Limit)+1)
	rows, err := transaction.Query(ctx, `
SELECT message_id, client_message_id, sender_actor_id, message_type, status,
       text, ext_info, created_at, revision, last_event_sequence, last_event_position,
       projection_revision
FROM wanwork_im.message_snapshots
WHERE tenant_id = $1 AND workspace_id = $2 AND conversation_id = $3`+whereAfter+`
ORDER BY created_at, message_id
LIMIT $`+strconv.Itoa(len(args)), args...)
	if err != nil {
		return store.MessageReadPage{}, store.ErrStoreUnavailable
	}
	defer rows.Close()
	messages := make([]im.MessageSnapshot, 0, query.Limit)
	for rows.Next() {
		var messageID, clientMessageID, senderActorID, messageType, status string
		var text, extInfo string
		var createdAt time.Time
		var revision, lastSequence, lastPosition, rowProjectionRevision int64
		if err := rows.Scan(
			&messageID, &clientMessageID, &senderActorID, &messageType, &status,
			&text, &extInfo, &createdAt, &revision, &lastSequence, &lastPosition,
			&rowProjectionRevision,
		); err != nil {
			return store.MessageReadPage{}, store.ErrIntegrity
		}
		message, err := materializedMessageSnapshot(
			query.Conversation, messageID, clientMessageID, senderActorID,
			messageType, status, text, extInfo, createdAt, revision,
		)
		if err != nil || rowProjectionRevision <= 0 || rowProjectionRevision > projectionRevision ||
			lastSequence <= 0 || lastPosition <= 0 || lastSequence > sequence {
			return store.MessageReadPage{}, store.ErrIntegrity
		}
		messages = append(messages, message)
	}
	if err := rows.Err(); err != nil {
		return store.MessageReadPage{}, store.ErrStoreUnavailable
	}
	hasMore := len(messages) > int(query.Limit)
	if hasMore {
		messages = messages[:query.Limit]
	}
	next := ""
	if hasMore {
		last := messages[len(messages)-1]
		next, err = encodeCursor(materializedCursorBinding{
			tenantID: query.Conversation.TenantID().String(), workspaceID: workspace,
			workspaceSet: query.WorkspaceID != nil, conversationID: query.Conversation.ConversationID().String(),
		}, materializedCursor{
			projectionRevision: uint64(projectionRevision), createdAt: last.CreatedAt().UTC(),
			messageID: last.Ref().MessageID().String(),
		})
		if err != nil {
			return store.MessageReadPage{}, store.ErrIntegrity
		}
	}
	if err := transaction.Commit(ctx); err != nil {
		return store.MessageReadPage{}, store.ErrStoreUnavailable
	}
	return store.MessageReadPage{
		Conversation: query.Conversation, Messages: messages, NextCursor: next, HasMore: hasMore,
		ConversationRevision: query.ConversationRevision, ProjectionRevision: uint64(projectionRevision),
	}, nil
}

func materializedMessageSnapshot(
	conversation im.ConversationRef,
	messageID, clientMessageID, senderActorID, messageType, status, text, extInfo string,
	createdAt time.Time, revision int64,
) (im.MessageSnapshot, error) {
	if revision <= 0 || revision > int64(^uint64(0)>>1) || senderActorID == "" ||
		createdAt.IsZero() {
		return im.MessageSnapshot{}, store.ErrIntegrity
	}
	parsedMessageID, err := im.ParseMessageID(messageID)
	if err != nil {
		return im.MessageSnapshot{}, store.ErrIntegrity
	}
	parsedClientID, err := im.ParseMessageID(clientMessageID)
	if err != nil {
		return im.MessageSnapshot{}, store.ErrIntegrity
	}
	parsedActorID, err := im.ParseActorID(senderActorID)
	if err != nil {
		return im.MessageSnapshot{}, store.ErrIntegrity
	}
	actor, err := im.NewActorRef(conversation.TenantID(), parsedActorID)
	if err != nil {
		return im.MessageSnapshot{}, store.ErrIntegrity
	}
	reference, err := im.NewMessageRef(conversation, parsedMessageID)
	if err != nil {
		return im.MessageSnapshot{}, store.ErrIntegrity
	}
	snapshot, err := im.NewMessageSnapshot(
		reference, actor, parsedClientID, im.MessageType(messageType), im.MessageStatus(status),
		text, extInfo, createdAt.UTC(), uint64(revision),
	)
	if err != nil {
		return im.MessageSnapshot{}, store.ErrIntegrity
	}
	return snapshot, nil
}

type materializedCursorBinding struct {
	tenantID, workspaceID, conversationID string
	workspaceSet                          bool
}

type materializedCursor struct {
	projectionRevision uint64
	createdAt          time.Time
	messageID          string
}

func encodeCursor(binding materializedCursorBinding, cursor materializedCursor) (string, error) {
	if binding.tenantID == "" || binding.conversationID == "" || cursor.projectionRevision == 0 ||
		cursor.createdAt.IsZero() || cursor.messageID == "" {
		return "", store.ErrIntegrity
	}
	set := "0"
	if binding.workspaceSet {
		set = "1"
	}
	raw := strings.Join([]string{
		materializedVersion, binding.tenantID, set, binding.workspaceID, binding.conversationID,
		strconv.FormatUint(cursor.projectionRevision, 10), cursor.createdAt.UTC().Format(time.RFC3339Nano), cursor.messageID,
	}, "\n")
	return base64.RawURLEncoding.EncodeToString([]byte(raw)), nil
}

func decodeCursor(raw string, binding materializedCursorBinding) (materializedCursor, error) {
	if raw == "" {
		return materializedCursor{}, nil
	}
	if len(raw) > maxCursorBytes || strings.TrimSpace(raw) != raw {
		return materializedCursor{}, store.ErrInvalidRequest
	}
	decoded, err := base64.RawURLEncoding.Strict().DecodeString(raw)
	if err != nil {
		return materializedCursor{}, store.ErrInvalidRequest
	}
	parts := strings.Split(string(decoded), "\n")
	if len(parts) != 8 || parts[0] != materializedVersion || parts[1] != binding.tenantID ||
		parts[3] != binding.workspaceID || parts[4] != binding.conversationID ||
		(parts[2] == "1") != binding.workspaceSet || (parts[2] != "0" && parts[2] != "1") {
		return materializedCursor{}, store.ErrInvalidRequest
	}
	projectionRevision, err := strconv.ParseUint(parts[5], 10, 64)
	if err != nil || projectionRevision == 0 || strconv.FormatUint(projectionRevision, 10) != parts[5] {
		return materializedCursor{}, store.ErrInvalidRequest
	}
	createdAt, err := time.Parse(time.RFC3339Nano, parts[6])
	if err != nil || createdAt.Location() != time.UTC || parts[7] == "" {
		return materializedCursor{}, store.ErrInvalidRequest
	}
	if _, err := im.ParseMessageID(parts[7]); err != nil {
		return materializedCursor{}, store.ErrInvalidRequest
	}
	return materializedCursor{projectionRevision: projectionRevision, createdAt: createdAt, messageID: parts[7]}, nil
}

func materializedWorkspaceValue(workspace *im.WorkspaceID) string {
	if workspace == nil {
		return ""
	}
	return workspace.String()
}

func bindMaterializedTenant(ctx context.Context, transaction pgx.Tx, tenantID string) error {
	if _, err := transaction.Exec(ctx, "SET LOCAL search_path = pg_catalog"); err != nil {
		return store.ErrStoreUnavailable
	}
	var recorded string
	if err := transaction.QueryRow(ctx,
		"SELECT pg_catalog.set_config('wanwork.tenant_id', $1, true)", tenantID,
	).Scan(&recorded); err != nil || recorded != tenantID {
		return store.ErrStoreUnavailable
	}
	return nil
}

func rollbackMaterializedTransaction(transaction pgx.Tx) {
	if transaction == nil {
		return
	}
	_ = transaction.Rollback(context.Background())
}
