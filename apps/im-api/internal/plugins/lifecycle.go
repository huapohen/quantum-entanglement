package plugins

import (
	"context"
	"errors"
	"fmt"
	"slices"
	"sync"
	"time"
)

var (
	ErrInvalidLifecycle  = errors.New("invalid plugin host lifecycle transition")
	ErrInvalidEffect     = errors.New("invalid plugin lifecycle effect")
	ErrInvalidActivation = errors.New("invalid effective plugin activation")
)

type HostState string

const (
	HostStateNew      HostState = "new"
	HostStateStarting HostState = "starting"
	HostStateReady    HostState = "ready"
	HostStateStopping HostState = "stopping"
	HostStateStopped  HostState = "stopped"
	HostStateFailed   HostState = "failed"
)

type Host struct {
	mu         sync.Mutex
	activation []activationEntry
	state      HostState
	started    []runningPlugin
}

type activationEntry struct {
	id       PluginID
	factory  Factory
	timeouts LifecycleTimeouts
	config   PluginConfig
}

type runningPlugin struct {
	id       PluginID
	instance Instance
	timeouts LifecycleTimeouts
	effects  *effectScope
}

func NewHost(registry *Registry, configuration EffectiveConfiguration) (*Host, error) {
	if registry == nil || validateEffectiveBaseline(configuration) != nil {
		return nil, ErrInvalidActivation
	}
	registry.definitionsMu.RLock()
	defer registry.definitionsMu.RUnlock()
	if !registry.frozen {
		return nil, ErrInvalidActivation
	}
	resolved, err := registry.resolveSelectionLocked(configuration.plan.Order)
	if err != nil || !slices.Equal(resolved.Order, configuration.plan.Order) ||
		!slices.Equal(resolved.Bindings, configuration.plan.Bindings) ||
		!activationRowsMatchRegistry(registry, configuration) {
		return nil, ErrInvalidActivation
	}
	snapshot, err := newEffectiveConfiguration(
		configuration.tenantID,
		configuration.sources,
		configuration.rows,
		configuration.plan,
	)
	if err != nil || snapshot.digest != configuration.digest {
		return nil, ErrInvalidActivation
	}
	activation := make([]activationEntry, 0, len(snapshot.plan.Order))
	configs := snapshot.PluginConfigs()
	for _, pluginID := range snapshot.plan.Order {
		registered := registry.entries[pluginID]
		activation = append(activation, activationEntry{
			id:       pluginID,
			factory:  registered.factory,
			timeouts: registered.manifest.Timeouts,
			config:   cloneConfig(configs[pluginID]),
		})
	}
	return &Host{
		activation: activation,
		state:      HostStateNew,
	}, nil
}

func (host *Host) State() HostState {
	host.mu.Lock()
	defer host.mu.Unlock()
	return host.state
}

func (host *Host) Start(ctx context.Context) error {
	host.mu.Lock()
	if host.state != HostStateNew {
		host.mu.Unlock()
		return ErrInvalidLifecycle
	}
	host.state = HostStateStarting
	host.mu.Unlock()

	configured := make([]runningPlugin, 0, len(host.activation))
	for _, selected := range host.activation {
		if selected.factory == nil {
			host.mu.Lock()
			host.state = HostStateFailed
			host.mu.Unlock()
			return fmt.Errorf("plugin %s: %w", selected.id, ErrMissingFactory)
		}
		instance, configureErr := selected.factory.Configure(cloneConfig(selected.config))
		if configureErr != nil {
			host.mu.Lock()
			host.state = HostStateFailed
			host.mu.Unlock()
			return fmt.Errorf("configure plugin %s: %w", selected.id, configureErr)
		}
		if instance == nil {
			host.mu.Lock()
			host.state = HostStateFailed
			host.mu.Unlock()
			return fmt.Errorf("configure plugin %s: %w", selected.id, ErrMissingFactory)
		}
		configured = append(configured, runningPlugin{
			id:       selected.id,
			instance: instance,
			timeouts: selected.timeouts,
			effects:  newEffectScope(),
		})
	}

	for index := range configured {
		host.mu.Lock()
		host.started = append(host.started, configured[index])
		host.mu.Unlock()
		if startErr := callWithTimeout(
			ctx,
			configured[index].timeouts.Start,
			func(callCtx context.Context) error {
				return configured[index].instance.Start(callCtx, configured[index].effects)
			},
		); startErr != nil {
			return host.failAndRollback(
				ctx,
				fmt.Errorf("start plugin %s: %w", configured[index].id, startErr),
			)
		}
	}
	for index := range configured {
		if readyErr := callWithTimeout(
			ctx,
			configured[index].timeouts.Ready,
			configured[index].instance.Ready,
		); readyErr != nil {
			return host.failAndRollback(
				ctx,
				fmt.Errorf("ready plugin %s: %w", configured[index].id, readyErr),
			)
		}
	}
	host.mu.Lock()
	host.state = HostStateReady
	host.mu.Unlock()
	return nil
}

