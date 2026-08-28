package authoritycutover

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"errors"
	"fmt"
	"os"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/platform/postgres/migrations"
	"github.com/jackc/pgx/v5"
)

var preflightIntegrationSequence atomic.Uint64

type preflightArtifactFixture struct {
	backupDigest  string
	backupError   error
	panicBackup   bool
	panicRelease  bool
	releaseDigest string
	releaseError  error
}

func (fixture preflightArtifactFixture) BackupAttestationDigest(
	context.Context,
	string,
) (string, error) {
	if fixture.panicBackup {
		panic("backup provider canary")
	}
	return fixture.backupDigest, fixture.backupError
}

func (fixture preflightArtifactFixture) ReleaseArtifactDigest(
	context.Context,
	string,
	string,
) (string, error) {
	if fixture.panicRelease {
		panic("release provider canary")
	}
	return fixture.releaseDigest, fixture.releaseError
}

type preflightPostgresFixture struct {
	admin         *pgx.Conn
	databaseAdmin *pgx.Conn
	connection    *pgx.Conn
	plan          Plan
	approval      VerifiedApproval
	artifacts     preflightArtifactFixture
	provisioner   string
	observedAt    time.Time
}

func TestObservePreflightReportRejectsInvalidInputs(t *testing.T) {
	fixture := newApprovalFixture(t, 0)
	approval, err := fixture.verifier.Verify(fixture.plan, fixture.raw, fixture.now)
	if err != nil {
		t.Fatalf("Verify: %v", err)
	}
	_, err = observePreflightReport(
		nil,
		nil,
		fixture.plan,
		approval,
		nil,
		fixture.now,
		func(*pgx.Conn, PostgreSQLClusterProbeExpectation) bool { return true },
	)
	if !errors.Is(err, ErrInvalidPreflightObservation) || err != ErrInvalidPreflightObservation {
		t.Fatalf("error = %v, want fixed %v", err, ErrInvalidPreflightObservation)
	}
	assertPreflightObservationErrorHasNoSecrets(t, err)
}

