package authoritycutover

import (
	"errors"
	"slices"
	"testing"
)

func TestBuildPlanDerivesExactCodeOwnedWorkflow(t *testing.T) {
	plan, err := BuildPlan(validPlanInput())
	if err != nil {
		t.Fatalf("BuildPlan: %v", err)
	}
	snapshot := plan.Snapshot()
	wantActions := []string{
		"read-authority",
		"create-authority",
		"apply-catalog",
		"converge-ownership",
		"attest-runtime",
	}
	if len(snapshot.Steps) != len(wantActions) {
		t.Fatalf("steps = %d, want %d", len(snapshot.Steps), len(wantActions))
	}
	for index, wantAction := range wantActions {
		if snapshot.Steps[index].Action != wantAction {
			t.Fatalf("step %d action = %q, want %q", index, snapshot.Steps[index].Action, wantAction)
		}
	}
	if !slices.Equal(snapshot.AbortConditions, preflightCheckRegistry[:]) {
		t.Fatalf("abort conditions = %q, want %q", snapshot.AbortConditions, preflightCheckRegistry)
	}

	expectation, passPolicy, abortPolicy := derivePreflightPolicies(snapshot)
	if expectation.MutationAuthorized || passPolicy.MutationAuthorized || abortPolicy.MutationAuthorized {
		t.Fatalf("preflight policy authorized mutation: expectation=%#v pass=%#v abort=%#v", expectation, passPolicy, abortPolicy)
	}
	if !passPolicy.AllChecksRequired || !passPolicy.UnknownBlocks ||
		passPolicy.MaximumObservationAgeSeconds != preflightMaximumObservationAgeSeconds ||
		!slices.Equal(expectation.Checks, preflightCheckRegistry[:]) ||
		!slices.Equal(passPolicy.Checks, preflightCheckRegistry[:]) ||
		!slices.Equal(abortPolicy.Checks, preflightCheckRegistry[:]) ||
		!slices.Equal(abortPolicy.AbortOutcomes, []string{"block", "unknown"}) {
		t.Fatalf("preflight policy is not fail closed: expectation=%#v pass=%#v abort=%#v", expectation, passPolicy, abortPolicy)
	}
	expectationDigest, err := digestWorkflowValue(preflightExpectationDigestDomain, expectation)
	if err != nil {
		t.Fatalf("expectation digest: %v", err)
	}
	passDigest, err := digestWorkflowValue(preflightPassPolicyDigestDomain, passPolicy)
	if err != nil {
		t.Fatalf("pass digest: %v", err)
	}
	abortDigest, err := digestWorkflowValue(preflightAbortPolicyDigestDomain, abortPolicy)
	if err != nil {
		t.Fatalf("abort digest: %v", err)
	}
	preflight := snapshot.Steps[0]
	if preflight.PreconditionDigest != expectationDigest || preflight.PostconditionDigest != passDigest ||
		preflight.AbortConditionDigest != abortDigest || expectationDigest == passDigest ||
		expectationDigest == abortDigest || passDigest == abortDigest {
		t.Fatalf("preflight typed digest binding = %#v", preflight)
	}

	expectation.Checks[0] = "mutated"
	passPolicy.Checks[0] = "mutated"
	abortPolicy.Checks[0] = "mutated"
	nextExpectation, nextPass, nextAbort := derivePreflightPolicies(snapshot)
	if nextExpectation.Checks[0] == "mutated" || nextPass.Checks[0] == "mutated" ||
		nextAbort.Checks[0] == "mutated" || preflightCheckRegistry[0] == "mutated" {
		t.Fatal("caller mutation escaped the code-owned workflow registry")
	}
}

func TestPlanRejectsAnyDerivedWorkflowDrift(t *testing.T) {
	plan, err := BuildPlan(validPlanInput())
	if err != nil {
		t.Fatalf("BuildPlan: %v", err)
	}
	mutations := map[string]func(*PlanSnapshot){
		"missing step": func(value *PlanSnapshot) { value.Steps = value.Steps[:4] },
		"extra step": func(value *PlanSnapshot) {
			value.Steps = append(value.Steps, value.Steps[len(value.Steps)-1])
		},
		"reordered step": func(value *PlanSnapshot) { value.Steps[0], value.Steps[1] = value.Steps[1], value.Steps[0] },
		"unknown action": func(value *PlanSnapshot) { value.Steps[0].Action = "caller-action" },
		"wrong executor": func(value *PlanSnapshot) { value.Steps[0].RequiredExecutor = ExecutorOwner },
		"caller precondition": func(value *PlanSnapshot) {
			value.Steps[0].PreconditionDigest = value.Steps[1].PreconditionDigest
		},
		"missing check": func(value *PlanSnapshot) { value.AbortConditions = value.AbortConditions[1:] },
		"extra check": func(value *PlanSnapshot) {
			value.AbortConditions = append(value.AbortConditions, "caller/check")
		},
	}
	for name, mutate := range mutations {
		t.Run(name, func(t *testing.T) {
			snapshot := plan.Snapshot()
			mutate(&snapshot)
			if _, err := sealPlan(snapshot); !errors.Is(err, ErrInvalidPlan) {
				t.Fatalf("sealPlan error = %v, want %v", err, ErrInvalidPlan)
			}
		})
	}
}

func TestPreflightExpectationDigestChangesWithObservedTargetBinding(t *testing.T) {
	plan, err := BuildPlan(validPlanInput())
	if err != nil {
		t.Fatalf("BuildPlan: %v", err)
	}
	changed := validPlanInput()
	changed.Backup.AttestationDigest = "sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
	changedPlan, err := BuildPlan(changed)
	if err != nil {
		t.Fatalf("BuildPlan changed: %v", err)
	}
	first := plan.Snapshot().Steps[0]
	second := changedPlan.Snapshot().Steps[0]
	if first.PreconditionDigest == second.PreconditionDigest ||
		first.PostconditionDigest != second.PostconditionDigest ||
		first.AbortConditionDigest != second.AbortConditionDigest {
		t.Fatalf("unexpected typed policy digest change first=%#v second=%#v", first, second)
	}
}
