package plugins

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"slices"
	"sort"
	"strings"
	"unicode/utf8"
)

var (
	ErrInvalidComposition  = errors.New("invalid plugin configuration composition")
	ErrDuplicateLayer      = errors.New("duplicate configuration layer ID")
	ErrDuplicateRow        = errors.New("duplicate configuration row ID")
	ErrDuplicatePluginRow  = errors.New("plugin has multiple effective rows")
	ErrUnknownPlugin       = errors.New("configuration references an unknown plugin")
	ErrPluginVersionDrift  = errors.New("configured plugin version does not match admitted package")
	ErrArtifactDigestDrift = errors.New("configured artifact digest does not match admitted package")
	ErrMissingConfigSchema = errors.New("host-owned plugin configuration schema is missing")
	ErrInvalidPluginConfig = errors.New("invalid plugin configuration row")
	ErrInvalidBaseline     = errors.New("invalid effective configuration baseline")

	configurationIDPattern = regexp.MustCompile(`^[a-z][a-z0-9_.-]*$`)
	configKeyPattern       = regexp.MustCompile(`^[a-z][a-z0-9_.-]*$`)
	secretBrokerPattern    = regexp.MustCompile(`^[a-z][a-z0-9.-]*$`)
	sensitiveKeyPattern    = regexp.MustCompile(`(?i)(^|[._-])(api[_-]?key|token|password|passwd|secret|credential|private[_-]?key)($|[._-])`)
)

const (
	effectiveConfigurationSchemaVersion uint32 = 3
	configurationLayerSchemaVersion     uint32 = 2
	maxConfigurationLayers                     = 64
	maxConfigurationRows                       = 512
	maxConfigValues                            = 128
	maxSecretReferences                        = 32
	maxConfigValueBytes                        = 4096
	maxReferenceIDBytes                        = 256
	maxScopeIDBytes                            = 128
	maxConfigurationIDBytes                    = 128

	layerDigestDomain     = "wanwork.im/configuration-layer/2\n"
	effectiveDigestDomain = "wanwork.im/effective-configuration/3\n"
)

type RowID string
type LayerKind string
type RowOperation string

const (
	LayerKindProfile       LayerKind = "profile"
	LayerKindBundle        LayerKind = "bundle"
	LayerKindTenantOverlay LayerKind = "tenant_overlay"

	RowUpsert RowOperation = "upsert"
	RowRemove RowOperation = "remove"
)

type ConfigurationLayer struct {
	ID       string
	Revision uint64
	TenantID string
	Rows     []ConfigurationRow
}

type ConfigurationRow struct {
	RowID          RowID
	Operation      RowOperation
	PluginID       PluginID
	PluginVersion  string
	ArtifactDigest string
	Config         ConfigurationInput
}

type Composition struct {
	TenantID      string
	Profile       ConfigurationLayer
	Bundles       []ConfigurationLayer
	TenantOverlay *ConfigurationLayer
}

type EffectiveSource struct {
	Kind     LayerKind
	ID       string
	Revision uint64
	Digest   string
}

type EffectiveRow struct {
	RowID              RowID
	PluginID           PluginID
	PluginVersion      string
	ArtifactDigest     string
	ManifestDigest     string
	AdmissionRevision  uint64
	ConfigSchemaDigest string
	Config             PluginConfig
	Capabilities       []CapabilityID
	Egress             []string
}

// EffectiveConfiguration is an immutable, content-addressed deployment snapshot. Its fields stay
// private so callers cannot mutate slices or maps after the digest has been computed.
type EffectiveConfiguration struct {
	schemaVersion uint32
	tenantID      string
	sources       []EffectiveSource
	rows          []EffectiveRow
	plan          Plan
	canonical     []byte
	digest        string
}

func (configuration EffectiveConfiguration) SchemaVersion() uint32 {
	return configuration.schemaVersion
}

func (configuration EffectiveConfiguration) TenantID() string {
	return configuration.tenantID
}

func (configuration EffectiveConfiguration) Sources() []EffectiveSource {
	return slices.Clone(configuration.sources)
}

func (configuration EffectiveConfiguration) Rows() []EffectiveRow {
	return cloneEffectiveRows(configuration.rows)
}

func (configuration EffectiveConfiguration) Plan() Plan {
	return Plan{
		Order:    slices.Clone(configuration.plan.Order),
		Bindings: slices.Clone(configuration.plan.Bindings),
	}
}

func (configuration EffectiveConfiguration) CanonicalBytes() []byte {
	return slices.Clone(configuration.canonical)
}

func (configuration EffectiveConfiguration) Digest() string {
	return configuration.digest
}

func (configuration EffectiveConfiguration) PluginConfigs() map[PluginID]PluginConfig {
	configs := make(map[PluginID]PluginConfig, len(configuration.rows))
	for _, row := range configuration.rows {
		configs[row.PluginID] = cloneConfig(row.Config)
	}
	return configs
}

type ScopedCapability struct {
	RowID      RowID
	PluginID   PluginID
	Capability CapabilityID
}

type ScopedEgress struct {
	RowID    RowID
	PluginID PluginID
	Egress   string
}

type SecretRefChangeKind string

const (
	SecretRefAdded      SecretRefChangeKind = "added"
	SecretRefRemoved    SecretRefChangeKind = "removed"
	SecretRefRetargeted SecretRefChangeKind = "retargeted"
)

type SecretRefChange struct {
	Kind              SecretRefChangeKind
	RowID             RowID
	PluginID          PluginID
	LogicalName       string
	BeforeFingerprint string
	AfterFingerprint  string
}

type ArtifactChange struct {
	RowID    RowID
	PluginID PluginID
	Before   string
	After    string
}

