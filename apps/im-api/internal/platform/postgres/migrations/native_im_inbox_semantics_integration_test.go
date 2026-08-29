package migrations

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
)

func TestNativeIMInboxSemanticsRejectsFormatValidToxicPayloads(t *testing.T) {
	adminURL := os.Getenv(postgresIntegrationURL)
	if adminURL == "" {
		t.Skip(postgresIntegrationURL + " is not set")
	}
	connection, _ := newIntegrationDatabase(t, adminURL)
	ctx, cancel := context.WithTimeout(t.Context(), 15*time.Second)
	defer cancel()
	if _, err := Apply(ctx, connection); err != nil {
		t.Fatalf("Apply native IM inbox semantics: %v", err)
	}

	seedTx, err := connection.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.Serializable})
	if err != nil {
		t.Fatalf("begin semantic transaction: %v", err)
	}
	if _, err := setTenantContext(ctx, seedTx, "ten_semantics"); err != nil {
		t.Fatalf("set tenant context: %v", err)
	}
	if _, err := seedTx.Exec(ctx, `
INSERT INTO wanwork_im.tenants (tenant_id, status, revision)
VALUES ('ten_semantics', 'active', 1)`); err != nil {
		t.Fatalf("seed tenant: %v", err)
	}
	if err := seedTx.Commit(ctx); err != nil {
		t.Fatalf("commit tenant seed: %v", err)
	}

	transaction, err := connection.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.Serializable})
	if err != nil {
		t.Fatalf("begin toxic semantic transaction: %v", err)
	}
	defer func() { _ = transaction.Rollback(context.Background()) }()
	if _, err := setTenantContext(ctx, transaction, "ten_semantics"); err != nil {
		t.Fatalf("set toxic tenant context: %v", err)
	}

	const payloadInline = `{"message":"hello"}`
	validPayloadDigest := sha256.Sum256([]byte(payloadInline))
	validPayloadDigestText := "sha256:" + hex.EncodeToString(validPayloadDigest[:])
	args := []any{
		"ten_semantics", "", "rongcloud", "channel_semantics", "event_semantics",
		"sha256:" + strings.Repeat("a", 64), "verification_semantics", "inline", payloadInline,
		"", "", int64(-1), validPayloadDigestText,
	}
	toxicArgs := append([]any(nil), args...)
	toxicArgs[12] = "sha256:" + strings.Repeat("0", 64)
	var status string
	if err := transaction.QueryRow(ctx, `
SELECT wanwork_im.admit_native_im_inbox(
    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13
)`, toxicArgs...).Scan(&status); !hasPostgresCode(err, "22023") {
		t.Fatalf("toxic zero payload digest error = %v, want SQLSTATE 22023", err)
	}
	if err := transaction.Rollback(ctx); err != nil {
		t.Fatalf("rollback toxic transaction: %v", err)
	}

	validTx, err := connection.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.Serializable})
	if err != nil {
		t.Fatalf("begin valid semantic transaction: %v", err)
	}
	defer func() { _ = validTx.Rollback(context.Background()) }()
	if _, err := setTenantContext(ctx, validTx, "ten_semantics"); err != nil {
		t.Fatalf("set valid tenant context: %v", err)
	}
	if err := validTx.QueryRow(ctx, `
SELECT wanwork_im.admit_native_im_inbox(
    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13
)`, args...).Scan(&status); err != nil || status != "inserted" {
		t.Fatalf("valid semantic admission = %q/%v", status, err)
	}
	if err := validTx.Commit(ctx); err != nil {
		t.Fatalf("commit valid semantic admission: %v", err)
	}
}
