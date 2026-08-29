package agentstore

import (
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/immetadata"
)

// BuildProviderUserProvision projects an installed Agent through the same normal-user provider
// request used for humans. The ext_info contains only the reviewed non-secret identity projection;
// Passport capabilities, tenant, routes, attestations, and authority never leave platform state.
func BuildProviderUserProvision(
	installation InstallationSnapshot,
	passport TrustPassport,
	profile im.ProviderProfile,
	idempotencyKey string,
) (im.ProviderUserProvision, error) {
	if installation.IsZero() || passport.IsZero() ||
		profile.Provider != im.IdentityProviderRongCloud ||
		!profile.Supports(im.ProviderCapabilityUserProvision) ||
		(installation.Status() != InstallationPending && installation.Status() != InstallationActive) ||
		installation.DefinitionID() != passport.Release().DefinitionID() ||
		installation.ReleaseID() != passport.Release().ID() ||
		installation.Version() != passport.Release().Version() ||
		installation.TenantID() != passport.Definition().TenantID() {
		return im.ProviderUserProvision{}, ErrInvalidValue
	}
	projection, err := immetadata.NewUserProjection(
		im.SubjectAgent,
		installation.AgentActor(),
		installation.DefinitionID(),
		installation.Version(),
	)
	if err != nil {
		return im.ProviderUserProvision{}, ErrInvalidValue
	}
	extInfo, err := immetadata.EncodeUserProjection(projection)
	if err != nil {
		return im.ProviderUserProvision{}, ErrInvalidValue
	}
	request := im.ProviderUserProvision{
		Actor: installation.AgentActor(), DisplayName: passport.Definition().DisplayName(),
		ExtInfo: extInfo, IdempotencyKey: idempotencyKey,
	}
	if request.Validate(profile) != nil {
		return im.ProviderUserProvision{}, ErrInvalidValue
	}
	return request, nil
}
