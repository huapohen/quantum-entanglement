package plugins

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"strings"
	"testing"
)

const (
	publicConfigCanary   = "public-config-canary"
	publicDefaultCanary  = "public-default-canary"
	rawLocatorCanary     = "raw-locator-canary"
	secretMaterialCanary = "p0_secret_material_canary"
)

func TestSecretAdmissionKeepsRawLocatorOutsideCompositionAndFactory(t *testing.T) {
	fixture := newSecretAdmissionFixture(t, secretAdmissionSchema(),
		SecretReferenceAdmissionBrokerFunc(func(request SecretClaimRequest) error {
			if !strings.HasPrefix(request.PresentedReferenceID, "issued:") {
				return errors.New("reference was not issued")
			}
			return nil
		}))
	request := testSecretClaimRequest(
		fixture.registry,
		"im",
		fixture.manifest.ID,
		imArtifactDigest,
		rawLocatorCanary,
	)
	reference, err := fixture.registry.AdmitSecretClaim(request)
	if err != nil {
		t.Fatalf("admit secret claim: %v", err)
	}
	result, err := fixture.registry.Compose(secretFixtureComposition(reference), nil)
	if err != nil {
		t.Fatalf("compose: %v", err)
	}

	publicViews := []struct {
		name string
		text string
	}{
		{name: "canonical", text: string(result.Candidate.CanonicalBytes())},
		{name: "rows", text: fmt.Sprintf("%#v", result.Candidate.Rows())},
		{name: "configs", text: fmt.Sprintf("%#v", result.Candidate.PluginConfigs())},
		{name: "diff", text: fmt.Sprintf("%#v", result.Diff)},
		{name: "claim store", text: fmt.Sprintf("%#v", fixture.registry.secretClaims)},
	}
	for _, view := range publicViews {
		if strings.Contains(view.text, rawLocatorCanary) || strings.Contains(view.text, secretMaterialCanary) {
			t.Fatalf("%s leaked a secret canary: %s", view.name, view.text)
		}
	}
	canonical := string(result.Candidate.CanonicalBytes())
	if !strings.Contains(canonical, publicConfigCanary) ||
		!strings.Contains(canonical, publicDefaultCanary) {
		t.Fatalf("canonical configuration omitted declared public values: %s", canonical)
	}

	host, err := NewHost(fixture.registry, result.Candidate)
	if err != nil {
		t.Fatalf("new host: %v", err)
	}
	if err := host.Start(context.Background()); err != nil {
		t.Fatalf("start host: %v", err)
	}
	if err := host.Stop(context.Background()); err != nil {
		t.Fatalf("stop host: %v", err)
	}
	if len(fixture.factory.configs) != 1 {
		t.Fatalf("factory configure calls = %d, want 1", len(fixture.factory.configs))
	}
	factoryText := fmt.Sprintf("%#v", fixture.factory.configs[0])
	if strings.Contains(factoryText, rawLocatorCanary) || strings.Contains(factoryText, secretMaterialCanary) {
		t.Fatalf("factory received secret material: %s", factoryText)
	}
	if fixture.factory.configs[0].Values["mode"] != publicConfigCanary ||
		fixture.factory.configs[0].Values["default_mode"] != publicDefaultCanary {
		t.Fatalf("factory public configuration = %#v", fixture.factory.configs[0].Values)
	}
	binding := fixture.factory.configs[0].SecretBindings["provider_credential"]
	if binding.ClaimDigest != reference.ClaimDigest ||
		!hmacSHA256Pattern.MatchString(binding.BindingFingerprint) ||
		!sha256DigestPattern.MatchString(binding.ScopeDigest) {
		t.Fatalf("factory binding view = %#v", binding)
	}
}

