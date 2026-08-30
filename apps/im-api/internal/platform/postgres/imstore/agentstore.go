package imstore

import (
	"context"
	"errors"
	"slices"
	"strings"
	"time"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/agentstore"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
	store "github.com/huapohen/quantum-entanglement/apps/im-api/internal/imstore"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

const maxAgentStorePostgresRevision uint64 = 1<<63 - 1

// CurrentDefinition reads the catalog row inside the Unit of Work transaction. The tenant
// predicate is repeated even though RLS is enabled so a missing context can never widen a query.
func (repositories *tenantRepositories) CurrentDefinition(
	ctx context.Context,
	definitionID im.AgentDefinitionID,
) (agentstore.DefinitionSnapshot, error) {
	if err := repositories.agentStoreUsable(ctx); err != nil {
		return agentstore.DefinitionSnapshot{}, repositories.agentReadFailure(err)
	}
	if definitionID.IsZero() {
		return agentstore.DefinitionSnapshot{}, repositories.agentReadFailure(agentstore.ErrInvalidValue)
	}
	var claimedBy, publisherID, displayName, summary, status string
	var revision int64
	err := repositories.tx.QueryRow(ctx, `
SELECT claimed_by, publisher_id, display_name, summary, status, revision
FROM wanwork_im.agent_definitions
WHERE tenant_id = $1 AND definition_id = $2`, repositories.tenantID.String(), definitionID.String()).Scan(
		&claimedBy, &publisherID, &displayName, &summary, &status, &revision,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return agentstore.DefinitionSnapshot{}, agentstore.ErrNotFound
	}
	if err != nil {
		return agentstore.DefinitionSnapshot{}, repositories.agentReadFailure(agentStoreDBError(err))
	}
	revisionValue, ok := agentStoreRevision(revision)
	if !ok {
		return agentstore.DefinitionSnapshot{}, repositories.agentReadFailure(agentstore.ErrIntegrity)
	}
	claimedByValue, err := im.ParseHumanPrincipalID(claimedBy)
	if err != nil {
		return agentstore.DefinitionSnapshot{}, repositories.agentReadFailure(agentstore.ErrIntegrity)
	}
	tenantID := repositories.tenantID
	publisher, err := agentstore.ParsePublisherID(publisherID)
	if err != nil {
		return agentstore.DefinitionSnapshot{}, repositories.agentReadFailure(agentstore.ErrIntegrity)
	}
	value, err := agentstore.NewDefinitionSnapshot(
		definitionID, tenantID, claimedByValue, publisher, displayName, summary,
		agentstore.DefinitionStatus(status), revisionValue,
	)
	if err != nil {
		return agentstore.DefinitionSnapshot{}, repositories.agentReadFailure(agentstore.ErrIntegrity)
	}
	return value, nil
}

func (repositories *tenantRepositories) CompareAndSwapDefinition(
	ctx context.Context,
	expectedRevision uint64,
	next agentstore.DefinitionSnapshot,
) (agentstore.DefinitionSnapshot, error) {
	if err := repositories.agentStoreUsable(ctx); err != nil {
		return agentstore.DefinitionSnapshot{}, repositories.agentWriteFailure(err)
	}
	if next.IsZero() || next.TenantID() != repositories.tenantID || next.ID().IsZero() {
		return agentstore.DefinitionSnapshot{}, repositories.agentWriteFailure(agentstore.ErrInvalidValue)
	}
	if err := validAgentStoreCASRevision(expectedRevision, next.Revision(), agentstore.ErrDefinitionConflict); err != nil {
		return agentstore.DefinitionSnapshot{}, repositories.agentWriteFailure(err)
	}
	args := []any{
		repositories.tenantID.String(), next.ID().String(), int64(expectedRevision), int64(next.Revision()),
	}
	payload, err := agentstore.EncodeDefinition(next)
	if err != nil {
		return agentstore.DefinitionSnapshot{}, repositories.agentWriteFailure(agentstore.ErrInvalidValue)
	}
	args = append(args, string(payload))
	var changed bool
	err = repositories.tx.QueryRow(ctx, `
SELECT wanwork_im.write_agent_definition_revision($1, $2, $3, $4, $5)`, args...).Scan(&changed)
	if err != nil {
		return agentstore.DefinitionSnapshot{}, repositories.agentWriteFailure(agentStoreDBErrorWithConflict(err, agentstore.ErrDefinitionConflict))
	}
	if !changed {
		return agentstore.DefinitionSnapshot{}, repositories.agentWriteFailure(agentstore.ErrDefinitionConflict)
	}
	return next, nil
}

func (repositories *tenantRepositories) CurrentRelease(
	ctx context.Context,
	releaseID agentstore.ReleaseID,
) (agentstore.ReleaseSnapshot, error) {
	if err := repositories.agentStoreUsable(ctx); err != nil {
		return agentstore.ReleaseSnapshot{}, repositories.agentReadFailure(err)
	}
	if releaseID.IsZero() {
		return agentstore.ReleaseSnapshot{}, repositories.agentReadFailure(agentstore.ErrInvalidValue)
	}
	var (
		definitionID, version, artifactDigest, manifestDigest, personaDigest string
		requestedJSON, prohibitionsJSON, routesJSON                          []byte
		isolation, status                                                    string
		publishedAt                                                          *time.Time
		revision                                                             int64
	)
	err := repositories.tx.QueryRow(ctx, `
SELECT definition_id, version, artifact_digest, manifest_digest, persona_digest,
       requested_capabilities, prohibitions, data_routes, isolation, status, published_at, revision
FROM wanwork_im.agent_releases
WHERE tenant_id = $1 AND release_id = $2`, repositories.tenantID.String(), releaseID.String()).Scan(
		&definitionID, &version, &artifactDigest, &manifestDigest, &personaDigest,
		&requestedJSON, &prohibitionsJSON, &routesJSON, &isolation, &status, &publishedAt, &revision,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return agentstore.ReleaseSnapshot{}, agentstore.ErrNotFound
	}
	if err != nil {
		return agentstore.ReleaseSnapshot{}, repositories.agentReadFailure(agentStoreDBError(err))
	}
	definitionValue, err := im.ParseAgentDefinitionID(definitionID)
	if err != nil {
		return agentstore.ReleaseSnapshot{}, repositories.agentReadFailure(agentstore.ErrIntegrity)
	}
	versionValue, err := im.ParseAgentVersion(version)
	if err != nil {
		return agentstore.ReleaseSnapshot{}, repositories.agentReadFailure(agentstore.ErrIntegrity)
	}
	artifact, err := parseStoredAgentDigest(artifactDigest)
	if err != nil {
		return agentstore.ReleaseSnapshot{}, repositories.agentReadFailure(agentstore.ErrIntegrity)
	}
	manifest, err := parseStoredAgentDigest(manifestDigest)
	if err != nil {
		return agentstore.ReleaseSnapshot{}, repositories.agentReadFailure(agentstore.ErrIntegrity)
	}
	persona, err := parseStoredAgentDigest(personaDigest)
	if err != nil {
		return agentstore.ReleaseSnapshot{}, repositories.agentReadFailure(agentstore.ErrIntegrity)
	}
	requested, err := agentstore.DecodeCapabilitiesJSON(requestedJSON)
	if err != nil {
		return agentstore.ReleaseSnapshot{}, repositories.agentReadFailure(agentstore.ErrIntegrity)
	}
	prohibitions, err := agentstore.DecodeCapabilitiesJSON(prohibitionsJSON)
	if err != nil {
		return agentstore.ReleaseSnapshot{}, repositories.agentReadFailure(agentstore.ErrIntegrity)
	}
	routes, err := agentstore.DecodeRoutesJSON(routesJSON)
	if err != nil {
		return agentstore.ReleaseSnapshot{}, repositories.agentReadFailure(agentstore.ErrIntegrity)
	}
	revisionValue, ok := agentStoreRevision(revision)
	if !ok {
		return agentstore.ReleaseSnapshot{}, repositories.agentReadFailure(agentstore.ErrIntegrity)
	}
	published := time.Time{}
	if publishedAt != nil {
		published = publishedAt.UTC()
	}
	value, err := agentstore.NewReleaseSnapshot(
		releaseID, definitionValue, versionValue, artifact, manifest, persona,
		requested, prohibitions, routes, agentstore.RuntimeIsolation(isolation),
		agentstore.ReleaseStatus(status), published, revisionValue,
	)
	if err != nil {
		return agentstore.ReleaseSnapshot{}, repositories.agentReadFailure(agentstore.ErrIntegrity)
	}
	return value, nil
}

func (repositories *tenantRepositories) CompareAndSwapRelease(
	ctx context.Context,
	expectedRevision uint64,
	next agentstore.ReleaseSnapshot,
) (agentstore.ReleaseSnapshot, error) {
	if err := repositories.agentStoreUsable(ctx); err != nil {
		return agentstore.ReleaseSnapshot{}, repositories.agentWriteFailure(err)
	}
	if next.IsZero() {
		return agentstore.ReleaseSnapshot{}, repositories.agentWriteFailure(agentstore.ErrInvalidValue)
	}
	if err := validAgentStoreCASRevision(expectedRevision, next.Revision(), agentstore.ErrReleaseConflict); err != nil {
		return agentstore.ReleaseSnapshot{}, repositories.agentWriteFailure(err)
	}
	if err := repositories.requireDefinitionTenant(ctx, next.DefinitionID()); err != nil {
		return agentstore.ReleaseSnapshot{}, repositories.agentWriteFailure(err)
	}
	_, err := agentstore.EncodeCapabilitiesJSON(next.RequestedCapabilities())
	if err != nil {
		return agentstore.ReleaseSnapshot{}, repositories.agentWriteFailure(agentstore.ErrInvalidValue)
	}
	_, err = agentstore.EncodeCapabilitiesJSON(next.Prohibitions())
	if err != nil {
		return agentstore.ReleaseSnapshot{}, repositories.agentWriteFailure(agentstore.ErrInvalidValue)
	}
	_, err = agentstore.EncodeRoutesJSON(next.DataRoutes())
	if err != nil {
		return agentstore.ReleaseSnapshot{}, repositories.agentWriteFailure(agentstore.ErrInvalidValue)
	}
	payload, err := agentstore.EncodeRelease(next)
	if err != nil {
		return agentstore.ReleaseSnapshot{}, repositories.agentWriteFailure(agentstore.ErrInvalidValue)
	}
	if expectedRevision == 0 {
		var changed bool
		err := repositories.tx.QueryRow(ctx, `
SELECT wanwork_im.write_agent_release_revision($1, $2, $3, $4, $5)`,
			repositories.tenantID.String(), next.ID().String(), int64(expectedRevision), int64(next.Revision()), string(payload),
		).Scan(&changed)
		if err != nil {
			return agentstore.ReleaseSnapshot{}, repositories.agentWriteFailure(agentStoreDBErrorWithConflict(err, agentstore.ErrReleaseConflict))
		}
		if !changed {
			return agentstore.ReleaseSnapshot{}, repositories.agentWriteFailure(agentstore.ErrReleaseConflict)
		}
		return next, nil
	}
	current, err := repositories.CurrentRelease(ctx, next.ID())
	if err != nil {
		if errors.Is(err, agentstore.ErrNotFound) {
			return agentstore.ReleaseSnapshot{}, repositories.agentWriteFailure(agentstore.ErrReleaseConflict)
		}
		return agentstore.ReleaseSnapshot{}, repositories.agentWriteFailure(err)
	}
	if current.Revision() != expectedRevision || !sameReleaseIdentity(current, next) {
		return agentstore.ReleaseSnapshot{}, repositories.agentWriteFailure(agentstore.ErrReleaseConflict)
	}
	var changed bool
	err = repositories.tx.QueryRow(ctx, `
SELECT wanwork_im.write_agent_release_revision($1, $2, $3, $4, $5)`,
		repositories.tenantID.String(), next.ID().String(), int64(expectedRevision), int64(next.Revision()), string(payload),
	).Scan(&changed)
	if err != nil {
		return agentstore.ReleaseSnapshot{}, repositories.agentWriteFailure(agentStoreDBErrorWithConflict(err, agentstore.ErrReleaseConflict))
	}
	if !changed {
		return agentstore.ReleaseSnapshot{}, repositories.agentWriteFailure(agentstore.ErrReleaseConflict)
	}
	return next, nil
}

func (repositories *tenantRepositories) CurrentPassport(
	ctx context.Context,
	releaseID agentstore.ReleaseID,
) (agentstore.TrustPassport, error) {
	if err := repositories.agentStoreUsable(ctx); err != nil {
		return agentstore.TrustPassport{}, repositories.agentReadFailure(err)
	}
	release, err := repositories.CurrentRelease(ctx, releaseID)
	if err != nil {
		return agentstore.TrustPassport{}, err
	}
	definition, err := repositories.CurrentDefinition(ctx, release.DefinitionID())
	if err != nil {
		return agentstore.TrustPassport{}, err
	}
	var definitionID, status string
	var attestationsJSON []byte
	var revision int64
	err = repositories.tx.QueryRow(ctx, `
SELECT definition_id, status, attestations, revision
FROM wanwork_im.agent_passports
WHERE tenant_id = $1 AND release_id = $2`, repositories.tenantID.String(), releaseID.String()).Scan(
		&definitionID, &status, &attestationsJSON, &revision,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return agentstore.TrustPassport{}, agentstore.ErrNotFound
	}
	if err != nil {
		return agentstore.TrustPassport{}, repositories.agentReadFailure(agentStoreDBError(err))
	}
	if definitionID != definition.ID().String() {
		return agentstore.TrustPassport{}, repositories.agentReadFailure(agentstore.ErrIntegrity)
	}
	attestations, err := agentstore.DecodeAttestationsJSON(attestationsJSON)
	if err != nil {
		return agentstore.TrustPassport{}, repositories.agentReadFailure(agentstore.ErrIntegrity)
	}
	revisionValue, ok := agentStoreRevision(revision)
	if !ok {
		return agentstore.TrustPassport{}, repositories.agentReadFailure(agentstore.ErrIntegrity)
	}
	value, err := agentstore.NewTrustPassport(definition, release, attestations, agentstore.PassportStatus(status), revisionValue)
	if err != nil {
		return agentstore.TrustPassport{}, repositories.agentReadFailure(agentstore.ErrIntegrity)
	}
	return value, nil
}

func (repositories *tenantRepositories) CompareAndSwapPassport(
	ctx context.Context,
	expectedRevision uint64,
	next agentstore.TrustPassport,
) (agentstore.TrustPassport, error) {
	if err := repositories.agentStoreUsable(ctx); err != nil {
		return agentstore.TrustPassport{}, repositories.agentWriteFailure(err)
	}
	if next.IsZero() || next.Definition().TenantID() != repositories.tenantID {
		return agentstore.TrustPassport{}, repositories.agentWriteFailure(agentstore.ErrInvalidValue)
	}
	if err := validAgentStoreCASRevision(expectedRevision, next.Revision(), agentstore.ErrPassportConflict); err != nil {
		return agentstore.TrustPassport{}, repositories.agentWriteFailure(err)
	}
	if err := repositories.requireDefinitionTenant(ctx, next.Definition().ID()); err != nil {
		return agentstore.TrustPassport{}, repositories.agentWriteFailure(err)
	}
	_, err := agentstore.EncodeAttestationsJSON(next.Attestations())
	if err != nil {
		return agentstore.TrustPassport{}, repositories.agentWriteFailure(agentstore.ErrInvalidValue)
	}
	payload, err := agentstore.EncodeTrustPassport(next)
	if err != nil {
		return agentstore.TrustPassport{}, repositories.agentWriteFailure(agentstore.ErrInvalidValue)
	}
	if expectedRevision == 0 {
		var changed bool
		err := repositories.tx.QueryRow(ctx, `
SELECT wanwork_im.write_agent_passport_revision($1, $2, $3, $4, $5)`,
			repositories.tenantID.String(), next.Release().ID().String(), int64(expectedRevision), int64(next.Revision()), string(payload),
		).Scan(&changed)
		if err != nil {
			return agentstore.TrustPassport{}, repositories.agentWriteFailure(agentStoreDBErrorWithConflict(err, agentstore.ErrPassportConflict))
		}
		if !changed {
			return agentstore.TrustPassport{}, repositories.agentWriteFailure(agentstore.ErrPassportConflict)
		}
		return next, nil
	}
	current, err := repositories.CurrentPassport(ctx, next.Release().ID())
	if err != nil {
		if errors.Is(err, agentstore.ErrNotFound) {
			return agentstore.TrustPassport{}, repositories.agentWriteFailure(agentstore.ErrPassportConflict)
		}
		return agentstore.TrustPassport{}, repositories.agentWriteFailure(err)
	}
	if current.Revision() != expectedRevision || current.Definition().ID() != next.Definition().ID() || current.Release().ID() != next.Release().ID() {
		return agentstore.TrustPassport{}, repositories.agentWriteFailure(agentstore.ErrPassportConflict)
	}
	var changed bool
	err = repositories.tx.QueryRow(ctx, `
SELECT wanwork_im.write_agent_passport_revision($1, $2, $3, $4, $5)`,
		repositories.tenantID.String(), next.Release().ID().String(), int64(expectedRevision), int64(next.Revision()), string(payload),
	).Scan(&changed)
	if err != nil {
		return agentstore.TrustPassport{}, repositories.agentWriteFailure(agentStoreDBErrorWithConflict(err, agentstore.ErrPassportConflict))
	}
	if !changed {
		return agentstore.TrustPassport{}, repositories.agentWriteFailure(agentstore.ErrPassportConflict)
	}
	return next, nil
}

func (repositories *tenantRepositories) CurrentInstallation(
	ctx context.Context,
	installationID agentstore.InstallationID,
) (agentstore.InstallationSnapshot, error) {
	if err := repositories.agentStoreUsable(ctx); err != nil {
		return agentstore.InstallationSnapshot{}, repositories.agentReadFailure(err)
	}
	if installationID.IsZero() {
		return agentstore.InstallationSnapshot{}, repositories.agentReadFailure(agentstore.ErrInvalidValue)
	}
	var (
		workspaceID, definitionID, releaseID, version, actorID, installedBy, status string
		capabilitiesJSON, routesJSON                                                []byte
		createdAt                                                                   time.Time
		disabledAt                                                                  *time.Time
		revision                                                                    int64
	)
	err := repositories.tx.QueryRow(ctx, `
SELECT snapshot.workspace_id, snapshot.definition_id, snapshot.release_id, snapshot.version,
       snapshot.agent_actor_id, snapshot.installed_by, snapshot.granted_capabilities,
       snapshot.bound_data_routes, snapshot.status, snapshot.created_at, snapshot.disabled_at,
       snapshot.revision
FROM wanwork_im.agent_installation_heads AS head
JOIN wanwork_im.agent_installation_snapshots AS snapshot
  ON snapshot.tenant_id = head.tenant_id
 AND snapshot.installation_id = head.installation_id
 AND snapshot.revision = head.current_revision
WHERE head.tenant_id = $1 AND head.installation_id = $2`, repositories.tenantID.String(), installationID.String()).Scan(
		&workspaceID, &definitionID, &releaseID, &version, &actorID, &installedBy,
		&capabilitiesJSON, &routesJSON, &status, &createdAt, &disabledAt, &revision,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return agentstore.InstallationSnapshot{}, agentstore.ErrNotFound
	}
	if err != nil {
		return agentstore.InstallationSnapshot{}, repositories.agentReadFailure(agentStoreDBError(err))
	}
	passportID, err := agentstore.ParseReleaseID(releaseID)
	if err != nil {
		return agentstore.InstallationSnapshot{}, repositories.agentReadFailure(agentstore.ErrIntegrity)
	}
	passport, err := repositories.CurrentPassport(ctx, passportID)
	if err != nil {
		if errors.Is(err, agentstore.ErrNotFound) {
			return agentstore.InstallationSnapshot{}, repositories.agentReadFailure(agentstore.ErrIntegrity)
		}
		return agentstore.InstallationSnapshot{}, err
	}
	capabilities, err := agentstore.DecodeCapabilitiesJSON(capabilitiesJSON)
	if err != nil {
		return agentstore.InstallationSnapshot{}, repositories.agentReadFailure(agentstore.ErrIntegrity)
	}
	routes, err := agentstore.DecodeRouteNamesJSON(routesJSON)
	if err != nil {
		return agentstore.InstallationSnapshot{}, repositories.agentReadFailure(agentstore.ErrIntegrity)
	}
	revisionValue, ok := agentStoreRevision(revision)
	if !ok {
		return agentstore.InstallationSnapshot{}, repositories.agentReadFailure(agentstore.ErrIntegrity)
	}
	value, err := agentstore.NewInstallationSnapshot(
		installationID, repositories.tenantID,
		mustAgentWorkspaceID(workspaceID), mustAgentActorID(actorID), mustAgentPrincipalID(installedBy), passport,
		capabilities, routes, agentstore.InstallationStatus(status), createdAt.UTC(), agentStoreTimeOrZero(disabledAt), revisionValue,
	)
	if err != nil || value.DefinitionID().String() != definitionID || value.Version().String() != version {
		return agentstore.InstallationSnapshot{}, repositories.agentReadFailure(agentstore.ErrIntegrity)
	}
	return value, nil
}

func (repositories *tenantRepositories) CompareAndSwapInstallation(
	ctx context.Context,
	expectedRevision uint64,
	next agentstore.InstallationSnapshot,
) (agentstore.InstallationSnapshot, error) {
	if err := repositories.agentStoreUsable(ctx); err != nil {
		return agentstore.InstallationSnapshot{}, repositories.agentWriteFailure(err)
	}
	if next.IsZero() || next.TenantID() != repositories.tenantID {
		return agentstore.InstallationSnapshot{}, repositories.agentWriteFailure(agentstore.ErrInvalidValue)
	}
	if err := validAgentStoreCASRevision(expectedRevision, next.Revision(), agentstore.ErrInstallationConflict); err != nil {
		return agentstore.InstallationSnapshot{}, repositories.agentWriteFailure(err)
	}
	passport, err := repositories.CurrentPassport(ctx, next.ReleaseID())
	if err != nil {
		return agentstore.InstallationSnapshot{}, repositories.agentWriteFailure(err)
	}
	canonical, err := agentstore.EncodeInstallation(next)
	if err != nil {
		return agentstore.InstallationSnapshot{}, repositories.agentWriteFailure(agentstore.ErrInvalidValue)
	}
	if _, err := agentstore.DecodeInstallation(canonical, passport); err != nil {
		return agentstore.InstallationSnapshot{}, repositories.agentWriteFailure(agentstore.ErrInvalidValue)
	}
	_, err = agentstore.EncodeCapabilitiesJSON(next.GrantedCapabilities())
	if err != nil {
		return agentstore.InstallationSnapshot{}, repositories.agentWriteFailure(agentstore.ErrInvalidValue)
	}
	_, err = agentstore.EncodeRouteNamesJSON(next.BoundDataRoutes())
	if err != nil {
		return agentstore.InstallationSnapshot{}, repositories.agentWriteFailure(agentstore.ErrInvalidValue)
	}
	payload, err := agentstore.EncodeInstallation(next)
	if err != nil {
		return agentstore.InstallationSnapshot{}, repositories.agentWriteFailure(agentstore.ErrInvalidValue)
	}
	if expectedRevision == 0 {
		var changed bool
		err = repositories.tx.QueryRow(ctx, `
SELECT wanwork_im.write_agent_installation_revision($1, $2, $3, $4, $5)`,
			repositories.tenantID.String(), next.ID().String(), int64(expectedRevision), int64(next.Revision()), string(payload),
		).Scan(&changed)
		if err != nil {
			return agentstore.InstallationSnapshot{}, repositories.agentWriteFailure(agentStoreDBErrorWithConflict(err, agentstore.ErrInstallationConflict))
		}
		if !changed {
			return agentstore.InstallationSnapshot{}, repositories.agentWriteFailure(agentstore.ErrInstallationConflict)
		}
		return next, nil
	}
	current, err := repositories.CurrentInstallation(ctx, next.ID())
	if err != nil {
		if errors.Is(err, agentstore.ErrNotFound) {
			return agentstore.InstallationSnapshot{}, repositories.agentWriteFailure(agentstore.ErrInstallationConflict)
		}
		return agentstore.InstallationSnapshot{}, repositories.agentWriteFailure(err)
	}
	if current.Revision() != expectedRevision || !sameInstallationIdentity(current, next) {
		return agentstore.InstallationSnapshot{}, repositories.agentWriteFailure(agentstore.ErrInstallationConflict)
	}
	transitionAt := next.CreatedAt()
	if next.Status() == agentstore.InstallationRevoked || next.Status() == agentstore.InstallationOffboarded {
		transitionAt = next.DisabledAt()
	}
	transitioned, err := agentstore.TransitionInstallation(current, next.Status(), transitionAt, next.Revision())
	if err != nil || !sameInstallationState(transitioned, next) {
		return agentstore.InstallationSnapshot{}, repositories.agentWriteFailure(agentstore.ErrInstallationConflict)
	}
	var changed bool
	err = repositories.tx.QueryRow(ctx, `
SELECT wanwork_im.write_agent_installation_revision($1, $2, $3, $4, $5)`,
		repositories.tenantID.String(), next.ID().String(), int64(expectedRevision), int64(next.Revision()), string(payload),
	).Scan(&changed)
	if err != nil {
		return agentstore.InstallationSnapshot{}, repositories.agentWriteFailure(agentStoreDBErrorWithConflict(err, agentstore.ErrInstallationConflict))
	}
	if !changed {
		return agentstore.InstallationSnapshot{}, repositories.agentWriteFailure(agentstore.ErrInstallationConflict)
	}
	return next, nil
}

func (repositories *tenantRepositories) requireDefinitionTenant(ctx context.Context, definitionID im.AgentDefinitionID) error {
	var exists bool
	err := repositories.tx.QueryRow(ctx, `
SELECT EXISTS (
    SELECT 1 FROM wanwork_im.agent_definitions
    WHERE tenant_id = $1 AND definition_id = $2
)`, repositories.tenantID.String(), definitionID.String()).Scan(&exists)
	if err != nil {
		return agentStoreDBError(err)
	}
	if !exists {
		return agentstore.ErrInvalidValue
	}
	return nil
}

func (repositories *tenantRepositories) agentStoreUsable(ctx context.Context) error {
	if err := repositories.usable(ctx); err != nil {
		if errors.Is(err, store.ErrInvalidRequest) {
			return agentstore.ErrInvalidValue
		}
		return agentstore.ErrStoreUnavailable
	}
	return nil
}

func (repositories *tenantRepositories) agentReadFailure(err error) error {
	if err == nil || errors.Is(err, agentstore.ErrNotFound) {
		return err
	}
	return repositories.poison(err)
}

func (repositories *tenantRepositories) agentWriteFailure(err error) error {
	if err == nil {
		return nil
	}
	return repositories.poison(err)
}

func agentStoreRevision(value int64) (uint64, bool) {
	if value <= 0 {
		return 0, false
	}
	return uint64(value), uint64(value) <= maxAgentStorePostgresRevision
}

func validAgentStoreCASRevision(expected, next uint64, conflict error) error {
	if expected > maxAgentStorePostgresRevision || next > maxAgentStorePostgresRevision {
		return conflict
	}
	if expected == 0 {
		if next != 1 {
			return conflict
		}
		return nil
	}
	if expected == maxAgentStorePostgresRevision || next != expected+1 {
		return conflict
	}
	return nil
}

func parseStoredAgentDigest(value string) (agentstore.SHA256Digest, error) {
	if !strings.HasPrefix(value, "sha256:") {
		return agentstore.SHA256Digest{}, agentstore.ErrInvalidValue
	}
	return agentstore.ParseSHA256Digest(strings.TrimPrefix(value, "sha256:"))
}

func storedAgentDigest(value agentstore.SHA256Digest) string { return "sha256:" + value.Hex() }

func agentStoreDBError(err error) error {
	if errors.Is(err, pgx.ErrNoRows) {
		return agentstore.ErrNotFound
	}
	var postgresError *pgconn.PgError
	if errors.As(err, &postgresError) {
		switch postgresError.Code {
		case "23502", "23503", "23514", "23522":
			return agentstore.ErrIntegrity
		case "40001", "40P01", "42501", "57P01", "57P02", "57P03":
			return agentstore.ErrStoreUnavailable
		}
	}
	return agentstore.ErrStoreUnavailable
}

func agentStoreDBErrorWithConflict(err error, conflict error) error {
	var postgresError *pgconn.PgError
	if errors.As(err, &postgresError) && postgresError.Code == "23505" {
		return conflict
	}
	return agentStoreDBError(err)
}

func sameReleaseIdentity(left, right agentstore.ReleaseSnapshot) bool {
	// Status, published_at and revision are the only fields allowed to advance through this CAS.
	return left.ID() == right.ID() && left.DefinitionID() == right.DefinitionID() &&
		left.Version() == right.Version() && left.ArtifactDigest() == right.ArtifactDigest() &&
		left.ManifestDigest() == right.ManifestDigest() && left.PersonaDigest() == right.PersonaDigest() &&
		slices.Equal(left.RequestedCapabilities(), right.RequestedCapabilities()) &&
		slices.Equal(left.Prohibitions(), right.Prohibitions()) &&
		slices.EqualFunc(left.DataRoutes(), right.DataRoutes(), func(a, b agentstore.DataRoute) bool {
			return a.Name() == b.Name() && a.Direction() == b.Direction() && a.Classification() == b.Classification() &&
				a.RetentionDays() == b.RetentionDays() && slices.Equal(a.Destinations(), b.Destinations())
		}) && left.Isolation() == right.Isolation()
}

func sameInstallationIdentity(left, right agentstore.InstallationSnapshot) bool {
	return left.ID() == right.ID() && left.TenantID() == right.TenantID() && left.WorkspaceID() == right.WorkspaceID() &&
		left.DefinitionID() == right.DefinitionID() && left.ReleaseID() == right.ReleaseID() && left.Version() == right.Version() &&
		left.AgentActor() == right.AgentActor() && left.InstalledBy() == right.InstalledBy() &&
		slices.Equal(left.GrantedCapabilities(), right.GrantedCapabilities()) && slices.Equal(left.BoundDataRoutes(), right.BoundDataRoutes()) &&
		left.CreatedAt().Equal(right.CreatedAt())
}

func sameInstallationState(left, right agentstore.InstallationSnapshot) bool {
	return sameInstallationIdentity(left, right) && left.Status() == right.Status() &&
		left.DisabledAt().Equal(right.DisabledAt()) && left.Revision() == right.Revision()
}

func agentStoreTimeOrZero(value *time.Time) time.Time {
	if value == nil {
		return time.Time{}
	}
	return value.UTC()
}

func mustAgentWorkspaceID(value string) im.WorkspaceID {
	parsed, _ := im.ParseWorkspaceID(value)
	return parsed
}

func mustAgentActorID(value string) im.ActorID {
	parsed, _ := im.ParseActorID(value)
	return parsed
}

func mustAgentPrincipalID(value string) im.HumanPrincipalID {
	parsed, _ := im.ParseHumanPrincipalID(value)
	return parsed
}

var _ agentstore.Repository = (*tenantRepositories)(nil)
