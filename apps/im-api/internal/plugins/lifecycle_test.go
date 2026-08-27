package plugins

import (
	"context"
	"errors"
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

func TestHostRejectsMissingFactoryAndRepeatedStart(t *testing.T) {
	t.Parallel()

	manifest := testManifest("im.fake.v1", []PortID{"im.transport.v1"}, nil)
	registry := NewRegistry()
	if err := registry.Register(manifest, admittedPackage(manifest)); err != nil {
		t.Fatalf("register manifest: %v", err)
	}
	host := NewHost(registry, nil)
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
	factory := &fakeFactory{
		manifest: manifest,
		log:      log,
		blockAt:  "start:slow.fake.v1",
	}
	if err := registry.RegisterFactory(factory, admittedPackage(manifest)); err != nil {
		t.Fatalf("register factory: %v", err)
	}
	host := NewHost(registry, nil)
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
	for _, manifest := range manifests {
		factory := &fakeFactory{manifest: manifest, log: log, failAt: failAt}
		if err := registry.RegisterFactory(factory, admittedPackage(manifest)); err != nil {
			t.Fatalf("register %s: %v", manifest.ID, err)
		}
	}
	return NewHost(registry, nil)
}

type fakeFactory struct {
	manifest Manifest
	log      *callLog
	failAt   string
	blockAt  string
}

func (factory *fakeFactory) Manifest() Manifest {
	return factory.manifest
}

func (factory *fakeFactory) Configure(PluginConfig) (Instance, error) {
	factory.log.add("configure:" + string(factory.manifest.ID))
	return &fakeInstance{
		id:      factory.manifest.ID,
		log:     factory.log,
		failAt:  factory.failAt,
		blockAt: factory.blockAt,
	}, nil
}

type fakeInstance struct {
	id      PluginID
	log     *callLog
	failAt  string
	blockAt string
}

func (instance *fakeInstance) Start(ctx context.Context, effects Effects) error {
	call := "start:" + string(instance.id)
	instance.log.add(call)
	if err := effects.Defer("resource", func(context.Context) error {
		cleanupCall := "cleanup:" + string(instance.id)
		instance.log.add(cleanupCall)
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
	if instance.failAt == call {
		return errors.New("injected failure")
	}
	return nil
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
