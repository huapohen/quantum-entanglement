package plugins

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"reflect"
	"regexp"
	"slices"
	"sort"
	"strings"
	"sync"
	"time"
	"unicode/utf8"
)

var (
	ErrInvalidManifest       = errors.New("invalid plugin manifest")
	ErrPackageNotAdmitted    = errors.New("plugin package is not admitted")
	ErrDuplicatePlugin       = errors.New("duplicate plugin ID")
	ErrMissingProvider       = errors.New("required plugin port has no provider")
	ErrAmbiguousProvider     = errors.New("required plugin port has multiple providers")
	ErrInvalidProvider       = errors.New("pinned provider does not provide required port")
	ErrDependencyCycle       = errors.New("plugin dependency cycle")
	ErrMissingFactory        = errors.New("plugin factory is missing")
	ErrDuplicateSchema       = errors.New("duplicate plugin configuration schema")
	ErrInvalidConfigSchema   = errors.New("invalid host-owned plugin configuration schema")
	ErrInvalidSecretBroker   = errors.New("invalid secret reference admission broker")
	ErrDuplicateSecretBroker = errors.New("duplicate secret reference admission broker")
	ErrSecretClaimConflict   = errors.New("secret claim idempotency conflict")
	ErrSecretClaimDenied     = errors.New("secret claim admission denied")
	pluginIDPattern          = regexp.MustCompile(`^[a-z][a-z0-9-]*(?:\.[a-z][a-z0-9-]*)*\.v[1-9][0-9]*$`)
	portIDPattern            = regexp.MustCompile(`^[a-z][a-z0-9-]*(?:\.[a-z][a-z0-9-]*)+\.v[1-9][0-9]*$`)
	capabilityIDPattern      = regexp.MustCompile(`^[a-z][a-z0-9-]*(?:\.[a-z][a-z0-9-]*)+$`)
	secretReferencePattern   = regexp.MustCompile(`^[a-z][a-z0-9_.-]*$`)
	secretMaterialPattern    = regexp.MustCompile(`(?i)(sk-[a-z0-9]|bearer[ :]|api[_-]?key|password|private[_-]?key|secret.*canary|p0_.*canary|-----begin )`)
	semanticVersionPattern   = regexp.MustCompile(`^[0-9]+\.[0-9]+\.[0-9]+$`)
	sha256DigestPattern      = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)
	hmacSHA256Pattern        = regexp.MustCompile(`^hmac-sha256:[0-9a-f]{64}$`)
)

const (
	manifestSchemaVersion     uint32 = 1
	configSchemaVersion       uint32 = 1
	secretBrokerSchemaVersion uint32 = 1
	maxEgressBytes                   = 2048
	maxLifecycleTimeout              = 10 * time.Minute
	maxSchemaFields                  = 128
	maxEnumValues                    = 128
)

type Registry struct {
	entries           map[PluginID]entry
	schemas           map[string]ConfigSchemaDefinition
	secretBrokers     map[string]secretBrokerEntry
	secretBindingKey  [32]byte
	secretClaimMu     sync.Mutex
	secretClaims      map[string]secretClaimRecord
	secretIdempotency map[string]string
}

type secretBrokerEntry struct {
	definition SecretBrokerDefinition
	digest     string
	broker     SecretReferenceAdmissionBroker
}

type secretClaimRecord struct {
	request canonicalSecretClaimRequest
	view    SecretBindingView
	revoked bool
}

type entry struct {
	manifest       Manifest
	manifestDigest string
	packageRecord  PackageRecord
	factory        Factory
}

func NewRegistry() *Registry {
	var key [32]byte
	if _, err := rand.Read(key[:]); err != nil {
		panic("plugins: operating system random source unavailable")
	}
	return newRegistryWithSecretBindingKey(key)
}

func newRegistryWithSecretBindingKey(key [32]byte) *Registry {
	return &Registry{
		entries:           make(map[PluginID]entry),
		schemas:           make(map[string]ConfigSchemaDefinition),
		secretBrokers:     make(map[string]secretBrokerEntry),
		secretBindingKey:  key,
		secretClaims:      make(map[string]secretClaimRecord),
		secretIdempotency: make(map[string]string),
	}
}

