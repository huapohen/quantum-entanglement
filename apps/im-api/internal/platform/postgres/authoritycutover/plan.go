package authoritycutover

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/platform/postgres/migrations"
	"golang.org/x/text/unicode/norm"
)

const (
	PlanFormat                     = "wanwork.im.postgres-authority-cutover-plan/4"
	maximumPlanBytes               = 256 * 1024
	planDigestDomain               = "wanwork.im/postgres-authority-cutover-plan/4\n"
	postgresSystemIdentifierDomain = "wanwork.im/postgres-cluster-system-identifier/1\n"
	maximumPlanSetValues           = 128
)

var (
	ErrInvalidPlan  = errors.New("invalid PostgreSQL authority cutover plan")
	ErrPlanTooLarge = errors.New("PostgreSQL authority cutover plan exceeds size limit")

	canonicalDigest                     = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)
	canonicalGitID                      = regexp.MustCompile(`^[0-9a-f]{40}(?:[0-9a-f]{24})?$`)
	canonicalID                         = regexp.MustCompile(`^[a-z0-9][a-z0-9._:/-]{0,255}$`)
	canonicalPostgreSQLSystemIdentifier = regexp.MustCompile(`^[1-9][0-9]{0,19}$`)
)

type NonEmptyClassification string

const (
	ClassificationEmpty    NonEmptyClassification = "empty"
	ClassificationNonEmpty NonEmptyClassification = "non_empty"
)

type CutoverPhase string

const (
	PhasePreflight    CutoverPhase = "preflight"
	PhaseBootstrap    CutoverPhase = "bootstrap"
	PhaseMigrate      CutoverPhase = "migrate"
	PhaseCutover      CutoverPhase = "cutover"
	PhaseRuntimeProof CutoverPhase = "runtime_proof"
)

type TransactionClass string

const (
	TransactionReadOnlyRepeatable TransactionClass = "read_only_repeatable_read"
	TransactionReconciledStep     TransactionClass = "reconciled_step"
	TransactionMigration          TransactionClass = "migration_transaction"
	TransactionTransactional      TransactionClass = "transactional"
	TransactionReadOnly           TransactionClass = "read_only"
)

type ExecutorIdentity string

const (
	ExecutorProvisioner      ExecutorIdentity = "provisioner"
	ExecutorMigrationToOwner ExecutorIdentity = "migration_login_to_owner"
	ExecutorOwner            ExecutorIdentity = "owner"
	ExecutorRuntimeToRuntime ExecutorIdentity = "runtime_login_to_runtime"
)

type CredentialConsumer string

const (
	CredentialProvisioner CredentialConsumer = "provisioner"
	CredentialMigration   CredentialConsumer = "migration"
	CredentialRuntime     CredentialConsumer = "runtime"
)

// PlanInput contains semantic values only. BuildPlan resolves all derived digests from production
// migration code and never accepts a caller-supplied authority or migration digest.
type PlanInput struct {
	ApprovalIdentity       string
	ApprovalReference      string
	AuthorityManifest      migrations.AuthorityAccessManifest
	Backup                 BackupPrerequisite
	CellID                 string
	ClusterIdentity        VerifiedPostgreSQLClusterIdentity
	Credentials            []CredentialGeneration
	DeploymentID           string
	EvidenceDestination    string
	ExpiresAt              time.Time
	FromSchemaVersion      int64
	NonEmptyClassification NonEmptyClassification
	PlanID                 string
	PostgreSQLMajor        int
	ProvisionerGrantorRole string
	ReleaseArtifactDigest  string
	Rollback               RollbackBoundary
	ServerIdentity         string
	SourceCommit           string
	SourceTree             string
	TLS                    TLSProfile
	ToSchemaVersion        int64
}

// VerifiedPostgreSQLClusterIdentity is produced only by a trusted pre-approval cluster probe or
// authenticated deployment inventory loader in this package. Its fields are deliberately private:
// BuildPlan must not accept a caller-reported system identifier after approval.
type VerifiedPostgreSQLClusterIdentity struct {
	caDigest         string
	catalogVersionNo int
	database         string
	loginRole        string
	pgControlVersion int
	postgreSQLMajor  int
	primary          bool
	serverIdentity   string
	systemIdentifier string
}

