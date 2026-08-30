package eventstore

import (
	"context"
	"errors"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/events"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/platform/postgres/runtimepool"
	"github.com/jackc/pgx/v5"
)

// NativeIMAtomicStore is the transaction boundary for a verified native IM delivery. It admits
// the provider inbox row and appends the canonical event on the same PostgreSQL transaction. The
// standalone NativeIMInboxStore and Store remain useful for migrations and independent callers,
// but transport ingress should use this adapter so an inbox receipt can never commit without its
// event (or vice versa).
type NativeIMAtomicStore struct {
	pool       *runtimepool.Pool
	commitHook func(context.Context, pgx.Tx) error
}

var _ events.AtomicInboxEventStore = (*NativeIMAtomicStore)(nil)

func NewNativeIMAtomicStore(pool *runtimepool.Pool) (*NativeIMAtomicStore, error) {
	if pool == nil {
		return nil, events.ErrInvalidStore
	}
	return &NativeIMAtomicStore{pool: pool, commitHook: commitInboxTransaction}, nil
}

func (store *NativeIMAtomicStore) AdmitAndAppend(
	ctx context.Context,
	projection events.InboxEventProjection,
) (events.AtomicInboxEventAdmission, error) {
	if err := inboxStoreContextError(ctx); err != nil {
		return events.AtomicInboxEventAdmission{}, err
	}
	if store == nil || store.pool == nil || store.commitHook == nil {
		return events.AtomicInboxEventAdmission{}, events.ErrInvalidStore
	}
	batch, err := projection.EventBatch()
	if err != nil {
		return events.AtomicInboxEventAdmission{}, err
	}
	if !validBatch(batch) {
		return events.AtomicInboxEventAdmission{}, events.ErrInvalidBatch
	}
	parts, err := payloadParts(projection.Envelope.Payload)
	if err != nil {
		return events.AtomicInboxEventAdmission{}, events.ErrInvalidInboxEnvelope
	}

	connection, err := store.pool.Acquire(ctx)
	if err != nil {
		return events.AtomicInboxEventAdmission{}, events.ErrInboxStoreUnavailable
	}
	released := false
	release := func() {
		if !released {
			connection.Release()
			released = true
		}
	}
	defer release()
	transaction, err := connection.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.Serializable})
	if err != nil {
		return events.AtomicInboxEventAdmission{}, mapInboxStoreError(ctx, err)
	}
	defer rollbackInboxTransaction(transaction)
	if err := bindTenant(ctx, transaction, projection.Envelope.Scope.TenantID); err != nil {
		return events.AtomicInboxEventAdmission{}, mapInboxStoreError(ctx, err)
	}

	status, err := admitNativeIMInboxTx(ctx, transaction, projection.Envelope, parts)
	if err != nil {
		return events.AtomicInboxEventAdmission{}, mapInboxStoreError(ctx, err)
	}
	if status != "inserted" && status != "replayed" && status != "conflict" {
		return events.AtomicInboxEventAdmission{}, events.ErrInboxStoreUnavailable
	}
	if status == "conflict" {
		return events.AtomicInboxEventAdmission{}, events.ErrInboxDigestConflict
	}
	receipt, err := readNativeIMInboxReceipt(ctx, transaction, projection.Envelope.Scope, projection.Envelope.EventID)
	if err != nil {
		return events.AtomicInboxEventAdmission{}, err
	}
	if receipt.Envelope.EventDigest != projection.Envelope.EventDigest ||
		receipt.Envelope.Payload.Digest() != projection.Envelope.Payload.Digest() {
		return events.AtomicInboxEventAdmission{}, events.ErrInboxDigestConflict
	}

	var appendResult events.AppendResult
	if status == "inserted" {
		appendResult, err = appendBatchTx(ctx, transaction, batch)
		if err != nil {
			return events.AtomicInboxEventAdmission{}, err
		}
		if appendResult.Replayed || len(appendResult.Events) != 1 ||
			!sameStoredEvent(appendResult.Events[0], batch.Events[0], projection.Envelope.EventDigest,
				batch.StreamID, workspaceValue(batch.WorkspaceID), appendResult.Events[0].Sequence) {
			return events.AtomicInboxEventAdmission{}, events.ErrInboxEventInconsistent
		}
	} else {
		// A replayed inbox row must already have its event. Do not silently repair an
		// inconsistent historical state by appending with a caller-selected revision.
		stored, readErr := readEvent(ctx, transaction, batch.TenantID, workspaceValue(batch.WorkspaceID), batch.Events[0].EventID)
		if errors.Is(readErr, errEventNotFound) {
			return events.AtomicInboxEventAdmission{}, events.ErrInboxEventInconsistent
		}
		if readErr != nil {
			return events.AtomicInboxEventAdmission{}, mapEventReadError(readErr)
		}
		if !sameStoredEvent(stored, batch.Events[0], projection.Envelope.EventDigest,
			batch.StreamID, workspaceValue(batch.WorkspaceID), stored.Sequence) {
			return events.AtomicInboxEventAdmission{}, events.ErrInboxEventInconsistent
		}
		appendResult = events.AppendResult{Events: []events.StoredEvent{stored}, Replayed: true}
	}

	if err := store.commitHook(ctx, transaction); err != nil {
		if definiteInboxRollback(err) {
			return events.AtomicInboxEventAdmission{}, mapInboxStoreError(ctx, err)
		}
		quarantineInboxConnection(connection)
		released = true
		reconcileContext, cancel := context.WithTimeout(context.Background(), inboxReconcileTimeout)
		defer cancel()
		reconciled, reconcileErr := store.reconcile(reconcileContext, projection)
		if reconcileErr == nil {
			return reconciled, nil
		}
		return events.AtomicInboxEventAdmission{}, events.ErrInboxCommitUnknown
	}
	inboxStatus := events.InboxReplayed
	if status == "inserted" {
		inboxStatus = events.InboxInserted
	}
	return events.AtomicInboxEventAdmission{
		Inbox:  events.InboxAdmission{Status: inboxStatus, Receipt: receipt},
		Append: appendResult,
	}, nil
}

