package agentstore

import (
	"errors"
	"testing"
	"time"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
)

func TestDefinitionBindsClaimOwnerPublisherAndTenant(t *testing.T) {
	t.Parallel()
	definition := catalogTestDefinition(t)
	if definition.ID().String() != "agd_research" || definition.TenantID().String() != "ten_acme" ||
		definition.ClaimedBy().String() != "hpr_alice" || definition.PublisherID().String() != "pub_acme" ||
		definition.Status() != DefinitionActive || definition.Revision() != 1 || definition.IsZero() {
		t.Fatalf("unexpected definition: %#v", definition)
	}
	if _, err := NewDefinitionSnapshot(
		definition.ID(), definition.TenantID(), definition.ClaimedBy(), definition.PublisherID(),
		" Research", "Summary", DefinitionActive, 1,
	); !errors.Is(err, ErrInvalidValue) {
		t.Fatalf("leading whitespace name = %v", err)
	}
	if _, err := NewDefinitionSnapshot(
		definition.ID(), definition.TenantID(), definition.ClaimedBy(), definition.PublisherID(),
		"Research", "unsafe\nsummary", DefinitionActive, 1,
	); !errors.Is(err, ErrInvalidValue) {
		t.Fatalf("control character summary = %v", err)
	}
}

func TestReleaseFreezesVersionCapabilitiesDataRoutesAndDigests(t *testing.T) {
	t.Parallel()
	capability := catalogTestCapability(t, "conversation.read")
	other := catalogTestCapability(t, "artifact.write")
	prohibited := catalogTestCapability(t, "payment.execute")
	route, err := NewDataRoute(
		"conversation.context", DataInput, DataConfidential,
		[]string{"provider:rongcloud", "local"}, 30,
	)
	if err != nil {
		t.Fatal(err)
	}
	capabilities := []Capability{other, capability}
	routes := []DataRoute{route}
	release := catalogTestReleaseFrom(t, capabilities, []Capability{prohibited}, routes)
	capabilities[0] = prohibited
	routes[0].destinations[0] = "connector:evil"
	if got := release.RequestedCapabilities(); len(got) != 2 || got[0] != other || got[1] != capability {
		t.Fatalf("capabilities not normalized/copied: %#v", got)
	}
	if got := release.DataRoutes()[0].Destinations(); got[0] != "local" || got[1] != "provider:rongcloud" {
		t.Fatalf("data routes not deeply copied: %#v", got)
	}
	if release.Status() != ReleasePublished || release.Isolation() != IsolationMicroVM || release.IsZero() {
		t.Fatalf("unexpected release: %#v", release)
	}
	if _, err := catalogBuildRelease(
		t, []Capability{capability}, []Capability{capability}, []DataRoute{route},
		ReleasePublished, time.Unix(1700000000, 0).UTC(),
	); !errors.Is(err, ErrInvalidValue) {
		t.Fatalf("capability/prohibition overlap = %v", err)
	}
	if _, err := catalogBuildRelease(
		t, []Capability{capability}, nil, []DataRoute{route},
		ReleaseDraft, time.Unix(1700000000, 0).UTC(),
	); !errors.Is(err, ErrInvalidValue) {
		t.Fatalf("draft with published time = %v", err)
	}
}

func TestTrustPassportRequiresCompleteCurrentAttestationSet(t *testing.T) {
	t.Parallel()
	definition := catalogTestDefinition(t)
	release := catalogTestRelease(t)
	issued := release.PublishedAt().Add(-time.Hour)
	expires := release.PublishedAt().Add(24 * time.Hour)
	attestations := catalogTestAttestations(t, issued, expires)
	passport, err := NewTrustPassport(definition, release, attestations, PassportActive, 1)
	if err != nil {
		t.Fatal(err)
	}
	if !passport.ValidAt(release.PublishedAt()) ||
		!passport.Allows(catalogTestCapability(t, "conversation.read")) ||
		passport.Allows(catalogTestCapability(t, "payment.execute")) || passport.IsZero() {
		t.Fatalf("unexpected passport: %#v", passport)
	}
	copyOfAttestations := passport.Attestations()
	copyOfAttestations[0] = TrustAttestation{}
	if len(passport.Attestations()) != 3 || passport.Attestations()[0].Issuer().IsZero() {
		t.Fatal("passport leaked mutable attestation storage")
	}
	if passport.ValidAt(expires) {
		t.Fatal("passport must fail closed at attestation expiry")
	}
	if _, err := NewTrustPassport(definition, release, attestations[:2], PassportActive, 1); !errors.Is(err, ErrInvalidValue) {
		t.Fatalf("incomplete attestation set = %v", err)
	}
	otherDefinition := definition
	otherID, err := im.ParseAgentDefinitionID("agd_other")
	if err != nil {
		t.Fatal(err)
	}
	otherDefinition.id = otherID
	if _, err := NewTrustPassport(otherDefinition, release, attestations, PassportActive, 1); !errors.Is(err, ErrInvalidValue) {
		t.Fatalf("definition/release mismatch = %v", err)
	}
}

