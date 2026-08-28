package authoritycutover

import (
	"bytes"
	"errors"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/platform/postgres/migrations"
)

func TestBuildPlanIsDeterministicImmutableAndSemanticallyBound(t *testing.T) {
	input := validPlanInput()
	first, err := BuildPlan(input)
	if err != nil {
		t.Fatalf("BuildPlan: %v", err)
	}
	if !canonicalDigest.MatchString(first.Digest()) {
		t.Fatalf("plan digest = %q", first.Digest())
	}
	snapshot := first.Snapshot()
	if snapshot.PlanDigest != first.Digest() || snapshot.Approval.ExactPlanDigest != first.Digest() {
		t.Fatalf("self-binding digest = %#v", snapshot)
	}
	if snapshot.Source.MigrationCatalogDigest == "" || snapshot.Authority.ManifestDigest == "" ||
		snapshot.Authority.SpecificationDigest == "" {
		t.Fatalf("derived binding is incomplete: %#v", snapshot)
	}

	reordered := validPlanInput()
	slices.Reverse(reordered.AbortConditions)
	slices.Reverse(reordered.Credentials)
	slices.Reverse(reordered.AuthorityManifest.MigrationLoginRoles)
	slices.Reverse(reordered.AuthorityManifest.RuntimeLoginRoles)
	second, err := BuildPlan(reordered)
	if err != nil {
		t.Fatalf("BuildPlan reordered: %v", err)
	}
	if first.Digest() != second.Digest() || !bytes.Equal(first.CanonicalBytes(), second.CanonicalBytes()) {
		t.Fatalf("semantic set ordering changed plan\nfirst: %s\nsecond: %s", first.CanonicalBytes(), second.CanonicalBytes())
	}

	canonicalCopy := first.CanonicalBytes()
	canonicalCopy[0] ^= 0xff
	snapshot.AbortConditions[0] = "mutated"
	snapshot.Credentials[0].SecretRef = "secret-ref/mutated"
	snapshot.Authority.Manifest.MigrationLoginRoles[0] = "mutated"
	if !bytes.Equal(first.CanonicalBytes(), second.CanonicalBytes()) ||
		first.Snapshot().AbortConditions[0] == "mutated" ||
		first.Snapshot().Credentials[0].SecretRef == "secret-ref/mutated" ||
		first.Snapshot().Authority.Manifest.MigrationLoginRoles[0] == "mutated" {
		t.Fatal("caller mutation escaped immutable plan boundary")
	}

	changed := validPlanInput()
	changed.EvidenceDestination = "evidence/postgres-cell-a/revision-2"
	third, err := BuildPlan(changed)
	if err != nil {
		t.Fatalf("BuildPlan changed: %v", err)
	}
	if third.Digest() == first.Digest() {
		t.Fatal("semantic change did not change plan digest")
	}

	lower := strings.ToLower(string(first.CanonicalBytes()))
	for _, forbidden := range []string{"password", "privatekey", "connectionstring", "dsn", "token"} {
		if strings.Contains(lower, forbidden) {
			t.Fatalf("plan contains credential-bearing field %q", forbidden)
		}
	}
}

func TestBuildPlanRejectsIncompleteOrUnsafeSemantics(t *testing.T) {
	tests := map[string]func(*PlanInput){
		"wrong postgres major":                    func(input *PlanInput) { input.PostgreSQLMajor = 17 },
		"transport without hostname verification": func(input *PlanInput) { input.TLS.Mode = "require" },
		"tls identity mismatch": func(input *PlanInput) {
			input.TLS.ServerName = "postgres-reader.prod.internal"
		},
		"missing backup":            func(input *PlanInput) { input.Backup.Required = false },
		"implicit classification":   func(input *PlanInput) { input.NonEmptyClassification = "" },
		"reordered phases":          func(input *PlanInput) { input.Steps[0], input.Steps[1] = input.Steps[1], input.Steps[0] },
		"missing phase":             func(input *PlanInput) { input.Steps = input.Steps[:4] },
		"wrong executor":            func(input *PlanInput) { input.Steps[0].RequiredExecutor = ExecutorOwner },
		"duplicate abort condition": func(input *PlanInput) { input.AbortConditions[1] = input.AbortConditions[0] },
		"credential material shaped ref": func(input *PlanInput) {
			input.Credentials[0].SecretRef = "postgresql://user:secret@db.invalid/postgres"
		},
		"untyped secret locator": func(input *PlanInput) {
			input.Credentials[0].SecretRef = "opaque-reference"
		},
		"shared secret locator": func(input *PlanInput) {
			input.Credentials[1].SecretRef = input.Credentials[0].SecretRef
		},
		"provisioner authority collision": func(input *PlanInput) {
			input.Credentials[0].LoginRole = input.AuthorityManifest.DatabaseOwnerRole
		},
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			input := validPlanInput()
			mutate(&input)
			if _, err := BuildPlan(input); !errors.Is(err, ErrInvalidPlan) {
				t.Fatalf("BuildPlan error = %v, want %v", err, ErrInvalidPlan)
			}
		})
	}
}