func (registry *Registry) RegisterConfigSchema(digest string, definition ConfigSchemaDefinition) error {
	if registry == nil || !sha256DigestPattern.MatchString(digest) {
		return ErrInvalidConfigSchema
	}
	normalized, err := normalizeConfigSchemaDefinition(definition)
	if err != nil {
		return err
	}
	computedDigest, err := digestConfigSchemaDefinition(normalized)
	if err != nil || computedDigest != digest {
		return ErrInvalidConfigSchema
	}
	if _, exists := registry.schemas[digest]; exists {
		return fmt.Errorf("%w: %s", ErrDuplicateSchema, digest)
	}
	registry.schemas[digest] = normalized
	return nil
}

func (registry *Registry) RegisterSecretReferenceBroker(
	definition SecretBrokerDefinition,
	broker SecretReferenceAdmissionBroker,
) (string, error) {
	if registry == nil || isNilInterface(broker) {
		return "", ErrInvalidSecretBroker
	}
	normalized, err := normalizeSecretBrokerDefinition(definition)
	if err != nil {
		return "", err
	}
	digest, err := digestSecretBrokerDefinition(normalized)
	if err != nil {
		return "", ErrInvalidSecretBroker
	}
	if _, exists := registry.secretBrokers[normalized.ID]; exists {
		return "", fmt.Errorf("%w: %s", ErrDuplicateSecretBroker, normalized.ID)
	}
	registry.secretBrokers[normalized.ID] = secretBrokerEntry{
		definition: normalized,
		digest:     digest,
		broker:     broker,
	}
	return digest, nil
}

func isNilInterface(value any) bool {
	if value == nil {
		return true
	}
	reflected := reflect.ValueOf(value)
	switch reflected.Kind() {
	case reflect.Chan, reflect.Func, reflect.Interface, reflect.Map, reflect.Pointer, reflect.Slice:
		return reflected.IsNil()
	default:
		return false
	}
}

func normalizeConfigSchemaDefinition(
	definition ConfigSchemaDefinition,
) (ConfigSchemaDefinition, error) {
	if definition.SchemaVersion != configSchemaVersion ||
		!validConfigurationID(definition.ID) ||
		len(definition.ValueFields)+len(definition.SecretFields) > maxSchemaFields {
		return ConfigSchemaDefinition{}, ErrInvalidConfigSchema
	}
	normalized := ConfigSchemaDefinition{
		SchemaVersion: definition.SchemaVersion,
		ID:            definition.ID,
		ValueFields:   make([]ConfigValueField, 0, len(definition.ValueFields)),
		SecretFields:  make([]ConfigSecretField, 0, len(definition.SecretFields)),
	}
	names := make(map[string]struct{}, len(definition.ValueFields)+len(definition.SecretFields))
	for _, field := range definition.ValueFields {
		if !configKeyPattern.MatchString(field.Name) || sensitiveKeyPattern.MatchString(field.Name) ||
			field.Kind != ConfigValueEnum || len(field.Enum) == 0 || len(field.Enum) > maxEnumValues ||
			(!field.HasDefault && field.Default != "") {
			return ConfigSchemaDefinition{}, ErrInvalidConfigSchema
		}
		if _, exists := names[field.Name]; exists {
			return ConfigSchemaDefinition{}, ErrInvalidConfigSchema
		}
		names[field.Name] = struct{}{}
		field.Enum = slices.Clone(field.Enum)
		if !validUniqueStrings(field.Enum, validCanonicalPublicValue) {
			return ConfigSchemaDefinition{}, ErrInvalidConfigSchema
		}
		slices.Sort(field.Enum)
		if field.HasDefault && !slices.Contains(field.Enum, field.Default) {
			return ConfigSchemaDefinition{}, ErrInvalidConfigSchema
		}
		normalized.ValueFields = append(normalized.ValueFields, field)
	}
	for _, field := range definition.SecretFields {
		if !secretReferencePattern.MatchString(field.Name) || !validConfigurationID(field.Purpose) ||
			!validScopeID(field.Audience) || len(field.AllowedBrokers) == 0 {
			return ConfigSchemaDefinition{}, ErrInvalidConfigSchema
		}
		if _, exists := names[field.Name]; exists {
			return ConfigSchemaDefinition{}, ErrInvalidConfigSchema
		}
		names[field.Name] = struct{}{}
		field.AllowedBrokers = slices.Clone(field.AllowedBrokers)
		if !validUniqueStrings(field.AllowedBrokers, secretBrokerPattern.MatchString) {
			return ConfigSchemaDefinition{}, ErrInvalidConfigSchema
		}
		slices.Sort(field.AllowedBrokers)
		normalized.SecretFields = append(normalized.SecretFields, field)
	}
	sort.Slice(normalized.ValueFields, func(left, right int) bool {
		return normalized.ValueFields[left].Name < normalized.ValueFields[right].Name
	})
	sort.Slice(normalized.SecretFields, func(left, right int) bool {
		return normalized.SecretFields[left].Name < normalized.SecretFields[right].Name
	})
	return normalized, nil
}

