package agentstore

import (
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/immetadata"
)

func TestInstallationNarrowsPassportToTenantWorkspaceAgentAndCapabilities(t *testing.T) {
	t.Parallel()
	passport := installationTestPassport(t)
	capability := catalogTestCapability(t, "conversation.read")
	capabilities := []Capability{capability}
	routes := []string{"conversation.context"}
	installation := installationTestSnapshot(t, passport, capabilities, routes, InstallationActive)
	capabilities[0] = catalogTestCapability(t, "payment.execute")
	routes[0] = "other.route"
	if installation.DefinitionID() != passport.Release().DefinitionID() ||
		installation.ReleaseID() != passport.Release().ID() || installation.AgentActor().String() != "agt_research" ||
		!installation.CanInvoke(capability) || installation.CanInvoke(catalogTestCapability(t, "artifact.write")) ||
		installation.GrantedCapabilities()[0] != capability || installation.BoundDataRoutes()[0] != "conversation.context" ||
		installation.IsZero() {
		t.Fatalf("unexpected installation: %#v", installation)
	}
	otherTenant, err := im.ParseTenantID("ten_other")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := installationBuildSnapshot(
		t, passport, otherTenant, installation.AgentActor(), []Capability{capability},
		[]string{"conversation.context"}, InstallationActive,
	); !errors.Is(err, ErrInvalidValue) {
		t.Fatalf("cross-tenant installation = %v", err)
	}
	human, err := im.ParseActorID("usr_alice")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := installationBuildSnapshot(
		t, passport, passport.Definition().TenantID(), human, []Capability{capability},
		[]string{"conversation.context"}, InstallationActive,
	); !errors.Is(err, ErrInvalidValue) {
		t.Fatalf("human actor installation = %v", err)
	}
	if _, err := installationBuildSnapshot(
		t, passport, passport.Definition().TenantID(), installation.AgentActor(),
		[]Capability{catalogTestCapability(t, "payment.execute")},
		[]string{"conversation.context"}, InstallationActive,
	); !errors.Is(err, ErrInvalidValue) {
		t.Fatalf("prohibited capability installation = %v", err)
	}
	if _, err := installationBuildSnapshot(
		t, passport, passport.Definition().TenantID(), installation.AgentActor(), []Capability{capability},
		[]string{"unknown.route"}, InstallationActive,
	); !errors.Is(err, ErrInvalidValue) {
		t.Fatalf("unknown data route installation = %v", err)
	}
}

func TestInstallationLifecycleRevocationAndOffboardingAreExplicit(t *testing.T) {
	t.Parallel()
	passport := installationTestPassport(t)
	installation := installationTestSnapshot(
		t, passport, []Capability{catalogTestCapability(t, "conversation.read")},
		[]string{"conversation.context"}, InstallationActive,
	)
	at := installation.CreatedAt().Add(time.Hour)
	suspended, err := TransitionInstallation(installation, InstallationSuspended, at, 2)
	if err != nil || suspended.Status() != InstallationSuspended || suspended.CanInvoke(catalogTestCapability(t, "conversation.read")) {
		t.Fatalf("suspend = %#v, %v", suspended, err)
	}
	reactivated, err := TransitionInstallation(suspended, InstallationActive, at.Add(time.Hour), 3)
	if err != nil || !reactivated.CanInvoke(catalogTestCapability(t, "conversation.read")) {
		t.Fatalf("reactivate = %#v, %v", reactivated, err)
	}
	revoked, err := TransitionInstallation(reactivated, InstallationRevoked, at.Add(2*time.Hour), 4)
	if err != nil || revoked.DisabledAt().IsZero() || revoked.CanInvoke(catalogTestCapability(t, "conversation.read")) {
		t.Fatalf("revoke = %#v, %v", revoked, err)
	}
	offboarded, err := TransitionInstallation(revoked, InstallationOffboarded, at.Add(3*time.Hour), 5)
	if err != nil || offboarded.Status() != InstallationOffboarded {
		t.Fatalf("offboard = %#v, %v", offboarded, err)
	}
	if _, err := TransitionInstallation(offboarded, InstallationActive, at.Add(4*time.Hour), 6); !errors.Is(err, ErrInvalidValue) {
		t.Fatalf("terminal offboard transition = %v", err)
	}
	if _, err := TransitionInstallation(installation, InstallationSuspended, at, 99); !errors.Is(err, ErrInvalidValue) {
		t.Fatalf("revision skip = %v", err)
	}

	request, err := NewOffboardingRequest(
		installation, installation.InstalledBy(), at, true, true, true, true,
		DataDispositionArchive, DigestBytes([]byte("offboard request")),
	)
	if err != nil || request.InstallationID() != installation.ID() ||
		!request.RevokeProviderIdentity() || !request.RemoveConversationMemberships() ||
		!request.CancelActiveInvocations() || !request.RevokeCredentialLeases() {
		t.Fatalf("offboarding request = %#v, %v", request, err)
	}
	if _, err := NewOffboardingRequest(
		installation, installation.InstalledBy(), at, true, true, false, true,
		DataDispositionArchive, DigestBytes([]byte("offboard request")),
	); !errors.Is(err, ErrInvalidValue) {
		t.Fatalf("partial cleanup request = %v", err)
	}
}

