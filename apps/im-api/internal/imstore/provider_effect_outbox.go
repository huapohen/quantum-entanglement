package imstore

// This file defines the provider-effect outbox boundary used by Agent Store
// installation/offboarding. It deliberately stores only a rebuildable request
// reference and digests; provider credentials, ext_info and transport payloads
// never belong in this log. The file adapter is a single-process durability
// fixture for restart/reconcile tests. Production must provide the same
// contract with a tenant-bound transactional database implementation.

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"sync"
	"time"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
)

const (
	providerEffectFileFormat   = "quantum-entanglement.provider-effect-outbox/1"
	providerEffectMaxIDBytes   = 256
	providerEffectMaxKindBytes = 64
)

var (
	ErrProviderEffectOutboxClosed = errors.New("provider effect outbox is closed")
	ErrProviderEffectOutboxLog    = errors.New("provider effect outbox log is corrupt")
	ErrProviderEffectNotFound     = errors.New("provider effect outbox record not found")
	ErrProviderEffectConflict     = errors.New("provider effect outbox idempotency conflict")
	ErrProviderEffectLease        = errors.New("provider effect outbox lease is invalid")
	ErrProviderEffectInvalid      = errors.New("provider effect outbox request is invalid")
	ErrProviderEffectState        = errors.New("provider effect outbox state transition is invalid")
	ErrProviderEffectClock        = errors.New("provider effect outbox clock is invalid")
	providerEffectIDPattern       = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$`)
	providerEffectKindPattern     = regexp.MustCompile(`^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$`)
)

// ProviderEffectKind is intentionally a closed vocabulary. Adding a new
// effect requires a reviewed adapter contract instead of silently accepting a
// caller-defined operation.
type ProviderEffectKind string

const (
	ProviderEffectUserProvision ProviderEffectKind = "user_provision"
	ProviderEffectUserRevoke    ProviderEffectKind = "user_revoke"
	ProviderEffectGroupCreate   ProviderEffectKind = "group_create"
	ProviderEffectMemberAdd     ProviderEffectKind = "member_add"
	ProviderEffectMemberRemove  ProviderEffectKind = "member_remove"
	ProviderEffectTextSend      ProviderEffectKind = "text_send"
)

func (kind ProviderEffectKind) Valid() bool {
	switch kind {
	case ProviderEffectUserProvision, ProviderEffectUserRevoke,
		ProviderEffectGroupCreate, ProviderEffectMemberAdd,
		ProviderEffectMemberRemove, ProviderEffectTextSend:
		return true
	default:
		return false
	}
}

// ProviderEffectState is platform-owned state, not an assertion about the
// provider. In particular, unknown is a durable reconcile case and cannot be
// promoted to committed by timeout or by a blind retry.
type ProviderEffectState string

const (
	ProviderEffectQueued    ProviderEffectState = "queued"
	ProviderEffectSent      ProviderEffectState = "sent"
	ProviderEffectCommitted ProviderEffectState = "committed"
	ProviderEffectReplayed  ProviderEffectState = "replayed"
	ProviderEffectUnknown   ProviderEffectState = "unknown"
	ProviderEffectFailed    ProviderEffectState = "failed"
)

func (state ProviderEffectState) Valid() bool {
	switch state {
	case ProviderEffectQueued, ProviderEffectSent, ProviderEffectCommitted,
		ProviderEffectReplayed, ProviderEffectUnknown, ProviderEffectFailed:
		return true
	default:
		return false
	}
}

// ProviderEffectIntent contains the minimum non-secret information needed to
// reconstruct a provider request. RequestRef points at a separately governed
// payload/command; it is never the payload itself.
type ProviderEffectIntent struct {
	TenantID          string
	WorkspaceID       *string
	InstallationID    string
	EffectID          string
	EffectKind        ProviderEffectKind
	Provider          string
	ProviderRealmID   string
	ProviderSubjectID string
	OperationKey      string
	RequestRef        string
	RequestDigest     SHA256Digest
	CreatedAt         time.Time
}

func (intent ProviderEffectIntent) Validate() error {
	if !validProviderEffectID(intent.TenantID) ||
		!validOptionalProviderEffectID(intent.WorkspaceID) ||
		!validProviderEffectID(intent.InstallationID) ||
		!validProviderEffectID(intent.EffectID) ||
		!intent.EffectKind.Valid() || len(intent.EffectKind) > providerEffectMaxKindBytes ||
		!validProviderEffectID(intent.Provider) ||
		!validProviderEffectID(intent.ProviderRealmID) ||
		!validOptionalProviderEffectID(stringPointerOrNil(intent.ProviderSubjectID)) ||
		!validProviderEffectID(intent.OperationKey) ||
		!validProviderEffectID(intent.RequestRef) ||
		intent.RequestDigest == (SHA256Digest{}) || !validProviderEffectTime(intent.CreatedAt) {
		return ErrProviderEffectInvalid
	}
	return nil
}

func stringPointerOrNil(value string) *string {
	if value == "" {
		return nil
	}
	return &value
}

// ProviderEffectKey is the immutable lookup identity. EffectID is intentionally
// part of the key in addition to OperationKey so multiple effect kinds can be
// linked to one command without accidental overwrite.
type ProviderEffectKey struct {
	TenantID string
	EffectID string
}

func (key ProviderEffectKey) Validate() error {
	if !validProviderEffectID(key.TenantID) || !validProviderEffectID(key.EffectID) {
		return ErrProviderEffectInvalid
	}
	return nil
}

func (intent ProviderEffectIntent) Key() ProviderEffectKey {
	return ProviderEffectKey{TenantID: intent.TenantID, EffectID: intent.EffectID}
}

// ProviderEffectRecord is a durable state snapshot. ProviderReceipt is
// transport evidence only and is never used as platform authority without the
// explicit state transition below.
type ProviderEffectRecord struct {
	Intent          ProviderEffectIntent
	State           ProviderEffectState
	AttemptCount    uint64
	ProviderReceipt *im.ProviderEffectReceipt
	LastErrorCode   string
	FirstSentAt     time.Time
	LastAttemptAt   time.Time
	CommittedAt     time.Time
	UpdatedAt       time.Time
	LeaseExpiresAt  time.Time
}

func (record ProviderEffectRecord) Validate() error {
	if err := record.Intent.Validate(); err != nil || !record.State.Valid() || record.AttemptCount > 1<<63-1 ||
		!validProviderEffectTime(record.UpdatedAt) {
		return ErrProviderEffectInvalid
	}
	if !record.FirstSentAt.IsZero() && !validProviderEffectTime(record.FirstSentAt) ||
		!record.LastAttemptAt.IsZero() && !validProviderEffectTime(record.LastAttemptAt) ||
		!record.CommittedAt.IsZero() && !validProviderEffectTime(record.CommittedAt) ||
		!record.LeaseExpiresAt.IsZero() && !validProviderEffectTime(record.LeaseExpiresAt) {
		return ErrProviderEffectInvalid
	}
	if record.State == ProviderEffectQueued && record.AttemptCount != 0 {
		return ErrProviderEffectState
	}
	if record.State == ProviderEffectSent && record.AttemptCount == 0 {
		return ErrProviderEffectState
	}
	if (record.State == ProviderEffectSent) != !record.LeaseExpiresAt.IsZero() {
		return ErrProviderEffectState
	}
	if record.State != ProviderEffectSent && !record.LeaseExpiresAt.IsZero() {
		return ErrProviderEffectState
	}
	if (record.State == ProviderEffectCommitted || record.State == ProviderEffectReplayed) &&
		(record.ProviderReceipt == nil || record.CommittedAt.IsZero() || !record.LeaseExpiresAt.IsZero()) {
		return ErrProviderEffectState
	}
	if record.State == ProviderEffectUnknown && record.ProviderReceipt != nil &&
		record.ProviderReceipt.Status != im.ProviderEffectUnknown {
		return ErrProviderEffectState
	}
	if record.State == ProviderEffectCommitted && record.ProviderReceipt != nil &&
		record.ProviderReceipt.Status != im.ProviderEffectCommitted {
		return ErrProviderEffectState
	}
	if record.State == ProviderEffectReplayed && record.ProviderReceipt != nil &&
		record.ProviderReceipt.Status != im.ProviderEffectReplayed {
		return ErrProviderEffectState
	}
	if (record.State == ProviderEffectQueued || record.State == ProviderEffectSent || record.State == ProviderEffectFailed) && record.ProviderReceipt != nil {
		return ErrProviderEffectState
	}
	if record.ProviderReceipt != nil && record.ProviderReceipt.Validate() != nil {
		return ErrProviderEffectInvalid
	}
	if record.ProviderReceipt != nil && record.ProviderReceipt.OperationKey != record.Intent.OperationKey {
		return ErrProviderEffectConflict
	}
	return nil
}

// ProviderEffectClaim is returned only while the lease is held. LeaseToken is
// never persisted in plaintext; a crash leaves a digest that expires and can
// be reclaimed by another worker.
type ProviderEffectClaim struct {
	Record     ProviderEffectRecord
	LeaseToken string
}

// ProviderEffectOutbox is the narrow provider boundary. Enqueue must run in
// the same durable transaction as the platform command; ClaimDue/RecordReceipt
// happen in a worker and are safe to retry by lease and operation identity.
type ProviderEffectOutbox interface {
	Enqueue(context.Context, ProviderEffectIntent) (ProviderEffectRecord, bool, error)
	Load(context.Context, ProviderEffectKey) (ProviderEffectRecord, error)
	ClaimDue(context.Context, string, string, time.Duration, int) ([]ProviderEffectClaim, error)
	RecordReceipt(context.Context, ProviderEffectKey, string, im.ProviderEffectReceipt) (ProviderEffectRecord, error)
	MarkUnknown(context.Context, ProviderEffectKey, string, string) (ProviderEffectRecord, error)
	ResolveUnknown(context.Context, ProviderEffectKey, im.ProviderEffectReceipt) (ProviderEffectRecord, error)
	MarkFailed(context.Context, ProviderEffectKey, string, string) (ProviderEffectRecord, error)
}

// ProviderEffectClock is injected for deterministic restart/reconcile tests.
type ProviderEffectClock func(context.Context) time.Time

// DurableProviderEffectFileStore is a single-process append-and-fsync adapter.
// It is suitable for local crash/reopen tests only; production must replace it
// with a PostgreSQL implementation that atomically enqueues with the command.
type DurableProviderEffectFileStore struct {
	mu       sync.RWMutex
	path     string
	file     *os.File
	clock    ProviderEffectClock
	entries  map[ProviderEffectKey]providerEffectEntry
	leaseSeq uint64
	closed   bool
}

type providerEffectEntry struct {
	record           ProviderEffectRecord
	leaseTokenDigest string
}

var _ ProviderEffectOutbox = (*DurableProviderEffectFileStore)(nil)

func OpenDurableProviderEffectFileStore(
	ctx context.Context,
	path string,
	clock ProviderEffectClock,
) (*DurableProviderEffectFileStore, error) {
	if err := providerEffectContextError(ctx); err != nil {
		return nil, err
	}
	if path == "" || !filepath.IsAbs(path) || filepath.Clean(path) == "." || clock == nil {
		return nil, ErrProviderEffectInvalid
	}
	parent := filepath.Dir(path)
	info, err := os.Stat(parent)
	if err != nil || !info.IsDir() {
		return nil, ErrProviderEffectInvalid
	}
	file, err := os.OpenFile(path, os.O_RDWR|os.O_CREATE|os.O_APPEND, 0o600)
	if err != nil {
		return nil, ErrProviderEffectOutboxClosed
	}
	if err := file.Chmod(0o600); err != nil {
		_ = file.Close()
		return nil, ErrProviderEffectOutboxClosed
	}
	store := &DurableProviderEffectFileStore{
		path: path, file: file, clock: clock,
		entries: make(map[ProviderEffectKey]providerEffectEntry),
	}
	if err := store.load(ctx); err != nil {
		_ = file.Close()
		return nil, err
	}
	return store, nil
}

func (store *DurableProviderEffectFileStore) Enqueue(
	ctx context.Context,
	intent ProviderEffectIntent,
) (ProviderEffectRecord, bool, error) {
	if err := providerEffectContextError(ctx); err != nil {
		return ProviderEffectRecord{}, false, err
	}
	if err := intent.Validate(); err != nil {
		return ProviderEffectRecord{}, false, err
	}
	if store == nil {
		return ProviderEffectRecord{}, false, ErrProviderEffectOutboxClosed
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	if err := store.usableLocked(); err != nil {
		return ProviderEffectRecord{}, false, err
	}
	key := intent.Key()
	if existing, ok := store.entries[key]; ok {
		if !sameProviderEffectIntent(existing.record.Intent, intent) {
			return ProviderEffectRecord{}, false, ErrProviderEffectConflict
		}
		return cloneProviderEffectRecord(existing.record), true, nil
	}
	now, err := store.nowLocked(ctx)
	if err != nil {
		return ProviderEffectRecord{}, false, err
	}
	record := ProviderEffectRecord{Intent: cloneProviderEffectIntent(intent), State: ProviderEffectQueued, UpdatedAt: now}
	if err := record.Validate(); err != nil {
		return ProviderEffectRecord{}, false, err
	}
	if err := store.appendSnapshotLocked(record, ""); err != nil {
		return ProviderEffectRecord{}, false, err
	}
	store.entries[key] = providerEffectEntry{record: record}
	return cloneProviderEffectRecord(record), false, nil
}

func (store *DurableProviderEffectFileStore) Load(
	ctx context.Context,
	key ProviderEffectKey,
) (ProviderEffectRecord, error) {
	if err := providerEffectContextError(ctx); err != nil {
		return ProviderEffectRecord{}, err
	}
	if err := key.Validate(); err != nil {
		return ProviderEffectRecord{}, err
	}
	if store == nil {
		return ProviderEffectRecord{}, ErrProviderEffectOutboxClosed
	}
	store.mu.RLock()
	defer store.mu.RUnlock()
	if store.closed || store.file == nil {
		return ProviderEffectRecord{}, ErrProviderEffectOutboxClosed
	}
	entry, ok := store.entries[key]
	if !ok {
		return ProviderEffectRecord{}, ErrProviderEffectNotFound
	}
	return cloneProviderEffectRecord(entry.record), nil
}

func (store *DurableProviderEffectFileStore) ClaimDue(
	ctx context.Context,
	tenantID string,
	workerID string,
	lease time.Duration,
	limit int,
) ([]ProviderEffectClaim, error) {
	if err := providerEffectContextError(ctx); err != nil {
		return nil, err
	}
	if !validProviderEffectID(tenantID) || !validProviderEffectID(workerID) || lease <= 0 || limit <= 0 || limit > 100 {
		return nil, ErrProviderEffectInvalid
	}
	if store == nil {
		return nil, ErrProviderEffectOutboxClosed
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	if err := store.usableLocked(); err != nil {
		return nil, err
	}
	now, err := store.nowLocked(ctx)
	if err != nil {
		return nil, err
	}
	keys := make([]ProviderEffectKey, 0)
	for key, entry := range store.entries {
		if key.TenantID != tenantID || !providerEffectDue(entry, now) {
			continue
		}
		keys = append(keys, key)
	}
	slices.SortFunc(keys, func(left, right ProviderEffectKey) int {
		if left.EffectID < right.EffectID {
			return -1
		}
		if left.EffectID > right.EffectID {
			return 1
		}
		return 0
	})
	claims := make([]ProviderEffectClaim, 0, min(limit, len(keys)))
	for _, key := range keys {
		if len(claims) == limit {
			break
		}
		entry := store.entries[key]
		if entry.record.AttemptCount == 1<<63-1 {
			continue
		}
		token, digest, tokenErr := providerEffectLeaseToken(store, workerID, key)
		if tokenErr != nil {
			return nil, tokenErr
		}
		entry.record.AttemptCount++
		if entry.record.FirstSentAt.IsZero() {
			entry.record.FirstSentAt = now
		}
		entry.record.LastAttemptAt = now
		entry.record.UpdatedAt = now
		entry.record.LeaseExpiresAt = now.Add(lease).UTC()
		entry.record.State = ProviderEffectSent
		if err := entry.record.Validate(); err != nil {
			return nil, ErrProviderEffectOutboxLog
		}
		if err := store.appendSnapshotLocked(entry.record, digest); err != nil {
			return nil, err
		}
		entry.leaseTokenDigest = digest
		store.entries[key] = entry
		claims = append(claims, ProviderEffectClaim{Record: cloneProviderEffectRecord(entry.record), LeaseToken: token})
	}
	return claims, nil
}

func (store *DurableProviderEffectFileStore) RecordReceipt(
	ctx context.Context,
	key ProviderEffectKey,
	leaseToken string,
	receipt im.ProviderEffectReceipt,
) (ProviderEffectRecord, error) {
	if err := providerEffectContextError(ctx); err != nil {
		return ProviderEffectRecord{}, err
	}
	if err := key.Validate(); err != nil || receipt.Validate() != nil {
		return ProviderEffectRecord{}, ErrProviderEffectInvalid
	}
	if store == nil {
		return ProviderEffectRecord{}, ErrProviderEffectOutboxClosed
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	now, err := store.nowLocked(ctx)
	if err != nil {
		return ProviderEffectRecord{}, err
	}
	entry, err := store.claimLocked(key, leaseToken, now)
	if err != nil {
		return ProviderEffectRecord{}, err
	}
	if receipt.OperationKey != entry.record.Intent.OperationKey {
		return ProviderEffectRecord{}, ErrProviderEffectConflict
	}
	entry.record.ProviderReceipt = cloneProviderEffectReceipt(&receipt)
	entry.record.LeaseExpiresAt = time.Time{}
	entry.record.UpdatedAt = now
	switch receipt.Status {
	case im.ProviderEffectCommitted:
		entry.record.State = ProviderEffectCommitted
	case im.ProviderEffectReplayed:
		entry.record.State = ProviderEffectReplayed
	case im.ProviderEffectUnknown:
		entry.record.State = ProviderEffectUnknown
	default:
		return ProviderEffectRecord{}, ErrProviderEffectInvalid
	}
	if entry.record.State == ProviderEffectCommitted || entry.record.State == ProviderEffectReplayed {
		entry.record.CommittedAt = receipt.ObservedAt.UTC()
	}
	if err := entry.record.Validate(); err != nil {
		return ProviderEffectRecord{}, err
	}
	if err := store.appendSnapshotLocked(entry.record, ""); err != nil {
		return ProviderEffectRecord{}, err
	}
	entry.leaseTokenDigest = ""
	store.entries[key] = entry
	return cloneProviderEffectRecord(entry.record), nil
}

func (store *DurableProviderEffectFileStore) MarkUnknown(
	ctx context.Context,
	key ProviderEffectKey,
	leaseToken string,
	reasonCode string,
) (ProviderEffectRecord, error) {
	if err := providerEffectContextError(ctx); err != nil {
		return ProviderEffectRecord{}, err
	}
	if err := key.Validate(); err != nil || !validProviderEffectID(reasonCode) {
		return ProviderEffectRecord{}, ErrProviderEffectInvalid
	}
	if store == nil {
		return ProviderEffectRecord{}, ErrProviderEffectOutboxClosed
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	now, err := store.nowLocked(ctx)
	if err != nil {
		return ProviderEffectRecord{}, err
	}
	entry, err := store.claimLocked(key, leaseToken, now)
	if err != nil {
		return ProviderEffectRecord{}, err
	}
	entry.record.State = ProviderEffectUnknown
	entry.record.LastErrorCode = reasonCode
	entry.record.LeaseExpiresAt = time.Time{}
	entry.record.UpdatedAt = now
	if err := entry.record.Validate(); err != nil {
		return ProviderEffectRecord{}, err
	}
	if err := store.appendSnapshotLocked(entry.record, ""); err != nil {
		return ProviderEffectRecord{}, err
	}
	entry.leaseTokenDigest = ""
	store.entries[key] = entry
	return cloneProviderEffectRecord(entry.record), nil
}

func (store *DurableProviderEffectFileStore) ResolveUnknown(
	ctx context.Context,
	key ProviderEffectKey,
	receipt im.ProviderEffectReceipt,
) (ProviderEffectRecord, error) {
	if err := providerEffectContextError(ctx); err != nil {
		return ProviderEffectRecord{}, err
	}
	if err := key.Validate(); err != nil || receipt.Validate() != nil || receipt.Status == im.ProviderEffectUnknown {
		return ProviderEffectRecord{}, ErrProviderEffectInvalid
	}
	if store == nil {
		return ProviderEffectRecord{}, ErrProviderEffectOutboxClosed
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	if err := store.usableLocked(); err != nil {
		return ProviderEffectRecord{}, err
	}
	entry, ok := store.entries[key]
	if !ok {
		return ProviderEffectRecord{}, ErrProviderEffectNotFound
	}
	if entry.record.State != ProviderEffectUnknown || receipt.OperationKey != entry.record.Intent.OperationKey {
		return ProviderEffectRecord{}, ErrProviderEffectState
	}
	now, err := store.nowLocked(ctx)
	if err != nil {
		return ProviderEffectRecord{}, err
	}
	entry.record.ProviderReceipt = cloneProviderEffectReceipt(&receipt)
	entry.record.State = ProviderEffectCommitted
	if receipt.Status == im.ProviderEffectReplayed {
		entry.record.State = ProviderEffectReplayed
	}
	entry.record.CommittedAt = receipt.ObservedAt.UTC()
	entry.record.LastErrorCode = ""
	entry.record.UpdatedAt = now
	if err := entry.record.Validate(); err != nil {
		return ProviderEffectRecord{}, err
	}
	if err := store.appendSnapshotLocked(entry.record, ""); err != nil {
		return ProviderEffectRecord{}, err
	}
	store.entries[key] = entry
	return cloneProviderEffectRecord(entry.record), nil
}

func (store *DurableProviderEffectFileStore) MarkFailed(
	ctx context.Context,
	key ProviderEffectKey,
	leaseToken string,
	errorCode string,
) (ProviderEffectRecord, error) {
	if err := providerEffectContextError(ctx); err != nil {
		return ProviderEffectRecord{}, err
	}
	if err := key.Validate(); err != nil || !validProviderEffectID(errorCode) {
		return ProviderEffectRecord{}, ErrProviderEffectInvalid
	}
	if store == nil {
		return ProviderEffectRecord{}, ErrProviderEffectOutboxClosed
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	now, err := store.nowLocked(ctx)
	if err != nil {
		return ProviderEffectRecord{}, err
	}
	entry, err := store.claimLocked(key, leaseToken, now)
	if err != nil {
		return ProviderEffectRecord{}, err
	}
	entry.record.State = ProviderEffectFailed
	entry.record.LastErrorCode = errorCode
	entry.record.LeaseExpiresAt = time.Time{}
	entry.record.UpdatedAt = now
	if err := entry.record.Validate(); err != nil {
		return ProviderEffectRecord{}, err
	}
	if err := store.appendSnapshotLocked(entry.record, ""); err != nil {
		return ProviderEffectRecord{}, err
	}
	entry.leaseTokenDigest = ""
	store.entries[key] = entry
	return cloneProviderEffectRecord(entry.record), nil
}

func (store *DurableProviderEffectFileStore) claimLocked(
	key ProviderEffectKey,
	leaseToken string,
	now time.Time,
) (providerEffectEntry, error) {
	if err := store.usableLocked(); err != nil {
		return providerEffectEntry{}, err
	}
	entry, ok := store.entries[key]
	if !ok {
		return providerEffectEntry{}, ErrProviderEffectNotFound
	}
	if leaseToken == "" || entry.leaseTokenDigest == "" || digestLeaseToken(leaseToken) != entry.leaseTokenDigest ||
		entry.record.LeaseExpiresAt.IsZero() || !entry.record.LeaseExpiresAt.After(now) {
		return providerEffectEntry{}, ErrProviderEffectLease
	}
	return entry, nil
}

func (store *DurableProviderEffectFileStore) usableLocked() error {
	if store == nil || store.closed || store.file == nil {
		return ErrProviderEffectOutboxClosed
	}
	return nil
}

func (store *DurableProviderEffectFileStore) nowLocked(ctx context.Context) (time.Time, error) {
	if err := providerEffectContextError(ctx); err != nil {
		return time.Time{}, err
	}
	var now time.Time
	func() {
		defer func() { _ = recover() }()
		now = store.clock(ctx)
	}()
	now = now.Round(0).UTC()
	if !validProviderEffectTime(now) {
		return time.Time{}, ErrProviderEffectClock
	}
	return now, nil
}

func providerEffectDue(entry providerEffectEntry, now time.Time) bool {
	switch entry.record.State {
	case ProviderEffectQueued, ProviderEffectFailed:
		return true
	case ProviderEffectSent:
		return !entry.record.LeaseExpiresAt.IsZero() && !entry.record.LeaseExpiresAt.After(now)
	default:
		return false
	}
}

func providerEffectLeaseToken(store *DurableProviderEffectFileStore, workerID string, key ProviderEffectKey) (string, string, error) {
	store.leaseSeq++
	var random [16]byte
	if _, err := rand.Read(random[:]); err != nil {
		return "", "", ErrProviderEffectLease
	}
	token := hex.EncodeToString(random[:]) + "." + workerID + "." + key.EffectID
	return token, digestLeaseToken(token), nil
}

func digestLeaseToken(token string) string {
	return DigestBytes([]byte("provider-effect-lease/1\x00" + token)).Hex()
}

func (store *DurableProviderEffectFileStore) appendSnapshotLocked(record ProviderEffectRecord, leaseDigest string) error {
	wire, err := providerEffectWireFromRecord(record, leaseDigest)
	if err != nil {
		return ErrProviderEffectOutboxLog
	}
	encoded, err := json.Marshal(wire)
	if err != nil {
		return ErrProviderEffectOutboxLog
	}
	encoded = append(encoded, '\n')
	start, err := store.file.Seek(0, io.SeekEnd)
	if err != nil {
		return ErrProviderEffectOutboxClosed
	}
	if written, err := store.file.Write(encoded); err != nil || written != len(encoded) {
		_ = store.file.Truncate(start)
		_ = store.file.Sync()
		return ErrProviderEffectOutboxClosed
	}
	if err := store.file.Sync(); err != nil {
		_ = store.file.Truncate(start)
		_ = store.file.Sync()
		return ErrProviderEffectOutboxClosed
	}
	return nil
}

func (store *DurableProviderEffectFileStore) load(ctx context.Context) error {
	if err := providerEffectContextError(ctx); err != nil {
		return err
	}
	if _, err := store.file.Seek(0, io.SeekStart); err != nil {
		return ErrProviderEffectOutboxLog
	}
	raw, err := io.ReadAll(store.file)
	if err != nil {
		return ErrProviderEffectOutboxLog
	}
	// A process can die after writing an incomplete final record. Only that
	// final tail is discardable; every complete line must parse and validate.
	complete := raw
	if len(raw) > 0 && raw[len(raw)-1] != '\n' {
		tailStart := bytes.LastIndexByte(raw, '\n') + 1
		complete = raw[:tailStart]
		if err := store.file.Truncate(int64(tailStart)); err != nil {
			return ErrProviderEffectOutboxLog
		}
		if err := store.file.Sync(); err != nil {
			return ErrProviderEffectOutboxLog
		}
	}
	lines := bytes.Split(complete, []byte{'\n'})
	if len(lines) > 0 && len(lines[len(lines)-1]) == 0 {
		lines = lines[:len(lines)-1]
	}
	for _, line := range lines {
		if len(line) == 0 {
			return ErrProviderEffectOutboxLog
		}
		var wire providerEffectWire
		decoder := json.NewDecoder(bytes.NewReader(line))
		decoder.DisallowUnknownFields()
		if decoder.Decode(&wire) != nil {
			return ErrProviderEffectOutboxLog
		}
		record, leaseDigest, decodeErr := providerEffectRecordFromWire(wire)
		if decodeErr != nil {
			return ErrProviderEffectOutboxLog
		}
		key := record.Intent.Key()
		if previous, exists := store.entries[key]; exists {
			if !sameProviderEffectIntent(previous.record.Intent, record.Intent) ||
				record.AttemptCount < previous.record.AttemptCount ||
				!validProviderEffectTransition(previous.record.State, record.State) {
				return ErrProviderEffectOutboxLog
			}
		}
		store.entries[key] = providerEffectEntry{record: record, leaseTokenDigest: leaseDigest}
	}
	if _, err := store.file.Seek(0, io.SeekEnd); err != nil {
		return ErrProviderEffectOutboxLog
	}
	return nil
}

func validProviderEffectTransition(previous, next ProviderEffectState) bool {
	if previous == next {
		return true
	}
	switch previous {
	case ProviderEffectQueued:
		return next == ProviderEffectSent || next == ProviderEffectFailed || next == ProviderEffectUnknown || next == ProviderEffectCommitted || next == ProviderEffectReplayed
	case ProviderEffectSent:
		return next == ProviderEffectFailed || next == ProviderEffectUnknown || next == ProviderEffectCommitted || next == ProviderEffectReplayed
	case ProviderEffectUnknown:
		return next == ProviderEffectCommitted || next == ProviderEffectReplayed
	case ProviderEffectFailed:
		return next == ProviderEffectSent || next == ProviderEffectFailed || next == ProviderEffectUnknown
	case ProviderEffectCommitted, ProviderEffectReplayed:
		return false
	default:
		return false
	}
}

func (store *DurableProviderEffectFileStore) Close() error {
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
	syncErr := store.file.Sync()
	closeErr := store.file.Close()
	store.file = nil
	if syncErr != nil || closeErr != nil {
		return ErrProviderEffectOutboxClosed
	}
	return nil
}

type providerEffectWire struct {
	Format          string                     `json:"format"`
	Intent          providerEffectIntentWire   `json:"intent"`
	State           ProviderEffectState        `json:"state"`
	AttemptCount    uint64                     `json:"attemptCount"`
	ProviderReceipt *providerEffectReceiptWire `json:"providerReceipt,omitempty"`
	LastErrorCode   string                     `json:"lastErrorCode,omitempty"`
	FirstSentAt     string                     `json:"firstSentAt,omitempty"`
	LastAttemptAt   string                     `json:"lastAttemptAt,omitempty"`
	CommittedAt     string                     `json:"committedAt,omitempty"`
	UpdatedAt       string                     `json:"updatedAt"`
	LeaseExpiresAt  string                     `json:"leaseExpiresAt,omitempty"`
	LeaseDigest     string                     `json:"leaseTokenDigest,omitempty"`
}

type providerEffectIntentWire struct {
	TenantID          string  `json:"tenantId"`
	WorkspaceID       *string `json:"workspaceId,omitempty"`
	InstallationID    string  `json:"installationId"`
	EffectID          string  `json:"effectId"`
	EffectKind        string  `json:"effectKind"`
	Provider          string  `json:"provider"`
	ProviderRealmID   string  `json:"providerRealmId"`
	ProviderSubjectID string  `json:"providerSubjectId,omitempty"`
	OperationKey      string  `json:"operationKey"`
	RequestRef        string  `json:"requestRef"`
	RequestDigest     string  `json:"requestDigest"`
	CreatedAt         string  `json:"createdAt"`
}

type providerEffectReceiptWire struct {
	OperationKey string `json:"operationKey"`
	ExternalID   string `json:"externalId"`
	Status       string `json:"status"`
	ObservedAt   string `json:"observedAt"`
}

func providerEffectWireFromRecord(record ProviderEffectRecord, leaseDigest string) (providerEffectWire, error) {
	if err := record.Validate(); err != nil {
		return providerEffectWire{}, err
	}
	wire := providerEffectWire{
		Format: providerEffectFileFormat, Intent: providerEffectIntentWireFromIntent(record.Intent),
		State: record.State, AttemptCount: record.AttemptCount, LastErrorCode: record.LastErrorCode,
		FirstSentAt: providerEffectTimeString(record.FirstSentAt), LastAttemptAt: providerEffectTimeString(record.LastAttemptAt),
		CommittedAt: providerEffectTimeString(record.CommittedAt), UpdatedAt: record.UpdatedAt.UTC().Format(time.RFC3339Nano),
		LeaseExpiresAt: providerEffectTimeString(record.LeaseExpiresAt), LeaseDigest: leaseDigest,
	}
	if record.ProviderReceipt != nil {
		receipt := record.ProviderReceipt
		wire.ProviderReceipt = &providerEffectReceiptWire{OperationKey: receipt.OperationKey, ExternalID: receipt.ExternalID, Status: string(receipt.Status), ObservedAt: receipt.ObservedAt.UTC().Format(time.RFC3339Nano)}
	}
	return wire, nil
}

func providerEffectRecordFromWire(wire providerEffectWire) (ProviderEffectRecord, string, error) {
	if wire.Format != providerEffectFileFormat || !wire.State.Valid() || wire.LeaseDigest != "" && !providerEffectIDPattern.MatchString(wire.LeaseDigest) {
		return ProviderEffectRecord{}, "", ErrProviderEffectOutboxLog
	}
	intent, err := providerEffectIntentFromWire(wire.Intent)
	if err != nil {
		return ProviderEffectRecord{}, "", err
	}
	record := ProviderEffectRecord{Intent: intent, State: wire.State, AttemptCount: wire.AttemptCount, LastErrorCode: wire.LastErrorCode}
	parseTime := func(value string, target *time.Time) error {
		if value == "" {
			return nil
		}
		parsed, parseErr := time.Parse(time.RFC3339Nano, value)
		if parseErr != nil || parsed.Location() != time.UTC {
			return ErrProviderEffectOutboxLog
		}
		*target = parsed
		return nil
	}
	if err := parseTime(wire.FirstSentAt, &record.FirstSentAt); err != nil {
		return ProviderEffectRecord{}, "", err
	}
	if err := parseTime(wire.LastAttemptAt, &record.LastAttemptAt); err != nil {
		return ProviderEffectRecord{}, "", err
	}
	if err := parseTime(wire.CommittedAt, &record.CommittedAt); err != nil {
		return ProviderEffectRecord{}, "", err
	}
	if err := parseTime(wire.UpdatedAt, &record.UpdatedAt); err != nil {
		return ProviderEffectRecord{}, "", err
	}
	if err := parseTime(wire.LeaseExpiresAt, &record.LeaseExpiresAt); err != nil {
		return ProviderEffectRecord{}, "", err
	}
	if wire.ProviderReceipt != nil {
		observedAt, parseErr := time.Parse(time.RFC3339Nano, wire.ProviderReceipt.ObservedAt)
		if parseErr != nil || observedAt.Location() != time.UTC {
			return ProviderEffectRecord{}, "", ErrProviderEffectOutboxLog
		}
		receipt := &im.ProviderEffectReceipt{OperationKey: wire.ProviderReceipt.OperationKey, ExternalID: wire.ProviderReceipt.ExternalID, Status: im.ProviderEffectStatus(wire.ProviderReceipt.Status), ObservedAt: observedAt}
		record.ProviderReceipt = receipt
	}
	if err := record.Validate(); err != nil {
		return ProviderEffectRecord{}, "", err
	}
	return record, wire.LeaseDigest, nil
}

func providerEffectIntentWireFromIntent(intent ProviderEffectIntent) providerEffectIntentWire {
	return providerEffectIntentWire{
		TenantID: intent.TenantID, WorkspaceID: cloneProviderEffectStringPointer(intent.WorkspaceID), InstallationID: intent.InstallationID,
		EffectID: intent.EffectID, EffectKind: string(intent.EffectKind), Provider: intent.Provider, ProviderRealmID: intent.ProviderRealmID,
		ProviderSubjectID: intent.ProviderSubjectID, OperationKey: intent.OperationKey, RequestRef: intent.RequestRef,
		RequestDigest: intent.RequestDigest.Hex(), CreatedAt: intent.CreatedAt.UTC().Format(time.RFC3339Nano),
	}
}

func providerEffectIntentFromWire(wire providerEffectIntentWire) (ProviderEffectIntent, error) {
	digest, err := ParseSHA256Digest(wire.RequestDigest)
	if err != nil {
		return ProviderEffectIntent{}, ErrProviderEffectOutboxLog
	}
	createdAt, err := time.Parse(time.RFC3339Nano, wire.CreatedAt)
	if err != nil || createdAt.Location() != time.UTC {
		return ProviderEffectIntent{}, ErrProviderEffectOutboxLog
	}
	intent := ProviderEffectIntent{
		TenantID: wire.TenantID, WorkspaceID: cloneProviderEffectStringPointer(wire.WorkspaceID), InstallationID: wire.InstallationID,
		EffectID: wire.EffectID, EffectKind: ProviderEffectKind(wire.EffectKind), Provider: wire.Provider, ProviderRealmID: wire.ProviderRealmID,
		ProviderSubjectID: wire.ProviderSubjectID, OperationKey: wire.OperationKey, RequestRef: wire.RequestRef,
		RequestDigest: digest, CreatedAt: createdAt,
	}
	if err := intent.Validate(); err != nil {
		return ProviderEffectIntent{}, ErrProviderEffectOutboxLog
	}
	return intent, nil
}

func providerEffectTimeString(value time.Time) string {
	if value.IsZero() {
		return ""
	}
	return value.UTC().Format(time.RFC3339Nano)
}

func cloneProviderEffectRecord(record ProviderEffectRecord) ProviderEffectRecord {
	clone := record
	clone.Intent = cloneProviderEffectIntent(record.Intent)
	clone.ProviderReceipt = cloneProviderEffectReceipt(record.ProviderReceipt)
	return clone
}

func cloneProviderEffectIntent(intent ProviderEffectIntent) ProviderEffectIntent {
	intent.WorkspaceID = cloneProviderEffectStringPointer(intent.WorkspaceID)
	return intent
}

func cloneProviderEffectReceipt(receipt *im.ProviderEffectReceipt) *im.ProviderEffectReceipt {
	if receipt == nil {
		return nil
	}
	clone := *receipt
	return &clone
}

func cloneProviderEffectStringPointer(value *string) *string {
	if value == nil {
		return nil
	}
	clone := *value
	return &clone
}

func sameProviderEffectIntent(left, right ProviderEffectIntent) bool {
	return left.TenantID == right.TenantID && optionalProviderEffectStringEqual(left.WorkspaceID, right.WorkspaceID) &&
		left.InstallationID == right.InstallationID && left.EffectID == right.EffectID && left.EffectKind == right.EffectKind &&
		left.Provider == right.Provider && left.ProviderRealmID == right.ProviderRealmID && left.ProviderSubjectID == right.ProviderSubjectID &&
		left.OperationKey == right.OperationKey && left.RequestRef == right.RequestRef && left.RequestDigest == right.RequestDigest &&
		left.CreatedAt.Equal(right.CreatedAt)
}

func optionalProviderEffectStringEqual(left, right *string) bool {
	if left == nil || right == nil {
		return left == nil && right == nil
	}
	return *left == *right
}

func validProviderEffectID(value string) bool {
	return len(value) > 0 && len(value) <= providerEffectMaxIDBytes && providerEffectIDPattern.MatchString(value) && value != ".."
}

func validOptionalProviderEffectID(value *string) bool {
	return value == nil || validProviderEffectID(*value)
}

func validProviderEffectTime(value time.Time) bool {
	return !value.IsZero() && value.Location() == time.UTC && value.Year() >= 1 && value.Year() <= 9999
}

func providerEffectContextError(ctx context.Context) error {
	if ctx == nil {
		return ErrProviderEffectInvalid
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	return nil
}
