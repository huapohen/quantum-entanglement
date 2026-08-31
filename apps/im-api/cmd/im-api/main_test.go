package main

import (
	"context"
	"errors"
	"testing"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/auth"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/config"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/improjection"
)

func TestComposeKeepsDefaultZeroNetworkMode(t *testing.T) {
	settings, err := config.Load(func(string) (string, bool) { return "", false })
	if err != nil {
		t.Fatalf("load default composition: %v", err)
	}
	server, closeRuntime, err := compose(t.Context(), settings)
	if err != nil {
		t.Fatalf("compose default server: %v", err)
	}
	if server == nil || closeRuntime == nil {
		t.Fatal("default composition returned incomplete lifecycle")
	}
	closeRuntime()
}

func TestRejectAllVerifierHasNoAdmittedToken(t *testing.T) {
	verifier, err := newRejectAllVerifier()
	if err != nil {
		t.Fatalf("construct reject-all verifier: %v", err)
	}
	defer verifier.Close()
	if verifier.Profile().Realm.IsZero() {
		t.Fatal("reject-all verifier profile has no realm")
	}
	_, err = verifier.Verify(context.Background(), auth.VerifyRequest{
		BearerToken: "header.payload.signature",
	})
	if !errors.Is(err, auth.ErrInvalidToken) {
		t.Fatalf("unconfigured token error = %v, want %v", err, auth.ErrInvalidToken)
	}
}

func TestJoinedReadinessStopsOnDatabaseAndLatchesShadowMismatch(t *testing.T) {
	primaryCalls, shadowCalls := 0, 0
	primaryErr := errors.New("database unavailable")
	readiness := joinedReadiness{
		primary: readinessProbeFunc(func(context.Context) error {
			primaryCalls++
			return primaryErr
		}),
		shadow: readinessProbeFunc(func(context.Context) error {
			shadowCalls++
			return improjection.ErrShadowUnhealthy
		}),
	}
	if err := readiness.Ready(context.Background()); !errors.Is(err, primaryErr) {
		t.Fatalf("primary readiness error=%v", err)
	}
	if primaryCalls != 1 || shadowCalls != 0 {
		t.Fatalf("readiness calls primary=%d shadow=%d", primaryCalls, shadowCalls)
	}
	readiness.primary = readinessProbeFunc(func(context.Context) error {
		primaryCalls++
		return nil
	})
	if err := readiness.Ready(context.Background()); !errors.Is(err, improjection.ErrShadowUnhealthy) {
		t.Fatalf("shadow readiness error=%v", err)
	}
	if primaryCalls != 2 || shadowCalls != 1 {
		t.Fatalf("readiness calls primary=%d shadow=%d", primaryCalls, shadowCalls)
	}
}

type readinessProbeFunc func(context.Context) error

func (probe readinessProbeFunc) Ready(ctx context.Context) error { return probe(ctx) }
