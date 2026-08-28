package authoritycutover

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/platform/postgres/migrations"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

var approvalPolicyControlStoreIntegrationSequence atomic.Uint64

type approvalPolicyControlStorePostgresFixture struct {
	activatorRole string
	admin         *pgx.Conn
	databaseAdmin *pgx.Conn
	expectation   ApprovalPolicyControlStoreExpectation
	ownerRole     string
	policy        approvalPolicyFixture
	pool          *pgxpool.Pool
	readerRole    string
	store         *PostgresApprovalPolicyActivationStore
	writerConfig  *pgxpool.Config
}

func TestPostgresApprovalPolicyStorePersistsExactPolicyChain(t *testing.T) {
	fixture := provisionApprovalPolicyControlStorePostgresFixture(t)
	const expectedCatalogDigest = approvalPolicyControlStoreCatalogDigest
	catalogDigest, err := readApprovalPolicyControlStoreCatalogDigest(t.Context(), fixture.databaseAdmin)
	if err != nil || catalogDigest != expectedCatalogDigest {
		t.Fatalf("control-store catalog digest = %q, %v; want %q", catalogDigest, err, expectedCatalogDigest)
	}
	namespace := approvalPolicyNamespace(fixture.policy.toSign.snapshot)
	if _, err := fixture.store.Load(t.Context(), namespace); err != ErrApprovalPolicyStoreEmpty {
		t.Fatalf("empty Load error = %v, want fixed %v", err, ErrApprovalPolicyStoreEmpty)
	}

	activator := mustApprovalPolicyActivator(t, fixture.policy.verifier, fixture.store)
	first, err := activator.Activate(t.Context(), fixture.policy.raw, fixture.policy.now)
	if err != nil {
		t.Fatalf("Activate genesis: %v", err)
	}
	_, secondRaw, secondInput := nextApprovalPolicy(t, fixture.policy, func(input *ApprovalPolicyInput) {
		input.DenyAll = true
		input.Keys[1].Status = ApprovalPolicyKeyRevoked
		input.Keys[1].RevokedAt = input.NotBefore
		input.Keys[1].RevocationReason = "revocation/control-store-integration"
	})
	second, err := activator.Activate(t.Context(), secondRaw, secondInput.NotBefore.Add(1))
	if err != nil {
		t.Fatalf("Activate revision two: %v", err)
	}
	if first.Revision() != 1 || second.Revision() != 2 ||
		second.ActivationRecord().PreviousPolicyDigest != first.PolicyDigest() ||
		second.ApprovalVerificationEnabled() {
		t.Fatalf("persisted chain = first %+v, second %+v", first.ActivationRecord(), second.ActivationRecord())
	}

	fixture.pool.Close()
	reopenedPool, err := pgxpool.NewWithConfig(t.Context(), fixture.writerConfig.Copy())
	if err != nil {
		t.Fatalf("reopen writer pool: %v", ErrApprovalPolicyStoreUnavailable)
	}
	t.Cleanup(reopenedPool.Close)
	reopened, err := newPostgresApprovalPolicyActivationStore(
		reopenedPool,
		fixture.expectation,
		func(*pgx.Conn, PostgreSQLClusterProbeExpectation) bool { return true },
	)
	if err != nil {
		t.Fatalf("reopen store: %v", err)
	}
	state, err := reopened.Load(t.Context(), namespace)
	if err != nil {
		t.Fatalf("Load after pool rebuild: %v", err)
	}
	if state.Head != second.ActivationRecord().Head() ||
		!strings.Contains(string(state.CanonicalPolicy), `"denyAll":true`) {
		t.Fatalf("rebuilt store state = %+v", state.Head)
	}

	var archiveCount, headCount, recordCount int
	if err := fixture.databaseAdmin.QueryRow(t.Context(), `
SELECT
    (SELECT pg_catalog.count(*) FROM wanwork_policy_control.approval_policy_archive),
    (SELECT pg_catalog.count(*) FROM wanwork_policy_control.approval_policy_head),
    (SELECT pg_catalog.count(*) FROM wanwork_policy_control.approval_policy_activation_record)`).Scan(
		&archiveCount,
		&headCount,
		&recordCount,
	); err != nil || archiveCount != 2 || headCount != 1 || recordCount != 2 {
		t.Fatalf("durable row counts = (%d, %d, %d, %v)", archiveCount, headCount, recordCount, err)
	}
}

