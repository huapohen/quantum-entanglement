package im

import (
	"errors"
	"strings"
	"testing"
)

func TestActorIdentityBindsSubjectTypeToStableID(t *testing.T) {
	t.Parallel()

	tenantID := mustTenantID(t, "ten_acme")
	for _, test := range []struct {
		name        string
		actorID     string
		subjectType SubjectType
	}{
		{name: "human", actorID: "usr_alice", subjectType: SubjectHuman},
		{name: "agent", actorID: "agt_finance_v1", subjectType: SubjectAgent},
		{name: "system", actorID: "sys_membership_projection", subjectType: SubjectSystem},
		{name: "service", actorID: "svc_rongcloud_adapter", subjectType: SubjectService},
	} {
		test := test
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()
			actorID := mustActorID(t, test.actorID)
			identity, err := NewActorIdentity(tenantID, actorID, test.subjectType, 7)
			if err != nil {
				t.Fatalf("NewActorIdentity() error = %v", err)
			}
			if identity.TenantID() != tenantID || identity.ActorID() != actorID ||
				identity.SubjectType() != test.subjectType || identity.Revision() != 7 || identity.IsZero() {
				t.Fatalf("unexpected identity: %#v", identity)
			}
		})
	}
}

func TestActorIdentityRejectsTypeImpersonationAndIncompleteScope(t *testing.T) {
	t.Parallel()

	tenantID := mustTenantID(t, "ten_acme")
	humanID := mustActorID(t, "usr_alice")
	for _, test := range []struct {
		name        string
		tenantID    TenantID
		actorID     ActorID
		subjectType SubjectType
		revision    uint64
	}{
		{name: "human ID cannot self-report agent", tenantID: tenantID, actorID: humanID, subjectType: SubjectAgent, revision: 1},
		{name: "missing tenant", actorID: humanID, subjectType: SubjectHuman, revision: 1},
		{name: "missing actor", tenantID: tenantID, subjectType: SubjectHuman, revision: 1},
		{name: "unknown subject", tenantID: tenantID, actorID: humanID, subjectType: SubjectType("owner"), revision: 1},
		{name: "zero revision", tenantID: tenantID, actorID: humanID, subjectType: SubjectHuman},
	} {
		test := test
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()
			identity, err := NewActorIdentity(
				test.tenantID, test.actorID, test.subjectType, test.revision,
			)
			if !errors.Is(err, ErrInvalidIdentity) || !identity.IsZero() {
				t.Fatalf("NewActorIdentity() = (%#v, %v), want zero and ErrInvalidIdentity", identity, err)
			}
		})
	}
}

func TestPlatformIdentifiersRejectAmbiguousOrUnboundedText(t *testing.T) {
	t.Parallel()

	for _, test := range []struct {
		name  string
		parse func(string) error
		value string
	}{
		{name: "tenant wrong prefix", parse: tenantParseError, value: "org_acme"},
		{name: "workspace empty suffix", parse: workspaceParseError, value: "wsp_"},
		{name: "actor unknown prefix", parse: actorParseError, value: "bot_helper"},
		{name: "agent definition whitespace", parse: agentDefinitionParseError, value: "agd_finance bot"},
		{name: "unicode confusable", parse: actorParseError, value: "agt_ａｄｍｉｎ"},
		{name: "control character", parse: tenantParseError, value: "ten_acme\nroot"},
		{name: "trailing separator", parse: actorParseError, value: "usr_alice_"},
		{name: "oversize bytes", parse: actorParseError, value: "agt_" + strings.Repeat("a", maxPlatformIDBytes)},
	} {
		test := test
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()
			if err := test.parse(test.value); !errors.Is(err, ErrInvalidIdentity) {
				t.Fatalf("parse(%q) error = %v, want ErrInvalidIdentity", test.value, err)
			}
		})
	}
}