func TestSecretCanariesAreRejectedFromOrdinaryValuesAndSchemaDefaults(t *testing.T) {
	fixture := newSecretAdmissionFixture(t, secretAdmissionSchema(), allowIssuedReferences)
	reference := admitFixtureClaim(t, fixture, rawLocatorCanary)
	composition := secretFixtureComposition(reference)
	composition.Profile.Rows[0].Config.Values["mode"] = secretMaterialCanary
	_, err := fixture.registry.Compose(composition, nil)
	if !errors.Is(err, ErrInvalidPluginConfig) {
		t.Fatalf("ordinary secret canary error = %v, want %v", err, ErrInvalidPluginConfig)
	}
	if strings.Contains(err.Error(), secretMaterialCanary) {
		t.Fatalf("ordinary-value rejection echoed canary: %v", err)
	}

	definition := secretAdmissionSchema()
	definition.ValueFields[1].Default = "p0_schema_default_secret_canary"
	definition.ValueFields[1].Enum = []string{"p0_schema_default_secret_canary"}
	_, err = normalizeConfigSchemaDefinition(definition)
	if !errors.Is(err, ErrInvalidConfigSchema) {
		t.Fatalf("schema secret default error = %v, want %v", err, ErrInvalidConfigSchema)
	}
	if strings.Contains(err.Error(), "p0_schema_default_secret_canary") {
		t.Fatalf("schema rejection echoed canary: %v", err)
	}
}

func TestSecretAdmissionRejectsUnknownBrokerAndForgedLocator(t *testing.T) {
	fixture := newSecretAdmissionFixture(t, secretAdmissionSchema(), allowIssuedReferences)
	unknown := testSecretClaimRequest(
		fixture.registry, "im", fixture.manifest.ID, imArtifactDigest, "unknown-broker",
	)
	delete(fixture.registry.secretBrokers, "test-broker")
	if _, err := fixture.registry.AdmitSecretClaim(unknown); !errors.Is(err, ErrSecretClaimDenied) {
		t.Fatalf("unknown broker error = %v, want %v", err, ErrSecretClaimDenied)
	}

	fixture = newSecretAdmissionFixture(t, secretAdmissionSchema(), allowIssuedReferences)
	forged := testSecretClaimRequest(
		fixture.registry, "im", fixture.manifest.ID, imArtifactDigest, "forged-locator",
	)
	forged.PresentedReferenceID = "forged-locator-canary"
	_, err := fixture.registry.AdmitSecretClaim(forged)
	if !errors.Is(err, ErrSecretClaimDenied) {
		t.Fatalf("forged locator error = %v, want %v", err, ErrSecretClaimDenied)
	}
	if strings.Contains(err.Error(), forged.PresentedReferenceID) {
		t.Fatalf("forged locator was echoed: %v", err)
	}
}

func TestSecretAdmissionRetryIsExactAndIdempotencyConflictFailsClosed(t *testing.T) {
	brokerCalls := 0
	fixture := newSecretAdmissionFixture(t, secretAdmissionSchema(),
		SecretReferenceAdmissionBrokerFunc(func(request SecretClaimRequest) error {
			brokerCalls++
			return allowIssuedReferences.ValidateReference(request)
		}))
	request := testSecretClaimRequest(
		fixture.registry, "im", fixture.manifest.ID, imArtifactDigest, "idempotent",
	)
	first, err := fixture.registry.AdmitSecretClaim(request)
	if err != nil {
		t.Fatalf("first admission: %v", err)
	}
	second, err := fixture.registry.AdmitSecretClaim(request)
	if err != nil {
		t.Fatalf("exact retry: %v", err)
	}
	if first != second || brokerCalls != 1 {
		t.Fatalf("exact retry reference/calls = %#v/%#v/%d", first, second, brokerCalls)
	}

	conflict := request
	conflict.PresentedReferenceID = "issued:different-reference"
	if _, err := fixture.registry.AdmitSecretClaim(conflict); !errors.Is(err, ErrSecretClaimConflict) {
		t.Fatalf("idempotency conflict error = %v, want %v", err, ErrSecretClaimConflict)
	}
	if brokerCalls != 1 {
		t.Fatalf("conflicting retry reached broker; calls = %d", brokerCalls)
	}
}

