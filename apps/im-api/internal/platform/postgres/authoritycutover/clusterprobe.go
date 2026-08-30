package authoritycutover

import (
	"context"
	"crypto/sha256"
	"crypto/tls"
	"encoding/hex"
	"errors"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/platform/postgres/migrations"
	"github.com/jackc/pgx/v5"
)

var (
	ErrInvalidClusterProbe     = errors.New("invalid PostgreSQL cluster identity probe")
	ErrUntrustedClusterProbe   = errors.New("untrusted PostgreSQL cluster identity probe")
	ErrClusterProbeUnavailable = errors.New("PostgreSQL cluster identity probe unavailable")
)

// PostgreSQLClusterProbeExpectation is public deployment identity only. It contains no DSN,
// password, certificate material, or provisioner secret.
type PostgreSQLClusterProbeExpectation struct {
	Database        string
	LoginRole       string
	PostgreSQLMajor int
	ServerIdentity  string
	TLS             TLSProfile
}

type clusterTransportVerifier func(*pgx.Conn, PostgreSQLClusterProbeExpectation) bool

// ProbePostgreSQLClusterIdentity performs the approval-before-plan cluster probe. It verifies the
// already-negotiated TLS connection and reads the physical cluster identity in one read-only,
// repeatable-read transaction. Only the opaque result can enter BuildPlan.
func ProbePostgreSQLClusterIdentity(
	ctx context.Context,
	connection *pgx.Conn,
	expectation PostgreSQLClusterProbeExpectation,
) (VerifiedPostgreSQLClusterIdentity, error) {
	return probePostgreSQLClusterIdentity(ctx, connection, expectation, verifyClusterTLSTransport)
}

func probePostgreSQLClusterIdentity(
	ctx context.Context,
	connection *pgx.Conn,
	expectation PostgreSQLClusterProbeExpectation,
	verifyTransport clusterTransportVerifier,
) (VerifiedPostgreSQLClusterIdentity, error) {
	if ctx == nil || connection == nil || connection.IsClosed() ||
		!validClusterProbeExpectation(expectation) || verifyTransport == nil {
		return VerifiedPostgreSQLClusterIdentity{}, ErrInvalidClusterProbe
	}
	if !verifyTransport(connection, expectation) {
		return VerifiedPostgreSQLClusterIdentity{}, ErrUntrustedClusterProbe
	}
	transaction, err := connection.BeginTx(ctx, pgx.TxOptions{
		IsoLevel:   pgx.RepeatableRead,
		AccessMode: pgx.ReadOnly,
	})
	if err != nil {
		return VerifiedPostgreSQLClusterIdentity{}, ErrClusterProbeUnavailable
	}
	defer func() { _ = transaction.Rollback(context.Background()) }()

	var (
		catalogVersionNo int
		currentDatabase  string
		currentUser      string
		inRecovery       bool
		isolation        string
		pgControlVersion int
		readOnly         bool
		serverVersion    int
		sessionUser      string
		systemIdentifier string
	)
	if err := transaction.QueryRow(ctx, `
SELECT session_user,
       current_user,
       current_database(),
       pg_catalog.current_setting('server_version_num')::integer,
       pg_catalog.current_setting('transaction_isolation'),
       pg_catalog.current_setting('transaction_read_only')::boolean,
       pg_catalog.pg_is_in_recovery(),
       control.system_identifier::text,
       control.pg_control_version,
       control.catalog_version_no
FROM pg_catalog.pg_control_system() AS control`).Scan(
		&sessionUser,
		&currentUser,
		&currentDatabase,
		&serverVersion,
		&isolation,
		&readOnly,
		&inRecovery,
		&systemIdentifier,
		&pgControlVersion,
		&catalogVersionNo,
	); err != nil {
		return VerifiedPostgreSQLClusterIdentity{}, ErrClusterProbeUnavailable
	}
	identity := VerifiedPostgreSQLClusterIdentity{
		caDigest:         expectation.TLS.CADigest,
		catalogVersionNo: catalogVersionNo,
		database:         currentDatabase,
		loginRole:        sessionUser,
		pgControlVersion: pgControlVersion,
		postgreSQLMajor:  serverVersion / 10000,
		primary:          !inRecovery,
		serverIdentity:   expectation.ServerIdentity,
		systemIdentifier: systemIdentifier,
	}
	if sessionUser != expectation.LoginRole || currentUser != expectation.LoginRole ||
		currentDatabase != expectation.Database || identity.postgreSQLMajor != expectation.PostgreSQLMajor ||
		isolation != "repeatable read" || !readOnly || inRecovery ||
		!validVerifiedPostgreSQLClusterIdentityForScope(
			identity,
			expectation.Database,
			expectation.LoginRole,
			expectation.PostgreSQLMajor,
			expectation.ServerIdentity,
			expectation.TLS.CADigest,
		) {
		return VerifiedPostgreSQLClusterIdentity{}, ErrUntrustedClusterProbe
	}
	if err := transaction.Commit(ctx); err != nil {
		return VerifiedPostgreSQLClusterIdentity{}, ErrClusterProbeUnavailable
	}
	return identity, nil
}

func validClusterProbeExpectation(expectation PostgreSQLClusterProbeExpectation) bool {
	return canonicalIdentity(expectation.Database) && canonicalIdentity(expectation.LoginRole) &&
		expectation.PostgreSQLMajor == migrations.AuthorityAccessPostgreSQLMajor &&
		canonicalIdentity(expectation.ServerIdentity) && validTLS(expectation.TLS) &&
		expectation.TLS.ServerName == expectation.ServerIdentity
}

func verifyClusterTLSTransport(
	connection *pgx.Conn,
	expectation PostgreSQLClusterProbeExpectation,
) bool {
	config := connection.Config()
	if config == nil || config.Database != expectation.Database || config.User != expectation.LoginRole ||
		config.Host != expectation.ServerIdentity || config.TLSConfig == nil ||
		config.TLSConfig.InsecureSkipVerify || config.TLSConfig.ServerName != expectation.ServerIdentity ||
		len(config.Fallbacks) != 0 {
		return false
	}
	tlsConnection, ok := connection.PgConn().Conn().(*tls.Conn)
	if !ok {
		return false
	}
	state := tlsConnection.ConnectionState()
	if !state.HandshakeComplete || state.Version < tls.VersionTLS12 || state.CipherSuite == 0 ||
		state.ServerName != expectation.ServerIdentity || len(state.PeerCertificates) == 0 ||
		len(state.VerifiedChains) == 0 ||
		state.PeerCertificates[0].VerifyHostname(expectation.ServerIdentity) != nil {
		return false
	}
	for _, chain := range state.VerifiedChains {
		if len(chain) == 0 {
			continue
		}
		rootHash := sha256.Sum256(chain[len(chain)-1].Raw)
		if "sha256:"+hex.EncodeToString(rootHash[:]) == expectation.TLS.CADigest {
			return true
		}
	}
	return false
}