func validCanonicalPublicValue(value string) bool {
	if value == "" || len(value) > maxConfigValueBytes || !utf8.ValidString(value) ||
		strings.TrimSpace(value) != value || secretMaterialPattern.MatchString(value) {
		return false
	}
	for _, character := range value {
		if character < 0x21 || character == 0x7f {
			return false
		}
	}
	return true
}

func cloneConfigSchemaDefinition(definition ConfigSchemaDefinition) ConfigSchemaDefinition {
	cloned := definition
	cloned.ValueFields = make([]ConfigValueField, 0, len(definition.ValueFields))
	for _, field := range definition.ValueFields {
		field.Enum = slices.Clone(field.Enum)
		cloned.ValueFields = append(cloned.ValueFields, field)
	}
	cloned.SecretFields = make([]ConfigSecretField, 0, len(definition.SecretFields))
	for _, field := range definition.SecretFields {
		field.AllowedBrokers = slices.Clone(field.AllowedBrokers)
		cloned.SecretFields = append(cloned.SecretFields, field)
	}
	return cloned
}

func normalizeSecretBrokerDefinition(
	definition SecretBrokerDefinition,
) (SecretBrokerDefinition, error) {
	if definition.SchemaVersion != secretBrokerSchemaVersion ||
		!secretBrokerPattern.MatchString(definition.ID) ||
		!semanticVersionPattern.MatchString(definition.Version) ||
		!sha256DigestPattern.MatchString(definition.ImplementationDigest) ||
		definition.PolicyRevision == 0 || len(definition.SupportedPurposes) == 0 {
		return SecretBrokerDefinition{}, ErrInvalidSecretBroker
	}
	normalized := definition
	normalized.SupportedPurposes = slices.Clone(definition.SupportedPurposes)
	if !validUniqueStrings(normalized.SupportedPurposes, validConfigurationID) {
		return SecretBrokerDefinition{}, ErrInvalidSecretBroker
	}
	slices.Sort(normalized.SupportedPurposes)
	return normalized, nil
}

const (
	configSchemaDigestDomain       = "wanwork.im/plugin-config-schema/1\n"
	secretBrokerDigestDomain       = "wanwork.im/secret-broker-definition/1\n"
	secretClaimDigestDomain        = "wanwork.im/secret-claim/1\n"
	secretClaimScopeDomain         = "wanwork.im/secret-claim-scope/1\n"
	secretLocatorBindingDomain     = "wanwork.im/secret-locator-binding/1\n"
	secretBindingFingerprintDomain = "wanwork.im/secret-binding-fingerprint/1\n"
)

type canonicalConfigSchemaDefinition struct {
	SchemaVersion uint32                       `json:"schemaVersion"`
	ID            string                       `json:"id"`
	ValueFields   []canonicalConfigValueField  `json:"valueFields"`
	SecretFields  []canonicalConfigSecretField `json:"secretFields"`
}

type canonicalConfigValueField struct {
	Name       string          `json:"name"`
	Kind       ConfigValueKind `json:"kind"`
	Required   bool            `json:"required"`
	HasDefault bool            `json:"hasDefault"`
	Default    string          `json:"default"`
	Enum       []string        `json:"enum"`
}

type canonicalConfigSecretField struct {
	Name           string   `json:"name"`
	Required       bool     `json:"required"`
	Purpose        string   `json:"purpose"`
	Audience       string   `json:"audience"`
	AllowedBrokers []string `json:"allowedBrokers"`
}

func digestConfigSchemaDefinition(definition ConfigSchemaDefinition) (string, error) {
	canonical, err := canonicalConfigSchemaDefinitionBytes(definition)
	if err != nil {
		return "", err
	}
	return digestBytes(configSchemaDigestDomain, canonical), nil
}

