package agentstore

import (
	"slices"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
	"golang.org/x/text/unicode/norm"
)

type DefinitionStatus string

const (
	DefinitionDraft   DefinitionStatus = "draft"
	DefinitionActive  DefinitionStatus = "active"
	DefinitionRevoked DefinitionStatus = "revoked"
)

func (status DefinitionStatus) Valid() bool {
	return status == DefinitionDraft || status == DefinitionActive || status == DefinitionRevoked
}

// DefinitionSnapshot is the claimed catalog identity. Claim ownership and publisher identity are
// explicit; neither grants a runtime capability or tenant membership.
type DefinitionSnapshot struct {
	id          im.AgentDefinitionID
	tenant      im.TenantID
	claimedBy   im.HumanPrincipalID
	publisher   PublisherID
	displayName string
	summary     string
	status      DefinitionStatus
	revision    uint64
}

func NewDefinitionSnapshot(
	id im.AgentDefinitionID,
	tenant im.TenantID,
	claimedBy im.HumanPrincipalID,
	publisher PublisherID,
	displayName string,
	summary string,
	status DefinitionStatus,
	revision uint64,
) (DefinitionSnapshot, error) {
	if id.IsZero() || tenant.IsZero() || claimedBy.IsZero() || publisher.IsZero() ||
		!validDisplayText(displayName, maxNameBytes) || !validDisplayText(summary, maxSummaryBytes) ||
		!status.Valid() || revision == 0 {
		return DefinitionSnapshot{}, ErrInvalidValue
	}
	return DefinitionSnapshot{
		id: id, tenant: tenant, claimedBy: claimedBy, publisher: publisher,
		displayName: displayName, summary: summary, status: status, revision: revision,
	}, nil
}

func (value DefinitionSnapshot) ID() im.AgentDefinitionID       { return value.id }
func (value DefinitionSnapshot) TenantID() im.TenantID          { return value.tenant }
func (value DefinitionSnapshot) ClaimedBy() im.HumanPrincipalID { return value.claimedBy }
func (value DefinitionSnapshot) PublisherID() PublisherID       { return value.publisher }
func (value DefinitionSnapshot) DisplayName() string            { return value.displayName }
func (value DefinitionSnapshot) Summary() string                { return value.summary }
func (value DefinitionSnapshot) Status() DefinitionStatus       { return value.status }
func (value DefinitionSnapshot) Revision() uint64               { return value.revision }
func (value DefinitionSnapshot) IsZero() bool {
	return value.id.IsZero() && value.tenant.IsZero() && value.claimedBy.IsZero() &&
		value.publisher.IsZero() && value.displayName == "" && value.summary == "" &&
		value.status == "" && value.revision == 0
}

type RuntimeIsolation string

const (
	IsolationProcess   RuntimeIsolation = "process"
	IsolationContainer RuntimeIsolation = "container"
	IsolationMicroVM   RuntimeIsolation = "microvm"
)

func (isolation RuntimeIsolation) Valid() bool {
	return isolation == IsolationProcess || isolation == IsolationContainer || isolation == IsolationMicroVM
}

type ReleaseStatus string

const (
	ReleaseDraft       ReleaseStatus = "draft"
	ReleasePublished   ReleaseStatus = "published"
	ReleaseQuarantined ReleaseStatus = "quarantined"
	ReleaseRevoked     ReleaseStatus = "revoked"
)

func (status ReleaseStatus) Valid() bool {
	return status == ReleaseDraft || status == ReleasePublished ||
		status == ReleaseQuarantined || status == ReleaseRevoked
}

// ReleaseSnapshot freezes executable identity, persona, permissions, and data routes. Requested
// capability is inventory input only; an installation may grant a strict subset.
type ReleaseSnapshot struct {
	id                    ReleaseID
	definitionID          im.AgentDefinitionID
	version               im.AgentVersion
	artifactDigest        SHA256Digest
	manifestDigest        SHA256Digest
	personaDigest         SHA256Digest
	requestedCapabilities []Capability
	prohibitions          []Capability
	dataRoutes            []DataRoute
	isolation             RuntimeIsolation
	status                ReleaseStatus
	publishedAt           time.Time
	revision              uint64
}

