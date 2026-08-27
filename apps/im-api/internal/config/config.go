package config

import (
	"errors"
	"net"
	"strconv"
)

const (
	listenAddressVariable = "WANWORK_IM_LISTEN_ADDRESS"
	defaultListenAddress  = "127.0.0.1:18080"
)

var (
	ErrInvalidListenAddress = errors.New("listen address must be a numeric loopback host and valid port")
	ErrUnsafeComposition    = errors.New("only the local fake composition is admitted in this stage")
)

type ProviderID string

const (
	ProviderFakeAuth ProviderID = "auth.fake.v1"
	ProviderFakeIM   ProviderID = "im.fake.v1"
)

type OutboundMode string

const OutboundDisabled OutboundMode = "disabled"

// Config is immutable outside this package. It intentionally contains no endpoint, credential,
// secret value, or ambient provider selection.
type Config struct {
	listenAddress string
	authProvider  ProviderID
	imProvider    ProviderID
	outboundMode  OutboundMode
}

type PublicSnapshot struct {
	ListenAddress string       `json:"listenAddress"`
	AuthProvider  ProviderID   `json:"authProvider"`
	IMProvider    ProviderID   `json:"imProvider"`
	OutboundMode  OutboundMode `json:"outboundMode"`
}

type LookupEnv func(string) (string, bool)

func Load(lookup LookupEnv) (Config, error) {
	config := defaultConfig()
	if value, ok := lookup(listenAddressVariable); ok && value != "" {
		config.listenAddress = value
	}
	if err := config.validate(); err != nil {
		return Config{}, err
	}
	return config, nil
}

func (config Config) ListenAddress() string {
	return config.listenAddress
}

func (config Config) Snapshot() PublicSnapshot {
	return PublicSnapshot{
		ListenAddress: config.listenAddress,
		AuthProvider:  config.authProvider,
		IMProvider:    config.imProvider,
		OutboundMode:  config.outboundMode,
	}
}

func defaultConfig() Config {
	return Config{
		listenAddress: defaultListenAddress,
		authProvider:  ProviderFakeAuth,
		imProvider:    ProviderFakeIM,
		outboundMode:  OutboundDisabled,
	}
}

func (config Config) validate() error {
	if config.authProvider != ProviderFakeAuth ||
		config.imProvider != ProviderFakeIM ||
		config.outboundMode != OutboundDisabled {
		return ErrUnsafeComposition
	}

	host, portText, err := net.SplitHostPort(config.listenAddress)
	if err != nil {
		return ErrInvalidListenAddress
	}
	address := net.ParseIP(host)
	if address == nil || !address.IsLoopback() {
		return ErrInvalidListenAddress
	}
	port, err := strconv.Atoi(portText)
	if err != nil || port < 1 || port > 65535 {
		return ErrInvalidListenAddress
	}
	return nil
}