func catalogTestDefinition(t *testing.T) DefinitionSnapshot {
	t.Helper()
	definitionID, err := im.ParseAgentDefinitionID("agd_research")
	if err != nil {
		t.Fatal(err)
	}
	tenant, err := im.ParseTenantID("ten_acme")
	if err != nil {
		t.Fatal(err)
	}
	owner, err := im.ParseHumanPrincipalID("hpr_alice")
	if err != nil {
		t.Fatal(err)
	}
	publisher, err := ParsePublisherID("pub_acme")
	if err != nil {
		t.Fatal(err)
	}
	definition, err := NewDefinitionSnapshot(
		definitionID, tenant, owner, publisher, "Research Agent", "Produces cited research.",
		DefinitionActive, 1,
	)
	if err != nil {
		t.Fatal(err)
	}
	return definition
}

func catalogTestRelease(t *testing.T) ReleaseSnapshot {
	t.Helper()
	route, err := NewDataRoute(
		"conversation.context", DataInput, DataConfidential,
		[]string{"local", "provider:rongcloud"}, 30,
	)
	if err != nil {
		t.Fatal(err)
	}
	return catalogTestReleaseFrom(t,
		[]Capability{catalogTestCapability(t, "conversation.read"), catalogTestCapability(t, "artifact.write")},
		[]Capability{catalogTestCapability(t, "payment.execute")}, []DataRoute{route},
	)
}

func catalogTestReleaseFrom(
	t *testing.T,
	capabilities []Capability,
	prohibitions []Capability,
	routes []DataRoute,
) ReleaseSnapshot {
	t.Helper()
	release, err := catalogBuildRelease(
		t, capabilities, prohibitions, routes, ReleasePublished, time.Unix(1700000000, 0).UTC(),
	)
	if err != nil {
		t.Fatal(err)
	}
	return release
}

func catalogBuildRelease(
	t *testing.T,
	capabilities []Capability,
	prohibitions []Capability,
	routes []DataRoute,
	status ReleaseStatus,
	publishedAt time.Time,
) (ReleaseSnapshot, error) {
	t.Helper()
	releaseID, err := ParseReleaseID("agr_research_100")
	if err != nil {
		t.Fatal(err)
	}
	definitionID, err := im.ParseAgentDefinitionID("agd_research")
	if err != nil {
		t.Fatal(err)
	}
	version, err := im.ParseAgentVersion("1.0.0")
	if err != nil {
		t.Fatal(err)
	}
	return NewReleaseSnapshot(
		releaseID, definitionID, version, DigestBytes([]byte("artifact")),
		DigestBytes([]byte("manifest")), DigestBytes([]byte("persona")),
		capabilities, prohibitions, routes, IsolationMicroVM, status, publishedAt, 1,
	)
}

func catalogTestAttestations(t *testing.T, issued time.Time, expires time.Time) []TrustAttestation {
	t.Helper()
	publisher, err := ParsePublisherID("pub_security")
	if err != nil {
		t.Fatal(err)
	}
	values := make([]TrustAttestation, 0, 3)
	for _, claim := range []AttestationClaim{
		AttestationPublisherVerified, AttestationSecurityReviewed, AttestationDataRoutesReviewed,
	} {
		attestation, err := NewTrustAttestation(
			publisher, claim, 1, DigestBytes([]byte(claim)), issued, expires,
		)
		if err != nil {
			t.Fatal(err)
		}
		values = append(values, attestation)
	}
	return values
}

func catalogTestCapability(t *testing.T, value string) Capability {
	t.Helper()
	parsed, err := ParseCapability(value)
	if err != nil {
		t.Fatalf("ParseCapability(%q): %v", value, err)
	}
	return parsed
}
