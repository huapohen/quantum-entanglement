package agentstore

import (
	"context"
	"slices"
	"strings"
	"time"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
)

type InstallationStatus string

const (
	InstallationPending    InstallationStatus = "pending"
	InstallationActive     InstallationStatus = "active"
	InstallationSuspended  InstallationStatus = "suspended"
	InstallationRevoked    InstallationStatus = "revoked"
	InstallationOffboarded InstallationStatus = "offboarded"
)

func (status InstallationStatus) Valid() bool {
	return status == InstallationPending || status == InstallationActive ||
		status == InstallationSuspended || status == InstallationRevoked ||
		status == InstallationOffboarded
}

// InstallationSnapshot is the tenant/workspace decision that narrows a reviewed Passport to a
// concrete Agent actor. It remains separate from conversation membership and invocation grants.
type InstallationSnapshot struct {
	id                  InstallationID
	tenant              im.TenantID
	workspace           im.WorkspaceID
	definitionID        im.AgentDefinitionID
	releaseID           ReleaseID
	version             im.AgentVersion
	agentActor          im.ActorID
	installedBy         im.HumanPrincipalID
	grantedCapabilities []Capability
	boundDataRoutes     []string
	status              InstallationStatus
	createdAt           time.Time
	disabledAt          time.Time
	revision            uint64
}

func NewInstallationSnapshot(
	id InstallationID,
	tenant im.TenantID,
	workspace im.WorkspaceID,
	agentActor im.ActorID,
	installedBy im.HumanPrincipalID,
	passport TrustPassport,
	grantedCapabilities []Capability,
	boundDataRoutes []string,
	status InstallationStatus,
	createdAt time.Time,
	disabledAt time.Time,
	revision uint64,
) (InstallationSnapshot, error) {
	capabilities, err := normalizeCapabilities(grantedCapabilities)
	if err != nil || len(capabilities) == 0 {
		return InstallationSnapshot{}, ErrInvalidValue
	}
	routes, err := normalizeRouteNames(boundDataRoutes)
	if err != nil || len(routes) == 0 {
		return InstallationSnapshot{}, ErrInvalidValue
	}
	subjectType, hasSubjectType := agentActor.SubjectType()
	if id.IsZero() || tenant.IsZero() || workspace.IsZero() || !hasSubjectType ||
		subjectType != im.SubjectAgent || installedBy.IsZero() || passport.IsZero() ||
		passport.Definition().TenantID() != tenant || !status.Valid() || revision == 0 ||
		createdAt.IsZero() || createdAt.Location() != time.UTC || !passport.ValidAt(createdAt) {
		return InstallationSnapshot{}, ErrInvalidValue
	}
	for _, capability := range capabilities {
		if !passport.Allows(capability) {
			return InstallationSnapshot{}, ErrInvalidValue
		}
	}
	availableRoutes := passport.Release().DataRoutes()
	for _, routeName := range routes {
		if !slices.ContainsFunc(availableRoutes, func(route DataRoute) bool { return route.Name() == routeName }) {
			return InstallationSnapshot{}, ErrInvalidValue
		}
	}
	if status == InstallationRevoked || status == InstallationOffboarded {
		if disabledAt.IsZero() || disabledAt.Location() != time.UTC || disabledAt.Before(createdAt) {
			return InstallationSnapshot{}, ErrInvalidValue
		}
	} else if !disabledAt.IsZero() {
		return InstallationSnapshot{}, ErrInvalidValue
	}
	release := passport.Release()
	return InstallationSnapshot{
		id: id, tenant: tenant, workspace: workspace,
		definitionID: release.DefinitionID(), releaseID: release.ID(), version: release.Version(),
		agentActor: agentActor, installedBy: installedBy, grantedCapabilities: capabilities,
		boundDataRoutes: routes, status: status, createdAt: createdAt,
		disabledAt: disabledAt, revision: revision,
	}, nil
}

