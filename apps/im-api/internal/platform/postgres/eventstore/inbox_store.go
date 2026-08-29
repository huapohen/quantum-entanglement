package eventstore

import (
	"context"
	"errors"
	"math"
	"regexp"
	"time"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/events"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/platform/postgres/runtimepool"
	"github.com/jackc/pgx/v5"
)

const (
	maxInboxIdentifierBytes = 256
	maxInboxProviderBytes   = 64
)

var inboxDigestPattern = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)

// NativeIMInboxStore is the durable PostgreSQL admission store for verified provider events.
// Runtime callers can SELECT receipts and execute the one fixed admission function, but cannot
// directly INSERT, UPDATE, or DELETE inbox rows.
type NativeIMInboxStore struct {
	pool *runtimepool.Pool
}

var _ events.InboxStore = (*NativeIMInboxStore)(nil)

func NewNativeIMInboxStore(pool *runtimepool.Pool) (*NativeIMInboxStore, error) {
	if pool == nil {
		return nil, events.ErrInvalidStore
	}
	return &NativeIMInboxStore{pool: pool}, nil
}

func (store *NativeIMInboxStore) Admit(
	ctx context.Context,
	envelope events.InboxEnvelope,
) (events.InboxAdmission, error) {
	if err := inboxStoreContextError(ctx); err != nil {
		return events.InboxAdmission{}, err
	}
	if store == nil || store.pool == nil || !validNativeIMInboxEnvelope(envelope) {
		return events.InboxAdmission{}, events.ErrInvalidInboxEnvelope
	}
	parts, err := payloadParts(envelope.Payload)
	if err != nil {
		return events.InboxAdmission{}, events.ErrInvalidInboxEnvelope
	}
	connection, err := store.pool.Acquire(ctx)
	if err != nil {
		return events.InboxAdmission{}, events.ErrInboxStoreUnavailable
	}
	defer connection.Release()
	transaction, err := connection.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.Serializable})
	if err != nil {
		return events.InboxAdmission{}, mapInboxStoreError(ctx, err)
	}
	defer rollbackInboxTransaction(transaction)
	if err := bindTenant(ctx, transaction, envelope.Scope.TenantID); err != nil {
		return events.InboxAdmission{}, mapInboxStoreError(ctx, err)
	}
	var status string
	err = transaction.QueryRow(ctx, `
SELECT wanwork_im.admit_native_im_inbox(
    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13
)`,
		envelope.Scope.TenantID, inboxWorkspaceValue(envelope.Scope.WorkspaceID), envelope.Scope.Provider,
		envelope.Scope.ChannelID, envelope.EventID, string(envelope.EventDigest), envelope.VerificationID,
		parts.kind, parts.inline, parts.storage, parts.referenceID, parts.byteLength, string(parts.digest),
	).Scan(&status)
	if err != nil {
		return events.InboxAdmission{}, mapInboxStoreError(ctx, err)
	}
	if status == "conflict" {
		return events.InboxAdmission{}, events.ErrInboxDigestConflict
	}
	if status != "inserted" && status != "replayed" {
		return events.InboxAdmission{}, events.ErrInboxStoreUnavailable
	}
	receipt, err := readNativeIMInboxReceipt(ctx, transaction, envelope.Scope, envelope.EventID)
	if err != nil {
		return events.InboxAdmission{}, err
	}
	if receipt.Envelope.EventDigest != envelope.EventDigest ||
		receipt.Envelope.Payload.Digest() != envelope.Payload.Digest() {
		return events.InboxAdmission{}, events.ErrInboxDigestConflict
	}
	if err := transaction.Commit(ctx); err != nil {
		return events.InboxAdmission{}, mapInboxStoreError(ctx, err)
	}
	admissionStatus := events.InboxReplayed
	if status == "inserted" {
		admissionStatus = events.InboxInserted
	}
	return events.InboxAdmission{Status: admissionStatus, Receipt: receipt}, nil
}