func canonicalConfigSchemaDefinitionBytes(definition ConfigSchemaDefinition) ([]byte, error) {
	values := make([]canonicalConfigValueField, 0, len(definition.ValueFields))
	for _, field := range definition.ValueFields {
		values = append(values, canonicalConfigValueField{
			Name: field.Name, Kind: field.Kind, Required: field.Required,
			HasDefault: field.HasDefault, Default: field.Default,
			Enum: nonNilStringSlice(field.Enum),
		})
	}
	secrets := make([]canonicalConfigSecretField, 0, len(definition.SecretFields))
	for _, field := range definition.SecretFields {
		secrets = append(secrets, canonicalConfigSecretField{
			Name: field.Name, Required: field.Required, Purpose: field.Purpose,
			Audience: field.Audience, AllowedBrokers: nonNilStringSlice(field.AllowedBrokers),
		})
	}
	return marshalCanonical(canonicalConfigSchemaDefinition{
		SchemaVersion: definition.SchemaVersion,
		ID:            definition.ID,
		ValueFields:   values,
		SecretFields:  secrets,
	})
}

type canonicalSecretBrokerDefinition struct {
	SchemaVersion        uint32   `json:"schemaVersion"`
	ID                   string   `json:"id"`
	Version              string   `json:"version"`
	ImplementationDigest string   `json:"implementationDigest"`
	PolicyRevision       uint64   `json:"policyRevision"`
	SupportedPurposes    []string `json:"supportedPurposes"`
}

func digestSecretBrokerDefinition(definition SecretBrokerDefinition) (string, error) {
	canonical, err := canonicalSecretBrokerDefinitionBytes(definition)
	if err != nil {
		return "", err
	}
	return digestBytes(secretBrokerDigestDomain, canonical), nil
}

func canonicalSecretBrokerDefinitionBytes(definition SecretBrokerDefinition) ([]byte, error) {
	return marshalCanonical(canonicalSecretBrokerDefinition{
		SchemaVersion:        definition.SchemaVersion,
		ID:                   definition.ID,
		Version:              definition.Version,
		ImplementationDigest: definition.ImplementationDigest,
		PolicyRevision:       definition.PolicyRevision,
		SupportedPurposes:    nonNilStringSlice(definition.SupportedPurposes),
	})
}

type canonicalSecretClaimRequest struct {
	TenantID               string   `json:"tenantId"`
	RowID                  RowID    `json:"rowId"`
	PluginID               PluginID `json:"pluginId"`
	PluginVersion          string   `json:"pluginVersion"`
	ArtifactDigest         string   `json:"artifactDigest"`
	ManifestDigest         string   `json:"manifestDigest"`
	AdmissionRevision      uint64   `json:"admissionRevision"`
	ConfigSchemaDigest     string   `json:"configSchemaDigest"`
	LogicalName            string   `json:"logicalName"`
	BrokerID               string   `json:"brokerId"`
	BrokerDefinitionDigest string   `json:"brokerDefinitionDigest"`
	BrokerPolicyRevision   uint64   `json:"brokerPolicyRevision"`
	Purpose                string   `json:"purpose"`
	Audience               string   `json:"audience"`
	LocatorBinding         string   `json:"locatorBinding"`
}

func canonicalSecretClaimRequestBytes(request canonicalSecretClaimRequest) ([]byte, error) {
	return marshalCanonical(request)
}

