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

func TestLifecycleCallbacksObserveStateWithoutHostLockAndRejectReentrancy(t *testing.T) {
	t.Parallel()

	observer := &lifecycleStateObserver{}
	host := directLifecycleHost(lifecycleFactoryFunc(func(PluginConfig) (Instance, error) {
		if err := observer.observe("configure", HostStateStarting); err != nil {
			return nil, err
		}
		return &observingLifecycleInstance{observer: observer}, nil
	}))
	observer.host = host

	startDone := make(chan error, 1)
	go func() {
		startDone <- host.Start(context.Background())
	}()
	if err := awaitLifecycleResult(t, "start callbacks", startDone); err != nil {
		t.Fatalf("start host: %v", err)
	}
	stopDone := make(chan error, 1)
	go func() {
		stopDone <- host.Stop(context.Background())
	}()
	if err := awaitLifecycleResult(t, "stop callbacks", stopDone); err != nil {
		t.Fatalf("stop host: %v", err)
	}

	want := []string{"configure", "start", "ready", "drain", "stop", "cleanup"}
	if observed := observer.snapshot(); !slices.Equal(observed, want) {
		t.Fatalf("observed callbacks = %v, want %v", observed, want)
	}
}

func TestBlockedStartKeepsLifecycleStateObservableAndSingleOwned(t *testing.T) {
	t.Parallel()

	instance := newBlockingLifecycleInstance()
	host := directLifecycleHost(lifecycleFactoryFunc(func(PluginConfig) (Instance, error) {
		return instance, nil
	}))
	startDone := make(chan error, 1)
	go func() {
		startDone <- host.Start(context.Background())
	}()
	awaitLifecycleSignal(t, "plugin start", instance.startEntered)

	if state := host.State(); state != HostStateStarting {
		t.Fatalf("state during blocked start = %q, want %q", state, HostStateStarting)
	}
	concurrentStart := make(chan error, 1)
	go func() {
		concurrentStart <- host.Start(context.Background())
	}()
	if err := awaitLifecycleResult(t, "concurrent start", concurrentStart); !errors.Is(err, ErrInvalidLifecycle) {
		t.Fatalf("concurrent start error = %v, want %v", err, ErrInvalidLifecycle)
	}
	concurrentStop := make(chan error, 1)
	go func() {
		concurrentStop <- host.Stop(context.Background())
	}()
	if err := awaitLifecycleResult(t, "stop during start", concurrentStop); !errors.Is(err, ErrInvalidLifecycle) {
		t.Fatalf("stop during start error = %v, want %v", err, ErrInvalidLifecycle)
	}

	close(instance.startRelease)
	if err := awaitLifecycleResult(t, "outer start", startDone); err != nil {
		t.Fatalf("outer start: %v", err)
	}
	close(instance.drainRelease)
	if err := host.Stop(context.Background()); err != nil {
		t.Fatalf("stop host: %v", err)
	}
}

func TestBlockedStopKeepsLifecycleStateObservableAndSingleOwned(t *testing.T) {
	t.Parallel()

	instance := newBlockingLifecycleInstance()
	close(instance.startRelease)
	host := directLifecycleHost(lifecycleFactoryFunc(func(PluginConfig) (Instance, error) {
		return instance, nil
	}))
	if err := host.Start(context.Background()); err != nil {
		t.Fatalf("start host: %v", err)
	}

	stopDone := make(chan error, 1)
	go func() {
		stopDone <- host.Stop(context.Background())
	}()
	awaitLifecycleSignal(t, "plugin drain", instance.drainEntered)
	if state := host.State(); state != HostStateStopping {
		t.Fatalf("state during blocked stop = %q, want %q", state, HostStateStopping)
	}
	concurrentStop := make(chan error, 1)
	go func() {
		concurrentStop <- host.Stop(context.Background())
	}()
	if err := awaitLifecycleResult(t, "concurrent stop", concurrentStop); !errors.Is(err, ErrInvalidLifecycle) {
		t.Fatalf("concurrent stop error = %v, want %v", err, ErrInvalidLifecycle)
	}
	if calls := instance.drainCalls(); calls != 1 {
		t.Fatalf("drain calls while blocked = %d, want 1", calls)
	}

	close(instance.drainRelease)
	if err := awaitLifecycleResult(t, "outer stop", stopDone); err != nil {
		t.Fatalf("outer stop: %v", err)
	}
	if calls := instance.drainCalls(); calls != 1 {
		t.Fatalf("final drain calls = %d, want 1", calls)
	}
}

