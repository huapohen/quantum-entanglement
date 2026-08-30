package agentstore

import (
	"bytes"
	"encoding/json"
	"io"
	"time"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
)

// The persisted records are deliberately separate from the domain values. Domain values keep
// their fields private; these records are the exact, versionless JSON shape used by the first
// PostgreSQL adapter. A later incompatible shape must introduce a new codec instead of silently
// accepting a wider record.
type persistedDefinition struct {
	ID          string           `json:"id"`
	TenantID    string           `json:"tenantId"`
	ClaimedBy   string           `json:"claimedBy"`
	PublisherID string           `json:"publisherId"`
	DisplayName string           `json:"displayName"`
	Summary     string           `json:"summary"`
	Status      DefinitionStatus `json:"status"`
	Revision    uint64           `json:"revision"`
}

type persistedRoute struct {
	Name           string             `json:"name"`
	Direction      DataDirection      `json:"direction"`
	Classification DataClassification `json:"classification"`
	Destinations   []string           `json:"destinations"`
	RetentionDays  uint16             `json:"retentionDays"`
}

type persistedRelease struct {
	ID                    string           `json:"id"`
	DefinitionID          string           `json:"definitionId"`
	Version               string           `json:"version"`
	ArtifactDigest        string           `json:"artifactDigest"`
	ManifestDigest        string           `json:"manifestDigest"`
	PersonaDigest         string           `json:"personaDigest"`
	RequestedCapabilities []string         `json:"requestedCapabilities"`
	Prohibitions          []string         `json:"prohibitions"`
	DataRoutes            []persistedRoute `json:"dataRoutes"`
	Isolation             RuntimeIsolation `json:"isolation"`
	Status                ReleaseStatus    `json:"status"`
	PublishedAt           *time.Time       `json:"publishedAt"`
	Revision              uint64           `json:"revision"`
}

type persistedAttestation struct {
	Issuer         string           `json:"issuer"`
	Claim          AttestationClaim `json:"claim"`
	PolicyRevision uint64           `json:"policyRevision"`
	EvidenceDigest string           `json:"evidenceDigest"`
	IssuedAt       time.Time        `json:"issuedAt"`
	ExpiresAt      time.Time        `json:"expiresAt"`
}

type persistedPassport struct {
	Definition   persistedDefinition    `json:"definition"`
	Release      persistedRelease       `json:"release"`
	Attestations []persistedAttestation `json:"attestations"`
	Status       PassportStatus         `json:"status"`
	Revision     uint64                 `json:"revision"`
}

type persistedInstallation struct {
	ID                  string             `json:"id"`
	TenantID            string             `json:"tenantId"`
	WorkspaceID         string             `json:"workspaceId"`
	DefinitionID        string             `json:"definitionId"`
	ReleaseID           string             `json:"releaseId"`
	Version             string             `json:"version"`
	AgentActorID        string             `json:"agentActorId"`
	InstalledBy         string             `json:"installedBy"`
	GrantedCapabilities []string           `json:"grantedCapabilities"`
	BoundDataRoutes     []string           `json:"boundDataRoutes"`
	Status              InstallationStatus `json:"status"`
	CreatedAt           time.Time          `json:"createdAt"`
	DisabledAt          *time.Time         `json:"disabledAt"`
	Revision            uint64             `json:"revision"`
}

// EncodeDefinition returns canonical JSON for a validated definition snapshot.
func EncodeDefinition(value DefinitionSnapshot) ([]byte, error) {
	if value.IsZero() {
		return nil, ErrInvalidValue
	}
	return marshalCanonical(persistedDefinition{
		ID: value.ID().String(), TenantID: value.TenantID().String(), ClaimedBy: value.ClaimedBy().String(),
		PublisherID: value.PublisherID().String(), DisplayName: value.DisplayName(), Summary: value.Summary(),
		Status: value.Status(), Revision: value.Revision(),
	})
}