type PlanSnapshot struct {
	AbortConditions     []string               `json:"abortConditions"`
	Approval            ApprovalBinding        `json:"approval"`
	Authority           AuthorityBinding       `json:"authority"`
	Backup              BackupPrerequisite     `json:"backup"`
	Credentials         []CredentialGeneration `json:"credentials"`
	EvidenceDestination string                 `json:"evidenceDestination"`
	ExpiresAt           time.Time              `json:"expiresAt"`
	Format              string                 `json:"format"`
	PlanDigest          string                 `json:"planDigest"`
	PlanID              string                 `json:"planId"`
	Rollback            RollbackBoundary       `json:"rollback"`
	SchemaTransition    SchemaTransition       `json:"schemaTransition"`
	Source              SourceBinding          `json:"source"`
	Steps               []Step                 `json:"steps"`
	Target              TargetBinding          `json:"target"`
}

type ApprovalBinding struct {
	ExactPlanDigest string `json:"exactPlanDigest"`
	Identity        string `json:"identity"`
	Reference       string `json:"reference"`
}

type AuthorityBinding struct {
	CutoverSpecificationDigest    string            `json:"cutoverSpecificationDigest"`
	CutoverTopology               string            `json:"cutoverTopology"`
	ExecutorCompatibilityVersion  string            `json:"executorCompatibilityVersion"`
	Manifest                      AuthorityManifest `json:"manifest"`
	ManifestDigest                string            `json:"manifestDigest"`
	ProvisionerGrantorRole        string            `json:"provisionerGrantorRole"`
	SpecificationDigest           string            `json:"specificationDigest"`
	ValidatorCompatibilityVersion string            `json:"validatorCompatibilityVersion"`
}

type AuthorityManifest struct {
	DatabaseName        string   `json:"databaseName"`
	DatabaseOwnerRole   string   `json:"databaseOwnerRole"`
	MigrationLoginRoles []string `json:"migrationLoginRoles"`
	MigratorRole        string   `json:"migratorRole"`
	OwnerRole           string   `json:"ownerRole"`
	RuntimeLoginRoles   []string `json:"runtimeLoginRoles"`
	RuntimeRole         string   `json:"runtimeRole"`
}

type BackupPrerequisite struct {
	ArtifactReference string `json:"artifactReference"`
	AttestationDigest string `json:"attestationDigest"`
	Required          bool   `json:"required"`
}

type CredentialGeneration struct {
	Consumer   CredentialConsumer `json:"consumer"`
	Generation string             `json:"generation"`
	LoginRole  string             `json:"loginRole"`
	SecretRef  string             `json:"secretRef"`
}

type RollbackBoundary struct {
	ArtifactDigest string `json:"artifactDigest"`
	BoundaryStepID string `json:"boundaryStepId"`
	Mode           string `json:"mode"`
}

type SchemaTransition struct {
	From                   int64                  `json:"from"`
	NonEmptyClassification NonEmptyClassification `json:"nonEmptyClassification"`
	To                     int64                  `json:"to"`
}

type SourceBinding struct {
	Commit                 string `json:"commit"`
	MigrationCatalogDigest string `json:"migrationCatalogDigest"`
	ReleaseArtifactDigest  string `json:"releaseArtifactDigest"`
	Tree                   string `json:"tree"`
}

type Step struct {
	AbortConditionDigest string           `json:"abortConditionDigest"`
	Action               string           `json:"action"`
	ID                   string           `json:"id"`
	Phase                CutoverPhase     `json:"phase"`
	PostconditionDigest  string           `json:"postconditionDigest"`
	PreconditionDigest   string           `json:"preconditionDigest"`
	RequiredExecutor     ExecutorIdentity `json:"requiredExecutor"`
	TransactionClass     TransactionClass `json:"transactionClass"`
}

