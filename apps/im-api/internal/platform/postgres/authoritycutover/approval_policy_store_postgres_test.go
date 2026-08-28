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