// DecodeDefinition rejects unknown fields, non-canonical JSON, and any value that does not pass
// the same constructor used for newly admitted catalog state.
func DecodeDefinition(data []byte) (DefinitionSnapshot, error) {
	var record persistedDefinition
	if err := unmarshalCanonical(data, &record); err != nil {
		return DefinitionSnapshot{}, ErrInvalidValue
	}
	id, err := im.ParseAgentDefinitionID(record.ID)
	if err != nil {
		return DefinitionSnapshot{}, ErrInvalidValue
	}
	tenant, err := im.ParseTenantID(record.TenantID)
	if err != nil {
		return DefinitionSnapshot{}, ErrInvalidValue
	}
	claimedBy, err := im.ParseHumanPrincipalID(record.ClaimedBy)
	if err != nil {
		return DefinitionSnapshot{}, ErrInvalidValue
	}
	publisher, err := ParsePublisherID(record.PublisherID)
	if err != nil {
		return DefinitionSnapshot{}, ErrInvalidValue
	}
	value, err := NewDefinitionSnapshot(id, tenant, claimedBy, publisher, record.DisplayName, record.Summary, record.Status, record.Revision)
	if err != nil {
		return DefinitionSnapshot{}, err
	}
	return requireCanonicalRoundTrip(data, value, EncodeDefinition)
}

// EncodeRelease returns canonical JSON for a release snapshot. Digest fields use lowercase bare
// SHA-256 hex, matching ParseSHA256Digest; the PostgreSQL adapter adds its storage prefix at the
// SQL boundary and never stores raw artifact content.
func EncodeRelease(value ReleaseSnapshot) ([]byte, error) {
	if value.IsZero() {
		return nil, ErrInvalidValue
	}
	record := persistedRelease{
		ID: value.ID().String(), DefinitionID: value.DefinitionID().String(), Version: value.Version().String(),
		ArtifactDigest: value.ArtifactDigest().Hex(), ManifestDigest: value.ManifestDigest().Hex(), PersonaDigest: value.PersonaDigest().Hex(),
		RequestedCapabilities: capabilityStrings(value.RequestedCapabilities()), Prohibitions: capabilityStrings(value.Prohibitions()),
		DataRoutes: routeRecords(value.DataRoutes()), Isolation: value.Isolation(), Status: value.Status(), Revision: value.Revision(),
	}
	if publishedAt := value.PublishedAt(); !publishedAt.IsZero() {
		record.PublishedAt = timePointer(publishedAt)
	}
	return marshalCanonical(record)
}

func DecodeRelease(data []byte) (ReleaseSnapshot, error) {
	var record persistedRelease
	if err := unmarshalCanonical(data, &record); err != nil {
		return ReleaseSnapshot{}, ErrInvalidValue
	}
	id, err := ParseReleaseID(record.ID)
	if err != nil {
		return ReleaseSnapshot{}, ErrInvalidValue
	}
	definitionID, err := im.ParseAgentDefinitionID(record.DefinitionID)
	if err != nil {
		return ReleaseSnapshot{}, ErrInvalidValue
	}
	version, err := im.ParseAgentVersion(record.Version)
	if err != nil {
		return ReleaseSnapshot{}, ErrInvalidValue
	}
	artifact, err := ParseSHA256Digest(record.ArtifactDigest)
	if err != nil {
		return ReleaseSnapshot{}, ErrInvalidValue
	}
	manifest, err := ParseSHA256Digest(record.ManifestDigest)
	if err != nil {
		return ReleaseSnapshot{}, ErrInvalidValue
	}
	persona, err := ParseSHA256Digest(record.PersonaDigest)
	if err != nil {
		return ReleaseSnapshot{}, ErrInvalidValue
	}
	capabilities, err := parseCapabilities(record.RequestedCapabilities)
	if err != nil {
		return ReleaseSnapshot{}, err
	}
	prohibitions, err := parseCapabilities(record.Prohibitions)
	if err != nil {
		return ReleaseSnapshot{}, err
	}
	routes, err := parseRoutes(record.DataRoutes)
	if err != nil {
		return ReleaseSnapshot{}, err
	}
	publishedAt := time.Time{}
	if record.PublishedAt != nil {
		publishedAt = *record.PublishedAt
	}
	value, err := NewReleaseSnapshot(id, definitionID, version, artifact, manifest, persona, capabilities, prohibitions, routes, record.Isolation, record.Status, publishedAt, record.Revision)
	if err != nil {
		return ReleaseSnapshot{}, err
	}
	return requireCanonicalRoundTrip(data, value, EncodeRelease)
}

