package main

import (
	"log"
	"os"

	"github.com/gofiber/fiber/v3"
	wanworkapp "github.com/huapohen/quantum-entanglement/apps/im-api/internal/app"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/config"
)

func main() {
	settings, err := config.Load(os.LookupEnv)
	if err != nil {
		log.Fatal(err)
	}

	if err := wanworkapp.New().Listen(settings.ListenAddress(), fiber.ListenConfig{
		DisableStartupMessage: true,
	}); err != nil {
		log.Fatal(err)
	}
}