func TestSecretClaimReferenceCannotReplayAcrossBoundScope(t *testing.T) {
	fixture := newSecretAdmissionFixture(t, secretAdmissionSchema(), allowIssuedReferences)
	reference := admitFixtureClaim(t, fixture, "bound-scope")
	registered := fixture.registry.entries[fixture.manifest.ID]
	field, _ := findConfigSecretField(fixture.schema, "provider_credential")
	row := secretFixtureComposition(reference).Profile.Rows[0]

	if _, err := fixture.registry.resolveSecretClaim(
		"tenant-acme", registered, row, field, reference,
	); err != nil {
		t.Fatalf("exact-bound control failed: %v", err)
	}

	testCases := []struct {
		name       string
		tenantID   string
		registered entry
		row        ConfigurationRow
		field      ConfigSecretField
	}{
		{name: "tenant", tenantID: "tenant-other", registered: registered, row: row, field: field},
		{name: "row", tenantID: "tenant-acme", registered: registered, row: func() ConfigurationRow {
			changed := row
			changed.RowID = "other-row"
			return changed
		}(), field: field},
		{name: "plugin", tenantID: "tenant-acme", registered: registered, row: func() ConfigurationRow {
			changed := row
			changed.PluginID = "other.fake.v1"
			return changed
		}(), field: field},
		{name: "logical name", tenantID: "tenant-acme", registered: registered, row: row, field: func() ConfigSecretField {
			changed := field
			changed.Name = "other_credential"
			return changed
		}()},
	}
	for _, testCase := range testCases {
		t.Run(testCase.name, func(t *testing.T) {
			_, err := fixture.registry.resolveSecretClaim(
				testCase.tenantID,
				testCase.registered,
				testCase.row,
				testCase.field,
				reference,
			)
			if !errors.Is(err, ErrSecretClaimDenied) {
				t.Fatalf("replay error = %v, want %v", err, ErrSecretClaimDenied)
			}
		})
	}
}

func TestSecretClaimRejectsTrustAndPolicyDrift(t *testing.T) {
	fixture := newSecretAdmissionFixture(t, secretAdmissionSchema(), allowIssuedReferences)
	reference := admitFixtureClaim(t, fixture, "drift")
	registered := fixture.registry.entries[fixture.manifest.ID]
	field, _ := findConfigSecretField(fixture.schema, "provider_credential")
	row := secretFixtureComposition(reference).Profile.Rows[0]

	testCases := []struct {
		name       string
		registered entry
		field      ConfigSecretField
	}{
		{name: "manifest", registered: func() entry {
			changed := registered
			changed.manifestDigest = testArtifactDigest
			return changed
		}(), field: field},
		{name: "admission", registered: func() entry {
			changed := registered
			changed.packageRecord.AdmissionRevision++
			return changed
		}(), field: field},
		{name: "schema", registered: func() entry {
			changed := registered
			changed.manifest.ConfigSchemaDigest = testArtifactDigest
			return changed
		}(), field: field},
		{name: "purpose", registered: registered, field: func() ConfigSecretField {
			changed := field
			changed.Purpose = "other-purpose"
			return changed
		}()},
		{name: "audience", registered: registered, field: func() ConfigSecretField {
			changed := field
			changed.Audience = "other.fake.v1"
			return changed
		}()},
	}
	for _, testCase := range testCases {
		t.Run(testCase.name, func(t *testing.T) {
			_, err := fixture.registry.resolveSecretClaim(
				"tenant-acme", testCase.registered, row, testCase.field, reference,
			)
			if !errors.Is(err, ErrSecretClaimDenied) {
				t.Fatalf("drift error = %v, want %v", err, ErrSecretClaimDenied)
			}
		})
	}

	broker := fixture.registry.secretBrokers["test-broker"]
	broker.definition.PolicyRevision++
	fixture.registry.secretBrokers["test-broker"] = broker
	if _, err := fixture.registry.resolveSecretClaim(
		"tenant-acme", registered, row, field, reference,
	); !errors.Is(err, ErrSecretClaimDenied) {
		t.Fatalf("broker policy drift error = %v, want %v", err, ErrSecretClaimDenied)
	}
}

