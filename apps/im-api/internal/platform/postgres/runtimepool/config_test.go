package runtimepool

import (
	"errors"
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
	remote.ConnectionString = "postgresql://wanwork_app_a@db.example.com/wanwork_im?sslmode=disable"
	if _, err := parseConfig(remote); !errors.Is(err, ErrUnsafeTransport) {
		t.Fatalf("remote plaintext error = %v, want %v", err, ErrUnsafeTransport)
	}
	remote.ConnectionString = "postgresql://wanwork_app_a@db.example.com/wanwork_im?sslmode=prefer"
	if _, err := parseConfig(remote); !errors.Is(err, ErrUnsafeTransport) {
		t.Fatalf("remote TLS downgrade fallback error = %v, want %v", err, ErrUnsafeTransport)
	}
	remote.ConnectionString = "postgresql://wanwork_app_a@db.example.com/wanwork_im?sslmode=disable"
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
}

func validConfig() Config {
	manifest := migrations.DefaultAuthorityAccessManifest()
	manifest.MigrationLoginRoles = []string{"wanwork_migration_a"}
	manifest.RuntimeLoginRoles = []string{"wanwork_app_a"}
	return Config{
		ConnectionString:   "postgresql://wanwork_app_a@db.example.com/wanwork_im?sslmode=verify-full",
		Manifest:           manifest,
		MaxConnections:     8,
		MinIdleConnections: 1,
		ConnectTimeout:     3 * time.Second,
		PingTimeout:        time.Second,
	}
}
