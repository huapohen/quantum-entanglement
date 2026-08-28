package imstore

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"sync/atomic"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
	store "github.com/huapohen/quantum-entanglement/apps/im-api/internal/imstore"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

type tenantRepositories struct {
	tx        pgx.Tx
	tenantID  im.TenantID
	active    atomic.Bool
	failureMu sync.Mutex
	failure   error
}

func newTenantRepositories(tx pgx.Tx, tenantID im.TenantID) *tenantRepositories {
	repositories := &tenantRepositories{tx: tx, tenantID: tenantID}
	repositories.active.Store(true)
	return repositories
}

func (repositories *tenantRepositories) Conversations() store.ConversationRepository {
	return repositories
}

func (repositories *tenantRepositories) Authority() store.ConversationAuthorityRepository {
	return repositories
}

func (repositories *tenantRepositories) deactivate() { repositories.active.Store(false) }

func (repositories *tenantRepositories) recordedFailure() error {
	repositories.failureMu.Lock()
	defer repositories.failureMu.Unlock()
	return repositories.failure
}

func (repositories *tenantRepositories) poison(err error) error {
	if err == nil {
		return nil
	}
	repositories.failureMu.Lock()
	if repositories.failure == nil {
		repositories.failure = err
	}
	repositories.failureMu.Unlock()
	return err
}

func (repositories *tenantRepositories) usable(ctx context.Context) error {
	if repositories == nil || repositories.tx == nil || !repositories.active.Load() {
		return store.ErrTransactionClosed
	}
	if ctx == nil || ctx.Err() != nil {
		return store.ErrInvalidRequest
	}
	return nil
}

func (repositories *tenantRepositories) CurrentConversation(
	ctx context.Context,
	reference im.ConversationRef,
) (im.ConversationSnapshot, error) {
	if err := repositories.usable(ctx); err != nil {
		return im.ConversationSnapshot{}, repositories.readError(err)
	}
	if reference.IsZero() || reference.TenantID() != repositories.tenantID {
		return im.ConversationSnapshot{}, repositories.readError(store.ErrInvalidRequest)
	}
	var workspaceValue *string
	var conversationType string
	var status string
	var revision uint64
	err := repositories.tx.QueryRow(ctx, `
SELECT snapshot.workspace_id,
       snapshot.conversation_type,
       snapshot.status,
       snapshot.revision
FROM wanwork_im.conversation_heads AS head
JOIN wanwork_im.conversation_snapshots AS snapshot
  ON snapshot.tenant_id = head.tenant_id
 AND snapshot.conversation_id = head.conversation_id
 AND snapshot.revision = head.current_revision
WHERE head.tenant_id = $1
  AND head.conversation_id = $2`,
		repositories.tenantID.String(),
		reference.ConversationID().String(),
	).Scan(&workspaceValue, &conversationType, &status, &revision)
	if err != nil {
		return im.ConversationSnapshot{}, repositories.readError(mapReadError(err))
	}
	var workspaceID *im.WorkspaceID
	if workspaceValue != nil {
		parsed, err := im.ParseWorkspaceID(*workspaceValue)
		if err != nil {
			return im.ConversationSnapshot{}, repositories.readError(store.ErrIntegrity)
		}
		workspaceID = &parsed
	}
	snapshot, err := im.NewConversationSnapshot(
		reference,
		workspaceID,
		im.ConversationType(conversationType),
		im.ConversationStatus(status),
		im.ConversationID{},
		im.MessageID{},
		im.InvocationID{},
		revision,
	)
	if err != nil {
		return im.ConversationSnapshot{}, repositories.readError(store.ErrIntegrity)
	}
	return snapshot, nil
}