type SchemaChange struct {
	RowID    RowID
	PluginID PluginID
	Before   string
	After    string
}

type ManifestChange struct {
	RowID    RowID
	PluginID PluginID
	Before   string
	After    string
}

type AdmissionChange struct {
	RowID    RowID
	PluginID PluginID
	Before   uint64
	After    uint64
}

type EffectiveConfigurationDiff struct {
	BaseDigest          string
	CandidateDigest     string
	RowsAdded           []RowID
	RowsRemoved         []RowID
	RowsChanged         []RowID
	ConfigChanged       []RowID
	BindingsAdded       []PortBinding
	BindingsRemoved     []PortBinding
	CapabilitiesAdded   []ScopedCapability
	CapabilitiesRemoved []ScopedCapability
	EgressAdded         []ScopedEgress
	EgressRemoved       []ScopedEgress
	SecretRefs          []SecretRefChange
	Artifacts           []ArtifactChange
	Schemas             []SchemaChange
	Manifests           []ManifestChange
	Admissions          []AdmissionChange
}

type CompositionResult struct {
	Candidate EffectiveConfiguration
	Diff      EffectiveConfigurationDiff
}

// Compose applies a profile, ordered bundles, and an optional tenant overlay. Upserts replace an
// entire row by stable row ID; removal requires an explicit tombstone. There is no deep merge,
// prompt patch, CLI patch, home patch, or ambient-environment layer.
func (registry *Registry) Compose(
	composition Composition,
	baseline *EffectiveConfiguration,
) (CompositionResult, error) {
	if registry == nil || !validScopeID(composition.TenantID) {
		return CompositionResult{}, ErrInvalidComposition
	}
	registry.definitionsMu.RLock()
	defer registry.definitionsMu.RUnlock()
	if !registry.frozen {
		return CompositionResult{}, ErrRegistryNotFrozen
	}
	if baseline != nil {
		if err := validateEffectiveBaseline(*baseline); err != nil || baseline.tenantID != composition.TenantID {
			return CompositionResult{}, ErrInvalidBaseline
		}
	}

	layers := make([]layerWithKind, 0, 2+len(composition.Bundles))
	layers = append(layers, layerWithKind{kind: LayerKindProfile, layer: composition.Profile})
	for _, bundle := range composition.Bundles {
		layers = append(layers, layerWithKind{kind: LayerKindBundle, layer: bundle})
	}
	if composition.TenantOverlay != nil {
		layers = append(layers, layerWithKind{
			kind:  LayerKindTenantOverlay,
			layer: *composition.TenantOverlay,
		})
	}
	if len(layers) > maxConfigurationLayers {
		return CompositionResult{}, ErrInvalidComposition
	}

	seenLayers := make(map[string]struct{}, len(layers))
	rowsByID := make(map[RowID]EffectiveRow)
	sources := make([]EffectiveSource, 0, len(layers))
	for _, selectedLayer := range layers {
		if err := validateLayerScope(composition.TenantID, selectedLayer); err != nil {
			return CompositionResult{}, err
		}
		normalized, materializedRows, err := registry.normalizeLayer(composition.TenantID, selectedLayer.layer)
		if err != nil {
			return CompositionResult{}, fmt.Errorf("layer %s: %w", selectedLayer.layer.ID, err)
		}
		if _, exists := seenLayers[normalized.ID]; exists {
			return CompositionResult{}, fmt.Errorf("%w: %s", ErrDuplicateLayer, normalized.ID)
		}
		seenLayers[normalized.ID] = struct{}{}
		layerDigest, err := digestConfigurationLayer(normalized)
		if err != nil {
			return CompositionResult{}, fmt.Errorf("layer %s: %w", normalized.ID, ErrInvalidComposition)
		}
		sources = append(sources, EffectiveSource{
			Kind:     selectedLayer.kind,
			ID:       normalized.ID,
			Revision: normalized.Revision,
			Digest:   layerDigest,
		})
		for _, row := range normalized.Rows {
			if row.Operation == RowRemove {
				delete(rowsByID, row.RowID)
				continue
			}
			rowsByID[row.RowID] = materializedRows[row.RowID]
		}
		if len(rowsByID) > maxConfigurationRows {
			return CompositionResult{}, ErrInvalidComposition
		}
	}

	rowIDs := make([]RowID, 0, len(rowsByID))
	for rowID := range rowsByID {
		rowIDs = append(rowIDs, rowID)
	}
	slices.Sort(rowIDs)
	rows := make([]EffectiveRow, 0, len(rowIDs))
	pluginRows := make(map[PluginID]RowID, len(rowIDs))
	selectedPlugins := make([]PluginID, 0, len(rowIDs))
	for _, rowID := range rowIDs {
		row := rowsByID[rowID]
		if previousRow, exists := pluginRows[row.PluginID]; exists {
			return CompositionResult{}, fmt.Errorf(
				"%w: plugin %s in rows %s and %s",
				ErrDuplicatePluginRow,
				row.PluginID,
				previousRow,
				row.RowID,
			)
		}
		pluginRows[row.PluginID] = row.RowID
		selectedPlugins = append(selectedPlugins, row.PluginID)
		rows = append(rows, cloneEffectiveRow(row))
	}
	plan, err := registry.resolveSelectionLocked(selectedPlugins)
	if err != nil {
		return CompositionResult{}, fmt.Errorf("resolve effective plugin plan: %w", err)
	}

	candidate, err := newEffectiveConfiguration(composition.TenantID, sources, rows, plan)
	if err != nil {
		return CompositionResult{}, err
	}
	return CompositionResult{
		Candidate: candidate,
		Diff:      diffEffectiveConfigurations(baseline, &candidate),
	}, nil
}