func (registry *Registry) AdmitSecretClaim(
	request SecretClaimRequest,
) (SecretClaimReference, error) {
	if registry == nil || !validConfigurationID(request.IdempotencyKey) ||
		!validScopeID(request.TenantID) || !validConfigurationID(string(request.RowID)) ||
		!validOpaqueReferenceID(request.PresentedReferenceID) {
		return SecretClaimReference{}, ErrSecretClaimDenied
	}
	registered, exists := registry.entries[request.PluginID]
	if !exists || request.PluginVersion != registered.manifest.Version ||
		request.ArtifactDigest != registered.packageRecord.ArtifactDigest ||
		request.ManifestDigest != registered.manifestDigest ||
		request.AdmissionRevision != registered.packageRecord.AdmissionRevision ||
		request.ConfigSchemaDigest != registered.manifest.ConfigSchemaDigest ||
		registered.packageRecord.Revoked {
		return SecretClaimReference{}, ErrSecretClaimDenied
	}
	schema, exists := registry.schemas[request.ConfigSchemaDigest]
	if !exists {
		return SecretClaimReference{}, ErrSecretClaimDenied
	}
	field, exists := findConfigSecretField(schema, request.LogicalName)
	if !exists || request.Purpose != field.Purpose || request.Audience != field.Audience ||
		!slices.Contains(field.AllowedBrokers, request.BrokerID) {
		return SecretClaimReference{}, ErrSecretClaimDenied
	}
	broker, exists := registry.secretBrokers[request.BrokerID]
	if !exists || !slices.Contains(broker.definition.SupportedPurposes, request.Purpose) {
		return SecretClaimReference{}, ErrSecretClaimDenied
	}
	locatorBinding := registry.hmacDigest(
		secretLocatorBindingDomain,
		[]byte(request.PresentedReferenceID),
	)
	canonical := canonicalSecretClaimRequest{
		TenantID: request.TenantID, RowID: request.RowID, PluginID: request.PluginID,
		PluginVersion: request.PluginVersion, ArtifactDigest: request.ArtifactDigest,
		ManifestDigest: request.ManifestDigest, AdmissionRevision: request.AdmissionRevision,
		ConfigSchemaDigest: request.ConfigSchemaDigest, LogicalName: request.LogicalName,
		BrokerID: request.BrokerID, BrokerDefinitionDigest: broker.digest,
		BrokerPolicyRevision: broker.definition.PolicyRevision, Purpose: request.Purpose,
		Audience: request.Audience, LocatorBinding: locatorBinding,
	}
	canonicalBytes, err := canonicalSecretClaimRequestBytes(canonical)
	if err != nil {
		return SecretClaimReference{}, ErrSecretClaimDenied
	}
	requestDigest := digestBytes(secretClaimDigestDomain, canonicalBytes)

	registry.secretClaimMu.Lock()
	defer registry.secretClaimMu.Unlock()
	if previousDigest, exists := registry.secretIdempotency[request.IdempotencyKey]; exists {
		if previousDigest != requestDigest {
			return SecretClaimReference{}, ErrSecretClaimConflict
		}
		previous := registry.secretClaims[previousDigest]
		if previous.revoked {
			return SecretClaimReference{}, ErrSecretClaimDenied
		}
		return SecretClaimReference{
			ClaimDigest: previous.view.ClaimDigest, ClaimRevision: previous.view.ClaimRevision,
		}, nil
	}
	if err := validateSecretReferenceSafely(broker.broker, request); err != nil {
		return SecretClaimReference{}, ErrSecretClaimDenied
	}
	view := SecretBindingView{
		BrokerID:               request.BrokerID,
		BrokerDefinitionDigest: broker.digest,
		ClaimDigest:            requestDigest,
		ClaimRevision:          1,
		BindingFingerprint:     registry.hmacDigest(secretBindingFingerprintDomain, canonicalBytes),
		BrokerPolicyRevision:   broker.definition.PolicyRevision,
		ScopeDigest:            digestBytes(secretClaimScopeDomain, canonicalBytes),
	}
	record := secretClaimRecord{request: canonical, view: view}
	registry.secretClaims[requestDigest] = record
	registry.secretIdempotency[request.IdempotencyKey] = requestDigest
	return SecretClaimReference{ClaimDigest: requestDigest, ClaimRevision: 1}, nil
}

func (registry *Registry) RevokeSecretClaim(reference SecretClaimReference) error {
	if registry == nil || !sha256DigestPattern.MatchString(reference.ClaimDigest) ||
		reference.ClaimRevision == 0 {
		return ErrSecretClaimDenied
	}
	registry.secretClaimMu.Lock()
	defer registry.secretClaimMu.Unlock()
	record, exists := registry.secretClaims[reference.ClaimDigest]
	if !exists || record.view.ClaimRevision != reference.ClaimRevision {
		return ErrSecretClaimDenied
	}
	record.revoked = true
	registry.secretClaims[reference.ClaimDigest] = record
	return nil
}

func findConfigSecretField(
	definition ConfigSchemaDefinition,
	name string,
) (ConfigSecretField, bool) {
	for _, field := range definition.SecretFields {
		if field.Name == name {
			return field, true
		}
	}
	return ConfigSecretField{}, false
}

func validateSecretReferenceSafely(
	broker SecretReferenceAdmissionBroker,
	request SecretClaimRequest,
) (err error) {
	defer func() {
		if recover() != nil {
			err = ErrSecretClaimDenied
		}
	}()
	if err := broker.ValidateReference(request); err != nil {
		return ErrSecretClaimDenied
	}
	return nil
}

func (registry *Registry) hmacDigest(domain string, payload []byte) string {
	hash := hmac.New(sha256.New, registry.secretBindingKey[:])
	_, _ = hash.Write([]byte(domain))
	_, _ = hash.Write(payload)
	return "hmac-sha256:" + hex.EncodeToString(hash.Sum(nil))
}

func (registry *Registry) Register(manifest Manifest, packageRecord PackageRecord) error {
	return registry.register(manifest, packageRecord, nil)
}

