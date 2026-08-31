package imstore

// ProviderEffectRepository is the PostgreSQL worker seam for provider effects. It intentionally
// accepts an existing transaction: enqueue can run inside the platform command transaction, while
// claim/receipt/reconcile can use a short worker transaction. Callers must bind
// wanwork.tenant_id on that transaction before invoking a method; RLS remains the final guard.

import (
	"context"
	"crypto/rand"
	"errors"
	"fmt"
	"regexp"
	"time"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
	store "github.com/huapohen/quantum-entanglement/apps/im-api/internal/imstore"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

var providerEffectWorkerIDPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$`)

// ProviderEffectRepository implements the durable provider-effect contract over a tenant-bound
// pgx transaction. The transaction owner is responsible for commit/rollback and for binding the
// tenant setting with SET LOCAL before use.
type ProviderEffectRepository struct {
	tx       pgx.Tx
	tenantID im.TenantID
}

func NewProviderEffectRepository(tx pgx.Tx, tenantID im.TenantID) (*ProviderEffectRepository, error) {
	if tx == nil || tenantID.IsZero() {
		return nil, store.ErrInvalidRequest
	}
	return &ProviderEffectRepository{tx: tx, tenantID: tenantID}, nil
}

var _ store.ProviderEffectOutbox = (*ProviderEffectRepository)(nil)

func (repository *ProviderEffectRepository) Enqueue(
	ctx context.Context,
	intent store.ProviderEffectIntent,
) (store.ProviderEffectRecord, bool, error) {
	if err := repository.usable(ctx, intent.TenantID); err != nil {
		return store.ProviderEffectRecord{}, false, err
	}
	if err := intent.Validate(); err != nil {
		return store.ProviderEffectRecord{}, false, err
	}
	var outcome string
	workspaceID := ""
	if intent.WorkspaceID != nil {
		workspaceID = *intent.WorkspaceID
	}
	err := repository.tx.QueryRow(ctx, `
SELECT wanwork_im.write_agent_provider_effect(
    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11
)`,
		intent.TenantID, workspaceID, intent.InstallationID, intent.EffectID,
		string(intent.EffectKind), intent.Provider, intent.ProviderRealmID,
		intent.ProviderSubjectID, intent.OperationKey, intent.RequestRef, intent.RequestDigest.Hex(),
	).Scan(&outcome)
	if err != nil {
		return store.ProviderEffectRecord{}, false, mapProviderEffectWriteError(err)
	}
	switch outcome {
	case "inserted":
	case "replayed":
		record, loadErr := repository.Load(ctx, intent.Key())
		return record, true, loadErr
	case "conflict":
		return store.ProviderEffectRecord{}, false, store.ErrProviderEffectConflict
	default:
		return store.ProviderEffectRecord{}, false, store.ErrIntegrity
	}
	record, err := repository.Load(ctx, intent.Key())
	return record, false, err
}

// ClaimDue atomically fences queued/failed effects and expired leases. A worker must commit the
// transaction before sending provider traffic; a rollback releases row locks without advancing
// the attempt count.
func (repository *ProviderEffectRepository) ClaimDue(
	ctx context.Context,
	tenantID string,
	workerID string,
	lease time.Duration,
	limit int,
) ([]store.ProviderEffectClaim, error) {
	if err := repository.usable(ctx, tenantID); err != nil {
		return nil, err
	}
	if !providerEffectWorkerIDPattern.MatchString(workerID) || lease < time.Microsecond || lease > time.Hour ||
		limit <= 0 || limit > 100 {
		return nil, store.ErrProviderEffectInvalid
	}
	claims := make([]store.ProviderEffectClaim, 0, limit)
	for len(claims) < limit {
		token, digest, err := providerEffectLease(workerID)
		if err != nil {
			return nil, err
		}
		var effectID string
		err = repository.tx.QueryRow(ctx, `
SELECT wanwork_im.claim_agent_provider_effect($1, $2, $3)`,
			tenantID, digest, lease.Microseconds()).Scan(&effectID)
		if err != nil {
			return nil, mapProviderEffectWriteError(err)
		}
		if effectID == "" {
			break
		}
		// Claim rows are decoded by Load so nullable receipt and digest fields cannot be
		// accidentally interpreted as platform authority.
		record, err := repository.Load(ctx, store.ProviderEffectKey{TenantID: tenantID, EffectID: effectID})
		if err != nil {
			return nil, err
		}
		if record.State != store.ProviderEffectSent || record.LeaseExpiresAt.IsZero() {
			return nil, store.ErrIntegrity
		}
		claims = append(claims, store.ProviderEffectClaim{Record: record, LeaseToken: token})
	}
	return claims, nil
}

func (repository *ProviderEffectRepository) Load(
	ctx context.Context,
	key store.ProviderEffectKey,
) (store.ProviderEffectRecord, error) {
	if err := repository.usable(ctx, key.TenantID); err != nil {
		return store.ProviderEffectRecord{}, err
	}
	if err := key.Validate(); err != nil {
		return store.ProviderEffectRecord{}, err
	}
	var (
		workspaceID, providerSubjectID, lastErrorCode                                                                    *string
		installationID, effectID, effectKind, provider, providerRealmID, operationKey, requestRef, requestDigest, status string
		providerReceiptDigest, providerExternalID, providerReceiptStatus                                                 *string
		providerReceiptObservedAt, firstSentAt, lastAttemptAt, committedAt, leaseExpiresAt                               *time.Time
		attemptCount                                                                                                     int64
		createdAt, updatedAt                                                                                             time.Time
	)
	err := repository.tx.QueryRow(ctx, `
SELECT workspace_id, installation_id, effect_id, effect_kind, provider,
       provider_realm_id, provider_subject_id, operation_key, request_ref,
       request_sha256, status, attempt_count, provider_receipt_digest,
       provider_external_id, provider_receipt_status, provider_receipt_observed_at,
       last_error_code, first_sent_at, last_attempt_at, committed_at,
       lease_expires_at, created_at, updated_at
FROM wanwork_im.agent_provider_effects
WHERE tenant_id = $1 AND effect_id = $2`, key.TenantID, key.EffectID).Scan(
		&workspaceID, &installationID, &effectID, &effectKind, &provider,
		&providerRealmID, &providerSubjectID, &operationKey, &requestRef,
		&requestDigest, &status, &attemptCount, &providerReceiptDigest,
		&providerExternalID, &providerReceiptStatus, &providerReceiptObservedAt,
		&lastErrorCode, &firstSentAt, &lastAttemptAt, &committedAt,
		&leaseExpiresAt, &createdAt, &updatedAt,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return store.ProviderEffectRecord{}, store.ErrProviderEffectNotFound
	}
	if err != nil {
		return store.ProviderEffectRecord{}, mapProviderEffectReadError(err)
	}
	parsedDigest, err := store.ParseSHA256Digest(requestDigest)
	if err != nil || attemptCount < 0 || effectID != key.EffectID {
		return store.ProviderEffectRecord{}, store.ErrIntegrity
	}
	intent := store.ProviderEffectIntent{
		TenantID: key.TenantID, WorkspaceID: workspaceID, InstallationID: installationID,
		EffectID: effectID, EffectKind: store.ProviderEffectKind(effectKind), Provider: provider,
		ProviderRealmID: providerRealmID, ProviderSubjectID: valueOrEmpty(providerSubjectID),
		OperationKey: operationKey, RequestRef: requestRef, RequestDigest: parsedDigest,
		CreatedAt: createdAt.UTC(),
	}
	record := store.ProviderEffectRecord{
		Intent: intent, State: store.ProviderEffectState(status), AttemptCount: uint64(attemptCount),
		LastErrorCode: valueOrEmpty(lastErrorCode), FirstSentAt: valueOrZero(firstSentAt),
		LastAttemptAt: valueOrZero(lastAttemptAt), CommittedAt: valueOrZero(committedAt),
		LeaseExpiresAt: valueOrZero(leaseExpiresAt), UpdatedAt: updatedAt.UTC(),
	}
	if providerReceiptStatus != nil {
		if providerReceiptDigest == nil || providerExternalID == nil || providerReceiptObservedAt == nil {
			return store.ProviderEffectRecord{}, store.ErrIntegrity
		}
		receipt := &im.ProviderEffectReceipt{
			OperationKey: operationKey, ExternalID: *providerExternalID,
			Status: im.ProviderEffectStatus(*providerReceiptStatus), ObservedAt: providerReceiptObservedAt.UTC(),
		}
		if receipt.Validate() != nil || providerEffectReceiptDigest(*receipt) != *providerReceiptDigest {
			return store.ProviderEffectRecord{}, store.ErrIntegrity
		}
		record.ProviderReceipt = receipt
	}
	if record.Validate() != nil {
		return store.ProviderEffectRecord{}, store.ErrIntegrity
	}
	return record, nil
}

func (repository *ProviderEffectRepository) RecordReceipt(
	ctx context.Context,
	key store.ProviderEffectKey,
	leaseToken string,
	receipt im.ProviderEffectReceipt,
) (store.ProviderEffectRecord, error) {
	return repository.recordReceipt(ctx, key, leaseToken, receipt, false)
}

func (repository *ProviderEffectRepository) ResolveUnknown(
	ctx context.Context,
	key store.ProviderEffectKey,
	receipt im.ProviderEffectReceipt,
) (store.ProviderEffectRecord, error) {
	return repository.recordReceipt(ctx, key, "", receipt, true)
}

func (repository *ProviderEffectRepository) recordReceipt(
	ctx context.Context,
	key store.ProviderEffectKey,
	leaseToken string,
	receipt im.ProviderEffectReceipt,
	resolveUnknown bool,
) (store.ProviderEffectRecord, error) {
	if err := repository.usable(ctx, key.TenantID); err != nil {
		return store.ProviderEffectRecord{}, err
	}
	if err := key.Validate(); err != nil || receipt.Validate() != nil ||
		(resolveUnknown && receipt.Status == im.ProviderEffectUnknown) ||
		(!resolveUnknown && leaseToken == "") {
		return store.ProviderEffectRecord{}, store.ErrProviderEffectInvalid
	}
	state := string(store.ProviderEffectCommitted)
	if receipt.Status == im.ProviderEffectReplayed {
		state = string(store.ProviderEffectReplayed)
	} else if receipt.Status == im.ProviderEffectUnknown {
		state = string(store.ProviderEffectUnknown)
	}
	digest := providerEffectReceiptDigest(receipt)
	leaseDigest := ""
	if leaseToken != "" {
		leaseDigest = store.DigestBytes([]byte("provider-effect-lease/1\x00" + leaseToken)).Hex()
	}
	var changed bool
	var err error
	if resolveUnknown {
		err = repository.tx.QueryRow(ctx, `
SELECT wanwork_im.resolve_agent_provider_effect($1, $2, $3, $4, $5, $6, $7)`,
			key.TenantID, key.EffectID, receipt.OperationKey, state, digest,
			receipt.ExternalID, receipt.ObservedAt.UTC()).Scan(&changed)
	} else {
		err = repository.tx.QueryRow(ctx, `
SELECT wanwork_im.record_agent_provider_effect_receipt($1, $2, $3, $4, $5, $6, $7, $8)`,
			key.TenantID, key.EffectID, leaseDigest, receipt.OperationKey, state, digest,
			receipt.ExternalID, receipt.ObservedAt.UTC()).Scan(&changed)
	}
	if err != nil {
		return store.ProviderEffectRecord{}, mapProviderEffectWriteError(err)
	}
	if !changed {
		if resolveUnknown {
			return store.ProviderEffectRecord{}, store.ErrProviderEffectState
		}
		return store.ProviderEffectRecord{}, store.ErrProviderEffectLease
	}
	return repository.Load(ctx, key)
}

func (repository *ProviderEffectRepository) MarkUnknown(
	ctx context.Context,
	key store.ProviderEffectKey,
	leaseToken string,
	reasonCode string,
) (store.ProviderEffectRecord, error) {
	return repository.markTerminal(ctx, key, leaseToken, store.ProviderEffectUnknown, reasonCode)
}

func (repository *ProviderEffectRepository) MarkFailed(
	ctx context.Context,
	key store.ProviderEffectKey,
	leaseToken string,
	errorCode string,
) (store.ProviderEffectRecord, error) {
	return repository.markTerminal(ctx, key, leaseToken, store.ProviderEffectFailed, errorCode)
}

func (repository *ProviderEffectRepository) markTerminal(
	ctx context.Context,
	key store.ProviderEffectKey,
	leaseToken string,
	state store.ProviderEffectState,
	errorCode string,
) (store.ProviderEffectRecord, error) {
	if err := repository.usable(ctx, key.TenantID); err != nil {
		return store.ProviderEffectRecord{}, err
	}
	if err := key.Validate(); err != nil || !providerEffectWorkerIDPattern.MatchString(errorCode) || leaseToken == "" {
		return store.ProviderEffectRecord{}, store.ErrProviderEffectInvalid
	}
	leaseDigest := store.DigestBytes([]byte("provider-effect-lease/1\x00" + leaseToken)).Hex()
	var changed bool
	err := repository.tx.QueryRow(ctx, `
SELECT wanwork_im.mark_agent_provider_effect_terminal($1, $2, $3, $4, $5)`,
		key.TenantID, key.EffectID, leaseDigest, string(state), errorCode).Scan(&changed)
	if err != nil {
		return store.ProviderEffectRecord{}, mapProviderEffectWriteError(err)
	}
	if !changed {
		return store.ProviderEffectRecord{}, store.ErrProviderEffectLease
	}
	return repository.Load(ctx, key)
}

func (repository *ProviderEffectRepository) usable(ctx context.Context, tenantID string) error {
	if repository == nil || repository.tx == nil || ctx == nil || ctx.Err() != nil || tenantID != repository.tenantID.String() {
		return store.ErrInvalidRequest
	}
	return nil
}

func providerEffectLease(workerID string) (string, string, error) {
	var bytes [16]byte
	if _, err := rand.Read(bytes[:]); err != nil {
		return "", "", store.ErrProviderEffectLease
	}
	token := fmt.Sprintf("%x.%s", bytes, workerID)
	digest := store.DigestBytes([]byte("provider-effect-lease/1\x00" + token)).Hex()
	return token, digest, nil
}

func providerEffectReceiptDigest(receipt im.ProviderEffectReceipt) string {
	value := "provider-effect-receipt/1\x00" + receipt.OperationKey + "\x00" + receipt.ExternalID +
		"\x00" + string(receipt.Status) + "\x00" + receipt.ObservedAt.UTC().Format(time.RFC3339Nano)
	return store.DigestBytes([]byte(value)).Hex()
}

func valueOrEmpty(value *string) string {
	if value == nil {
		return ""
	}
	return *value
}

func valueOrZero(value *time.Time) time.Time {
	if value == nil {
		return time.Time{}
	}
	return value.UTC()
}

func mapProviderEffectReadError(err error) error {
	if errors.Is(err, pgx.ErrNoRows) {
		return store.ErrProviderEffectNotFound
	}
	return store.ErrStoreUnavailable
}

func mapProviderEffectWriteError(err error) error {
	var postgresError *pgconn.PgError
	if errors.As(err, &postgresError) {
		switch postgresError.Code {
		case "23505":
			return store.ErrProviderEffectConflict
		case "22023", "23503", "23514":
			return store.ErrProviderEffectInvalid
		case "42501":
			return store.ErrProviderEffectLease
		}
	}
	return store.ErrStoreUnavailable
}
