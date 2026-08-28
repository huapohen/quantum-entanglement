package authoritycutover

import (
	"strings"
	"testing"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/platform/postgres/migrations"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

func TestApprovalPolicyControlStoreContractDigestIsFrozen(t *testing.T) {
	const expected = "sha256:8a64c08cb0268beade6be94a410d865db4d384cd50107168e7d573386c23e4c5"
	if actual := CurrentApprovalPolicyControlStoreSchemaDigest(); actual != expected {
		t.Fatalf("control-store contract digest = %q, want %q", actual, expected)
	}
}

func TestApprovalPolicyControlStoreV2ContractDigestIsFrozen(t *testing.T) {
	const expected = "sha256:4ac5b4da641d2d31d814addc964a0ccf33749996ea3f81274741827374aa8de5"
	if actual := CurrentApprovalPolicyControlStoreSchemaDigestV2(); actual != expected {
		t.Fatalf("control-store v2 contract digest = %q, want %q", actual, expected)
	}
}

func TestApprovalPolicyControlStoreV2CatalogDigestIsFrozen(t *testing.T) {
	const expected = "sha256:e06225e0adf9452874f0db4cdd2fb7e584d3334015ef9f7e2d77af4b011ab3ce"
	if approvalPolicyControlStoreCatalogDigestV2 != expected {
		t.Fatalf("control-store v2 catalog digest = %q, want %q", approvalPolicyControlStoreCatalogDigestV2, expected)
	}
	if approvalPolicyControlStoreCatalogDigestDomainV2 == approvalPolicyControlStoreCatalogDigestDomain ||
		approvalPolicyControlStoreCatalogDigestV2 == approvalPolicyControlStoreCatalogDigest {
		t.Fatal("control-store v1 and v2 catalog attestations are not domain-separated")
	}
}

func TestPostgresApprovalPolicyStoreRequiresSeparateExactControlIdentity(t *testing.T) {
	fixture := newApprovalPolicyFixture(t)
	digest := "sha256:" + strings.Repeat("d", 64)
	expectation := ApprovalPolicyControlStoreExpectation{
		ControlDatabase:               "wanwork_policy_control_prod",
		ControlLoginRole:              "wanwork_policy_control_activator",
		ControlOwnerRole:              "wanwork_policy_control_owner",
		ControlReaderRole:             "wanwork_policy_control_reader",
		ControlPostgreSQLMajor:        migrations.AuthorityAccessPostgreSQLMajor,
		ControlServerIdentity:         "postgres-policy-control.prod.internal",
		ControlSystemIdentifierDigest: digest,
		ControlTLS: TLSProfile{
			CADigest:   digest,
			CARef:      "trust/postgres-policy-control/generation-1",
			Mode:       "verify-full",
			ServerName: "postgres-policy-control.prod.internal",
		},
		PolicyID:     fixture.input.PolicyID,
		PolicyTarget: fixture.input.Target,
	}
	pool := new(pgxpool.Pool)
	store, err := newPostgresApprovalPolicyActivationStore(
		pool,
		expectation,
		func(*pgx.Conn, PostgreSQLClusterProbeExpectation) bool { return true },
	)
	if err != nil || store.namespace != approvalPolicyNamespace(fixture.toSign.snapshot) {
		t.Fatalf("new control store = (%+v, %v)", store, err)
	}

	mutations := map[string]func(*ApprovalPolicyControlStoreExpectation){
		"v2 activator role supplied": func(value *ApprovalPolicyControlStoreExpectation) {
			value.ControlActivatorRole = "wanwork_policy_control_v2_activator"
		},
		"v2 fencer role supplied": func(value *ApprovalPolicyControlStoreExpectation) {
			value.ControlFencerRole = "wanwork_policy_control_v2_fencer"
		},
		"same physical cluster": func(value *ApprovalPolicyControlStoreExpectation) {
			value.ControlSystemIdentifierDigest = value.PolicyTarget.SystemIdentifierDigest
		},
		"owner is login": func(value *ApprovalPolicyControlStoreExpectation) {
			value.ControlOwnerRole = value.ControlLoginRole
		},
		"reader is activator": func(value *ApprovalPolicyControlStoreExpectation) {
			value.ControlReaderRole = value.ControlLoginRole
		},
		"server name drift": func(value *ApprovalPolicyControlStoreExpectation) {
			value.ControlTLS.ServerName = "other.prod.internal"
		},
		"wrong major": func(value *ApprovalPolicyControlStoreExpectation) {
			value.ControlPostgreSQLMajor++
		},
		"wrong policy namespace": func(value *ApprovalPolicyControlStoreExpectation) {
			value.PolicyID = "policy/not-an-approval-policy"
		},
	}
	for name, mutate := range mutations {
		t.Run(name, func(t *testing.T) {
			candidate := expectation
			mutate(&candidate)
			if _, err := newPostgresApprovalPolicyActivationStore(
				pool,
				candidate,
				func(*pgx.Conn, PostgreSQLClusterProbeExpectation) bool { return true },
			); err != ErrInvalidPostgresApprovalPolicyStore {
				t.Fatalf("constructor error = %v, want fixed %v", err, ErrInvalidPostgresApprovalPolicyStore)
			}
		})
	}
}

func TestPostgresApprovalPolicyStoreV2RequiresFiveSeparateControlRoles(t *testing.T) {
	fixture := newApprovalPolicyFixture(t)
	digest := "sha256:" + strings.Repeat("d", 64)
	expectation := ApprovalPolicyControlStoreExpectation{
		ControlActivatorRole:          "wanwork_policy_control_activator",
		ControlAttemptIssuerRole:      "wanwork_policy_control_attempt_issuer",
		ControlDatabase:               "wanwork_policy_control_prod",
		ControlFencerRole:             "wanwork_policy_control_fencer",
		ControlLoginRole:              "wanwork_policy_control_activator",
		ControlOwnerRole:              "wanwork_policy_control_owner",
		ControlReaderRole:             "wanwork_policy_control_reader",
		ControlPostgreSQLMajor:        migrations.AuthorityAccessPostgreSQLMajor,
		ControlServerIdentity:         "postgres-policy-control.prod.internal",
		ControlSystemIdentifierDigest: digest,
		ControlTLS: TLSProfile{
			CADigest:   digest,
			CARef:      "trust/postgres-policy-control/generation-1",
			Mode:       "verify-full",
			ServerName: "postgres-policy-control.prod.internal",
		},
		PolicyID:     fixture.input.PolicyID,
		PolicyTarget: fixture.input.Target,
	}
	pool := new(pgxpool.Pool)
	store, err := newPostgresApprovalPolicyActivationStoreV2(
		pool,
		expectation,
		func(*pgx.Conn, PostgreSQLClusterProbeExpectation) bool { return true },
	)
	if err != nil || store.schemaVersion != 2 ||
		store.namespace != approvalPolicyNamespace(fixture.toSign.snapshot) {
		t.Fatalf("new v2 control store = (%+v, %v)", store, err)
	}

	mutations := map[string]func(*ApprovalPolicyControlStoreExpectation){
		"login is reader": func(value *ApprovalPolicyControlStoreExpectation) {
			value.ControlLoginRole = value.ControlReaderRole
		},
		"login is fencer": func(value *ApprovalPolicyControlStoreExpectation) {
			value.ControlLoginRole = value.ControlFencerRole
		},
		"activator is owner": func(value *ApprovalPolicyControlStoreExpectation) {
			value.ControlActivatorRole = value.ControlOwnerRole
		},
		"activator is reader": func(value *ApprovalPolicyControlStoreExpectation) {
			value.ControlActivatorRole = value.ControlReaderRole
		},
		"fencer is owner": func(value *ApprovalPolicyControlStoreExpectation) {
			value.ControlFencerRole = value.ControlOwnerRole
		},
		"fencer is reader": func(value *ApprovalPolicyControlStoreExpectation) {
			value.ControlFencerRole = value.ControlReaderRole
		},
		"fencer is activator": func(value *ApprovalPolicyControlStoreExpectation) {
			value.ControlFencerRole = value.ControlActivatorRole
		},
		"missing fencer": func(value *ApprovalPolicyControlStoreExpectation) {
			value.ControlFencerRole = ""
		},
		"missing attempt issuer": func(value *ApprovalPolicyControlStoreExpectation) {
			value.ControlAttemptIssuerRole = ""
		},
	}
	for name, mutate := range mutations {
		t.Run(name, func(t *testing.T) {
			candidate := expectation
			mutate(&candidate)
			if _, err := newPostgresApprovalPolicyActivationStoreV2(
				pool,
				candidate,
				func(*pgx.Conn, PostgreSQLClusterProbeExpectation) bool { return true },
			); err != ErrInvalidPostgresApprovalPolicyStore {
				t.Fatalf("constructor error = %v, want fixed %v", err, ErrInvalidPostgresApprovalPolicyStore)
			}
		})
	}
}

func TestApprovalPolicyControlStoreV2ACLIsExactAndRoleSeparated(t *testing.T) {
	expectation := ApprovalPolicyControlStoreExpectation{
		ControlActivatorRole:     "control_activator",
		ControlAttemptIssuerRole: "control_attempt_issuer",
		ControlDatabase:          "control_database",
		ControlFencerRole:        "control_fencer",
		ControlOwnerRole:         "control_owner",
		ControlReaderRole:        "control_reader",
	}
	entries := expectedApprovalPolicyControlStoreACLV2(expectation)
	if len(entries) != 101 {
		t.Fatalf("v2 ACL entries = %d, want 101", len(entries))
	}
	seen := make(map[string]struct{}, len(entries))
	functionGrants := make(map[string]map[string]bool)
	for _, entry := range entries {
		key := entry.Kind + "\x00" + entry.Object + "\x00" + entry.Grantor + "\x00" +
			entry.Grantee + "\x00" + entry.Privilege
		if _, exists := seen[key]; exists {
			t.Fatalf("duplicate v2 ACL entry: %+v", entry)
		}
		seen[key] = struct{}{}
		if entry.Grantor != expectation.ControlOwnerRole || entry.Grantable {
			t.Fatalf("untrusted v2 ACL grant: %+v", entry)
		}
		if entry.Kind == "function" {
			if functionGrants[entry.Grantee] == nil {
				functionGrants[entry.Grantee] = make(map[string]bool)
			}
			functionGrants[entry.Grantee][entry.Object] = true
		}
	}
	expectedFunctions := map[string][]string{
		expectation.ControlOwnerRole: {
			approvalPolicyControlStoreActivateFunction,
			approvalPolicyControlStoreAdmissionFunction,
			approvalPolicyControlStoreAttemptIssueFunction,
			approvalPolicyControlStoreAttemptReadFunction,
			"approval_execution_attempt_admission_is_trusted",
			"approval_execution_attempt_is_valid",
			approvalPolicyControlStoreFenceOpenFunction,
			approvalPolicyControlStoreFenceReadFunction,
			approvalPolicyControlStoreIdentityFunction,
			approvalPolicyControlStoreReadFunction,
		},
		expectation.ControlReaderRole: {
			approvalPolicyControlStoreFenceReadFunction,
			approvalPolicyControlStoreIdentityFunction,
			approvalPolicyControlStoreReadFunction,
		},
		expectation.ControlActivatorRole: {
			approvalPolicyControlStoreActivateFunction,
			approvalPolicyControlStoreIdentityFunction,
			approvalPolicyControlStoreReadFunction,
		},
		expectation.ControlAttemptIssuerRole: {
			approvalPolicyControlStoreAttemptIssueFunction,
			approvalPolicyControlStoreAttemptReadFunction,
			approvalPolicyControlStoreIdentityFunction,
			approvalPolicyControlStoreReadFunction,
		},
		expectation.ControlFencerRole: {
			approvalPolicyControlStoreFenceOpenFunction,
			approvalPolicyControlStoreFenceReadFunction,
			approvalPolicyControlStoreIdentityFunction,
			approvalPolicyControlStoreReadFunction,
		},
	}
	for role, functions := range expectedFunctions {
		if len(functionGrants[role]) != len(functions) {
			t.Fatalf("v2 function grants for %q = %+v, want %+v", role, functionGrants[role], functions)
		}
		for _, function := range functions {
			if !functionGrants[role][function] {
				t.Fatalf("v2 role %q is missing EXECUTE on %q", role, function)
			}
		}
	}
	for _, role := range []string{
		expectation.ControlReaderRole,
		expectation.ControlActivatorRole,
		expectation.ControlAttemptIssuerRole,
		expectation.ControlFencerRole,
	} {
		if functionGrants[role][approvalPolicyControlStoreAdmissionFunction] {
			t.Fatalf("v2 functional role %q can execute owner-only admission validator", role)
		}
	}
}

func TestPostgresApprovalExecutionFenceStoreRequiresExactFencerCredential(t *testing.T) {
	fixture := newApprovalPolicyFixture(t)
	digest := "sha256:" + strings.Repeat("d", 64)
	expectation := ApprovalPolicyControlStoreExpectation{
		ControlActivatorRole:          "wanwork_policy_control_activator",
		ControlAttemptIssuerRole:      "wanwork_policy_control_attempt_issuer",
		ControlDatabase:               "wanwork_policy_control_prod",
		ControlFencerRole:             "wanwork_policy_control_fencer",
		ControlLoginRole:              "wanwork_policy_control_fencer",
		ControlOwnerRole:              "wanwork_policy_control_owner",
		ControlReaderRole:             "wanwork_policy_control_reader",
		ControlPostgreSQLMajor:        migrations.AuthorityAccessPostgreSQLMajor,
		ControlServerIdentity:         "postgres-policy-control.prod.internal",
		ControlSystemIdentifierDigest: digest,
		ControlTLS: TLSProfile{
			CADigest:   digest,
			CARef:      "trust/postgres-policy-control/generation-1",
			Mode:       "verify-full",
			ServerName: "postgres-policy-control.prod.internal",
		},
		PolicyID:     fixture.input.PolicyID,
		PolicyTarget: fixture.input.Target,
	}
	pool := new(pgxpool.Pool)
	store, err := newPostgresApprovalExecutionFenceStore(
		pool,
		expectation,
		func(*pgx.Conn, PostgreSQLClusterProbeExpectation) bool { return true },
	)
	if err != nil || store.namespace != approvalPolicyNamespace(fixture.toSign.snapshot) {
		t.Fatalf("new PostgreSQL fence store = (%+v, %v)", store, err)
	}
	for name, role := range map[string]string{
		"activator": expectation.ControlActivatorRole,
		"reader":    expectation.ControlReaderRole,
		"owner":     expectation.ControlOwnerRole,
		"unknown":   "wanwork_policy_control_unknown",
	} {
		t.Run(name, func(t *testing.T) {
			candidate := expectation
			candidate.ControlLoginRole = role
			if _, err := newPostgresApprovalExecutionFenceStore(
				pool,
				candidate,
				func(*pgx.Conn, PostgreSQLClusterProbeExpectation) bool { return true },
			); err != ErrInvalidPostgresApprovalPolicyStore {
				t.Fatalf("constructor error = %v, want fixed %v", err, ErrInvalidPostgresApprovalPolicyStore)
			}
		})
	}
}
