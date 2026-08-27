package plugins

import (
	"bytes"
	"errors"
	"os"
	"slices"
	"testing"
	"time"
)

const (
	testSchemaDigest   = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	testArtifactDigest = "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
)

func TestResolveIsDeterministicAcrossRegistrationOrder(t *testing.T) {
	t.Parallel()

	manifests := []Manifest{
		testManifest("auth.fake.v1", []PortID{"auth.verify.v1"}, nil),
		testManifest("im.fake.v1", []PortID{"im.transport.v1"}, nil),
		testManifest("runtime.fake.v1", []PortID{"runtime.invoke.v1"}, []PortRequirement{
			{Port: "auth.verify.v1"},
			{Port: "im.transport.v1"},
		}),
	}

	var reference Plan
	for index, order := range [][]int{{0, 1, 2}, {2, 0, 1}, {1, 2, 0}} {
		registry := NewRegistry()
		for _, manifestIndex := range order {
			manifest := manifests[manifestIndex]
			if err := registry.Register(manifest, admittedPackage(manifest)); err != nil {
				t.Fatalf("register %s: %v", manifest.ID, err)
			}
		}
		plan, err := registry.Resolve()
		if err != nil {
			t.Fatalf("resolve plan: %v", err)
		}
		if index == 0 {
			reference = plan
			continue
		}
		if !slices.Equal(plan.Order, reference.Order) || !slices.Equal(plan.Bindings, reference.Bindings) {
			t.Fatalf("plan = %#v, want %#v", plan, reference)
		}
	}

	wantOrder := []PluginID{"auth.fake.v1", "im.fake.v1", "runtime.fake.v1"}
	if !slices.Equal(reference.Order, wantOrder) {
		t.Fatalf("order = %v, want %v", reference.Order, wantOrder)
	}
}

func TestResolveRejectsMissingAmbiguousAndInvalidProviders(t *testing.T) {
	t.Parallel()

	testCases := []struct {
		name      string
		manifests []Manifest
		wantError error
	}{
		{
			name: "missing",
			manifests: []Manifest{
				testManifest("consumer.fake.v1", nil, []PortRequirement{{Port: "im.transport.v1"}}),
			},
			wantError: ErrMissingProvider,
		},
		{
			name: "ambiguous",
			manifests: []Manifest{
				testManifest("im.first.v1", []PortID{"im.transport.v1"}, nil),
				testManifest("im.second.v1", []PortID{"im.transport.v1"}, nil),
				testManifest("consumer.fake.v1", nil, []PortRequirement{{Port: "im.transport.v1"}}),
			},
			wantError: ErrAmbiguousProvider,
		},
		{
			name: "invalid pinned provider",
			manifests: []Manifest{
				testManifest("im.fake.v1", []PortID{"im.transport.v1"}, nil),
				testManifest("consumer.fake.v1", nil, []PortRequirement{{
					Port:       "im.transport.v1",
					ProviderID: "missing.fake.v1",
				}}),
			},
			wantError: ErrInvalidProvider,
		},
	}

	for _, testCase := range testCases {
		t.Run(testCase.name, func(t *testing.T) {
			t.Parallel()

			registry := NewRegistry()
			for _, manifest := range testCase.manifests {
				if err := registry.Register(manifest, admittedPackage(manifest)); err != nil {
					t.Fatalf("register %s: %v", manifest.ID, err)
				}
			}
			_, err := registry.Resolve()
			if !errors.Is(err, testCase.wantError) {
				t.Fatalf("resolve error = %v, want %v", err, testCase.wantError)
			}
		})
	}
}

func TestResolveRejectsDependencyCycle(t *testing.T) {
	t.Parallel()

	first := testManifest("first.fake.v1", []PortID{"first.port.v1"}, []PortRequirement{{Port: "second.port.v1"}})
	second := testManifest("second.fake.v1", []PortID{"second.port.v1"}, []PortRequirement{{Port: "first.port.v1"}})
	registry := NewRegistry()
	for _, manifest := range []Manifest{first, second} {
		if err := registry.Register(manifest, admittedPackage(manifest)); err != nil {
			t.Fatalf("register %s: %v", manifest.ID, err)
		}
	}
	if _, err := registry.Resolve(); !errors.Is(err, ErrDependencyCycle) {
		t.Fatalf("resolve error = %v, want %v", err, ErrDependencyCycle)
	}
}

