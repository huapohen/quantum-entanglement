package imstore

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"regexp"
	"time"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
)

var (
	ErrInvalidRequest         = errors.New("invalid IM store request")
	ErrNotFound               = errors.New("IM store value not found")
	ErrRevisionConflict       = errors.New("IM store revision conflict")
	ErrIdempotencyConflict    = errors.New("IM store idempotency conflict")
	ErrIntegrity              = errors.New("IM store integrity failure")
	ErrStoreUnavailable       = errors.New("IM store unavailable")
	ErrCommitOutcomeUnknown   = errors.New("IM store commit outcome unknown")
	ErrPersistenceUnsupported = errors.New("IM value is not supported by this persistence version")
	ErrTransactionClosed      = errors.New("IM store transaction is closed")

	commandKindPattern = regexp.MustCompile(`^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$`)
	idempotencyPattern = regexp.MustCompile(`^[A-Za-z0-9](?:[A-Za-z0-9_.:-]{0,126}[A-Za-z0-9])?$`)
)

type SHA256Digest [sha256.Size]byte

func ParseSHA256Digest(value string) (SHA256Digest, error) {
	if len(value) != sha256.Size*2 {
		return SHA256Digest{}, ErrInvalidRequest
	}
	decoded, err := hex.DecodeString(value)
	if err != nil || hex.EncodeToString(decoded) != value {
		return SHA256Digest{}, ErrInvalidRequest
	}
	var digest SHA256Digest
	copy(digest[:], decoded)
	return digest, nil
}

func DigestBytes(value []byte) SHA256Digest { return sha256.Sum256(value) }

func (digest SHA256Digest) Hex() string { return hex.EncodeToString(digest[:]) }

type CommandIdentity struct {
	kind           string
	idempotencyKey string
	requestDigest  SHA256Digest
}

func NewCommandIdentity(
	kind string,
	idempotencyKey string,
	requestDigest SHA256Digest,
) (CommandIdentity, error) {
	if len(kind) == 0 || len(kind) > 64 || !commandKindPattern.MatchString(kind) ||
		len(idempotencyKey) == 0 || len(idempotencyKey) > 128 ||
		!idempotencyPattern.MatchString(idempotencyKey) {
		return CommandIdentity{}, ErrInvalidRequest
	}
	return CommandIdentity{
		kind: kind, idempotencyKey: idempotencyKey, requestDigest: requestDigest,
	}, nil
}

func (identity CommandIdentity) Kind() string                { return identity.kind }
func (identity CommandIdentity) IdempotencyKey() string      { return identity.idempotencyKey }
func (identity CommandIdentity) RequestDigest() SHA256Digest { return identity.requestDigest }
func (identity CommandIdentity) IsZero() bool {
	return identity.kind == "" && identity.idempotencyKey == "" &&
		identity.requestDigest == SHA256Digest{}
}

type CommitReceipt struct {
	command      CommandIdentity
	resultDigest SHA256Digest
	committedAt  time.Time
	replayed     bool
	resolved     bool
}

func NewCommitReceipt(
	command CommandIdentity,
	resultDigest SHA256Digest,
	committedAt time.Time,
	replayed bool,
	resolved bool,
) (CommitReceipt, error) {
	if command.IsZero() || committedAt.IsZero() || (resolved && !replayed) {
		return CommitReceipt{}, ErrInvalidRequest
	}
	return CommitReceipt{
		command: command, resultDigest: resultDigest, committedAt: committedAt,
		replayed: replayed, resolved: resolved,
	}, nil
}

func (receipt CommitReceipt) Command() CommandIdentity   { return receipt.command }
func (receipt CommitReceipt) ResultDigest() SHA256Digest { return receipt.resultDigest }
func (receipt CommitReceipt) CommittedAt() time.Time     { return receipt.committedAt }
func (receipt CommitReceipt) Replayed() bool             { return receipt.replayed }
func (receipt CommitReceipt) ResolvedAfterUnknown() bool { return receipt.resolved }
func (receipt CommitReceipt) IsZero() bool {
	return receipt.command.IsZero() && receipt.resultDigest == SHA256Digest{} &&
		receipt.committedAt.IsZero() && !receipt.replayed && !receipt.resolved
}

