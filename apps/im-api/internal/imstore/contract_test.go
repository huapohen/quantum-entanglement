package imstore

import (
	"errors"
	"strings"
	"testing"
	"time"
)

func TestSHA256DigestRequiresCanonicalLowercaseHex(t *testing.T) {
	want := DigestBytes([]byte("wanwork command"))
	got, err := ParseSHA256Digest(want.Hex())
	if err != nil || got != want {
		t.Fatalf("ParseSHA256Digest() = (%s, %v), want %s", got.Hex(), err, want.Hex())
	}
	for _, value := range []string{
		"",
		strings.Repeat("0", 63),
		strings.Repeat("0", 65),
		strings.ToUpper(want.Hex()),
		strings.Repeat("g", 64),
	} {
		got, err := ParseSHA256Digest(value)
		if !errors.Is(err, ErrInvalidRequest) || got != (SHA256Digest{}) {
			t.Fatalf("ParseSHA256Digest(%q) = (%#v, %v)", value, got, err)
		}
	}
}

func TestCommandIdentityFreezesSQLCompatibleKeyGrammar(t *testing.T) {
	digest := DigestBytes([]byte("request"))
	identity, err := NewCommandIdentity(
		"conversation.authority_create",
		"request:2026-08-28.alpha",
		digest,
	)
	if err != nil {
		t.Fatalf("NewCommandIdentity(): %v", err)
	}
	if identity.Kind() != "conversation.authority_create" ||
		identity.IdempotencyKey() != "request:2026-08-28.alpha" ||
		identity.RequestDigest() != digest || identity.IsZero() {
		t.Fatalf("unexpected command identity: %#v", identity)
	}

	for _, fixture := range []struct {
		kind string
		key  string
	}{
		{kind: "", key: "key"},
		{kind: "Conversation.Create", key: "key"},
		{kind: "conversation..create", key: "key"},
		{kind: strings.Repeat("a", 65), key: "key"},
		{kind: "conversation.create", key: ""},
		{kind: "conversation.create", key: "-key"},
		{kind: "conversation.create", key: "key-"},
		{kind: "conversation.create", key: strings.Repeat("a", 129)},
	} {
		got, err := NewCommandIdentity(fixture.kind, fixture.key, digest)
		if !errors.Is(err, ErrInvalidRequest) || !got.IsZero() {
			t.Fatalf("NewCommandIdentity(%q, %q) = (%#v, %v)", fixture.kind, fixture.key, got, err)
		}
	}
}

func TestAgentStoreCommandReservesAgentNamespace(t *testing.T) {
	t.Parallel()
	digest := DigestBytes([]byte("agent install request"))
	command, err := NewAgentStoreCommand("agent.install", "install-1", digest)
	if err != nil || command.Kind() != "agent.install" || command.RequestDigest() != digest {
		t.Fatalf("agent command = %#v, %v", command, err)
	}
	for _, kind := range []string{"conversation.create", "agent", "agent/unsafe"} {
		if command, err := NewAgentStoreCommand(kind, "install-1", digest); !errors.Is(err, ErrInvalidRequest) || !command.IsZero() {
			t.Fatalf("NewAgentStoreCommand(%q) = %#v, %v", kind, command, err)
		}
	}
}

func TestCommitReceiptSeparatesFreshReplayAndUnknownResolution(t *testing.T) {
	command, err := NewCommandIdentity("conversation.create", "create-alpha", DigestBytes([]byte("request")))
	if err != nil {
		t.Fatalf("NewCommandIdentity(): %v", err)
	}
	committedAt := time.Date(2026, 8, 28, 12, 0, 0, 123, time.UTC)
	result := DigestBytes([]byte("result"))

	for _, fixture := range []struct {
		name     string
		replayed bool
		resolved bool
	}{
		{name: "fresh"},
		{name: "replay", replayed: true},
		{name: "resolved", replayed: true, resolved: true},
	} {
		t.Run(fixture.name, func(t *testing.T) {
			receipt, err := NewCommitReceipt(
				command,
				result,
				committedAt,
				fixture.replayed,
				fixture.resolved,
			)
			if err != nil || receipt.Command() != command || receipt.ResultDigest() != result ||
				receipt.CommittedAt() != committedAt || receipt.Replayed() != fixture.replayed ||
				receipt.ResolvedAfterUnknown() != fixture.resolved || receipt.IsZero() {
				t.Fatalf("unexpected receipt: %#v, %v", receipt, err)
			}
		})
	}

	invalid, err := NewCommitReceipt(command, result, committedAt, false, true)
	if !errors.Is(err, ErrInvalidRequest) || !invalid.IsZero() {
		t.Fatalf("resolved fresh receipt = (%#v, %v)", invalid, err)
	}
}
