// Package agentstore defines the Agent Store control-plane contracts. Discovery metadata never
// grants runtime, data, tenant, or conversation authority; installation and action-time policy
// must narrow those declarations before an Agent can run.
package agentstore

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"regexp"
	"slices"
	"strings"
	"time"
)

var (
	ErrInvalidValue         = errors.New("invalid Agent Store value")
	ErrDefinitionConflict   = errors.New("Agent definition conflict")
	ErrReleaseConflict      = errors.New("Agent release conflict")
	ErrInstallationConflict = errors.New("Agent installation conflict")
	ErrNotFound             = errors.New("Agent Store value not found")
	ErrRevoked              = errors.New("Agent Store value revoked")
	ErrStoreUnavailable     = errors.New("Agent Store unavailable")
)

const (
	maxIDBytes          = 128
	maxNameBytes        = 128
	maxSummaryBytes     = 2048
	maxCapabilityBytes  = 128
	maxDestinationBytes = 256
	maxCollectionItems  = 128
	maxRetentionDays    = 3650
)

var (
	idSuffixPattern    = regexp.MustCompile(`^[A-Za-z0-9](?:[A-Za-z0-9_-]{0,122}[A-Za-z0-9])?$`)
	capabilityPattern  = regexp.MustCompile(`^[a-z][a-z0-9]*(?:[._:-][a-z0-9]+)*$`)
	destinationPattern = regexp.MustCompile(`^[a-z][a-z0-9]*(?:[._:/-][a-z0-9]+)*$`)
)

type SHA256Digest [sha256.Size]byte

func ParseSHA256Digest(value string) (SHA256Digest, error) {
	if len(value) != sha256.Size*2 {
		return SHA256Digest{}, ErrInvalidValue
	}
	decoded, err := hex.DecodeString(value)
	if err != nil || hex.EncodeToString(decoded) != value {
		return SHA256Digest{}, ErrInvalidValue
	}
	var digest SHA256Digest
	copy(digest[:], decoded)
	return digest, nil
}

func DigestBytes(value []byte) SHA256Digest { return sha256.Sum256(value) }
func (digest SHA256Digest) Hex() string     { return hex.EncodeToString(digest[:]) }
func (digest SHA256Digest) IsZero() bool    { return digest == SHA256Digest{} }

type ReleaseID struct{ value string }

func ParseReleaseID(value string) (ReleaseID, error) {
	if !validID(value, "agr_") {
		return ReleaseID{}, ErrInvalidValue
	}
	return ReleaseID{value: value}, nil
}
func (value ReleaseID) String() string { return value.value }
func (value ReleaseID) IsZero() bool   { return value.value == "" }

type InstallationID struct{ value string }

func ParseInstallationID(value string) (InstallationID, error) {
	if !validID(value, "ins_") {
		return InstallationID{}, ErrInvalidValue
	}
	return InstallationID{value: value}, nil
}
func (value InstallationID) String() string { return value.value }
func (value InstallationID) IsZero() bool   { return value.value == "" }

type PublisherID struct{ value string }

func ParsePublisherID(value string) (PublisherID, error) {
	if !validID(value, "pub_") {
		return PublisherID{}, ErrInvalidValue
	}
	return PublisherID{value: value}, nil
}
func (value PublisherID) String() string { return value.value }
func (value PublisherID) IsZero() bool   { return value.value == "" }

type Capability string

func ParseCapability(value string) (Capability, error) {
	if value == "" || len(value) > maxCapabilityBytes || !capabilityPattern.MatchString(value) {
		return "", ErrInvalidValue
	}
	return Capability(value), nil
}

type DataDirection string

const (
	DataInput         DataDirection = "input"
	DataOutput        DataDirection = "output"
	DataBidirectional DataDirection = "bidirectional"
)

func (direction DataDirection) Valid() bool {
	return direction == DataInput || direction == DataOutput || direction == DataBidirectional
}

type DataClassification string

const (
	DataPublic       DataClassification = "public"
	DataInternal     DataClassification = "internal"
	DataConfidential DataClassification = "confidential"
	DataRestricted   DataClassification = "restricted"
)

func (classification DataClassification) Valid() bool {
	return classification == DataPublic || classification == DataInternal ||
		classification == DataConfidential || classification == DataRestricted
}