func (repositories *tenantRepositories) CompareAndSwapConversation(
	ctx context.Context,
	expectedRevision uint64,
	next im.ConversationSnapshot,
) (im.ConversationSnapshot, error) {
	if err := repositories.usable(ctx); err != nil {
		return im.ConversationSnapshot{}, repositories.poison(err)
	}
	if err := repositories.validateConversationCAS(expectedRevision, next); err != nil {
		return im.ConversationSnapshot{}, repositories.poison(err)
	}
	reference := next.Ref()
	conversationType := string(next.ConversationType())
	workspaceValue := ""
	if workspaceID, exists := next.WorkspaceID(); exists {
		workspaceValue = workspaceID.String()
	}
	var written bool
	err := repositories.tx.QueryRow(ctx, `
SELECT wanwork_im.write_conversation_revision($1, $2, $3, $4, $5, $6, $7)`,
		repositories.tenantID.String(),
		reference.ConversationID().String(),
		int64(expectedRevision),
		int64(next.Revision()),
		workspaceValue,
		conversationType,
		string(next.Status()),
	).Scan(&written)
	if err != nil {
		return im.ConversationSnapshot{}, repositories.poison(
			mapWriteError(err, store.ErrRevisionConflict),
		)
	}
	if !written {
		return im.ConversationSnapshot{}, repositories.poison(store.ErrRevisionConflict)
	}
	return next, nil
}

func (repositories *tenantRepositories) validateConversationCAS(
	expectedRevision uint64,
	next im.ConversationSnapshot,
) error {
	if next.IsZero() || next.Ref().TenantID() != repositories.tenantID {
		return store.ErrInvalidRequest
	}
	if next.ConversationType() == im.ConversationAgentThread ||
		!next.ParentConversationID().IsZero() || !next.RootMessageID().IsZero() ||
		!next.AgentInvocationID().IsZero() {
		return store.ErrPersistenceUnsupported
	}
	return validCASRevision(expectedRevision, next.Revision())
}

func (repositories *tenantRepositories) CurrentProviderBinding(
	ctx context.Context,
	externalReference im.ProviderConversationRef,
) (im.ProviderConversationBinding, error) {
	if err := repositories.usable(ctx); err != nil {
		return im.ProviderConversationBinding{}, repositories.readError(err)
	}
	if externalReference.IsZero() {
		return im.ProviderConversationBinding{}, repositories.readError(store.ErrInvalidRequest)
	}
	var conversationID string
	var status string
	var revision uint64
	err := repositories.tx.QueryRow(ctx, `
SELECT snapshot.conversation_id,
       snapshot.status,
       snapshot.revision
FROM wanwork_im.provider_conversation_binding_heads AS head
JOIN wanwork_im.provider_conversation_binding_snapshots AS snapshot
  ON snapshot.tenant_id = head.tenant_id
 AND snapshot.provider = head.provider
 AND snapshot.realm_id = head.realm_id
 AND snapshot.provider_conversation_id = head.provider_conversation_id
 AND snapshot.revision = head.current_revision
WHERE head.tenant_id = $1
  AND head.provider = $2
  AND head.realm_id = $3
  AND head.provider_conversation_id = $4`,
		repositories.tenantID.String(),
		string(externalReference.Provider()),
		externalReference.RealmID().String(),
		externalReference.SubjectID(),
	).Scan(&conversationID, &status, &revision)
	if err != nil {
		return im.ProviderConversationBinding{}, repositories.readError(mapReadError(err))
	}
	parsedConversationID, err := im.ParseConversationID(conversationID)
	if err != nil {
		return im.ProviderConversationBinding{}, repositories.readError(store.ErrIntegrity)
	}
	conversationReference, err := im.NewConversationRef(
		repositories.tenantID,
		parsedConversationID,
	)
	if err != nil {
		return im.ProviderConversationBinding{}, repositories.readError(store.ErrIntegrity)
	}
	binding, err := im.NewProviderConversationBinding(
		externalReference,
		conversationReference,
		im.ExternalIdentityBindingStatus(status),
		revision,
	)
	if err != nil {
		return im.ProviderConversationBinding{}, repositories.readError(store.ErrIntegrity)
	}
	return binding, nil
}