type layerWithKind struct {
	kind  LayerKind
	layer ConfigurationLayer
}

type normalizedConfigurationLayer struct {
	ID       string
	Revision uint64
	TenantID string
	Rows     []normalizedConfigurationRow
}

type normalizedConfigurationRow struct {
	RowID          RowID
	Operation      RowOperation
	PluginID       PluginID
	PluginVersion  string
	ArtifactDigest string
	Config         PluginConfig
}

func validateLayerScope(tenantID string, selected layerWithKind) error {
	switch selected.kind {
	case LayerKindProfile, LayerKindBundle:
		if selected.layer.TenantID != "" {
			return ErrInvalidComposition
		}
	case LayerKindTenantOverlay:
		if selected.layer.TenantID != tenantID {
			return ErrInvalidComposition
		}
	default:
		return ErrInvalidComposition
	}
	return nil
}

func (registry *Registry) normalizeLayer(
	tenantID string,
	layer ConfigurationLayer,
) (normalizedConfigurationLayer, map[RowID]EffectiveRow, error) {
	if !validConfigurationID(layer.ID) ||
		layer.Revision == 0 ||
		len(layer.Rows) > maxConfigurationRows {
		return normalizedConfigurationLayer{}, nil, ErrInvalidComposition
	}
	normalized := normalizedConfigurationLayer{
		ID:       layer.ID,
		Revision: layer.Revision,
		TenantID: layer.TenantID,
		Rows:     make([]normalizedConfigurationRow, 0, len(layer.Rows)),
	}
	materializedRows := make(map[RowID]EffectiveRow, len(layer.Rows))
	seenRows := make(map[RowID]struct{}, len(layer.Rows))
	for _, row := range layer.Rows {
		if _, exists := seenRows[row.RowID]; exists {
			return normalizedConfigurationLayer{}, nil, fmt.Errorf("%w: %s", ErrDuplicateRow, row.RowID)
		}
		seenRows[row.RowID] = struct{}{}
		normalizedRow, effectiveRow, err := registry.normalizeConfigurationRow(tenantID, row)
		if err != nil {
			return normalizedConfigurationLayer{}, nil, err
		}
		normalized.Rows = append(normalized.Rows, normalizedRow)
		if normalizedRow.Operation == RowUpsert {
			materializedRows[normalizedRow.RowID] = effectiveRow
		}
	}
	sort.Slice(normalized.Rows, func(left, right int) bool {
		return normalized.Rows[left].RowID < normalized.Rows[right].RowID
	})
	return normalized, materializedRows, nil
}

func (registry *Registry) normalizeConfigurationRow(
	tenantID string,
	row ConfigurationRow,
) (normalizedConfigurationRow, EffectiveRow, error) {
	if !validConfigurationID(string(row.RowID)) {
		return normalizedConfigurationRow{}, EffectiveRow{}, ErrInvalidPluginConfig
	}
	if row.Operation == RowRemove {
		if row.PluginID != "" || row.PluginVersion != "" || row.ArtifactDigest != "" ||
			len(row.Config.Values) != 0 || len(row.Config.SecretClaims) != 0 {
			return normalizedConfigurationRow{}, EffectiveRow{}, ErrInvalidPluginConfig
		}
		return normalizedConfigurationRow{RowID: row.RowID, Operation: RowRemove}, EffectiveRow{}, nil
	}
	if row.Operation != RowUpsert {
		return normalizedConfigurationRow{}, EffectiveRow{}, ErrInvalidPluginConfig
	}
	registered, exists := registry.entries[row.PluginID]
	if !exists {
		return normalizedConfigurationRow{}, EffectiveRow{}, fmt.Errorf("%w: %s", ErrUnknownPlugin, row.PluginID)
	}
	if row.PluginVersion != registered.packageRecord.Version {
		return normalizedConfigurationRow{}, EffectiveRow{}, fmt.Errorf("%w: %s", ErrPluginVersionDrift, row.PluginID)
	}
	if row.ArtifactDigest != registered.packageRecord.ArtifactDigest {
		return normalizedConfigurationRow{}, EffectiveRow{}, fmt.Errorf("%w: %s", ErrArtifactDigestDrift, row.PluginID)
	}
	schema, schemaExists := registry.schemas[registered.manifest.ConfigSchemaDigest]
	if !schemaExists {
		return normalizedConfigurationRow{}, EffectiveRow{}, fmt.Errorf(
			"plugin %s: %w",
			row.PluginID,
			ErrMissingConfigSchema,
		)
	}
	materializedConfig, err := registry.materializeConfigurationInput(
		tenantID,
		registered,
		row,
		schema,
	)
	if err != nil {
		return normalizedConfigurationRow{}, EffectiveRow{}, fmt.Errorf("plugin %s: %w", row.PluginID, err)
	}
	normalized := normalizedConfigurationRow{
		RowID:          row.RowID,
		Operation:      RowUpsert,
		PluginID:       row.PluginID,
		PluginVersion:  row.PluginVersion,
		ArtifactDigest: row.ArtifactDigest,
		Config:         cloneConfig(materializedConfig),
	}
	effective := EffectiveRow{
		RowID:              row.RowID,
		PluginID:           row.PluginID,
		PluginVersion:      row.PluginVersion,
		ArtifactDigest:     row.ArtifactDigest,
		ManifestDigest:     registered.manifestDigest,
		AdmissionRevision:  registered.packageRecord.AdmissionRevision,
		ConfigSchemaDigest: registered.manifest.ConfigSchemaDigest,
		Config:             cloneConfig(materializedConfig),
		Capabilities:       slices.Clone(registered.manifest.Capabilities),
		Egress:             slices.Clone(registered.manifest.Egress),
	}
	return normalized, effective, nil
}