func EncodeTrustPassport(value TrustPassport) ([]byte, error) {
	if value.IsZero() {
		return nil, ErrInvalidValue
	}
	definition, err := encodeDefinitionRecord(value.Definition())
	if err != nil {
		return nil, err
	}
	release, err := encodeReleaseRecord(value.Release())
	if err != nil {
		return nil, err
	}
	attestations, err := encodeAttestationRecords(value.Attestations())
	if err != nil {
		return nil, err
	}
	return marshalCanonical(persistedPassport{Definition: definition, Release: release, Attestations: attestations, Status: value.Status(), Revision: value.Revision()})
}

func DecodeTrustPassport(data []byte) (TrustPassport, error) {
	var record persistedPassport
	if err := unmarshalCanonical(data, &record); err != nil {
		return TrustPassport{}, ErrInvalidValue
	}
	definition, err := decodeDefinitionRecord(record.Definition)
	if err != nil {
		return TrustPassport{}, err
	}
	release, err := decodeReleaseRecord(record.Release)
	if err != nil {
		return TrustPassport{}, err
	}
	attestations, err := decodeAttestationRecords(record.Attestations)
	if err != nil {
		return TrustPassport{}, err
	}
	value, err := NewTrustPassport(definition, release, attestations, record.Status, record.Revision)
	if err != nil {
		return TrustPassport{}, err
	}
	return requireCanonicalRoundTrip(data, value, EncodeTrustPassport)
}

func EncodeInstallation(value InstallationSnapshot) ([]byte, error) {
	if value.IsZero() {
		return nil, ErrInvalidValue
	}
	record := persistedInstallation{
		ID: value.ID().String(), TenantID: value.TenantID().String(), WorkspaceID: value.WorkspaceID().String(),
		DefinitionID: value.DefinitionID().String(), ReleaseID: value.ReleaseID().String(), Version: value.Version().String(),
		AgentActorID: value.AgentActor().String(), InstalledBy: value.InstalledBy().String(),
		GrantedCapabilities: capabilityStrings(value.GrantedCapabilities()), BoundDataRoutes: append([]string{}, value.BoundDataRoutes()...),
		Status: value.Status(), CreatedAt: value.CreatedAt(), Revision: value.Revision(),
	}
	if disabledAt := value.DisabledAt(); !disabledAt.IsZero() {
		record.DisabledAt = timePointer(disabledAt)
	}
	return marshalCanonical(record)
}

// DecodeInstallation requires the exact passport used when the installation was admitted. This
// prevents a row that merely names a release from being reconstructed as an authorized decision.
func DecodeInstallation(data []byte, passport TrustPassport) (InstallationSnapshot, error) {
	var record persistedInstallation
	if err := unmarshalCanonical(data, &record); err != nil || passport.IsZero() {
		return InstallationSnapshot{}, ErrInvalidValue
	}
	id, err := ParseInstallationID(record.ID)
	if err != nil {
		return InstallationSnapshot{}, ErrInvalidValue
	}
	tenant, err := im.ParseTenantID(record.TenantID)
	if err != nil {
		return InstallationSnapshot{}, ErrInvalidValue
	}
	workspace, err := im.ParseWorkspaceID(record.WorkspaceID)
	if err != nil {
		return InstallationSnapshot{}, ErrInvalidValue
	}
	actor, err := im.ParseActorID(record.AgentActorID)
	if err != nil {
		return InstallationSnapshot{}, ErrInvalidValue
	}
	installedBy, err := im.ParseHumanPrincipalID(record.InstalledBy)
	if err != nil {
		return InstallationSnapshot{}, ErrInvalidValue
	}
	capabilities, err := parseCapabilities(record.GrantedCapabilities)
	if err != nil {
		return InstallationSnapshot{}, err
	}
	boundRoutes := append([]string{}, record.BoundDataRoutes...)
	if record.DefinitionID != passport.Release().DefinitionID().String() || record.ReleaseID != passport.Release().ID().String() || record.Version != passport.Release().Version().String() {
		return InstallationSnapshot{}, ErrInvalidValue
	}
	if record.DisabledAt != nil && record.DisabledAt.IsZero() {
		return InstallationSnapshot{}, ErrInvalidValue
	}
	disabledAt := time.Time{}
	if record.DisabledAt != nil {
		disabledAt = *record.DisabledAt
	}
	value, err := NewInstallationSnapshot(id, tenant, workspace, actor, installedBy, passport, capabilities, boundRoutes, record.Status, record.CreatedAt, disabledAt, record.Revision)
	if err != nil {
		return InstallationSnapshot{}, err
	}
	return requireCanonicalRoundTrip(data, value, EncodeInstallation)
}

