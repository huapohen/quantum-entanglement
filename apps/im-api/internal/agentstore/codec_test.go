package agentstore

import (
	"bytes"
	"errors"
	"testing"
	"time"
)

func TestSnapshotCodecsRoundTripCanonicalDomainValues(t *testing.T) {
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
	installation := installationTestSnapshot(t, passport, []Capability{catalogTestCapability(t, "conversation.read")}, []string{"conversation.context"}, InstallationActive)

	definitionBytes, err := EncodeDefinition(definition)
	if err != nil {
		t.Fatal(err)
	}
	definitionRoundTrip, err := DecodeDefinition(definitionBytes)
	if err != nil || !bytes.Equal(definitionBytes, mustEncode(t, EncodeDefinition, definitionRoundTrip)) {
		t.Fatalf("definition round trip = %v", err)
	}

	releaseBytes, err := EncodeRelease(release)
	if err != nil {
		t.Fatal(err)
	}
	releaseRoundTrip, err := DecodeRelease(releaseBytes)
	if err != nil || !bytes.Equal(releaseBytes, mustEncode(t, EncodeRelease, releaseRoundTrip)) {
		t.Fatalf("release round trip = %v", err)
	}

	passportBytes, err := EncodeTrustPassport(passport)
	if err != nil {
		t.Fatal(err)
	}
	passportRoundTrip, err := DecodeTrustPassport(passportBytes)
	if err != nil || !bytes.Equal(passportBytes, mustEncode(t, EncodeTrustPassport, passportRoundTrip)) {
		t.Fatalf("passport round trip = %v", err)
	}

	installationBytes, err := EncodeInstallation(installation)
	if err != nil {
		t.Fatal(err)
	}
	installationRoundTrip, err := DecodeInstallation(installationBytes, passport)
	if err != nil || !bytes.Equal(installationBytes, mustEncode(t, EncodeInstallation, installationRoundTrip)) {
		t.Fatalf("installation round trip = %v", err)
	}
	if installationRoundTrip.DefinitionID() != passport.Release().DefinitionID() ||
		installationRoundTrip.ReleaseID() != passport.Release().ID() ||
		installationRoundTrip.Version() != passport.Release().Version() {
		t.Fatalf("installation identity was not rebound to passport: %#v", installationRoundTrip)
	}
}

func TestSnapshotCodecsRejectNonCanonicalOrUntrustedShape(t *testing.T) {
	t.Parallel()
	definition := catalogTestDefinition(t)
	encoded, err := EncodeDefinition(definition)
	if err != nil {
		t.Fatal(err)
	}
	for name, mutate := range map[string]func([]byte) []byte{
		"trailing whitespace": func(value []byte) []byte { return append(append([]byte{}, value...), ' ') },
		"unknown field": func(value []byte) []byte {
			return bytes.Replace(value, []byte(`"revision":1}`), []byte(`"revision":1,"extra":true}`), 1)
		},
		"trailing value": func(value []byte) []byte { return append(append(append([]byte{}, value...), ' '), []byte(`{}`)...) },
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := DecodeDefinition(mutate(encoded)); !errors.Is(err, ErrInvalidValue) {
				t.Fatalf("DecodeDefinition error = %v, want %v", err, ErrInvalidValue)
			}
		})
	}

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
	installation := installationTestSnapshot(t, passport, []Capability{catalogTestCapability(t, "conversation.read")}, []string{"conversation.context"}, InstallationActive)
	installationBytes, err := EncodeInstallation(installation)
	if err != nil {
		t.Fatal(err)
	}
	otherPassport := passport
	otherPassport.release.id = ReleaseID{value: "agr_other_100"}
	if _, err := DecodeInstallation(installationBytes, otherPassport); !errors.Is(err, ErrInvalidValue) {
		t.Fatalf("installation with mismatched passport error = %v, want %v", err, ErrInvalidValue)
	}
}

func mustEncode[T any](t *testing.T, encoder func(T) ([]byte, error), value T) []byte {
	t.Helper()
	encoded, err := encoder(value)
	if err != nil {
		t.Fatal(err)
	}
	return encoded
}
