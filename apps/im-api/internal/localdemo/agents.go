package localdemo

import (
	"context"
	"crypto/sha256"
	"errors"
	"slices"
	"strings"
	"time"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/agentstore"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
)

// AgentStorePage is the authenticated catalog projection used by the local Web client. It
// deliberately exposes reviewed declarations and the current installation decision separately;
// neither catalog metadata nor a granted capability is a runtime bearer credential.
type AgentStorePage struct {
	Agents []AgentStoreView `json:"agents"`
}

type AgentStoreInstallInput struct {
	IdempotencyKey      string   `json:"idempotencyKey"`
	GrantedCapabilities []string `json:"grantedCapabilities,omitempty"`
}

type AgentStoreInstallResult struct {
	Agent         AgentStoreView `json:"agent"`
	Replayed      bool           `json:"replayed"`
	CommandStatus string         `json:"commandStatus"`
}

const (
	agentStoreCommandCommitted = "committed"
	agentStoreCommandReplayed  = "replayed"
)

type agentCatalogRecord struct {
	passport     agentstore.TrustPassport
	installation agentstore.InstallationSnapshot
}

type agentInstallRecord struct {
	digest       [sha256.Size]byte
	definitionID string
}

type AgentStoreView struct {
	DefinitionID   string `json:"definitionId"`
	ReleaseID      string `json:"releaseId"`
	InstallationID string `json:"installationId"`
	Name           string `json:"name"`
	Summary        string `json:"summary"`
	Version        string `json:"version"`
	// These are read-only content identities. The projection intentionally carries hashes only;
	// it never exposes artifact bytes, manifest/persona contents, secret locators, or credentials.
	ArtifactDigest        string                     `json:"artifactDigest"`
	ManifestDigest        string                     `json:"manifestDigest"`
	PersonaDigest         string                     `json:"personaDigest"`
	VersionProvenance     AgentVersionProvenanceView `json:"versionProvenance"`
	DefinitionStatus      string                     `json:"definitionStatus"`
	ReleaseStatus         string                     `json:"releaseStatus"`
	PassportStatus        string                     `json:"passportStatus"`
	InstallationStatus    string                     `json:"installationStatus"`
	AgentActorID          string                     `json:"agentActorId"`
	Isolation             string                     `json:"isolation"`
	RequestedCapabilities []string                   `json:"requestedCapabilities"`
	GrantedCapabilities   []string                   `json:"grantedCapabilities"`
	DataRoutes            []AgentDataRouteView       `json:"dataRoutes"`
	Attestations          []string                   `json:"attestations"`
	CanInstall            bool                       `json:"canInstall"`
}

// AgentVersionProvenanceView is the non-secret provenance summary for one immutable release.
// Digest values are lowercase SHA-256 hex strings; publisher/revision/timestamp values are
// catalog evidence, not bearer credentials or runtime authority.
type AgentVersionProvenanceView struct {
	PublisherID        string `json:"publisherId"`
	DefinitionRevision uint64 `json:"definitionRevision"`
	ReleaseRevision    uint64 `json:"releaseRevision"`
	PassportRevision   uint64 `json:"passportRevision"`
	PublishedAt        string `json:"publishedAt"`
	DigestAlgorithm    string `json:"digestAlgorithm"`
}

type AgentDataRouteView struct {
	Name           string   `json:"name"`
	Direction      string   `json:"direction"`
	Classification string   `json:"classification"`
	Destinations   []string `json:"destinations"`
	RetentionDays  uint16   `json:"retentionDays"`
}

// ListAgents returns the catalog and installation projection for the authenticated local tenant.
// The local composition contains one pre-installed and one fully attested installable v0版 Agent;
// production will replace this in-memory read with a tenant-bound repository and action-time resolver.
func (service *Service) ListAgents(ctx context.Context, bearerToken string) (AgentStorePage, error) {
	if service == nil || ctx == nil {
		return AgentStorePage{}, ErrInvalidInput
	}
	if err := service.verifyRequester(ctx, bearerToken); err != nil {
		return AgentStorePage{}, err
	}

	service.mu.Lock()
	defer service.mu.Unlock()
	if len(service.agentCatalog) == 0 {
		return AgentStorePage{}, ErrIntegrity
	}
	agents := make([]AgentStoreView, 0, len(service.agentCatalog))
	for _, record := range service.agentCatalog {
		agents = append(agents, service.agentStoreView(record))
	}
	return AgentStorePage{Agents: agents}, nil
}