// EncodeCapabilitiesJSON and DecodeCapabilitiesJSON are the canonical JSONB boundary for
// capability/prohibition/grant arrays. They intentionally do not accept arbitrary object shapes.
func EncodeCapabilitiesJSON(values []Capability) ([]byte, error) {
	normalized, err := normalizeCapabilities(values)
	if err != nil {
		return nil, ErrInvalidValue
	}
	return marshalCanonical(capabilityStrings(normalized))
}

func DecodeCapabilitiesJSON(data []byte) ([]Capability, error) {
	var values []string
	if err := unmarshalCanonical(data, &values); err != nil {
		return nil, ErrInvalidValue
	}
	parsed, err := parseCapabilities(values)
	if err != nil {
		return nil, err
	}
	canonical, err := EncodeCapabilitiesJSON(parsed)
	if err != nil || !bytes.Equal(data, canonical) {
		return nil, ErrInvalidValue
	}
	return parsed, nil
}

func EncodeRoutesJSON(values []DataRoute) ([]byte, error) {
	normalized, err := normalizeRoutes(values)
	if err != nil {
		return nil, ErrInvalidValue
	}
	return marshalCanonical(routeRecords(normalized))
}

func DecodeRoutesJSON(data []byte) ([]DataRoute, error) {
	var values []persistedRoute
	if err := unmarshalCanonical(data, &values); err != nil {
		return nil, ErrInvalidValue
	}
	parsed, err := parseRoutes(values)
	if err != nil {
		return nil, err
	}
	// PostgreSQL JSONB normalizes object-key order on storage/readback. Validate the
	// decoded domain value above, but do not compare raw bytes to Go's object order.
	return parsed, nil
}

func EncodeRouteNamesJSON(values []string) ([]byte, error) {
	normalized, err := normalizeRouteNames(values)
	if err != nil {
		return nil, ErrInvalidValue
	}
	return marshalCanonical(normalized)
}

func DecodeRouteNamesJSON(data []byte) ([]string, error) {
	var values []string
	if err := unmarshalCanonical(data, &values); err != nil {
		return nil, ErrInvalidValue
	}
	normalized, err := normalizeRouteNames(values)
	if err != nil {
		return nil, err
	}
	canonical, err := EncodeRouteNamesJSON(normalized)
	if err != nil || !bytes.Equal(data, canonical) {
		return nil, ErrInvalidValue
	}
	return normalized, nil
}

func EncodeAttestationsJSON(values []TrustAttestation) ([]byte, error) {
	normalized, err := normalizeAttestations(values)
	if err != nil {
		return nil, ErrInvalidValue
	}
	records, err := encodeAttestationRecords(normalized)
	if err != nil {
		return nil, err
	}
	return marshalCanonical(records)
}

func DecodeAttestationsJSON(data []byte) ([]TrustAttestation, error) {
	var records []persistedAttestation
	if err := unmarshalCanonical(data, &records); err != nil {
		return nil, ErrInvalidValue
	}
	values, err := decodeAttestationRecords(records)
	if err != nil {
		return nil, err
	}
	return values, nil
}

func marshalCanonical(value any) ([]byte, error) {
	encoded, err := json.Marshal(value)
	if err != nil {
		return nil, ErrInvalidValue
	}
	return encoded, nil
}