type TargetBinding struct {
	CatalogVersionNo       int        `json:"catalogVersionNo"`
	CellID                 string     `json:"cellId"`
	Database               string     `json:"database"`
	DeploymentID           string     `json:"deploymentId"`
	PGControlVersion       int        `json:"pgControlVersion"`
	PostgreSQLMajor        int        `json:"postgresqlMajor"`
	PrimaryRequired        bool       `json:"primaryRequired"`
	ServerIdentity         string     `json:"serverIdentity"`
	SystemIdentifierDigest string     `json:"systemIdentifierDigest"`
	TLS                    TLSProfile `json:"tls"`
}

type TLSProfile struct {
	// CADigest is the SHA-256 digest of the raw DER root certificate in the
	// negotiated verified chain. It is not a CA bundle file digest.
	CADigest   string `json:"caDigest"`
	CARef      string `json:"caRef"`
	Mode       string `json:"mode"`
	ServerName string `json:"serverName"`
}

// Plan is immutable: accessors return detached slices and byte arrays.
type Plan struct {
	snapshot  PlanSnapshot
	canonical []byte
	digest    string
}

func BuildPlan(input PlanInput) (Plan, error) {
	if !validVerifiedPostgreSQLClusterIdentity(input.ClusterIdentity, input) {
		return Plan{}, ErrInvalidPlan
	}
	specification, err := migrations.CurrentAuthorityAccessSpecification(input.AuthorityManifest)
	if err != nil {
		return Plan{}, ErrInvalidPlan
	}
	specificationDigest, err := migrations.DigestAuthorityAccessSpecification(specification)
	if err != nil {
		return Plan{}, ErrInvalidPlan
	}
	provisionerLogin, uniqueProvisioner := provisionerLoginRole(input.Credentials)
	if !uniqueProvisioner {
		return Plan{}, ErrInvalidPlan
	}
	cutoverSpecification, err := migrations.CurrentAuthorityCutoverSpecification(
		input.AuthorityManifest,
		provisionerLogin,
		input.ProvisionerGrantorRole,
	)
	if err != nil || cutoverSpecification.ManagedAuthoritySpecificationDigest != specificationDigest {
		return Plan{}, ErrInvalidPlan
	}
	cutoverSpecificationDigest, err := migrations.DigestAuthorityCutoverSpecification(cutoverSpecification)
	if err != nil {
		return Plan{}, ErrInvalidPlan
	}
	snapshot := PlanSnapshot{
		Approval: ApprovalBinding{
			Identity:  input.ApprovalIdentity,
			Reference: input.ApprovalReference,
		},
		Authority: AuthorityBinding{
			CutoverSpecificationDigest:    cutoverSpecificationDigest,
			CutoverTopology:               cutoverSpecification.Topology,
			ExecutorCompatibilityVersion:  specification.ExecutorCompatibilityVersion,
			Manifest:                      authorityManifestSnapshot(input.AuthorityManifest),
			ManifestDigest:                specification.AuthorityManifestDigest,
			ProvisionerGrantorRole:        input.ProvisionerGrantorRole,
			SpecificationDigest:           specificationDigest,
			ValidatorCompatibilityVersion: specification.ValidatorCompatibilityVersion,
		},
		Backup:              input.Backup,
		Credentials:         slices.Clone(input.Credentials),
		EvidenceDestination: input.EvidenceDestination,
		ExpiresAt:           input.ExpiresAt,
		Format:              PlanFormat,
		PlanID:              input.PlanID,
		Rollback:            input.Rollback,
		SchemaTransition: SchemaTransition{
			From:                   input.FromSchemaVersion,
			NonEmptyClassification: input.NonEmptyClassification,
			To:                     input.ToSchemaVersion,
		},
		Source: SourceBinding{
			Commit:                 input.SourceCommit,
			MigrationCatalogDigest: specification.MigrationCatalogDigest,
			ReleaseArtifactDigest:  input.ReleaseArtifactDigest,
			Tree:                   input.SourceTree,
		},
		Target: TargetBinding{
			CatalogVersionNo:       input.ClusterIdentity.catalogVersionNo,
			CellID:                 input.CellID,
			Database:               input.AuthorityManifest.DatabaseName,
			DeploymentID:           input.DeploymentID,
			PGControlVersion:       input.ClusterIdentity.pgControlVersion,
			PostgreSQLMajor:        input.PostgreSQLMajor,
			PrimaryRequired:        true,
			ServerIdentity:         input.ServerIdentity,
			SystemIdentifierDigest: digestPostgreSQLSystemIdentifier(input.ClusterIdentity.systemIdentifier),
			TLS:                    input.TLS,
		},
	}
	if err := setDerivedWorkflow(&snapshot); err != nil {
		return Plan{}, ErrInvalidPlan
	}
	normalizePlan(&snapshot)
	if !validPlanSnapshot(snapshot, false) {
		return Plan{}, ErrInvalidPlan
	}
	canonicalWithoutDigest, err := marshalCanonical(snapshot)
	if err != nil {
		return Plan{}, ErrInvalidPlan
	}
	digest := digestPlan(canonicalWithoutDigest)
	snapshot.PlanDigest = digest
	snapshot.Approval.ExactPlanDigest = digest
	return sealPlan(snapshot)
}