// InstallAgent admits a reviewed catalog release into the local tenant. It is deliberately
// explicit and idempotent: the caller supplies a stable action key, while the server binds the
// action to the exact definition/release digest and only accepts a strict subset of the
// capabilities declared by the reviewed Trust Passport. An omitted capability list preserves the
// legacy behavior of granting the complete reviewed request; an explicitly empty list is invalid.
func (service *Service) InstallAgent(
	ctx context.Context,
	bearerToken string,
	definitionIDValue string,
	input AgentStoreInstallInput,
) (AgentStoreInstallResult, error) {
	if service == nil || ctx == nil || !validLocalID(input.IdempotencyKey) {
		return AgentStoreInstallResult{}, ErrInvalidInput
	}
	if err := service.verifyRequester(ctx, bearerToken); err != nil {
		return AgentStoreInstallResult{}, err
	}
	definitionID, err := im.ParseAgentDefinitionID(definitionIDValue)
	if err != nil {
		return AgentStoreInstallResult{}, ErrNotFound
	}

	service.mu.Lock()
	defer service.mu.Unlock()
	var targetIndex = -1
	for index, record := range service.agentCatalog {
		if record.passport.IsZero() || record.passport.Definition().ID() != definitionID {
			continue
		}
		targetIndex = index
		break
	}
	if targetIndex < 0 {
		return AgentStoreInstallResult{}, ErrNotFound
	}
	target := service.agentCatalog[targetIndex]
	// Catalog metadata is only a discovery projection. Installation is an effect and must
	// re-check the reviewed passport at action time so an expired, quarantined, revoked, or
	// otherwise stale release can never be admitted merely because it is still listed.
	if !service.usableAgentPassport(target.passport) {
		return AgentStoreInstallResult{}, ErrForbidden
	}
	grantedCapabilities, capabilityErr := resolveInstallCapabilities(target.passport, input.GrantedCapabilities, service.nowUTC())
	if capabilityErr != nil {
		return AgentStoreInstallResult{}, capabilityErr
	}
	digest := installRequestDigest(definitionID.String(), target.passport, grantedCapabilities)
	requestKey := definitionIDValue + "\x00" + input.IdempotencyKey
	if existing, ok := service.agentInstallRequests[requestKey]; ok {
		if existing.digest != digest {
			return AgentStoreInstallResult{}, ErrConflict
		}
		return AgentStoreInstallResult{Agent: service.agentStoreView(target), Replayed: true, CommandStatus: agentStoreCommandReplayed}, nil
	}
	// An already-active installation is a no-op. Resolve and validate the requested subset before
	// this branch so a replay cannot be used to smuggle an undeclared capability past the gate.
	if !target.installation.IsZero() && target.installation.Status() == agentstore.InstallationActive {
		if input.GrantedCapabilities != nil &&
			!slices.Equal(grantedCapabilities, target.installation.GrantedCapabilities()) {
			return AgentStoreInstallResult{}, ErrConflict
		}
		return AgentStoreInstallResult{Agent: service.agentStoreView(target), Replayed: true, CommandStatus: agentStoreCommandReplayed}, nil
	}
	passport := target.passport
	workspace, ok := service.parent.WorkspaceID()
	if !ok {
		return AgentStoreInstallResult{}, ErrIntegrity
	}
	actorID, err := im.ParseActorID("agt_local_planner")
	if err != nil {
		return AgentStoreInstallResult{}, ErrIntegrity
	}
	installationID, err := agentstore.ParseInstallationID("ins_local_planner")
	if err != nil {
		return AgentStoreInstallResult{}, ErrIntegrity
	}
	installation, err := agentstore.NewInstallationSnapshot(
		installationID, service.parent.Ref().TenantID(), workspace, actorID,
		passport.Definition().ClaimedBy(), passport, grantedCapabilities,
		[]string{"conversation.context"}, agentstore.InstallationActive, service.nowUTC(), time.Time{}, 1,
	)
	if err != nil {
		return AgentStoreInstallResult{}, ErrIntegrity
	}
	provision, err := agentstore.BuildProviderUserProvision(
		installation, passport, service.provider.Profile(), "demo/store/install/"+definitionIDValue,
	)
	if err != nil {
		return AgentStoreInstallResult{}, ErrIntegrity
	}
	if _, receipt, providerErr := service.provider.ProvisionUser(ctx, provision); providerErr != nil || im.RequireCommittedProviderEffect(receipt) != nil {
		if providerErr != nil {
			return AgentStoreInstallResult{}, errors.Join(ErrProvider, providerErr)
		}
		return AgentStoreInstallResult{}, ErrProvider
	}
	// Retire the previous active demo installation before switching the selected runtime. The old
	// actor remains in historical conversations, preserving producer/version provenance. Build the
	// complete CAS set first so a durable backend can commit the control-plane switch atomically.
	retiredInstallations := make([]agentstore.InstallationSnapshot, 0)
	retiredIndexes := make([]int, 0)
	for index, record := range service.agentCatalog {
		if record.installation.IsZero() || record.installation.Status() != agentstore.InstallationActive {
			continue
		}
		retiredInstallation, transitionErr := agentstore.TransitionInstallation(
			record.installation, agentstore.InstallationOffboarded, service.nowUTC(), record.installation.Revision()+1,
		)
		if transitionErr != nil {
			return AgentStoreInstallResult{}, ErrIntegrity
		}
		retiredInstallations = append(retiredInstallations, retiredInstallation)
		retiredIndexes = append(retiredIndexes, index)
	}
	if service.agentStoreBackend != nil {
		if err := service.agentStoreBackend.CommitInstall(
			ctx, service.parent.Ref().TenantID(), input.IdempotencyKey,
			agentstore.SHA256Digest(digest), installation, retiredInstallations,
		); err != nil {
			return AgentStoreInstallResult{}, errors.Join(ErrPersistence, err)
		}
	}
	for index, retiredInstallation := range retiredInstallations {
		service.agentCatalog[retiredIndexes[index]].installation = retiredInstallation
	}
	service.agentCatalog[targetIndex].installation = installation
	service.installation, service.passport = installation, passport
	actorRef, actorRefErr := im.NewActorRef(service.parent.Ref().TenantID(), actorID)
	if actorRefErr != nil {
		return AgentStoreInstallResult{}, ErrIntegrity
	}
	service.knownActors[actorID] = actorRef
	if err := service.addInstalledAgentToParent(ctx, actorID); err != nil {
		return AgentStoreInstallResult{}, err
	}
	service.agentInstallRequests[requestKey] = agentInstallRecord{digest: digest, definitionID: definitionIDValue}
	return AgentStoreInstallResult{
		Agent: service.agentStoreView(service.agentCatalog[targetIndex]), CommandStatus: agentStoreCommandCommitted,
	}, nil
}

