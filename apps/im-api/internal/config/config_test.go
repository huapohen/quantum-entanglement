package config

import (
	"encoding/json"
	"errors"
	"slices"
	"strings"
	"testing"
)

func TestLoadDefaultsToLoopbackFakeComposition(t *testing.T) {
	t.Parallel()

	var queried []string
	loaded, err := Load(func(name string) (string, bool) {
		queried = append(queried, name)
		return "", false
	})
	if err != nil {
		t.Fatalf("load defaults: %v", err)
	}
	if !slices.Equal(queried, []string{listenAddressVariable}) {
		t.Fatalf("queried environment variables = %v", queried)
	}

	want := PublicSnapshot{
		ListenAddress: defaultListenAddress,
		AuthProvider:  ProviderFakeAuth,
		IMProvider:    ProviderFakeIM,
		OutboundMode:  OutboundDisabled,
	}
	if loaded.Snapshot() != want {
		t.Fatalf("snapshot = %#v, want %#v", loaded.Snapshot(), want)
	}
}

func TestLoadAcceptsOnlyNumericLoopbackOverride(t *testing.T) {
	t.Parallel()

	for _, address := range []string{"127.0.0.1:19080", "[::1]:19080"} {
		loaded, err := Load(fixedLookup(listenAddressVariable, address))
		if err != nil {
			t.Fatalf("load %q: %v", address, err)
		}
		if loaded.ListenAddress() != address {
			t.Fatalf("listen address = %q, want %q", loaded.ListenAddress(), address)
		}
	}
}

func TestLoadRejectsUnsafeListenAddressesWithoutEchoingInput(t *testing.T) {
	t.Parallel()

	for _, address := range []string{
		"0.0.0.0:18080",
		"localhost:18080",
		"192.0.2.1:18080",
		"127.0.0.1:0",
		"127.0.0.1:70000",
		"credential-canary",
	} {
		_, err := Load(fixedLookup(listenAddressVariable, address))
		if !errors.Is(err, ErrInvalidListenAddress) {
			t.Fatalf("load %q error = %v, want %v", address, err, ErrInvalidListenAddress)
		}
		if strings.Contains(err.Error(), address) {
			t.Fatalf("invalid environment value %q leaked into error", address)
		}
	}
}

func TestSnapshotContainsNoSecretOrEndpointFields(t *testing.T) {
	t.Parallel()

	loaded, err := Load(fixedLookup("RONGCLOUD_SECRET", "secret-canary"))
	if err != nil {
		t.Fatalf("load config: %v", err)
	}
	payload, err := json.Marshal(loaded.Snapshot())
	if err != nil {
		t.Fatalf("marshal snapshot: %v", err)
	}
	for _, forbidden := range []string{"secret-canary", "secret", "token", "apiKey", "endpoint"} {
		if strings.Contains(string(payload), forbidden) {
			t.Fatalf("snapshot %q contains forbidden value %q", payload, forbidden)
		}
	}
}

func TestValidationRejectsNonFakeOrOutboundComposition(t *testing.T) {
	t.Parallel()

	testCases := []Config{
		{listenAddress: defaultListenAddress, authProvider: "auth.clerk.v1", imProvider: ProviderFakeIM, outboundMode: OutboundDisabled},
		{listenAddress: defaultListenAddress, authProvider: ProviderFakeAuth, imProvider: "im.rongcloud.v1", outboundMode: OutboundDisabled},
		{listenAddress: defaultListenAddress, authProvider: ProviderFakeAuth, imProvider: ProviderFakeIM, outboundMode: "enabled"},
	}
	for _, testCase := range testCases {
		if err := testCase.validate(); !errors.Is(err, ErrUnsafeComposition) {
			t.Fatalf("validate %#v error = %v, want %v", testCase, err, ErrUnsafeComposition)
		}
	}
}

func fixedLookup(expectedName, value string) LookupEnv {
	return func(name string) (string, bool) {
		if name != expectedName {
			return "", false
		}
		return value, true
	}
}
