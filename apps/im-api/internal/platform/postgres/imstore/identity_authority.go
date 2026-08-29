package imstore

import (
	"context"
	"errors"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/auth"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
	store "github.com/huapohen/quantum-entanglement/apps/im-api/internal/imstore"
	"github.com/jackc/pgx/v5"
)

// Identity returns the current identity authority view bound to this unit-of-work
// transaction. The transaction is Repeatable Read for reads, so all four joins observe one
// database snapshot. It is deliberately exposed as a read-only contract.
func (repositories *tenantRepositories) Identity() store.IdentityAuthorityRepository {
	return repositories
}

func (repositories *tenantRepositories) CurrentHumanIdentityBinding(
	ctx context.Context,
	reference im.ExternalIdentityRef,
) (im.HumanExternalIdentityBinding, error) {
	if err := repositories.usable(ctx); err != nil {
		return im.HumanExternalIdentityBinding{}, mapIdentityRepositoryError(err)
	}
	if reference.IsZero() || reference.Provider() != im.IdentityProviderClerk {
		return im.HumanExternalIdentityBinding{}, store.ErrInvalidRequest
	}
	var currentRevision int64
	var currentPrincipalID, currentStatus string
	err := repositories.tx.QueryRow(ctx, `
SELECT current_revision, current_principal_id, current_status
FROM wanwork_im.human_identity_binding_heads
WHERE provider = $1
  AND realm_id = $2
  AND subject_id = $3`,
		string(reference.Provider()), reference.RealmID().String(), reference.SubjectID(),
	).Scan(&currentRevision, &currentPrincipalID, &currentStatus)
	if errors.Is(err, pgx.ErrNoRows) {
		return im.HumanExternalIdentityBinding{}, auth.ErrContextAuthorityMissing
	}
	if err != nil {
		return im.HumanExternalIdentityBinding{}, auth.ErrContextUnavailable
	}
	revision, ok := postgresRevision(currentRevision)
	if !ok || currentPrincipalID == "" || currentStatus == "" {
		return im.HumanExternalIdentityBinding{}, auth.ErrContextIntegrity
	}
	var snapshotPrincipalID, snapshotStatus string
	var snapshotRevision int64
	err = repositories.tx.QueryRow(ctx, `
SELECT principal_id, status, revision
FROM wanwork_im.human_identity_binding_snapshots
WHERE provider = $1
  AND realm_id = $2
  AND subject_id = $3
  AND revision = $4`,
		string(reference.Provider()), reference.RealmID().String(), reference.SubjectID(), currentRevision,
	).Scan(&snapshotPrincipalID, &snapshotStatus, &snapshotRevision)
	if errors.Is(err, pgx.ErrNoRows) {
		return im.HumanExternalIdentityBinding{}, auth.ErrContextIntegrity
	}
	if err != nil {
		return im.HumanExternalIdentityBinding{}, auth.ErrContextUnavailable
	}
	if snapshotRevision != currentRevision || snapshotPrincipalID != currentPrincipalID ||
		snapshotStatus != currentStatus {
		return im.HumanExternalIdentityBinding{}, auth.ErrContextIntegrity
	}
	principalID, err := im.ParseHumanPrincipalID(snapshotPrincipalID)
	if err != nil {
		return im.HumanExternalIdentityBinding{}, auth.ErrContextIntegrity
	}
	binding, err := im.NewHumanExternalIdentityBinding(
		reference, principalID, im.ExternalIdentityBindingStatus(snapshotStatus), revision,
	)
	if err != nil {
		return im.HumanExternalIdentityBinding{}, auth.ErrContextIntegrity
	}
	return binding, nil
}

func (repositories *tenantRepositories) CurrentHumanPrincipal(
	ctx context.Context,
	principalID im.HumanPrincipalID,
) (im.HumanPrincipalSnapshot, error) {
	if err := repositories.usable(ctx); err != nil {
		return im.HumanPrincipalSnapshot{}, mapIdentityRepositoryError(err)
	}
	if principalID.IsZero() {
		return im.HumanPrincipalSnapshot{}, store.ErrInvalidRequest
	}
	var currentRevision int64
	err := repositories.tx.QueryRow(ctx, `
SELECT current_revision
FROM wanwork_im.human_principal_heads
WHERE principal_id = $1`, principalID.String()).Scan(&currentRevision)
	if errors.Is(err, pgx.ErrNoRows) {
		return im.HumanPrincipalSnapshot{}, auth.ErrContextAuthorityMissing
	}
	if err != nil {
		return im.HumanPrincipalSnapshot{}, auth.ErrContextUnavailable
	}
	revision, ok := postgresRevision(currentRevision)
	if !ok {
		return im.HumanPrincipalSnapshot{}, auth.ErrContextIntegrity
	}
	var snapshotStatus string
	var snapshotRevision int64
	err = repositories.tx.QueryRow(ctx, `
SELECT status, revision
FROM wanwork_im.human_principal_snapshots
WHERE principal_id = $1
  AND revision = $2`, principalID.String(), currentRevision).Scan(&snapshotStatus, &snapshotRevision)
	if errors.Is(err, pgx.ErrNoRows) {
		return im.HumanPrincipalSnapshot{}, auth.ErrContextIntegrity
	}
	if err != nil {
		return im.HumanPrincipalSnapshot{}, auth.ErrContextUnavailable
	}
	if snapshotRevision != currentRevision {
		return im.HumanPrincipalSnapshot{}, auth.ErrContextIntegrity
	}
	snapshot, err := im.NewHumanPrincipalSnapshot(
		principalID, im.HumanPrincipalStatus(snapshotStatus), revision,
	)
	if err != nil {
		return im.HumanPrincipalSnapshot{}, auth.ErrContextIntegrity
	}
	return snapshot, nil
}