// resolveInstallCapabilities adapts the shared action-time resolver to the localdemo error
// vocabulary. The resolver itself remains provider/runtime independent and receives the exact
// service clock so tests and future durable callers evaluate one instant consistently.
func resolveInstallCapabilities(
	passport agentstore.TrustPassport,
	raw []string,
	now time.Time,
) ([]agentstore.Capability, error) {
	capabilities, err := agentstore.ResolveGrantedCapabilities(passport, raw, now)
	if errors.Is(err, agentstore.ErrCapabilityNotAllowed) {
		return nil, ErrForbidden
	}
	if errors.Is(err, agentstore.ErrRevoked) {
		return nil, ErrForbidden
	}
	if err != nil {
		return nil, ErrInvalidInput
	}
	return capabilities, nil
}

func installRequestDigest(
	definitionID string,
	passport agentstore.TrustPassport,
	granted []agentstore.Capability,
) [sha256.Size]byte {
	release := passport.Release()
	parts := []string{
		"wanwork.local-demo-agent-install/2",
		definitionID,
		release.ID().String(),
		release.Version().String(),
		release.ArtifactDigest().Hex(),
		release.ManifestDigest().Hex(),
		release.PersonaDigest().Hex(),
	}
	for _, capability := range granted {
		parts = append(parts, string(capability))
	}
	return sha256.Sum256([]byte(strings.Join(parts, "\x00")))
}

