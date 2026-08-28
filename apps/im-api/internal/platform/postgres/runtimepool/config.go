// Package runtimepool constructs PostgreSQL pools whose connections are continuously bound to
// the exact WanWork IM runtime authority. It never accepts migration or owner credentials.
package runtimepool

import (
	"errors"
	"net"
	"net/url"
	"slices"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/platform/postgres/migrations"
)

var (
	ErrInvalidConfig   = errors.New("invalid PostgreSQL runtime pool config")
	ErrUnsafeTransport = errors.New("PostgreSQL runtime pool transport is not admitted")
)

const maximumConnections int32 = 100

var connectionStringAllowedKeys = []string{
	"database",
	"dbname",
	"host",
	"password",
	"port",
	"sslcert",
	"sslkey",
	"sslmode",
	"sslpassword",
	"sslrootcert",
	"user",
}

// Config is private runtime composition input. ConnectionString can contain a credential and
// therefore must never be logged, serialized, exposed through diagnostics, or included in an
// error. AllowInsecureLocalhost is only for disposable local integration tests.
type Config struct {
	ConnectionString       string
	Manifest               migrations.AuthorityAccessManifest
	MaxConnections         int32
	MinIdleConnections     int32
	ConnectTimeout         time.Duration
	PingTimeout            time.Duration
	AllowInsecureLocalhost bool
}

func parseConfig(input Config) (*pgxpool.Config, error) {
	if strings.TrimSpace(input.ConnectionString) == "" || input.Manifest.Validate() != nil ||
		input.MaxConnections < 1 || input.MaxConnections > maximumConnections ||
		input.MinIdleConnections < 0 || input.MinIdleConnections > input.MaxConnections ||
		input.ConnectTimeout <= 0 || input.PingTimeout <= 0 {
		return nil, ErrInvalidConfig
	}
	if !strictConnectionStringShape(input.ConnectionString) {
		return nil, ErrInvalidConfig
	}

	// pgx and pgxpool consume and remove some query, cache, and pool settings during parsing.
	// Validate the original connection string first so those settings cannot evade the final
	// RuntimeParams check. Only endpoint, identity, credential, and TLS material belong in the
	// private DSN; lifecycle and query behavior stay host-owned.
	if _, err := pgx.ParseConfigWithOptions(input.ConnectionString, pgx.ParseConfigOptions{
		ParseConfigOptions: pgconn.ParseConfigOptions{
			ConnStringAllowedKeys: connectionStringAllowedKeys,
		},
	}); err != nil {
		return nil, ErrInvalidConfig
	}
	parsed, err := pgxpool.ParseConfig(input.ConnectionString)
	if err != nil {
		// pgx parse errors can retain the connection string. Do not wrap or return them.
		return nil, ErrInvalidConfig
	}
	connection := &parsed.ConnConfig.Config
	if connection.Database != input.Manifest.DatabaseName ||
		!slices.Contains(input.Manifest.RuntimeLoginRoles, connection.User) ||
		len(connection.RuntimeParams) != 0 {
		return nil, ErrInvalidConfig
	}
	if !transportAdmitted(connection, input.AllowInsecureLocalhost) {
		return nil, ErrUnsafeTransport
	}

	connection.ConnectTimeout = input.ConnectTimeout
	parsed.MaxConns = input.MaxConnections
	parsed.MinConns = 0
	parsed.MinIdleConns = input.MinIdleConnections
	parsed.PingTimeout = input.PingTimeout
	return parsed, nil
}

func strictConnectionStringShape(connectionString string) bool {
	parsed, err := url.Parse(connectionString)
	if err != nil || (parsed.Scheme != "postgres" && parsed.Scheme != "postgresql") ||
		parsed.Opaque != "" || parsed.Fragment != "" || parsed.User == nil ||
		parsed.User.Username() == "" || parsed.Path == "" || parsed.Path == "/" {
		return false
	}
	query := parsed.Query()
	if len(query["sslmode"]) != 1 || query.Get("sslmode") == "" ||
		query.Has("user") || query.Has("password") || query.Has("database") || query.Has("dbname") {
		return false
	}
	if parsed.Hostname() != "" {
		return parsed.Port() != "" && !query.Has("host") && !query.Has("port")
	}
	return len(query["host"]) == 1 && strings.HasPrefix(query.Get("host"), "/") &&
		len(query["port"]) == 1 && query.Get("port") != ""
}

func transportAdmitted(config *pgconn.Config, allowInsecureLocalhost bool) bool {
	if config == nil || len(config.Fallbacks) != 0 {
		return false
	}
	if config.TLSConfig != nil && !config.TLSConfig.InsecureSkipVerify &&
		config.TLSConfig.ServerName != "" {
		return true
	}
	return allowInsecureLocalhost && localPostgresHost(config.Host)
}

func localPostgresHost(host string) bool {
	if strings.HasPrefix(host, "/") {
		return true
	}
	address := net.ParseIP(host)
	return address != nil && address.IsLoopback()
}