func (store *NativeIMInboxStore) Load(
	ctx context.Context,
	scope events.InboxScope,
	eventID string,
) (events.InboxReceipt, error) {
	if err := inboxStoreContextError(ctx); err != nil {
		return events.InboxReceipt{}, err
	}
	if store == nil || store.pool == nil || !validNativeIMInboxScope(scope) ||
		!validInboxIdentifier(eventID) {
		return events.InboxReceipt{}, events.ErrInvalidInboxScope
	}
	connection, err := store.pool.Acquire(ctx)
	if err != nil {
		return events.InboxReceipt{}, events.ErrInboxStoreUnavailable
	}
	defer connection.Release()
	transaction, err := connection.BeginTx(ctx, pgx.TxOptions{
		IsoLevel: pgx.RepeatableRead, AccessMode: pgx.ReadOnly,
	})
	if err != nil {
		return events.InboxReceipt{}, mapInboxStoreError(ctx, err)
	}
	defer rollbackInboxTransaction(transaction)
	if err := bindTenant(ctx, transaction, scope.TenantID); err != nil {
		return events.InboxReceipt{}, mapInboxStoreError(ctx, err)
	}
	receipt, err := readNativeIMInboxReceipt(ctx, transaction, scope, eventID)
	if err != nil {
		return events.InboxReceipt{}, err
	}
	if err := transaction.Commit(ctx); err != nil {
		return events.InboxReceipt{}, mapInboxStoreError(ctx, err)
	}
	return receipt, nil
}

func validNativeIMInboxEnvelope(envelope events.InboxEnvelope) bool {
	return events.ValidateInboxEnvelope(envelope) == nil &&
		validNativeIMInboxScope(envelope.Scope) && inboxDigestPattern.MatchString(string(envelope.EventDigest)) &&
		validInboxIdentifier(envelope.EventID) && validInboxIdentifier(envelope.VerificationID)
}

func validNativeIMInboxScope(scope events.InboxScope) bool {
	return validTenant(scope.TenantID) && validWorkspace(scope.WorkspaceID) &&
		validInboxProvider(scope.Provider) && validInboxIdentifier(scope.ChannelID)
}

func validInboxProvider(value string) bool {
	return len(value) > 0 && len(value) <= maxInboxProviderBytes && providerIDPattern.MatchString(value)
}

func validInboxIdentifier(value string) bool {
	return len(value) > 0 && len(value) <= maxInboxIdentifierBytes && inboxIdentifierPattern.MatchString(value)
}

var (
	providerIDPattern      = regexp.MustCompile(`^[a-z][a-z0-9.-]*$`)
	inboxIdentifierPattern = regexp.MustCompile(`^[^\x00-\x20\x7f]+$`)
)