func (plan Plan) CanonicalBytes() []byte { return slices.Clone(plan.canonical) }

func (plan Plan) Digest() string { return plan.digest }

func (plan Plan) Snapshot() PlanSnapshot { return clonePlanSnapshot(plan.snapshot) }

func sealPlan(snapshot PlanSnapshot) (Plan, error) {
	if !validPlanSnapshot(snapshot, true) {
		return Plan{}, ErrInvalidPlan
	}
	wantDigest := snapshot.PlanDigest
	unsigned := clonePlanSnapshot(snapshot)
	unsigned.PlanDigest = ""
	unsigned.Approval.ExactPlanDigest = ""
	canonicalWithoutDigest, err := marshalCanonical(unsigned)
	if err != nil || digestPlan(canonicalWithoutDigest) != wantDigest {
		return Plan{}, ErrInvalidPlan
	}
	canonical, err := marshalCanonical(snapshot)
	if err != nil || len(canonical) > maximumPlanBytes {
		if len(canonical) > maximumPlanBytes {
			return Plan{}, ErrPlanTooLarge
		}
		return Plan{}, ErrInvalidPlan
	}
	return Plan{
		snapshot:  clonePlanSnapshot(snapshot),
		canonical: slices.Clone(canonical),
		digest:    wantDigest,
	}, nil
}

func normalizePlan(snapshot *PlanSnapshot) {
	slices.Sort(snapshot.AbortConditions)
	slices.SortFunc(snapshot.Credentials, func(left, right CredentialGeneration) int {
		for _, pair := range [][2]string{
			{string(left.Consumer), string(right.Consumer)},
			{left.Generation, right.Generation},
			{left.LoginRole, right.LoginRole},
			{left.SecretRef, right.SecretRef},
		} {
			if result := strings.Compare(pair[0], pair[1]); result != 0 {
				return result
			}
		}
		return 0
	})
	slices.Sort(snapshot.Authority.Manifest.MigrationLoginRoles)
	slices.Sort(snapshot.Authority.Manifest.RuntimeLoginRoles)
}

