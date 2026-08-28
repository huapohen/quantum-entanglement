package config

import (
	"encoding/json"
	"errors"
	"io"
	"net"
	"strconv"
	"strings"
	"time"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/platform/postgres/migrations"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/platform/postgres/runtimepool"
)

const (
	listenAddressVariable                  = "WANWORK_IM_LISTEN_ADDRESS"
	postgresMigrationURLVariable           = "WANWORK_IM_POSTGRES_MIGRATION_URL"
	postgresRuntimeURLVariable             = "WANWORK_IM_POSTGRES_RUNTIME_URL"
	postgresAuthorityManifestVariable      = "WANWORK_IM_POSTGRES_AUTHORITY_MANIFEST"
	postgresAllowInsecureLocalTestVariable = "WANWORK_IM_POSTGRES_ALLOW_INSECURE_LOCAL_TEST"
	defaultListenAddress                   = "127.0.0.1:18080"
)

var (
	ErrInvalidListenAddress = errors.New("listen address must be a numeric loopback host and valid port")
	ErrUnsafeComposition    = errors.New("only fake external providers with disabled outbound are admitted")
	ErrInvalidPostgres      = errors.New("invalid PostgreSQL runtime composition")
	ErrMigrationCredential  = errors.New("PostgreSQL migration credential is not admitted by the API")
)

type ProviderID string

const (
	ProviderFakeAuth ProviderID = "auth.fake.v1"
	ProviderFakeIM   ProviderID = "im.fake.v1"
)

type OutboundMode string

const OutboundDisabled OutboundMode = "disabled"

type PostgresMode string

const (
	PostgresDisabled PostgresMode = "disabled"
	PostgresRuntime  PostgresMode = "runtime"
)

// Config is immutable outside this package. It intentionally contains no endpoint, credential,
// secret value, or ambient provider selection.
type Config struct {
	listenAddress string
	authProvider  ProviderID
	imProvider    ProviderID
	outboundMode  OutboundMode
	runtimePool   *runtimepool.Config
}

type PublicSnapshot struct {
	ListenAddress string       `json:"listenAddress"`
	AuthProvider  ProviderID   `json:"authProvider"`
	IMProvider    ProviderID   `json:"imProvider"`
	OutboundMode  OutboundMode `json:"outboundMode"`
	PostgresMode  PostgresMode `json:"postgresMode"`
}

type authorityManifestJSON struct {
	DatabaseName        string   `json:"databaseName"`
	DatabaseOwnerRole   string   `json:"databaseOwnerRole"`
	OwnerRole           string   `json:"ownerRole"`
	MigratorRole        string   `json:"migratorRole"`
	RuntimeRole         string   `json:"runtimeRole"`
	MigrationLoginRoles []string `json:"migrationLoginRoles"`
	RuntimeLoginRoles   []string `json:"runtimeLoginRoles"`
}

type LookupEnv func(string) (string, bool)

func Load(lookup LookupEnv) (Config, error) {
	config := defaultConfig()
	if value, ok := lookup(listenAddressVariable); ok && value != "" {
		config.listenAddress = value
	}
	// The API process must not be launched from an environment that contains the one-shot
	// migrator credential, even when the variable is present with an empty value. Inspect only
	// presence and never retain, log, or return the value.
	if _, ok := lookup(postgresMigrationURLVariable); ok {
		return Config{}, ErrMigrationCredential
	}
	if err := config.loadPostgres(lookup); err != nil {
		return Config{}, err
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
	postgresMode := PostgresDisabled
	if config.runtimePool != nil {
		postgresMode = PostgresRuntime
	}
	return PublicSnapshot{
		ListenAddress: config.listenAddress,
		AuthProvider:  config.authProvider,
		IMProvider:    config.imProvider,
		OutboundMode:  config.outboundMode,
		PostgresMode:  postgresMode,
	}
}

// RuntimePostgres returns a detached copy of private runtime composition. Callers must not log,
// serialize, or expose it through diagnostics.
func (config Config) RuntimePostgres() (runtimepool.Config, bool) {
	if config.runtimePool == nil {
		return runtimepool.Config{}, false
	}
	value := *config.runtimePool
	value.Manifest.MigrationLoginRoles = append([]string(nil), value.Manifest.MigrationLoginRoles...)
	value.Manifest.RuntimeLoginRoles = append([]string(nil), value.Manifest.RuntimeLoginRoles...)
	return value, true
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
	if config.runtimePool != nil &&
		(strings.TrimSpace(config.runtimePool.ConnectionString) == "" ||
			config.runtimePool.Manifest.Validate() != nil) {
		return ErrInvalidPostgres
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

func (config *Config) loadPostgres(lookup LookupEnv) error {
	runtimeURL, _ := lookup(postgresRuntimeURLVariable)
	manifestValue, _ := lookup(postgresAuthorityManifestVariable)
	allowInsecureValue, _ := lookup(postgresAllowInsecureLocalTestVariable)
	if runtimeURL == "" {
		if manifestValue != "" || allowInsecureValue != "" {
			return ErrInvalidPostgres
		}
		return nil
	}
	if manifestValue == "" {
		return ErrInvalidPostgres
	}
	allowInsecure := false
	switch allowInsecureValue {
	case "", "false":
	case "true":
		allowInsecure = true
	default:
		return ErrInvalidPostgres
	}
	manifest, err := ParseAuthorityManifestJSON(manifestValue)
	if err != nil {
		return err
	}
	config.runtimePool = &runtimepool.Config{
		ConnectionString:       runtimeURL,
		Manifest:               manifest,
		MaxConnections:         8,
		MinIdleConnections:     1,
		ConnectTimeout:         3 * time.Second,
		PingTimeout:            time.Second,
		AllowInsecureLocalhost: allowInsecure,
	}
	return nil
}

// ParseAuthorityManifestJSON decodes the non-secret exact access manifest shared by the API and
// the one-shot migrator. It rejects unknown fields, trailing values, and non-canonical identities.
func ParseAuthorityManifestJSON(value string) (migrations.AuthorityAccessManifest, error) {
	decoder := json.NewDecoder(strings.NewReader(value))
	decoder.DisallowUnknownFields()
	var decoded authorityManifestJSON
	if err := decoder.Decode(&decoded); err != nil {
		return migrations.AuthorityAccessManifest{}, ErrInvalidPostgres
	}
	var trailing any
	if err := decoder.Decode(&trailing); !errors.Is(err, io.EOF) {
		return migrations.AuthorityAccessManifest{}, ErrInvalidPostgres
	}
	manifest := migrations.AuthorityAccessManifest{
		DatabaseName:        decoded.DatabaseName,
		DatabaseOwnerRole:   decoded.DatabaseOwnerRole,
		OwnerRole:           decoded.OwnerRole,
		MigratorRole:        decoded.MigratorRole,
		RuntimeRole:         decoded.RuntimeRole,
		MigrationLoginRoles: append([]string(nil), decoded.MigrationLoginRoles...),
		RuntimeLoginRoles:   append([]string(nil), decoded.RuntimeLoginRoles...),
	}
	if manifest.Validate() != nil {
		return migrations.AuthorityAccessManifest{}, ErrInvalidPostgres
	}
	return manifest, nil
}