type ConversationRepository interface {
	CurrentConversation(context.Context, im.ConversationRef) (im.ConversationSnapshot, error)
	CompareAndSwapConversation(
		context.Context,
		uint64,
		im.ConversationSnapshot,
	) (im.ConversationSnapshot, error)
}

type ConversationAuthorityRepository interface {
	CurrentProviderBinding(
		context.Context,
		im.ProviderConversationRef,
	) (im.ProviderConversationBinding, error)
	CompareAndSwapProviderBinding(
		context.Context,
		uint64,
		im.ProviderConversationBinding,
	) (im.ProviderConversationBinding, error)

	CurrentMembership(
		context.Context,
		im.ConversationRef,
		im.ActorRef,
	) (im.ConversationMembershipSnapshot, error)
	CompareAndSwapMembership(
		context.Context,
		uint64,
		im.ConversationMembershipSnapshot,
	) (im.ConversationMembershipSnapshot, error)

	CurrentAccess(
		context.Context,
		im.ConversationRef,
		im.ActorRef,
	) (im.ConversationAccessSnapshot, error)
	CompareAndSwapAccess(
		context.Context,
		uint64,
		im.ConversationAccessSnapshot,
	) (im.ConversationAccessSnapshot, error)
}

// MessageReadPageQuery is the read-only contract between an authenticated IM route and the
// platform message projection. The repository must bind every field to one tenant-scoped
// repeatable-read snapshot; cursor and revisions are observations, never capabilities.
type MessageReadPageQuery struct {
	Conversation         im.ConversationRef
	AfterCursor          string
	Limit                uint32
	ConversationRevision uint64
	AccessRevision       uint64
}

type MessageReadPage struct {
	Conversation         im.ConversationRef
	Messages             []im.MessageSnapshot
	NextCursor           string
	HasMore              bool
	ConversationRevision uint64
	ProjectionRevision   uint64
}

// MessageReadRepository is deliberately read-only. A production implementation must return
// snapshots from durable message heads and reject a cursor/revision/scope mismatch instead of
// falling back to an empty page. Message writes remain event/command contracts, not this port.
type MessageReadRepository interface {
	ReadPage(context.Context, MessageReadPageQuery) (MessageReadPage, error)
}

// IdentityAuthorityRepository exposes only current, immutable identity authority snapshots.
// Implementations must use the same transaction snapshot as the surrounding operation and must
// never accept caller-supplied principal/actor claims as authority.
type IdentityAuthorityRepository interface {
	CurrentHumanIdentityBinding(
		context.Context,
		im.ExternalIdentityRef,
	) (im.HumanExternalIdentityBinding, error)
	CurrentHumanPrincipal(
		context.Context,
		im.HumanPrincipalID,
	) (im.HumanPrincipalSnapshot, error)
	CurrentTenantMembership(
		context.Context,
		im.TenantID,
		im.HumanPrincipalID,
	) (im.TenantMembershipSnapshot, error)
	CurrentActor(context.Context, im.ActorRef) (im.ActorSnapshot, error)
}

type TenantRepositories interface {
	Conversations() ConversationRepository
	Authority() ConversationAuthorityRepository
	Identity() IdentityAuthorityRepository
}

type ExecuteOperation func(context.Context, TenantRepositories) (SHA256Digest, error)
type ReadOperation func(context.Context, TenantRepositories) error

type TenantUnitOfWork interface {
	Read(context.Context, im.TenantID, ReadOperation) error
	Execute(
		context.Context,
		im.TenantID,
		CommandIdentity,
		ExecuteOperation,
	) (CommitReceipt, error)
	Resolve(context.Context, im.TenantID, CommandIdentity) (CommitReceipt, error)
}