func validPlanSnapshot(snapshot PlanSnapshot, requireDigest bool) bool {
	if snapshot.Format != PlanFormat || !canonicalIdentity(snapshot.PlanID) ||
		!canonicalGitID.MatchString(snapshot.Source.Commit) ||
		!canonicalGitID.MatchString(snapshot.Source.Tree) ||
		!canonicalDigest.MatchString(snapshot.Source.ReleaseArtifactDigest) ||
		!canonicalDigest.MatchString(snapshot.Source.MigrationCatalogDigest) ||
		!canonicalDigest.MatchString(snapshot.Authority.ManifestDigest) ||
		!canonicalDigest.MatchString(snapshot.Authority.SpecificationDigest) ||
		!canonicalDigest.MatchString(snapshot.Authority.CutoverSpecificationDigest) ||
		!validAuthorityBinding(snapshot) ||
		snapshot.Target.Database != snapshot.Authority.Manifest.DatabaseName ||
		snapshot.Target.PostgreSQLMajor != migrations.AuthorityAccessPostgreSQLMajor ||
		snapshot.Target.CatalogVersionNo <= 0 || snapshot.Target.PGControlVersion <= 0 ||
		!snapshot.Target.PrimaryRequired || !canonicalDigest.MatchString(snapshot.Target.SystemIdentifierDigest) ||
		!canonicalIdentity(snapshot.Target.DeploymentID) || !canonicalIdentity(snapshot.Target.CellID) ||
		!canonicalIdentity(snapshot.Target.ServerIdentity) || !validTLS(snapshot.Target.TLS) ||
		snapshot.Target.TLS.ServerName != snapshot.Target.ServerIdentity ||
		!validCredentials(snapshot.Credentials, snapshot.Authority.Manifest) ||
		!validSchemaTransition(snapshot.SchemaTransition, currentSchemaVersion()) ||
		!validDerivedWorkflow(snapshot) ||
		!validBackup(snapshot.Backup) || !validRollback(snapshot.Rollback, snapshot.Steps) ||
		!canonicalIdentity(snapshot.EvidenceDestination) ||
		!strings.HasPrefix(snapshot.EvidenceDestination, "evidence/") ||
		!canonicalIdentity(snapshot.Approval.Identity) || !validApprovalReference(snapshot.Approval.Reference) ||
		snapshot.ExpiresAt.IsZero() || snapshot.ExpiresAt.Location() != time.UTC ||
		snapshot.ExpiresAt.Nanosecond() != 0 {
		return false
	}
	if requireDigest {
		return canonicalDigest.MatchString(snapshot.PlanDigest) &&
			snapshot.Approval.ExactPlanDigest == snapshot.PlanDigest
	}
	return snapshot.PlanDigest == "" && snapshot.Approval.ExactPlanDigest == ""
}

func validVerifiedPostgreSQLClusterIdentity(
	identity VerifiedPostgreSQLClusterIdentity,
	input PlanInput,
) bool {
	provisionerLogin, uniqueProvisioner := provisionerLoginRole(input.Credentials)
	return uniqueProvisioner && validVerifiedPostgreSQLClusterIdentityForScope(
		identity,
		input.AuthorityManifest.DatabaseName,
		provisionerLogin,
		input.PostgreSQLMajor,
		input.ServerIdentity,
		input.TLS.CADigest,
	)
}

func validVerifiedPostgreSQLClusterIdentityForScope(
	identity VerifiedPostgreSQLClusterIdentity,
	database string,
	loginRole string,
	postgreSQLMajor int,
	serverIdentity string,
	caDigest string,
) bool {
	if identity.catalogVersionNo <= 0 || identity.pgControlVersion <= 0 || !identity.primary ||
		identity.postgreSQLMajor != postgreSQLMajor || identity.database != database ||
		identity.loginRole != loginRole || identity.serverIdentity != serverIdentity ||
		identity.caDigest != caDigest ||
		!canonicalPostgreSQLSystemIdentifier.MatchString(identity.systemIdentifier) {
		return false
	}
	_, err := strconv.ParseUint(identity.systemIdentifier, 10, 64)
	return err == nil
}

func provisionerLoginRole(credentials []CredentialGeneration) (string, bool) {
	value := ""
	for _, credential := range credentials {
		if credential.Consumer != CredentialProvisioner {
			continue
		}
		if value != "" {
			return "", false
		}
		value = credential.LoginRole
	}
	return value, canonicalIdentity(value)
}

func digestPostgreSQLSystemIdentifier(systemIdentifier string) string {
	hash := sha256.New()
	_, _ = hash.Write([]byte(postgresSystemIdentifierDomain))
	_, _ = hash.Write([]byte(systemIdentifier))
	return "sha256:" + hex.EncodeToString(hash.Sum(nil))
}

func validAuthorityManifest(manifest AuthorityManifest) bool {
	value := migrationsAuthorityManifest(manifest)
	return value.Validate() == nil && slices.IsSorted(manifest.MigrationLoginRoles) &&
		slices.IsSorted(manifest.RuntimeLoginRoles)
}

