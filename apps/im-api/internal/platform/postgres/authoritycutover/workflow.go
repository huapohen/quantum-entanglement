package authoritycutover

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"slices"
)

const (
	preflightExpectationFormat = "wanwork.im.postgres-preflight-expectation/1"
	preflightPassPolicyFormat  = "wanwork.im.postgres-preflight-pass-policy/1"
	preflightAbortPolicyFormat = "wanwork.im.postgres-preflight-abort-policy/1"
	workflowConditionFormat    = "wanwork.im.postgres-cutover-workflow-condition/1"

	preflightExpectationDigestDomain = "wanwork.im/postgres-preflight-expectation/1\n"
	preflightPassPolicyDigestDomain  = "wanwork.im/postgres-preflight-pass-policy/1\n"
	preflightAbortPolicyDigestDomain = "wanwork.im/postgres-preflight-abort-policy/1\n"
	workflowConditionDigestDomain    = "wanwork.im/postgres-cutover-workflow-condition/1\n"
)

const preflightMaximumObservationAgeSeconds = 60

var preflightCheckRegistry = [...]string{
	"authority/database-owner-attributes",
	"authority/provisioner-attributes",
	"authority/provisioner-connect",
	"authority/provisioner-membership",
	"backup/attestation",
	"cluster/identity",
	"database/existence",
	"database/non-empty-classification",
	"source/release-artifact",
	"tls/transport",
}

// PreflightExpectation is a typed, immutable-by-digest view of everything the read-only probe
// must observe. It contains public identities and digests only, never connection credentials.
type PreflightExpectation struct {
	Format                       string                 `json:"format"`
	AuthoritySpecificationDigest string                 `json:"authoritySpecificationDigest"`
	BackupAttestationDigest      string                 `json:"backupAttestationDigest"`
	CatalogVersionNo             int                    `json:"catalogVersionNo"`
	Checks                       []string               `json:"checks"`
	CutoverSpecificationDigest   string                 `json:"cutoverSpecificationDigest"`
	Database                     string                 `json:"database"`
	DatabaseClassification       NonEmptyClassification `json:"databaseClassification"`
	MigrationCatalogDigest       string                 `json:"migrationCatalogDigest"`
	MutationAuthorized           bool                   `json:"mutationAuthorized"`
	PGControlVersion             int                    `json:"pgControlVersion"`
	PostgreSQLMajor              int                    `json:"postgresqlMajor"`
	PrimaryRequired              bool                   `json:"primaryRequired"`
	ReleaseArtifactDigest        string                 `json:"releaseArtifactDigest"`
	SystemIdentifierDigest       string                 `json:"systemIdentifierDigest"`
	TLSCADigest                  string                 `json:"tlsCaDigest"`
	TLSMode                      string                 `json:"tlsMode"`
	Topology                     string                 `json:"topology"`
}

// PreflightPassPolicy requires every known check to pass and treats unknown as blocking. A report
// is deliberately short-lived and never authorizes mutation by itself.
type PreflightPassPolicy struct {
	AllChecksRequired            bool     `json:"allChecksRequired"`
	Checks                       []string `json:"checks"`
	Format                       string   `json:"format"`
	MaximumObservationAgeSeconds int      `json:"maximumObservationAgeSeconds"`
	MutationAuthorized           bool     `json:"mutationAuthorized"`
	UnknownBlocks                bool     `json:"unknownBlocks"`
}

// PreflightAbortPolicy is the code-owned fail-closed outcome policy. Both a positive block and an
// inability to establish a check abort the workflow.
type PreflightAbortPolicy struct {
	AbortOutcomes      []string `json:"abortOutcomes"`
	Checks             []string `json:"checks"`
	Format             string   `json:"format"`
	MutationAuthorized bool     `json:"mutationAuthorized"`
}

type workflowCondition struct {
	Condition string `json:"condition"`
	Format    string `json:"format"`
	StepID    string `json:"stepId"`
}

func setDerivedWorkflow(snapshot *PlanSnapshot) error {
	abortConditions, steps, err := deriveWorkflow(*snapshot)
	if err != nil {
		return err
	}
	snapshot.AbortConditions = abortConditions
	snapshot.Steps = steps
	return nil
}

func validDerivedWorkflow(snapshot PlanSnapshot) bool {
	abortConditions, steps, err := deriveWorkflow(snapshot)
	return err == nil && slices.Equal(snapshot.AbortConditions, abortConditions) &&
		slices.Equal(snapshot.Steps, steps)
}

