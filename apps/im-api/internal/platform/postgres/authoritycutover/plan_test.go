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
		snapshot.Authority.SpecificationDigest == "" ||
		snapshot.Authority.CutoverSpecificationDigest == "" ||
		snapshot.Authority.CutoverTopology != migrations.AuthorityCutoverTopology ||
		snapshot.Authority.ProvisionerGrantorRole != input.ProvisionerGrantorRole ||
		!canonicalDigest.MatchString(snapshot.Target.SystemIdentifierDigest) ||
		snapshot.Target.CatalogVersionNo <= 0 || snapshot.Target.PGControlVersion <= 0 ||
		!snapshot.Target.PrimaryRequired {
		t.Fatalf("derived binding is incomplete: %#v", snapshot)
	}

	reordered := validPlanInput()
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
	changed.ClusterIdentity.systemIdentifier = "7678902413432981334"
	third, err := BuildPlan(changed)
	if err != nil {
		t.Fatalf("BuildPlan changed: %v", err)
	}
	if third.Digest() == first.Digest() {
		t.Fatal("semantic change did not change plan digest")
	}

	lower := strings.ToLower(string(first.CanonicalBytes()))
	if strings.Contains(lower, validPlanInput().ClusterIdentity.systemIdentifier) {
		t.Fatal("plan exposed the raw PostgreSQL system identifier")
	}
	for _, forbidden := range []string{"password", "privatekey", "connectionstring", "dsn", "token"} {
		if strings.Contains(lower, forbidden) {
			t.Fatalf("plan contains credential-bearing field %q", forbidden)
		}
	}
}

func TestBuildPlanRejectsIncompleteOrUnsafeSemantics(t *testing.T) {
	tests := map[string]func(*PlanInput){
		"wrong postgres major":       func(input *PlanInput) { input.PostgreSQLMajor = 17 },
		"cluster major mismatch":     func(input *PlanInput) { input.ClusterIdentity.postgreSQLMajor = 17 },
		"cluster replica":            func(input *PlanInput) { input.ClusterIdentity.primary = false },
		"cluster database mismatch":  func(input *PlanInput) { input.ClusterIdentity.database = "wanwork_im_other" },
		"cluster login mismatch":     func(input *PlanInput) { input.ClusterIdentity.loginRole = "postgres_platform_login_other" },
		"cluster server mismatch":    func(input *PlanInput) { input.ClusterIdentity.serverIdentity = "postgres-reader.prod.internal" },
		"cluster ca mismatch":        func(input *PlanInput) { input.ClusterIdentity.caDigest = "sha256:" + strings.Repeat("e", 64) },
		"missing pg control version": func(input *PlanInput) { input.ClusterIdentity.pgControlVersion = 0 },
		"missing catalog version":    func(input *PlanInput) { input.ClusterIdentity.catalogVersionNo = 0 },
		"leading-zero system identifier": func(input *PlanInput) {
			input.ClusterIdentity.systemIdentifier = "07678902413432981333"
		},
		"overflow system identifier": func(input *PlanInput) {
			input.ClusterIdentity.systemIdentifier = "18446744073709551616"
		},
		"transport without hostname verification": func(input *PlanInput) { input.TLS.Mode = "require" },
		"tls identity mismatch": func(input *PlanInput) {
			input.TLS.ServerName = "postgres-reader.prod.internal"
		},
		"missing backup":          func(input *PlanInput) { input.Backup.Required = false },
		"implicit classification": func(input *PlanInput) { input.NonEmptyClassification = "" },
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
		"grantor authority collision": func(input *PlanInput) {
			input.ProvisionerGrantorRole = input.AuthorityManifest.OwnerRole
		},
		"grantor provisioner collision": func(input *PlanInput) {
			input.ProvisionerGrantorRole = input.Credentials[0].LoginRole
		},
		"missing exact grantor": func(input *PlanInput) { input.ProvisionerGrantorRole = "" },
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
	const wantDigest = "sha256:af5380c224227f5214cafe068fbe3365e363164d26b3d2bfd51e6481f5812dc2"
	if plan.Digest() != wantDigest {
		t.Fatalf("golden digest = %q, want %q", plan.Digest(), wantDigest)
	}
}

func TestPostgreSQLSystemIdentifierDigestGolden(t *testing.T) {
	const want = "sha256:261329632cfb14ee84c64d49c4a0235f290f7ba66419357a6d1471ffb50b1ebf"
	if got := digestPostgreSQLSystemIdentifier("7678902413432981333"); got != want {
		t.Fatalf("system identifier digest = %q, want %q", got, want)
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
		ApprovalIdentity:  "release-owner/primary",
		ApprovalReference: "approval/postgres-cell-a/20260829-0001",
		AuthorityManifest: manifest,
		Backup: BackupPrerequisite{
			ArtifactReference: "backup/postgres-cell-a/20260829-0001",
			AttestationDigest: digestA,
			Required:          true,
		},
		CellID: "postgres-cell-a",
		ClusterIdentity: VerifiedPostgreSQLClusterIdentity{
			caDigest:         digestD,
			catalogVersionNo: 202509102,
			database:         "wanwork_im",
			loginRole:        "postgres_platform_login",
			pgControlVersion: 1800,
			postgreSQLMajor:  migrations.AuthorityAccessPostgreSQLMajor,
			primary:          true,
			serverIdentity:   "postgres-writer.prod.internal",
			systemIdentifier: "7678902413432981333",
		},
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
		ProvisionerGrantorRole: "postgres_iac_bootstrap",
		ReleaseArtifactDigest:  digestB,
		Rollback: RollbackBoundary{
			ArtifactDigest: digestC,
			BoundaryStepID: "cutover-ownership",
			Mode:           "restore_or_forward_fix",
		},
		ServerIdentity: "postgres-writer.prod.internal",
		SourceCommit:   strings.Repeat("1", 40),
		SourceTree:     strings.Repeat("2", 40),
		TLS: TLSProfile{
			CADigest:   digestD,
			CARef:      "trust/postgres-root-ca/generation-1",
			Mode:       "verify-full",
			ServerName: "postgres-writer.prod.internal",
		},
		ToSchemaVersion: 9,
	}
}
