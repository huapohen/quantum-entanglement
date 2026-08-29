// Package fake provides a deterministic, zero-network Clerk-shaped verifier for local tests.
// It stores fixture tokens in memory only and never logs or exposes token material.
package fake

import (
	"context"
	"sync"
	"time"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/auth"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
)

type Clock func() time.Time

type TokenFixture struct {
	ExternalSubject string
	PrincipalID     im.HumanPrincipalID
	SessionID       string
	IssuedAt        time.Time
	ExpiresAt       time.Time
}

type Options struct {
	Realm    im.ProviderRealmID
	Issuer   string
	Audience string
	Now      Clock
	Tokens   map[string]TokenFixture
}

type Verifier struct {
	mu      sync.Mutex
	profile auth.ProviderProfile
	now     Clock
	tokens  map[string]TokenFixture
	closed  bool
}

func New(options Options) (*Verifier, error) {
	if options.Realm.IsZero() {
		return nil, auth.ErrInvalidRequest
	}
	clock := options.Now
	if clock == nil {
		clock = func() time.Time { return time.Now().UTC() }
	}
	profile, err := auth.NewProviderProfile(
		im.IdentityProviderClerk, options.Realm, options.Issuer, options.Audience,
		[]auth.Capability{auth.CapabilityVerify}, auth.MaxTokenBytes,
	)
	if err != nil {
		return nil, err
	}
	tokens := make(map[string]TokenFixture, len(options.Tokens))
	for token, fixture := range options.Tokens {
		if err := (auth.VerifyRequest{BearerToken: token}).Validate(profile); err != nil {
			return nil, auth.ErrInvalidRequest
		}
		if err := fixture.validate(profile); err != nil {
			return nil, err
		}
		if _, exists := tokens[token]; exists {
			return nil, auth.ErrInvalidRequest
		}
		tokens[token] = fixture
	}
	return &Verifier{profile: profile, now: clock, tokens: tokens}, nil
}

func (verifier *Verifier) Profile() auth.ProviderProfile {
	if verifier == nil {
		return auth.ProviderProfile{}
	}
	verifier.mu.Lock()
	defer verifier.mu.Unlock()
	profile := verifier.profile
	profile.Capabilities = append([]auth.Capability(nil), profile.Capabilities...)
	return profile
}

func (verifier *Verifier) Verify(ctx context.Context, request auth.VerifyRequest) (auth.VerifiedIdentity, error) {
	if verifier == nil || ctx == nil {
		return auth.VerifiedIdentity{}, auth.ErrInvalidRequest
	}
	if err := ctx.Err(); err != nil {
		return auth.VerifiedIdentity{}, err
	}
	profile := verifier.Profile()
	if err := request.Validate(profile); err != nil {
		return auth.VerifiedIdentity{}, err
	}
	verifier.mu.Lock()
	defer verifier.mu.Unlock()
	if verifier.closed {
		return auth.VerifiedIdentity{}, auth.ErrProviderClosed
	}
	fixture, ok := verifier.tokens[request.BearerToken]
	if !ok {
		return auth.VerifiedIdentity{}, auth.ErrInvalidToken
	}
	externalRef, err := im.NewExternalIdentityRef(
		im.IdentityProviderClerk, profile.Realm, fixture.ExternalSubject,
	)
	if err != nil {
		return auth.VerifiedIdentity{}, auth.ErrInvalidToken
	}
	identity := auth.VerifiedIdentity{
		ExternalRef: externalRef, PrincipalID: fixture.PrincipalID, SessionID: fixture.SessionID,
		IssuedAt: fixture.IssuedAt.UTC(), ExpiresAt: fixture.ExpiresAt.UTC(),
	}
	if err := identity.Validate(profile, verifier.now().UTC()); err != nil {
		return auth.VerifiedIdentity{}, err
	}
	return identity, nil
}

func (verifier *Verifier) Close() {
	if verifier == nil {
		return
	}
	verifier.mu.Lock()
	verifier.closed = true
	verifier.mu.Unlock()
}

func (fixture TokenFixture) validate(profile auth.ProviderProfile) error {
	if fixture.PrincipalID.IsZero() || fixture.IssuedAt.IsZero() || fixture.ExpiresAt.IsZero() ||
		fixture.IssuedAt.Location() != time.UTC || fixture.ExpiresAt.Location() != time.UTC {
		return auth.ErrInvalidRequest
	}
	// Constructing the mapping here enforces Clerk's user_ subject syntax before the fixture is
	// admitted. Expiry is checked at Verify time so tests can advance their clock deterministically.
	externalRef, err := im.NewExternalIdentityRef(im.IdentityProviderClerk, profile.Realm, fixture.ExternalSubject)
	if err != nil {
		return auth.ErrInvalidRequest
	}
	identity := auth.VerifiedIdentity{
		ExternalRef: externalRef,
		PrincipalID: fixture.PrincipalID, SessionID: fixture.SessionID,
		IssuedAt: fixture.IssuedAt, ExpiresAt: fixture.ExpiresAt,
	}
	if err := identity.Validate(profile, time.Time{}); err != nil {
		return auth.ErrInvalidRequest
	}
	return nil
}