func (repositories *tenantRepositories) CompareAndSwapProviderBinding(
	ctx context.Context,
	expectedRevision uint64,
	next im.ProviderConversationBinding,
) (im.ProviderConversationBinding, error) {
	if err := repositories.usable(ctx); err != nil {
		return im.ProviderConversationBinding{}, repositories.poison(err)
	}
	if next.IsZero() || next.ConversationRef().TenantID() != repositories.tenantID {
		return im.ProviderConversationBinding{}, repositories.poison(store.ErrInvalidRequest)
	}
	if err := validCASRevision(expectedRevision, next.Revision()); err != nil {
		return im.ProviderConversationBinding{}, repositories.poison(err)
	}
	externalReference := next.ExternalRef()
	conversationReference := next.ConversationRef()
	if expectedRevision == 0 {
		if _, err := repositories.tx.Exec(ctx, `
INSERT INTO wanwork_im.provider_conversation_binding_heads (
    tenant_id, provider, realm_id, provider_conversation_id,
    current_revision, current_conversation_id, current_conversation_type, current_status
) VALUES ($1, $2, $3, $4, $5, $6, 'group', $7)`,
			repositories.tenantID.String(),
			string(externalReference.Provider()),
			externalReference.RealmID().String(),
			externalReference.SubjectID(),
			next.Revision(),
			conversationReference.ConversationID().String(),
			string(next.Status()),
		); err != nil {
			return im.ProviderConversationBinding{}, repositories.poison(
				mapWriteError(err, store.ErrRevisionConflict),
			)
		}
	} else {
		tag, err := repositories.tx.Exec(ctx, `
UPDATE wanwork_im.provider_conversation_binding_heads
SET current_revision = $5,
    current_conversation_id = $6,
    current_conversation_type = 'group',
    current_status = $7
WHERE tenant_id = $1
  AND provider = $2
  AND realm_id = $3
  AND provider_conversation_id = $4
  AND current_revision = $8`,
			repositories.tenantID.String(),
			string(externalReference.Provider()),
			externalReference.RealmID().String(),
			externalReference.SubjectID(),
			next.Revision(),
			conversationReference.ConversationID().String(),
			string(next.Status()),
			expectedRevision,
		)
		if err != nil {
			return im.ProviderConversationBinding{}, repositories.poison(
				mapWriteError(err, store.ErrRevisionConflict),
			)
		}
		if tag.RowsAffected() != 1 {
			return im.ProviderConversationBinding{}, repositories.poison(store.ErrRevisionConflict)
		}
	}
	if _, err := repositories.tx.Exec(ctx, `
INSERT INTO wanwork_im.provider_conversation_binding_snapshots (
    tenant_id, provider, realm_id, provider_conversation_id,
    revision, conversation_id, conversation_type, status
) VALUES ($1, $2, $3, $4, $5, $6, 'group', $7)`,
		repositories.tenantID.String(),
		string(externalReference.Provider()),
		externalReference.RealmID().String(),
		externalReference.SubjectID(),
		next.Revision(),
		conversationReference.ConversationID().String(),
		string(next.Status()),
	); err != nil {
		return im.ProviderConversationBinding{}, repositories.poison(
			mapWriteError(err, store.ErrRevisionConflict),
		)
	}
	return next, nil
}

