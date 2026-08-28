package runtimepool

import (
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/platform/postgres/migrations"
)

func TestParseConfigBindsRuntimeIdentityAndPoolLimits(t *testing.T) {
	input := validConfig()
	parsed, err := parseConfig(input)
	if err != nil {
		t.Fatalf("parse runtime pool config: %v", err)
	}
	if parsed.ConnConfig.Database != input.Manifest.DatabaseName ||
		parsed.ConnConfig.User != input.Manifest.RuntimeLoginRoles[0] {
		t.Fatalf(
			"parsed identity database=%q user=%q",
			parsed.ConnConfig.Database,
			parsed.ConnConfig.User,
		)
	}
	if parsed.MaxConns != input.MaxConnections || parsed.MinConns != 0 ||
		parsed.MinIdleConns != input.MinIdleConnections ||
		parsed.ConnConfig.ConnectTimeout != input.ConnectTimeout ||
		parsed.PingTimeout != input.PingTimeout {
		t.Fatalf("parsed pool limits do not match explicit runtime config: %#v", parsed)
	}
	if len(parsed.ConnConfig.RuntimeParams) != 0 {
		t.Fatalf("runtime parameters were admitted: %v", parsed.ConnConfig.RuntimeParams)
	}
}

func TestParseConfigRejectsIdentityAndSessionParameterDrift(t *testing.T) {
	for name, mutate := range map[string]func(*Config){
		"empty connection string": func(value *Config) { value.ConnectionString = "" },
		"wrong database": func(value *Config) {
			value.ConnectionString = "postgresql://wanwork_app_a@db.example.com/wrong?sslmode=verify-full"
		},
		"wrong login": func(value *Config) {
			value.ConnectionString = "postgresql://wanwork_other@db.example.com/wanwork_im?sslmode=verify-full"
		},
		"role": func(value *Config) {
			value.ConnectionString += "&role=wanwork_im_owner"
		},
		"search path": func(value *Config) {
			value.ConnectionString += "&search_path=wanwork_im"
		},
		"options": func(value *Config) {
			value.ConnectionString += "&options=-c%20role%3Dwanwork_im_owner"
		},
	} {
		t.Run(name, func(t *testing.T) {
			input := validConfig()
			mutate(&input)
			if _, err := parseConfig(input); !errors.Is(err, ErrInvalidConfig) {
				t.Fatalf("invalid config error = %v, want %v", err, ErrInvalidConfig)
			}
		})
	}
}

func TestParseConfigRequiresExplicitURLIdentityEndpointAndTLSMode(t *testing.T) {
	for name, connectionString := range map[string]string{
		"keyword form":            "host=db.example.com port=5432 user=wanwork_app_a dbname=wanwork_im sslmode=verify-full",
		"missing user":            "postgresql://db.example.com:5432/wanwork_im?sslmode=verify-full",
		"missing host":            "postgresql://wanwork_app_a@/wanwork_im?sslmode=verify-full",
		"missing port":            "postgresql://wanwork_app_a@db.example.com/wanwork_im?sslmode=verify-full",
		"missing database":        "postgresql://wanwork_app_a@db.example.com:5432/?sslmode=verify-full",
		"missing sslmode":         "postgresql://wanwork_app_a@db.example.com:5432/wanwork_im",
		"query identity override": "postgresql://wanwork_app_a@db.example.com:5432/wanwork_im?sslmode=verify-full&user=other",
	} {
		t.Run(name, func(t *testing.T) {
			input := validConfig()
			input.ConnectionString = connectionString
			if _, err := parseConfig(input); !errors.Is(err, ErrInvalidConfig) {
				t.Fatalf("implicit connection config error = %v, want %v", err, ErrInvalidConfig)
			}
		})
	}
}

func TestParseConfigDoesNotReturnCredentialCanary(t *testing.T) {
	const credentialCanary = "runtime-secret-must-not-escape"
	input := validConfig()
	input.ConnectionString = "postgresql://wanwork_app_a:" + credentialCanary +
		"@db.example.com:5432/wanwork_im?sslmode=verify-full&forbidden=value"
	_, err := parseConfig(input)
	if err != ErrInvalidConfig {
		t.Fatalf("parse error identity = %v, want fixed sentinel", err)
	}
	if strings.Contains(err.Error(), credentialCanary) {
		t.Fatal("parse error disclosed credential canary")
	}
}

