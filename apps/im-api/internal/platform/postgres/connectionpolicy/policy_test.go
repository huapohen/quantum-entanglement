package connectionpolicy

import (
	"errors"
	"net/url"
	"os"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

func TestParseFreezesIdentityAndConnectTimeout(t *testing.T) {
	clearAmbientPostgresSettings(t)
	input := validPolicyConfig()
	parsed, err := Parse(input)
	if err != nil {
		t.Fatalf("parse exact connection policy: %v", err)
	}
	if parsed.Database != input.DatabaseName || parsed.User != input.LoginRoles[0] ||
		parsed.Host != "db.example.com" || parsed.Port != 5432 ||
		parsed.Password != "policy-test-password" ||
		parsed.ConnectTimeout != input.ConnectTimeout || parsed.DialFunc == nil ||
		len(parsed.RuntimeParams) != 0 {
		t.Fatalf("parsed connection policy = %#v", parsed.Config)
	}
}

func TestParseRejectsConsumedParametersAndUnsafeRemoteComposition(t *testing.T) {
	clearAmbientPostgresSettings(t)
	for name, connectionString := range map[string]string{
		"pool parameter": "postgresql://wanwork_app:policy-test-password@db.example.com:5432/wanwork_im?sslmode=verify-full&pool_max_conn_lifetime=1m",
		"query mode":     "postgresql://wanwork_app:policy-test-password@db.example.com:5432/wanwork_im?sslmode=verify-full&default_query_exec_mode=simple_protocol",
		"tls require":    "postgresql://wanwork_app:policy-test-password@db.example.com:5432/wanwork_im?sslmode=require",
		"multi host":     "postgresql://wanwork_app:policy-test-password@db.example.com:5432,db2.example.com:5432/wanwork_im?sslmode=verify-full",
		"no password":    "postgresql://wanwork_app@db.example.com:5432/wanwork_im?sslmode=verify-full",
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
	parsed, err := Parse(input)
	if err != nil {
		t.Fatalf("explicit numeric loopback exception: %v", err)
	}
	if parsed.Password != credentialCanary {
		t.Fatal("explicit URL credential was not preserved exactly")
	}
	input.ConnectionString = "postgresql://wanwork_app:" + credentialCanary +
		"@localhost:55488/wanwork_im?sslmode=disable"
	if _, err := Parse(input); !errors.Is(err, ErrUnsafeTransport) {
		t.Fatalf("hostname plaintext error = %v, want %v", err, ErrUnsafeTransport)
	}
}

func TestParseAllowsPasswordlessOnlyForExplicitLocalTest(t *testing.T) {
	clearAmbientPostgresSettings(t)
	input := validPolicyConfig()
	input.ConnectionString =
		"postgresql://wanwork_app@127.0.0.1:55488/wanwork_im?sslmode=disable"
	if _, err := Parse(input); !errors.Is(err, ErrInvalidConfig) {
		t.Fatalf("implicit passwordless local error = %v, want %v", err, ErrInvalidConfig)
	}
	input.AllowInsecureLocalhost = true
	parsed, err := Parse(input)
	if err != nil {
		t.Fatalf("explicit passwordless local test: %v", err)
	}
	if parsed.Password != "" {
		t.Fatal("passwordless local test adopted an implicit credential")
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

func TestParseRejectsEmptyAmbientSettingPresence(t *testing.T) {
	clearAmbientPostgresSettings(t)
	t.Setenv("PGHOST", "")
	if _, err := Parse(validPolicyConfig()); !errors.Is(err, ErrAmbientSettings) {
		t.Fatalf("empty ambient setting error = %v, want %v", err, ErrAmbientSettings)
	}
}

func TestParseRejectsAmbientSystemTrustOverrides(t *testing.T) {
	clearAmbientPostgresSettings(t)
	for _, name := range ambientTLSTrustVariableNames {
		t.Run(name, func(t *testing.T) {
			t.Setenv(name, "")
			if _, err := Parse(validPolicyConfig()); !errors.Is(err, ErrAmbientSettings) {
				t.Fatalf("ambient trust override %s error = %v, want %v", name, err, ErrAmbientSettings)
			}
		})
	}
}

func TestParseRejectsMalformedQueryPairsWithoutSilentlyDroppingThem(t *testing.T) {
	clearAmbientPostgresSettings(t)
	input := validPolicyConfig()
	// url.URL.Query silently discards a pair containing an unescaped semicolon. The strict parser
	// must reject the whole raw query rather than continue with only the valid sslmode pair.
	input.ConnectionString += "&unknown=value;also=value"
	if _, err := Parse(input); !errors.Is(err, ErrInvalidConfig) {
		t.Fatalf("malformed query error = %v, want %v", err, ErrInvalidConfig)
	}
}

func TestPrepareOverridesDefaultCredentialAndTLSFiles(t *testing.T) {
	clearAmbientPostgresSettings(t)
	explicit, original, err := prepare(validPolicyConfig())
	if err != nil {
		t.Fatalf("prepare exact connection policy: %v", err)
	}
	if original.Query().Has("passfile") || original.Query().Has("sslcert") ||
		original.Query().Has("sslkey") || original.Query().Has("sslrootcert") {
		t.Fatal("test precondition includes explicit file settings")
	}
	prepared, err := url.Parse(explicit)
	if err != nil {
		t.Fatalf("parse prepared URL: %v", err)
	}
	query := prepared.Query()
	for _, name := range []string{"passfile", "sslcert", "sslkey", "sslrootcert"} {
		if !query.Has(name) || query.Get(name) != "" {
			t.Fatalf("prepared %s = %q, present=%t", name, query.Get(name), query.Has(name))
		}
	}
}

func TestParsePoolReturnsStrictConstructiblePoolConfig(t *testing.T) {
	clearAmbientPostgresSettings(t)
	input := validPolicyConfig()
	parsed, err := ParsePool(input)
	if err != nil {
		t.Fatalf("parse exact pool policy: %v", err)
	}
	if parsed.ConnConfig.Database != input.DatabaseName ||
		parsed.ConnConfig.User != input.LoginRoles[0] ||
		parsed.ConnConfig.Password != "policy-test-password" ||
		parsed.ConnConfig.Host != "db.example.com" || parsed.ConnConfig.Port != 5432 ||
		parsed.ConnConfig.ConnectTimeout != input.ConnectTimeout ||
		parsed.ConnConfig.DialFunc == nil || len(parsed.ConnConfig.RuntimeParams) != 0 {
		t.Fatalf("parsed pool connection policy = %#v", parsed.ConnConfig.Config)
	}
	pool, err := pgxpool.NewWithConfig(t.Context(), parsed)
	if err != nil {
		t.Fatalf("construct pool from strict parsed config: %v", err)
	}
	pool.Close()
}

func clearAmbientPostgresSettings(t *testing.T) {
	t.Helper()
	for _, name := range slices.Concat(
		ambientPostgresVariableNames,
		ambientTLSTrustVariableNames,
	) {
		name := name
		value, present := os.LookupEnv(name)
		if err := os.Unsetenv(name); err != nil {
			t.Fatalf("unset %s: %v", name, err)
		}
		t.Cleanup(func() {
			var err error
			if present {
				err = os.Setenv(name, value)
			} else {
				err = os.Unsetenv(name)
			}
			if err != nil {
				t.Errorf("restore %s: %v", name, err)
			}
		})
	}
}

func validPolicyConfig() Config {
	return Config{
		ConnectionString: "postgresql://wanwork_app:policy-test-password@db.example.com:5432/wanwork_im?sslmode=verify-full",
		DatabaseName:     "wanwork_im",
		LoginRoles:       []string{"wanwork_app"},
		ConnectTimeout:   3 * time.Second,
	}
}
