// Package runtimepool constructs PostgreSQL pools whose connections are continuously bound to
// the exact WanWork IM runtime authority. It never accepts migration or owner credentials.
package runtimepool

import (
	"errors"
	"strings"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/platform/postgres/connectionpolicy"
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
	validated, err := connectionpolicy.Parse(connectionpolicy.Config{
		ConnectionString:       input.ConnectionString,
		DatabaseName:           input.Manifest.DatabaseName,
		LoginRoles:             input.Manifest.RuntimeLoginRoles,
		ConnectTimeout:         input.ConnectTimeout,
		AllowInsecureLocalhost: input.AllowInsecureLocalhost,
	})
	if errors.Is(err, connectionpolicy.ErrUnsafeTransport) {
		return nil, ErrUnsafeTransport
	}
	if err != nil {
		return nil, ErrInvalidConfig
	}
	parsed, err := pgxpool.ParseConfig(input.ConnectionString)
	if err != nil {
		// pgx parse errors can retain the connection string. Do not wrap or return them.
		return nil, ErrInvalidConfig
	}
	connection := &parsed.ConnConfig.Config
	if connection.Database != validated.Database || connection.User != validated.User ||
		len(connection.RuntimeParams) != 0 {
		return nil, ErrInvalidConfig
	}

	connection.ConnectTimeout = input.ConnectTimeout
	parsed.MaxConns = input.MaxConnections
	parsed.MinConns = 0
	parsed.MinIdleConns = input.MinIdleConnections
	parsed.PingTimeout = input.PingTimeout
	return parsed, nil
}
