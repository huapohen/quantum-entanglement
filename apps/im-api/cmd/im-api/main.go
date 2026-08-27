package main

import (
	"log"
	"os"

	"github.com/gofiber/fiber/v3"
	wanworkapp "github.com/huapohen/quantum-entanglement/apps/im-api/internal/app"
)

const defaultListenAddress = "127.0.0.1:18080"

func main() {
	listenAddress := os.Getenv("WANWORK_IM_LISTEN_ADDRESS")
	if listenAddress == "" {
		listenAddress = defaultListenAddress
	}

	if err := wanworkapp.New().Listen(listenAddress, fiber.ListenConfig{
		DisableStartupMessage: true,
	}); err != nil {
		log.Fatal(err)
	}
}