func TestParseConfigRejectsParserConsumedAndFileBackedParameters(t *testing.T) {
	for _, parameter := range []string{
		"default_query_exec_mode=simple_protocol",
		"statement_cache_capacity=0",
		"description_cache_capacity=0",
		"pool_max_conns=2",
		"pool_min_conns=1",
		"pool_min_idle_conns=1",
		"pool_max_conn_lifetime=1m",
		"pool_max_conn_idle_time=1m",
		"pool_health_check_period=1m",
		"pool_max_conn_lifetime_jitter=1m",
		"service=untrusted",
		"servicefile=/tmp/untrusted",
		"passfile=/tmp/untrusted",
		"application_name=untrusted",
	} {
		t.Run(parameter, func(t *testing.T) {
			input := validConfig()
			input.ConnectionString += "&" + parameter
			if _, err := parseConfig(input); !errors.Is(err, ErrInvalidConfig) {
				t.Fatalf("consumed parameter error = %v, want %v", err, ErrInvalidConfig)
			}
		})
	}
}

func TestParseConfigRejectsInvalidManifestAndLimits(t *testing.T) {
	for name, mutate := range map[string]func(*Config){
		"invalid manifest": func(value *Config) { value.Manifest.RuntimeRole = "" },
		"zero max":         func(value *Config) { value.MaxConnections = 0 },
		"excess max":       func(value *Config) { value.MaxConnections = maximumConnections + 1 },
		"negative idle":    func(value *Config) { value.MinIdleConnections = -1 },
		"idle over max":    func(value *Config) { value.MinIdleConnections = value.MaxConnections + 1 },
		"zero connect":     func(value *Config) { value.ConnectTimeout = 0 },
		"zero ping":        func(value *Config) { value.PingTimeout = 0 },
	} {
		t.Run(name, func(t *testing.T) {
			input := validConfig()
			mutate(&input)
			if _, err := parseConfig(input); !errors.Is(err, ErrInvalidConfig) {
				t.Fatalf("invalid config error = %v, want %v", err, ErrInvalidConfig)
			}
		})
	}
}

func TestParseConfigRequiresTLSExceptExplicitLocalTest(t *testing.T) {
	remote := validConfig()
	remote.ConnectionString = "postgresql://wanwork_app_a@db.example.com:5432/wanwork_im?sslmode=disable"
	if _, err := parseConfig(remote); !errors.Is(err, ErrUnsafeTransport) {
		t.Fatalf("remote plaintext error = %v, want %v", err, ErrUnsafeTransport)
	}
	remote.ConnectionString = "postgresql://wanwork_app_a@db.example.com:5432/wanwork_im?sslmode=prefer"
	if _, err := parseConfig(remote); !errors.Is(err, ErrUnsafeTransport) {
		t.Fatalf("remote TLS downgrade fallback error = %v, want %v", err, ErrUnsafeTransport)
	}
	remote.ConnectionString = "postgresql://wanwork_app_a@db.example.com:5432/wanwork_im?sslmode=require"
	if _, err := parseConfig(remote); !errors.Is(err, ErrUnsafeTransport) {
		t.Fatalf("unauthenticated TLS error = %v, want %v", err, ErrUnsafeTransport)
	}
	remote.ConnectionString = "postgresql://wanwork_app_a@db.example.com:5432,db2.example.com:5432/wanwork_im?sslmode=verify-full"
	if _, err := parseConfig(remote); !errors.Is(err, ErrUnsafeTransport) {
		t.Fatalf("unmodeled multi-host error = %v, want %v", err, ErrUnsafeTransport)
	}
	remote.ConnectionString = "postgresql://wanwork_app_a@db.example.com:5432/wanwork_im?sslmode=disable"
	remote.AllowInsecureLocalhost = true
	if _, err := parseConfig(remote); !errors.Is(err, ErrUnsafeTransport) {
		t.Fatalf("remote plaintext exception error = %v, want %v", err, ErrUnsafeTransport)
	}

	local := validConfig()
	local.ConnectionString = "postgresql://wanwork_app_a@127.0.0.1:55488/wanwork_im?sslmode=disable"
	if _, err := parseConfig(local); !errors.Is(err, ErrUnsafeTransport) {
		t.Fatalf("implicit local plaintext error = %v, want %v", err, ErrUnsafeTransport)
	}
	local.AllowInsecureLocalhost = true
	if _, err := parseConfig(local); err != nil {
		t.Fatalf("explicit local test exception: %v", err)
	}
	local.ConnectionString = "postgresql://wanwork_app_a@localhost:55488/wanwork_im?sslmode=disable"
	if _, err := parseConfig(local); !errors.Is(err, ErrUnsafeTransport) {
		t.Fatalf("hostname local exception error = %v, want %v", err, ErrUnsafeTransport)
	}
}

func validConfig() Config {
	manifest := migrations.DefaultAuthorityAccessManifest()
	manifest.MigrationLoginRoles = []string{"wanwork_migration_a"}
	manifest.RuntimeLoginRoles = []string{"wanwork_app_a"}
	return Config{
		ConnectionString:   "postgresql://wanwork_app_a@db.example.com:5432/wanwork_im?sslmode=verify-full",
		Manifest:           manifest,
		MaxConnections:     8,
		MinIdleConnections: 1,
		ConnectTimeout:     3 * time.Second,
		PingTimeout:        time.Second,
	}
}