func validAuthorityBinding(snapshot PlanSnapshot) bool {
	if snapshot.Authority.ExecutorCompatibilityVersion != migrations.AuthorityAccessExecutorCompatibility ||
		snapshot.Authority.ValidatorCompatibilityVersion != migrations.AuthorityAccessValidatorCompatibility ||
		!validAuthorityManifest(snapshot.Authority.Manifest) {
		return false
	}
	manifest := migrationsAuthorityManifest(snapshot.Authority.Manifest)
	specification, err := migrations.CurrentAuthorityAccessSpecification(manifest)
	if err != nil || specification.MigrationCatalogDigest != snapshot.Source.MigrationCatalogDigest ||
		specification.AuthorityManifestDigest != snapshot.Authority.ManifestDigest {
		return false
	}
	digest, err := migrations.DigestAuthorityAccessSpecification(specification)
	if err != nil || digest != snapshot.Authority.SpecificationDigest {
		return false
	}
	provisionerLogin, uniqueProvisioner := provisionerLoginRole(snapshot.Credentials)
	if !uniqueProvisioner {
		return false
	}
	cutover, err := migrations.CurrentAuthorityCutoverSpecification(
		manifest,
		provisionerLogin,
		snapshot.Authority.ProvisionerGrantorRole,
	)
	if err != nil || cutover.Topology != snapshot.Authority.CutoverTopology ||
		cutover.ManagedAuthoritySpecificationDigest != digest {
		return false
	}
	cutoverDigest, err := migrations.DigestAuthorityCutoverSpecification(cutover)
	return err == nil && cutoverDigest == snapshot.Authority.CutoverSpecificationDigest
}

func migrationsAuthorityManifest(manifest AuthorityManifest) migrations.AuthorityAccessManifest {
	return migrations.AuthorityAccessManifest{
		DatabaseName:        manifest.DatabaseName,
		DatabaseOwnerRole:   manifest.DatabaseOwnerRole,
		OwnerRole:           manifest.OwnerRole,
		MigratorRole:        manifest.MigratorRole,
		RuntimeRole:         manifest.RuntimeRole,
		MigrationLoginRoles: slices.Clone(manifest.MigrationLoginRoles),
		RuntimeLoginRoles:   slices.Clone(manifest.RuntimeLoginRoles),
	}
}

func validTLS(profile TLSProfile) bool {
	return profile.Mode == "verify-full" && canonicalDigest.MatchString(profile.CADigest) &&
		canonicalIdentity(profile.CARef) && strings.HasPrefix(profile.CARef, "trust/") &&
		canonicalIdentity(profile.ServerName)
}

func validCredentials(values []CredentialGeneration, manifest AuthorityManifest) bool {
	if len(values) != 3 || !slices.IsSortedFunc(values, func(left, right CredentialGeneration) int {
		return strings.Compare(string(left.Consumer), string(right.Consumer))
	}) {
		return false
	}
	want := map[CredentialConsumer]bool{
		CredentialProvisioner: false,
		CredentialMigration:   false,
		CredentialRuntime:     false,
	}
	seenLoginRoles := make(map[string]struct{}, len(values))
	seenSecretRefs := make(map[string]struct{}, len(values))
	for _, value := range values {
		if _, exists := want[value.Consumer]; !exists || want[value.Consumer] ||
			!canonicalIdentity(value.Generation) || !canonicalIdentity(value.LoginRole) ||
			!canonicalIdentity(value.SecretRef) || !strings.HasPrefix(value.Generation, "generation-") ||
			!strings.HasPrefix(value.SecretRef, "secret/") {
			return false
		}
		if _, duplicate := seenLoginRoles[value.LoginRole]; duplicate {
			return false
		}
		if _, duplicate := seenSecretRefs[value.SecretRef]; duplicate {
			return false
		}
		seenLoginRoles[value.LoginRole] = struct{}{}
		seenSecretRefs[value.SecretRef] = struct{}{}
		switch value.Consumer {
		case CredentialProvisioner:
			if value.LoginRole == manifest.DatabaseOwnerRole || value.LoginRole == manifest.OwnerRole ||
				value.LoginRole == manifest.MigratorRole || value.LoginRole == manifest.RuntimeRole ||
				slices.Contains(manifest.MigrationLoginRoles, value.LoginRole) ||
				slices.Contains(manifest.RuntimeLoginRoles, value.LoginRole) {
				return false
			}
		case CredentialMigration:
			if !slices.Contains(manifest.MigrationLoginRoles, value.LoginRole) {
				return false
			}
		case CredentialRuntime:
			if !slices.Contains(manifest.RuntimeLoginRoles, value.LoginRole) {
				return false
			}
		}
		want[value.Consumer] = true
	}
	return want[CredentialProvisioner] && want[CredentialMigration] && want[CredentialRuntime]
}

