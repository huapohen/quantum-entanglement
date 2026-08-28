// Package runtimepool constructs PostgreSQL pools whose connections are continuously bound to
// the exact WanWork IM runtime authority. It never accepts migration or owner credentials.
package runtimepool

import (
	"errors"
	"net"
	"slices"
	"strings"
	"time"

	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/platform/postgres/migrations"
)

var (
	ErrInvalidConfig   = errors.New("invalid PostgreSQL runtime pool config")
	ErrUnsafeTransport = errors.New("PostgreSQL runtime pool transport is not admitted")
)

const maximumConnections int32 = 100

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

func transportAdmitted(config *pgconn.Config, allowInsecureLocalhost bool) bool {
	if config == nil {
		return false
	}
	candidates := []*pgconn.Config{config}
	for _, fallback := range config.Fallbacks {
		candidate := *config
		candidate.Host = fallback.Host
		candidate.Port = fallback.Port
		candidate.TLSConfig = fallback.TLSConfig
		candidates = append(candidates, &candidate)
	}
	for _, candidate := range candidates {
		if candidate.TLSConfig != nil {
			continue
		}
		if !allowInsecureLocalhost || !localPostgresHost(candidate.Host) {
			return false
		}
	}
	return true
}

func localPostgresHost(host string) bool {
	if strings.HasPrefix(host, "/") {
		return true
	}
	if host == "localhost" {
		return true
	}
	address := net.ParseIP(host)
	return address != nil && address.IsLoopback()
}
