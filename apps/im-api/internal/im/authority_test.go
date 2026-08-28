package im

import (
	"errors"
	"reflect"
	"testing"
)

func TestExternalIdentityBindingSeparatesProviderProofFromPlatformMapping(t *testing.T) {
	principalID := mustHumanPrincipalID(t, "hpr_alice")
	realm, err := ParseProviderRealmID("rlm_prod")
	if err != nil {
		t.Fatalf("parse realm: %v", err)
	}
	externalRef, err := NewExternalIdentityRef(IdentityProviderClerk, realm, "user_alice")
	if err != nil {
		t.Fatalf("create external ref: %v", err)
	}

	binding, err := NewHumanExternalIdentityBinding(
		externalRef,
		principalID,
		ExternalIdentityBindingActive,
		7,
	)
	if err != nil {
		t.Fatalf("create binding: %v", err)
	}
	if binding.ExternalRef() != externalRef || binding.PrincipalID() != principalID ||
		binding.Status() != ExternalIdentityBindingActive || binding.Revision() != 7 ||
		binding.IsZero() {
		t.Fatalf("unexpected binding: %#v", binding)
	}
}

func TestProviderActorBindingAllowsHumanAndAgentRongCloudUsers(t *testing.T) {
	realm, err := ParseProviderRealmID("rlm_prod")
	if err != nil {
		t.Fatalf("parse realm: %v", err)
	}
	for _, actorText := range []string{"usr_alice", "agt_finance"} {
		actorRef, err := NewActorRef(mustTenantID(t, "ten_alpha"), mustActorID(t, actorText))
		if err != nil {
			t.Fatalf("create actor ref: %v", err)
		}
		externalRef, err := NewExternalIdentityRef(
			IdentityProviderRongCloud,
			realm,
			actorText,
		)
		if err != nil {
			t.Fatalf("create external ref: %v", err)
		}

		binding, err := NewProviderActorBinding(
			externalRef,
			actorRef,
			ExternalIdentityBindingActive,
			1,
		)
		if err != nil {
			t.Fatalf("create provider actor binding: %v", err)
		}
		if binding.ExternalRef() != externalRef || binding.ActorRef() != actorRef ||
			binding.Status() != ExternalIdentityBindingActive || binding.Revision() != 1 ||
			binding.IsZero() {
			t.Fatalf("unexpected provider actor mapping: %#v", binding)
		}
	}
}

func TestProviderActorBindingRejectsActorDriftAndInternalSubjects(t *testing.T) {
	realm, err := ParseProviderRealmID("rlm_prod")
	if err != nil {
		t.Fatalf("parse realm: %v", err)
	}
	externalRef, err := NewExternalIdentityRef(
		IdentityProviderRongCloud,
		realm,
		"usr_alice",
	)
	if err != nil {
		t.Fatalf("create external ref: %v", err)
	}
	for _, actorText := range []string{"usr_mallory", "sys_projection", "svc_adapter"} {
		actorRef, actorErr := NewActorRef(
			mustTenantID(t, "ten_alpha"),
			mustActorID(t, actorText),
		)
		if actorErr != nil {
			t.Fatalf("create actor ref: %v", actorErr)
		}
		value, bindingErr := NewProviderActorBinding(
			externalRef,
			actorRef,
			ExternalIdentityBindingActive,
			1,
		)
		if !errors.Is(bindingErr, ErrInvalidAuthority) || !value.IsZero() {
			t.Fatalf("expected actor binding rejection, got %#v, %v", value, bindingErr)
		}
	}
}