func TestObservePreflightReportAgainstPostgres(t *testing.T) {
	fixture := provisionPreflightPostgresFixture(t)

	t.Run("exported API rejects insecure transport", func(t *testing.T) {
		_, err := ObservePreflightReport(
			t.Context(),
			fixture.connection,
			fixture.plan,
			fixture.approval,
			fixture.artifacts,
			fixture.observedAt,
		)
		if !errors.Is(err, ErrUntrustedPreflightObservation) || err != ErrUntrustedPreflightObservation {
			t.Fatalf("error = %v, want fixed %v", err, ErrUntrustedPreflightObservation)
		}
		assertPreflightObservationErrorHasNoSecrets(t, err)
	})

	t.Run("rejects unbound approval before catalog reads", func(t *testing.T) {
		_, err := observePreflightReport(
			t.Context(),
			fixture.connection,
			fixture.plan,
			VerifiedApproval{},
			fixture.artifacts,
			fixture.observedAt,
			func(*pgx.Conn, PostgreSQLClusterProbeExpectation) bool { return true },
		)
		if !errors.Is(err, ErrUntrustedPreflightObservation) || err != ErrUntrustedPreflightObservation {
			t.Fatalf("error = %v, want fixed %v", err, ErrUntrustedPreflightObservation)
		}
	})

	t.Run("fixed SQL proves an empty dedicated cell", func(t *testing.T) {
		report := observePassingPreflightReport(t, fixture)
		if report.Outcome() != PreflightCheckPass {
			t.Fatalf("outcome = %q, want %q; checks=%#v", report.Outcome(), PreflightCheckPass, report.Snapshot().Checks)
		}
		if err := ValidatePreflightReport(
			report,
			fixture.plan,
			fixture.approval,
			fixture.observedAt,
		); err != nil {
			t.Fatalf("ValidatePreflightReport: %v", err)
		}
	})

	t.Run("missing artifact verifier becomes unknown", func(t *testing.T) {
		report, err := observePreflightReport(
			t.Context(),
			fixture.connection,
			fixture.plan,
			fixture.approval,
			nil,
			fixture.observedAt,
			func(*pgx.Conn, PostgreSQLClusterProbeExpectation) bool { return true },
		)
		if err != nil {
			t.Fatalf("observePreflightReport: %v", err)
		}
		if report.Outcome() != PreflightCheckUnknown {
			t.Fatalf("outcome = %q, want %q", report.Outcome(), PreflightCheckUnknown)
		}
		if err := ValidatePreflightReport(
			report,
			fixture.plan,
			fixture.approval,
			fixture.observedAt,
		); !errors.Is(err, ErrPreflightBlocked) {
			t.Fatalf("validation error = %v, want %v", err, ErrPreflightBlocked)
		}
	})

	t.Run("artifact mismatch blocks", func(t *testing.T) {
		artifacts := fixture.artifacts
		artifacts.backupDigest = "sha256:" + strings.Repeat("e", 64)
		report, err := observePreflightReport(
			t.Context(),
			fixture.connection,
			fixture.plan,
			fixture.approval,
			artifacts,
			fixture.observedAt,
			func(*pgx.Conn, PostgreSQLClusterProbeExpectation) bool { return true },
		)
		if err != nil {
			t.Fatalf("observePreflightReport: %v", err)
		}
		if report.Outcome() != PreflightCheckBlock {
			t.Fatalf("outcome = %q, want %q", report.Outcome(), PreflightCheckBlock)
		}
	})

	t.Run("artifact error and panic are redacted unknown", func(t *testing.T) {
		artifacts := fixture.artifacts
		artifacts.backupError = errors.New("postgresql://user:preflight-canary@db.invalid/postgres")
		artifacts.panicRelease = true
		report, err := observePreflightReport(
			t.Context(),
			fixture.connection,
			fixture.plan,
			fixture.approval,
			artifacts,
			fixture.observedAt,
			func(*pgx.Conn, PostgreSQLClusterProbeExpectation) bool { return true },
		)
		if err != nil {
			t.Fatalf("observePreflightReport: %v", err)
		}
		if report.Outcome() != PreflightCheckUnknown {
			t.Fatalf("outcome = %q, want %q", report.Outcome(), PreflightCheckUnknown)
		}
		for _, forbidden := range []string{"preflight-canary", "postgresql://", "provider canary"} {
			if strings.Contains(strings.ToLower(string(report.CanonicalBytes())), forbidden) {
				t.Fatalf("report exposed artifact provider detail %q", forbidden)
			}
		}
	})

	t.Run("user object contradicts empty classification", func(t *testing.T) {
		if _, err := fixture.databaseAdmin.Exec(
			t.Context(),
			"CREATE TABLE public.preflight_drift_table (id bigint PRIMARY KEY)",
		); err != nil {
			t.Fatalf("create drift table: %v", err)
		}
		t.Cleanup(func() {
			_, _ = fixture.databaseAdmin.Exec(context.Background(), "DROP TABLE IF EXISTS public.preflight_drift_table")
		})
		report, err := observePreflightReport(
			t.Context(),
			fixture.connection,
			fixture.plan,
			fixture.approval,
			fixture.artifacts,
			fixture.observedAt,
			func(*pgx.Conn, PostgreSQLClusterProbeExpectation) bool { return true },
		)
		if err != nil {
			t.Fatalf("observePreflightReport: %v", err)
		}
		assertPreflightCheckOutcome(t, report, "database/non-empty-classification", PreflightCheckBlock)
		if _, err := fixture.databaseAdmin.Exec(t.Context(), "DROP TABLE public.preflight_drift_table"); err != nil {
			t.Fatalf("drop drift table: %v", err)
		}
	})

	t.Run("provisioner attribute drift blocks", func(t *testing.T) {
		quotedProvisioner := pgx.Identifier{fixture.provisioner}.Sanitize()
		if _, err := fixture.admin.Exec(t.Context(), "ALTER ROLE "+quotedProvisioner+" INHERIT"); err != nil {
			t.Fatalf("alter provisioner: %v", err)
		}
		t.Cleanup(func() { _, _ = fixture.admin.Exec(context.Background(), "ALTER ROLE "+quotedProvisioner+" NOINHERIT") })
		report, err := observePreflightReport(
			t.Context(),
			fixture.connection,
			fixture.plan,
			fixture.approval,
			fixture.artifacts,
			fixture.observedAt,
			func(*pgx.Conn, PostgreSQLClusterProbeExpectation) bool { return true },
		)
		if err != nil {
			t.Fatalf("observePreflightReport: %v", err)
		}
		assertPreflightCheckOutcome(t, report, "authority/provisioner-attributes", PreflightCheckBlock)
		if _, err := fixture.admin.Exec(t.Context(), "ALTER ROLE "+quotedProvisioner+" NOINHERIT"); err != nil {
			t.Fatalf("restore provisioner: %v", err)
		}
	})

	t.Run("provisioner database setting drift blocks", func(t *testing.T) {
		quotedProvisioner := pgx.Identifier{fixture.provisioner}.Sanitize()
		quotedDatabase := pgx.Identifier{fixture.plan.Snapshot().Target.Database}.Sanitize()
		setStatement := "ALTER ROLE " + quotedProvisioner + " IN DATABASE " + quotedDatabase +
			" SET search_path = pg_catalog"
		resetStatement := "ALTER ROLE " + quotedProvisioner + " IN DATABASE " + quotedDatabase + " RESET ALL"
		if _, err := fixture.admin.Exec(t.Context(), setStatement); err != nil {
			t.Fatalf("set provisioner database setting: %v", err)
		}
		t.Cleanup(func() { _, _ = fixture.admin.Exec(context.Background(), resetStatement) })
		report, err := observePreflightReport(
			t.Context(),
			fixture.connection,
			fixture.plan,
			fixture.approval,
			fixture.artifacts,
			fixture.observedAt,
			func(*pgx.Conn, PostgreSQLClusterProbeExpectation) bool { return true },
		)
		if err != nil {
			t.Fatalf("observePreflightReport: %v", err)
		}
		assertPreflightCheckOutcome(t, report, "authority/provisioner-attributes", PreflightCheckBlock)
		if _, err := fixture.admin.Exec(t.Context(), resetStatement); err != nil {
			t.Fatalf("reset provisioner database setting: %v", err)
		}
	})

	t.Run("database connection policy drift blocks existence", func(t *testing.T) {
		quotedDatabase := pgx.Identifier{fixture.plan.Snapshot().Target.Database}.Sanitize()
		if _, err := fixture.admin.Exec(t.Context(), "ALTER DATABASE "+quotedDatabase+" CONNECTION LIMIT 10"); err != nil {
			t.Fatalf("alter database connection limit: %v", err)
		}
		t.Cleanup(func() {
			_, _ = fixture.admin.Exec(context.Background(), "ALTER DATABASE "+quotedDatabase+" CONNECTION LIMIT -1")
		})
		report, err := observePreflightReport(
			t.Context(),
			fixture.connection,
			fixture.plan,
			fixture.approval,
			fixture.artifacts,
			fixture.observedAt,
			func(*pgx.Conn, PostgreSQLClusterProbeExpectation) bool { return true },
		)
		if err != nil {
			t.Fatalf("observePreflightReport: %v", err)
		}
		assertPreflightCheckOutcome(t, report, "database/existence", PreflightCheckBlock)
		if _, err := fixture.admin.Exec(t.Context(), "ALTER DATABASE "+quotedDatabase+" CONNECTION LIMIT -1"); err != nil {
			t.Fatalf("restore database connection limit: %v", err)
		}
	})

	t.Run("extra direct database privilege blocks", func(t *testing.T) {
		quotedDatabase := pgx.Identifier{fixture.plan.Snapshot().Target.Database}.Sanitize()
		quotedProvisioner := pgx.Identifier{fixture.provisioner}.Sanitize()
		grantStatement := "GRANT TEMPORARY ON DATABASE " + quotedDatabase + " TO " + quotedProvisioner
		revokeStatement := "REVOKE TEMPORARY ON DATABASE " + quotedDatabase + " FROM " + quotedProvisioner
		if _, err := fixture.admin.Exec(t.Context(), grantStatement); err != nil {
			t.Fatalf("grant extra database privilege: %v", err)
		}
		t.Cleanup(func() { _, _ = fixture.admin.Exec(context.Background(), revokeStatement) })
		report, err := observePreflightReport(
			t.Context(),
			fixture.connection,
			fixture.plan,
			fixture.approval,
			fixture.artifacts,
			fixture.observedAt,
			func(*pgx.Conn, PostgreSQLClusterProbeExpectation) bool { return true },
		)
		if err != nil {
			t.Fatalf("observePreflightReport: %v", err)
		}
		assertPreflightCheckOutcome(t, report, "authority/provisioner-connect", PreflightCheckBlock)
		if _, err := fixture.admin.Exec(t.Context(), revokeStatement); err != nil {
			t.Fatalf("revoke extra database privilege: %v", err)
		}
	})

	t.Run("extra membership blocks", func(t *testing.T) {
		quotedProvisioner := pgx.Identifier{fixture.provisioner}.Sanitize()
		if _, err := fixture.admin.Exec(t.Context(), "GRANT pg_read_all_data TO "+quotedProvisioner); err != nil {
			t.Fatalf("grant rogue membership: %v", err)
		}
		t.Cleanup(func() {
			_, _ = fixture.admin.Exec(context.Background(), "REVOKE pg_read_all_data FROM "+quotedProvisioner)
		})
		report, err := observePreflightReport(
			t.Context(),
			fixture.connection,
			fixture.plan,
			fixture.approval,
			fixture.artifacts,
			fixture.observedAt,
			func(*pgx.Conn, PostgreSQLClusterProbeExpectation) bool { return true },
		)
		if err != nil {
			t.Fatalf("observePreflightReport: %v", err)
		}
		assertPreflightCheckOutcome(t, report, "authority/provisioner-membership", PreflightCheckBlock)
		if _, err := fixture.admin.Exec(t.Context(), "REVOKE pg_read_all_data FROM "+quotedProvisioner); err != nil {
			t.Fatalf("revoke rogue membership: %v", err)
		}
	})

	t.Run("cancellation is a fixed unavailable error", func(t *testing.T) {
		ctx, cancel := context.WithCancel(t.Context())
		cancel()
		_, err := observePreflightReport(
			ctx,
			fixture.connection,
			fixture.plan,
			fixture.approval,
			fixture.artifacts,
			fixture.observedAt,
			func(*pgx.Conn, PostgreSQLClusterProbeExpectation) bool { return true },
		)
		if !errors.Is(err, ErrPreflightObservationUnavailable) || err != ErrPreflightObservationUnavailable {
			t.Fatalf("error = %v, want fixed %v", err, ErrPreflightObservationUnavailable)
		}
		assertPreflightObservationErrorHasNoSecrets(t, err)
	})
}