func TestRegisterRejectsInvalidManifestAndDuplicatePlugin(t *testing.T) {
	t.Parallel()

	valid := testManifest("im.fake.v1", []PortID{"im.transport.v1"}, nil)
	invalid := valid
	invalid.Provides = []PortID{"im.transport.v1", "im.transport.v1"}
	if err := NewRegistry().Register(invalid, admittedPackage(invalid)); !errors.Is(err, ErrInvalidManifest) {
		t.Fatalf("invalid manifest error = %v, want %v", err, ErrInvalidManifest)
	}

	registry := NewRegistry()
	if err := registry.Register(valid, admittedPackage(valid)); err != nil {
		t.Fatalf("register valid manifest: %v", err)
	}
	if err := registry.Register(valid, admittedPackage(valid)); !errors.Is(err, ErrDuplicatePlugin) {
		t.Fatalf("duplicate error = %v, want %v", err, ErrDuplicatePlugin)
	}
}

func TestManifestDigestIsCanonicalCompleteAndDomainSeparated(t *testing.T) {
	t.Parallel()

	manifest := testManifest(
		"runtime.fake.v1",
		[]PortID{"runtime.write.v1", "runtime.read.v1"},
		[]PortRequirement{
			{Port: "auth.verify.v1", ProviderID: "auth.fake.v1"},
			{Port: "im.transport.v1"},
		},
	)
	manifest.Capabilities = []CapabilityID{"runtime.write", "runtime.read"}
	manifest.Egress = []string{"https://write.invalid", "https://read.invalid"}
	manifest.SecretRefNames = []string{"write_credential", "read_credential"}
	normalized, err := normalizeManifest(manifest)
	if err != nil {
		t.Fatalf("normalize manifest: %v", err)
	}
	canonical, err := canonicalNormalizedManifestBytes(normalized)
	if err != nil {
		t.Fatalf("canonical manifest: %v", err)
	}
	wantCanonical, err := os.ReadFile("testdata/plugin_manifest_v1.golden.json")
	if err != nil {
		t.Fatalf("read manifest golden: %v", err)
	}
	wantCanonical = bytes.TrimSuffix(wantCanonical, []byte("\n"))
	if !bytes.Equal(canonical, wantCanonical) {
		t.Fatalf("canonical manifest:\n%s", canonical)
	}
	digest, err := digestNormalizedManifest(normalized)
	if err != nil {
		t.Fatalf("digest manifest: %v", err)
	}
	const wantDigest = "sha256:da176c9aa0cf176a6fe4aa36e14e35e627fe7e0b79f6ac4ac275fef3be010eff"
	if digest != wantDigest {
		t.Fatalf("manifest digest = %s", digest)
	}
	if digest == digestBytes(effectiveDigestDomain, canonical) ||
		digest == digestBytes(layerDigestDomain, canonical) {
		t.Fatal("manifest digest domain was reused")
	}

	reordered := manifest
	reordered.Provides = slices.Clone(manifest.Provides)
	reordered.Requires = slices.Clone(manifest.Requires)
	reordered.Capabilities = slices.Clone(manifest.Capabilities)
	reordered.Egress = slices.Clone(manifest.Egress)
	reordered.SecretRefNames = slices.Clone(manifest.SecretRefNames)
	slices.Reverse(reordered.Provides)
	slices.Reverse(reordered.Requires)
	slices.Reverse(reordered.Capabilities)
	slices.Reverse(reordered.Egress)
	slices.Reverse(reordered.SecretRefNames)
	reorderedNormalized, err := normalizeManifest(reordered)
	if err != nil {
		t.Fatalf("normalize reordered manifest: %v", err)
	}
	reorderedDigest, _ := digestNormalizedManifest(reorderedNormalized)
	if reorderedDigest != digest {
		t.Fatalf("reordered digest = %s, want %s", reorderedDigest, digest)
	}
	emptyCollections := testManifest("empty.fake.v1", nil, nil)
	emptyCollections.Capabilities = nil
	emptyCollections.Egress = nil
	emptyCollections.SecretRefNames = nil
	nilNormalized, err := normalizeManifest(emptyCollections)
	if err != nil {
		t.Fatalf("normalize nil collections: %v", err)
	}
	nilDigest, _ := digestNormalizedManifest(nilNormalized)
	emptyCollections.Provides = []PortID{}
	emptyCollections.Requires = []PortRequirement{}
	emptyCollections.Capabilities = []CapabilityID{}
	emptyCollections.Egress = []string{}
	emptyCollections.SecretRefNames = []string{}
	emptyNormalized, err := normalizeManifest(emptyCollections)
	if err != nil {
		t.Fatalf("normalize empty collections: %v", err)
	}
	emptyDigest, _ := digestNormalizedManifest(emptyNormalized)
	if nilDigest != emptyDigest {
		t.Fatalf("nil/empty digest = %s/%s", nilDigest, emptyDigest)
	}

	mutations := map[string]func(*Manifest){
		"plugin ID":     func(value *Manifest) { value.ID = "runtime.other.v1" },
		"version":       func(value *Manifest) { value.Version = "1.0.1" },
		"host API":      func(value *Manifest) { value.HostAPI = "wanwork.plugin-host/v2" },
		"provides":      func(value *Manifest) { value.Provides[0] = "runtime.other.v1" },
		"require port":  func(value *Manifest) { value.Requires[0].Port = "auth.other.v1" },
		"provider pin":  func(value *Manifest) { value.Requires[0].ProviderID = "auth.other.v1" },
		"capability":    func(value *Manifest) { value.Capabilities[0] = "runtime.admin" },
		"egress":        func(value *Manifest) { value.Egress[0] = "https://other.invalid" },
		"secret name":   func(value *Manifest) { value.SecretRefNames[0] = "other_credential" },
		"schema":        func(value *Manifest) { value.ConfigSchemaDigest = testArtifactDigest },
		"start timeout": func(value *Manifest) { value.Timeouts.Start += time.Millisecond },
		"ready timeout": func(value *Manifest) { value.Timeouts.Ready += time.Millisecond },
		"drain timeout": func(value *Manifest) { value.Timeouts.Drain += time.Millisecond },
		"stop timeout":  func(value *Manifest) { value.Timeouts.Stop += time.Millisecond },
	}
	for name, mutate := range mutations {
		name, mutate := name, mutate
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			changed := normalized
			changed.Provides = slices.Clone(normalized.Provides)
			changed.Requires = slices.Clone(normalized.Requires)
			changed.Capabilities = slices.Clone(normalized.Capabilities)
			changed.Egress = slices.Clone(normalized.Egress)
			changed.SecretRefNames = slices.Clone(normalized.SecretRefNames)
			mutate(&changed)
			changedDigest, digestErr := digestNormalizedManifest(changed)
			if digestErr != nil {
				t.Fatalf("digest changed manifest: %v", digestErr)
			}
			if changedDigest == digest {
				t.Fatalf("%s did not change manifest digest", name)
			}
		})
	}
}

