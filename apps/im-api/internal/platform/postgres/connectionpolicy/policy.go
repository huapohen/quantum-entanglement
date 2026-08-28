// Package connectionpolicy freezes the connection-string boundary shared by the one-shot
// migrator and the long-lived runtime pool.
package connectionpolicy

import (
	"errors"
	"net"
	"net/url"
	"os"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

var (
	ErrInvalidConfig   = errors.New("invalid PostgreSQL connection policy config")
	ErrAmbientSettings = errors.New("ambient PostgreSQL connection settings are not admitted")
	ErrUnsafeTransport = errors.New("PostgreSQL connection transport is not admitted")
)

// pgx v5.10 reads these libpq-compatible variables before it parses the explicit URL. Some
// variables can redirect the endpoint, read credentials or TLS material from files, or inject
// session parameters. The exact URL policy therefore rejects presence, including an empty value,
// before pgx can perform parsing.
var ambientPostgresVariableNames = []string{
	"PGHOST",
	"PGPORT",
	"PGDATABASE",
	"PGUSER",
	"PGPASSWORD",
	"PGPASSFILE",
	"PGAPPNAME",
	"PGCONNECT_TIMEOUT",
	"PGSSLMODE",
	"PGSSLKEY",
	"PGSSLCERT",
	"PGSSLSNI",
	"PGSSLROOTCERT",
	"PGSSLPASSWORD",
	"PGSSLNEGOTIATION",
	"PGTARGETSESSIONATTRS",
	"PGSERVICE",
	"PGSERVICEFILE",
	"PGTZ",
	"PGOPTIONS",
	"PGMINPROTOCOLVERSION",
	"PGMAXPROTOCOLVERSION",
	"PGCHANNELBINDING",
	"PGREQUIREAUTH",
}

var connectionStringAllowedKeys = []string{
	"database",
	"dbname",
	"host",
	"passfile",
	"password",
	"port",
	"sslcert",
	"sslkey",
	"sslmode",
	"sslpassword",
	"sslrootcert",
	"user",
}

var rawQueryAllowedKeys = map[string]struct{}{
	"host":        {},
	"port":        {},
	"sslcert":     {},
	"sslkey":      {},
	"sslmode":     {},
	"sslpassword": {},
	"sslrootcert": {},
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
	explicitConnectionString, expected, err := prepare(input)
	if err != nil {
		return nil, err
	}
	parsed, err := pgx.ParseConfigWithOptions(explicitConnectionString, pgx.ParseConfigOptions{
		ParseConfigOptions: pgconn.ParseConfigOptions{
			ConnStringAllowedKeys: connectionStringAllowedKeys,
		},
	})
	if err != nil {
		return nil, ErrInvalidConfig
	}
	return validateAndFreeze(input, expected, parsed)
}

// ParsePool performs the same single strict parse through pgxpool so the returned config carries
// pgxpool's private construction marker. Callers must not parse the raw DSN a second time.
func ParsePool(input Config) (*pgxpool.Config, error) {
	explicitConnectionString, expected, err := prepare(input)
	if err != nil {
		return nil, err
	}
	parsed, err := pgxpool.ParseConfig(explicitConnectionString)
	if err != nil {
		return nil, ErrInvalidConfig
	}
	validated, err := validateAndFreeze(input, expected, parsed.ConnConfig)
	if err != nil {
		return nil, err
	}
	parsed.ConnConfig = validated
	return parsed, nil
}

func prepare(input Config) (string, *url.URL, error) {
	if strings.TrimSpace(input.ConnectionString) == "" || input.DatabaseName == "" ||
		len(input.LoginRoles) == 0 || input.ConnectTimeout <= 0 {
		return "", nil, ErrInvalidConfig
	}
	expected, ok := strictConnectionURL(input.ConnectionString, input.AllowInsecureLocalhost)
	if !ok {
		return "", nil, ErrInvalidConfig
	}
	if ambientPostgresSettingsPresent() {
		return "", nil, ErrAmbientSettings
	}

	// pgx defaults to ~/.pgpass and may adopt ~/.postgresql client/root certificates. Empty
	// explicit settings override those defaults without authorizing an ambient file. Explicit
	// ssl* paths from the reviewed URL remain available for private PKI and mTLS profiles.
	query := expected.Query()
	query.Set("passfile", "")
	for _, key := range []string{"sslcert", "sslkey", "sslrootcert"} {
		if !query.Has(key) {
			query.Set(key, "")
		}
	}
	explicit := *expected
	explicit.RawQuery = query.Encode()
	return explicit.String(), expected, nil
}

func validateAndFreeze(
	input Config,
	expected *url.URL,
	parsed *pgx.ConnConfig,
) (*pgx.ConnConfig, error) {
	if expected == nil || parsed == nil || parsed.Database != input.DatabaseName ||
		!slices.Contains(input.LoginRoles, parsed.User) || len(parsed.RuntimeParams) != 0 {
		return nil, ErrInvalidConfig
	}
	expectedPassword, passwordPresent := expected.User.Password()
	if (!passwordPresent && parsed.Password != "") ||
		(passwordPresent && parsed.Password != expectedPassword) {
		return nil, ErrInvalidConfig
	}
	expectedHost, expectedPort, ok := explicitEndpoint(expected)
	if !ok || parsed.Host != expectedHost || parsed.Port != expectedPort {
		return nil, ErrInvalidConfig
	}
	if !transportAdmitted(&parsed.Config, input.AllowInsecureLocalhost) {
		return nil, ErrUnsafeTransport
	}
	parsed.ConnectTimeout = input.ConnectTimeout
	parsed.DialFunc = (&net.Dialer{Timeout: input.ConnectTimeout}).DialContext
	return parsed, nil
}

func ambientPostgresSettingsPresent() bool {
	for _, name := range ambientPostgresVariableNames {
		if _, ok := os.LookupEnv(name); ok {
			return true
		}
	}
	return false
}

func strictConnectionURL(connectionString string, allowInsecureLocalhost bool) (*url.URL, bool) {
	parsed, err := url.Parse(connectionString)
	if err != nil || (parsed.Scheme != "postgres" && parsed.Scheme != "postgresql") ||
		parsed.Opaque != "" || parsed.Fragment != "" || parsed.User == nil ||
		parsed.User.Username() == "" || parsed.Path == "" || parsed.Path == "/" {
		return nil, false
	}
	query := parsed.Query()
	for key, values := range query {
		if _, allowed := rawQueryAllowedKeys[key]; !allowed || len(values) != 1 || values[0] == "" {
			return nil, false
		}
	}
	if len(query["sslmode"]) != 1 ||
		(query.Has("sslcert") != query.Has("sslkey")) ||
		(query.Has("sslpassword") && !query.Has("sslkey")) {
		return nil, false
	}
	if parsed.Hostname() != "" {
		if parsed.Port() == "" || query.Has("host") || query.Has("port") {
			return nil, false
		}
	} else if len(query["host"]) != 1 || !strings.HasPrefix(query.Get("host"), "/") ||
		len(query["port"]) != 1 {
		return nil, false
	}
	password, passwordPresent := parsed.User.Password()
	if passwordPresent && password == "" {
		return nil, false
	}
	host, _, ok := explicitEndpoint(parsed)
	if !ok || (!passwordPresent && !(allowInsecureLocalhost && localPostgresHost(host))) {
		return nil, false
	}
	return parsed, true
}

func explicitEndpoint(parsed *url.URL) (string, uint16, bool) {
	if parsed == nil {
		return "", 0, false
	}
	host := parsed.Hostname()
	portValue := parsed.Port()
	if host == "" {
		query := parsed.Query()
		host = query.Get("host")
		portValue = query.Get("port")
	}
	port, err := strconv.ParseUint(portValue, 10, 16)
	return host, uint16(port), err == nil && port != 0
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
