package plugins

import (
	"context"
	"errors"
	"fmt"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestHostStartsAndStopsInDeterministicReverseOrder(t *testing.T) {
	t.Parallel()

	log := newCallLog()
	host := lifecycleHost(t, log, "")
	if err := host.Start(context.Background()); err != nil {
		t.Fatalf("start host: %v", err)
	}
	if host.State() != HostStateReady {
		t.Fatalf("state = %q, want %q", host.State(), HostStateReady)
	}
	if err := host.Stop(context.Background()); err != nil {
		t.Fatalf("stop host: %v", err)
	}
	if err := host.Stop(context.Background()); err != nil {
		t.Fatalf("repeat stop should be idempotent: %v", err)
	}

	want := []string{
		"configure:auth.fake.v1", "configure:im.fake.v1", "configure:runtime.fake.v1",
		"start:auth.fake.v1", "start:im.fake.v1", "start:runtime.fake.v1",
		"ready:auth.fake.v1", "ready:im.fake.v1", "ready:runtime.fake.v1",
		"drain:runtime.fake.v1", "drain:im.fake.v1", "drain:auth.fake.v1",
		"stop:runtime.fake.v1", "stop:im.fake.v1", "stop:auth.fake.v1",
		"cleanup:runtime.fake.v1", "cleanup:im.fake.v1", "cleanup:auth.fake.v1",
	}
	if calls := log.snapshot(); !slices.Equal(calls, want) {
		t.Fatalf("calls = %v, want %v", calls, want)
	}
}

func TestHostRollsBackPartialStartAndReadyFailures(t *testing.T) {
	t.Parallel()

	for _, failAt := range []string{"start:runtime.fake.v1", "ready:im.fake.v1"} {
		t.Run(failAt, func(t *testing.T) {
			t.Parallel()
			log := newCallLog()
			host := lifecycleHost(t, log, failAt)
			if err := host.Start(context.Background()); err == nil {
				t.Fatal("start host succeeded, want injected failure")
			}
			if host.State() != HostStateFailed {
				t.Fatalf("state = %q, want %q", host.State(), HostStateFailed)
			}
			calls := log.snapshot()
			for _, required := range []string{
				"drain:runtime.fake.v1", "drain:im.fake.v1", "drain:auth.fake.v1",
				"stop:runtime.fake.v1", "stop:im.fake.v1", "stop:auth.fake.v1",
				"cleanup:runtime.fake.v1", "cleanup:im.fake.v1", "cleanup:auth.fake.v1",
			} {
				if !slices.Contains(calls, required) {
					t.Fatalf("calls %v do not contain %q", calls, required)
				}
			}
		})
	}
}

func TestCleanupFailureDoesNotSkipRemainingEffectsOrPlugins(t *testing.T) {
	t.Parallel()

	log := newCallLog()
	host := lifecycleHost(t, log, "cleanup:runtime.fake.v1")
	if err := host.Start(context.Background()); err != nil {
		t.Fatalf("start host: %v", err)
	}
	err := host.Stop(context.Background())
	if err == nil || !strings.Contains(err.Error(), "injected failure") {
		t.Fatalf("stop error = %v, want joined cleanup failure", err)
	}
	if host.State() != HostStateFailed {
		t.Fatalf("state = %q, want retryable %q", host.State(), HostStateFailed)
	}
	calls := log.snapshot()
	for _, required := range []string{
		"cleanup:runtime.fake.v1",
		"cleanup:im.fake.v1",
		"cleanup:auth.fake.v1",
	} {
		if !slices.Contains(calls, required) {
			t.Fatalf("calls %v do not contain %q", calls, required)
		}
	}
}

func TestCancelledStopUsesIndependentBoundedCleanupContext(t *testing.T) {
	t.Parallel()

	log := newCallLog()
	manifest := testManifest("cleanup.fake.v1", []PortID{"runtime.invoke.v1"}, nil)
	registry := NewRegistry()
	registerLifecycleSchema(t, registry)
	factory := &fakeFactory{
		manifest:                   manifest,
		log:                        log,
		cleanupRequiresLiveContext: true,
	}
	if err := registry.RegisterFactory(factory, admittedPackage(manifest)); err != nil {
		t.Fatalf("register factory: %v", err)
	}
	host := lifecycleHostFromSelection(t, registry, []Manifest{manifest})
	if err := host.Start(context.Background()); err != nil {
		t.Fatalf("start host: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := host.Stop(ctx); err != nil {
		t.Fatalf("stop with cancelled caller context: %v", err)
	}
	if host.State() != HostStateStopped ||
		!slices.Contains(log.snapshot(), "cleanup:cleanup.fake.v1") {
		t.Fatalf("state/calls = %q/%v", host.State(), log.snapshot())
	}
}

func TestCancelledStartRollbackUsesIndependentCleanupContext(t *testing.T) {
	t.Parallel()

	log := newCallLog()
	manifest := testManifest("rollback.fake.v1", []PortID{"runtime.invoke.v1"}, nil)
	registry := NewRegistry()
	registerLifecycleSchema(t, registry)
	factory := &fakeFactory{
		manifest:                   manifest,
		log:                        log,
		blockAt:                    "start:rollback.fake.v1",
		cleanupRequiresLiveContext: true,
	}
	if err := registry.RegisterFactory(factory, admittedPackage(manifest)); err != nil {
		t.Fatalf("register factory: %v", err)
	}
	host := lifecycleHostFromSelection(t, registry, []Manifest{manifest})
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := host.Start(ctx); !errors.Is(err, context.Canceled) {
		t.Fatalf("start error = %v, want %v", err, context.Canceled)
	}
	if host.State() != HostStateFailed ||
		!slices.Contains(log.snapshot(), "cleanup:rollback.fake.v1") {
		t.Fatalf("state/calls = %q/%v", host.State(), log.snapshot())
	}
}

func TestFailedCleanupIsRetainedAndStopCanRetry(t *testing.T) {
	t.Parallel()

	log := newCallLog()
	manifest := testManifest("retry.fake.v1", []PortID{"runtime.invoke.v1"}, nil)
	registry := NewRegistry()
	registerLifecycleSchema(t, registry)
	factory := &fakeFactory{
		manifest:   manifest,
		log:        log,
		failOnceAt: "cleanup:retry.fake.v1",
	}
	if err := registry.RegisterFactory(factory, admittedPackage(manifest)); err != nil {
		t.Fatalf("register factory: %v", err)
	}
	host := lifecycleHostFromSelection(t, registry, []Manifest{manifest})
	if err := host.Start(context.Background()); err != nil {
		t.Fatalf("start host: %v", err)
	}
	if err := host.Stop(context.Background()); err == nil {
		t.Fatal("first stop succeeded, want injected cleanup failure")
	}
	if host.State() != HostStateFailed {
		t.Fatalf("state = %q, want %q", host.State(), HostStateFailed)
	}
	if err := host.Stop(context.Background()); err != nil {
		t.Fatalf("retry stop: %v", err)
	}
	if host.State() != HostStateStopped {
		t.Fatalf("state = %q, want %q", host.State(), HostStateStopped)
	}
	cleanupCall := "cleanup:retry.fake.v1"
	if calls := log.snapshot(); countCalls(calls, cleanupCall) != 2 {
		t.Fatalf("cleanup retry calls = %v", calls)
	}
}

func TestEffectScopeRetainsOnlyFailedCleanupForRetry(t *testing.T) {
	t.Parallel()

	scope := newEffectScope()
	attempts := 0
	if err := scope.Defer("retry", func(context.Context) error {
		attempts++
		if attempts == 1 {
			return errors.New("retry cleanup")
		}
		return nil
	}); err != nil {
		t.Fatalf("register cleanup: %v", err)
	}
	if err := scope.cleanup(context.Background(), time.Second); err == nil {
		t.Fatal("first cleanup succeeded, want retryable failure")
	}
	if err := scope.cleanup(context.Background(), time.Second); err != nil {
		t.Fatalf("retry cleanup: %v", err)
	}
	if attempts != 2 {
		t.Fatalf("cleanup attempts = %d, want 2", attempts)
	}
}

func TestHostRejectsMissingFactoryAndRepeatedStart(t *testing.T) {
	t.Parallel()

	manifest := testManifest("im.fake.v1", []PortID{"im.transport.v1"}, nil)
	registry := NewRegistry()
	registerLifecycleSchema(t, registry)
	if err := registry.Register(manifest, admittedPackage(manifest)); err != nil {
		t.Fatalf("register manifest: %v", err)
	}
	host := lifecycleHostFromSelection(t, registry, []Manifest{manifest})
	if err := host.Start(context.Background()); !errors.Is(err, ErrMissingFactory) {
		t.Fatalf("start error = %v, want %v", err, ErrMissingFactory)
	}
	if err := host.Start(context.Background()); !errors.Is(err, ErrInvalidLifecycle) {
		t.Fatalf("repeat start error = %v, want %v", err, ErrInvalidLifecycle)
	}
}

func TestLifecycleTimeoutTriggersRollback(t *testing.T) {
	t.Parallel()

	log := newCallLog()
	manifest := testManifest("slow.fake.v1", []PortID{"runtime.invoke.v1"}, nil)
	manifest.Timeouts.Start = 5 * time.Millisecond
	registry := NewRegistry()
	registerLifecycleSchema(t, registry)
	factory := &fakeFactory{
		manifest: manifest,
		log:      log,
		blockAt:  "start:slow.fake.v1",
	}
	if err := registry.RegisterFactory(factory, admittedPackage(manifest)); err != nil {
		t.Fatalf("register factory: %v", err)
	}
	host := lifecycleHostFromSelection(t, registry, []Manifest{manifest})
	if err := host.Start(context.Background()); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("start error = %v, want deadline exceeded", err)
	}
	for _, required := range []string{
		"drain:slow.fake.v1",
		"stop:slow.fake.v1",
		"cleanup:slow.fake.v1",
	} {
		if !slices.Contains(log.snapshot(), required) {
			t.Fatalf("calls %v do not contain %q", log.snapshot(), required)
		}
	}
}

func TestEffectScopeRejectsInvalidAndDuplicateLabels(t *testing.T) {
	t.Parallel()

	scope := newEffectScope()
	cleanup := func(context.Context) error { return nil }
	if err := scope.Defer("", cleanup); !errors.Is(err, ErrInvalidEffect) {
		t.Fatalf("empty label error = %v, want %v", err, ErrInvalidEffect)
	}
	if err := scope.Defer("valid", nil); !errors.Is(err, ErrInvalidEffect) {
		t.Fatalf("nil cleanup error = %v, want %v", err, ErrInvalidEffect)
	}
	if err := scope.Defer("valid", cleanup); err != nil {
		t.Fatalf("register effect: %v", err)
	}
	if err := scope.Defer("valid", cleanup); !errors.Is(err, ErrInvalidEffect) {
		t.Fatalf("duplicate effect error = %v, want %v", err, ErrInvalidEffect)
	}
}

func TestHostStartsOnlyPluginsFrozenInEffectiveConfiguration(t *testing.T) {
	t.Parallel()

	log := newCallLog()
	selected := testManifest("selected.fake.v1", []PortID{"runtime.invoke.v1"}, nil)
	extra := testManifest("extra.fake.v1", []PortID{"unused.port.v1"}, nil)
	registry := NewRegistry()
	registerLifecycleSchema(t, registry)
	for _, manifest := range []Manifest{selected, extra} {
		if err := registry.RegisterFactory(
			&fakeFactory{manifest: manifest, log: log},
			admittedPackage(manifest),
		); err != nil {
			t.Fatalf("register %s: %v", manifest.ID, err)
		}
	}
	host := lifecycleHostFromSelection(t, registry, []Manifest{selected})
	if err := host.Start(context.Background()); err != nil {
		t.Fatalf("start host: %v", err)
	}
	if err := host.Stop(context.Background()); err != nil {
		t.Fatalf("stop host: %v", err)
	}
	for _, call := range log.snapshot() {
		if strings.Contains(call, string(extra.ID)) {
			t.Fatalf("unselected plugin activated: %v", log.snapshot())
		}
	}
}

func TestNewHostRejectsTamperedEffectiveConfiguration(t *testing.T) {
	t.Parallel()

	manifest := testManifest("selected.fake.v1", []PortID{"runtime.invoke.v1"}, nil)
	registry := NewRegistry()
	registerLifecycleSchema(t, registry)
	if err := registry.RegisterFactory(
		&fakeFactory{manifest: manifest, log: newCallLog()},
		admittedPackage(manifest),
	); err != nil {
		t.Fatalf("register factory: %v", err)
	}
	configuration := lifecycleConfiguration(t, registry, []Manifest{manifest})
	configuration.plan.Order = append(configuration.plan.Order, "missing.fake.v1")
	if _, err := NewHost(registry, configuration); !errors.Is(err, ErrInvalidActivation) {
		t.Fatalf("new host error = %v, want %v", err, ErrInvalidActivation)
	}
}

func TestNewHostRejectsManifestAndAdmissionDrift(t *testing.T) {
	t.Parallel()

	manifest := testManifest("selected.fake.v1", []PortID{"runtime.invoke.v1"}, nil)
	baselineRegistry := NewRegistry()
	registerLifecycleSchema(t, baselineRegistry)
	if err := baselineRegistry.RegisterFactory(
		&fakeFactory{manifest: manifest, log: newCallLog()},
		admittedPackage(manifest),
	); err != nil {
		t.Fatalf("register baseline factory: %v", err)
	}
	configuration := lifecycleConfiguration(t, baselineRegistry, []Manifest{manifest})

	manifestDrift := manifest
	manifestDrift.Timeouts.Start += time.Millisecond
	driftRegistry := NewRegistry()
	registerLifecycleSchema(t, driftRegistry)
	if err := driftRegistry.RegisterFactory(
		&fakeFactory{manifest: manifestDrift, log: newCallLog()},
		admittedPackage(manifestDrift),
	); err != nil {
		t.Fatalf("register drifted manifest: %v", err)
	}
	if _, err := NewHost(driftRegistry, configuration); !errors.Is(err, ErrInvalidActivation) {
		t.Fatalf("manifest drift error = %v, want %v", err, ErrInvalidActivation)
	}

	admissionRegistry := NewRegistry()
	registerLifecycleSchema(t, admissionRegistry)
	admission := admittedPackage(manifest)
	admission.AdmissionRevision = 2
	if err := admissionRegistry.RegisterFactory(
		&fakeFactory{manifest: manifest, log: newCallLog()},
		admission,
	); err != nil {
		t.Fatalf("register revised admission: %v", err)
	}
	if _, err := NewHost(admissionRegistry, configuration); !errors.Is(err, ErrInvalidActivation) {
		t.Fatalf("admission drift error = %v, want %v", err, ErrInvalidActivation)
	}
}

func TestHostStartUsesFrozenActivationSnapshot(t *testing.T) {
	t.Parallel()

	originalLog := newCallLog()
	replacementLog := newCallLog()
	manifest := testManifest("selected.fake.v1", []PortID{"runtime.invoke.v1"}, nil)
	registry := NewRegistry()
	registerLifecycleSchema(t, registry)
	if err := registry.RegisterFactory(
		&fakeFactory{manifest: manifest, log: originalLog},
		admittedPackage(manifest),
	); err != nil {
		t.Fatalf("register factory: %v", err)
	}
	host := lifecycleHostFromSelection(t, registry, []Manifest{manifest})

	replaced := registry.entries[manifest.ID]
	replaced.factory = &fakeFactory{manifest: manifest, log: replacementLog}
	replaced.manifest.Timeouts.Start = time.Millisecond
	registry.entries[manifest.ID] = replaced

	if err := host.Start(context.Background()); err != nil {
		t.Fatalf("start host: %v", err)
	}
	if err := host.Stop(context.Background()); err != nil {
		t.Fatalf("stop host: %v", err)
	}
	if calls := originalLog.snapshot(); len(calls) == 0 {
		t.Fatal("frozen factory was not invoked")
	}
	if calls := replacementLog.snapshot(); len(calls) != 0 {
		t.Fatalf("host re-read mutable registry entry: %v", calls)
	}
}

func lifecycleHost(t *testing.T, log *callLog, failAt string) *Host {
	t.Helper()

	manifests := []Manifest{
		testManifest("auth.fake.v1", []PortID{"auth.verify.v1"}, nil),
		testManifest("im.fake.v1", []PortID{"im.transport.v1"}, nil),
		testManifest("runtime.fake.v1", []PortID{"runtime.invoke.v1"}, []PortRequirement{
			{Port: "auth.verify.v1"},
			{Port: "im.transport.v1"},
		}),
	}
	registry := NewRegistry()
	registerLifecycleSchema(t, registry)
	for _, manifest := range manifests {
		factory := &fakeFactory{manifest: manifest, log: log, failAt: failAt}
		if err := registry.RegisterFactory(factory, admittedPackage(manifest)); err != nil {
			t.Fatalf("register %s: %v", manifest.ID, err)
		}
	}
	return lifecycleHostFromSelection(t, registry, manifests)
}

func registerLifecycleSchema(t *testing.T, registry *Registry) {
	t.Helper()
	if err := registry.RegisterConfigSchema(testSchemaDigest, testConfigSchemaDefinition); err != nil {
		t.Fatalf("register lifecycle schema: %v", err)
	}
}

func lifecycleHostFromSelection(t *testing.T, registry *Registry, manifests []Manifest) *Host {
	t.Helper()
	host, err := NewHost(registry, lifecycleConfiguration(t, registry, manifests))
	if err != nil {
		t.Fatalf("new host: %v", err)
	}
	return host
}

func lifecycleConfiguration(
	t *testing.T,
	registry *Registry,
	manifests []Manifest,
) EffectiveConfiguration {
	t.Helper()
	rows := make([]ConfigurationRow, 0, len(manifests))
	for index, manifest := range manifests {
		rows = append(rows, ConfigurationRow{
			RowID:          RowID(fmt.Sprintf("plugin.%d", index)),
			Operation:      RowUpsert,
			PluginID:       manifest.ID,
			PluginVersion:  manifest.Version,
			ArtifactDigest: testArtifactDigest,
			Config:         ConfigurationInput{},
		})
	}
	result, err := registry.Compose(Composition{
		TenantID: "tenant-lifecycle",
		Profile:  ConfigurationLayer{ID: "profile.lifecycle", Revision: 1, Rows: rows},
	}, nil)
	if err != nil {
		t.Fatalf("compose lifecycle configuration: %v", err)
	}
	return result.Candidate
}

type fakeFactory struct {
	manifest                   Manifest
	log                        *callLog
	failAt                     string
	failOnceAt                 string
	blockAt                    string
	cleanupRequiresLiveContext bool
}

func (factory *fakeFactory) Manifest() Manifest {
	return factory.manifest
}

func (factory *fakeFactory) Configure(PluginConfig) (Instance, error) {
	factory.log.add("configure:" + string(factory.manifest.ID))
	return &fakeInstance{
		id:                         factory.manifest.ID,
		log:                        factory.log,
		failAt:                     factory.failAt,
		failOnceAt:                 factory.failOnceAt,
		blockAt:                    factory.blockAt,
		cleanupRequiresLiveContext: factory.cleanupRequiresLiveContext,
	}, nil
}

type fakeInstance struct {
	id                         PluginID
	log                        *callLog
	failAt                     string
	failOnceAt                 string
	blockAt                    string
	cleanupRequiresLiveContext bool
	failureMu                  sync.Mutex
	failedOnce                 bool
}

func (instance *fakeInstance) Start(ctx context.Context, effects Effects) error {
	call := "start:" + string(instance.id)
	instance.log.add(call)
	if err := effects.Defer("resource", func(cleanupCtx context.Context) error {
		cleanupCall := "cleanup:" + string(instance.id)
		instance.log.add(cleanupCall)
		if instance.cleanupRequiresLiveContext && cleanupCtx.Err() != nil {
			return cleanupCtx.Err()
		}
		return instance.failure(cleanupCall)
	}); err != nil {
		return err
	}
	if instance.blockAt == call {
		<-ctx.Done()
		return ctx.Err()
	}
	return instance.failure(call)
}

func (instance *fakeInstance) Ready(context.Context) error {
	call := "ready:" + string(instance.id)
	instance.log.add(call)
	return instance.failure(call)
}

func (instance *fakeInstance) Drain(context.Context) error {
	call := "drain:" + string(instance.id)
	instance.log.add(call)
	return instance.failure(call)
}

func (instance *fakeInstance) Stop(context.Context) error {
	call := "stop:" + string(instance.id)
	instance.log.add(call)
	return instance.failure(call)
}

func (instance *fakeInstance) failure(call string) error {
	instance.failureMu.Lock()
	defer instance.failureMu.Unlock()
	if instance.failAt == call {
		return errors.New("injected failure")
	}
	if instance.failOnceAt == call && !instance.failedOnce {
		instance.failedOnce = true
		return errors.New("injected one-time failure")
	}
	return nil
}

func countCalls(calls []string, target string) int {
	count := 0
	for _, call := range calls {
		if call == target {
			count++
		}
	}
	return count
}

type callLog struct {
	mu    sync.Mutex
	calls []string
}

func newCallLog() *callLog {
	return &callLog{}
}

func (log *callLog) add(call string) {
	log.mu.Lock()
	defer log.mu.Unlock()
	log.calls = append(log.calls, call)
}

func (log *callLog) snapshot() []string {
	log.mu.Lock()
	defer log.mu.Unlock()
	return slices.Clone(log.calls)
}