func (registry *Registry) RegisterFactory(factory Factory, packageRecord PackageRecord) error {
	if factory == nil {
		return ErrMissingFactory
	}
	return registry.register(factory.Manifest(), packageRecord, factory)
}

func (registry *Registry) register(
	manifest Manifest,
	packageRecord PackageRecord,
	factory Factory,
) error {
	normalized, err := normalizeManifest(manifest)
	if err != nil {
		return err
	}
	manifestDigest, err := digestNormalizedManifest(normalized)
	if err != nil {
		return ErrInvalidManifest
	}
	if err := validatePackageRecord(normalized, manifestDigest, packageRecord); err != nil {
		return err
	}
	if _, exists := registry.entries[normalized.ID]; exists {
		return fmt.Errorf("%w: %s", ErrDuplicatePlugin, normalized.ID)
	}
	registry.entries[normalized.ID] = entry{
		manifest:       normalized,
		manifestDigest: manifestDigest,
		packageRecord:  packageRecord,
		factory:        factory,
	}
	return nil
}

func (registry *Registry) Resolve() (Plan, error) {
	providers := make(map[PortID][]PluginID)
	pluginIDs := make([]PluginID, 0, len(registry.entries))
	for pluginID, registered := range registry.entries {
		pluginIDs = append(pluginIDs, pluginID)
		for _, port := range registered.manifest.Provides {
			providers[port] = append(providers[port], pluginID)
		}
	}
	slices.Sort(pluginIDs)
	for port := range providers {
		slices.Sort(providers[port])
	}

	bindings := make([]PortBinding, 0)
	edges := make(map[PluginID]map[PluginID]struct{}, len(pluginIDs))
	indegree := make(map[PluginID]int, len(pluginIDs))
	for _, pluginID := range pluginIDs {
		edges[pluginID] = make(map[PluginID]struct{})
		indegree[pluginID] = 0
	}

	for _, consumerID := range pluginIDs {
		manifest := registry.entries[consumerID].manifest
		for _, requirement := range manifest.Requires {
			providerID, err := resolveProvider(requirement, providers)
			if err != nil {
				return Plan{}, fmt.Errorf("plugin %s: %w", consumerID, err)
			}
			if providerID == consumerID {
				return Plan{}, fmt.Errorf("plugin %s: %w", consumerID, ErrDependencyCycle)
			}
			bindings = append(bindings, PortBinding{
				ConsumerID: consumerID,
				Port:       requirement.Port,
				ProviderID: providerID,
			})
			if _, exists := edges[providerID][consumerID]; !exists {
				edges[providerID][consumerID] = struct{}{}
				indegree[consumerID]++
			}
		}
	}

	order, err := topologicalOrder(pluginIDs, edges, indegree)
	if err != nil {
		return Plan{}, err
	}
	sort.Slice(bindings, func(left, right int) bool {
		if bindings[left].ConsumerID != bindings[right].ConsumerID {
			return bindings[left].ConsumerID < bindings[right].ConsumerID
		}
		if bindings[left].Port != bindings[right].Port {
			return bindings[left].Port < bindings[right].Port
		}
		return bindings[left].ProviderID < bindings[right].ProviderID
	})
	return Plan{Order: order, Bindings: bindings}, nil
}

// ResolveSelection resolves only the plugins selected by an effective configuration. Merely
// registering an admitted package must never make it active.
func (registry *Registry) ResolveSelection(selected []PluginID) (Plan, error) {
	if registry == nil {
		return Plan{}, ErrInvalidManifest
	}
	selection := NewRegistry()
	seen := make(map[PluginID]struct{}, len(selected))
	for _, pluginID := range selected {
		if _, exists := seen[pluginID]; exists {
			return Plan{}, fmt.Errorf("%w: %s", ErrDuplicatePlugin, pluginID)
		}
		seen[pluginID] = struct{}{}
		registered, exists := registry.entries[pluginID]
		if !exists {
			return Plan{}, fmt.Errorf("%w: %s", ErrUnknownPlugin, pluginID)
		}
		selection.entries[pluginID] = registered
	}
	return selection.Resolve()
}

