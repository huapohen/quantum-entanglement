package agentstore

import (
	"errors"
	"slices"
	"testing"
	"time"
)

func TestResolveGrantedCapabilitiesIsActionTimeAndCanonical(t *testing.T) {
	t.Parallel()
	definition := catalogTestDefinition(t)
	release := catalogTestRelease(t)
	passport, err := NewTrustPassport(
		definition,
		release,
		catalogTestAttestations(t, release.PublishedAt().Add(-time.Hour), release.PublishedAt().Add(24*time.Hour)),
		PassportActive,
		1,
	)
	if err != nil {
		t.Fatal(err)
	}
	now := release.PublishedAt().Add(time.Hour)

	all, err := ResolveGrantedCapabilities(passport, nil, now)
	if err != nil {
		t.Fatalf("default resolution: %v", err)
	}
	if want := release.RequestedCapabilities(); !slices.Equal(all, want) {
		t.Fatalf("default capabilities = %#v, want %#v", all, want)
	}

	raw := []string{"artifact.write", "conversation.read"}
	got, err := ResolveGrantedCapabilities(passport, raw, now)
	if err != nil {
		t.Fatalf("explicit resolution: %v", err)
	}
	if want := []Capability{"artifact.write", "conversation.read"}; !slices.Equal(got, want) {
		t.Fatalf("explicit capabilities = %#v, want %#v", got, want)
	}
	raw[0] = "payment.execute"
	if got[0] != "artifact.write" {
		t.Fatal("resolver leaked caller slice")
	}

	cases := []struct {
		name string
		raw  []string
		at   time.Time
		want error
	}{
		{name: "empty explicit list", raw: []string{}, at: now, want: ErrInvalidValue},
		{name: "duplicate", raw: []string{"conversation.read", "conversation.read"}, at: now, want: ErrInvalidValue},
		{name: "malformed", raw: []string{"Conversation.Read"}, at: now, want: ErrInvalidValue},
		{name: "prohibited", raw: []string{"payment.execute"}, at: now, want: ErrCapabilityNotAllowed},
		{name: "expired passport", raw: nil, at: release.PublishedAt().Add(24 * time.Hour), want: ErrRevoked},
		{name: "non canonical clock", raw: nil, at: now.In(time.FixedZone("UTC", 0)), want: ErrInvalidValue},
	}
	for _, test := range cases {
		test := test
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()
			if _, err := ResolveGrantedCapabilities(passport, test.raw, test.at); !errors.Is(err, test.want) {
				t.Fatalf("error = %v, want %v", err, test.want)
			}
		})
	}
}