func TestSecretBrokerErrorAndPanicNeverLeakBackendMaterial(t *testing.T) {
	testCases := []struct {
		name   string
		broker SecretReferenceAdmissionBroker
	}{
		{name: "error", broker: SecretReferenceAdmissionBrokerFunc(func(SecretClaimRequest) error {
			return fmt.Errorf("vault backend exposed %s", secretMaterialCanary)
		})},
		{name: "panic", broker: SecretReferenceAdmissionBrokerFunc(func(SecretClaimRequest) error {
			panic("vault panic exposed " + secretMaterialCanary)
		})},
	}
	for _, testCase := range testCases {
		t.Run(testCase.name, func(t *testing.T) {
			fixture := newSecretAdmissionFixture(t, secretAdmissionSchema(), testCase.broker)
			request := testSecretClaimRequest(
				fixture.registry, "im", fixture.manifest.ID, imArtifactDigest, rawLocatorCanary,
			)
			_, err := fixture.registry.AdmitSecretClaim(request)
			if !errors.Is(err, ErrSecretClaimDenied) {
				t.Fatalf("broker failure error = %v, want %v", err, ErrSecretClaimDenied)
			}
			if strings.Contains(err.Error(), rawLocatorCanary) ||
				strings.Contains(err.Error(), secretMaterialCanary) {
				t.Fatalf("broker failure leaked secret material: %v", err)
			}
		})
	}
}

func TestForgedAndRevokedSecretClaimsAreRejectedByComposeAndActivation(t *testing.T) {
	t.Run("forged reference", func(t *testing.T) {
		fixture := newSecretAdmissionFixture(t, secretAdmissionSchema(), allowIssuedReferences)
		valid := admitFixtureClaim(t, fixture, "valid")
		for _, reference := range []SecretClaimReference{
			{ClaimDigest: testArtifactDigest, ClaimRevision: 1},
			{ClaimDigest: valid.ClaimDigest, ClaimRevision: valid.ClaimRevision + 1},
		} {
			_, err := fixture.registry.Compose(secretFixtureComposition(reference), nil)
			if !errors.Is(err, ErrSecretClaimDenied) {
				t.Fatalf("forged reference %#v error = %v, want %v", reference, err, ErrSecretClaimDenied)
			}
		}
	})

	t.Run("revoked before compose", func(t *testing.T) {
		fixture := newSecretAdmissionFixture(t, secretAdmissionSchema(), allowIssuedReferences)
		reference := admitFixtureClaim(t, fixture, "revoke-compose")
		if err := fixture.registry.RevokeSecretClaim(reference); err != nil {
			t.Fatalf("revoke: %v", err)
		}
		_, err := fixture.registry.Compose(secretFixtureComposition(reference), nil)
		if !errors.Is(err, ErrSecretClaimDenied) {
			t.Fatalf("compose revoked claim error = %v, want %v", err, ErrSecretClaimDenied)
		}
	})

	t.Run("revoked after compose", func(t *testing.T) {
		fixture := newSecretAdmissionFixture(t, secretAdmissionSchema(), allowIssuedReferences)
		reference := admitFixtureClaim(t, fixture, "revoke-activation")
		result, err := fixture.registry.Compose(secretFixtureComposition(reference), nil)
		if err != nil {
			t.Fatalf("compose: %v", err)
		}
		if err := fixture.registry.RevokeSecretClaim(reference); err != nil {
			t.Fatalf("revoke: %v", err)
		}
		if _, err := NewHost(fixture.registry, result.Candidate); !errors.Is(err, ErrInvalidActivation) {
			t.Fatalf("new host revoked claim error = %v, want %v", err, ErrInvalidActivation)
		}
	})
}

func TestRegisterSecretBrokerRejectsTypedNil(t *testing.T) {
	var broker *typedNilSecretBroker
	if _, err := newTestRegistry().RegisterSecretReferenceBroker(
		testSecretBrokerDefinition(), broker,
	); !errors.Is(err, ErrInvalidSecretBroker) {
		t.Fatalf("typed-nil broker error = %v, want %v", err, ErrInvalidSecretBroker)
	}
}