func validatePluginConfigShape(
	schema ConfigSchemaDefinition,
	config ConfigurationInput,
) error {
	if len(config.Values) > maxConfigValues || len(config.SecretClaims) > maxSecretReferences {
		return ErrInvalidPluginConfig
	}
	valueFields := make(map[string]ConfigValueField, len(schema.ValueFields))
	for _, field := range schema.ValueFields {
		valueFields[field.Name] = field
	}
	for key, value := range config.Values {
		field, declared := valueFields[key]
		if len(key) > maxConfigurationIDBytes ||
			!configKeyPattern.MatchString(key) ||
			sensitiveKeyPattern.MatchString(key) ||
			!declared ||
			!utf8.ValidString(value) ||
			len(value) > maxConfigValueBytes ||
			field.Kind != ConfigValueEnum ||
			!slices.Contains(field.Enum, value) {
			return ErrInvalidPluginConfig
		}
	}
	secretFields := make(map[string]ConfigSecretField, len(schema.SecretFields))
	for _, field := range schema.SecretFields {
		secretFields[field.Name] = field
	}
	for name, reference := range config.SecretClaims {
		if _, exists := secretFields[name]; !exists ||
			!sha256DigestPattern.MatchString(reference.ClaimDigest) ||
			reference.ClaimRevision == 0 {
			return ErrInvalidPluginConfig
		}
	}
	return nil
}

func (registry *Registry) materializeConfigurationInput(
	tenantID string,
	registered entry,
	row ConfigurationRow,
	schema ConfigSchemaDefinition,
) (PluginConfig, error) {
	if err := validatePluginConfigShape(schema, row.Config); err != nil ||
		!schemaMatchesManifestSecrets(schema, registered.manifest) {
		return PluginConfig{}, ErrInvalidPluginConfig
	}
	values := cloneStringMap(row.Config.Values)
	if values == nil {
		values = make(map[string]string)
	}
	for _, field := range schema.ValueFields {
		value, exists := values[field.Name]
		if !exists && field.HasDefault {
			values[field.Name] = field.Default
			continue
		}
		if !exists && field.Required {
			return PluginConfig{}, ErrInvalidPluginConfig
		}
		if exists && !slices.Contains(field.Enum, value) {
			return PluginConfig{}, ErrInvalidPluginConfig
		}
	}
	bindings := make(map[string]SecretBindingView, len(row.Config.SecretClaims))
	for _, field := range schema.SecretFields {
		reference, exists := row.Config.SecretClaims[field.Name]
		if !exists && field.Required {
			return PluginConfig{}, ErrSecretClaimDenied
		}
		if !exists {
			continue
		}
		binding, err := registry.resolveSecretClaim(tenantID, registered, row, field, reference)
		if err != nil {
			return PluginConfig{}, err
		}
		bindings[field.Name] = binding
	}
	return PluginConfig{Values: values, SecretBindings: bindings}, nil
}

func schemaMatchesManifestSecrets(schema ConfigSchemaDefinition, manifest Manifest) bool {
	names := make([]string, 0, len(schema.SecretFields))
	for _, field := range schema.SecretFields {
		names = append(names, field.Name)
	}
	slices.Sort(names)
	return slices.Equal(names, manifest.SecretRefNames)
}

func (registry *Registry) resolveSecretClaim(
	tenantID string,
	registered entry,
	row ConfigurationRow,
	field ConfigSecretField,
	reference SecretClaimReference,
) (SecretBindingView, error) {
	registry.secretClaimMu.Lock()
	record, exists := registry.secretClaims[reference.ClaimDigest]
	registry.secretClaimMu.Unlock()
	if !exists || record.revoked || record.view.ClaimRevision != reference.ClaimRevision ||
		record.request.TenantID != tenantID || record.request.RowID != row.RowID ||
		record.request.PluginID != row.PluginID || record.request.PluginVersion != row.PluginVersion ||
		record.request.ArtifactDigest != row.ArtifactDigest ||
		record.request.ManifestDigest != registered.manifestDigest ||
		record.request.AdmissionRevision != registered.packageRecord.AdmissionRevision ||
		record.request.ConfigSchemaDigest != registered.manifest.ConfigSchemaDigest ||
		record.request.LogicalName != field.Name || record.request.Purpose != field.Purpose ||
		record.request.Audience != field.Audience ||
		!slices.Contains(field.AllowedBrokers, record.request.BrokerID) {
		return SecretBindingView{}, ErrSecretClaimDenied
	}
	broker, brokerExists := registry.secretBrokers[record.request.BrokerID]
	if !brokerExists || broker.digest != record.view.BrokerDefinitionDigest ||
		broker.definition.PolicyRevision != record.view.BrokerPolicyRevision {
		return SecretBindingView{}, ErrSecretClaimDenied
	}
	return record.view, nil
}

func validOpaqueReferenceID(value string) bool {
	if value == "" || len(value) > maxReferenceIDBytes || !utf8.ValidString(value) || strings.TrimSpace(value) != value {
		return false
	}
	for _, character := range value {
		if character < 0x21 || character == 0x7f {
			return false
		}
	}
	return true
}

func validScopeID(value string) bool {
	if value == "" || len(value) > maxScopeIDBytes || !utf8.ValidString(value) || strings.TrimSpace(value) != value {
		return false
	}
	for _, character := range value {
		if character < 0x21 || character == 0x7f {
			return false
		}
	}
	return true
}

func validConfigurationID(value string) bool {
	return value != "" && len(value) <= maxConfigurationIDBytes && configurationIDPattern.MatchString(value)
}