func provisionPreflightPostgresFixture(t *testing.T) preflightPostgresFixture {
	t.Helper()
	adminURL := os.Getenv(clusterProbeIntegrationURL)
	if adminURL == "" {
		t.Skip(clusterProbeIntegrationURL + " is not set")
	}
	adminConfig, err := pgx.ParseConfig(adminURL)
	if err != nil {
		t.Fatalf("parse %s: %v", clusterProbeIntegrationURL, ErrPreflightObservationUnavailable)
	}
	admin, err := pgx.ConnectConfig(t.Context(), adminConfig)
	if err != nil {
		t.Fatalf("connect %s: %v", clusterProbeIntegrationURL, ErrPreflightObservationUnavailable)
	}
	t.Cleanup(func() { _ = admin.Close(context.Background()) })

	suffix := fmt.Sprintf("%d_%d", os.Getpid(), preflightIntegrationSequence.Add(1))
	database := "wanwork_pf_" + suffix
	databaseOwner := "wanwork_pf_owner_" + suffix
	provisioner := "wanwork_pf_login_" + suffix
	grantor := adminConfig.User
	testSecret := "wanwork_preflight_test_secret_" + suffix
	quotedDatabase := pgx.Identifier{database}.Sanitize()
	quotedOwner := pgx.Identifier{databaseOwner}.Sanitize()
	quotedProvisioner := pgx.Identifier{provisioner}.Sanitize()
	t.Cleanup(func() {
		_, _ = admin.Exec(context.Background(), "RESET ROLE")
		_, _ = admin.Exec(context.Background(), "DROP DATABASE IF EXISTS "+quotedDatabase+" WITH (FORCE)")
		_, _ = admin.Exec(context.Background(), "DROP ROLE IF EXISTS "+quotedProvisioner)
		_, _ = admin.Exec(context.Background(), "DROP ROLE IF EXISTS "+quotedOwner)
	})

	for _, statement := range []string{
		"CREATE ROLE " + quotedOwner + " NOLOGIN NOSUPERUSER NOINHERIT CREATEROLE NOCREATEDB NOREPLICATION NOBYPASSRLS CONNECTION LIMIT -1",
		"CREATE ROLE " + quotedProvisioner + " LOGIN NOSUPERUSER NOINHERIT NOCREATEROLE NOCREATEDB NOREPLICATION NOBYPASSRLS CONNECTION LIMIT -1 PASSWORD '" + testSecret + "'",
		"CREATE DATABASE " + quotedDatabase + " OWNER " + quotedOwner + " TEMPLATE template0",
		"REVOKE CONNECT, CREATE, TEMPORARY ON DATABASE " + quotedDatabase + " FROM PUBLIC",
		"GRANT CONNECT ON DATABASE " + quotedDatabase + " TO " + quotedProvisioner,
		"GRANT " + quotedOwner + " TO " + quotedProvisioner + " WITH ADMIN FALSE, INHERIT FALSE, SET TRUE",
	} {
		if _, err := admin.Exec(t.Context(), statement); err != nil {
			t.Fatalf("provision preflight fixture: %v", err)
		}
	}
	databaseAdminConfig := adminConfig.Copy()
	databaseAdminConfig.Database = database
	databaseAdmin, err := pgx.ConnectConfig(t.Context(), databaseAdminConfig)
	if err != nil {
		t.Fatalf("connect fixture database admin: %v", ErrPreflightObservationUnavailable)
	}
	t.Cleanup(func() { _ = databaseAdmin.Close(context.Background()) })

	provisionerConfig := adminConfig.Copy()
	provisionerConfig.Database = database
	provisionerConfig.User = provisioner
	provisionerConfig.Password = testSecret
	connection, err := pgx.ConnectConfig(t.Context(), provisionerConfig)
	if err != nil {
		t.Fatalf("connect fixture provisioner: %v", ErrPreflightObservationUnavailable)
	}
	t.Cleanup(func() { _ = connection.Close(context.Background()) })

	digestD := "sha256:" + strings.Repeat("d", 64)
	expectation := PostgreSQLClusterProbeExpectation{
		Database:        database,
		LoginRole:       provisioner,
		PostgreSQLMajor: migrations.AuthorityAccessPostgreSQLMajor,
		ServerIdentity:  provisionerConfig.Host,
		TLS: TLSProfile{
			CADigest:   digestD,
			CARef:      "trust/postgres-root-ca/generation-1",
			Mode:       "verify-full",
			ServerName: provisionerConfig.Host,
		},
	}
	identity, err := probePostgreSQLClusterIdentity(
		t.Context(),
		connection,
		expectation,
		func(*pgx.Conn, PostgreSQLClusterProbeExpectation) bool { return true },
	)
	if err != nil {
		t.Fatalf("probe fixture cluster: %v", err)
	}
	input := validPlanInput()
	input.AuthorityManifest = migrations.AuthorityAccessManifest{
		DatabaseName:        database,
		DatabaseOwnerRole:   databaseOwner,
		OwnerRole:           "wanwork_pf_schema_" + suffix,
		MigratorRole:        "wanwork_pf_migrator_" + suffix,
		RuntimeRole:         "wanwork_pf_runtime_" + suffix,
		MigrationLoginRoles: []string{"wanwork_pf_deploy_" + suffix},
		RuntimeLoginRoles:   []string{"wanwork_pf_app_" + suffix},
	}
	input.ClusterIdentity = identity
	input.Credentials = []CredentialGeneration{
		{Consumer: CredentialProvisioner, Generation: "generation-1", LoginRole: provisioner, SecretRef: "secret/postgres-cell-a/provisioner/generation-1"},
		{Consumer: CredentialMigration, Generation: "generation-1", LoginRole: input.AuthorityManifest.MigrationLoginRoles[0], SecretRef: "secret/postgres-cell-a/migration/generation-1"},
		{Consumer: CredentialRuntime, Generation: "generation-1", LoginRole: input.AuthorityManifest.RuntimeLoginRoles[0], SecretRef: "secret/postgres-cell-a/runtime/generation-1"},
	}
	input.ProvisionerGrantorRole = grantor
	input.ServerIdentity = expectation.ServerIdentity
	input.TLS = expectation.TLS
	plan, err := BuildPlan(input)
	if err != nil {
		t.Fatalf("BuildPlan fixture: %v", err)
	}
	observedAt := time.Date(2026, 8, 29, 23, 45, 0, 0, time.UTC)
	approvedAt := observedAt.Add(-5 * time.Minute)
	expiresAt := observedAt.Add(5 * time.Minute)
	seed := bytes.Repeat([]byte{0x51}, ed25519.SeedSize)
	privateKey := ed25519.NewKeyFromSeed(seed)
	publicKey := privateKey.Public().(ed25519.PublicKey)
	toSign, err := NewApprovalToSign(plan, "preflight-test-key", approvedAt, expiresAt)
	if err != nil {
		t.Fatalf("NewApprovalToSign fixture: %v", err)
	}
	raw, err := toSign.Encode(ed25519.Sign(privateKey, toSign.SigningBytes()))
	if err != nil {
		t.Fatalf("Encode fixture approval: %v", err)
	}
	verifier, err := newApprovalVerifierForTesting([]ApprovalVerificationKey{{
		ApproverIdentity: input.ApprovalIdentity,
		Generation:       "generation-1",
		KeyID:            "preflight-test-key",
		NotAfter:         time.Date(2026, 9, 1, 0, 0, 0, 0, time.UTC),
		NotBefore:        time.Date(2026, 8, 1, 0, 0, 0, 0, time.UTC),
		PolicyRevision:   "policy/preflight-test/revision-1",
		PublicKey:        publicKey,
		Scope: ApprovalVerificationScope{
			CellID:          input.CellID,
			DeploymentID:    input.DeploymentID,
			ReferencePrefix: "approval/postgres-cell-a/",
		},
	}}, 0)
	if err != nil {
		t.Fatalf("NewApprovalVerifier fixture: %v", err)
	}
	approval, err := verifier.Verify(plan, raw, observedAt)
	if err != nil {
		t.Fatalf("Verify fixture approval: %v", err)
	}
	return preflightPostgresFixture{
		admin:         admin,
		databaseAdmin: databaseAdmin,
		connection:    connection,
		plan:          plan,
		approval:      approval,
		artifacts: preflightArtifactFixture{
			backupDigest:  plan.Snapshot().Backup.AttestationDigest,
			releaseDigest: plan.Snapshot().Source.ReleaseArtifactDigest,
		},
		provisioner: provisioner,
		observedAt:  observedAt,
	}
}