func TestSecretAdmissionCanonicalGoldenVectors(t *testing.T) {
	fixture := newSecretAdmissionFixture(t, secretAdmissionSchema(), allowIssuedReferences)
	request := testSecretClaimRequest(
		fixture.registry, "im", fixture.manifest.ID, imArtifactDigest, "golden-locator",
	)
	reference, err := fixture.registry.AdmitSecretClaim(request)
	if err != nil {
		t.Fatalf("admit golden claim: %v", err)
	}
	record := fixture.registry.secretClaims[reference.ClaimDigest]

	schemaCanonical, err := canonicalConfigSchemaDefinitionBytes(fixture.schema)
	if err != nil {
		t.Fatalf("canonical schema: %v", err)
	}
	brokerDefinition := testSecretBrokerDefinition()
	brokerCanonical, err := canonicalSecretBrokerDefinitionBytes(brokerDefinition)
	if err != nil {
		t.Fatalf("canonical broker: %v", err)
	}
	claimCanonical, err := canonicalSecretClaimRequestBytes(record.request)
	if err != nil {
		t.Fatalf("canonical claim: %v", err)
	}

	assertGoldenBytes(t, "testdata/config_schema_v1.golden.json", schemaCanonical)
	assertGoldenBytes(t, "testdata/secret_broker_definition_v1.golden.json", brokerCanonical)
	assertGoldenBytes(t, "testdata/secret_claim_v1.golden.json", claimCanonical)

	schemaDigest, _ := digestConfigSchemaDefinition(fixture.schema)
	brokerDigest, _ := digestSecretBrokerDefinition(brokerDefinition)
	const wantSchemaDigest = "sha256:f7b4dab60180aa172c3d413a2e46a18f6e4919559fa1aca5d20f5c8056caec7c"
	const wantBrokerDigest = "sha256:a1493b2752af1c6240ac045d02ee584f08b41fa52287c2b858282194e6caf6f5"
	const wantClaimDigest = "sha256:8c55cd2021fb0b551b646516be1d6078fec89148b2dd9361276b96a5ccbcaa47"
	if schemaDigest != wantSchemaDigest {
		t.Errorf("schema digest = %s", schemaDigest)
	}
	if brokerDigest != wantBrokerDigest {
		t.Errorf("broker digest = %s", brokerDigest)
	}
	if reference.ClaimDigest != wantClaimDigest {
		t.Errorf("claim digest = %s", reference.ClaimDigest)
	}
	if reference.ClaimDigest != digestBytes(secretClaimDigestDomain, claimCanonical) {
		t.Fatal("claim reference is not the domain-separated canonical claim digest")
	}
	if bytes.Contains(claimCanonical, []byte(request.PresentedReferenceID)) {
		t.Fatal("raw locator entered canonical claim vector")
	}
}

type secretAdmissionFixture struct {
	registry *Registry
	manifest Manifest
	schema   ConfigSchemaDefinition
	factory  *capturingSecretFactory
}

func newSecretAdmissionFixture(
	t *testing.T,
	schema ConfigSchemaDefinition,
	broker SecretReferenceAdmissionBroker,
) secretAdmissionFixture {
	t.Helper()
	registry := newTestRegistry()
	normalizedSchema, err := normalizeConfigSchemaDefinition(schema)
	if err != nil {
		t.Fatalf("normalize fixture schema: %v", err)
	}
	schemaDigest, err := digestConfigSchemaDefinition(normalizedSchema)
	if err != nil {
		t.Fatalf("digest fixture schema: %v", err)
	}
	if err := registry.RegisterConfigSchema(schemaDigest, normalizedSchema); err != nil {
		t.Fatalf("register fixture schema: %v", err)
	}
	manifest := testManifest("im.fake.v1", []PortID{"im.transport.v1"}, nil)
	manifest.ConfigSchemaDigest = schemaDigest
	manifest.SecretRefNames = []string{"provider_credential"}
	factory := &capturingSecretFactory{manifest: manifest}
	packageRecord := admittedPackage(manifest)
	packageRecord.ArtifactDigest = imArtifactDigest
	if err := registry.RegisterFactory(factory, packageRecord); err != nil {
		t.Fatalf("register fixture factory: %v", err)
	}
	if _, err := registry.RegisterSecretReferenceBroker(testSecretBrokerDefinition(), broker); err != nil {
		t.Fatalf("register fixture broker: %v", err)
	}
	freezeRegistryForTest(t, registry)
	return secretAdmissionFixture{
		registry: registry,
		manifest: manifest,
		schema:   normalizedSchema,
		factory:  factory,
	}
}

