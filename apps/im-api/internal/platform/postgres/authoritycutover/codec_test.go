package authoritycutover

import (
	"bytes"
	"encoding/json"
	"errors"
	"slices"
	"strings"
	"testing"
)

func TestDecodePlanNormalizesDeclaredSetsAndRoundTrips(t *testing.T) {
	plan, err := BuildPlan(validPlanInput())
	if err != nil {
		t.Fatalf("BuildPlan: %v", err)
	}
	decoded, err := DecodePlan(plan.CanonicalBytes())
	if err != nil {
		t.Fatalf("DecodePlan canonical: %v", err)
	}
	if decoded.Digest() != plan.Digest() || !bytes.Equal(decoded.CanonicalBytes(), plan.CanonicalBytes()) {
		t.Fatalf("canonical round trip changed plan")
	}

	snapshot := plan.Snapshot()
	slices.Reverse(snapshot.AbortConditions)
	slices.Reverse(snapshot.Credentials)
	slices.Reverse(snapshot.Authority.Manifest.MigrationLoginRoles)
	slices.Reverse(snapshot.Authority.Manifest.RuntimeLoginRoles)
	nonCanonicalOrder, err := json.MarshalIndent(snapshot, "", "  ")
	if err != nil {
		t.Fatalf("marshal reordered: %v", err)
	}
	decoded, err = DecodePlan(nonCanonicalOrder)
	if err != nil {
		t.Fatalf("DecodePlan reordered sets: %v", err)
	}
	if decoded.Digest() != plan.Digest() || !bytes.Equal(decoded.CanonicalBytes(), plan.CanonicalBytes()) {
		t.Fatal("declared set ordering changed canonical plan")
	}
}

func TestDecodePlanRejectsStructuralAndDigestDrift(t *testing.T) {
	plan, err := BuildPlan(validPlanInput())
	if err != nil {
		t.Fatalf("BuildPlan: %v", err)
	}
	canonical := plan.CanonicalBytes()
	planIDFragment := []byte(`"planId":"plan-20260829-0001"`)
	credentialFragment := []byte(`"credentials":[`)
	tests := map[string][]byte{
		"unknown field":      bytes.Replace(canonical, []byte(`{"abortConditions"`), []byte(`{"unknown":true,"abortConditions"`), 1),
		"duplicate key":      bytes.Replace(canonical, planIDFragment, append(slices.Clone(planIDFragment), []byte(`,"planId":"plan-20260829-0001"`)...), 1),
		"trailing value":     append(slices.Clone(canonical), []byte(` {}`)...),
		"fractional integer": bytes.Replace(canonical, []byte(`"postgresqlMajor":18`), []byte(`"postgresqlMajor":18.0`), 1),
		"null collection":    bytes.Replace(canonical, credentialFragment, []byte(`"credentials":null,"discarded":[`), 1),
		"tampered digest":    bytes.Replace(canonical, []byte("sha256:"+strings.Repeat("a", 64)), []byte("sha256:"+strings.Repeat("f", 64)), 1),
		"non utf8":           append(slices.Clone(canonical), 0xff),
	}
	for name, raw := range tests {
		t.Run(name, func(t *testing.T) {
			if _, err := DecodePlan(raw); !errors.Is(err, ErrInvalidPlan) {
				t.Fatalf("DecodePlan error = %v, want %v", err, ErrInvalidPlan)
			}
		})
	}
	oversized := bytes.Repeat([]byte{'x'}, maximumPlanBytes+1)
	if _, err := DecodePlan(oversized); !errors.Is(err, ErrPlanTooLarge) {
		t.Fatalf("oversized error = %v, want %v", err, ErrPlanTooLarge)
	}
}
