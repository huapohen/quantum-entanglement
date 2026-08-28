package authoritycutover

import (
	"context"
	"errors"
	"os"
	"strings"
	"testing"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/platform/postgres/migrations"
	"github.com/jackc/pgx/v5"
)

const clusterProbeIntegrationURL = "WANWORK_TEST_POSTGRES_ADMIN_URL"

func TestProbePostgreSQLClusterIdentityRejectsInvalidInputsWithoutDetails(t *testing.T) {
	expectation := validClusterProbeExpectationFixture()
	tests := map[string]struct {
		ctx         context.Context
		expectation PostgreSQLClusterProbeExpectation
		verifier    clusterTransportVerifier
	}{
		"nil context": {
			ctx:         nil,
			expectation: expectation,
			verifier:    func(*pgx.Conn, PostgreSQLClusterProbeExpectation) bool { return true },
		},
		"nil connection": {
			ctx:         t.Context(),
			expectation: expectation,
			verifier:    func(*pgx.Conn, PostgreSQLClusterProbeExpectation) bool { return true },
		},
		"nil verifier": {
			ctx:         t.Context(),
			expectation: expectation,
		},
		"wrong major": {
			ctx: t.Context(),
			expectation: func() PostgreSQLClusterProbeExpectation {
				value := expectation
				value.PostgreSQLMajor--
				return value
			}(),
			verifier: func(*pgx.Conn, PostgreSQLClusterProbeExpectation) bool { return true },
		},
		"credential-shaped database": {
			ctx: t.Context(),
			expectation: func() PostgreSQLClusterProbeExpectation {
				value := expectation
				value.Database = "password=cluster-probe-canary"
				return value
			}(),
			verifier: func(*pgx.Conn, PostgreSQLClusterProbeExpectation) bool { return true },
		},
	}
	for name, test := range tests {
		t.Run(name, func(t *testing.T) {
			_, err := probePostgreSQLClusterIdentity(
				test.ctx,
				nil,
				test.expectation,
				test.verifier,
			)
			if !errors.Is(err, ErrInvalidClusterProbe) || err != ErrInvalidClusterProbe {
				t.Fatalf("error = %v, want fixed %v", err, ErrInvalidClusterProbe)
			}
			assertProbeErrorHasNoSecrets(t, err)
		})
	}
}