func newEffectiveConfiguration(
	tenantID string,
	sources []EffectiveSource,
	rows []EffectiveRow,
	plan Plan,
) (EffectiveConfiguration, error) {
	if !validEffectiveTrustBindings(rows) {
		return EffectiveConfiguration{}, ErrInvalidComposition
	}
	configuration := EffectiveConfiguration{
		schemaVersion: effectiveConfigurationSchemaVersion,
		tenantID:      tenantID,
		sources:       slices.Clone(sources),
		rows:          cloneEffectiveRows(rows),
		plan: Plan{
			Order:    slices.Clone(plan.Order),
			Bindings: slices.Clone(plan.Bindings),
		},
	}
	canonical, err := canonicalEffectiveConfigurationBytes(configuration)
	if err != nil {
		return EffectiveConfiguration{}, ErrInvalidComposition
	}
	configuration.canonical = canonical
	configuration.digest = digestBytes(effectiveDigestDomain, canonical)
	return configuration, nil
}

func validateEffectiveBaseline(configuration EffectiveConfiguration) error {
	canonical, err := canonicalEffectiveConfigurationBytes(configuration)
	if err != nil ||
		configuration.schemaVersion != effectiveConfigurationSchemaVersion ||
		!validEffectiveTrustBindings(configuration.rows) ||
		!bytes.Equal(canonical, configuration.canonical) ||
		digestBytes(effectiveDigestDomain, canonical) != configuration.digest {
		return ErrInvalidBaseline
	}
	return nil
}

func validEffectiveTrustBindings(rows []EffectiveRow) bool {
	for _, row := range rows {
		if !sha256DigestPattern.MatchString(row.ManifestDigest) || row.AdmissionRevision == 0 {
			return false
		}
		for _, binding := range row.Config.SecretBindings {
			if !secretBrokerPattern.MatchString(binding.BrokerID) ||
				!sha256DigestPattern.MatchString(binding.BrokerDefinitionDigest) ||
				!sha256DigestPattern.MatchString(binding.ClaimDigest) ||
				binding.ClaimRevision == 0 ||
				!hmacSHA256Pattern.MatchString(binding.BindingFingerprint) ||
				binding.BrokerPolicyRevision == 0 ||
				!sha256DigestPattern.MatchString(binding.ScopeDigest) {
				return false
			}
		}
	}
	return true
}

func cloneEffectiveRows(rows []EffectiveRow) []EffectiveRow {
	cloned := make([]EffectiveRow, 0, len(rows))
	for _, row := range rows {
		cloned = append(cloned, cloneEffectiveRow(row))
	}
	return cloned
}

func cloneEffectiveRow(row EffectiveRow) EffectiveRow {
	return EffectiveRow{
		RowID:              row.RowID,
		PluginID:           row.PluginID,
		PluginVersion:      row.PluginVersion,
		ArtifactDigest:     row.ArtifactDigest,
		ManifestDigest:     row.ManifestDigest,
		AdmissionRevision:  row.AdmissionRevision,
		ConfigSchemaDigest: row.ConfigSchemaDigest,
		Config:             cloneConfig(row.Config),
		Capabilities:       slices.Clone(row.Capabilities),
		Egress:             slices.Clone(row.Egress),
	}
}

type canonicalLayer struct {
	SchemaVersion uint32         `json:"schemaVersion"`
	ID            string         `json:"id"`
	Revision      uint64         `json:"revision"`
	TenantID      string         `json:"tenantId"`
	Rows          []canonicalRow `json:"rows"`
}

type canonicalRow struct {
	RowID              RowID                    `json:"rowId"`
	Operation          RowOperation             `json:"operation,omitempty"`
	PluginID           PluginID                 `json:"pluginId,omitempty"`
	PluginVersion      string                   `json:"pluginVersion,omitempty"`
	ArtifactDigest     string                   `json:"artifactDigest,omitempty"`
	ManifestDigest     string                   `json:"manifestDigest,omitempty"`
	AdmissionRevision  uint64                   `json:"admissionRevision,omitempty"`
	ConfigSchemaDigest string                   `json:"configSchemaDigest,omitempty"`
	Values             []canonicalValue         `json:"values"`
	SecretBindings     []canonicalSecretBinding `json:"secretBindings"`
	Capabilities       []CapabilityID           `json:"capabilities,omitempty"`
	Egress             []string                 `json:"egress,omitempty"`
}

type canonicalValue struct {
	Name  string `json:"name"`
	Value string `json:"value"`
}

type canonicalSecretBinding struct {
	Name                   string `json:"name"`
	BrokerID               string `json:"brokerId"`
	BrokerDefinitionDigest string `json:"brokerDefinitionDigest"`
	ClaimDigest            string `json:"claimDigest"`
	ClaimRevision          uint64 `json:"claimRevision"`
	BindingFingerprint     string `json:"bindingFingerprint"`
	BrokerPolicyRevision   uint64 `json:"brokerPolicyRevision"`
	ScopeDigest            string `json:"scopeDigest"`
}

type canonicalSource struct {
	Kind     LayerKind `json:"kind"`
	ID       string    `json:"id"`
	Revision uint64    `json:"revision"`
	Digest   string    `json:"digest"`
}

type canonicalBinding struct {
	ConsumerID PluginID `json:"consumerId"`
	Port       PortID   `json:"port"`
	ProviderID PluginID `json:"providerId"`
}

type canonicalEffectiveConfiguration struct {
	SchemaVersion uint32             `json:"schemaVersion"`
	TenantID      string             `json:"tenantId"`
	Sources       []canonicalSource  `json:"sources"`
	Rows          []canonicalRow     `json:"rows"`
	Order         []PluginID         `json:"order"`
	Bindings      []canonicalBinding `json:"bindings"`
}

