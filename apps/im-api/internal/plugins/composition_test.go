package plugins

import (
	"bytes"
	"errors"
	"os"
	"slices"
	"strings"
	"testing"
)

const (
	authSchemaDigest    = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
	imSchemaDigest      = "sha256:2222222222222222222222222222222222222222222222222222222222222222"
	runtimeSchemaDigest = "sha256:3333333333333333333333333333333333333333333333333333333333333333"
	authArtifactDigest  = "sha256:4444444444444444444444444444444444444444444444444444444444444444"
	imArtifactDigest    = "sha256:5555555555555555555555555555555555555555555555555555555555555555"
	runtimeArtifact     = "sha256:6666666666666666666666666666666666666666666666666666666666666666"
)

func TestComposeAppliesWholeRowReplacementAndExplicitRemoval(t *testing.T) {
	t.Parallel()

	registry := compositionRegistry(t, nil)
	composition := baseComposition()
	composition.Bundles = []ConfigurationLayer{{
		ID: "bundle.local", Revision: 1,
		Rows: []ConfigurationRow{compositionRow("im", "im.fake.v1", imArtifactDigest, PluginConfig{
			Values: map[string]string{"mode": "bundle", "bundle_only": "present"},
			SecretRefs: map[string]SecretReference{
				"provider_credential": {Broker: "test-broker", ReferenceID: "im-middle"},
			},
		})},
	}}
	composition.TenantOverlay = &ConfigurationLayer{
		ID: "tenant.acme", Revision: 7, TenantID: "tenant-acme",
		Rows: []ConfigurationRow{
			compositionRow("im", "im.fake.v1", imArtifactDigest, PluginConfig{
				Values: map[string]string{"mode": "tenant"},
				SecretRefs: map[string]SecretReference{
					"provider_credential": {Broker: "test-broker", ReferenceID: "im-current"},
				},
			}),
			{RowID: "runtime", Operation: RowRemove},
		},
	}

	result, err := registry.Compose(composition, nil)
	if err != nil {
		t.Fatalf("compose: %v", err)
	}
	if result.Candidate.SchemaVersion() != effectiveConfigurationSchemaVersion {
		t.Fatalf("schema version = %d", result.Candidate.SchemaVersion())
	}
	if result.Candidate.TenantID() != "tenant-acme" {
		t.Fatalf("tenant = %q", result.Candidate.TenantID())
	}
	wantSources := []LayerKind{LayerKindProfile, LayerKindBundle, LayerKindTenantOverlay}
	sources := result.Candidate.Sources()
	if len(sources) != len(wantSources) {
		t.Fatalf("sources = %#v", sources)
	}
	for index, kind := range wantSources {
		if sources[index].Kind != kind || !sha256DigestPattern.MatchString(sources[index].Digest) {
			t.Fatalf("source %d = %#v", index, sources[index])
		}
	}

	rows := result.Candidate.Rows()
	if len(rows) != 2 || rows[0].RowID != "auth" || rows[1].RowID != "im" {
		t.Fatalf("rows = %#v", rows)
	}
	if got := rows[1].Config.Values; len(got) != 2 || got["mode"] != "tenant" || got["schema_default"] != "safe" {
		t.Fatalf("whole-row materialized values = %#v", got)
	}
	if _, leaked := rows[1].Config.Values["profile_only"]; leaked {
		t.Fatalf("profile value survived whole-row replacement: %#v", rows[1].Config.Values)
	}
	if _, leaked := rows[1].Config.Values["bundle_only"]; leaked {
		t.Fatalf("bundle value survived whole-row replacement: %#v", rows[1].Config.Values)
	}
	wantOrder := []PluginID{"auth.fake.v1", "im.fake.v1"}
	if plan := result.Candidate.Plan(); !slices.Equal(plan.Order, wantOrder) || len(plan.Bindings) != 0 {
		t.Fatalf("selected plan = %#v", plan)
	}
	if len(result.Diff.RowsAdded) != 2 || len(result.Diff.CapabilitiesAdded) != 2 || len(result.Diff.SecretRefs) != 1 {
		t.Fatalf("initial diff = %#v", result.Diff)
	}
}

