package plugins

import (
	"errors"
	"fmt"
	"regexp"
	"slices"
	"sort"
)

var (
	ErrInvalidManifest     = errors.New("invalid plugin manifest")
	ErrPackageNotAdmitted  = errors.New("plugin package is not admitted")
	ErrDuplicatePlugin     = errors.New("duplicate plugin ID")
	ErrMissingProvider     = errors.New("required plugin port has no provider")
	ErrAmbiguousProvider   = errors.New("required plugin port has multiple providers")
	ErrInvalidProvider     = errors.New("pinned provider does not provide required port")
	ErrDependencyCycle     = errors.New("plugin dependency cycle")
	ErrMissingFactory      = errors.New("plugin factory is missing")
	ErrDuplicateSchema     = errors.New("duplicate plugin configuration schema")
	pluginIDPattern        = regexp.MustCompile(`^[a-z][a-z0-9-]*(?:\.[a-z][a-z0-9-]*)*\.v[1-9][0-9]*$`)
	portIDPattern          = regexp.MustCompile(`^[a-z][a-z0-9-]*(?:\.[a-z][a-z0-9-]*)+\.v[1-9][0-9]*$`)
	capabilityIDPattern    = regexp.MustCompile(`^[a-z][a-z0-9-]*(?:\.[a-z][a-z0-9-]*)+$`)
	secretReferencePattern = regexp.MustCompile(`^[a-z][a-z0-9_.-]*$`)
	semanticVersionPattern = regexp.MustCompile(`^[0-9]+\.[0-9]+\.[0-9]+$`)
	sha256DigestPattern    = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)
)

type Registry struct {
	entries map[PluginID]entry
	schemas map[string]ConfigSchema
}

type entry struct {
	manifest      Manifest
	packageRecord PackageRecord
	factory       Factory
}

func NewRegistry() *Registry {
	return &Registry{
		entries: make(map[PluginID]entry),
		schemas: make(map[string]ConfigSchema),
	}
}

func (registry *Registry) RegisterConfigSchema(digest string, schema ConfigSchema) error {
	if registry == nil || !sha256DigestPattern.MatchString(digest) || schema == nil {
		return ErrInvalidManifest
	}
	if _, exists := registry.schemas[digest]; exists {
		return fmt.Errorf("%w: %s", ErrDuplicateSchema, digest)
	}
	registry.schemas[digest] = schema
	return nil
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
	if err := validatePackageRecord(normalized, packageRecord); err != nil {
		return err
	}
	if _, exists := registry.entries[normalized.ID]; exists {
		return fmt.Errorf("%w: %s", ErrDuplicatePlugin, normalized.ID)
	}
	registry.entries[normalized.ID] = entry{
		manifest:      normalized,
		packageRecord: packageRecord,
		factory:       factory,
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
		!validUniqueStrings(normalized.Egress, func(value string) bool { return value != "" }) ||
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
	return timeouts.Start > 0 && timeouts.Ready > 0 && timeouts.Drain > 0 && timeouts.Stop > 0
}

func validatePackageRecord(manifest Manifest, packageRecord PackageRecord) error {
	if packageRecord.PluginID != manifest.ID ||
		packageRecord.Version != manifest.Version ||
		!sha256DigestPattern.MatchString(packageRecord.ArtifactDigest) ||
		packageRecord.ProvenanceRef == "" ||
		packageRecord.SBOMRef == "" ||
		packageRecord.ApprovalRef == "" ||
		packageRecord.Revoked {
		return ErrPackageNotAdmitted
	}
	return nil
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