func readNativeIMInboxReceipt(
	ctx context.Context,
	transaction pgx.Tx,
	scope events.InboxScope,
	eventID string,
) (events.InboxReceipt, error) {
	var workspace, provider, channelID, storedEventID string
	var eventDigest, verificationID, payloadKind string
	var payloadInline *string
	var storage, referenceID, payloadDigest string
	var byteLength, deliveryCount int64
	var firstReceivedAt, lastReceivedAt time.Time
	err := transaction.QueryRow(ctx, `
SELECT workspace_id, provider, channel_id, event_id, event_digest, verification_id,
       payload_kind, payload_inline, payload_storage, payload_reference_id,
       payload_byte_length, payload_digest, first_received_at, last_received_at, delivery_count
FROM wanwork_im.native_im_inbox
WHERE tenant_id = $1 AND workspace_id = $2 AND provider = $3 AND channel_id = $4 AND event_id = $5`,
		scope.TenantID, inboxWorkspaceValue(scope.WorkspaceID), scope.Provider, scope.ChannelID, eventID,
	).Scan(
		&workspace, &provider, &channelID, &storedEventID, &eventDigest, &verificationID,
		&payloadKind, &payloadInline, &storage, &referenceID, &byteLength, &payloadDigest,
		&firstReceivedAt, &lastReceivedAt, &deliveryCount,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return events.InboxReceipt{}, events.ErrInboxNotFound
	}
	if err != nil {
		return events.InboxReceipt{}, mapInboxStoreError(ctx, err)
	}
	if byteLength < -1 || deliveryCount <= 0 || deliveryCount > math.MaxInt64 ||
		firstReceivedAt.IsZero() || lastReceivedAt.IsZero() || lastReceivedAt.Before(firstReceivedAt) {
		return events.InboxReceipt{}, events.ErrInboxStoreUnavailable
	}
	payload, err := materializeInboxPayload(payloadKind, payloadInline, storage, referenceID, byteLength, payloadDigest)
	if err != nil {
		return events.InboxReceipt{}, events.ErrInboxStoreUnavailable
	}
	storedScope := events.InboxScope{TenantID: scope.TenantID, Provider: provider, ChannelID: channelID}
	if workspace != "" {
		storedScope.WorkspaceID = &workspace
	}
	envelope := events.InboxEnvelope{
		Scope: storedScope, EventID: storedEventID, EventDigest: events.SHA256Digest(eventDigest),
		VerificationID: verificationID, Payload: payload,
	}
	if !validNativeIMInboxEnvelope(envelope) || !sameNativeIMInboxScope(storedScope, scope) || storedEventID != eventID {
		return events.InboxReceipt{}, events.ErrInboxStoreUnavailable
	}
	return events.InboxReceipt{
		Envelope: envelope, FirstReceivedAt: firstReceivedAt.UTC(), LastReceivedAt: lastReceivedAt.UTC(),
		DeliveryCount: uint64(deliveryCount),
	}, nil
}

func materializeInboxPayload(
	kind string,
	inline *string,
	storage, referenceID string,
	byteLength int64,
	digest string,
) (events.Payload, error) {
	switch kind {
	case "inline":
		if inline == nil || byteLength != -1 {
			return events.Payload{}, events.ErrInvalidPayload
		}
		payload, err := events.NewInlinePayload([]byte(*inline))
		if err != nil || string(payload.Digest()) != digest {
			return events.Payload{}, events.ErrInvalidPayload
		}
		return payload, nil
	case "reference":
		if inline != nil || byteLength < 0 {
			return events.Payload{}, events.ErrInvalidPayload
		}
		return events.NewReferencedPayload(events.OpaquePayloadRef{
			Storage: storage, ReferenceID: referenceID, ByteLength: uint64(byteLength),
		}, events.SHA256Digest(digest))
	default:
		return events.Payload{}, events.ErrInvalidPayload
	}
}

func sameNativeIMInboxScope(left, right events.InboxScope) bool {
	if left.TenantID != right.TenantID || left.Provider != right.Provider || left.ChannelID != right.ChannelID {
		return false
	}
	if left.WorkspaceID == nil || right.WorkspaceID == nil {
		return left.WorkspaceID == nil && right.WorkspaceID == nil
	}
	return *left.WorkspaceID == *right.WorkspaceID
}

func inboxWorkspaceValue(value *string) string {
	if value == nil {
		return ""
	}
	return *value
}

func inboxStoreContextError(ctx context.Context) error {
	if ctx == nil {
		return context.Canceled
	}
	return ctx.Err()
}

func mapInboxStoreError(ctx context.Context, err error) error {
	if err == nil {
		return nil
	}
	if ctxErr := inboxStoreContextError(ctx); ctxErr != nil {
		return ctxErr
	}
	if errors.Is(err, events.ErrInboxDigestConflict) || errors.Is(err, events.ErrInboxNotFound) ||
		errors.Is(err, events.ErrInvalidInboxScope) || errors.Is(err, events.ErrInvalidInboxEnvelope) {
		return err
	}
	return events.ErrInboxStoreUnavailable
}

func rollbackInboxTransaction(transaction pgx.Tx) {
	if transaction == nil {
		return
	}
	_ = transaction.Rollback(context.Background())
}