func TestComposeIsDeterministicAndReturnsImmutableSnapshot(t *testing.T) {
	t.Parallel()

	firstRegistry := compositionRegistry(t, []PluginID{"runtime.fake.v1", "im.fake.v1", "auth.fake.v1"})
	secondRegistry := compositionRegistry(t, []PluginID{"auth.fake.v1", "im.fake.v1", "runtime.fake.v1"})
	first := baseComposition()
	second := baseComposition()
	slices.Reverse(second.Profile.Rows)
	second.Profile.Rows[1].Config.Values = map[string]string{}
	second.Profile.Rows[1].Config.Values["profile_only"] = "present"
	second.Profile.Rows[1].Config.Values["mode"] = "profile"

	firstResult, err := firstRegistry.Compose(first, nil)
	if err != nil {
		t.Fatalf("compose first: %v", err)
	}
	secondResult, err := secondRegistry.Compose(second, nil)
	if err != nil {
		t.Fatalf("compose second: %v", err)
	}
	if firstResult.Candidate.Digest() != secondResult.Candidate.Digest() ||
		!bytes.Equal(firstResult.Candidate.CanonicalBytes(), secondResult.Candidate.CanonicalBytes()) {
		t.Fatalf("determinism mismatch: %s != %s", firstResult.Candidate.Digest(), secondResult.Candidate.Digest())
	}

	originalDigest := firstResult.Candidate.Digest()
	originalCanonical := firstResult.Candidate.CanonicalBytes()
	first.Profile.Rows[1].Config.Values["mode"] = "mutated-input"
	returnedRows := firstResult.Candidate.Rows()
	returnedRows[1].Config.Values["mode"] = "mutated-getter"
	returnedRows[1].Capabilities[0] = "mutated.capability"
	returnedPlan := firstResult.Candidate.Plan()
	returnedPlan.Order[0] = "mutated.fake.v1"
	returnedCanonical := firstResult.Candidate.CanonicalBytes()
	returnedCanonical[0] ^= 0xff
	returnedConfigs := firstResult.Candidate.PluginConfigs()
	returnedConfigs["im.fake.v1"].Values["mode"] = "mutated-config-getter"
	if firstResult.Candidate.Digest() != originalDigest ||
		!bytes.Equal(firstResult.Candidate.CanonicalBytes(), originalCanonical) ||
		firstResult.Candidate.Rows()[1].Config.Values["mode"] != "profile" {
		t.Fatal("effective configuration was mutated through an input or getter")
	}
}

func TestEffectiveConfigurationHasGoldenCanonicalBytesAndDomainSeparatedDigest(t *testing.T) {
	t.Parallel()

	registry := compositionRegistry(t, nil)
	result, err := registry.Compose(baseComposition(), nil)
	if err != nil {
		t.Fatalf("compose: %v", err)
	}
	wantCanonical, err := os.ReadFile("testdata/effective_configuration_v1.golden.json")
	if err != nil {
		t.Fatalf("read golden canonical bytes: %v", err)
	}
	wantCanonical = bytes.TrimSuffix(wantCanonical, []byte("\n"))
	const wantDigest = "sha256:495f1c65e5cb4e0c2216ddb6b824150c0503c7e0aeddeb5f4cae9ac93c81023e"
	if got := result.Candidate.CanonicalBytes(); !bytes.Equal(got, wantCanonical) {
		t.Fatalf("canonical bytes:\n%s", got)
	}
	if got := result.Candidate.Digest(); got != wantDigest {
		t.Fatalf("digest = %s", got)
	}
	if got := digestBytes(layerDigestDomain, result.Candidate.CanonicalBytes()); got == result.Candidate.Digest() {
		t.Fatal("layer and effective configuration digest domains are not separated")
	}
}