func digestConfigurationLayer(layer normalizedConfigurationLayer) (string, error) {
	rows := make([]canonicalRow, 0, len(layer.Rows))
	for _, row := range layer.Rows {
		rows = append(rows, canonicalizeConfigurationRow(row))
	}
	canonical, err := marshalCanonical(canonicalLayer{
		SchemaVersion: configurationLayerSchemaVersion,
		ID:            layer.ID,
		Revision:      layer.Revision,
		TenantID:      layer.TenantID,
		Rows:          rows,
	})
	if err != nil {
		return "", err
	}
	return digestBytes(layerDigestDomain, canonical), nil
}

func canonicalEffectiveConfigurationBytes(configuration EffectiveConfiguration) ([]byte, error) {
	rows := make([]canonicalRow, 0, len(configuration.rows))
	for _, row := range configuration.rows {
		rows = append(rows, canonicalizeEffectiveRow(row))
	}
	sources := make([]canonicalSource, 0, len(configuration.sources))
	for _, source := range configuration.sources {
		sources = append(sources, canonicalSource{
			Kind: source.Kind, ID: source.ID, Revision: source.Revision, Digest: source.Digest,
		})
	}
	if sources == nil {
		sources = []canonicalSource{}
	}
	order := slices.Clone(configuration.plan.Order)
	if order == nil {
		order = []PluginID{}
	}
	bindings := make([]canonicalBinding, 0, len(configuration.plan.Bindings))
	for _, binding := range configuration.plan.Bindings {
		bindings = append(bindings, canonicalBinding{
			ConsumerID: binding.ConsumerID, Port: binding.Port, ProviderID: binding.ProviderID,
		})
	}
	if bindings == nil {
		bindings = []canonicalBinding{}
	}
	return marshalCanonical(canonicalEffectiveConfiguration{
		SchemaVersion: configuration.schemaVersion,
		TenantID:      configuration.tenantID,
		Sources:       sources,
		Rows:          rows,
		Order:         order,
		Bindings:      bindings,
	})
}

func canonicalizeConfigurationRow(row normalizedConfigurationRow) canonicalRow {
	return canonicalRow{
		RowID:          row.RowID,
		Operation:      row.Operation,
		PluginID:       row.PluginID,
		PluginVersion:  row.PluginVersion,
		ArtifactDigest: row.ArtifactDigest,
		Values:         canonicalValues(row.Config.Values),
		SecretBindings: canonicalSecretBindings(row.Config.SecretBindings),
	}
}

func canonicalizeEffectiveRow(row EffectiveRow) canonicalRow {
	return canonicalRow{
		RowID:              row.RowID,
		PluginID:           row.PluginID,
		PluginVersion:      row.PluginVersion,
		ArtifactDigest:     row.ArtifactDigest,
		ManifestDigest:     row.ManifestDigest,
		AdmissionRevision:  row.AdmissionRevision,
		ConfigSchemaDigest: row.ConfigSchemaDigest,
		Values:             canonicalValues(row.Config.Values),
		SecretBindings:     canonicalSecretBindings(row.Config.SecretBindings),
		Capabilities:       nonNilCapabilitySlice(row.Capabilities),
		Egress:             nonNilStringSlice(row.Egress),
	}
}

func canonicalValues(values map[string]string) []canonicalValue {
	names := make([]string, 0, len(values))
	for name := range values {
		names = append(names, name)
	}
	slices.Sort(names)
	canonical := make([]canonicalValue, 0, len(names))
	for _, name := range names {
		canonical = append(canonical, canonicalValue{Name: name, Value: values[name]})
	}
	return canonical
}

func canonicalSecretBindings(values map[string]SecretBindingView) []canonicalSecretBinding {
	names := make([]string, 0, len(values))
	for name := range values {
		names = append(names, name)
	}
	slices.Sort(names)
	canonical := make([]canonicalSecretBinding, 0, len(names))
	for _, name := range names {
		binding := values[name]
		canonical = append(canonical, canonicalSecretBinding{
			Name: name, BrokerID: binding.BrokerID,
			BrokerDefinitionDigest: binding.BrokerDefinitionDigest,
			ClaimDigest:            binding.ClaimDigest, ClaimRevision: binding.ClaimRevision,
			BindingFingerprint:   binding.BindingFingerprint,
			BrokerPolicyRevision: binding.BrokerPolicyRevision,
			ScopeDigest:          binding.ScopeDigest,
		})
	}
	return canonical
}

func nonNilCapabilitySlice(values []CapabilityID) []CapabilityID {
	cloned := slices.Clone(values)
	if cloned == nil {
		return []CapabilityID{}
	}
	return cloned
}

func nonNilStringSlice(values []string) []string {
	cloned := slices.Clone(values)
	if cloned == nil {
		return []string{}
	}
	return cloned
}

func marshalCanonical(value any) ([]byte, error) {
	var output bytes.Buffer
	encoder := json.NewEncoder(&output)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(value); err != nil {
		return nil, err
	}
	return bytes.TrimSuffix(output.Bytes(), []byte("\n")), nil
}

func digestBytes(domain string, canonical []byte) string {
	hash := sha256.New()
	_, _ = hash.Write([]byte(domain))
	_, _ = hash.Write(canonical)
	return "sha256:" + hex.EncodeToString(hash.Sum(nil))
}

