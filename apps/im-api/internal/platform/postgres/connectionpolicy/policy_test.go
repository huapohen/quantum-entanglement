package connectionpolicy

import (
	"errors"
	"strings"
	"testing"
	"time"
)

func TestParseFreezesIdentityAndConnectTimeout(t *testing.T) {
	clearAmbientPostgresSettings(t)
	input := validPolicyConfig()
	parsed, err := Parse(input)
	if err != nil {
		t.Fatalf("parse exact connection policy: %v", err)
	}
	if parsed.Database != input.DatabaseName || parsed.User != input.LoginRoles[0] ||
		parsed.ConnectTimeout != input.ConnectTimeout || len(parsed.RuntimeParams) != 0 {
		t.Fatalf("parsed connection policy = %#v", parsed.Config)
	}
}

func TestParseRejectsConsumedParametersAndUnauthenticatedRemoteTLS(t *testing.T) {
	clearAmbientPostgresSettings(t)
	for name, connectionString := range map[string]string{
		"pool parameter": "postgresql://wanwork_app@db.example.com:5432/wanwork_im?sslmode=verify-full&pool_max_conn_lifetime=1m",
		"query mode":     "postgresql://wanwork_app@db.example.com:5432/wanwork_im?sslmode=verify-full&default_query_exec_mode=simple_protocol",
		"tls require":    "postgresql://wanwork_app@db.example.com:5432/wanwork_im?sslmode=require",
		"multi host":     "postgresql://wanwork_app@db.example.com:5432,db2.example.com:5432/wanwork_im?sslmode=verify-full",
	} {
		t.Run(name, func(t *testing.T) {
			input := validPolicyConfig()
			input.ConnectionString = connectionString
			if _, err := Parse(input); err == nil {
				t.Fatal("unsafe connection policy accepted")
			}
		})
	}
}

func TestParseAllowsOnlyExplicitNumericLoopbackPlaintextWithoutLeakingCredential(t *testing.T) {
	clearAmbientPostgresSettings(t)
	const credentialCanary = "connection-policy-secret"
	input := validPolicyConfig()
	input.ConnectionString = "postgresql://wanwork_app:" + credentialCanary +
		"@127.0.0.1:55488/wanwork_im?sslmode=disable"
	if _, err := Parse(input); !errors.Is(err, ErrUnsafeTransport) ||
		strings.Contains(err.Error(), credentialCanary) {
		t.Fatalf("implicit plaintext error = %v", err)
	}
	input.AllowInsecureLocalhost = true
	if _, err := Parse(input); err != nil {
		t.Fatalf("explicit numeric loopback exception: %v", err)
	}
	input.ConnectionString = "postgresql://wanwork_app@localhost:55488/wanwork_im?sslmode=disable"
	if _, err := Parse(input); !errors.Is(err, ErrUnsafeTransport) {
		t.Fatalf("hostname plaintext error = %v, want %v", err, ErrUnsafeTransport)
	}
}

func TestParseRejectsEveryPGXAmbientSettingBeforeParsing(t *testing.T) {
	clearAmbientPostgresSettings(t)
	for _, name := range ambientPostgresVariableNames {
		t.Run(name, func(t *testing.T) {
			canary := "ambient-credential-canary-" + name
			t.Setenv(name, canary)
			_, err := Parse(validPolicyConfig())
			if !errors.Is(err, ErrAmbientSettings) {
				t.Fatalf("ambient %s error = %v, want %v", name, err, ErrAmbientSettings)
			}
			if strings.Contains(err.Error(), canary) {
				t.Fatalf("ambient %s leaked its value", name)
			}
		})
	}
}

func clearAmbientPostgresSettings(t *testing.T) {
	t.Helper()
	for _, name := range ambientPostgresVariableNames {
		t.Setenv(name, "")
	}
}

func validPolicyConfig() Config {
	return Config{
		ConnectionString: "postgresql://wanwork_app@db.example.com:5432/wanwork_im?sslmode=verify-full",
		DatabaseName:     "wanwork_im",
		LoginRoles:       []string{"wanwork_app"},
		ConnectTimeout:   3 * time.Second,
	}
}