func TestComposeRejectsInvalidLayersRowsTrustAndSecrets(t *testing.T) {
	t.Parallel()

	valid := baseComposition()
	testCases := []struct {
		name      string
		mutate    func(*Composition, *Registry)
		wantError error
	}{
		{
			name: "duplicate layer",
			mutate: func(value *Composition, _ *Registry) {
				value.Bundles = []ConfigurationLayer{{ID: value.Profile.ID, Revision: 2}}
			},
			wantError: ErrDuplicateLayer,
		},
		{
			name: "duplicate row in layer",
			mutate: func(value *Composition, _ *Registry) {
				value.Profile.Rows = append(value.Profile.Rows, value.Profile.Rows[0])
			},
			wantError: ErrDuplicateRow,
		},
		{
			name: "cross tenant overlay",
			mutate: func(value *Composition, _ *Registry) {
				value.TenantOverlay = &ConfigurationLayer{ID: "tenant.other", Revision: 1, TenantID: "other"}
			},
			wantError: ErrInvalidComposition,
		},
		{
			name: "remove carries selection",
			mutate: func(value *Composition, _ *Registry) {
				value.Bundles = []ConfigurationLayer{{
					ID: "remove.invalid", Revision: 1,
					Rows: []ConfigurationRow{{RowID: "im", Operation: RowRemove, PluginID: "im.fake.v1"}},
				}}
			},
			wantError: ErrInvalidPluginConfig,
		},
		{
			name: "version drift",
			mutate: func(value *Composition, _ *Registry) {
				value.Profile.Rows[0].PluginVersion = "9.9.9"
			},
			wantError: ErrPluginVersionDrift,
		},
		{
			name: "artifact drift",
			mutate: func(value *Composition, _ *Registry) {
				value.Profile.Rows[0].ArtifactDigest = testArtifactDigest
			},
			wantError: ErrArtifactDigestDrift,
		},
		{
			name: "sensitive ordinary value",
			mutate: func(value *Composition, _ *Registry) {
				value.Profile.Rows[0].Config.Values = map[string]string{"api_key": "not-allowed-here"}
			},
			wantError: ErrInvalidPluginConfig,
		},
		{
			name: "undeclared secret reference",
			mutate: func(value *Composition, _ *Registry) {
				value.Profile.Rows[0].Config.SecretRefs = map[string]SecretReference{
					"unknown": {Broker: "test-broker", ReferenceID: "opaque"},
				}
			},
			wantError: ErrInvalidPluginConfig,
		},
		{
			name: "missing host schema",
			mutate: func(_ *Composition, registry *Registry) {
				delete(registry.schemas, authSchemaDigest)
			},
			wantError: ErrMissingConfigSchema,
		},
		{
			name: "same plugin in two rows",
			mutate: func(value *Composition, _ *Registry) {
				duplicate := value.Profile.Rows[0]
				duplicate.RowID = "auth.second"
				value.Profile.Rows = append(value.Profile.Rows, duplicate)
			},
			wantError: ErrDuplicatePluginRow,
		},
	}

	for _, testCase := range testCases {
		t.Run(testCase.name, func(t *testing.T) {
			t.Parallel()
			registry := compositionRegistry(t, nil)
			composition := cloneCompositionForTest(valid)
			testCase.mutate(&composition, registry)
			_, err := registry.Compose(composition, nil)
			if !errors.Is(err, testCase.wantError) {
				t.Fatalf("compose error = %v, want %v", err, testCase.wantError)
			}
		})
	}
}

func TestComposeFailsWhenRemovalLeavesMissingDependency(t *testing.T) {
	t.Parallel()

	registry := compositionRegistry(t, nil)
	composition := baseComposition()
	composition.Bundles = []ConfigurationLayer{{
		ID: "remove.im", Revision: 1,
		Rows: []ConfigurationRow{{RowID: "im", Operation: RowRemove}},
	}}
	if _, err := registry.Compose(composition, nil); !errors.Is(err, ErrMissingProvider) {
		t.Fatalf("compose error = %v, want %v", err, ErrMissingProvider)
	}
}