func TestProviderConversationBindingRequiresExactTenantConversationMapping(t *testing.T) {
	realm := mustProviderRealmID(t, "rlm_prod")
	externalRef, err := NewProviderConversationRef(
		IdentityProviderRongCloud,
		realm,
		"cnv_product",
	)
	if err != nil {
		t.Fatalf("create provider conversation ref: %v", err)
	}
	conversationRef, err := NewConversationRef(
		mustTenantID(t, "ten_alpha"),
		mustConversationID(t, "cnv_product"),
	)
	if err != nil {
		t.Fatalf("create conversation ref: %v", err)
	}
	binding, err := NewProviderConversationBinding(
		externalRef,
		conversationRef,
		ExternalIdentityBindingActive,
		2,
	)
	if err != nil {
		t.Fatalf("create provider conversation binding: %v", err)
	}
	if binding.ExternalRef() != externalRef || binding.ConversationRef() != conversationRef ||
		binding.Status() != ExternalIdentityBindingActive || binding.Revision() != 2 ||
		binding.IsZero() {
		t.Fatalf("unexpected provider conversation binding: %#v", binding)
	}

	driftedRef, err := NewConversationRef(
		mustTenantID(t, "ten_alpha"),
		mustConversationID(t, "cnv_other"),
	)
	if err != nil {
		t.Fatalf("create drifted conversation ref: %v", err)
	}
	value, err := NewProviderConversationBinding(
		externalRef,
		driftedRef,
		ExternalIdentityBindingActive,
		1,
	)
	if !errors.Is(err, ErrInvalidAuthority) || !value.IsZero() {
		t.Fatalf("expected provider conversation drift rejection, got %#v, %v", value, err)
	}
}

func TestExternalIdentityBindingRejectsInvalidStatusAndRevision(t *testing.T) {
	principalID := mustHumanPrincipalID(t, "hpr_alice")
	realm, err := ParseProviderRealmID("rlm_prod")
	if err != nil {
		t.Fatalf("parse realm: %v", err)
	}
	externalRef, err := NewExternalIdentityRef(IdentityProviderClerk, realm, "user_alice")
	if err != nil {
		t.Fatalf("create external ref: %v", err)
	}

	for _, fixture := range []struct {
		name     string
		status   ExternalIdentityBindingStatus
		revision uint64
	}{
		{name: "unknown status", status: "owner", revision: 1},
		{name: "zero revision", status: ExternalIdentityBindingActive, revision: 0},
		{name: "revision exceeds PostgreSQL bigint", status: ExternalIdentityBindingActive, revision: maxPersistentRevision + 1},
	} {
		t.Run(fixture.name, func(t *testing.T) {
			value, err := NewHumanExternalIdentityBinding(
				externalRef,
				principalID,
				fixture.status,
				fixture.revision,
			)
			if !errors.Is(err, ErrInvalidAuthority) || !value.IsZero() {
				t.Fatalf("expected zero value and fixed error, got %#v, %v", value, err)
			}
		})
	}
}

func TestHumanPrincipalSnapshotHasNoTenantAuthority(t *testing.T) {
	principalID := mustHumanPrincipalID(t, "hpr_alice")
	snapshot, err := NewHumanPrincipalSnapshot(principalID, HumanPrincipalActive, 3)
	if err != nil {
		t.Fatalf("create human principal snapshot: %v", err)
	}
	if snapshot.PrincipalID() != principalID || snapshot.Status() != HumanPrincipalActive ||
		snapshot.Revision() != 3 || snapshot.IsZero() {
		t.Fatalf("unexpected human principal snapshot: %#v", snapshot)
	}

	for _, fixture := range []struct {
		name      string
		principal HumanPrincipalID
		status    HumanPrincipalStatus
		revision  uint64
	}{
		{name: "zero principal", status: HumanPrincipalActive, revision: 1},
		{name: "unknown status", principal: principalID, status: "owner", revision: 1},
		{name: "zero revision", principal: principalID, status: HumanPrincipalActive},
	} {
		t.Run(fixture.name, func(t *testing.T) {
			value, err := NewHumanPrincipalSnapshot(
				fixture.principal,
				fixture.status,
				fixture.revision,
			)
			if !errors.Is(err, ErrInvalidAuthority) || !value.IsZero() {
				t.Fatalf("expected zero value and fixed error, got %#v, %v", value, err)
			}
		})
	}
}