func (repositories *tenantRepositories) CurrentMembership(
	ctx context.Context,
	conversationReference im.ConversationRef,
	actorReference im.ActorRef,
) (im.ConversationMembershipSnapshot, error) {
	if err := repositories.usable(ctx); err != nil {
		return im.ConversationMembershipSnapshot{}, repositories.readError(err)
	}
	if !repositories.referencesMatch(conversationReference, actorReference) {
		return im.ConversationMembershipSnapshot{}, repositories.readError(store.ErrInvalidRequest)
	}
	var role string
	var status string
	var revision uint64
	err := repositories.tx.QueryRow(ctx, `
SELECT snapshot.role, snapshot.status, snapshot.revision
FROM wanwork_im.conversation_membership_heads AS head
JOIN wanwork_im.conversation_membership_snapshots AS snapshot
  ON snapshot.tenant_id = head.tenant_id
 AND snapshot.conversation_id = head.conversation_id
 AND snapshot.actor_id = head.actor_id
 AND snapshot.revision = head.current_revision
WHERE head.tenant_id = $1
  AND head.conversation_id = $2
  AND head.actor_id = $3`,
		repositories.tenantID.String(),
		conversationReference.ConversationID().String(),
		actorReference.ActorID().String(),
	).Scan(&role, &status, &revision)
	if err != nil {
		return im.ConversationMembershipSnapshot{}, repositories.readError(mapReadError(err))
	}
	snapshot, err := im.NewConversationMembershipSnapshot(
		conversationReference,
		actorReference,
		im.ConversationMembershipRole(role),
		im.ConversationMembershipStatus(status),
		revision,
	)
	if err != nil {
		return im.ConversationMembershipSnapshot{}, repositories.readError(store.ErrIntegrity)
	}
	return snapshot, nil
}

func (repositories *tenantRepositories) CompareAndSwapMembership(
	ctx context.Context,
	expectedRevision uint64,
	next im.ConversationMembershipSnapshot,
) (im.ConversationMembershipSnapshot, error) {
	if err := repositories.usable(ctx); err != nil {
		return im.ConversationMembershipSnapshot{}, repositories.poison(err)
	}
	if next.IsZero() || !repositories.referencesMatch(next.ConversationRef(), next.ActorRef()) {
		return im.ConversationMembershipSnapshot{}, repositories.poison(store.ErrInvalidRequest)
	}
	if err := validCASRevision(expectedRevision, next.Revision()); err != nil {
		return im.ConversationMembershipSnapshot{}, repositories.poison(err)
	}
	conversationID := next.ConversationRef().ConversationID().String()
	actorID := next.ActorRef().ActorID().String()
	if expectedRevision == 0 {
		if _, err := repositories.tx.Exec(ctx, `
INSERT INTO wanwork_im.conversation_membership_heads (
    tenant_id, conversation_id, actor_id, current_revision
) VALUES ($1, $2, $3, $4)`,
			repositories.tenantID.String(),
			conversationID,
			actorID,
			next.Revision(),
		); err != nil {
			return im.ConversationMembershipSnapshot{}, repositories.poison(
				mapWriteError(err, store.ErrRevisionConflict),
			)
		}
	} else {
		tag, err := repositories.tx.Exec(ctx, `
UPDATE wanwork_im.conversation_membership_heads
SET current_revision = $4
WHERE tenant_id = $1
  AND conversation_id = $2
  AND actor_id = $3
  AND current_revision = $5`,
			repositories.tenantID.String(),
			conversationID,
			actorID,
			next.Revision(),
			expectedRevision,
		)
		if err != nil {
			return im.ConversationMembershipSnapshot{}, repositories.poison(
				mapWriteError(err, store.ErrRevisionConflict),
			)
		}
		if tag.RowsAffected() != 1 {
			return im.ConversationMembershipSnapshot{}, repositories.poison(store.ErrRevisionConflict)
		}
	}
	if _, err := repositories.tx.Exec(ctx, `
INSERT INTO wanwork_im.conversation_membership_snapshots (
    tenant_id, conversation_id, actor_id, revision, role, status
) VALUES ($1, $2, $3, $4, $5, $6)`,
		repositories.tenantID.String(),
		conversationID,
		actorID,
		next.Revision(),
		string(next.Role()),
		string(next.Status()),
	); err != nil {
		return im.ConversationMembershipSnapshot{}, repositories.poison(
			mapWriteError(err, store.ErrRevisionConflict),
		)
	}
	return next, nil
}