func TestCompositionValidationDoesNotEchoRejectedSecretMaterial(t *testing.T) {
	t.Parallel()

	const canary = "configuration-secret-canary"
	registry := compositionRegistry(t, nil)
	composition := baseComposition()
	composition.Profile.Rows[0].Config.Values = map[string]string{"api_key": canary}
	_, err := registry.Compose(composition, nil)
	if !errors.Is(err, ErrInvalidPluginConfig) {
		t.Fatalf("compose error = %v, want %v", err, ErrInvalidPluginConfig)
	}
	if strings.Contains(err.Error(), canary) {
		t.Fatal("validation error echoed rejected secret material")
	}
}

func TestBundleOrderChangesWholeRowWinnerAndDigest(t *testing.T) {
	t.Parallel()

	registry := compositionRegistry(t, nil)
	first := baseComposition()
	first.Bundles = []ConfigurationLayer{
		imModeBundle("bundle.first", "first"),
		imModeBundle("bundle.second", "second"),
	}
	second := cloneCompositionForTest(first)
	slices.Reverse(second.Bundles)
	firstResult, err := registry.Compose(first, nil)
	if err != nil {
		t.Fatalf("compose first: %v", err)
	}
	secondResult, err := registry.Compose(second, nil)
	if err != nil {
		t.Fatalf("compose second: %v", err)
	}
	if firstResult.Candidate.Rows()[1].Config.Values["mode"] != "second" ||
		secondResult.Candidate.Rows()[1].Config.Values["mode"] != "first" ||
		firstResult.Candidate.Digest() == secondResult.Candidate.Digest() {
		t.Fatal("ordered bundle precedence was not preserved")
	}
}

func TestConfigurationDiffScopesExpansionRetargetAndSupplyChainDrift(t *testing.T) {
	t.Parallel()

	oldRegistry := singlePluginRegistry(t, imSchemaDigest, imArtifactDigest, []CapabilityID{"im.send"}, []string{"https://old.invalid"})
	oldComposition := singlePluginComposition("old-reference")
	baselineResult, err := oldRegistry.Compose(oldComposition, nil)
	if err != nil {
		t.Fatalf("compose baseline: %v", err)
	}

	const newSchema = "sha256:7777777777777777777777777777777777777777777777777777777777777777"
	const newArtifact = "sha256:8888888888888888888888888888888888888888888888888888888888888888"
	newRegistry := singlePluginRegistry(t, newSchema, newArtifact, []CapabilityID{"im.admin", "im.send"}, []string{"https://new.invalid"})
	candidateComposition := singlePluginComposition("new-reference")
	candidateComposition.Profile.Rows[0].ArtifactDigest = newArtifact
	result, err := newRegistry.Compose(candidateComposition, &baselineResult.Candidate)
	if err != nil {
		t.Fatalf("compose candidate: %v", err)
	}
	diff := result.Diff
	if diff.BaseDigest != baselineResult.Candidate.Digest() || diff.CandidateDigest != result.Candidate.Digest() ||
		!slices.Equal(diff.RowsChanged, []RowID{"im"}) ||
		!slices.Equal(diff.ConfigChanged, []RowID{"im"}) {
		t.Fatalf("digest/row diff = %#v", diff)
	}
	if len(diff.CapabilitiesAdded) != 1 || diff.CapabilitiesAdded[0].Capability != "im.admin" ||
		len(diff.CapabilitiesRemoved) != 0 ||
		len(diff.EgressAdded) != 1 || len(diff.EgressRemoved) != 1 ||
		len(diff.SecretRefs) != 1 || diff.SecretRefs[0].Kind != SecretRefRetargeted ||
		len(diff.Artifacts) != 1 || len(diff.Schemas) != 1 {
		t.Fatalf("sensitive diff = %#v", diff)
	}
	if diff.SecretRefs[0].BeforeFingerprint == "old-reference" ||
		diff.SecretRefs[0].AfterFingerprint == "new-reference" {
		t.Fatal("secret reference ID leaked instead of being fingerprinted")
	}

	tampered := baselineResult.Candidate
	tampered.digest = testArtifactDigest
	if _, err := newRegistry.Compose(candidateComposition, &tampered); !errors.Is(err, ErrInvalidBaseline) {
		t.Fatalf("tampered baseline error = %v, want %v", err, ErrInvalidBaseline)
	}
}