func TestTenantMembershipMapsHumanPrincipalToTenantActor(t *testing.T) {
	tenant := mustTenantID(t, "ten_alpha")
	principalID := mustHumanPrincipalID(t, "hpr_alice")
	actorRef, err := NewActorRef(tenant, mustActorID(t, "usr_alice"))
	if err != nil {
		t.Fatalf("create actor ref: %v", err)
	}
	membership, err := NewTenantMembershipSnapshot(
		tenant,
		principalID,
		actorRef,
		TenantMembershipAdmin,
		TenantMembershipActive,
		5,
	)
	if err != nil {
		t.Fatalf("create tenant membership: %v", err)
	}
	if membership.TenantID() != tenant || membership.PrincipalID() != principalID ||
		membership.ActorRef() != actorRef || membership.Role() != TenantMembershipAdmin ||
		membership.Status() != TenantMembershipActive || membership.Revision() != 5 ||
		membership.IsZero() {
		t.Fatalf("unexpected tenant membership: %#v", membership)
	}
}

func TestTenantMembershipRejectsCrossTenantAndNonHumanActors(t *testing.T) {
	tenant := mustTenantID(t, "ten_alpha")
	principalID := mustHumanPrincipalID(t, "hpr_alice")
	humanOtherTenant, err := NewActorRef(
		mustTenantID(t, "ten_beta"),
		mustActorID(t, "usr_alice"),
	)
	if err != nil {
		t.Fatalf("create cross-tenant actor ref: %v", err)
	}
	agentRef, err := NewActorRef(tenant, mustActorID(t, "agt_finance"))
	if err != nil {
		t.Fatalf("create agent actor ref: %v", err)
	}

	for _, actorRef := range []ActorRef{humanOtherTenant, agentRef} {
		value, err := NewTenantMembershipSnapshot(
			tenant,
			principalID,
			actorRef,
			TenantMembershipMember,
			TenantMembershipActive,
			1,
		)
		if !errors.Is(err, ErrInvalidAuthority) || !value.IsZero() {
			t.Fatalf("expected tenant membership rejection, got %#v, %v", value, err)
		}
	}
}

func TestConversationMembershipIsTenantScopedAndIndependentFromTopology(t *testing.T) {
	tenant := mustTenantID(t, "ten_alpha")
	conversationRef, err := NewConversationRef(tenant, mustConversationID(t, "cnv_child"))
	if err != nil {
		t.Fatalf("create conversation ref: %v", err)
	}
	actorRef, err := NewActorRef(tenant, mustActorID(t, "agt_finance"))
	if err != nil {
		t.Fatalf("create actor ref: %v", err)
	}

	membership, err := NewConversationMembershipSnapshot(
		conversationRef,
		actorRef,
		ConversationMembershipManager,
		ConversationMembershipActive,
		2,
	)
	if err != nil {
		t.Fatalf("create membership: %v", err)
	}
	if membership.ConversationRef() != conversationRef || membership.ActorRef() != actorRef ||
		membership.Role() != ConversationMembershipManager ||
		membership.Status() != ConversationMembershipActive || membership.Revision() != 2 ||
		membership.IsZero() {
		t.Fatalf("unexpected membership: %#v", membership)
	}

	otherActor, err := NewActorRef(mustTenantID(t, "ten_beta"), mustActorID(t, "agt_finance"))
	if err != nil {
		t.Fatalf("create other actor ref: %v", err)
	}
	value, err := NewConversationMembershipSnapshot(
		conversationRef,
		otherActor,
		ConversationMembershipMember,
		ConversationMembershipActive,
		1,
	)
	if !errors.Is(err, ErrInvalidAuthority) || !value.IsZero() {
		t.Fatalf("expected cross-tenant membership rejection, got %#v, %v", value, err)
	}
	value, err = NewConversationMembershipSnapshot(
		conversationRef,
		actorRef,
		ConversationMembershipRole("admin"),
		ConversationMembershipActive,
		3,
	)
	if !errors.Is(err, ErrInvalidAuthority) || !value.IsZero() {
		t.Fatalf("expected unknown conversation role rejection, got %#v, %v", value, err)
	}
}