func (repositories *tenantRepositories) CurrentAccess(
	ctx context.Context,
	conversationReference im.ConversationRef,
	actorReference im.ActorRef,
) (im.ConversationAccessSnapshot, error) {
	if err := repositories.usable(ctx); err != nil {
		return im.ConversationAccessSnapshot{}, repositories.readError(err)
	}
	if !repositories.referencesMatch(conversationReference, actorReference) {
		return im.ConversationAccessSnapshot{}, repositories.readError(store.ErrInvalidRequest)
	}
	var revision uint64
	var canRead, canSend, canManageMembers, canManageConversation bool
	var canInvokeAgent, canPublishArtifact bool
	err := repositories.tx.QueryRow(ctx, `
SELECT snapshot.revision,
       snapshot.can_read,
       snapshot.can_send_message,
       snapshot.can_manage_members,
       snapshot.can_manage_conversation,
       snapshot.can_invoke_agent,
       snapshot.can_publish_artifact_reference
FROM wanwork_im.conversation_access_heads AS head
JOIN wanwork_im.conversation_access_snapshots AS snapshot
  ON snapshot.tenant_id = head.tenant_id
 AND snapshot.conversation_id = head.conversation_id
 AND snapshot.actor_id = head.actor_id
 AND snapshot.revision = head.current_revision
WHERE head.tenant_id = $1
  AND head.conversation_id = $2
  AND head.actor_id = $3`,
		repositories.tenantID.String(),
		conversationReference.ConversationID().String(),
		actorReference.ActorID().String(),
	).Scan(
		&revision,
		&canRead,
		&canSend,
		&canManageMembers,
		&canManageConversation,
		&canInvokeAgent,
		&canPublishArtifact,
	)
	if err != nil {
		return im.ConversationAccessSnapshot{}, repositories.readError(mapReadError(err))
	}
	permissions := make([]im.ConversationPermission, 0, 6)
	for _, value := range []struct {
		allowed    bool
		permission im.ConversationPermission
	}{
		{canRead, im.ConversationPermissionRead},
		{canSend, im.ConversationPermissionSendMessage},
		{canManageMembers, im.ConversationPermissionManageMembers},
		{canManageConversation, im.ConversationPermissionManageConversation},
		{canInvokeAgent, im.ConversationPermissionInvokeAgent},
		{canPublishArtifact, im.ConversationPermissionPublishArtifactReference},
	} {
		if value.allowed {
			permissions = append(permissions, value.permission)
		}
	}
	snapshot, err := im.NewConversationAccessSnapshot(
		conversationReference,
		actorReference,
		permissions,
		revision,
	)
	if err != nil {
		return im.ConversationAccessSnapshot{}, repositories.readError(store.ErrIntegrity)
	}
	return snapshot, nil
}

func (repositories *tenantRepositories) CompareAndSwapAccess(
	ctx context.Context,
	expectedRevision uint64,
	next im.ConversationAccessSnapshot,
) (im.ConversationAccessSnapshot, error) {
	if err := repositories.usable(ctx); err != nil {
		return im.ConversationAccessSnapshot{}, repositories.poison(err)
	}
	if next.IsZero() || !repositories.referencesMatch(next.ConversationRef(), next.ActorRef()) {
		return im.ConversationAccessSnapshot{}, repositories.poison(store.ErrInvalidRequest)
	}
	if err := validCASRevision(expectedRevision, next.Revision()); err != nil {
		return im.ConversationAccessSnapshot{}, repositories.poison(err)
	}
	conversationID := next.ConversationRef().ConversationID().String()
	actorID := next.ActorRef().ActorID().String()
	if expectedRevision == 0 {
		if _, err := repositories.tx.Exec(ctx, `
INSERT INTO wanwork_im.conversation_access_heads (
    tenant_id, conversation_id, actor_id, current_revision
) VALUES ($1, $2, $3, $4)`,
			repositories.tenantID.String(),
			conversationID,
			actorID,
			next.Revision(),
		); err != nil {
			return im.ConversationAccessSnapshot{}, repositories.poison(
				mapWriteError(err, store.ErrRevisionConflict),
			)
		}
	} else {
		tag, err := repositories.tx.Exec(ctx, `
UPDATE wanwork_im.conversation_access_heads
SET current_revision = $4
WHERE tenant_id = $1
  AND conversation_id = $2
  AND actor_id = $3
  AND current_revision = $5`,
			repositories.tenantID.String(),
			conversationID,
			actorID,
			next.Revision(),
			expectedRevision,
		)
		if err != nil {
			return im.ConversationAccessSnapshot{}, repositories.poison(
				mapWriteError(err, store.ErrRevisionConflict),
			)
		}
		if tag.RowsAffected() != 1 {
			return im.ConversationAccessSnapshot{}, repositories.poison(store.ErrRevisionConflict)
		}
	}
	if _, err := repositories.tx.Exec(ctx, `
INSERT INTO wanwork_im.conversation_access_snapshots (
    tenant_id, conversation_id, actor_id, revision,
    can_read, can_send_message, can_manage_members,
    can_manage_conversation, can_invoke_agent,
    can_publish_artifact_reference
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)`,
		repositories.tenantID.String(),
		conversationID,
		actorID,
		next.Revision(),
		next.HasPermission(im.ConversationPermissionRead),
		next.HasPermission(im.ConversationPermissionSendMessage),
		next.HasPermission(im.ConversationPermissionManageMembers),
		next.HasPermission(im.ConversationPermissionManageConversation),
		next.HasPermission(im.ConversationPermissionInvokeAgent),
		next.HasPermission(im.ConversationPermissionPublishArtifactReference),
	); err != nil {
		return im.ConversationAccessSnapshot{}, repositories.poison(
			mapWriteError(err, store.ErrRevisionConflict),
		)
	}
	return next, nil
}