func TestPostgresApprovalPolicyStoreSerializesConcurrentCandidates(t *testing.T) {
	fixture := provisionApprovalPolicyControlStorePostgresFixture(t)
	activator := mustApprovalPolicyActivator(t, fixture.policy.verifier, fixture.store)

	const workers = 64
	genesisErrors := make(chan error, workers)
	var wait sync.WaitGroup
	for range workers {
		wait.Add(1)
		go func() {
			defer wait.Done()
			activated, err := activator.Activate(t.Context(), fixture.policy.raw, fixture.policy.now)
			if err == nil && activated.PolicyDigest() != fixture.policy.toSign.PolicyDigest() {
				err = ErrInvalidApprovalPolicyStoreState
			}
			genesisErrors <- err
		}()
	}
	wait.Wait()
	close(genesisErrors)
	for err := range genesisErrors {
		if err != nil {
			t.Fatalf("same-candidate concurrent activation: %v", err)
		}
	}

	firstToSign, firstRaw, firstInput := nextApprovalPolicy(t, fixture.policy, nil)
	secondToSign, secondRaw, secondInput := nextApprovalPolicy(t, fixture.policy, func(input *ApprovalPolicyInput) {
		input.MaximumApprovalLifetime = 9 * time.Minute
	})
	type forkResult struct {
		digest string
		err    error
	}
	forkResults := make(chan forkResult, workers)
	for index := range workers {
		wait.Add(1)
		go func(useFirst bool) {
			defer wait.Done()
			raw := secondRaw
			now := secondInput.NotBefore.Add(time.Minute)
			if useFirst {
				raw = firstRaw
				now = firstInput.NotBefore.Add(time.Minute)
			}
			activated, err := activator.Activate(t.Context(), raw, now)
			result := forkResult{err: err}
			if err == nil {
				result.digest = activated.PolicyDigest()
			}
			forkResults <- result
		}(index%2 == 0)
	}
	wait.Wait()
	close(forkResults)
	winnerDigests := make(map[string]int)
	for result := range forkResults {
		if result.err == nil {
			winnerDigests[result.digest]++
			continue
		}
		if !errors.Is(result.err, ErrApprovalPolicyFork) &&
			!errors.Is(result.err, ErrApprovalPolicyActivationConflict) {
			t.Fatalf("fork candidate error = %v", result.err)
		}
	}
	if len(winnerDigests) != 1 {
		t.Fatalf("durable fork winners = %v", winnerDigests)
	}
	for digest := range winnerDigests {
		if digest != firstToSign.PolicyDigest() && digest != secondToSign.PolicyDigest() {
			t.Fatalf("unknown fork winner digest %q", digest)
		}
	}
	var archives, heads, records int
	if err := fixture.databaseAdmin.QueryRow(t.Context(), `
SELECT
    (SELECT pg_catalog.count(*) FROM wanwork_policy_control.approval_policy_archive),
    (SELECT pg_catalog.count(*) FROM wanwork_policy_control.approval_policy_head),
    (SELECT pg_catalog.count(*) FROM wanwork_policy_control.approval_policy_activation_record)`).Scan(
		&archives,
		&heads,
		&records,
	); err != nil || archives != 2 || heads != 1 || records != 2 {
		t.Fatalf("concurrent CAS rows = (%d, %d, %d, %v)", archives, heads, records, err)
	}
}