func NewReleaseSnapshot(
	id ReleaseID,
	definitionID im.AgentDefinitionID,
	version im.AgentVersion,
	artifactDigest SHA256Digest,
	manifestDigest SHA256Digest,
	personaDigest SHA256Digest,
	requestedCapabilities []Capability,
	prohibitions []Capability,
	dataRoutes []DataRoute,
	isolation RuntimeIsolation,
	status ReleaseStatus,
	publishedAt time.Time,
	revision uint64,
) (ReleaseSnapshot, error) {
	capabilities, err := normalizeCapabilities(requestedCapabilities)
	if err != nil || len(capabilities) == 0 {
		return ReleaseSnapshot{}, ErrInvalidValue
	}
	forbidden, err := normalizeCapabilities(prohibitions)
	if err != nil || intersects(capabilities, forbidden) {
		return ReleaseSnapshot{}, ErrInvalidValue
	}
	routes, err := normalizeRoutes(dataRoutes)
	if err != nil || len(routes) == 0 {
		return ReleaseSnapshot{}, ErrInvalidValue
	}
	if id.IsZero() || definitionID.IsZero() || version.IsZero() || artifactDigest.IsZero() ||
		manifestDigest.IsZero() || personaDigest.IsZero() || !isolation.Valid() ||
		!status.Valid() || revision == 0 {
		return ReleaseSnapshot{}, ErrInvalidValue
	}
	if status == ReleaseDraft {
		if !publishedAt.IsZero() {
			return ReleaseSnapshot{}, ErrInvalidValue
		}
	} else if publishedAt.IsZero() || publishedAt.Location() != time.UTC {
		return ReleaseSnapshot{}, ErrInvalidValue
	}
	return ReleaseSnapshot{
		id: id, definitionID: definitionID, version: version,
		artifactDigest: artifactDigest, manifestDigest: manifestDigest, personaDigest: personaDigest,
		requestedCapabilities: capabilities, prohibitions: forbidden, dataRoutes: routes,
		isolation: isolation, status: status, publishedAt: publishedAt, revision: revision,
	}, nil
}

func (value ReleaseSnapshot) ID() ReleaseID                      { return value.id }
func (value ReleaseSnapshot) DefinitionID() im.AgentDefinitionID { return value.definitionID }
func (value ReleaseSnapshot) Version() im.AgentVersion           { return value.version }
func (value ReleaseSnapshot) ArtifactDigest() SHA256Digest       { return value.artifactDigest }
func (value ReleaseSnapshot) ManifestDigest() SHA256Digest       { return value.manifestDigest }
func (value ReleaseSnapshot) PersonaDigest() SHA256Digest        { return value.personaDigest }
func (value ReleaseSnapshot) Isolation() RuntimeIsolation        { return value.isolation }
func (value ReleaseSnapshot) Status() ReleaseStatus              { return value.status }
func (value ReleaseSnapshot) PublishedAt() time.Time             { return value.publishedAt }
func (value ReleaseSnapshot) Revision() uint64                   { return value.revision }
func (value ReleaseSnapshot) RequestedCapabilities() []Capability {
	return append([]Capability(nil), value.requestedCapabilities...)
}
func (value ReleaseSnapshot) Prohibitions() []Capability {
	return append([]Capability(nil), value.prohibitions...)
}
func (value ReleaseSnapshot) DataRoutes() []DataRoute { return cloneRoutes(value.dataRoutes) }
func (value ReleaseSnapshot) IsZero() bool {
	return value.id.IsZero() && value.definitionID.IsZero() && value.version.IsZero() &&
		value.artifactDigest.IsZero() && value.manifestDigest.IsZero() && value.personaDigest.IsZero() &&
		len(value.requestedCapabilities) == 0 && len(value.prohibitions) == 0 &&
		len(value.dataRoutes) == 0 && value.isolation == "" && value.status == "" &&
		value.publishedAt.IsZero() && value.revision == 0
}

type PassportStatus string

const (
	PassportActive      PassportStatus = "active"
	PassportQuarantined PassportStatus = "quarantined"
	PassportRevoked     PassportStatus = "revoked"
)

func (status PassportStatus) Valid() bool {
	return status == PassportActive || status == PassportQuarantined || status == PassportRevoked
}

// TrustPassport binds the definition claim, immutable release, and reviewed attestations. It is
// platform governance evidence, not a bearer token.
type TrustPassport struct {
	definition   DefinitionSnapshot
	release      ReleaseSnapshot
	attestations []TrustAttestation
	status       PassportStatus
	revision     uint64
}

func NewTrustPassport(
	definition DefinitionSnapshot,
	release ReleaseSnapshot,
	attestations []TrustAttestation,
	status PassportStatus,
	revision uint64,
) (TrustPassport, error) {
	values, err := normalizeAttestations(attestations)
	if definition.IsZero() || release.IsZero() || definition.ID() != release.DefinitionID() ||
		definition.Status() != DefinitionActive || release.Status() != ReleasePublished ||
		err != nil || !hasRequiredAttestations(values) || !status.Valid() || revision == 0 {
		return TrustPassport{}, ErrInvalidValue
	}
	return TrustPassport{
		definition: definition, release: release, attestations: values,
		status: status, revision: revision,
	}, nil
}

