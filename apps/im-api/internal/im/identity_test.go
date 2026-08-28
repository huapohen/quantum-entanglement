package im

import (
	"errors"
	"strings"
	"testing"
)

func TestActorSnapshotBindsSubjectTypeToStableReference(t *testing.T) {
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
			inferredType, ok := actorID.SubjectType()
			if !ok || inferredType != test.subjectType {
				t.Fatalf("ActorID.SubjectType() = (%q, %v), want (%q, true)", inferredType, ok, test.subjectType)
			}
			reference, err := NewActorRef(tenantID, actorID)
			if err != nil {
				t.Fatalf("NewActorRef() error = %v", err)
			}
			snapshot, err := NewActorSnapshot(reference, test.subjectType, ActorActive, 7)
			if err != nil {
				t.Fatalf("NewActorSnapshot() error = %v", err)
			}
			if snapshot.Ref() != reference || reference.TenantID() != tenantID ||
				reference.ActorID() != actorID || snapshot.SubjectType() != test.subjectType ||
				snapshot.Status() != ActorActive ||
				snapshot.Revision() != 7 || reference.IsZero() || snapshot.IsZero() {
				t.Fatalf("unexpected actor reference/snapshot: %#v %#v", reference, snapshot)
			}
		})
	}
}

func TestActorReferenceRemainsStableAcrossSnapshotRevisions(t *testing.T) {
	t.Parallel()

	reference, err := NewActorRef(mustTenantID(t, "ten_acme"), mustActorID(t, "agt_finance"))
	if err != nil {
		t.Fatalf("NewActorRef() error = %v", err)
	}
	first, err := NewActorSnapshot(reference, SubjectAgent, ActorActive, 1)
	if err != nil {
		t.Fatalf("NewActorSnapshot(first) error = %v", err)
	}
	second, err := NewActorSnapshot(reference, SubjectAgent, ActorSuspended, 2)
	if err != nil {
		t.Fatalf("NewActorSnapshot(second) error = %v", err)
	}
	if first == second || first.Ref() != second.Ref() {
		t.Fatalf("snapshots must differ while stable refs match: %#v %#v", first, second)
	}
}

func TestZeroActorIDHasNoSubjectType(t *testing.T) {
	t.Parallel()

	if subjectType, ok := (ActorID{}).SubjectType(); ok || subjectType != "" {
		t.Fatalf("zero ActorID.SubjectType() = (%q, %v), want empty and false", subjectType, ok)
	}
}

func TestActorReferenceRejectsIncompleteScope(t *testing.T) {
	t.Parallel()

	tenantID := mustTenantID(t, "ten_acme")
	humanID := mustActorID(t, "usr_alice")
	for _, test := range []struct {
		name     string
		tenantID TenantID
		actorID  ActorID
	}{
		{name: "missing tenant", actorID: humanID},
		{name: "missing actor", tenantID: tenantID},
	} {
		test := test
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()
			reference, err := NewActorRef(test.tenantID, test.actorID)
			if !errors.Is(err, ErrInvalidIdentity) || !reference.IsZero() {
				t.Fatalf("NewActorRef() = (%#v, %v), want zero and ErrInvalidIdentity", reference, err)
			}
		})
	}
}