func TestPostgresApprovalPolicyStoreRejectsDirectMutationAndCatalogDrift(t *testing.T) {
	fixture := provisionApprovalPolicyControlStorePostgresFixture(t)

	for name, statement := range map[string]string{
		"select":   "SELECT * FROM wanwork_policy_control.approval_policy_head",
		"insert":   "INSERT INTO wanwork_policy_control.approval_policy_head DEFAULT VALUES",
		"update":   "UPDATE wanwork_policy_control.approval_policy_head SET revision = revision",
		"delete":   "DELETE FROM wanwork_policy_control.approval_policy_head",
		"truncate": "TRUNCATE wanwork_policy_control.approval_policy_head",
	} {
		t.Run("activator cannot "+name, func(t *testing.T) {
			if _, err := fixture.pool.Exec(t.Context(), statement); err == nil {
				t.Fatalf("activator unexpectedly executed %s", name)
			}
		})
	}

	readerConfig := fixture.writerConfig.Copy()
	readerConfig.ConnConfig.User = fixture.readerRole
	readerPool, err := pgxpool.NewWithConfig(t.Context(), readerConfig)
	if err != nil {
		t.Fatalf("open reader pool: %v", ErrApprovalPolicyStoreUnavailable)
	}
	t.Cleanup(readerPool.Close)
	if _, err := readerPool.Exec(t.Context(), `
SELECT wanwork_policy_control.compare_and_activate_approval_policy(
    'approval-policy/denied',
    'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    0, '', '', 1,
    'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
    'sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
    '{}'::text::bytea,
    '{}'::text::bytea
)`); err == nil {
		t.Fatal("reader executed activation CAS")
	}
	var rejectedOutcome string
	if err := fixture.pool.QueryRow(t.Context(), `
SELECT wanwork_policy_control.compare_and_activate_approval_policy(
    'approval-policy/denied',
    'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    0, '', '', 1,
    'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
    'sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
    '{}'::text::bytea,
    '{}'::text::bytea
)`).Scan(&rejectedOutcome); err != nil || rejectedOutcome != "rejected" {
		t.Fatalf("malformed activator call = (%q, %v)", rejectedOutcome, err)
	}
	var readerState string
	if err := readerPool.QueryRow(t.Context(), `
SELECT state.state_status
FROM wanwork_policy_control.read_approval_policy_state($1, $2) AS state`,
		fixture.policy.input.PolicyID,
		digestApprovalPolicyTarget(fixture.policy.input.Target),
	).Scan(&readerState); err != nil || readerState != "empty" {
		t.Fatalf("reader state = (%q, %v)", readerState, err)
	}

	if _, err := fixture.databaseAdmin.Exec(t.Context(), `
ALTER FUNCTION wanwork_policy_control.read_approval_policy_state(text, text) SECURITY INVOKER`); err != nil {
		t.Fatalf("tamper read function: %v", err)
	}
	_, err = fixture.store.Load(
		t.Context(),
		approvalPolicyNamespace(fixture.policy.toSign.snapshot),
	)
	if err != ErrUntrustedPostgresApprovalPolicyStore {
		t.Fatalf("catalog drift error = %v, want fixed %v", err, ErrUntrustedPostgresApprovalPolicyStore)
	}
	assertApprovalPolicyControlStoreErrorHasNoSecrets(t, err)
}

func TestPostgresApprovalPolicyStoreRejectsACLAndRoleGraphDrift(t *testing.T) {
	t.Run("table grant", func(t *testing.T) {
		fixture := provisionApprovalPolicyControlStorePostgresFixture(t)
		if _, err := fixture.databaseAdmin.Exec(t.Context(),
			"GRANT SELECT ON wanwork_policy_control.approval_policy_head TO "+
				pgx.Identifier{fixture.readerRole}.Sanitize(),
		); err != nil {
			t.Fatalf("grant rogue table access: %v", err)
		}
		if _, err := fixture.store.Load(
			t.Context(),
			approvalPolicyNamespace(fixture.policy.toSign.snapshot),
		); err != ErrUntrustedPostgresApprovalPolicyStore {
			t.Fatalf("ACL drift error = %v, want fixed %v", err, ErrUntrustedPostgresApprovalPolicyStore)
		}
	})

	t.Run("owner membership", func(t *testing.T) {
		fixture := provisionApprovalPolicyControlStorePostgresFixture(t)
		if _, err := fixture.admin.Exec(t.Context(),
			"GRANT "+pgx.Identifier{fixture.ownerRole}.Sanitize()+" TO "+
				pgx.Identifier{fixture.activatorRole}.Sanitize()+
				" WITH ADMIN FALSE, INHERIT FALSE, SET TRUE",
		); err != nil {
			t.Fatalf("grant rogue owner membership: %v", err)
		}
		if _, err := fixture.store.Load(
			t.Context(),
			approvalPolicyNamespace(fixture.policy.toSign.snapshot),
		); err != ErrUntrustedPostgresApprovalPolicyStore {
			t.Fatalf("role drift error = %v, want fixed %v", err, ErrUntrustedPostgresApprovalPolicyStore)
		}
	})
}