func (value InstallationSnapshot) ID() InstallationID                 { return value.id }
func (value InstallationSnapshot) TenantID() im.TenantID              { return value.tenant }
func (value InstallationSnapshot) WorkspaceID() im.WorkspaceID        { return value.workspace }
func (value InstallationSnapshot) DefinitionID() im.AgentDefinitionID { return value.definitionID }
func (value InstallationSnapshot) ReleaseID() ReleaseID               { return value.releaseID }
func (value InstallationSnapshot) Version() im.AgentVersion           { return value.version }
func (value InstallationSnapshot) AgentActor() im.ActorID             { return value.agentActor }
func (value InstallationSnapshot) InstalledBy() im.HumanPrincipalID   { return value.installedBy }
func (value InstallationSnapshot) Status() InstallationStatus         { return value.status }
func (value InstallationSnapshot) CreatedAt() time.Time               { return value.createdAt }
func (value InstallationSnapshot) DisabledAt() time.Time              { return value.disabledAt }
func (value InstallationSnapshot) Revision() uint64                   { return value.revision }
func (value InstallationSnapshot) GrantedCapabilities() []Capability {
	return append([]Capability(nil), value.grantedCapabilities...)
}
func (value InstallationSnapshot) BoundDataRoutes() []string {
	return append([]string(nil), value.boundDataRoutes...)
}
func (value InstallationSnapshot) IsZero() bool {
	return value.id.IsZero() && value.tenant.IsZero() && value.workspace.IsZero() &&
		value.definitionID.IsZero() && value.releaseID.IsZero() && value.version.IsZero() &&
		value.agentActor.IsZero() && value.installedBy.IsZero() && len(value.grantedCapabilities) == 0 &&
		len(value.boundDataRoutes) == 0 && value.status == "" && value.createdAt.IsZero() &&
		value.disabledAt.IsZero() && value.revision == 0
}
func (value InstallationSnapshot) CanInvoke(capability Capability) bool {
	return value.status == InstallationActive && slices.Contains(value.grantedCapabilities, capability)
}

// TransitionInstallation enforces the lifecycle and monotonic revision. Offboarded is terminal;
// revoke may only advance to offboarding cleanup, never back to active.
func TransitionInstallation(
	current InstallationSnapshot,
	nextStatus InstallationStatus,
	at time.Time,
	nextRevision uint64,
) (InstallationSnapshot, error) {
	if current.IsZero() || !nextStatus.Valid() || at.IsZero() || at.Location() != time.UTC ||
		nextRevision != current.revision+1 || !validInstallationTransition(current.status, nextStatus) {
		return InstallationSnapshot{}, ErrInvalidValue
	}
	next := current
	next.status = nextStatus
	next.revision = nextRevision
	if nextStatus == InstallationRevoked || nextStatus == InstallationOffboarded {
		next.disabledAt = at
	} else {
		next.disabledAt = time.Time{}
	}
	return next, nil
}

type DataDisposition string

const (
	DataDispositionRetain  DataDisposition = "retain"
	DataDispositionArchive DataDisposition = "archive"
	DataDispositionDelete  DataDisposition = "delete"
)

func (disposition DataDisposition) Valid() bool {
	return disposition == DataDispositionRetain || disposition == DataDispositionArchive ||
		disposition == DataDispositionDelete
}

// OffboardingRequest freezes mandatory cleanup intent. Provider identity, memberships, active
// invocations, and credentials are all explicit so one cleanup cannot silently stand in for the
// others.
type OffboardingRequest struct {
	installationID                InstallationID
	expectedRevision              uint64
	requestedBy                   im.HumanPrincipalID
	requestedAt                   time.Time
	revokeProviderIdentity        bool
	removeConversationMemberships bool
	cancelActiveInvocations       bool
	revokeCredentialLeases        bool
	dataDisposition               DataDisposition
	requestDigest                 SHA256Digest
}