func activationRowsMatchRegistry(registry *Registry, configuration EffectiveConfiguration) bool {
	if len(configuration.rows) != len(configuration.plan.Order) {
		return false
	}
	rows := make(map[PluginID]EffectiveRow, len(configuration.rows))
	for _, row := range configuration.rows {
		if _, exists := rows[row.PluginID]; exists {
			return false
		}
		rows[row.PluginID] = row
	}
	for _, pluginID := range configuration.plan.Order {
		row, rowExists := rows[pluginID]
		registered, pluginExists := registry.entries[pluginID]
		if !rowExists || !pluginExists ||
			row.PluginVersion != registered.manifest.Version ||
			row.ArtifactDigest != registered.packageRecord.ArtifactDigest ||
			row.ManifestDigest != registered.manifestDigest ||
			row.ManifestDigest != registered.packageRecord.ApprovedManifestDigest ||
			row.AdmissionRevision != registered.packageRecord.AdmissionRevision ||
			registered.packageRecord.Revoked ||
			row.ConfigSchemaDigest != registered.manifest.ConfigSchemaDigest ||
			!slices.Equal(row.Capabilities, registered.manifest.Capabilities) ||
			!slices.Equal(row.Egress, registered.manifest.Egress) ||
			!activationSecretBindingsMatchRegistry(registry, configuration.tenantID, row) {
			return false
		}
	}
	return true
}

func activationSecretBindingsMatchRegistry(
	registry *Registry,
	tenantID string,
	row EffectiveRow,
) bool {
	registry.secretClaimMu.Lock()
	defer registry.secretClaimMu.Unlock()
	for logicalName, binding := range row.Config.SecretBindings {
		record, exists := registry.secretClaims[binding.ClaimDigest]
		if !exists || record.revoked || record.view != binding ||
			record.request.TenantID != tenantID || record.request.RowID != row.RowID ||
			record.request.PluginID != row.PluginID ||
			record.request.PluginVersion != row.PluginVersion ||
			record.request.ArtifactDigest != row.ArtifactDigest ||
			record.request.ManifestDigest != row.ManifestDigest ||
			record.request.AdmissionRevision != row.AdmissionRevision ||
			record.request.ConfigSchemaDigest != row.ConfigSchemaDigest ||
			record.request.LogicalName != logicalName {
			return false
		}
	}
	return true
}

func (host *Host) Stop(ctx context.Context) error {
	host.mu.Lock()
	if host.state == HostStateStopped {
		host.mu.Unlock()
		return nil
	}
	if host.state != HostStateReady && !(host.state == HostStateFailed && len(host.started) > 0) {
		host.mu.Unlock()
		return ErrInvalidLifecycle
	}
	host.state = HostStateStopping
	started := slices.Clone(host.started)
	host.mu.Unlock()

	err := stopPlugins(ctx, started)
	host.mu.Lock()
	defer host.mu.Unlock()
	if err != nil {
		host.state = HostStateFailed
		return err
	}
	host.started = nil
	host.state = HostStateStopped
	return nil
}

func (host *Host) failAndRollback(ctx context.Context, cause error) error {
	host.mu.Lock()
	host.state = HostStateStopping
	started := slices.Clone(host.started)
	host.mu.Unlock()

	rollbackErr := stopPlugins(ctx, started)
	host.mu.Lock()
	defer host.mu.Unlock()
	if rollbackErr == nil {
		host.started = nil
	}
	host.state = HostStateFailed
	return errors.Join(cause, rollbackErr)
}