func TestPostgresApprovalPolicyStoreRejectsEveryExpectedHeadDriftWithoutOrphans(t *testing.T) {
	fixture := provisionApprovalPolicyControlStorePostgresFixture(t)
	activator := mustApprovalPolicyActivator(t, fixture.policy.verifier, fixture.store)
	first, err := activator.Activate(t.Context(), fixture.policy.raw, fixture.policy.now)
	if err != nil {
		t.Fatalf("Activate genesis: %v", err)
	}
	_, secondRaw, secondInput := nextApprovalPolicy(t, fixture.policy, nil)
	secondPolicy, err := fixture.policy.verifier.Verify(secondRaw, secondInput.NotBefore.Add(time.Minute))
	if err != nil {
		t.Fatalf("verify second policy: %v", err)
	}
	secondRecord, err := newApprovalPolicyActivationRecord(
		secondPolicy,
		secondInput.NotBefore.Add(time.Minute),
	)
	if err != nil {
		t.Fatalf("build second activation record: %v", err)
	}
	canonicalRecord, err := marshalApprovalPolicyActivationRecordCanonical(secondRecord)
	if err != nil {
		t.Fatalf("marshal second activation record: %v", err)
	}
	namespace := approvalPolicyNamespace(fixture.policy.toSign.snapshot)
	head := first.ActivationRecord().Head()
	digestE := "sha256:" + strings.Repeat("e", 64)
	digestF := "sha256:" + strings.Repeat("f", 64)
	tests := map[string]struct {
		policyID                 string
		targetDigest             string
		expectedRevision         int64
		expectedPolicyDigest     string
		expectedActivationDigest string
		want                     string
	}{
		"activation digest": {
			policyID:                 namespace.PolicyID,
			targetDigest:             namespace.TargetDigest,
			expectedRevision:         int64(head.Revision),
			expectedPolicyDigest:     head.PolicyDigest,
			expectedActivationDigest: digestE,
			want:                     "conflict",
		},
		"policy digest": {
			policyID:                 namespace.PolicyID,
			targetDigest:             namespace.TargetDigest,
			expectedRevision:         int64(head.Revision),
			expectedPolicyDigest:     digestF,
			expectedActivationDigest: head.ActivationRecordDigest,
			want:                     "rejected",
		},
		"revision": {
			policyID:                 namespace.PolicyID,
			targetDigest:             namespace.TargetDigest,
			expectedRevision:         0,
			expectedPolicyDigest:     "",
			expectedActivationDigest: "",
			want:                     "rejected",
		},
		"target namespace": {
			policyID:                 namespace.PolicyID,
			targetDigest:             digestE,
			expectedRevision:         int64(head.Revision),
			expectedPolicyDigest:     head.PolicyDigest,
			expectedActivationDigest: head.ActivationRecordDigest,
			want:                     "rejected",
		},
	}
	for name, test := range tests {
		t.Run(name, func(t *testing.T) {
			var outcome string
			err := fixture.pool.QueryRow(t.Context(), `
SELECT wanwork_policy_control.compare_and_activate_approval_policy(
    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10
)`,
				test.policyID,
				test.targetDigest,
				test.expectedRevision,
				test.expectedPolicyDigest,
				test.expectedActivationDigest,
				int64(secondPolicy.Revision()),
				secondPolicy.PolicyDigest(),
				secondRecord.ActivationRecordDigest,
				secondPolicy.CanonicalBytes(),
				canonicalRecord,
			).Scan(&outcome)
			if err != nil || outcome != test.want {
				t.Fatalf("CAS outcome = (%q, %v), want %q", outcome, err, test.want)
			}
		})
	}
	var archives, records int
	if err := fixture.databaseAdmin.QueryRow(t.Context(), `
SELECT
    (SELECT pg_catalog.count(*) FROM wanwork_policy_control.approval_policy_archive),
    (SELECT pg_catalog.count(*) FROM wanwork_policy_control.approval_policy_activation_record)`).Scan(
		&archives,
		&records,
	); err != nil || archives != 1 || records != 1 {
		t.Fatalf("failed CAS left rows = (%d, %d, %v)", archives, records, err)
	}
}

