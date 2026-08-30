package runtimepool

import (
	"errors"
	"testing"
)

func TestOpenAndPoolOperationsRejectInvalidContextOrPool(t *testing.T) {
	if _, err := Open(nil, validConfig()); !errors.Is(err, ErrInvalidConfig) {
		t.Fatalf("nil Open context error = %v, want %v", err, ErrInvalidConfig)
	}
	var pool *Pool
	if _, err := pool.Acquire(t.Context()); !errors.Is(err, ErrNotReady) {
		t.Fatalf("nil pool Acquire error = %v, want %v", err, ErrNotReady)
	}
	if err := pool.Ready(t.Context()); !errors.Is(err, ErrNotReady) {
		t.Fatalf("nil pool Ready error = %v, want %v", err, ErrNotReady)
	}
	pool.Close()
}

func TestCloneManifestDetachesMutableRoleLists(t *testing.T) {
	input := validConfig().Manifest
	cloned := cloneManifest(input)
	input.MigrationLoginRoles[0] = "changed_migration"
	input.RuntimeLoginRoles[0] = "changed_runtime"
	if cloned.MigrationLoginRoles[0] == input.MigrationLoginRoles[0] ||
		cloned.RuntimeLoginRoles[0] == input.RuntimeLoginRoles[0] {
		t.Fatal("manifest role lists alias caller-owned slices")
	}
}