// DataRoute is a declared data path, not a credential or permission. Destinations are abstract
// reviewed identifiers such as local, provider:rongcloud, or connector:drive; no URL, token, or
// connection string belongs in this value.
type DataRoute struct {
	name           string
	direction      DataDirection
	classification DataClassification
	destinations   []string
	retentionDays  uint16
}

func NewDataRoute(
	name string,
	direction DataDirection,
	classification DataClassification,
	destinations []string,
	retentionDays uint16,
) (DataRoute, error) {
	if name == "" || len(name) > maxNameBytes || !capabilityPattern.MatchString(name) ||
		!direction.Valid() || !classification.Valid() || len(destinations) == 0 ||
		len(destinations) > maxCollectionItems || retentionDays > maxRetentionDays {
		return DataRoute{}, ErrInvalidValue
	}
	values := append([]string(nil), destinations...)
	for _, destination := range values {
		if destination == "" || len(destination) > maxDestinationBytes ||
			!destinationPattern.MatchString(destination) || strings.Contains(destination, "..") {
			return DataRoute{}, ErrInvalidValue
		}
	}
	slices.Sort(values)
	if hasAdjacentDuplicate(values) {
		return DataRoute{}, ErrInvalidValue
	}
	return DataRoute{
		name: name, direction: direction, classification: classification,
		destinations: values, retentionDays: retentionDays,
	}, nil
}

func (route DataRoute) Name() string                       { return route.name }
func (route DataRoute) Direction() DataDirection           { return route.direction }
func (route DataRoute) Classification() DataClassification { return route.classification }
func (route DataRoute) Destinations() []string             { return append([]string(nil), route.destinations...) }
func (route DataRoute) RetentionDays() uint16              { return route.retentionDays }
func (route DataRoute) IsZero() bool {
	return route.name == "" && route.direction == "" && route.classification == "" &&
		len(route.destinations) == 0 && route.retentionDays == 0
}

type AttestationClaim string

const (
	AttestationPublisherVerified  AttestationClaim = "publisher_verified"
	AttestationSecurityReviewed   AttestationClaim = "security_reviewed"
	AttestationDataRoutesReviewed AttestationClaim = "data_routes_reviewed"
)

func (claim AttestationClaim) Valid() bool {
	return claim == AttestationPublisherVerified || claim == AttestationSecurityReviewed ||
		claim == AttestationDataRoutesReviewed
}

type TrustAttestation struct {
	issuer         PublisherID
	claim          AttestationClaim
	policyRevision uint64
	evidenceDigest SHA256Digest
	issuedAt       time.Time
	expiresAt      time.Time
}

func NewTrustAttestation(
	issuer PublisherID,
	claim AttestationClaim,
	policyRevision uint64,
	evidenceDigest SHA256Digest,
	issuedAt time.Time,
	expiresAt time.Time,
) (TrustAttestation, error) {
	if issuer.IsZero() || !claim.Valid() || policyRevision == 0 || evidenceDigest.IsZero() ||
		issuedAt.IsZero() || expiresAt.IsZero() || issuedAt.Location() != time.UTC ||
		expiresAt.Location() != time.UTC || !expiresAt.After(issuedAt) {
		return TrustAttestation{}, ErrInvalidValue
	}
	return TrustAttestation{
		issuer: issuer, claim: claim, policyRevision: policyRevision,
		evidenceDigest: evidenceDigest, issuedAt: issuedAt, expiresAt: expiresAt,
	}, nil
}

func (value TrustAttestation) Issuer() PublisherID          { return value.issuer }
func (value TrustAttestation) Claim() AttestationClaim      { return value.claim }
func (value TrustAttestation) PolicyRevision() uint64       { return value.policyRevision }
func (value TrustAttestation) EvidenceDigest() SHA256Digest { return value.evidenceDigest }
func (value TrustAttestation) IssuedAt() time.Time          { return value.issuedAt }
func (value TrustAttestation) ExpiresAt() time.Time         { return value.expiresAt }

func validID(value string, prefix string) bool {
	if value == "" || len(value) > maxIDBytes || !strings.HasPrefix(value, prefix) {
		return false
	}
	return idSuffixPattern.MatchString(strings.TrimPrefix(value, prefix))
}

func hasAdjacentDuplicate[T comparable](values []T) bool {
	for index := 1; index < len(values); index++ {
		if values[index] == values[index-1] {
			return true
		}
	}
	return false
}