func TestPostgresApprovalPolicyStoreDistinguishesEmptyFromCorruptHistory(t *testing.T) {
	t.Run("orphan archive", func(t *testing.T) {
		fixture := provisionApprovalPolicyControlStorePostgresFixture(t)
		namespace := approvalPolicyNamespace(fixture.policy.toSign.snapshot)
		verified, err := fixture.policy.verifier.Verify(fixture.policy.raw, fixture.policy.now)
		if err != nil {
			t.Fatalf("verify policy: %v", err)
		}
		if _, err := fixture.databaseAdmin.Exec(t.Context(), `
INSERT INTO wanwork_policy_control.approval_policy_archive (
    policy_id, target_digest, revision, policy_digest, previous_policy_digest, canonical_policy
) VALUES ($1, $2, 1, $3, '', $4)`,
			namespace.PolicyID,
			namespace.TargetDigest,
			verified.PolicyDigest(),
			verified.CanonicalBytes(),
		); err != nil {
			t.Fatalf("seed orphan archive: %v", err)
		}
		if _, err := fixture.store.Load(t.Context(), namespace); err != ErrInvalidApprovalPolicyStoreState {
			t.Fatalf("orphan Load error = %v, want fixed %v", err, ErrInvalidApprovalPolicyStoreState)
		}
	})

	t.Run("missing historical record", func(t *testing.T) {
		fixture := provisionApprovalPolicyControlStorePostgresFixture(t)
		activator := mustApprovalPolicyActivator(t, fixture.policy.verifier, fixture.store)
		if _, err := activator.Activate(t.Context(), fixture.policy.raw, fixture.policy.now); err != nil {
			t.Fatalf("Activate genesis: %v", err)
		}
		_, secondRaw, secondInput := nextApprovalPolicy(t, fixture.policy, nil)
		if _, err := activator.Activate(t.Context(), secondRaw, secondInput.NotBefore.Add(time.Minute)); err != nil {
			t.Fatalf("Activate second: %v", err)
		}
		if _, err := fixture.databaseAdmin.Exec(t.Context(), `
SET session_replication_role = replica;
DELETE FROM wanwork_policy_control.approval_policy_activation_record WHERE revision = 1;
DELETE FROM wanwork_policy_control.approval_policy_archive WHERE revision = 1;
SET session_replication_role = origin;`); err != nil {
			t.Fatalf("simulate historical corruption: %v", err)
		}
		if _, err := fixture.store.Load(
			t.Context(),
			approvalPolicyNamespace(fixture.policy.toSign.snapshot),
		); err != ErrInvalidApprovalPolicyStoreState {
			t.Fatalf("broken history Load error = %v, want fixed %v", err, ErrInvalidApprovalPolicyStoreState)
		}
	})
}