func normalizeManifest(manifest Manifest) (Manifest, error) {
	if !pluginIDPattern.MatchString(string(manifest.ID)) ||
		!semanticVersionPattern.MatchString(manifest.Version) ||
		manifest.HostAPI != HostAPIV1 ||
		!sha256DigestPattern.MatchString(manifest.ConfigSchemaDigest) ||
		!validTimeouts(manifest.Timeouts) {
		return Manifest{}, ErrInvalidManifest
	}

	normalized := manifest
	normalized.Provides = slices.Clone(manifest.Provides)
	normalized.Requires = slices.Clone(manifest.Requires)
	normalized.Capabilities = slices.Clone(manifest.Capabilities)
	normalized.Egress = slices.Clone(manifest.Egress)
	normalized.SecretRefNames = slices.Clone(manifest.SecretRefNames)

	if !validUniquePorts(normalized.Provides) ||
		!validUniqueRequirements(normalized.Requires) ||
		!validUniqueCapabilities(normalized.Capabilities) ||
		!validUniqueStrings(normalized.Egress, validEgressDeclaration) ||
		!validUniqueStrings(normalized.SecretRefNames, secretReferencePattern.MatchString) {
		return Manifest{}, ErrInvalidManifest
	}

	slices.Sort(normalized.Provides)
	sort.Slice(normalized.Requires, func(left, right int) bool {
		if normalized.Requires[left].Port != normalized.Requires[right].Port {
			return normalized.Requires[left].Port < normalized.Requires[right].Port
		}
		return normalized.Requires[left].ProviderID < normalized.Requires[right].ProviderID
	})
	slices.Sort(normalized.Capabilities)
	slices.Sort(normalized.Egress)
	slices.Sort(normalized.SecretRefNames)
	return normalized, nil
}

func validTimeouts(timeouts LifecycleTimeouts) bool {
	for _, timeout := range []time.Duration{
		timeouts.Start, timeouts.Ready, timeouts.Drain, timeouts.Stop,
	} {
		if timeout <= 0 || timeout > maxLifecycleTimeout || timeout%time.Millisecond != 0 {
			return false
		}
	}
	return true
}

func validEgressDeclaration(value string) bool {
	if value == "" || len(value) > maxEgressBytes || !utf8.ValidString(value) || strings.TrimSpace(value) != value {
		return false
	}
	for _, character := range value {
		if character < 0x21 || character == 0x7f {
			return false
		}
	}
	return true
}

func validatePackageRecord(
	manifest Manifest,
	manifestDigest string,
	packageRecord PackageRecord,
) error {
	if packageRecord.PluginID != manifest.ID ||
		packageRecord.Version != manifest.Version ||
		!sha256DigestPattern.MatchString(packageRecord.ArtifactDigest) ||
		packageRecord.ApprovedManifestDigest != manifestDigest ||
		packageRecord.AdmissionRevision == 0 ||
		packageRecord.ProvenanceRef == "" ||
		packageRecord.SBOMRef == "" ||
		packageRecord.ApprovalRef == "" ||
		packageRecord.Revoked {
		return ErrPackageNotAdmitted
	}
	return nil
}

const manifestDigestDomain = "wanwork.im/plugin-manifest/1\n"

type canonicalManifest struct {
	SchemaVersion      uint32                     `json:"schemaVersion"`
	PluginID           PluginID                   `json:"pluginId"`
	Version            string                     `json:"version"`
	HostAPI            string                     `json:"hostApi"`
	Provides           []PortID                   `json:"provides"`
	Requires           []canonicalPortRequirement `json:"requires"`
	Capabilities       []CapabilityID             `json:"capabilities"`
	Egress             []string                   `json:"egress"`
	SecretRefNames     []string                   `json:"secretRefNames"`
	ConfigSchemaDigest string                     `json:"configSchemaDigest"`
	TimeoutsMS         canonicalLifecycleTimeouts `json:"timeoutsMs"`
}

type canonicalPortRequirement struct {
	Port       PortID   `json:"port"`
	ProviderID PluginID `json:"providerId"`
}

// Lifecycle admission uses bounded whole milliseconds. This keeps the canonical JSON unit
// explicit and every valid value exactly representable by common JSON runtimes.
type canonicalLifecycleTimeouts struct {
	Start uint64 `json:"start"`
	Ready uint64 `json:"ready"`
	Drain uint64 `json:"drain"`
	Stop  uint64 `json:"stop"`
}

func digestNormalizedManifest(manifest Manifest) (string, error) {
	canonical, err := canonicalNormalizedManifestBytes(manifest)
	if err != nil {
		return "", err
	}
	return digestBytes(manifestDigestDomain, canonical), nil
}