func TestProbePostgreSQLClusterIdentityAgainstPostgres(t *testing.T) {
	connection, expectation := openClusterProbeIntegrationConnection(t)

	t.Run("exported API rejects insecure transport", func(t *testing.T) {
		_, err := ProbePostgreSQLClusterIdentity(t.Context(), connection, expectation)
		if !errors.Is(err, ErrUntrustedClusterProbe) || err != ErrUntrustedClusterProbe {
			t.Fatalf("error = %v, want fixed %v", err, ErrUntrustedClusterProbe)
		}
		assertProbeErrorHasNoSecrets(t, err)
	})

	t.Run("trusted transport reads an opaque primary identity", func(t *testing.T) {
		identity, err := probePostgreSQLClusterIdentity(
			t.Context(),
			connection,
			expectation,
			func(*pgx.Conn, PostgreSQLClusterProbeExpectation) bool { return true },
		)
		if err != nil {
			t.Fatalf("probe trusted integration transport: %v", err)
		}
		if identity.database != expectation.Database || identity.loginRole != expectation.LoginRole ||
			identity.serverIdentity != expectation.ServerIdentity ||
			identity.postgreSQLMajor != expectation.PostgreSQLMajor ||
			identity.caDigest != expectation.TLS.CADigest || !identity.primary ||
			identity.pgControlVersion <= 0 || identity.catalogVersionNo <= 0 ||
			!canonicalPostgreSQLSystemIdentifier.MatchString(identity.systemIdentifier) {
			t.Fatalf("incomplete identity: %#v", identity)
		}

		input := planInputForProbedCluster(expectation, identity)
		plan, err := BuildPlan(input)
		if err != nil {
			t.Fatalf("BuildPlan with probed identity: %v", err)
		}
		if strings.Contains(string(plan.CanonicalBytes()), identity.systemIdentifier) {
			t.Fatal("plan exposed the raw PostgreSQL system identifier")
		}

		driftTests := map[string]func(*PlanInput){
			"database": func(value *PlanInput) {
				value.AuthorityManifest.DatabaseName += "_other"
			},
			"login": func(value *PlanInput) {
				for index := range value.Credentials {
					if value.Credentials[index].Consumer == CredentialProvisioner {
						value.Credentials[index].LoginRole += "_other"
					}
				}
			},
			"server": func(value *PlanInput) {
				value.ServerIdentity = "postgres-other.internal"
				value.TLS.ServerName = value.ServerIdentity
			},
			"root CA": func(value *PlanInput) {
				value.TLS.CADigest = "sha256:" + strings.Repeat("e", 64)
			},
		}
		for name, mutate := range driftTests {
			t.Run("rejects caller "+name+" drift", func(t *testing.T) {
				drifted := input
				drifted.Credentials = append([]CredentialGeneration(nil), input.Credentials...)
				mutate(&drifted)
				if _, err := BuildPlan(drifted); !errors.Is(err, ErrInvalidPlan) {
					t.Fatalf("BuildPlan error = %v, want %v", err, ErrInvalidPlan)
				}
			})
		}
	})

	t.Run("rejects database and login drift after catalog read", func(t *testing.T) {
		for name, mutate := range map[string]func(*PostgreSQLClusterProbeExpectation){
			"database": func(value *PostgreSQLClusterProbeExpectation) { value.Database += "_other" },
			"login":    func(value *PostgreSQLClusterProbeExpectation) { value.LoginRole += "_other" },
		} {
			t.Run(name, func(t *testing.T) {
				drifted := expectation
				mutate(&drifted)
				_, err := probePostgreSQLClusterIdentity(
					t.Context(),
					connection,
					drifted,
					func(*pgx.Conn, PostgreSQLClusterProbeExpectation) bool { return true },
				)
				if !errors.Is(err, ErrUntrustedClusterProbe) || err != ErrUntrustedClusterProbe {
					t.Fatalf("error = %v, want fixed %v", err, ErrUntrustedClusterProbe)
				}
				assertProbeErrorHasNoSecrets(t, err)
			})
		}
	})

	t.Run("rejects an active set role", func(t *testing.T) {
		if _, err := connection.Exec(t.Context(), "SET ROLE pg_monitor"); err != nil {
			t.Fatalf("set integration role: %v", err)
		}
		t.Cleanup(func() { _, _ = connection.Exec(context.Background(), "RESET ROLE") })
		_, err := probePostgreSQLClusterIdentity(
			t.Context(),
			connection,
			expectation,
			func(*pgx.Conn, PostgreSQLClusterProbeExpectation) bool { return true },
		)
		if !errors.Is(err, ErrUntrustedClusterProbe) || err != ErrUntrustedClusterProbe {
			t.Fatalf("error = %v, want fixed %v", err, ErrUntrustedClusterProbe)
		}
		if _, err := connection.Exec(t.Context(), "RESET ROLE"); err != nil {
			t.Fatalf("reset integration role: %v", err)
		}
	})

	t.Run("rejects server drift at transport boundary", func(t *testing.T) {
		drifted := expectation
		drifted.ServerIdentity = "postgres-other.internal"
		drifted.TLS.ServerName = drifted.ServerIdentity
		_, err := ProbePostgreSQLClusterIdentity(t.Context(), connection, drifted)
		if !errors.Is(err, ErrUntrustedClusterProbe) || err != ErrUntrustedClusterProbe {
			t.Fatalf("error = %v, want fixed %v", err, ErrUntrustedClusterProbe)
		}
	})

	t.Run("maps cancellation to a fixed unavailable error", func(t *testing.T) {
		ctx, cancel := context.WithCancel(t.Context())
		cancel()
		_, err := probePostgreSQLClusterIdentity(
			ctx,
			connection,
			expectation,
			func(*pgx.Conn, PostgreSQLClusterProbeExpectation) bool { return true },
		)
		if !errors.Is(err, ErrClusterProbeUnavailable) || err != ErrClusterProbeUnavailable {
			t.Fatalf("error = %v, want fixed %v", err, ErrClusterProbeUnavailable)
		}
		assertProbeErrorHasNoSecrets(t, err)
	})

	t.Run("rejects a closed connection", func(t *testing.T) {
		closed, closedExpectation := openClusterProbeIntegrationConnection(t)
		if err := closed.Close(t.Context()); err != nil {
			t.Fatalf("close integration connection: %v", err)
		}
		_, err := probePostgreSQLClusterIdentity(
			t.Context(),
			closed,
			closedExpectation,
			func(*pgx.Conn, PostgreSQLClusterProbeExpectation) bool { return true },
		)
		if !errors.Is(err, ErrInvalidClusterProbe) || err != ErrInvalidClusterProbe {
			t.Fatalf("error = %v, want fixed %v", err, ErrInvalidClusterProbe)
		}
	})
}