func (store *NativeIMAtomicStore) reconcile(
	ctx context.Context,
	projection events.InboxEventProjection,
) (events.AtomicInboxEventAdmission, error) {
	if err := inboxStoreContextError(ctx); err != nil {
		return events.AtomicInboxEventAdmission{}, err
	}
	batch, err := projection.EventBatch()
	if err != nil || !validBatch(batch) {
		return events.AtomicInboxEventAdmission{}, events.ErrInvalidInboxEvent
	}
	connection, err := store.pool.Acquire(ctx)
	if err != nil {
		return events.AtomicInboxEventAdmission{}, events.ErrInboxCommitUnknown
	}
	defer connection.Release()
	transaction, err := connection.BeginTx(ctx, pgx.TxOptions{
		IsoLevel: pgx.RepeatableRead, AccessMode: pgx.ReadOnly,
	})
	if err != nil {
		return events.AtomicInboxEventAdmission{}, mapInboxStoreError(ctx, err)
	}
	defer rollbackInboxTransaction(transaction)
	if err := bindTenant(ctx, transaction, batch.TenantID); err != nil {
		return events.AtomicInboxEventAdmission{}, mapInboxStoreError(ctx, err)
	}
	receipt, err := readNativeIMInboxReceipt(ctx, transaction, projection.Envelope.Scope, projection.Envelope.EventID)
	if err != nil {
		return events.AtomicInboxEventAdmission{}, err
	}
	stored, err := readEvent(ctx, transaction, batch.TenantID, workspaceValue(batch.WorkspaceID), batch.Events[0].EventID)
	if errors.Is(err, errEventNotFound) {
		return events.AtomicInboxEventAdmission{}, events.ErrInboxCommitUnknown
	}
	if err != nil {
		return events.AtomicInboxEventAdmission{}, mapEventReadError(err)
	}
	if receipt.Envelope.EventDigest != projection.Envelope.EventDigest ||
		receipt.Envelope.Payload.Digest() != projection.Envelope.Payload.Digest() ||
		!sameStoredEvent(stored, batch.Events[0], projection.Envelope.EventDigest,
			batch.StreamID, workspaceValue(batch.WorkspaceID), stored.Sequence) {
		return events.AtomicInboxEventAdmission{}, events.ErrInboxEventInconsistent
	}
	if err := transaction.Commit(ctx); err != nil {
		return events.AtomicInboxEventAdmission{}, mapInboxStoreError(ctx, err)
	}
	return events.AtomicInboxEventAdmission{
		Inbox: events.InboxAdmission{
			Status: events.InboxReplayed, Receipt: receipt, ResolvedAfterUnknown: true,
		},
		Append: events.AppendResult{Events: []events.StoredEvent{stored}, Replayed: true},
	}, nil
}