func TestActorSnapshotRejectsSubjectPrefixMismatchAndIncompleteSnapshot(t *testing.T) {
	t.Parallel()

	reference, err := NewActorRef(mustTenantID(t, "ten_acme"), mustActorID(t, "usr_alice"))
	if err != nil {
		t.Fatalf("NewActorRef() error = %v", err)
	}
	for _, test := range []struct {
		name        string
		reference   ActorRef
		subjectType SubjectType
		status      ActorStatus
		revision    uint64
	}{
		{name: "human prefix cannot claim agent type", reference: reference, subjectType: SubjectAgent, status: ActorActive, revision: 1},
		{name: "missing reference", subjectType: SubjectHuman, status: ActorActive, revision: 1},
		{name: "unknown subject", reference: reference, subjectType: SubjectType("owner"), status: ActorActive, revision: 1},
		{name: "unknown status", reference: reference, subjectType: SubjectHuman, status: ActorStatus("ready"), revision: 1},
		{name: "zero revision", reference: reference, subjectType: SubjectHuman, status: ActorActive},
		{
			name:        "revision exceeds PostgreSQL bigint",
			reference:   reference,
			subjectType: SubjectHuman,
			status:      ActorActive,
			revision:    maxPersistentRevision + 1,
		},
	} {
		test := test
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()
			snapshot, err := NewActorSnapshot(test.reference, test.subjectType, test.status, test.revision)
			if !errors.Is(err, ErrInvalidIdentity) || !snapshot.IsZero() {
				t.Fatalf("NewActorSnapshot() = (%#v, %v), want zero and ErrInvalidIdentity", snapshot, err)
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
		{name: "provider realm wrong prefix", parse: providerRealmParseError, value: "app_prod"},
		{name: "human principal wrong prefix", parse: humanPrincipalParseError, value: "usr_alice"},
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

func TestAgentVersionUsesStrictSemanticVersionLabel(t *testing.T) {
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
	realmID := mustProviderRealmID(t, "rlm_prod")

	for _, test := range []struct {
		provider  IdentityProvider
		subjectID string
	}{
		{provider: IdentityProviderClerk, subjectID: "user_2abcDEF-123"},
		{provider: IdentityProviderRongCloud, subjectID: "usr_alice"},
		{provider: IdentityProviderRongCloud, subjectID: "agt_finance_v1"},
	} {
		reference, err := NewExternalIdentityRef(test.provider, realmID, test.subjectID)
		if err != nil || reference.Provider() != test.provider ||
			reference.RealmID() != realmID || reference.SubjectID() != test.subjectID ||
			reference.IsZero() {
			t.Fatalf("NewExternalIdentityRef(%q, %q, %q) = (%#v, %v)", test.provider, realmID.String(), test.subjectID, reference, err)
		}
	}

	for _, test := range []struct {
		name      string
		provider  IdentityProvider
		realmID   ProviderRealmID
		subjectID string
	}{
		{name: "unknown provider", provider: IdentityProvider("slack"), realmID: realmID, subjectID: "usr_alice"},
		{name: "missing realm", provider: IdentityProviderClerk, subjectID: "user_alice"},
		{name: "Clerk requires user prefix", provider: IdentityProviderClerk, realmID: realmID, subjectID: "agt_finance"},
		{name: "RongCloud requires platform actor", provider: IdentityProviderRongCloud, realmID: realmID, subjectID: "external-random-user"},
		{name: "whitespace", provider: IdentityProviderClerk, realmID: realmID, subjectID: "user_alice root"},
		{name: "unicode", provider: IdentityProviderClerk, realmID: realmID, subjectID: "user_爱丽丝"},
		{name: "oversize", provider: IdentityProviderClerk, realmID: realmID, subjectID: "user_" + strings.Repeat("a", maxExternalSubjectBytes)},
	} {
		test := test
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()
			reference, err := NewExternalIdentityRef(test.provider, test.realmID, test.subjectID)
			if !errors.Is(err, ErrInvalidIdentity) || !reference.IsZero() {
				t.Fatalf("NewExternalIdentityRef() = (%#v, %v), want zero and ErrInvalidIdentity", reference, err)
			}
		})
	}
}

func TestExternalIdentityReferenceIsolatesProviderRealms(t *testing.T) {
	t.Parallel()

	firstRealm := mustProviderRealmID(t, "rlm_staging")
	secondRealm := mustProviderRealmID(t, "rlm_production")
	first, err := NewExternalIdentityRef(IdentityProviderClerk, firstRealm, "user_alice")
	if err != nil {
		t.Fatalf("NewExternalIdentityRef(first) error = %v", err)
	}
	second, err := NewExternalIdentityRef(IdentityProviderClerk, secondRealm, "user_alice")
	if err != nil {
		t.Fatalf("NewExternalIdentityRef(second) error = %v", err)
	}
	if first == second {
		t.Fatalf("same provider subject in separate realms must not collapse: %#v %#v", first, second)
	}
}

func mustProviderRealmID(t *testing.T, value string) ProviderRealmID {
	t.Helper()
	identifier, err := ParseProviderRealmID(value)
	if err != nil {
		t.Fatalf("ParseProviderRealmID(%q) error = %v", value, err)
	}
	return identifier
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

func providerRealmParseError(value string) error {
	_, err := ParseProviderRealmID(value)
	return err
}

func humanPrincipalParseError(value string) error {
	_, err := ParseHumanPrincipalID(value)
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