func deriveWorkflow(snapshot PlanSnapshot) ([]string, []Step, error) {
	expectation, passPolicy, abortPolicy := derivePreflightPolicies(snapshot)
	expectationDigest, err := digestWorkflowValue(preflightExpectationDigestDomain, expectation)
	if err != nil {
		return nil, nil, err
	}
	passDigest, err := digestWorkflowValue(preflightPassPolicyDigestDomain, passPolicy)
	if err != nil {
		return nil, nil, err
	}
	abortDigest, err := digestWorkflowValue(preflightAbortPolicyDigestDomain, abortPolicy)
	if err != nil {
		return nil, nil, err
	}
	condition := func(stepID string, name string) (string, error) {
		return digestWorkflowValue(workflowConditionDigestDomain, workflowCondition{
			Condition: name,
			Format:    workflowConditionFormat,
			StepID:    stepID,
		})
	}
	steps := []Step{{
		ID:                   "preflight-authority",
		Action:               "read-authority",
		Phase:                PhasePreflight,
		RequiredExecutor:     ExecutorProvisioner,
		TransactionClass:     TransactionReadOnlyRepeatable,
		PreconditionDigest:   expectationDigest,
		PostconditionDigest:  passDigest,
		AbortConditionDigest: abortDigest,
	}}
	definitions := []struct {
		id             string
		action         string
		phase          CutoverPhase
		executor       ExecutorIdentity
		transaction    TransactionClass
		precondition   string
		postcondition  string
		abortCondition string
	}{
		{"bootstrap-authority", "create-authority", PhaseBootstrap, ExecutorProvisioner, TransactionReconciledStep, "preflight-passed", "cutover-authority-exact", "authority-drift"},
		{"migrate-catalog", "apply-catalog", PhaseMigrate, ExecutorMigrationToOwner, TransactionMigration, "cutover-authority-exact", "migration-catalog-exact", "migration-failed"},
		{"cutover-ownership", "converge-ownership", PhaseCutover, ExecutorOwner, TransactionTransactional, "migration-catalog-exact", "managed-authority-exact", "ownership-drift"},
		{"runtime-attestation", "attest-runtime", PhaseRuntimeProof, ExecutorRuntimeToRuntime, TransactionReadOnly, "managed-authority-exact", "runtime-authority-attested", "runtime-attestation-failed"},
	}
	for _, definition := range definitions {
		preconditionDigest, err := condition(definition.id, "precondition/"+definition.precondition)
		if err != nil {
			return nil, nil, err
		}
		postconditionDigest, err := condition(definition.id, "postcondition/"+definition.postcondition)
		if err != nil {
			return nil, nil, err
		}
		abortConditionDigest, err := condition(definition.id, "abort/"+definition.abortCondition)
		if err != nil {
			return nil, nil, err
		}
		steps = append(steps, Step{
			ID:                   definition.id,
			Action:               definition.action,
			Phase:                definition.phase,
			RequiredExecutor:     definition.executor,
			TransactionClass:     definition.transaction,
			PreconditionDigest:   preconditionDigest,
			PostconditionDigest:  postconditionDigest,
			AbortConditionDigest: abortConditionDigest,
		})
	}
	return slices.Clone(preflightCheckRegistry[:]), steps, nil
}

func derivePreflightPolicies(
	snapshot PlanSnapshot,
) (PreflightExpectation, PreflightPassPolicy, PreflightAbortPolicy) {
	expectation := PreflightExpectation{
		Format:                       preflightExpectationFormat,
		AuthoritySpecificationDigest: snapshot.Authority.SpecificationDigest,
		BackupAttestationDigest:      snapshot.Backup.AttestationDigest,
		CatalogVersionNo:             snapshot.Target.CatalogVersionNo,
		Checks:                       slices.Clone(preflightCheckRegistry[:]),
		CutoverSpecificationDigest:   snapshot.Authority.CutoverSpecificationDigest,
		Database:                     snapshot.Target.Database,
		DatabaseClassification:       snapshot.SchemaTransition.NonEmptyClassification,
		MigrationCatalogDigest:       snapshot.Source.MigrationCatalogDigest,
		MutationAuthorized:           false,
		PGControlVersion:             snapshot.Target.PGControlVersion,
		PostgreSQLMajor:              snapshot.Target.PostgreSQLMajor,
		PrimaryRequired:              snapshot.Target.PrimaryRequired,
		ReleaseArtifactDigest:        snapshot.Source.ReleaseArtifactDigest,
		SystemIdentifierDigest:       snapshot.Target.SystemIdentifierDigest,
		TLSCADigest:                  snapshot.Target.TLS.CADigest,
		TLSMode:                      snapshot.Target.TLS.Mode,
		Topology:                     snapshot.Authority.CutoverTopology,
	}
	passPolicy := PreflightPassPolicy{
		AllChecksRequired:            true,
		Checks:                       slices.Clone(preflightCheckRegistry[:]),
		Format:                       preflightPassPolicyFormat,
		MaximumObservationAgeSeconds: preflightMaximumObservationAgeSeconds,
		MutationAuthorized:           false,
		UnknownBlocks:                true,
	}
	abortPolicy := PreflightAbortPolicy{
		AbortOutcomes:      []string{"block", "unknown"},
		Checks:             slices.Clone(preflightCheckRegistry[:]),
		Format:             preflightAbortPolicyFormat,
		MutationAuthorized: false,
	}
	return expectation, passPolicy, abortPolicy
}

func digestWorkflowValue(domain string, value any) (string, error) {
	canonical, err := json.Marshal(value)
	if err != nil {
		return "", ErrInvalidPlan
	}
	hash := sha256.New()
	_, _ = hash.Write([]byte(domain))
	_, _ = hash.Write(canonical)
	return "sha256:" + hex.EncodeToString(hash.Sum(nil)), nil
}