func TestRegistryFreezesManifestSlicesAndDigestAtRegistration(t *testing.T) {
	t.Parallel()

	manifest := testManifest("runtime.fake.v1", []PortID{"runtime.invoke.v1"}, nil)
	registry := NewRegistry()
	if err := registry.Register(manifest, admittedPackage(manifest)); err != nil {
		t.Fatalf("register manifest: %v", err)
	}
	registered := registry.entries[manifest.ID]
	wantDigest := registered.manifestDigest
	manifest.Provides[0] = "runtime.changed.v1"
	manifest.Capabilities[0] = "runtime.changed"
	if registered.manifest.Provides[0] != "runtime.invoke.v1" ||
		registered.manifest.Capabilities[0] != "runtime.local" ||
		registered.manifestDigest != wantDigest {
		t.Fatalf("registered manifest changed through caller slices: %#v", registered)
	}
}

func TestManifestRejectsAmbiguousCanonicalInputsAndTimeouts(t *testing.T) {
	t.Parallel()

	valid := testManifest("runtime.fake.v1", []PortID{"runtime.invoke.v1"}, nil)
	testCases := []struct {
		name   string
		mutate func(*Manifest)
	}{
		{name: "invalid UTF-8 egress", mutate: func(value *Manifest) { value.Egress = []string{string([]byte{0xff})} }},
		{name: "control in egress", mutate: func(value *Manifest) { value.Egress = []string{"https://ok.invalid\n"} }},
		{name: "sub-millisecond timeout", mutate: func(value *Manifest) { value.Timeouts.Start = time.Nanosecond }},
		{name: "timeout over limit", mutate: func(value *Manifest) { value.Timeouts.Stop = maxLifecycleTimeout + time.Millisecond }},
	}
	for _, testCase := range testCases {
		testCase := testCase
		t.Run(testCase.name, func(t *testing.T) {
			t.Parallel()
			manifest := valid
			testCase.mutate(&manifest)
			if err := NewRegistry().Register(manifest, admittedPackage(manifest)); !errors.Is(err, ErrInvalidManifest) {
				t.Fatalf("register error = %v, want %v", err, ErrInvalidManifest)
			}
		})
	}
}

