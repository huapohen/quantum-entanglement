package main

import (
	"context"
	"errors"
	"testing"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/auth"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/config"
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
