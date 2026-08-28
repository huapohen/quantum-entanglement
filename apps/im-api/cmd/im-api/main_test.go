package main

import (
	"testing"

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