func (service *Service) agentStoreView(record agentCatalogRecord) AgentStoreView {
	definition := record.passport.Definition()
	release := record.passport.Release()
	routes := release.DataRoutes()
	dataRoutes := make([]AgentDataRouteView, 0, len(routes))
	for _, route := range routes {
		dataRoutes = append(dataRoutes, AgentDataRouteView{
			Name: route.Name(), Direction: string(route.Direction()), Classification: string(route.Classification()),
			Destinations: route.Destinations(), RetentionDays: route.RetentionDays(),
		})
	}
	claims := make([]string, 0, len(record.passport.Attestations()))
	for _, attestation := range record.passport.Attestations() {
		claims = append(claims, string(attestation.Claim()))
	}
	requested := make([]string, 0, len(release.RequestedCapabilities()))
	for _, capability := range release.RequestedCapabilities() {
		requested = append(requested, string(capability))
	}
	granted := make([]string, 0)
	installationID, agentActorID, installationStatus := "", "", "available"
	if !record.installation.IsZero() {
		installationID, agentActorID, installationStatus = record.installation.ID().String(), record.installation.AgentActor().String(), string(record.installation.Status())
		for _, capability := range record.installation.GrantedCapabilities() {
			granted = append(granted, string(capability))
		}
	}
	canInstall := installationStatus == "available" && service.usableAgentPassport(record.passport)
	return AgentStoreView{
		DefinitionID: definition.ID().String(), ReleaseID: release.ID().String(), InstallationID: installationID,
		Name: definition.DisplayName(), Summary: definition.Summary(), Version: release.Version().String(),
		ArtifactDigest: release.ArtifactDigest().Hex(), ManifestDigest: release.ManifestDigest().Hex(),
		PersonaDigest: release.PersonaDigest().Hex(), VersionProvenance: AgentVersionProvenanceView{
			PublisherID: definition.PublisherID().String(), DefinitionRevision: definition.Revision(),
			ReleaseRevision: release.Revision(), PassportRevision: record.passport.Revision(),
			PublishedAt: release.PublishedAt().Format(time.RFC3339Nano), DigestAlgorithm: "sha256",
		},
		DefinitionStatus: string(definition.Status()), ReleaseStatus: string(release.Status()),
		PassportStatus: string(record.passport.Status()), InstallationStatus: installationStatus,
		AgentActorID: agentActorID, Isolation: string(release.Isolation()), RequestedCapabilities: requested,
		GrantedCapabilities: granted, DataRoutes: dataRoutes, Attestations: claims,
		CanInstall: canInstall,
	}
}

func (service *Service) usableAgentPassport(passport agentstore.TrustPassport) bool {
	if service == nil || passport.IsZero() {
		return false
	}
	definition, release := passport.Definition(), passport.Release()
	return definition.Status() == agentstore.DefinitionActive &&
		release.Status() == agentstore.ReleasePublished &&
		passport.ValidAt(service.nowUTC())
}

func (service *Service) addInstalledAgentToParent(ctx context.Context, actorID im.ActorID) error {
	parentRecord, ok := service.conversations[service.parent.Ref().ConversationID()]
	if !ok {
		return ErrIntegrity
	}
	if _, exists := parentRecord.members[actorID]; exists {
		return nil
	}
	receipt, err := service.provider.AddMembers(ctx, im.ProviderMemberUpdate{
		Conversation: parentRecord.providerRef, MemberActors: []im.ActorID{actorID},
		IdempotencyKey: "demo/store/parent-members/" + actorID.String(),
	})
	if err != nil || im.RequireCommittedProviderEffect(receipt) != nil {
		if err != nil {
			return errors.Join(ErrProvider, err)
		}
		return ErrProvider
	}
	actorRef := service.knownActors[actorID]
	membership, err := im.NewConversationMembershipSnapshot(parentRecord.snapshot.Ref(), actorRef, im.ConversationMembershipMember, im.ConversationMembershipActive, 1)
	if err != nil {
		return ErrIntegrity
	}
	access, err := im.NewConversationAccessSnapshot(parentRecord.snapshot.Ref(), actorRef, []im.ConversationPermission{im.ConversationPermissionRead}, 1)
	if err != nil {
		return ErrIntegrity
	}
	parentRecord.members[actorID], parentRecord.access[actorID] = membership, access
	return nil
}
