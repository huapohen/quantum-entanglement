package auth

import (
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
)

func TestProviderProfileAndVerifyRequestValidation(t *testing.T) {
	t.Parallel()
	realm := mustRealm(t, "rlm_auth")
	caps := []Capability{CapabilityVerify}
	profile, err := NewProviderProfile(im.IdentityProviderClerk, realm, "clerk.example", "wanwork-web", caps, 1024)
	if err != nil {
		t.Fatal(err)
	}
	caps[0] = "other"
	if !profile.Supports(CapabilityVerify) || profile.Supports("other") {
		t.Fatalf("profile capabilities were not copied: %#v", profile.Capabilities)
	}
	if err := (VerifyRequest{BearerToken: "header.payload.signature"}).Validate(profile); err != nil {
		t.Fatalf("valid request rejected: %v", err)
	}
	for _, token := range []string{"", "no-dot", "header payload.signature", strings.Repeat("a", 1025)} {
		if err := (VerifyRequest{BearerToken: token}).Validate(profile); !errors.Is(err, ErrInvalidRequest) {
			t.Errorf("token %q validation = %v", token, err)
		}
	}
	for _, test := range []struct {
		name     string
		provider im.IdentityProvider
		issuer   string
		audience string
		caps     []Capability
	}{
		{name: "rongcloud is not auth", provider: im.IdentityProviderRongCloud, issuer: "issuer", audience: "aud", caps: caps},
		{name: "missing issuer", provider: im.IdentityProviderClerk, issuer: "", audience: "aud", caps: caps},
		{name: "unknown capability", provider: im.IdentityProviderClerk, issuer: "issuer", audience: "aud", caps: []Capability{"admin"}},
		{name: "empty capabilities", provider: im.IdentityProviderClerk, issuer: "issuer", audience: "aud"},
	} {
		test := test
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()
			profile, err := NewProviderProfile(test.provider, realm, test.issuer, test.audience, test.caps, 1024)
			if !errors.Is(err, ErrInvalidRequest) || !profile.IsZero() {
				t.Fatalf("profile = %#v, error = %v", profile, err)
			}
		})
	}
}

func TestVerifiedIdentityIsTimeBoundAndRealmBound(t *testing.T) {
	t.Parallel()
	realm := mustRealm(t, "rlm_auth")
	profile, err := NewProviderProfile(im.IdentityProviderClerk, realm, "clerk.example", "wanwork-web", []Capability{CapabilityVerify}, 1024)
	if err != nil {
		t.Fatal(err)
	}
	external, err := im.NewExternalIdentityRef(im.IdentityProviderClerk, realm, "user_alice")
	if err != nil {
		t.Fatal(err)
	}
	issued := time.Unix(1700000000, 0).UTC()
	valid := VerifiedIdentity{
		ExternalRef: external, SessionID: "sess_1",
		IssuedAt: issued, ExpiresAt: issued.Add(time.Hour),
	}
	if err := valid.Validate(profile, issued.Add(10*time.Minute)); err != nil {
		t.Fatalf("valid identity rejected: %v", err)
	}
	if err := valid.Validate(profile, issued.Add(time.Hour)); !errors.Is(err, ErrTokenExpired) {
		t.Fatalf("expired identity = %v", err)
	}
	valid.SessionID = "bad session"
	if err := valid.Validate(profile, issued); !errors.Is(err, ErrInvalidToken) {
		t.Fatalf("unsafe session id = %v", err)
	}
	valid.SessionID = "sess_1"
	otherRealm := mustRealm(t, "rlm_other")
	valid.ExternalRef, err = im.NewExternalIdentityRef(im.IdentityProviderClerk, otherRealm, "user_alice")
	if err != nil {
		t.Fatal(err)
	}
	if err := valid.Validate(profile, issued); !errors.Is(err, ErrInvalidToken) {
		t.Fatalf("cross-realm identity = %v", err)
	}
	valid.ExternalRef = external
	valid.ExpiresAt = time.Unix(1700003600, 0).In(time.FixedZone("UTC", 0))
	if err := valid.Validate(profile, issued); !errors.Is(err, ErrInvalidToken) {
		t.Fatalf("non-canonical UTC identity = %v", err)
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