func stopPlugins(ctx context.Context, plugins []runningPlugin) error {
	cleanupCtx := context.WithoutCancel(ctx)
	reversed := slices.Clone(plugins)
	slices.Reverse(reversed)
	var failures []error
	for _, plugin := range reversed {
		plugin.effects.beginClosing()
	}
	for _, plugin := range reversed {
		if err := callWithTimeout(cleanupCtx, plugin.timeouts.Drain, plugin.instance.Drain); err != nil {
			failures = append(failures, fmt.Errorf("drain plugin %s: %w", plugin.id, err))
		}
	}
	for _, plugin := range reversed {
		if err := callWithTimeout(cleanupCtx, plugin.timeouts.Stop, plugin.instance.Stop); err != nil {
			failures = append(failures, fmt.Errorf("stop plugin %s: %w", plugin.id, err))
		}
	}
	for _, plugin := range reversed {
		if err := plugin.effects.cleanup(cleanupCtx, plugin.timeouts.Stop); err != nil {
			failures = append(failures, fmt.Errorf("cleanup plugin %s: %w", plugin.id, err))
		}
	}
	return errors.Join(failures...)
}

func callWithTimeout(parent context.Context, timeout time.Duration, operation func(context.Context) error) error {
	ctx, cancel := context.WithTimeout(parent, timeout)
	defer cancel()
	return operation(ctx)
}

type registeredEffect struct {
	label   string
	cleanup func(context.Context) error
}

type effectScope struct {
	mu                sync.Mutex
	state             effectScopeState
	cleanupInProgress bool
	effects           []registeredEffect
	labels            map[string]struct{}
}

type effectScopeState string

const (
	effectScopeOpen    effectScopeState = "open"
	effectScopeClosing effectScopeState = "closing"
	effectScopeClosed  effectScopeState = "closed"
)

func newEffectScope() *effectScope {
	return &effectScope{state: effectScopeOpen, labels: make(map[string]struct{})}
}

func (scope *effectScope) Defer(label string, cleanup func(context.Context) error) error {
	scope.mu.Lock()
	defer scope.mu.Unlock()
	if scope.state != effectScopeOpen || label == "" || cleanup == nil {
		return ErrInvalidEffect
	}
	if _, exists := scope.labels[label]; exists {
		return ErrInvalidEffect
	}
	scope.labels[label] = struct{}{}
	scope.effects = append(scope.effects, registeredEffect{label: label, cleanup: cleanup})
	return nil
}

func (scope *effectScope) beginClosing() {
	scope.mu.Lock()
	defer scope.mu.Unlock()
	if scope.state == effectScopeOpen {
		scope.state = effectScopeClosing
	}
}

func (scope *effectScope) cleanup(parent context.Context, timeout time.Duration) error {
	scope.mu.Lock()
	if scope.state == effectScopeClosed {
		scope.mu.Unlock()
		return nil
	}
	if scope.cleanupInProgress {
		scope.mu.Unlock()
		return ErrInvalidEffect
	}
	if scope.state == effectScopeOpen {
		scope.state = effectScopeClosing
	}
	scope.cleanupInProgress = true
	effects := slices.Clone(scope.effects)
	scope.mu.Unlock()
	slices.Reverse(effects)

	var failures []error
	cleaned := make(map[string]struct{}, len(effects))
	for _, effect := range effects {
		if err := callWithTimeout(parent, timeout, effect.cleanup); err != nil {
			failures = append(failures, fmt.Errorf("effect %s: %w", effect.label, err))
			continue
		}
		cleaned[effect.label] = struct{}{}
	}
	scope.mu.Lock()
	scope.cleanupInProgress = false
	retained := make([]registeredEffect, 0, len(scope.effects))
	for _, effect := range scope.effects {
		if _, wasCleaned := cleaned[effect.label]; wasCleaned {
			delete(scope.labels, effect.label)
			continue
		}
		retained = append(retained, effect)
	}
	scope.effects = retained
	if len(scope.effects) == 0 {
		scope.state = effectScopeClosed
	}
	scope.mu.Unlock()
	return errors.Join(failures...)
}

func cloneConfigs(configs map[PluginID]PluginConfig) map[PluginID]PluginConfig {
	cloned := make(map[PluginID]PluginConfig, len(configs))
	for pluginID, config := range configs {
		cloned[pluginID] = cloneConfig(config)
	}
	return cloned
}

func cloneConfig(config PluginConfig) PluginConfig {
	return PluginConfig{
		Values:         cloneStringMap(config.Values),
		SecretBindings: cloneSecretBindings(config.SecretBindings),
	}
}

func cloneStringMap(values map[string]string) map[string]string {
	if values == nil {
		return nil
	}
	cloned := make(map[string]string, len(values))
	for key, value := range values {
		cloned[key] = value
	}
	return cloned
}

func cloneSecretBindings(values map[string]SecretBindingView) map[string]SecretBindingView {
	if values == nil {
		return nil
	}
	cloned := make(map[string]SecretBindingView, len(values))
	for key, value := range values {
		cloned[key] = value
	}
	return cloned
}