func canonicalNormalizedManifestBytes(manifest Manifest) ([]byte, error) {
	requires := make([]canonicalPortRequirement, 0, len(manifest.Requires))
	for _, requirement := range manifest.Requires {
		requires = append(requires, canonicalPortRequirement{
			Port: requirement.Port, ProviderID: requirement.ProviderID,
		})
	}
	return marshalCanonical(canonicalManifest{
		SchemaVersion:      manifestSchemaVersion,
		PluginID:           manifest.ID,
		Version:            manifest.Version,
		HostAPI:            manifest.HostAPI,
		Provides:           nonNilPortSlice(manifest.Provides),
		Requires:           requires,
		Capabilities:       nonNilCapabilitySlice(manifest.Capabilities),
		Egress:             nonNilStringSlice(manifest.Egress),
		SecretRefNames:     nonNilStringSlice(manifest.SecretRefNames),
		ConfigSchemaDigest: manifest.ConfigSchemaDigest,
		TimeoutsMS: canonicalLifecycleTimeouts{
			Start: uint64(manifest.Timeouts.Start / time.Millisecond),
			Ready: uint64(manifest.Timeouts.Ready / time.Millisecond),
			Drain: uint64(manifest.Timeouts.Drain / time.Millisecond),
			Stop:  uint64(manifest.Timeouts.Stop / time.Millisecond),
		},
	})
}

func nonNilPortSlice(values []PortID) []PortID {
	cloned := slices.Clone(values)
	if cloned == nil {
		return []PortID{}
	}
	return cloned
}

func resolveProvider(requirement PortRequirement, providers map[PortID][]PluginID) (PluginID, error) {
	candidates := providers[requirement.Port]
	if requirement.ProviderID != "" {
		if slices.Contains(candidates, requirement.ProviderID) {
			return requirement.ProviderID, nil
		}
		return "", ErrInvalidProvider
	}
	if len(candidates) == 0 {
		return "", ErrMissingProvider
	}
	if len(candidates) > 1 {
		return "", ErrAmbiguousProvider
	}
	return candidates[0], nil
}

func topologicalOrder(
	pluginIDs []PluginID,
	edges map[PluginID]map[PluginID]struct{},
	indegree map[PluginID]int,
) ([]PluginID, error) {
	ready := make([]PluginID, 0)
	for _, pluginID := range pluginIDs {
		if indegree[pluginID] == 0 {
			ready = append(ready, pluginID)
		}
	}

	order := make([]PluginID, 0, len(pluginIDs))
	for len(ready) > 0 {
		pluginID := ready[0]
		ready = ready[1:]
		order = append(order, pluginID)

		consumers := make([]PluginID, 0, len(edges[pluginID]))
		for consumerID := range edges[pluginID] {
			consumers = append(consumers, consumerID)
		}
		slices.Sort(consumers)
		for _, consumerID := range consumers {
			indegree[consumerID]--
			if indegree[consumerID] == 0 {
				ready = append(ready, consumerID)
				slices.Sort(ready)
			}
		}
	}
	if len(order) != len(pluginIDs) {
		return nil, ErrDependencyCycle
	}
	return order, nil
}

func validUniquePorts(values []PortID) bool {
	seen := make(map[PortID]struct{}, len(values))
	for _, value := range values {
		if !portIDPattern.MatchString(string(value)) {
			return false
		}
		if _, exists := seen[value]; exists {
			return false
		}
		seen[value] = struct{}{}
	}
	return true
}

func validUniqueRequirements(values []PortRequirement) bool {
	seen := make(map[PortID]struct{}, len(values))
	for _, value := range values {
		if !portIDPattern.MatchString(string(value.Port)) ||
			(value.ProviderID != "" && !pluginIDPattern.MatchString(string(value.ProviderID))) {
			return false
		}
		if _, exists := seen[value.Port]; exists {
			return false
		}
		seen[value.Port] = struct{}{}
	}
	return true
}

func validUniqueCapabilities(values []CapabilityID) bool {
	seen := make(map[CapabilityID]struct{}, len(values))
	for _, value := range values {
		if !capabilityIDPattern.MatchString(string(value)) {
			return false
		}
		if _, exists := seen[value]; exists {
			return false
		}
		seen[value] = struct{}{}
	}
	return true
}

func validUniqueStrings(values []string, valid func(string) bool) bool {
	seen := make(map[string]struct{}, len(values))
	for _, value := range values {
		if !valid(value) {
			return false
		}
		if _, exists := seen[value]; exists {
			return false
		}
		seen[value] = struct{}{}
	}
	return true
}