func TestConversationAccessCanonicalizesAndDetachesPermissions(t *testing.T) {
	tenant := mustTenantID(t, "ten_alpha")
	conversationRef, err := NewConversationRef(tenant, mustConversationID(t, "cnv_child"))
	if err != nil {
		t.Fatalf("create conversation ref: %v", err)
	}
	actorRef, err := NewActorRef(tenant, mustActorID(t, "usr_alice"))
	if err != nil {
		t.Fatalf("create actor ref: %v", err)
	}
	input := []ConversationPermission{
		ConversationPermissionInvokeAgent,
		ConversationPermissionRead,
		ConversationPermissionSendMessage,
	}

	access, err := NewConversationAccessSnapshot(conversationRef, actorRef, input, 3)
	if err != nil {
		t.Fatalf("create access snapshot: %v", err)
	}
	input[0] = ConversationPermissionManageConversation
	want := []ConversationPermission{
		ConversationPermissionRead,
		ConversationPermissionSendMessage,
		ConversationPermissionInvokeAgent,
	}
	if got := access.Permissions(); !reflect.DeepEqual(got, want) {
		t.Fatalf("unexpected canonical permissions: %#v", got)
	}
	first := access.Permissions()
	first[0] = ConversationPermissionManageMembers
	if got := access.Permissions(); !reflect.DeepEqual(got, want) {
		t.Fatalf("permissions getter leaked mutable state: %#v", got)
	}
	if !access.HasPermission(ConversationPermissionInvokeAgent) ||
		access.HasPermission(ConversationPermissionManageConversation) || access.IsZero() {
		t.Fatalf("unexpected permission lookup: %#v", access)
	}
}

func TestConversationAccessAcceptsExplicitEmptyRevocation(t *testing.T) {
	tenant := mustTenantID(t, "ten_alpha")
	conversationRef, err := NewConversationRef(tenant, mustConversationID(t, "cnv_child"))
	if err != nil {
		t.Fatalf("create conversation ref: %v", err)
	}
	actorRef, err := NewActorRef(tenant, mustActorID(t, "usr_alice"))
	if err != nil {
		t.Fatalf("create actor ref: %v", err)
	}

	access, err := NewConversationAccessSnapshot(conversationRef, actorRef, nil, 4)
	if err != nil {
		t.Fatalf("create empty access snapshot: %v", err)
	}
	if len(access.Permissions()) != 0 || access.IsZero() {
		t.Fatalf("unexpected empty access revision: %#v", access)
	}
}

func TestConversationAccessRejectsDuplicateUnknownAndCrossTenantValues(t *testing.T) {
	tenant := mustTenantID(t, "ten_alpha")
	conversationRef, err := NewConversationRef(tenant, mustConversationID(t, "cnv_child"))
	if err != nil {
		t.Fatalf("create conversation ref: %v", err)
	}
	actorRef, err := NewActorRef(tenant, mustActorID(t, "usr_alice"))
	if err != nil {
		t.Fatalf("create actor ref: %v", err)
	}
	otherActor, err := NewActorRef(mustTenantID(t, "ten_beta"), mustActorID(t, "usr_alice"))
	if err != nil {
		t.Fatalf("create other actor ref: %v", err)
	}

	for _, fixture := range []struct {
		name        string
		actor       ActorRef
		permissions []ConversationPermission
		revision    uint64
	}{
		{
			name:  "duplicate",
			actor: actorRef,
			permissions: []ConversationPermission{
				ConversationPermissionRead,
				ConversationPermissionRead,
			},
			revision: 1,
		},
		{name: "unknown", actor: actorRef, permissions: []ConversationPermission{"owner"}, revision: 1},
		{name: "cross tenant", actor: otherActor, permissions: nil, revision: 1},
		{name: "zero revision", actor: actorRef, permissions: nil, revision: 0},
	} {
		t.Run(fixture.name, func(t *testing.T) {
			value, err := NewConversationAccessSnapshot(
				conversationRef,
				fixture.actor,
				fixture.permissions,
				fixture.revision,
			)
			if !errors.Is(err, ErrInvalidAuthority) || !value.IsZero() {
				t.Fatalf("expected zero value and fixed error, got %#v, %v", value, err)
			}
		})
	}
}

func mustHumanPrincipalID(t *testing.T, value string) HumanPrincipalID {
	t.Helper()
	principalID, err := ParseHumanPrincipalID(value)
	if err != nil {
		t.Fatalf("parse human principal ID %q: %v", value, err)
	}
	return principalID
}
