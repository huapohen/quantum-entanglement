package main

import (
	"context"
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/gofiber/fiber/v3"
	authfake "github.com/huapohen/quantum-entanglement/apps/im-api/internal/adapters/auth/fake"
	wanworkapp "github.com/huapohen/quantum-entanglement/apps/im-api/internal/app"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/config"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
	postgresstore "github.com/huapohen/quantum-entanglement/apps/im-api/internal/platform/postgres/imstore"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/platform/postgres/runtimepool"
)

func main() {
	shutdown, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if err := run(shutdown, os.LookupEnv); err != nil {
		log.Fatal(err)
	}
}

func run(ctx context.Context, lookup config.LookupEnv) error {
	settings, err := config.Load(lookup)
	if err != nil {
		return err
	}
	server, closeRuntime, err := compose(ctx, settings)
	if err != nil {
		return err
	}
	defer closeRuntime()

	return server.Listen(settings.ListenAddress(), fiber.ListenConfig{
		DisableStartupMessage: true,
		GracefulContext:       ctx,
		ShutdownTimeout:       10 * time.Second,
	})
}

func compose(
	ctx context.Context,
	settings config.Config,
) (*fiber.App, func(), error) {
	poolConfig, configured := settings.RuntimePostgres()
	if !configured {
		server, err := wanworkapp.NewLocalDemo()
		if err != nil {
			return nil, nil, err
		}
		return server, func() {}, nil
	}
	pool, err := runtimepool.Open(ctx, poolConfig)
	if err != nil {
		return nil, nil, err
	}
	closeRuntime := func() { pool.Close() }
	persistence, err := postgresstore.NewUnitOfWork(pool)
	if err != nil {
		closeRuntime()
		return nil, nil, err
	}
	verifier, err := newRejectAllVerifier()
	if err != nil {
		closeRuntime()
		return nil, nil, err
	}
	server, err := wanworkapp.NewRuntime(wanworkapp.RuntimeDependencies{
		Database:    pool,
		Persistence: persistence,
		Verifier:    verifier,
	})
	if err != nil {
		verifier.Close()
		closeRuntime()
		return nil, nil, err
	}
	return server, func() {
		verifier.Close()
		closeRuntime()
	}, nil
}

// newRejectAllVerifier keeps the PostgreSQL composition honest until a reviewed Clerk/JWKS
// adapter is available. It has a valid provider profile but an empty fixture set, so every API
// bearer token is rejected while health/readiness can still be exercised locally.
func newRejectAllVerifier() (*authfake.Verifier, error) {
	realm, err := im.ParseProviderRealmID("rlm_runtime_unconfigured")
	if err != nil {
		return nil, err
	}
	return authfake.New(authfake.Options{
		Realm: realm, Issuer: "clerk.runtime.unconfigured", Audience: "wanwork.runtime.unconfigured",
		Tokens: map[string]authfake.TokenFixture{},
	})
}