func (repositories *tenantRepositories) referencesMatch(
	conversationReference im.ConversationRef,
	actorReference im.ActorRef,
) bool {
	return !conversationReference.IsZero() && !actorReference.IsZero() &&
		conversationReference.TenantID() == repositories.tenantID &&
		actorReference.TenantID() == repositories.tenantID
}

func (repositories *tenantRepositories) readError(err error) error {
	if errors.Is(err, store.ErrNotFound) {
		return err
	}
	return repositories.poison(err)
}

func validCASRevision(expectedRevision uint64, nextRevision uint64) error {
	const maxPostgresRevision uint64 = 1<<63 - 1
	if expectedRevision > maxPostgresRevision || nextRevision > maxPostgresRevision {
		return store.ErrRevisionConflict
	}
	if expectedRevision == 0 {
		if nextRevision != 1 {
			return store.ErrRevisionConflict
		}
		return nil
	}
	if expectedRevision == maxPostgresRevision || nextRevision != expectedRevision+1 {
		return store.ErrRevisionConflict
	}
	return nil
}

func mapReadError(err error) error {
	if errors.Is(err, pgx.ErrNoRows) {
		return store.ErrNotFound
	}
	return mapStoreError(err, store.ErrStoreUnavailable)
}

func mapWriteError(err error, uniqueError error) error {
	var postgresError *pgconn.PgError
	if errors.As(err, &postgresError) {
		switch postgresError.Code {
		case "23505":
			return fmt.Errorf("%w: PostgreSQL constraint %s", uniqueError, postgresError.ConstraintName)
		case "23502", "23503", "23514":
			return fmt.Errorf("%w: PostgreSQL constraint %s", store.ErrIntegrity, postgresError.ConstraintName)
		case "40001", "40P01", "42501", "57P01", "57P02", "57P03":
			return fmt.Errorf("%w: PostgreSQL state %s", store.ErrStoreUnavailable, postgresError.Code)
		}
	}
	return mapStoreError(err, store.ErrStoreUnavailable)
}

func mapStoreError(err error, fallback error) error {
	if errors.Is(err, pgx.ErrTxClosed) {
		return store.ErrTransactionClosed
	}
	if err == nil {
		return nil
	}
	return fmt.Errorf("%w: %v", fallback, err)
}

var _ store.TenantRepositories = (*tenantRepositories)(nil)
var _ store.ConversationRepository = (*tenantRepositories)(nil)
var _ store.ConversationAuthorityRepository = (*tenantRepositories)(nil)