func TestAgentVersionUsesStrictSemanticVersionIdentity(t *testing.T) {
	t.Parallel()

	for _, value := range []string{
		"0.0.0",
		"1.0.0",
		"2.17.4-rc.1",
		"10.20.30-alpha.beta+build.20260828",
	} {
		version, err := ParseAgentVersion(value)
		if err != nil || version.String() != value || version.IsZero() {
			t.Fatalf("ParseAgentVersion(%q) = (%q, %v)", value, version.String(), err)
		}
	}

	for _, value := range []string{
		"",
		"v1.0.0",
		"1",
		"1.0",
		"01.0.0",
		"1.01.0",
		"1.0.0-01",
		"1.0.0+",
		"1.0.0+build_1",
		"１.0.0",
		strings.Repeat("1", maxAgentVersionBytes+1),
	} {
		version, err := ParseAgentVersion(value)
		if !errors.Is(err, ErrInvalidIdentity) || !version.IsZero() {
			t.Fatalf("ParseAgentVersion(%q) = (%q, %v), want zero and ErrInvalidIdentity", value, version.String(), err)
		}
	}
}

func TestExternalIdentityReferenceIsMappingMetadataNotAnArbitrarySubject(t *testing.T) {
	t.Parallel()

	for _, test := range []struct {
		provider  IdentityProvider
		subjectID string
	}{
		{provider: IdentityProviderClerk, subjectID: "user_2abcDEF-123"},
		{provider: IdentityProviderRongCloud, subjectID: "usr_alice"},
		{provider: IdentityProviderRongCloud, subjectID: "agt_finance_v1"},
	} {
		reference, err := NewExternalIdentityRef(test.provider, test.subjectID)
		if err != nil || reference.Provider() != test.provider ||
			reference.SubjectID() != test.subjectID || reference.IsZero() {
			t.Fatalf("NewExternalIdentityRef(%q, %q) = (%#v, %v)", test.provider, test.subjectID, reference, err)
		}
	}

	for _, test := range []struct {
		name      string
		provider  IdentityProvider
		subjectID string
	}{
		{name: "unknown provider", provider: IdentityProvider("slack"), subjectID: "usr_alice"},
		{name: "Clerk requires user prefix", provider: IdentityProviderClerk, subjectID: "agt_finance"},
		{name: "RongCloud requires platform actor", provider: IdentityProviderRongCloud, subjectID: "external-random-user"},
		{name: "whitespace", provider: IdentityProviderClerk, subjectID: "user_alice root"},
		{name: "unicode", provider: IdentityProviderClerk, subjectID: "user_爱丽丝"},
		{name: "oversize", provider: IdentityProviderClerk, subjectID: "user_" + strings.Repeat("a", maxExternalSubjectBytes)},
	} {
		test := test
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()
			reference, err := NewExternalIdentityRef(test.provider, test.subjectID)
			if !errors.Is(err, ErrInvalidIdentity) || !reference.IsZero() {
				t.Fatalf("NewExternalIdentityRef() = (%#v, %v), want zero and ErrInvalidIdentity", reference, err)
			}
		})
	}
}

func mustTenantID(t *testing.T, value string) TenantID {
	t.Helper()
	identifier, err := ParseTenantID(value)
	if err != nil {
		t.Fatalf("ParseTenantID(%q) error = %v", value, err)
	}
	return identifier
}

func mustActorID(t *testing.T, value string) ActorID {
	t.Helper()
	identifier, err := ParseActorID(value)
	if err != nil {
		t.Fatalf("ParseActorID(%q) error = %v", value, err)
	}
	return identifier
}

func tenantParseError(value string) error {
	_, err := ParseTenantID(value)
	return err
}

func workspaceParseError(value string) error {
	_, err := ParseWorkspaceID(value)
	return err
}

func actorParseError(value string) error {
	_, err := ParseActorID(value)
	return err
}

func agentDefinitionParseError(value string) error {
	_, err := ParseAgentDefinitionID(value)
	return err
}