func TestPostgresApprovalPolicyStoreQuarantinesCanceledCASAndRecoversFresh(t *testing.T) {
	fixture := provisionApprovalPolicyControlStorePostgresFixture(t)
	namespace := approvalPolicyNamespace(fixture.policy.toSign.snapshot)
	lockTransaction, err := fixture.databaseAdmin.Begin(t.Context())
	if err != nil {
		t.Fatalf("begin namespace lock: %v", err)
	}
	defer func() { _ = lockTransaction.Rollback(context.Background()) }()
	if _, err := lockTransaction.Exec(t.Context(), `
SELECT pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended($1 || E'\\000' || $2, 7318470029)
)`, namespace.PolicyID, namespace.TargetDigest); err != nil {
		t.Fatalf("hold namespace lock: %v", err)
	}

	ctx, cancel := context.WithTimeout(t.Context(), 300*time.Millisecond)
	defer cancel()
	activator := mustApprovalPolicyActivator(t, fixture.policy.verifier, fixture.store)
	if _, err := activator.Activate(ctx, fixture.policy.raw, fixture.policy.now); err != ErrApprovalPolicyCommitUncertain {
		t.Fatalf("canceled CAS error = %v, want fixed %v", err, ErrApprovalPolicyCommitUncertain)
	}
	if err := lockTransaction.Rollback(t.Context()); err != nil {
		t.Fatalf("release namespace lock: %v", err)
	}
	var archives, heads, records int
	if err := fixture.databaseAdmin.QueryRow(t.Context(), `
SELECT
    (SELECT pg_catalog.count(*) FROM wanwork_policy_control.approval_policy_archive),
    (SELECT pg_catalog.count(*) FROM wanwork_policy_control.approval_policy_head),
    (SELECT pg_catalog.count(*) FROM wanwork_policy_control.approval_policy_activation_record)`).Scan(
		&archives,
		&heads,
		&records,
	); err != nil || archives != 0 || heads != 0 || records != 0 {
		t.Fatalf("canceled CAS rows = (%d, %d, %d, %v)", archives, heads, records, err)
	}
	activated, err := activator.Activate(t.Context(), fixture.policy.raw, fixture.policy.now)
	if err != nil || activated.PolicyDigest() != fixture.policy.toSign.PolicyDigest() {
		t.Fatalf("fresh recovery activation = (%+v, %v)", activated, err)
	}
}