func validSchemaTransition(value SchemaTransition, currentVersion int64) bool {
	return currentVersion > 0 && value.From >= 0 && value.To == currentVersion && value.To >= value.From &&
		(value.NonEmptyClassification == ClassificationEmpty ||
			value.NonEmptyClassification == ClassificationNonEmpty)
}

func currentSchemaVersion() int64 {
	catalog, err := migrations.Catalog()
	if err != nil || len(catalog) == 0 {
		return 0
	}
	return catalog[len(catalog)-1].Version
}

func validBackup(value BackupPrerequisite) bool {
	return value.Required && canonicalIdentity(value.ArtifactReference) &&
		strings.HasPrefix(value.ArtifactReference, "backup/") &&
		canonicalDigest.MatchString(value.AttestationDigest)
}

func validRollback(value RollbackBoundary, steps []Step) bool {
	if !canonicalDigest.MatchString(value.ArtifactDigest) || !canonicalIdentity(value.BoundaryStepID) ||
		value.Mode != "restore_or_forward_fix" {
		return false
	}
	for _, step := range steps {
		if step.ID == value.BoundaryStepID {
			return true
		}
	}
	return false
}

func validCanonicalSet(values []string) bool {
	if len(values) == 0 || len(values) > maximumPlanSetValues || !slices.IsSorted(values) {
		return false
	}
	for index, value := range values {
		if !canonicalIdentity(value) || (index > 0 && values[index-1] == value) {
			return false
		}
	}
	return true
}

func canonicalIdentity(value string) bool {
	return canonicalID.MatchString(value) && utf8.ValidString(value) && norm.NFC.IsNormalString(value)
}

func authorityManifestSnapshot(manifest migrations.AuthorityAccessManifest) AuthorityManifest {
	return AuthorityManifest{
		DatabaseName:        manifest.DatabaseName,
		DatabaseOwnerRole:   manifest.DatabaseOwnerRole,
		MigrationLoginRoles: slices.Clone(manifest.MigrationLoginRoles),
		MigratorRole:        manifest.MigratorRole,
		OwnerRole:           manifest.OwnerRole,
		RuntimeLoginRoles:   slices.Clone(manifest.RuntimeLoginRoles),
		RuntimeRole:         manifest.RuntimeRole,
	}
}

func clonePlanSnapshot(snapshot PlanSnapshot) PlanSnapshot {
	cloned := snapshot
	cloned.AbortConditions = slices.Clone(snapshot.AbortConditions)
	cloned.Credentials = slices.Clone(snapshot.Credentials)
	cloned.Steps = slices.Clone(snapshot.Steps)
	cloned.Authority.Manifest.MigrationLoginRoles = slices.Clone(snapshot.Authority.Manifest.MigrationLoginRoles)
	cloned.Authority.Manifest.RuntimeLoginRoles = slices.Clone(snapshot.Authority.Manifest.RuntimeLoginRoles)
	return cloned
}

func marshalCanonical(value PlanSnapshot) ([]byte, error) {
	var output bytes.Buffer
	encoder := json.NewEncoder(&output)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(value); err != nil {
		return nil, err
	}
	return bytes.TrimSuffix(output.Bytes(), []byte("\n")), nil
}

func digestPlan(canonical []byte) string {
	hash := sha256.New()
	_, _ = hash.Write([]byte(planDigestDomain))
	_, _ = hash.Write(canonical)
	return "sha256:" + hex.EncodeToString(hash.Sum(nil))
}