func TestRollbackCallbacksObserveStoppingStateWithoutHostLock(t *testing.T) {
	t.Parallel()

	observer := &lifecycleStateObserver{}
	host := directLifecycleHost(lifecycleFactoryFunc(func(PluginConfig) (Instance, error) {
		return &rollbackObservingInstance{observer: observer}, nil
	}))
	observer.host = host
	startDone := make(chan error, 1)
	go func() {
		startDone <- host.Start(context.Background())
	}()
	if err := awaitLifecycleResult(t, "rollback callbacks", startDone); err == nil ||
		!strings.Contains(err.Error(), "force rollback") {
		t.Fatalf("start error = %v, want forced rollback", err)
	}
	if state := host.State(); state != HostStateFailed {
		t.Fatalf("state after rollback = %q, want %q", state, HostStateFailed)
	}
	want := []string{"drain", "stop", "cleanup"}
	if observed := observer.snapshot(); !slices.Equal(observed, want) {
		t.Fatalf("rollback callbacks = %v, want %v", observed, want)
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
	successes := 0
	if err := scope.Defer("success", func(context.Context) error {
		successes++
		return nil
	}); err != nil {
		t.Fatalf("register successful cleanup: %v", err)
	}
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
	if err := scope.Defer("late", func(context.Context) error { return nil }); !errors.Is(err, ErrInvalidEffect) {
		t.Fatalf("registration after failed cleanup error = %v, want %v", err, ErrInvalidEffect)
	}
	if scope.state != effectScopeClosing || successes != 1 {
		t.Fatalf("failed cleanup state/successes = %q/%d", scope.state, successes)
	}
	if err := scope.cleanup(context.Background(), time.Second); err != nil {
		t.Fatalf("retry cleanup: %v", err)
	}
	if attempts != 2 || successes != 1 || scope.state != effectScopeClosed {
		t.Fatalf("cleanup attempts/successes/state = %d/%d/%q", attempts, successes, scope.state)
	}
}

func TestEffectScopeRejectsLateRegistrationDuringAndAfterCleanup(t *testing.T) {
	t.Parallel()

	scope := newEffectScope()
	started := make(chan struct{})
	release := make(chan struct{})
	if err := scope.Defer("held", func(context.Context) error {
		close(started)
		<-release
		return nil
	}); err != nil {
		t.Fatalf("register held cleanup: %v", err)
	}
	scope.beginClosing()
	if err := scope.Defer("during-drain", func(context.Context) error { return nil }); !errors.Is(err, ErrInvalidEffect) {
		t.Fatalf("registration after closing error = %v, want %v", err, ErrInvalidEffect)
	}
	done := make(chan error, 1)
	go func() {
		done <- scope.cleanup(context.Background(), time.Second)
	}()
	<-started
	if err := scope.Defer("during-cleanup", func(context.Context) error { return nil }); !errors.Is(err, ErrInvalidEffect) {
		t.Fatalf("registration during cleanup error = %v, want %v", err, ErrInvalidEffect)
	}
	if err := scope.cleanup(context.Background(), time.Second); !errors.Is(err, ErrInvalidEffect) {
		t.Fatalf("concurrent cleanup error = %v, want %v", err, ErrInvalidEffect)
	}
	close(release)
	if err := <-done; err != nil {
		t.Fatalf("outer cleanup: %v", err)
	}
	if err := scope.Defer("after-close", func(context.Context) error { return nil }); !errors.Is(err, ErrInvalidEffect) {
		t.Fatalf("registration after close error = %v, want %v", err, ErrInvalidEffect)
	}
	if err := scope.cleanup(context.Background(), time.Second); err != nil {
		t.Fatalf("closed cleanup should be idempotent: %v", err)
	}
}

func TestEffectScopeRejectsRecursiveCleanup(t *testing.T) {
	t.Parallel()

	scope := newEffectScope()
	var recursiveErr error
	if err := scope.Defer("recursive", func(context.Context) error {
		recursiveErr = scope.cleanup(context.Background(), time.Second)
		return nil
	}); err != nil {
		t.Fatalf("register recursive cleanup: %v", err)
	}
	if err := scope.cleanup(context.Background(), time.Second); err != nil {
		t.Fatalf("outer cleanup: %v", err)
	}
	if !errors.Is(recursiveErr, ErrInvalidEffect) || scope.state != effectScopeClosed {
		t.Fatalf("recursive error/state = %v/%q", recursiveErr, scope.state)
	}
}

func TestStopPluginsClosesEffectRegistrationBeforeDrain(t *testing.T) {
	t.Parallel()

	scope := newEffectScope()
	cleaned := 0
	if err := scope.Defer("initial", func(context.Context) error {
		cleaned++
		return nil
	}); err != nil {
		t.Fatalf("register initial cleanup: %v", err)
	}
	instance := &shutdownRegistrationInstance{effects: scope}
	err := stopPlugins(context.Background(), []runningPlugin{{
		id: "shutdown.fake.v1", instance: instance,
		timeouts: LifecycleTimeouts{Drain: time.Second, Stop: time.Second}, effects: scope,
	}})
	if err != nil {
		t.Fatalf("stop plugins: %v", err)
	}
	if !errors.Is(instance.registrationErr, ErrInvalidEffect) || cleaned != 1 ||
		scope.state != effectScopeClosed {
		t.Fatalf("late registration/cleaned/state = %v/%d/%q", instance.registrationErr, cleaned, scope.state)
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
	freezeRegistryForTest(t, driftRegistry)
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
	freezeRegistryForTest(t, admissionRegistry)
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
	freezeRegistryForTest(t, registry)
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

type shutdownRegistrationInstance struct {
	effects         Effects
	registrationErr error
}

func (*shutdownRegistrationInstance) Start(context.Context, Effects) error { return nil }
func (*shutdownRegistrationInstance) Ready(context.Context) error          { return nil }
func (instance *shutdownRegistrationInstance) Drain(context.Context) error {
	instance.registrationErr = instance.effects.Defer("late", func(context.Context) error { return nil })
	return nil
}
func (*shutdownRegistrationInstance) Stop(context.Context) error { return nil }

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

type lifecycleFactoryFunc func(PluginConfig) (Instance, error)

func (lifecycleFactoryFunc) Manifest() Manifest {
	return Manifest{}
}

func (factory lifecycleFactoryFunc) Configure(config PluginConfig) (Instance, error) {
	return factory(config)
}

func directLifecycleHost(factory Factory) *Host {
	return &Host{
		activation: []activationEntry{{
			id:      "direct.fake.v1",
			factory: factory,
			timeouts: LifecycleTimeouts{
				Start: time.Second,
				Ready: time.Second,
				Drain: time.Second,
				Stop:  time.Second,
			},
		}},
		state: HostStateNew,
	}
}

type lifecycleStateObserver struct {
	host *Host
	mu   sync.Mutex
	seen []string
}

func (observer *lifecycleStateObserver) observe(callback string, want HostState) error {
	if state := observer.host.State(); state != want {
		return fmt.Errorf("%s observed state %q, want %q", callback, state, want)
	}
	if err := observer.host.Start(context.Background()); !errors.Is(err, ErrInvalidLifecycle) {
		return fmt.Errorf("%s reentrant start: %w", callback, err)
	}
	if err := observer.host.Stop(context.Background()); !errors.Is(err, ErrInvalidLifecycle) {
		return fmt.Errorf("%s reentrant stop: %w", callback, err)
	}
	observer.mu.Lock()
	defer observer.mu.Unlock()
	observer.seen = append(observer.seen, callback)
	return nil
}

func (observer *lifecycleStateObserver) snapshot() []string {
	observer.mu.Lock()
	defer observer.mu.Unlock()
	return slices.Clone(observer.seen)
}

type observingLifecycleInstance struct {
	observer *lifecycleStateObserver
}

func (instance *observingLifecycleInstance) Start(_ context.Context, effects Effects) error {
	if err := instance.observer.observe("start", HostStateStarting); err != nil {
		return err
	}
	return effects.Defer("observe-cleanup", func(context.Context) error {
		return instance.observer.observe("cleanup", HostStateStopping)
	})
}

func (instance *observingLifecycleInstance) Ready(context.Context) error {
	return instance.observer.observe("ready", HostStateStarting)
}

func (instance *observingLifecycleInstance) Drain(context.Context) error {
	return instance.observer.observe("drain", HostStateStopping)
}

func (instance *observingLifecycleInstance) Stop(context.Context) error {
	return instance.observer.observe("stop", HostStateStopping)
}

type blockingLifecycleInstance struct {
	startEntered chan struct{}
	startRelease chan struct{}
	drainEntered chan struct{}
	drainRelease chan struct{}
	drainMu      sync.Mutex
	drains       int
}

func newBlockingLifecycleInstance() *blockingLifecycleInstance {
	return &blockingLifecycleInstance{
		startEntered: make(chan struct{}),
		startRelease: make(chan struct{}),
		drainEntered: make(chan struct{}),
		drainRelease: make(chan struct{}),
	}
}

func (instance *blockingLifecycleInstance) Start(context.Context, Effects) error {
	close(instance.startEntered)
	<-instance.startRelease
	return nil
}

func (*blockingLifecycleInstance) Ready(context.Context) error { return nil }

func (instance *blockingLifecycleInstance) Drain(context.Context) error {
	instance.drainMu.Lock()
	instance.drains++
	instance.drainMu.Unlock()
	close(instance.drainEntered)
	<-instance.drainRelease
	return nil
}

func (*blockingLifecycleInstance) Stop(context.Context) error { return nil }

func (instance *blockingLifecycleInstance) drainCalls() int {
	instance.drainMu.Lock()
	defer instance.drainMu.Unlock()
	return instance.drains
}

type rollbackObservingInstance struct {
	observer *lifecycleStateObserver
}

func (instance *rollbackObservingInstance) Start(_ context.Context, effects Effects) error {
	if err := effects.Defer("observe-rollback-cleanup", func(context.Context) error {
		return instance.observer.observe("cleanup", HostStateStopping)
	}); err != nil {
		return err
	}
	return errors.New("force rollback")
}

func (*rollbackObservingInstance) Ready(context.Context) error { return nil }

func (instance *rollbackObservingInstance) Drain(context.Context) error {
	return instance.observer.observe("drain", HostStateStopping)
}

func (instance *rollbackObservingInstance) Stop(context.Context) error {
	return instance.observer.observe("stop", HostStateStopping)
}

func awaitLifecycleSignal(t *testing.T, label string, signal <-chan struct{}) {
	t.Helper()
	select {
	case <-signal:
	case <-time.After(2 * time.Second):
		t.Fatalf("timed out waiting for %s", label)
	}
}

func awaitLifecycleResult(t *testing.T, label string, result <-chan error) error {
	t.Helper()
	select {
	case err := <-result:
		return err
	case <-time.After(2 * time.Second):
		t.Fatalf("timed out waiting for %s", label)
		return nil
	}
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
