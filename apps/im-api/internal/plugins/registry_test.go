package plugins

import (
	"errors"
	"slices"
	"testing"
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

func TestRegisterRejectsUntrustedPackageClaims(t *testing.T) {
	t.Parallel()

	manifest := testManifest("im.fake.v1", []PortID{"im.transport.v1"}, nil)
	valid := admittedPackage(manifest)
	testCases := []PackageRecord{
		func() PackageRecord { value := valid; value.PluginID = "other.fake.v1"; return value }(),
		func() PackageRecord { value := valid; value.Version = "9.9.9"; return value }(),
		func() PackageRecord { value := valid; value.ArtifactDigest = "self-asserted"; return value }(),
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

func testManifest(id PluginID, provides []PortID, requires []PortRequirement) Manifest {
	return Manifest{
		ID:                 id,
		Version:            "1.0.0",
		HostAPI:            HostAPIV1,
		Provides:           provides,
		Requires:           requires,
		Capabilities:       []CapabilityID{"runtime.local"},
		Egress:             []string{"none"},
		SecretRefNames:     []string{},
		ConfigSchemaDigest: testSchemaDigest,
	}
}

func admittedPackage(manifest Manifest) PackageRecord {
	return PackageRecord{
		PluginID:       manifest.ID,
		Version:        manifest.Version,
		ArtifactDigest: testArtifactDigest,
		ProvenanceRef:  "builtin://wanwork",
		SBOMRef:        "sbom://test",
		ApprovalRef:    "approval://test",
	}
}