func diffEffectiveConfigurations(
	baseline *EffectiveConfiguration,
	candidate *EffectiveConfiguration,
) EffectiveConfigurationDiff {
	diff := EffectiveConfigurationDiff{CandidateDigest: candidate.digest}
	beforeRows := make(map[RowID]EffectiveRow)
	if baseline != nil {
		diff.BaseDigest = baseline.digest
		for _, row := range baseline.rows {
			beforeRows[row.RowID] = row
		}
	}
	afterRows := make(map[RowID]EffectiveRow, len(candidate.rows))
	for _, row := range candidate.rows {
		afterRows[row.RowID] = row
	}

	rowSet := make(map[RowID]struct{}, len(beforeRows)+len(afterRows))
	for rowID := range beforeRows {
		rowSet[rowID] = struct{}{}
	}
	for rowID := range afterRows {
		rowSet[rowID] = struct{}{}
	}
	rowIDs := make([]RowID, 0, len(rowSet))
	for rowID := range rowSet {
		rowIDs = append(rowIDs, rowID)
	}
	slices.Sort(rowIDs)
	for _, rowID := range rowIDs {
		before, beforeExists := beforeRows[rowID]
		after, afterExists := afterRows[rowID]
		switch {
		case !beforeExists:
			diff.RowsAdded = append(diff.RowsAdded, rowID)
		case !afterExists:
			diff.RowsRemoved = append(diff.RowsRemoved, rowID)
		default:
			if effectiveRowDigest(before) != effectiveRowDigest(after) {
				diff.RowsChanged = append(diff.RowsChanged, rowID)
			}
			if pluginConfigDigest(before.Config) != pluginConfigDigest(after.Config) {
				diff.ConfigChanged = append(diff.ConfigChanged, rowID)
			}
		}
	}

	beforeCapabilities, beforeEgress := effectiveClaims(beforeRows)
	afterCapabilities, afterEgress := effectiveClaims(afterRows)
	diff.CapabilitiesAdded = addedValues(beforeCapabilities, afterCapabilities, scopedCapabilityLess)
	diff.CapabilitiesRemoved = addedValues(afterCapabilities, beforeCapabilities, scopedCapabilityLess)
	diff.EgressAdded = addedValues(beforeEgress, afterEgress, scopedEgressLess)
	diff.EgressRemoved = addedValues(afterEgress, beforeEgress, scopedEgressLess)
	diff.SecretRefs = diffSecretReferences(rowIDs, beforeRows, afterRows)
	diff.Artifacts = diffArtifacts(rowIDs, beforeRows, afterRows)
	diff.Schemas = diffSchemas(rowIDs, beforeRows, afterRows)
	diff.Manifests = diffManifests(rowIDs, beforeRows, afterRows)
	diff.Admissions = diffAdmissions(rowIDs, beforeRows, afterRows)
	diff.BindingsAdded = addedBindings(baseline, candidate)
	diff.BindingsRemoved = addedBindings(candidate, baseline)
	return diff
}

func diffManifests(
	rowIDs []RowID,
	beforeRows map[RowID]EffectiveRow,
	afterRows map[RowID]EffectiveRow,
) []ManifestChange {
	changes := make([]ManifestChange, 0)
	for _, rowID := range rowIDs {
		before := beforeRows[rowID]
		after := afterRows[rowID]
		if before.ManifestDigest == after.ManifestDigest {
			continue
		}
		pluginID := after.PluginID
		if pluginID == "" {
			pluginID = before.PluginID
		}
		changes = append(changes, ManifestChange{
			RowID: rowID, PluginID: pluginID, Before: before.ManifestDigest, After: after.ManifestDigest,
		})
	}
	return changes
}

func diffAdmissions(
	rowIDs []RowID,
	beforeRows map[RowID]EffectiveRow,
	afterRows map[RowID]EffectiveRow,
) []AdmissionChange {
	changes := make([]AdmissionChange, 0)
	for _, rowID := range rowIDs {
		before := beforeRows[rowID]
		after := afterRows[rowID]
		if before.AdmissionRevision == after.AdmissionRevision {
			continue
		}
		pluginID := after.PluginID
		if pluginID == "" {
			pluginID = before.PluginID
		}
		changes = append(changes, AdmissionChange{
			RowID: rowID, PluginID: pluginID,
			Before: before.AdmissionRevision, After: after.AdmissionRevision,
		})
	}
	return changes
}

func effectiveClaims(rows map[RowID]EffectiveRow) (
	map[string]ScopedCapability,
	map[string]ScopedEgress,
) {
	capabilities := make(map[string]ScopedCapability)
	egress := make(map[string]ScopedEgress)
	for _, row := range rows {
		for _, capability := range row.Capabilities {
			value := ScopedCapability{RowID: row.RowID, PluginID: row.PluginID, Capability: capability}
			capabilities[strings.Join([]string{
				string(row.RowID), string(row.PluginID), string(capability),
			}, "\x00")] = value
		}
		for _, declaration := range row.Egress {
			value := ScopedEgress{RowID: row.RowID, PluginID: row.PluginID, Egress: declaration}
			egress[strings.Join([]string{
				string(row.RowID), string(row.PluginID), declaration,
			}, "\x00")] = value
		}
	}
	return capabilities, egress
}

func addedValues[T any](before, after map[string]T, less func(T, T) bool) []T {
	values := make([]T, 0)
	for key, value := range after {
		if _, exists := before[key]; !exists {
			values = append(values, value)
		}
	}
	sort.Slice(values, func(left, right int) bool { return less(values[left], values[right]) })
	return values
}

func scopedCapabilityLess(left, right ScopedCapability) bool {
	if left.RowID != right.RowID {
		return left.RowID < right.RowID
	}
	if left.PluginID != right.PluginID {
		return left.PluginID < right.PluginID
	}
	return left.Capability < right.Capability
}