func provisionApprovalPolicyControlStorePostgresFixture(
	t *testing.T,
) approvalPolicyControlStorePostgresFixture {
	t.Helper()
	adminURL := os.Getenv(clusterProbeIntegrationURL)
	if adminURL == "" {
		t.Skip(clusterProbeIntegrationURL + " is not set")
	}
	adminConfig, err := pgx.ParseConfig(adminURL)
	if err != nil {
		t.Fatalf("parse integration admin URL: %v", ErrApprovalPolicyStoreUnavailable)
	}
	admin, err := pgx.ConnectConfig(t.Context(), adminConfig)
	if err != nil {
		t.Fatalf("connect integration admin: %v", ErrApprovalPolicyStoreUnavailable)
	}
	t.Cleanup(func() { _ = admin.Close(context.Background()) })

	suffix := fmt.Sprintf("%d_%d", os.Getpid(), approvalPolicyControlStoreIntegrationSequence.Add(1))
	database := "wanwork_pc_" + suffix
	ownerRole := "wanwork_pc_owner_" + suffix
	readerRole := "wanwork_pc_reader_" + suffix
	activatorRole := "wanwork_pc_activator_" + suffix
	testSecret := "wanwork_policy_control_test_secret_" + suffix
	quotedDatabase := pgx.Identifier{database}.Sanitize()
	quotedOwner := pgx.Identifier{ownerRole}.Sanitize()
	quotedReader := pgx.Identifier{readerRole}.Sanitize()
	quotedActivator := pgx.Identifier{activatorRole}.Sanitize()
	quotedAdmin := pgx.Identifier{adminConfig.User}.Sanitize()
	t.Cleanup(func() {
		_, _ = admin.Exec(context.Background(), "RESET ROLE")
		_, _ = admin.Exec(context.Background(), "DROP DATABASE IF EXISTS "+quotedDatabase+" WITH (FORCE)")
		_, _ = admin.Exec(context.Background(), "REVOKE "+quotedOwner+" FROM "+quotedAdmin)
		_, _ = admin.Exec(context.Background(), "DROP ROLE IF EXISTS "+quotedActivator)
		_, _ = admin.Exec(context.Background(), "DROP ROLE IF EXISTS "+quotedReader)
		_, _ = admin.Exec(context.Background(), "DROP ROLE IF EXISTS "+quotedOwner)
	})
	for _, statement := range []string{
		"CREATE ROLE " + quotedOwner + " NOLOGIN NOSUPERUSER NOINHERIT NOCREATEROLE NOCREATEDB NOREPLICATION NOBYPASSRLS CONNECTION LIMIT -1",
		"CREATE ROLE " + quotedReader + " LOGIN NOSUPERUSER NOINHERIT NOCREATEROLE NOCREATEDB NOREPLICATION NOBYPASSRLS CONNECTION LIMIT -1 PASSWORD '" + testSecret + "'",
		"CREATE ROLE " + quotedActivator + " LOGIN NOSUPERUSER NOINHERIT NOCREATEROLE NOCREATEDB NOREPLICATION NOBYPASSRLS CONNECTION LIMIT -1 PASSWORD '" + testSecret + "'",
		"CREATE DATABASE " + quotedDatabase + " OWNER " + quotedOwner + " TEMPLATE template0 ENCODING 'UTF8'",
		"REVOKE ALL ON DATABASE " + quotedDatabase + " FROM PUBLIC",
		"REVOKE ALL ON DATABASE " + quotedDatabase + " FROM " + quotedReader,
		"REVOKE ALL ON DATABASE " + quotedDatabase + " FROM " + quotedActivator,
		"GRANT CONNECT ON DATABASE " + quotedDatabase + " TO " + quotedReader,
		"GRANT CONNECT ON DATABASE " + quotedDatabase + " TO " + quotedActivator,
		"GRANT " + quotedOwner + " TO " + quotedAdmin + " WITH ADMIN FALSE, INHERIT FALSE, SET TRUE",
	} {
		if _, err := admin.Exec(t.Context(), statement); err != nil {
			t.Fatalf("provision control-store fixture: %v", err)
		}
	}

	databaseAdminConfig := adminConfig.Copy()
	databaseAdminConfig.Database = database
	databaseAdmin, err := pgx.ConnectConfig(t.Context(), databaseAdminConfig)
	if err != nil {
		t.Fatalf("connect control database admin: %v", ErrApprovalPolicyStoreUnavailable)
	}
	t.Cleanup(func() { _ = databaseAdmin.Close(context.Background()) })
	schemaSQL := renderApprovalPolicyControlStoreSchema(t, quotedOwner, quotedReader, quotedActivator)
	if _, err := databaseAdmin.Exec(t.Context(), schemaSQL); err != nil {
		t.Fatalf("install control-store schema: %v", err)
	}

	writerConfig, err := pgxpool.ParseConfig(adminURL)
	if err != nil {
		t.Fatalf("parse writer pool config: %v", ErrApprovalPolicyStoreUnavailable)
	}
	writerConfig.ConnConfig.Database = database
	writerConfig.ConnConfig.User = activatorRole
	writerConfig.ConnConfig.Password = testSecret
	writerConfig.MaxConns = 70
	pool, err := pgxpool.NewWithConfig(t.Context(), writerConfig.Copy())
	if err != nil {
		t.Fatalf("open writer pool: %v", ErrApprovalPolicyStoreUnavailable)
	}
	t.Cleanup(pool.Close)

	var systemIdentifier string
	if err := databaseAdmin.QueryRow(t.Context(), `
SELECT control.system_identifier::text FROM pg_catalog.pg_control_system() AS control`).Scan(
		&systemIdentifier,
	); err != nil {
		t.Fatalf("read control system identifier: %v", ErrApprovalPolicyStoreUnavailable)
	}
	policy := newApprovalPolicyControlStorePolicyFixture(t, systemIdentifier)
	digest := "sha256:" + strings.Repeat("d", 64)
	expectation := ApprovalPolicyControlStoreExpectation{
		ControlDatabase:               database,
		ControlLoginRole:              activatorRole,
		ControlOwnerRole:              ownerRole,
		ControlReaderRole:             readerRole,
		ControlPostgreSQLMajor:        migrations.AuthorityAccessPostgreSQLMajor,
		ControlServerIdentity:         writerConfig.ConnConfig.Host,
		ControlSystemIdentifierDigest: digestPostgreSQLSystemIdentifier(systemIdentifier),
		ControlTLS: TLSProfile{
			CADigest:   digest,
			CARef:      "trust/postgres-policy-control/generation-1",
			Mode:       "verify-full",
			ServerName: writerConfig.ConnConfig.Host,
		},
		PolicyID:     policy.input.PolicyID,
		PolicyTarget: policy.input.Target,
	}
	store, err := newPostgresApprovalPolicyActivationStore(
		pool,
		expectation,
		func(*pgx.Conn, PostgreSQLClusterProbeExpectation) bool { return true },
	)
	if err != nil {
		t.Fatalf("construct control store: %v", err)
	}
	return approvalPolicyControlStorePostgresFixture{
		activatorRole: activatorRole,
		admin:         admin,
		databaseAdmin: databaseAdmin,
		expectation:   expectation,
		ownerRole:     ownerRole,
		policy:        policy,
		pool:          pool,
		readerRole:    readerRole,
		store:         store,
		writerConfig:  writerConfig,
	}
}