func TestRegisterRejectsUntrustedPackageClaims(t *testing.T) {
	t.Parallel()

	manifest := testManifest("im.fake.v1", []PortID{"im.transport.v1"}, nil)
	valid := admittedPackage(manifest)
	testCases := []PackageRecord{
		func() PackageRecord { value := valid; value.PluginID = "other.fake.v1"; return value }(),
		func() PackageRecord { value := valid; value.Version = "9.9.9"; return value }(),
		func() PackageRecord { value := valid; value.ArtifactDigest = "self-asserted"; return value }(),
		func() PackageRecord { value := valid; value.ApprovedManifestDigest = testArtifactDigest; return value }(),
		func() PackageRecord { value := valid; value.AdmissionRevision = 0; return value }(),
		func() PackageRecord { value := valid; value.ProvenanceRef = ""; return value }(),
		func() PackageRecord { value := valid; value.SBOMRef = ""; return value }(),
		func() PackageRecord { value := valid; value.ApprovalRef = ""; return value }(),
		func() PackageRecord { value := valid; value.Revoked = true; return value }(),
	}
	for _, packageRecord := range testCases {
		if err := NewRegistry().Register(manifest, packageRecord); !errors.Is(err, ErrPackageNotAdmitted) {
			t.Fatalf("register package %#v error = %v, want %v", packageRecord, err, ErrPackageNotAdmitted)
		}
	}
}

func TestRegisterConfigSchemaIsHostOwnedAndDigestPinned(t *testing.T) {
	t.Parallel()

	registry := NewRegistry()
	schema := ConfigSchemaFunc(func(config PluginConfig) (PluginConfig, error) { return config, nil })
	if err := registry.RegisterConfigSchema(testSchemaDigest, schema); err != nil {
		t.Fatalf("register schema: %v", err)
	}
	if err := registry.RegisterConfigSchema(testSchemaDigest, schema); !errors.Is(err, ErrDuplicateSchema) {
		t.Fatalf("duplicate schema error = %v, want %v", err, ErrDuplicateSchema)
	}
	if err := NewRegistry().RegisterConfigSchema("not-a-digest", schema); !errors.Is(err, ErrInvalidManifest) {
		t.Fatalf("invalid digest error = %v, want %v", err, ErrInvalidManifest)
	}
	if err := NewRegistry().RegisterConfigSchema(testSchemaDigest, nil); !errors.Is(err, ErrInvalidManifest) {
		t.Fatalf("nil schema error = %v, want %v", err, ErrInvalidManifest)
	}
}

func TestResolveSelectionDoesNotActivateEveryRegisteredPlugin(t *testing.T) {
	t.Parallel()

	registry := NewRegistry()
	for _, manifest := range []Manifest{
		testManifest("auth.fake.v1", []PortID{"auth.verify.v1"}, nil),
		testManifest("im.fake.v1", []PortID{"im.transport.v1"}, nil),
	} {
		if err := registry.Register(manifest, admittedPackage(manifest)); err != nil {
			t.Fatalf("register %s: %v", manifest.ID, err)
		}
	}
	plan, err := registry.ResolveSelection([]PluginID{"im.fake.v1"})
	if err != nil {
		t.Fatalf("resolve selection: %v", err)
	}
	if !slices.Equal(plan.Order, []PluginID{"im.fake.v1"}) {
		t.Fatalf("order = %v", plan.Order)
	}
	if _, err := registry.ResolveSelection([]PluginID{"missing.fake.v1"}); !errors.Is(err, ErrUnknownPlugin) {
		t.Fatalf("unknown selection error = %v, want %v", err, ErrUnknownPlugin)
	}
}

func testManifest(id PluginID, provides []PortID, requires []PortRequirement) Manifest {
	return Manifest{
		ID:                 id,
		Version:            "1.0.0",
		HostAPI:            HostAPIV1,
		Provides:           provides,
		Requires:           requires,
		Capabilities:       []CapabilityID{"runtime.local"},
		Egress:             []string{},
		SecretRefNames:     []string{},
		ConfigSchemaDigest: testSchemaDigest,
		Timeouts: LifecycleTimeouts{
			Start: time.Second,
			Ready: time.Second,
			Drain: time.Second,
			Stop:  time.Second,
		},
	}
}

func admittedPackage(manifest Manifest) PackageRecord {
	approvedManifestDigest := testArtifactDigest
	if normalized, err := normalizeManifest(manifest); err == nil {
		approvedManifestDigest, _ = digestNormalizedManifest(normalized)
	}
	return PackageRecord{
		PluginID:               manifest.ID,
		Version:                manifest.Version,
		ArtifactDigest:         testArtifactDigest,
		ApprovedManifestDigest: approvedManifestDigest,
		AdmissionRevision:      1,
		ProvenanceRef:          "builtin://wanwork",
		SBOMRef:                "sbom://test",
		ApprovalRef:            "approval://test",
	}
}
