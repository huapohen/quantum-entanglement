package agentstore

import (
	"errors"
	"strings"
	"testing"
	"time"
)

func TestAgentStoreIdentifiersAndDigestsAreCanonical(t *testing.T) {
	t.Parallel()
	release, err := ParseReleaseID("agr_research_1")
	if err != nil || release.String() != "agr_research_1" || release.IsZero() {
		t.Fatalf("release = %#v, %v", release, err)
	}
	installation, err := ParseInstallationID("ins_acme_research")
	if err != nil || installation.IsZero() {
		t.Fatalf("installation = %#v, %v", installation, err)
	}
	publisher, err := ParsePublisherID("pub_acme")
	if err != nil || publisher.IsZero() {
		t.Fatalf("publisher = %#v, %v", publisher, err)
	}
	for _, value := range []string{"", "release_x", "agr_-bad", "agr_bad-"} {
		if parsed, err := ParseReleaseID(value); !errors.Is(err, ErrInvalidValue) || !parsed.IsZero() {
			t.Errorf("ParseReleaseID(%q) = %#v, %v", value, parsed, err)
		}
	}
	digest := DigestBytes([]byte("release artifact"))
	parsed, err := ParseSHA256Digest(digest.Hex())
	if err != nil || parsed != digest || digest.IsZero() {
		t.Fatalf("digest round trip = %s, %v", parsed.Hex(), err)
	}
	if _, err := ParseSHA256Digest(strings.ToUpper(digest.Hex())); !errors.Is(err, ErrInvalidValue) {
		t.Fatalf("uppercase digest = %v", err)
	}
}

func TestDataRouteSortsAndCopiesDestinations(t *testing.T) {
	t.Parallel()
	destinations := []string{"provider:rongcloud", "local"}
	route, err := NewDataRoute("conversation.context", DataBidirectional, DataConfidential, destinations, 30)
	if err != nil {
		t.Fatal(err)
	}
	destinations[0] = "connector:evil"
	got := route.Destinations()
	if len(got) != 2 || got[0] != "local" || got[1] != "provider:rongcloud" || route.IsZero() {
		t.Fatalf("unexpected route: %#v", got)
	}
	got[0] = "changed"
	if route.Destinations()[0] != "local" {
		t.Fatal("route destination accessor leaked mutable storage")
	}
	for _, test := range []struct {
		name         string
		destinations []string
		retention    uint16
	}{
		{name: "empty", destinations: nil},
		{name: "duplicate", destinations: []string{"local", "local"}},
		{name: "URL forbidden", destinations: []string{"https://example.com"}},
		{name: "parent segment", destinations: []string{"connector:../secret"}},
		{name: "retention", destinations: []string{"local"}, retention: maxRetentionDays + 1},
	} {
		test := test
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()
			route, err := NewDataRoute("route", DataInput, DataInternal, test.destinations, test.retention)
			if !errors.Is(err, ErrInvalidValue) || !route.IsZero() {
				t.Fatalf("route = %#v, error = %v", route, err)
			}
		})
	}
}

func TestTrustAttestationRequiresBoundedUTCValidity(t *testing.T) {
	t.Parallel()
	publisher, err := ParsePublisherID("pub_security")
	if err != nil {
		t.Fatal(err)
	}
	issued := time.Unix(1700000000, 0).UTC()
	attestation, err := NewTrustAttestation(
		publisher, AttestationSecurityReviewed, 7, DigestBytes([]byte("evidence")),
		issued, issued.Add(24*time.Hour),
	)
	if err != nil || attestation.Issuer() != publisher || attestation.PolicyRevision() != 7 ||
		attestation.Claim() != AttestationSecurityReviewed {
		t.Fatalf("attestation = %#v, %v", attestation, err)
	}
	if _, err := NewTrustAttestation(
		publisher, AttestationSecurityReviewed, 7, DigestBytes([]byte("evidence")),
		issued, issued,
	); !errors.Is(err, ErrInvalidValue) {
		t.Fatalf("non-positive attestation validity = %v", err)
	}
	if _, err := NewTrustAttestation(
		publisher, AttestationSecurityReviewed, 7, DigestBytes([]byte("evidence")),
		issued.In(time.FixedZone("UTC", 0)), issued.Add(time.Hour),
	); !errors.Is(err, ErrInvalidValue) {
		t.Fatalf("non-canonical UTC attestation = %v", err)
	}
}