func TestPlanGoldenDigest(t *testing.T) {
	plan, err := BuildPlan(validPlanInput())
	if err != nil {
		t.Fatalf("BuildPlan: %v", err)
	}
	const wantDigest = "sha256:7d833045167e66b8270513c25a1aac3a24ad7272b5d5423fa5131785bc82a564"
	if plan.Digest() != wantDigest {
		t.Fatalf("golden digest = %q, want %q", plan.Digest(), wantDigest)
	}
}

func validPlanInput() PlanInput {
	digestA := "sha256:" + strings.Repeat("a", 64)
	digestB := "sha256:" + strings.Repeat("b", 64)
	digestC := "sha256:" + strings.Repeat("c", 64)
	digestD := "sha256:" + strings.Repeat("d", 64)
	manifest := migrations.AuthorityAccessManifest{
		DatabaseName:        "wanwork_im",
		DatabaseOwnerRole:   "wanwork_im_provisioner",
		OwnerRole:           "wanwork_im_owner",
		MigratorRole:        "wanwork_im_migrator",
		RuntimeRole:         "wanwork_im_runtime",
		MigrationLoginRoles: []string{"wanwork_im_deploy_login_b", "wanwork_im_deploy_login_a"},
		RuntimeLoginRoles:   []string{"wanwork_im_app_login_b", "wanwork_im_app_login_a"},
	}
	return PlanInput{
		AbortConditions:   []string{"drift/authority", "drift/backup", "drift/tls"},
		ApprovalIdentity:  "release-owner/primary",
		ApprovalReference: "approval/postgres-cell-a/20260829-0001",
		AuthorityManifest: manifest,
		Backup: BackupPrerequisite{
			ArtifactReference: "backup/postgres-cell-a/20260829-0001",
			AttestationDigest: digestA,
			Required:          true,
		},
		CellID: "postgres-cell-a",
		Credentials: []CredentialGeneration{
			{Consumer: CredentialProvisioner, Generation: "generation-1", LoginRole: "postgres_platform_login", SecretRef: "secret/postgres-cell-a/provisioner/generation-1"},
			{Consumer: CredentialMigration, Generation: "generation-1", LoginRole: "wanwork_im_deploy_login_a", SecretRef: "secret/postgres-cell-a/migration/generation-1"},
			{Consumer: CredentialRuntime, Generation: "generation-1", LoginRole: "wanwork_im_app_login_a", SecretRef: "secret/postgres-cell-a/runtime/generation-1"},
		},
		DeploymentID:           "wanwork-im-prod-a",
		EvidenceDestination:    "evidence/postgres-cell-a/revision-1",
		ExpiresAt:              time.Date(2026, 8, 30, 0, 0, 0, 0, time.UTC),
		FromSchemaVersion:      0,
		NonEmptyClassification: ClassificationEmpty,
		PlanID:                 "plan-20260829-0001",
		PostgreSQLMajor:        migrations.AuthorityAccessPostgreSQLMajor,
		ReleaseArtifactDigest:  digestB,
		Rollback: RollbackBoundary{
			ArtifactDigest: digestC,
			BoundaryStepID: "cutover-ownership",
			Mode:           "restore_or_forward_fix",
		},
		ServerIdentity: "postgres-writer.prod.internal",
		SourceCommit:   strings.Repeat("1", 40),
		SourceTree:     strings.Repeat("2", 40),
		Steps: []Step{
			{ID: "preflight-authority", Action: "read-authority", Phase: PhasePreflight, RequiredExecutor: ExecutorProvisioner, TransactionClass: TransactionReadOnlyRepeatable, PreconditionDigest: digestA, PostconditionDigest: digestB, AbortConditionDigest: digestC},
			{ID: "bootstrap-authority", Action: "create-authority", Phase: PhaseBootstrap, RequiredExecutor: ExecutorProvisioner, TransactionClass: TransactionReconciledStep, PreconditionDigest: digestB, PostconditionDigest: digestC, AbortConditionDigest: digestD},
			{ID: "migrate-catalog", Action: "apply-catalog", Phase: PhaseMigrate, RequiredExecutor: ExecutorMigrationToOwner, TransactionClass: TransactionMigration, PreconditionDigest: digestC, PostconditionDigest: digestD, AbortConditionDigest: digestA},
			{ID: "cutover-ownership", Action: "converge-ownership", Phase: PhaseCutover, RequiredExecutor: ExecutorOwner, TransactionClass: TransactionTransactional, PreconditionDigest: digestD, PostconditionDigest: digestA, AbortConditionDigest: digestB},
			{ID: "runtime-attestation", Action: "attest-runtime", Phase: PhaseRuntimeProof, RequiredExecutor: ExecutorRuntimeToRuntime, TransactionClass: TransactionReadOnly, PreconditionDigest: digestA, PostconditionDigest: digestB, AbortConditionDigest: digestC},
		},
		TLS: TLSProfile{
			CADigest:   digestD,
			CARef:      "trust/postgres-root-ca/generation-1",
			Mode:       "verify-full",
			ServerName: "postgres-writer.prod.internal",
		},
		ToSchemaVersion: 5,
	}
}