func (value TrustPassport) Definition() DefinitionSnapshot { return value.definition }
func (value TrustPassport) Release() ReleaseSnapshot       { return value.release }
func (value TrustPassport) Status() PassportStatus         { return value.status }
func (value TrustPassport) Revision() uint64               { return value.revision }
func (value TrustPassport) Attestations() []TrustAttestation {
	return append([]TrustAttestation(nil), value.attestations...)
}
func (value TrustPassport) IsZero() bool {
	return value.definition.IsZero() && value.release.IsZero() && len(value.attestations) == 0 &&
		value.status == "" && value.revision == 0
}
func (value TrustPassport) Allows(capability Capability) bool {
	return value.status == PassportActive && slices.Contains(value.release.requestedCapabilities, capability) &&
		!slices.Contains(value.release.prohibitions, capability)
}
func (value TrustPassport) ValidAt(now time.Time) bool {
	if value.status != PassportActive || now.IsZero() || now.Location() != time.UTC {
		return false
	}
	for _, attestation := range value.attestations {
		if now.Before(attestation.issuedAt) || !now.Before(attestation.expiresAt) {
			return false
		}
	}
	return true
}

func normalizeCapabilities(input []Capability) ([]Capability, error) {
	if len(input) > maxCollectionItems {
		return nil, ErrInvalidValue
	}
	values := append([]Capability(nil), input...)
	for _, capability := range values {
		if _, err := ParseCapability(string(capability)); err != nil {
			return nil, ErrInvalidValue
		}
	}
	slices.Sort(values)
	if hasAdjacentDuplicate(values) {
		return nil, ErrInvalidValue
	}
	return values, nil
}

func normalizeRoutes(input []DataRoute) ([]DataRoute, error) {
	if len(input) > maxCollectionItems {
		return nil, ErrInvalidValue
	}
	values := cloneRoutes(input)
	for _, route := range values {
		if route.IsZero() {
			return nil, ErrInvalidValue
		}
	}
	slices.SortFunc(values, func(left, right DataRoute) int { return strings.Compare(left.Name(), right.Name()) })
	for index := 1; index < len(values); index++ {
		if values[index].Name() == values[index-1].Name() {
			return nil, ErrInvalidValue
		}
	}
	return values, nil
}

func normalizeAttestations(input []TrustAttestation) ([]TrustAttestation, error) {
	if len(input) == 0 || len(input) > maxCollectionItems {
		return nil, ErrInvalidValue
	}
	values := append([]TrustAttestation(nil), input...)
	for _, attestation := range values {
		if attestation.issuer.IsZero() || !attestation.claim.Valid() {
			return nil, ErrInvalidValue
		}
	}
	slices.SortFunc(values, func(left, right TrustAttestation) int {
		if compared := strings.Compare(string(left.claim), string(right.claim)); compared != 0 {
			return compared
		}
		return strings.Compare(left.issuer.String(), right.issuer.String())
	})
	for index := 1; index < len(values); index++ {
		if values[index].claim == values[index-1].claim && values[index].issuer == values[index-1].issuer {
			return nil, ErrInvalidValue
		}
	}
	return values, nil
}

func hasRequiredAttestations(values []TrustAttestation) bool {
	for _, required := range []AttestationClaim{
		AttestationPublisherVerified, AttestationSecurityReviewed, AttestationDataRoutesReviewed,
	} {
		if !slices.ContainsFunc(values, func(value TrustAttestation) bool { return value.claim == required }) {
			return false
		}
	}
	return true
}

func intersects(left []Capability, right []Capability) bool {
	for _, value := range left {
		if slices.Contains(right, value) {
			return true
		}
	}
	return false
}

func cloneRoutes(input []DataRoute) []DataRoute {
	values := make([]DataRoute, 0, len(input))
	for _, route := range input {
		cloned := route
		cloned.destinations = append([]string(nil), route.destinations...)
		values = append(values, cloned)
	}
	return values
}

func validDisplayText(value string, maxBytes int) bool {
	if value == "" || len(value) > maxBytes || !utf8.ValidString(value) || !norm.NFC.IsNormalString(value) ||
		strings.TrimSpace(value) != value {
		return false
	}
	for _, character := range value {
		if unicode.IsControl(character) {
			return false
		}
	}
	return true
}
