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
	if !slices.Equal(queried, []string{
		listenAddressVariable,
		postgresMigrationURLVariable,
		postgresRuntimeURLVariable,
		postgresAuthorityManifestVariable,
		postgresAllowInsecureLocalTestVariable,
	}) {
		t.Fatalf("queried environment variables = %v", queried)
	}

	want := PublicSnapshot{
		ListenAddress: defaultListenAddress,
		AuthProvider:  ProviderFakeAuth,
		IMProvider:    ProviderFakeIM,
		OutboundMode:  OutboundDisabled,
		PostgresMode:  PostgresDisabled,
	}
	if loaded.Snapshot() != want {
		t.Fatalf("snapshot = %#v, want %#v", loaded.Snapshot(), want)
	}
}

func TestLoadRejectsInheritedMigrationCredentialWithoutRetainingValue(t *testing.T) {
	t.Parallel()
	const credentialCanary = "migration-credential-canary"
	for name, values := range map[string]map[string]string{
		"non-empty":     {postgresMigrationURLVariable: credentialCanary},
		"present empty": {postgresMigrationURLVariable: ""},
	} {
		t.Run(name, func(t *testing.T) {
			_, err := Load(mapLookup(values))
			if !errors.Is(err, ErrMigrationCredential) {
				t.Fatalf("migration credential error = %v, want %v", err, ErrMigrationCredential)
			}
			if strings.Contains(err.Error(), credentialCanary) {
				t.Fatal("migration credential value leaked into error")
			}
		})
	}
}

func TestLoadBuildsPrivateRuntimePostgresComposition(t *testing.T) {
	t.Parallel()
	const credentialCanary = "runtime-secret-canary"
	loaded, err := Load(mapLookup(map[string]string{
		postgresRuntimeURLVariable: "postgresql://wanwork_app_a:" + credentialCanary +
			"@127.0.0.1:55488/wanwork_im?sslmode=disable",
		postgresAuthorityManifestVariable:      validAuthorityManifestJSON(),
		postgresAllowInsecureLocalTestVariable: "true",
	}))
	if err != nil {
		t.Fatalf("load runtime postgres composition: %v", err)
	}
	private, ok := loaded.RuntimePostgres()
	if !ok || private.Manifest.DatabaseName != "wanwork_im" ||
		private.Manifest.RuntimeLoginRoles[0] != "wanwork_app_a" ||
		!private.AllowInsecureLocalhost {
		t.Fatalf("private runtime composition = %#v present=%v", private.Manifest, ok)
	}
	payload, err := json.Marshal(loaded.Snapshot())
	if err != nil {
		t.Fatalf("marshal runtime snapshot: %v", err)
	}
	if loaded.Snapshot().PostgresMode != PostgresRuntime ||
		strings.Contains(string(payload), credentialCanary) ||
		strings.Contains(string(payload), "55488") || strings.Contains(string(payload), "postgresql") {
		t.Fatalf("unsafe runtime public snapshot: %s", payload)
	}
	private.Manifest.RuntimeLoginRoles[0] = "mutated"
	again, _ := loaded.RuntimePostgres()
	if again.Manifest.RuntimeLoginRoles[0] != "wanwork_app_a" {
		t.Fatal("runtime manifest aliases returned caller-owned list")
	}
}

func TestLoadRejectsPartialOrMalformedRuntimePostgresComposition(t *testing.T) {
	t.Parallel()
	validURL := "postgresql://wanwork_app_a@127.0.0.1:55488/wanwork_im?sslmode=disable"
	for name, values := range map[string]map[string]string{
		"manifest without url": {
			postgresAuthorityManifestVariable: validAuthorityManifestJSON(),
		},
		"url without manifest": {
			postgresRuntimeURLVariable: validURL,
		},
		"malformed manifest": {
			postgresRuntimeURLVariable:        validURL,
			postgresAuthorityManifestVariable: `{`,
		},
		"unknown manifest field": {
			postgresRuntimeURLVariable: validURL,
			postgresAuthorityManifestVariable: strings.TrimSuffix(validAuthorityManifestJSON(), "}") +
				`,"credential":"canary"}`,
		},
		"invalid insecure flag": {
			postgresRuntimeURLVariable:             validURL,
			postgresAuthorityManifestVariable:      validAuthorityManifestJSON(),
			postgresAllowInsecureLocalTestVariable: "yes",
		},
	} {
		t.Run(name, func(t *testing.T) {
			_, err := Load(mapLookup(values))
			if !errors.Is(err, ErrInvalidPostgres) {
				t.Fatalf("runtime config error = %v, want %v", err, ErrInvalidPostgres)
			}
		})
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

func mapLookup(values map[string]string) LookupEnv {
	return func(name string) (string, bool) {
		value, ok := values[name]
		return value, ok
	}
}

func validAuthorityManifestJSON() string {
	return `{
        "databaseName":"wanwork_im",
        "databaseOwnerRole":"wanwork_im_provisioner",
        "ownerRole":"wanwork_im_owner",
        "migratorRole":"wanwork_im_migrator",
        "runtimeRole":"wanwork_im_runtime",
        "migrationLoginRoles":["wanwork_deploy_a"],
        "runtimeLoginRoles":["wanwork_app_a"]
    }`
}
