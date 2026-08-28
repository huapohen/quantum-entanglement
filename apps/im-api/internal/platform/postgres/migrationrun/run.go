// Package migrationrun owns the one-shot migration connection lifecycle. It is deliberately
// separate from the long-lived API composition so the API never retains an owner-capable DSN.
package migrationrun

import (
	"context"
	"errors"
	"slices"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/platform/postgres/connectionpolicy"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/platform/postgres/migrations"
)

var (
	ErrInvalidConfig  = errors.New("invalid PostgreSQL migration run config")
	ErrUnavailable    = errors.New("PostgreSQL migration connection is unavailable")
	ErrAuthorityDrift = errors.New("PostgreSQL migration authority is not exact")
)

type Config struct {
	ConnectionString       string
	Manifest               migrations.AuthorityAccessManifest
	ConnectTimeout         time.Duration
	AllowInsecureLocalhost bool
}

// Run connects through one explicitly listed migration login, selects the exact owner role,
// applies the immutable migration catalog, and validates the complete authority manifest. A
// first deployment may apply schema and then fail the final access check until the separate DBA
// ownership/grant cutover is completed; rerunning is the required reconciliation path.
func Run(ctx context.Context, input Config) (migrations.State, error) {
	if ctx == nil || ctx.Err() != nil || input.Manifest.Validate() != nil {
		return migrations.State{}, ErrInvalidConfig
	}
	input.Manifest.MigrationLoginRoles = append([]string(nil), input.Manifest.MigrationLoginRoles...)
	input.Manifest.RuntimeLoginRoles = append([]string(nil), input.Manifest.RuntimeLoginRoles...)
	connectionConfig, err := connectionpolicy.Parse(connectionpolicy.Config{
		ConnectionString:       input.ConnectionString,
		DatabaseName:           input.Manifest.DatabaseName,
		LoginRoles:             input.Manifest.MigrationLoginRoles,
		ConnectTimeout:         input.ConnectTimeout,
		AllowInsecureLocalhost: input.AllowInsecureLocalhost,
	})
	if err != nil {
		return migrations.State{}, ErrInvalidConfig
	}
	connection, err := pgx.ConnectConfig(ctx, connectionConfig)
	if err != nil {
		return migrations.State{}, ErrUnavailable
	}
	defer closeConnection(connection)
	var sessionUser, currentUser, databaseName string
	if err := connection.QueryRow(ctx, "SELECT session_user, current_user, current_database()").Scan(
		&sessionUser,
		&currentUser,
		&databaseName,
	); err != nil || sessionUser != currentUser ||
		!slices.Contains(input.Manifest.MigrationLoginRoles, sessionUser) ||
		databaseName != input.Manifest.DatabaseName {
		return migrations.State{}, ErrAuthorityDrift
	}
	if _, err := connection.Exec(
		ctx,
		"SET ROLE "+pgx.Identifier{input.Manifest.OwnerRole}.Sanitize(),
	); err != nil {
		return migrations.State{}, ErrAuthorityDrift
	}
	state, err := migrations.Apply(ctx, connection)
	if err != nil {
		return migrations.State{}, err
	}
	if migrations.ValidateAuthorityAccess(ctx, connection, input.Manifest) != nil {
		return migrations.State{}, ErrAuthorityDrift
	}
	return state, nil
}

func closeConnection(connection *pgx.Conn) {
	if connection == nil || connection.IsClosed() {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = connection.Close(ctx)
}