func secretAdmissionSchema() ConfigSchemaDefinition {
	return ConfigSchemaDefinition{
		SchemaVersion: configSchemaVersion,
		ID:            "im.secret.config.v1",
		ValueFields: []ConfigValueField{
			{Name: "mode", Kind: ConfigValueEnum, Required: true, Enum: []string{publicConfigCanary}},
			{Name: "default_mode", Kind: ConfigValueEnum, HasDefault: true,
				Default: publicDefaultCanary, Enum: []string{publicDefaultCanary}},
		},
		SecretFields: []ConfigSecretField{{
			Name: "provider_credential", Required: true, Purpose: "provider-auth",
			Audience: "im.fake.v1", AllowedBrokers: []string{"test-broker"},
		}},
	}
}

var allowIssuedReferences = SecretReferenceAdmissionBrokerFunc(func(request SecretClaimRequest) error {
	if !strings.HasPrefix(request.PresentedReferenceID, "issued:") {
		return errors.New("reference was not issued")
	}
	return nil
})

func admitFixtureClaim(
	t *testing.T,
	fixture secretAdmissionFixture,
	referenceID string,
) SecretClaimReference {
	t.Helper()
	request := testSecretClaimRequest(
		fixture.registry, "im", fixture.manifest.ID, imArtifactDigest, referenceID,
	)
	reference, err := fixture.registry.AdmitSecretClaim(request)
	if err != nil {
		t.Fatalf("admit fixture claim: %v", err)
	}
	return reference
}

func secretFixtureComposition(reference SecretClaimReference) Composition {
	return Composition{
		TenantID: "tenant-acme",
		Profile: ConfigurationLayer{
			ID: "profile.secret", Revision: 1,
			Rows: []ConfigurationRow{{
				RowID: "im", Operation: RowUpsert, PluginID: "im.fake.v1",
				PluginVersion: "1.0.0", ArtifactDigest: imArtifactDigest,
				Config: ConfigurationInput{
					Values:       map[string]string{"mode": publicConfigCanary},
					SecretClaims: map[string]SecretClaimReference{"provider_credential": reference},
				},
			}},
		},
	}
}

func assertGoldenBytes(t *testing.T, path string, got []byte) {
	t.Helper()
	want, err := os.ReadFile(path)
	if err != nil {
		t.Errorf("read golden %s: %v; got %s", path, err, got)
		return
	}
	want = bytes.TrimSuffix(want, []byte("\n"))
	if !bytes.Equal(got, want) {
		t.Errorf("golden %s mismatch; got %s", path, got)
	}
}

type capturingSecretFactory struct {
	manifest Manifest
	configs  []PluginConfig
}

func (factory *capturingSecretFactory) Manifest() Manifest {
	return factory.manifest
}

func (factory *capturingSecretFactory) Configure(config PluginConfig) (Instance, error) {
	factory.configs = append(factory.configs, cloneConfig(config))
	return noOpSecretInstance{}, nil
}

type noOpSecretInstance struct{}

func (noOpSecretInstance) Start(context.Context, Effects) error { return nil }
func (noOpSecretInstance) Ready(context.Context) error          { return nil }
func (noOpSecretInstance) Drain(context.Context) error          { return nil }
func (noOpSecretInstance) Stop(context.Context) error           { return nil }

type typedNilSecretBroker struct{}

func (*typedNilSecretBroker) ValidateReference(SecretClaimRequest) error { return nil }
