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
	mu            sync.Mutex
	registry      *Registry
	configuration EffectiveConfiguration
	state         HostState
	started       []runningPlugin
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
	resolved, err := registry.ResolveSelection(configuration.plan.Order)
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
	return &Host{registry: registry, configuration: snapshot, state: HostStateNew}, nil
}

func (host *Host) State() HostState {
	host.mu.Lock()
	defer host.mu.Unlock()
	return host.state
}

func (host *Host) Start(ctx context.Context) error {
	host.mu.Lock()
	defer host.mu.Unlock()
	if host.state != HostStateNew {
		return ErrInvalidLifecycle
	}
	host.state = HostStateStarting

	plan := host.configuration.plan
	configs := host.configuration.PluginConfigs()
	configured := make([]runningPlugin, 0, len(plan.Order))
	for _, pluginID := range plan.Order {
		registered := host.registry.entries[pluginID]
		if registered.factory == nil {
			host.state = HostStateFailed
			return fmt.Errorf("plugin %s: %w", pluginID, ErrMissingFactory)
		}
		instance, configureErr := registered.factory.Configure(cloneConfig(configs[pluginID]))
		if configureErr != nil {
			host.state = HostStateFailed
			return fmt.Errorf("configure plugin %s: %w", pluginID, configureErr)
		}
		if instance == nil {
			host.state = HostStateFailed
			return fmt.Errorf("configure plugin %s: %w", pluginID, ErrMissingFactory)
		}
		configured = append(configured, runningPlugin{
			id:       pluginID,
			instance: instance,
			timeouts: registered.manifest.Timeouts,
			effects:  newEffectScope(),
		})
	}

	for index := range configured {
		host.started = append(host.started, configured[index])
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
	host.state = HostStateReady
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
			row.ConfigSchemaDigest != registered.manifest.ConfigSchemaDigest ||
			!slices.Equal(row.Capabilities, registered.manifest.Capabilities) ||
			!slices.Equal(row.Egress, registered.manifest.Egress) {
			return false
		}
	}
	return true
}

func (host *Host) Stop(ctx context.Context) error {
	host.mu.Lock()
	defer host.mu.Unlock()
	if host.state == HostStateStopped {
		return nil
	}
	if host.state != HostStateReady {
		return ErrInvalidLifecycle
	}
	host.state = HostStateStopping
	err := stopPlugins(ctx, host.started)
	host.started = nil
	host.state = HostStateStopped
	return err
}

func (host *Host) failAndRollback(ctx context.Context, cause error) error {
	rollbackErr := stopPlugins(ctx, host.started)
	host.started = nil
	host.state = HostStateFailed
	return errors.Join(cause, rollbackErr)
}

func stopPlugins(ctx context.Context, plugins []runningPlugin) error {
	reversed := slices.Clone(plugins)
	slices.Reverse(reversed)
	var failures []error
	for _, plugin := range reversed {
		if err := callWithTimeout(ctx, plugin.timeouts.Drain, plugin.instance.Drain); err != nil {
			failures = append(failures, fmt.Errorf("drain plugin %s: %w", plugin.id, err))
		}
	}
	for _, plugin := range reversed {
		if err := callWithTimeout(ctx, plugin.timeouts.Stop, plugin.instance.Stop); err != nil {
			failures = append(failures, fmt.Errorf("stop plugin %s: %w", plugin.id, err))
		}
	}
	for _, plugin := range reversed {
		if err := plugin.effects.cleanup(ctx, plugin.timeouts.Stop); err != nil {
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
	mu      sync.Mutex
	effects []registeredEffect
	labels  map[string]struct{}
}

func newEffectScope() *effectScope {
	return &effectScope{labels: make(map[string]struct{})}
}

func (scope *effectScope) Defer(label string, cleanup func(context.Context) error) error {
	scope.mu.Lock()
	defer scope.mu.Unlock()
	if label == "" || cleanup == nil {
		return ErrInvalidEffect
	}
	if _, exists := scope.labels[label]; exists {
		return ErrInvalidEffect
	}
	scope.labels[label] = struct{}{}
	scope.effects = append(scope.effects, registeredEffect{label: label, cleanup: cleanup})
	return nil
}

func (scope *effectScope) cleanup(parent context.Context, timeout time.Duration) error {
	scope.mu.Lock()
	effects := slices.Clone(scope.effects)
	scope.effects = nil
	scope.labels = make(map[string]struct{})
	scope.mu.Unlock()
	slices.Reverse(effects)

	var failures []error
	for _, effect := range effects {
		if err := callWithTimeout(parent, timeout, effect.cleanup); err != nil {
			failures = append(failures, fmt.Errorf("effect %s: %w", effect.label, err))
		}
	}
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
		Values:     cloneStringMap(config.Values),
		SecretRefs: cloneSecretReferences(config.SecretRefs),
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

func cloneSecretReferences(values map[string]SecretReference) map[string]SecretReference {
	if values == nil {
		return nil
	}
	cloned := make(map[string]SecretReference, len(values))
	for key, value := range values {
		cloned[key] = value
	}
	return cloned
}