func unmarshalCanonical(data []byte, value any) error {
	if len(data) == 0 {
		return ErrInvalidValue
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(value); err != nil {
		return ErrInvalidValue
	}
	var extra any
	if err := decoder.Decode(&extra); err != io.EOF {
		return ErrInvalidValue
	}
	return nil
}

func requireCanonicalRoundTrip[T any](data []byte, value T, encoder func(T) ([]byte, error)) (T, error) {
	canonical, err := encoder(value)
	if err != nil || !bytes.Equal(data, canonical) {
		var zero T
		return zero, ErrInvalidValue
	}
	return value, nil
}

func capabilityStrings(values []Capability) []string {
	result := make([]string, len(values))
	for index, value := range values {
		result[index] = string(value)
	}
	return result
}

func parseCapabilities(values []string) ([]Capability, error) {
	result := make([]Capability, len(values))
	for index, value := range values {
		parsed, err := ParseCapability(value)
		if err != nil {
			return nil, ErrInvalidValue
		}
		result[index] = parsed
	}
	return normalizeCapabilities(result)
}

func routeRecords(values []DataRoute) []persistedRoute {
	result := make([]persistedRoute, len(values))
	for index, value := range values {
		result[index] = persistedRoute{Name: value.Name(), Direction: value.Direction(), Classification: value.Classification(), Destinations: append([]string{}, value.Destinations()...), RetentionDays: value.RetentionDays()}
	}
	return result
}

func parseRoutes(values []persistedRoute) ([]DataRoute, error) {
	result := make([]DataRoute, len(values))
	for index, value := range values {
		route, err := NewDataRoute(value.Name, value.Direction, value.Classification, value.Destinations, value.RetentionDays)
		if err != nil {
			return nil, ErrInvalidValue
		}
		result[index] = route
	}
	return normalizeRoutes(result)
}

func timePointer(value time.Time) *time.Time {
	copy := value
	return &copy
}

func encodeDefinitionRecord(value DefinitionSnapshot) (persistedDefinition, error) {
	encoded, err := EncodeDefinition(value)
	if err != nil {
		return persistedDefinition{}, err
	}
	var record persistedDefinition
	if err := unmarshalCanonical(encoded, &record); err != nil {
		return persistedDefinition{}, ErrInvalidValue
	}
	return record, nil
}

func decodeDefinitionRecord(record persistedDefinition) (DefinitionSnapshot, error) {
	encoded, err := marshalCanonical(record)
	if err != nil {
		return DefinitionSnapshot{}, err
	}
	return DecodeDefinition(encoded)
}

func encodeReleaseRecord(value ReleaseSnapshot) (persistedRelease, error) {
	encoded, err := EncodeRelease(value)
	if err != nil {
		return persistedRelease{}, err
	}
	var record persistedRelease
	if err := unmarshalCanonical(encoded, &record); err != nil {
		return persistedRelease{}, ErrInvalidValue
	}
	return record, nil
}

func decodeReleaseRecord(record persistedRelease) (ReleaseSnapshot, error) {
	encoded, err := marshalCanonical(record)
	if err != nil {
		return ReleaseSnapshot{}, err
	}
	return DecodeRelease(encoded)
}

func encodeAttestationRecords(values []TrustAttestation) ([]persistedAttestation, error) {
	result := make([]persistedAttestation, len(values))
	for index, value := range values {
		if value.Issuer().IsZero() || value.EvidenceDigest().IsZero() || value.IssuedAt().Location() != time.UTC || value.ExpiresAt().Location() != time.UTC {
			return nil, ErrInvalidValue
		}
		result[index] = persistedAttestation{Issuer: value.Issuer().String(), Claim: value.Claim(), PolicyRevision: value.PolicyRevision(), EvidenceDigest: value.EvidenceDigest().Hex(), IssuedAt: value.IssuedAt(), ExpiresAt: value.ExpiresAt()}
	}
	return result, nil
}

func decodeAttestationRecords(values []persistedAttestation) ([]TrustAttestation, error) {
	result := make([]TrustAttestation, len(values))
	for index, value := range values {
		issuer, err := ParsePublisherID(value.Issuer)
		if err != nil {
			return nil, ErrInvalidValue
		}
		digest, err := ParseSHA256Digest(value.EvidenceDigest)
		if err != nil {
			return nil, ErrInvalidValue
		}
		attestation, err := NewTrustAttestation(issuer, value.Claim, value.PolicyRevision, digest, value.IssuedAt, value.ExpiresAt)
		if err != nil {
			return nil, ErrInvalidValue
		}
		result[index] = attestation
	}
	return normalizeAttestations(result)
}
