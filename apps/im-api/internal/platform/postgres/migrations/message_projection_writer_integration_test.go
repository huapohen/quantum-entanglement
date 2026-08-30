package migrations

import (
	"context"
	"os"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
)

func TestMessageProjectionWriterAgainstPostgres(t *testing.T) {
	adminURL := os.Getenv(postgresIntegrationURL)
	if adminURL == "" {
		t.Skip(postgresIntegrationURL + " is not set")
	}
	connection, _ := newIntegrationDatabase(t, adminURL)
	if _, err := Apply(t.Context(), connection); err != nil {
		t.Fatalf("apply migrations: %v", err)
	}
	ctx, cancel := context.WithTimeout(t.Context(), 15*time.Second)
	defer cancel()
	transaction, err := connection.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.Serializable})
	if err != nil {
		t.Fatalf("begin writer transaction: %v", err)
	}
	defer func() { _ = transaction.Rollback(context.Background()) }()
	if _, err := transaction.Exec(ctx, "SELECT pg_catalog.set_config('wanwork.tenant_id', 'ten_alpha', true)"); err != nil {
		t.Fatalf("bind tenant: %v", err)
	}
	if _, err := transaction.Exec(ctx, `
INSERT INTO wanwork_im.tenants (tenant_id, status, revision)
VALUES ('ten_alpha', 'active', 1)`); err != nil {
		t.Fatalf("seed tenant: %v", err)
	}
	createdAt := time.Date(2026, 8, 30, 12, 0, 0, 123000, time.UTC)
	if !callMessageProjectionWriter(t, ctx, transaction,
		0, 0, 0, 1, 1, 1,
		"msg_1", "msg_client_1", "usr_alice", "text", "active", "hello", "", createdAt, 1, 1, 1, 1) {
		t.Fatal("first message projection write rejected")
	}
	if !callMessageProjectionWriter(t, ctx, transaction,
		0, 0, 0, 1, 1, 1,
		"msg_1", "msg_client_1", "usr_alice", "text", "active", "hello", "", createdAt, 1, 1, 1, 1) {
		t.Fatal("exact message projection replay rejected")
	}
	if !callMessageProjectionWriter(t, ctx, transaction,
		1, 1, 1, 2, 2, 2,
		"msg_1", "msg_client_1", "usr_alice", "text", "edited", "hello again", "", createdAt, 2, 2, 2, 2) {
		t.Fatal("message projection edit rejected")
	}
	if callMessageProjectionWriter(t, ctx, transaction,
		0, 0, 0, 1, 1, 1,
		"msg_1", "msg_client_1", "usr_alice", "text", "active", "hello", "", createdAt, 1, 1, 1, 1) {
		t.Fatal("stale message projection write unexpectedly accepted")
	}
	var headSequence, headPosition, headRevision int64
	if err := transaction.QueryRow(ctx, `
SELECT current_sequence, current_global_position, current_revision
FROM wanwork_im.message_projection_heads
WHERE tenant_id = 'ten_alpha' AND workspace_id = ''
  AND conversation_id = 'cnv_room' AND projection_id = 'messages-v1'`).Scan(
		&headSequence, &headPosition, &headRevision,
	); err != nil {
		t.Fatalf("read projection head: %v", err)
	}
	var status, text string
	if err := transaction.QueryRow(ctx, `
SELECT status, text
FROM wanwork_im.message_snapshots
WHERE tenant_id = 'ten_alpha' AND workspace_id = ''
  AND conversation_id = 'cnv_room' AND message_id = 'msg_1'`).Scan(&status, &text); err != nil {
		t.Fatalf("read message snapshot: %v", err)
	}
	if headSequence != 2 || headPosition != 2 || headRevision != 2 || status != "edited" || text != "hello again" {
		t.Fatalf("projection state head=(%d,%d,%d) snapshot=(%q,%q)", headSequence, headPosition, headRevision, status, text)
	}
	if err := transaction.Commit(ctx); err != nil {
		t.Fatalf("commit writer transaction: %v", err)
	}
}

func callMessageProjectionWriter(
	t *testing.T,
	ctx context.Context,
	transaction pgx.Tx,
	expectedSequence, expectedPosition, expectedRevision,
	nextSequence, nextPosition, nextRevision int64,
	messageID, clientMessageID, senderActorID, messageType, status, text, extInfo string,
	createdAt time.Time, messageRevision, lastEventSequence, lastEventPosition, projectionRevision int64,
) bool {
	t.Helper()
	var written bool
	err := transaction.QueryRow(ctx, `
SELECT wanwork_im.write_message_projection(
    'ten_alpha', '', 'cnv_room', 'messages-v1', $1, $2, $3, $4, $5, $6,
    $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18
)`, expectedSequence, expectedPosition, expectedRevision, nextSequence, nextPosition, nextRevision,
		messageID, clientMessageID, senderActorID, messageType, status, text, extInfo, createdAt,
		messageRevision, lastEventSequence, lastEventPosition, projectionRevision).Scan(&written)
	if err != nil {
		t.Fatalf("call writer: %v", err)
	}
	return written
}