func compositionRegistry(t *testing.T, registrationOrder []PluginID) *Registry {
	t.Helper()
	manifests := map[PluginID]Manifest{
		"auth.fake.v1": compositionManifest(
			"auth.fake.v1", authSchemaDigest, authArtifactDigest,
			[]PortID{"auth.verify.v1"}, nil, []CapabilityID{"auth.verify"}, nil, nil,
		),
		"im.fake.v1": compositionManifest(
			"im.fake.v1", imSchemaDigest, imArtifactDigest,
			[]PortID{"im.transport.v1"}, nil, []CapabilityID{"im.send"},
			[]string{"https://im.invalid"}, []string{"provider_credential"},
		),
		"runtime.fake.v1": compositionManifest(
			"runtime.fake.v1", runtimeSchemaDigest, runtimeArtifact,
			[]PortID{"runtime.invoke.v1"},
			[]PortRequirement{{Port: "auth.verify.v1"}, {Port: "im.transport.v1"}},
			[]CapabilityID{"runtime.invoke"}, nil, nil,
		),
	}
	if registrationOrder == nil {
		registrationOrder = []PluginID{"auth.fake.v1", "im.fake.v1", "runtime.fake.v1"}
	}
	registry := NewRegistry()
	registerSchema(t, registry, authSchemaDigest, strictSchema(nil, nil))
	registerSchema(t, registry, imSchemaDigest, strictSchema(
		[]string{"mode", "profile_only", "bundle_only", "schema_default"},
		map[string]string{"schema_default": "safe"},
	))
	registerSchema(t, registry, runtimeSchemaDigest, strictSchema(nil, nil))
	for _, pluginID := range registrationOrder {
		manifest := manifests[pluginID]
		packageRecord := admittedPackage(manifest)
		switch pluginID {
		case "auth.fake.v1":
			packageRecord.ArtifactDigest = authArtifactDigest
		case "im.fake.v1":
			packageRecord.ArtifactDigest = imArtifactDigest
		case "runtime.fake.v1":
			packageRecord.ArtifactDigest = runtimeArtifact
		}
		if err := registry.Register(manifest, packageRecord); err != nil {
			t.Fatalf("register %s: %v", pluginID, err)
		}
	}
	return registry
}

func singlePluginRegistry(
	t *testing.T,
	schemaDigest string,
	artifactDigest string,
	capabilities []CapabilityID,
	egress []string,
) *Registry {
	t.Helper()
	registry := NewRegistry()
	registerSchema(t, registry, schemaDigest, strictSchema([]string{"mode"}, nil))
	manifest := compositionManifest(
		"im.fake.v1", schemaDigest, artifactDigest,
		[]PortID{"im.transport.v1"}, nil, capabilities, egress, []string{"provider_credential"},
	)
	packageRecord := admittedPackage(manifest)
	packageRecord.ArtifactDigest = artifactDigest
	if err := registry.Register(manifest, packageRecord); err != nil {
		t.Fatalf("register plugin: %v", err)
	}
	return registry
}

func compositionManifest(
	id PluginID,
	schemaDigest string,
	_ string,
	provides []PortID,
	requires []PortRequirement,
	capabilities []CapabilityID,
	egress []string,
	secretRefNames []string,
) Manifest {
	manifest := testManifest(id, provides, requires)
	manifest.ConfigSchemaDigest = schemaDigest
	manifest.Capabilities = capabilities
	manifest.Egress = egress
	manifest.SecretRefNames = secretRefNames
	return manifest
}

