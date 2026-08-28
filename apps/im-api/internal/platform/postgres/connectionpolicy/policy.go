// Package connectionpolicy freezes the connection-string boundary shared by the one-shot
// migrator and the long-lived runtime pool.
package connectionpolicy

import (
	"errors"
	"net"
	"net/url"
	"slices"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

var (
	ErrInvalidConfig   = errors.New("invalid PostgreSQL connection policy config")
	ErrUnsafeTransport = errors.New("PostgreSQL connection transport is not admitted")
)

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

type Config struct {
	ConnectionString       string
	DatabaseName           string
	LoginRoles             []string
	ConnectTimeout         time.Duration
	AllowInsecureLocalhost bool
}

// Parse returns a detached pgx config only after the original connection string and its final
// resolved identity/transport pass policy. Errors are fixed sentinels and never retain the DSN.
func Parse(input Config) (*pgx.ConnConfig, error) {
	if strings.TrimSpace(input.ConnectionString) == "" || input.DatabaseName == "" ||
		len(input.LoginRoles) == 0 || input.ConnectTimeout <= 0 ||
		!strictConnectionStringShape(input.ConnectionString) {
		return nil, ErrInvalidConfig
	}
	parsed, err := pgx.ParseConfigWithOptions(input.ConnectionString, pgx.ParseConfigOptions{
		ParseConfigOptions: pgconn.ParseConfigOptions{
			ConnStringAllowedKeys: connectionStringAllowedKeys,
		},
	})
	if err != nil {
		return nil, ErrInvalidConfig
	}
	if parsed.Database != input.DatabaseName || !slices.Contains(input.LoginRoles, parsed.User) ||
		len(parsed.RuntimeParams) != 0 {
		return nil, ErrInvalidConfig
	}
	if !transportAdmitted(&parsed.Config, input.AllowInsecureLocalhost) {
		return nil, ErrUnsafeTransport
	}
	parsed.ConnectTimeout = input.ConnectTimeout
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
