package fake

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/auth"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
)

func TestVerifierAuthenticatesFixtureWithoutTenantAuthority(t *testing.T) {
	t.Parallel()
	realm := mustRealm(t, "rlm_fake_auth")
	now := time.Unix(1700000000, 0).UTC()
	principal, err := im.ParseHumanPrincipalID("hpr_alice")
	if err != nil {
		t.Fatal(err)
	}
	verifier, err := New(Options{
		Realm: realm, Issuer: "clerk.example", Audience: "wanwork-web",
		Now: func() time.Time { return now },
		Tokens: map[string]TokenFixture{
			"header.payload.signature": {
				ExternalSubject: "user_alice", PrincipalID: principal, SessionID: "sess_1",
				IssuedAt: now.Add(-time.Minute), ExpiresAt: now.Add(time.Hour),
			},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	identity, err := verifier.Verify(context.Background(), auth.VerifyRequest{BearerToken: "header.payload.signature"})
	if err != nil {
		t.Fatalf("Verify() error = %v", err)
	}
	if identity.ExternalRef.Provider() != im.IdentityProviderClerk ||
		identity.ExternalRef.RealmID() != realm || identity.ExternalRef.SubjectID() != "user_alice" ||
		identity.PrincipalID != principal || identity.SessionID != "sess_1" {
		t.Fatalf("unexpected verified identity: %#v", identity)
	}
	if err := identity.Validate(verifier.Profile(), now); err != nil {
		t.Fatalf("verified identity validation = %v", err)
	}
	if _, err := verifier.Verify(context.Background(), auth.VerifyRequest{BearerToken: "unknown.payload.signature"}); !errors.Is(err, auth.ErrInvalidToken) {
		t.Fatalf("unknown token = %v", err)
	}
	canceled, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := verifier.Verify(canceled, auth.VerifyRequest{BearerToken: "header.payload.signature"}); !errors.Is(err, context.Canceled) {
		t.Fatalf("canceled verify = %v", err)
	}
}

func TestVerifierRejectsExpiredOrMalformedFixturesAndClose(t *testing.T) {
	t.Parallel()
	realm := mustRealm(t, "rlm_fake_auth")
	now := time.Unix(1700000000, 0).UTC()
	principal, err := im.ParseHumanPrincipalID("hpr_alice")
	if err != nil {
		t.Fatal(err)
	}
	base := func() Options {
		return Options{
			Realm: realm, Issuer: "clerk.example", Audience: "wanwork-web",
			Now: func() time.Time { return now },
		}
	}
	bad := base()
	bad.Tokens = map[string]TokenFixture{
		"header.payload.signature": {
			ExternalSubject: "not-clerk-subject", PrincipalID: principal, SessionID: "sess_1",
			IssuedAt: now, ExpiresAt: now.Add(time.Hour),
		},
	}
	if _, err := New(bad); !errors.Is(err, auth.ErrInvalidRequest) {
		t.Fatalf("malformed fixture construction = %v", err)
	}
	expired := base()
	expired.Tokens = map[string]TokenFixture{
		"header.payload.signature": {
			ExternalSubject: "user_alice", PrincipalID: principal, SessionID: "sess_1",
			IssuedAt: now.Add(-time.Hour), ExpiresAt: now.Add(-time.Minute),
		},
	}
	verifier, err := New(expired)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := verifier.Verify(context.Background(), auth.VerifyRequest{BearerToken: "header.payload.signature"}); !errors.Is(err, auth.ErrTokenExpired) {
		t.Fatalf("expired fixture verify = %v", err)
	}
	verifier.Close()
	if _, err := verifier.Verify(context.Background(), auth.VerifyRequest{BearerToken: "header.payload.signature"}); !errors.Is(err, auth.ErrProviderClosed) {
		t.Fatalf("closed verifier = %v", err)
	}
}

func mustRealm(t *testing.T, value string) im.ProviderRealmID {
	t.Helper()
	parsed, err := im.ParseProviderRealmID(value)
	if err != nil {
		t.Fatalf("ParseProviderRealmID(%q): %v", value, err)
	}
	return parsed
}