func (repositories *tenantRepositories) CurrentTenantMembership(
	ctx context.Context,
	tenantID im.TenantID,
	principalID im.HumanPrincipalID,
) (im.TenantMembershipSnapshot, error) {
	if err := repositories.usable(ctx); err != nil {
		return im.TenantMembershipSnapshot{}, mapIdentityRepositoryError(err)
	}
	if tenantID.IsZero() || principalID.IsZero() || tenantID != repositories.tenantID {
		return im.TenantMembershipSnapshot{}, store.ErrInvalidRequest
	}
	var currentRevision int64
	var currentActorID string
	err := repositories.tx.QueryRow(ctx, `
SELECT current_revision, actor_id
FROM wanwork_im.tenant_membership_heads
WHERE tenant_id = $1
  AND principal_id = $2`, tenantID.String(), principalID.String()).Scan(&currentRevision, &currentActorID)
	if errors.Is(err, pgx.ErrNoRows) {
		return im.TenantMembershipSnapshot{}, auth.ErrContextAuthorityMissing
	}
	if err != nil {
		return im.TenantMembershipSnapshot{}, auth.ErrContextUnavailable
	}
	revision, ok := postgresRevision(currentRevision)
	if !ok || currentActorID == "" {
		return im.TenantMembershipSnapshot{}, auth.ErrContextIntegrity
	}
	var snapshotActorID, role, status string
	var snapshotRevision int64
	err = repositories.tx.QueryRow(ctx, `
SELECT actor_id, role, status, revision
FROM wanwork_im.tenant_membership_snapshots
WHERE tenant_id = $1
  AND principal_id = $2
  AND revision = $3`, tenantID.String(), principalID.String(), currentRevision).
		Scan(&snapshotActorID, &role, &status, &snapshotRevision)
	if errors.Is(err, pgx.ErrNoRows) {
		return im.TenantMembershipSnapshot{}, auth.ErrContextIntegrity
	}
	if err != nil {
		return im.TenantMembershipSnapshot{}, auth.ErrContextUnavailable
	}
	if snapshotRevision != currentRevision || snapshotActorID != currentActorID {
		return im.TenantMembershipSnapshot{}, auth.ErrContextIntegrity
	}
	actorID, err := im.ParseActorID(snapshotActorID)
	if err != nil {
		return im.TenantMembershipSnapshot{}, auth.ErrContextIntegrity
	}
	actorRef, err := im.NewActorRef(tenantID, actorID)
	if err != nil {
		return im.TenantMembershipSnapshot{}, auth.ErrContextIntegrity
	}
	snapshot, err := im.NewTenantMembershipSnapshot(
		tenantID,
		principalID,
		actorRef,
		im.TenantMembershipRole(role),
		im.TenantMembershipStatus(status),
		revision,
	)
	if err != nil {
		return im.TenantMembershipSnapshot{}, auth.ErrContextIntegrity
	}
	return snapshot, nil
}

func (repositories *tenantRepositories) CurrentActor(
	ctx context.Context,
	reference im.ActorRef,
) (im.ActorSnapshot, error) {
	if err := repositories.usable(ctx); err != nil {
		return im.ActorSnapshot{}, mapIdentityRepositoryError(err)
	}
	if reference.IsZero() || reference.TenantID() != repositories.tenantID {
		return im.ActorSnapshot{}, store.ErrInvalidRequest
	}
	var currentRevision int64
	err := repositories.tx.QueryRow(ctx, `
SELECT current_revision
FROM wanwork_im.actor_heads
WHERE tenant_id = $1
  AND actor_id = $2`, reference.TenantID().String(), reference.ActorID().String()).Scan(&currentRevision)
	if errors.Is(err, pgx.ErrNoRows) {
		return im.ActorSnapshot{}, auth.ErrContextAuthorityMissing
	}
	if err != nil {
		return im.ActorSnapshot{}, auth.ErrContextUnavailable
	}
	revision, ok := postgresRevision(currentRevision)
	if !ok {
		return im.ActorSnapshot{}, auth.ErrContextIntegrity
	}
	var subjectType, status string
	var snapshotRevision int64
	err = repositories.tx.QueryRow(ctx, `
SELECT subject_type, status, revision
FROM wanwork_im.actor_snapshots
WHERE tenant_id = $1
  AND actor_id = $2
  AND revision = $3`, reference.TenantID().String(), reference.ActorID().String(), currentRevision).
		Scan(&subjectType, &status, &snapshotRevision)
	if errors.Is(err, pgx.ErrNoRows) {
		return im.ActorSnapshot{}, auth.ErrContextIntegrity
	}
	if err != nil {
		return im.ActorSnapshot{}, auth.ErrContextUnavailable
	}
	if snapshotRevision != currentRevision {
		return im.ActorSnapshot{}, auth.ErrContextIntegrity
	}
	snapshot, err := im.NewActorSnapshot(
		reference, im.SubjectType(subjectType), im.ActorStatus(status), revision,
	)
	if err != nil {
		return im.ActorSnapshot{}, auth.ErrContextIntegrity
	}
	return snapshot, nil
}

func postgresRevision(value int64) (uint64, bool) {
	if value <= 0 {
		return 0, false
	}
	return uint64(value), true
}

func mapIdentityRepositoryError(err error) error {
	switch {
	case errors.Is(err, store.ErrInvalidRequest), errors.Is(err, store.ErrTransactionClosed):
		return err
	default:
		return auth.ErrContextUnavailable
	}
}

var _ store.IdentityAuthorityRepository = (*tenantRepositories)(nil)