func observePassingPreflightReport(
	t *testing.T,
	fixture preflightPostgresFixture,
) PreflightReport {
	t.Helper()
	report, err := observePreflightReport(
		t.Context(),
		fixture.connection,
		fixture.plan,
		fixture.approval,
		fixture.artifacts,
		fixture.observedAt,
		func(*pgx.Conn, PostgreSQLClusterProbeExpectation) bool { return true },
	)
	if err != nil {
		t.Fatalf("observePreflightReport: %v", err)
	}
	return report
}

func assertPreflightCheckOutcome(
	t *testing.T,
	report PreflightReport,
	checkID string,
	want PreflightCheckOutcome,
) {
	t.Helper()
	for _, check := range report.Snapshot().Checks {
		if check.CheckID == checkID {
			if check.Outcome != want {
				t.Fatalf("check %q outcome = %q, want %q", checkID, check.Outcome, want)
			}
			return
		}
	}
	t.Fatalf("missing check %q", checkID)
}

func assertPreflightObservationErrorHasNoSecrets(t *testing.T, err error) {
	t.Helper()
	lower := strings.ToLower(err.Error())
	for _, forbidden := range []string{"password", "preflight-canary", "postgresql://", "sslkey"} {
		if strings.Contains(lower, forbidden) {
			t.Fatalf("preflight observation error exposed %q: %v", forbidden, err)
		}
	}
}
