package main

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/config"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/platform/postgres/migrationrun"
)

const (
	migrationURLVariable           = "WANWORK_IM_POSTGRES_MIGRATION_URL"
	authorityManifestVariable      = "WANWORK_IM_POSTGRES_AUTHORITY_MANIFEST"
	allowInsecureLocalTestVariable = "WANWORK_IM_POSTGRES_ALLOW_INSECURE_LOCAL_TEST"
)

var ErrInvalidCommandConfig = errors.New("invalid IM migration command config")

type lookupEnv func(string) (string, bool)

type migrationSummary struct {
	AppliedCount int    `json:"appliedCount"`
	Version      int64  `json:"version"`
	Name         string `json:"name"`
}

func main() {
	shutdown, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if err := run(shutdown, os.LookupEnv, os.Stdout); err != nil {
		log.Fatal(err)
	}
}

func run(ctx context.Context, lookup lookupEnv, output io.Writer) error {
	if ctx == nil || lookup == nil || output == nil {
		return ErrInvalidCommandConfig
	}
	migrationURL, _ := lookup(migrationURLVariable)
	manifestValue, _ := lookup(authorityManifestVariable)
	allowValue, _ := lookup(allowInsecureLocalTestVariable)
	if migrationURL == "" || manifestValue == "" {
		return ErrInvalidCommandConfig
	}
	allowInsecure := false
	switch allowValue {
	case "", "false":
	case "true":
		allowInsecure = true
	default:
		return ErrInvalidCommandConfig
	}
	manifest, err := config.ParseAuthorityManifestJSON(manifestValue)
	if err != nil {
		return ErrInvalidCommandConfig
	}
	state, err := migrationrun.Run(ctx, migrationrun.Config{
		ConnectionString:       migrationURL,
		Manifest:               manifest,
		ConnectTimeout:         5 * time.Second,
		AllowInsecureLocalhost: allowInsecure,
	})
	if err != nil {
		return err
	}
	summary := migrationSummary{AppliedCount: len(state.Applied)}
	if len(state.Applied) != 0 {
		latest := state.Applied[len(state.Applied)-1]
		summary.Version = latest.Version
		summary.Name = latest.Name
	}
	if err := json.NewEncoder(output).Encode(summary); err != nil {
		return ErrInvalidCommandConfig
	}
	return nil
}