func newApprovalPolicyControlStorePolicyFixture(
	t *testing.T,
	controlSystemIdentifier string,
) approvalPolicyFixture {
	t.Helper()
	fixture := newApprovalPolicyFixture(t)
	targetSystemIdentifier := controlSystemIdentifier + "7"
	if !canonicalPostgreSQLSystemIdentifier.MatchString(targetSystemIdentifier) {
		targetSystemIdentifier = "8678902413432981444"
	}
	fixture.input.Target.SystemIdentifierDigest = digestPostgreSQLSystemIdentifier(targetSystemIdentifier)
	toSign, err := NewApprovalPolicyToSign(fixture.input)
	if err != nil {
		t.Fatalf("rebuild control-store policy: %v", err)
	}
	fixture.bundle.Target = fixture.input.Target
	verifier, err := NewApprovalPolicyVerifier(fixture.bundle, 0)
	if err != nil {
		t.Fatalf("rebuild control-store verifier: %v", err)
	}
	fixture.toSign = toSign
	fixture.verifier = verifier
	fixture.raw = signApprovalPolicy(t, toSign, fixture.rootKeys, []int{0, 1})
	return fixture
}

func renderApprovalPolicyControlStoreSchema(
	t *testing.T,
	quotedOwner string,
	quotedReader string,
	quotedActivator string,
) string {
	t.Helper()
	_, filename, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("resolve integration test source path")
	}
	path := filepath.Clean(filepath.Join(
		filepath.Dir(filename),
		"..", "..", "..", "..", "..", "..",
		"deploy", "postgres", "approval-policy-control-store", "schema.psql",
	))
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read control-store schema: %v", err)
	}
	lines := make([]string, 0)
	for _, line := range strings.Split(string(raw), "\n") {
		if strings.HasPrefix(strings.TrimSpace(line), `\`) {
			continue
		}
		lines = append(lines, line)
	}
	rendered := strings.Join(lines, "\n")
	rendered = strings.ReplaceAll(rendered, `:"owner_role"`, quotedOwner)
	rendered = strings.ReplaceAll(rendered, `:"reader_role"`, quotedReader)
	rendered = strings.ReplaceAll(rendered, `:"activator_role"`, quotedActivator)
	if strings.Contains(rendered, `:"owner_role"`) || strings.Contains(rendered, `:"reader_role"`) ||
		strings.Contains(rendered, `:"activator_role"`) {
		t.Fatal("unresolved psql identifier in control-store schema")
	}
	return rendered
}

func assertApprovalPolicyControlStoreErrorHasNoSecrets(t *testing.T, err error) {
	t.Helper()
	if err == nil {
		t.Fatal("expected control-store error")
	}
	for _, forbidden := range []string{
		"postgresql://",
		"wanwork_policy_control_test_secret_",
		`"rootSignatures"`,
		`"publicKey"`,
	} {
		if strings.Contains(err.Error(), forbidden) {
			t.Fatalf("control-store error leaked %q: %v", forbidden, err)
		}
	}
}