func validClusterProbeExpectationFixture() PostgreSQLClusterProbeExpectation {
	return PostgreSQLClusterProbeExpectation{
		Database:        "wanwork_im",
		LoginRole:       "wanwork_provisioner_login",
		PostgreSQLMajor: migrations.AuthorityAccessPostgreSQLMajor,
		ServerIdentity:  "postgres-writer.internal",
		TLS: TLSProfile{
			CADigest:   "sha256:" + strings.Repeat("d", 64),
			CARef:      "trust/postgres-root-ca/generation-1",
			Mode:       "verify-full",
			ServerName: "postgres-writer.internal",
		},
	}
}

func openClusterProbeIntegrationConnection(
	t *testing.T,
) (*pgx.Conn, PostgreSQLClusterProbeExpectation) {
	t.Helper()
	adminURL := os.Getenv(clusterProbeIntegrationURL)
	if adminURL == "" {
		t.Skip(clusterProbeIntegrationURL + " is not set")
	}
	config, err := pgx.ParseConfig(adminURL)
	if err != nil {
		t.Fatalf("parse %s: %v", clusterProbeIntegrationURL, ErrClusterProbeUnavailable)
	}
	connection, err := pgx.ConnectConfig(t.Context(), config)
	if err != nil {
		t.Fatalf("connect %s: %v", clusterProbeIntegrationURL, ErrClusterProbeUnavailable)
	}
	t.Cleanup(func() { _ = connection.Close(context.Background()) })
	expectation := validClusterProbeExpectationFixture()
	expectation.Database = config.Database
	expectation.LoginRole = config.User
	expectation.ServerIdentity = config.Host
	expectation.TLS.ServerName = config.Host
	if !validClusterProbeExpectation(expectation) {
		t.Fatalf("%s must resolve to a canonical database, user, and host", clusterProbeIntegrationURL)
	}
	return connection, expectation
}

func planInputForProbedCluster(
	expectation PostgreSQLClusterProbeExpectation,
	identity VerifiedPostgreSQLClusterIdentity,
) PlanInput {
	input := validPlanInput()
	input.AuthorityManifest.DatabaseName = expectation.Database
	input.ClusterIdentity = identity
	for index := range input.Credentials {
		if input.Credentials[index].Consumer == CredentialProvisioner {
			input.Credentials[index].LoginRole = expectation.LoginRole
		}
	}
	input.PostgreSQLMajor = expectation.PostgreSQLMajor
	input.ServerIdentity = expectation.ServerIdentity
	input.TLS = expectation.TLS
	return input
}

func assertProbeErrorHasNoSecrets(t *testing.T, err error) {
	t.Helper()
	lower := strings.ToLower(err.Error())
	for _, forbidden := range []string{
		"password",
		"cluster-probe-canary",
		"postgresql://",
		"sslcert",
		"sslkey",
		"sslrootcert",
	} {
		if strings.Contains(lower, forbidden) {
			t.Fatalf("probe error exposed forbidden detail %q: %v", forbidden, err)
		}
	}
}