func TestAgentProviderProjectionUsesNormalUserAndLeaksNoAuthority(t *testing.T) {
	t.Parallel()
	passport := installationTestPassport(t)
	installation := installationTestSnapshot(
		t, passport, []Capability{catalogTestCapability(t, "conversation.read")},
		[]string{"conversation.context"}, InstallationPending,
	)
	realm, err := im.ParseProviderRealmID("rlm_fake")
	if err != nil {
		t.Fatal(err)
	}
	profile, err := im.NewProviderProfile(
		im.IdentityProviderRongCloud, realm,
		[]im.ProviderCapability{im.ProviderCapabilityUserProvision},
		1024, 1024, 1024,
	)
	if err != nil {
		t.Fatal(err)
	}
	request, err := BuildProviderUserProvision(installation, passport, profile, "user/agent-research")
	if err != nil {
		t.Fatal(err)
	}
	projection, err := immetadata.DecodeUserProjection(request.ExtInfo)
	if err != nil {
		t.Fatal(err)
	}
	if request.Actor != installation.AgentActor() || projection.SubjectType() != im.SubjectAgent ||
		projection.PlatformActorID() != installation.AgentActor() ||
		projection.AgentDefinitionID() != installation.DefinitionID() || projection.AgentVersion() != installation.Version() {
		t.Fatalf("unexpected provider user request: %#v %#v", request, projection)
	}
	for _, forbidden := range []string{
		installation.TenantID().String(), installation.WorkspaceID().String(), "conversation.read",
		passport.Release().ArtifactDigest().Hex(), passport.Attestations()[0].EvidenceDigest().Hex(),
	} {
		if strings.Contains(request.ExtInfo, forbidden) {
			t.Fatalf("provider ext_info leaks platform authority %q: %s", forbidden, request.ExtInfo)
		}
	}
}

func installationTestPassport(t *testing.T) TrustPassport {
	t.Helper()
	definition := catalogTestDefinition(t)
	release := catalogTestRelease(t)
	passport, err := NewTrustPassport(
		definition, release,
		catalogTestAttestations(t, release.PublishedAt().Add(-time.Hour), release.PublishedAt().Add(24*time.Hour)),
		PassportActive, 1,
	)
	if err != nil {
		t.Fatal(err)
	}
	return passport
}

func installationTestSnapshot(
	t *testing.T,
	passport TrustPassport,
	capabilities []Capability,
	routes []string,
	status InstallationStatus,
) InstallationSnapshot {
	t.Helper()
	agent, err := im.ParseActorID("agt_research")
	if err != nil {
		t.Fatal(err)
	}
	installation, err := installationBuildSnapshot(
		t, passport, passport.Definition().TenantID(), agent, capabilities, routes, status,
	)
	if err != nil {
		t.Fatal(err)
	}
	return installation
}

func installationBuildSnapshot(
	t *testing.T,
	passport TrustPassport,
	tenant im.TenantID,
	agent im.ActorID,
	capabilities []Capability,
	routes []string,
	status InstallationStatus,
) (InstallationSnapshot, error) {
	t.Helper()
	id, err := ParseInstallationID("ins_acme_research")
	if err != nil {
		t.Fatal(err)
	}
	workspace, err := im.ParseWorkspaceID("wsp_product")
	if err != nil {
		t.Fatal(err)
	}
	createdAt := passport.Release().PublishedAt().Add(time.Hour)
	var disabledAt time.Time
	if status == InstallationRevoked || status == InstallationOffboarded {
		disabledAt = createdAt.Add(time.Hour)
	}
	return NewInstallationSnapshot(
		id, tenant, workspace, agent, passport.Definition().ClaimedBy(), passport,
		capabilities, routes, status, createdAt, disabledAt, 1,
	)
}