func scopedEgressLess(left, right ScopedEgress) bool {
	if left.RowID != right.RowID {
		return left.RowID < right.RowID
	}
	if left.PluginID != right.PluginID {
		return left.PluginID < right.PluginID
	}
	return left.Egress < right.Egress
}

func diffSecretReferences(
	rowIDs []RowID,
	beforeRows map[RowID]EffectiveRow,
	afterRows map[RowID]EffectiveRow,
) []SecretRefChange {
	changes := make([]SecretRefChange, 0)
	for _, rowID := range rowIDs {
		before, beforeExists := beforeRows[rowID]
		after, afterExists := afterRows[rowID]
		names := make(map[string]struct{})
		if beforeExists {
			for name := range before.Config.SecretBindings {
				names[name] = struct{}{}
			}
		}
		if afterExists {
			for name := range after.Config.SecretBindings {
				names[name] = struct{}{}
			}
		}
		orderedNames := make([]string, 0, len(names))
		for name := range names {
			orderedNames = append(orderedNames, name)
		}
		slices.Sort(orderedNames)
		for _, name := range orderedNames {
			beforeRef, beforeRefExists := before.Config.SecretBindings[name]
			afterRef, afterRefExists := after.Config.SecretBindings[name]
			change := SecretRefChange{RowID: rowID, LogicalName: name}
			switch {
			case !beforeRefExists && afterRefExists:
				change.Kind, change.PluginID = SecretRefAdded, after.PluginID
				change.AfterFingerprint = secretBindingFingerprint(afterRef)
			case beforeRefExists && !afterRefExists:
				change.Kind, change.PluginID = SecretRefRemoved, before.PluginID
				change.BeforeFingerprint = secretBindingFingerprint(beforeRef)
			case beforeRef != afterRef || before.PluginID != after.PluginID:
				change.Kind, change.PluginID = SecretRefRetargeted, after.PluginID
				change.BeforeFingerprint = secretBindingFingerprint(beforeRef)
				change.AfterFingerprint = secretBindingFingerprint(afterRef)
			default:
				continue
			}
			changes = append(changes, change)
		}
	}
	return changes
}

func secretBindingFingerprint(binding SecretBindingView) string {
	canonical, _ := marshalCanonical(canonicalSecretBinding{
		BrokerID: binding.BrokerID, BrokerDefinitionDigest: binding.BrokerDefinitionDigest,
		ClaimDigest: binding.ClaimDigest, ClaimRevision: binding.ClaimRevision,
		BindingFingerprint:   binding.BindingFingerprint,
		BrokerPolicyRevision: binding.BrokerPolicyRevision, ScopeDigest: binding.ScopeDigest,
	})
	return digestBytes("wanwork.im/secret-binding-view/1\n", canonical)
}

func diffArtifacts(
	rowIDs []RowID,
	beforeRows map[RowID]EffectiveRow,
	afterRows map[RowID]EffectiveRow,
) []ArtifactChange {
	changes := make([]ArtifactChange, 0)
	for _, rowID := range rowIDs {
		before := beforeRows[rowID]
		after := afterRows[rowID]
		if before.ArtifactDigest == after.ArtifactDigest {
			continue
		}
		pluginID := after.PluginID
		if pluginID == "" {
			pluginID = before.PluginID
		}
		changes = append(changes, ArtifactChange{
			RowID: rowID, PluginID: pluginID, Before: before.ArtifactDigest, After: after.ArtifactDigest,
		})
	}
	return changes
}

func diffSchemas(
	rowIDs []RowID,
	beforeRows map[RowID]EffectiveRow,
	afterRows map[RowID]EffectiveRow,
) []SchemaChange {
	changes := make([]SchemaChange, 0)
	for _, rowID := range rowIDs {
		before := beforeRows[rowID]
		after := afterRows[rowID]
		if before.ConfigSchemaDigest == after.ConfigSchemaDigest {
			continue
		}
		pluginID := after.PluginID
		if pluginID == "" {
			pluginID = before.PluginID
		}
		changes = append(changes, SchemaChange{
			RowID: rowID, PluginID: pluginID, Before: before.ConfigSchemaDigest, After: after.ConfigSchemaDigest,
		})
	}
	return changes
}

func addedBindings(before *EffectiveConfiguration, after *EffectiveConfiguration) []PortBinding {
	beforeSet := make(map[PortBinding]struct{})
	if before != nil {
		for _, binding := range before.plan.Bindings {
			beforeSet[binding] = struct{}{}
		}
	}
	values := make([]PortBinding, 0)
	if after != nil {
		for _, binding := range after.plan.Bindings {
			if _, exists := beforeSet[binding]; !exists {
				values = append(values, binding)
			}
		}
	}
	sort.Slice(values, func(left, right int) bool {
		if values[left].ConsumerID != values[right].ConsumerID {
			return values[left].ConsumerID < values[right].ConsumerID
		}
		if values[left].Port != values[right].Port {
			return values[left].Port < values[right].Port
		}
		return values[left].ProviderID < values[right].ProviderID
	})
	return values
}

func effectiveRowDigest(row EffectiveRow) string {
	canonical, _ := marshalCanonical(canonicalizeEffectiveRow(row))
	return digestBytes("wanwork.im/effective-row/3\n", canonical)
}

func pluginConfigDigest(config PluginConfig) string {
	canonical, _ := marshalCanonical(struct {
		Values         []canonicalValue         `json:"values"`
		SecretBindings []canonicalSecretBinding `json:"secretBindings"`
	}{Values: canonicalValues(config.Values), SecretBindings: canonicalSecretBindings(config.SecretBindings)})
	return digestBytes("wanwork.im/plugin-config/2\n", canonical)
}
