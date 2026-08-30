// Package auth defines the provider-neutral authentication boundary for the native IM.
//
// The port returns only a verified Clerk subject. It does not carry a platform principal mapping,
// tenant membership, conversation ACL, Agent installation, or any other platform authority; those
// facts must be resolved again by the platform at action time.
package auth

import (
	"context"
	"errors"
	"regexp"
	"strings"
	"time"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
)

var (
	ErrInvalidRequest      = errors.New("invalid auth provider request")
	ErrInvalidToken        = errors.New("invalid auth token")
	ErrTokenExpired        = errors.New("auth token expired")
	ErrProviderUnavailable = errors.New("auth provider unavailable")
	ErrProviderClosed      = errors.New("auth provider is closed")
)

const (
	ProviderMetadataSchemaVersion = 1
	MaxTokenBytes                 = 16 * 1024
	MaxIssuerBytes                = 256
	MaxAudienceBytes              = 256
	MaxSessionIDBytes             = 256
)

var opaqueIDPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$`)

type Capability string

const CapabilityVerify Capability = "verify"

func (capability Capability) Valid() bool { return capability == CapabilityVerify }

// ProviderProfile is configuration metadata reviewed for one Clerk application/environment.
// Issuer and Audience are matching constraints, never secrets.
type ProviderProfile struct {
	Provider              im.IdentityProvider
	Realm                 im.ProviderRealmID
	Issuer                string
	Audience              string
	Capabilities          []Capability
	MetadataSchemaVersion int
	MaxTokenBytes         int
}

func NewProviderProfile(
	provider im.IdentityProvider,
	realm im.ProviderRealmID,
	issuer string,
	audience string,
	capabilities []Capability,
	maxTokenBytes int,
) (ProviderProfile, error) {
	if provider != im.IdentityProviderClerk || realm.IsZero() ||
		!validOpaqueText(issuer, MaxIssuerBytes) || !validOpaqueText(audience, MaxAudienceBytes) ||
		len(capabilities) == 0 || maxTokenBytes <= 0 || maxTokenBytes > MaxTokenBytes {
		return ProviderProfile{}, ErrInvalidRequest
	}
	seen := make(map[Capability]struct{}, len(capabilities))
	for _, capability := range capabilities {
		if !capability.Valid() {
			return ProviderProfile{}, ErrInvalidRequest
		}
		if _, exists := seen[capability]; exists {
			return ProviderProfile{}, ErrInvalidRequest
		}
		seen[capability] = struct{}{}
	}
	return ProviderProfile{
		Provider: provider, Realm: realm, Issuer: issuer, Audience: audience,
		Capabilities:          append([]Capability(nil), capabilities...),
		MetadataSchemaVersion: ProviderMetadataSchemaVersion, MaxTokenBytes: maxTokenBytes,
	}, nil
}

func (profile ProviderProfile) Supports(capability Capability) bool {
	for _, candidate := range profile.Capabilities {
		if candidate == capability {
			return true
		}
	}
	return false
}

func (profile ProviderProfile) IsZero() bool {
	return profile.Provider == "" && profile.Realm.IsZero() && profile.Issuer == "" && profile.Audience == "" && len(profile.Capabilities) == 0
}

type VerifyRequest struct{ BearerToken string }

func (request VerifyRequest) Validate(profile ProviderProfile) error {
	if profile.Provider != im.IdentityProviderClerk || !profile.Supports(CapabilityVerify) ||
		request.BearerToken == "" || len(request.BearerToken) > profile.MaxTokenBytes ||
		strings.IndexByte(request.BearerToken, ' ') >= 0 || !strings.Contains(request.BearerToken, ".") {
		return ErrInvalidRequest
	}
	return nil
}

// VerifiedIdentity is an authentication assertion only. The external reference and principal
// mapping must still be joined to current tenant membership and policy before any command runs.
type VerifiedIdentity struct {
	ExternalRef im.ExternalIdentityRef
	SessionID   string
	IssuedAt    time.Time
	ExpiresAt   time.Time
}

func (identity VerifiedIdentity) Validate(profile ProviderProfile, now time.Time) error {
	if profile.Provider != im.IdentityProviderClerk || identity.ExternalRef.IsZero() ||
		identity.ExternalRef.Provider() != profile.Provider || identity.ExternalRef.RealmID() != profile.Realm ||
		!opaqueIDPattern.MatchString(identity.SessionID) ||
		len(identity.SessionID) > MaxSessionIDBytes || identity.IssuedAt.IsZero() || identity.ExpiresAt.IsZero() ||
		identity.IssuedAt.Location() != time.UTC || identity.ExpiresAt.Location() != time.UTC ||
		!now.IsZero() && now.Location() != time.UTC || !identity.ExpiresAt.After(identity.IssuedAt) {
		return ErrInvalidToken
	}
	if !now.IsZero() && !now.Before(identity.ExpiresAt) {
		return ErrTokenExpired
	}
	return nil
}

// Verifier is deliberately narrower than a Clerk SDK. A real adapter must verify signature,
// issuer, audience, time claims, and key rotation before returning VerifiedIdentity.
type Verifier interface {
	Profile() ProviderProfile
	Verify(context.Context, VerifyRequest) (VerifiedIdentity, error)
}

func validOpaqueText(value string, max int) bool {
	return value != "" && len(value) <= max && opaqueIDPattern.MatchString(value)
}
