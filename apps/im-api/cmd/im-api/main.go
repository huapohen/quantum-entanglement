package main

import (
	"context"
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/gofiber/fiber/v3"
	wanworkapp "github.com/huapohen/quantum-entanglement/apps/im-api/internal/app"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/config"
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
	server, err := wanworkapp.NewRuntime(wanworkapp.RuntimeDependencies{
		Database:    pool,
		Persistence: persistence,
	})
	if err != nil {
		closeRuntime()
		return nil, nil, err
	}
	return server, closeRuntime, nil
}
