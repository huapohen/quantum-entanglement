package imstore

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
)

func testProviderEffectIntent(t *testing.T) ProviderEffectIntent {
	t.Helper()
	createdAt := time.Date(2026, 8, 30, 15, 0, 0, 0, time.UTC)
	return ProviderEffectIntent{
		TenantID: "ten_outbox", WorkspaceID: stringPtr("wsp_outbox"), InstallationID: "ins_outbox",
		EffectID: "eff_outbox_1", EffectKind: ProviderEffectUserProvision,
		Provider: "rongcloud", ProviderRealmID: "rlm_outbox", ProviderSubjectID: "agt_outbox",
		OperationKey: "agent/install/outbox-1", RequestRef: "install/request/outbox-1",
		RequestDigest: DigestBytes([]byte("provider effect request")), CreatedAt: createdAt,
	}
}

func stringPtr(value string) *string { return &value }

func testProviderEffectReceipt() im.ProviderEffectReceipt {
	return im.ProviderEffectReceipt{
		OperationKey: "agent/install/outbox-1", ExternalID: "agt_outbox",
		Status:     im.ProviderEffectCommitted,
		ObservedAt: time.Date(2026, 8, 30, 15, 0, 2, 0, time.UTC),
	}
}

func TestDurableProviderEffectOutboxReplaysAndResolvesUnknownAfterRestart(t *testing.T) {
	t.Parallel()
	path := filepath.Join(t.TempDir(), "provider-effects.log")
	now := time.Date(2026, 8, 30, 15, 0, 1, 0, time.UTC)
	clock := func(context.Context) time.Time { return now }
	store, err := OpenDurableProviderEffectFileStore(t.Context(), path, clock)
	if err != nil {
		t.Fatal(err)
	}
	intent := testProviderEffectIntent(t)
	first, replayed, err := store.Enqueue(t.Context(), intent)
	if err != nil || replayed || first.State != ProviderEffectQueued || first.AttemptCount != 0 {
		t.Fatalf("enqueue = %#v, replayed=%v, err=%v", first, replayed, err)
	}
	replay, replayed, err := store.Enqueue(t.Context(), intent)
	if err != nil || !replayed || replay.State != ProviderEffectQueued {
		t.Fatalf("enqueue replay = %#v, replayed=%v, err=%v", replay, replayed, err)
	}
	changed := intent
	changed.RequestDigest = DigestBytes([]byte("changed request"))
	if _, _, err := store.Enqueue(t.Context(), changed); !errors.Is(err, ErrProviderEffectConflict) {
		t.Fatalf("digest drift = %v, want conflict", err)
	}
	claims, err := store.ClaimDue(t.Context(), intent.TenantID, "worker-a", time.Minute, 1)
	if err != nil || len(claims) != 1 || claims[0].Record.State != ProviderEffectSent || claims[0].Record.AttemptCount != 1 || claims[0].LeaseToken == "" {
		t.Fatalf("claim = %#v, err=%v", claims, err)
	}
	if _, err := store.MarkUnknown(t.Context(), intent.Key(), "wrong-lease", "provider-timeout"); !errors.Is(err, ErrProviderEffectLease) {
		t.Fatalf("wrong lease = %v, want lease error", err)
	}
	unknown, err := store.MarkUnknown(t.Context(), intent.Key(), claims[0].LeaseToken, "provider-timeout")
	if err != nil || unknown.State != ProviderEffectUnknown || unknown.LastErrorCode != "provider-timeout" {
		t.Fatalf("mark unknown = %#v, err=%v", unknown, err)
	}
	if due, err := store.ClaimDue(t.Context(), intent.TenantID, "worker-b", time.Minute, 1); err != nil || len(due) != 0 {
		t.Fatalf("unknown must not blind-retry: due=%#v, err=%v", due, err)
	}
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}
	reopened, err := OpenDurableProviderEffectFileStore(t.Context(), path, clock)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = reopened.Close() })
	loaded, err := reopened.Load(t.Context(), intent.Key())
	if err != nil || loaded.State != ProviderEffectUnknown || loaded.AttemptCount != 1 {
		t.Fatalf("unknown after restart = %#v, err=%v", loaded, err)
	}
	resolved, err := reopened.ResolveUnknown(t.Context(), intent.Key(), testProviderEffectReceipt())
	if err != nil || resolved.State != ProviderEffectCommitted || resolved.ProviderReceipt == nil || resolved.CommittedAt.IsZero() {
		t.Fatalf("resolved = %#v, err=%v", resolved, err)
	}
	final, err := reopened.Load(t.Context(), intent.Key())
	if err != nil || final.State != ProviderEffectCommitted || final.ProviderReceipt.ExternalID != "agt_outbox" {
		t.Fatalf("final durable record = %#v, err=%v", final, err)
	}
}

func TestDurableProviderEffectOutboxFailedClaimCanRetryWithNewLease(t *testing.T) {
	t.Parallel()
	path := filepath.Join(t.TempDir(), "provider-effects-retry.log")
	clock := func(context.Context) time.Time { return time.Date(2026, 8, 30, 16, 0, 0, 0, time.UTC) }
	store, err := OpenDurableProviderEffectFileStore(t.Context(), path, clock)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = store.Close() })
	intent := testProviderEffectIntent(t)
	if _, _, err := store.Enqueue(t.Context(), intent); err != nil {
		t.Fatal(err)
	}
	first, err := store.ClaimDue(t.Context(), intent.TenantID, "worker-a", time.Minute, 1)
	if err != nil || len(first) != 1 {
		t.Fatalf("first claim = %#v, err=%v", first, err)
	}
	failed, err := store.MarkFailed(t.Context(), intent.Key(), first[0].LeaseToken, "provider-rejected")
	if err != nil || failed.State != ProviderEffectFailed || failed.AttemptCount != 1 {
		t.Fatalf("failed = %#v, err=%v", failed, err)
	}
	second, err := store.ClaimDue(t.Context(), intent.TenantID, "worker-b", time.Minute, 1)
	if err != nil || len(second) != 1 || second[0].Record.AttemptCount != 2 || second[0].LeaseToken == first[0].LeaseToken {
		t.Fatalf("retry claim = %#v, err=%v", second, err)
	}
}

func TestDurableProviderEffectOutboxDiscardsOnlyInterruptedTail(t *testing.T) {
	t.Parallel()
	path := filepath.Join(t.TempDir(), "provider-effects-tail.log")
	clock := func(context.Context) time.Time { return time.Date(2026, 8, 30, 17, 0, 0, 0, time.UTC) }
	store, err := OpenDurableProviderEffectFileStore(t.Context(), path, clock)
	if err != nil {
		t.Fatal(err)
	}
	intent := testProviderEffectIntent(t)
	if _, _, err := store.Enqueue(t.Context(), intent); err != nil {
		t.Fatal(err)
	}
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := file.WriteString(`{"format":"quantum-entanglement.provider-effect-outbox/1"}`); err != nil {
		t.Fatal(err)
	}
	if err := file.Close(); err != nil {
		t.Fatal(err)
	}
	reopened, err := OpenDurableProviderEffectFileStore(t.Context(), path, clock)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = reopened.Close() })
	loaded, err := reopened.Load(t.Context(), intent.Key())
	if err != nil || loaded.State != ProviderEffectQueued {
		t.Fatalf("load after interrupted tail = %#v, err=%v", loaded, err)
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Count(string(raw), "\n") != 1 || strings.Contains(string(raw), `{"format":"quantum-entanglement.provider-effect-outbox/1"}`) {
		t.Fatalf("tail was not truncated: %q", raw)
	}
}