func registerSchema(t *testing.T, registry *Registry, digest string, schema ConfigSchema) {
	t.Helper()
	if err := registry.RegisterConfigSchema(digest, schema); err != nil {
		t.Fatalf("register schema %s: %v", digest, err)
	}
}

func strictSchema(allowedKeys []string, defaults map[string]string) ConfigSchema {
	allowed := make(map[string]struct{}, len(allowedKeys))
	for _, key := range allowedKeys {
		allowed[key] = struct{}{}
	}
	return ConfigSchemaFunc(func(config PluginConfig) (PluginConfig, error) {
		for key := range config.Values {
			if _, exists := allowed[key]; !exists {
				return PluginConfig{}, ErrInvalidPluginConfig
			}
		}
		materialized := cloneConfig(config)
		if materialized.Values == nil {
			materialized.Values = make(map[string]string)
		}
		for key, value := range defaults {
			if _, exists := materialized.Values[key]; !exists {
				materialized.Values[key] = value
			}
		}
		return materialized, nil
	})
}

func baseComposition() Composition {
	return Composition{
		TenantID: "tenant-acme",
		Profile: ConfigurationLayer{
			ID: "profile.base", Revision: 1,
			Rows: []ConfigurationRow{
				compositionRow("auth", "auth.fake.v1", authArtifactDigest, PluginConfig{}),
				compositionRow("im", "im.fake.v1", imArtifactDigest, PluginConfig{
					Values: map[string]string{"mode": "profile", "profile_only": "present"},
					SecretRefs: map[string]SecretReference{
						"provider_credential": {Broker: "test-broker", ReferenceID: "im-initial"},
					},
				}),
				compositionRow("runtime", "runtime.fake.v1", runtimeArtifact, PluginConfig{}),
			},
		},
	}
}

func singlePluginComposition(referenceID string) Composition {
	return Composition{
		TenantID: "tenant-acme",
		Profile: ConfigurationLayer{
			ID: "profile.single", Revision: 1,
			Rows: []ConfigurationRow{compositionRow("im", "im.fake.v1", imArtifactDigest, PluginConfig{
				Values: map[string]string{"mode": referenceID},
				SecretRefs: map[string]SecretReference{
					"provider_credential": {Broker: "test-broker", ReferenceID: referenceID},
				},
			})},
		},
	}
}

func imModeBundle(id string, mode string) ConfigurationLayer {
	return ConfigurationLayer{
		ID: id, Revision: 1,
		Rows: []ConfigurationRow{compositionRow("im", "im.fake.v1", imArtifactDigest, PluginConfig{
			Values: map[string]string{"mode": mode},
			SecretRefs: map[string]SecretReference{
				"provider_credential": {Broker: "test-broker", ReferenceID: "im-" + mode},
			},
		})},
	}
}

func compositionRow(
	rowID RowID,
	pluginID PluginID,
	artifactDigest string,
	config PluginConfig,
) ConfigurationRow {
	return ConfigurationRow{
		RowID: rowID, Operation: RowUpsert, PluginID: pluginID,
		PluginVersion: "1.0.0", ArtifactDigest: artifactDigest, Config: config,
	}
}

func cloneCompositionForTest(value Composition) Composition {
	cloneLayer := func(layer ConfigurationLayer) ConfigurationLayer {
		cloned := ConfigurationLayer{
			ID: layer.ID, Revision: layer.Revision, TenantID: layer.TenantID,
			Rows: make([]ConfigurationRow, 0, len(layer.Rows)),
		}
		for _, row := range layer.Rows {
			row.Config = cloneConfig(row.Config)
			cloned.Rows = append(cloned.Rows, row)
		}
		return cloned
	}
	cloned := Composition{TenantID: value.TenantID, Profile: cloneLayer(value.Profile)}
	for _, bundle := range value.Bundles {
		cloned.Bundles = append(cloned.Bundles, cloneLayer(bundle))
	}
	if value.TenantOverlay != nil {
		overlay := cloneLayer(*value.TenantOverlay)
		cloned.TenantOverlay = &overlay
	}
	return cloned
}