func NewOffboardingRequest(
	installation InstallationSnapshot,
	requestedBy im.HumanPrincipalID,
	requestedAt time.Time,
	revokeProviderIdentity bool,
	removeConversationMemberships bool,
	cancelActiveInvocations bool,
	revokeCredentialLeases bool,
	dataDisposition DataDisposition,
	requestDigest SHA256Digest,
) (OffboardingRequest, error) {
	if installation.IsZero() || installation.status == InstallationOffboarded || requestedBy.IsZero() ||
		requestedAt.IsZero() || requestedAt.Location() != time.UTC || requestedAt.Before(installation.createdAt) ||
		!revokeProviderIdentity || !removeConversationMemberships || !cancelActiveInvocations ||
		!revokeCredentialLeases || !dataDisposition.Valid() || requestDigest.IsZero() {
		return OffboardingRequest{}, ErrInvalidValue
	}
	return OffboardingRequest{
		installationID: installation.id, expectedRevision: installation.revision,
		requestedBy: requestedBy, requestedAt: requestedAt,
		revokeProviderIdentity:        revokeProviderIdentity,
		removeConversationMemberships: removeConversationMemberships,
		cancelActiveInvocations:       cancelActiveInvocations,
		revokeCredentialLeases:        revokeCredentialLeases,
		dataDisposition:               dataDisposition, requestDigest: requestDigest,
	}, nil
}

func (value OffboardingRequest) InstallationID() InstallationID   { return value.installationID }
func (value OffboardingRequest) ExpectedRevision() uint64         { return value.expectedRevision }
func (value OffboardingRequest) RequestedBy() im.HumanPrincipalID { return value.requestedBy }
func (value OffboardingRequest) RequestedAt() time.Time           { return value.requestedAt }
func (value OffboardingRequest) RevokeProviderIdentity() bool     { return value.revokeProviderIdentity }
func (value OffboardingRequest) RemoveConversationMemberships() bool {
	return value.removeConversationMemberships
}
func (value OffboardingRequest) CancelActiveInvocations() bool    { return value.cancelActiveInvocations }
func (value OffboardingRequest) RevokeCredentialLeases() bool     { return value.revokeCredentialLeases }
func (value OffboardingRequest) DataDisposition() DataDisposition { return value.dataDisposition }
func (value OffboardingRequest) RequestDigest() SHA256Digest      { return value.requestDigest }

type CatalogRepository interface {
	CurrentDefinition(context.Context, im.AgentDefinitionID) (DefinitionSnapshot, error)
	CompareAndSwapDefinition(context.Context, uint64, DefinitionSnapshot) (DefinitionSnapshot, error)
	CurrentRelease(context.Context, ReleaseID) (ReleaseSnapshot, error)
	CompareAndSwapRelease(context.Context, uint64, ReleaseSnapshot) (ReleaseSnapshot, error)
	CurrentPassport(context.Context, ReleaseID) (TrustPassport, error)
	CompareAndSwapPassport(context.Context, uint64, TrustPassport) (TrustPassport, error)
}

type InstallationRepository interface {
	CurrentInstallation(context.Context, InstallationID) (InstallationSnapshot, error)
	CompareAndSwapInstallation(context.Context, uint64, InstallationSnapshot) (InstallationSnapshot, error)
}

// Repository is the tenant-scoped Agent Store surface exposed by a Unit of Work. Implementations
// must use one transaction snapshot for catalog, Passport, and installation reads; callers must
// still perform action-time capability resolution before invoking an Agent.
type Repository interface {
	CatalogRepository
	InstallationRepository
}

func validInstallationTransition(current InstallationStatus, next InstallationStatus) bool {
	switch current {
	case InstallationPending:
		return next == InstallationActive || next == InstallationRevoked || next == InstallationOffboarded
	case InstallationActive:
		return next == InstallationSuspended || next == InstallationRevoked || next == InstallationOffboarded
	case InstallationSuspended:
		return next == InstallationActive || next == InstallationRevoked || next == InstallationOffboarded
	case InstallationRevoked:
		return next == InstallationOffboarded
	default:
		return false
	}
}

func normalizeRouteNames(input []string) ([]string, error) {
	if len(input) > maxCollectionItems {
		return nil, ErrInvalidValue
	}
	values := append([]string(nil), input...)
	for _, value := range values {
		if value == "" || len(value) > maxNameBytes || !capabilityPattern.MatchString(value) {
			return nil, ErrInvalidValue
		}
	}
	slices.SortFunc(values, strings.Compare)
	if hasAdjacentDuplicate(values) {
		return nil, ErrInvalidValue
	}
	return values, nil
}
