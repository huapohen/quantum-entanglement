package im

import (
	"errors"
	"reflect"
	"testing"
)

func TestExternalIdentityBindingSeparatesProviderProofFromPlatformMapping(t *testing.T) {
	tenant := mustTenantID(t, "ten_alpha")
	actorID := mustActorID(t, "usr_alice")
	actorRef, err := NewActorRef(tenant, actorID)
	if err != nil {
		t.Fatalf("create actor ref: %v", err)
	}
	realm, err := ParseProviderRealmID("rlm_prod")
	if err != nil {
		t.Fatalf("parse realm: %v", err)
	}
	externalRef, err := NewExternalIdentityRef(IdentityProviderClerk, realm, "user_alice")
	if err != nil {
		t.Fatalf("create external ref: %v", err)
	}

	binding, err := NewExternalIdentityBinding(
		externalRef,
		actorRef,
		ExternalIdentityBindingActive,
		7,
	)
	if err != nil {
		t.Fatalf("create binding: %v", err)
	}
	if binding.ExternalRef() != externalRef || binding.ActorRef() != actorRef ||
		binding.Status() != ExternalIdentityBindingActive || binding.Revision() != 7 ||
		binding.IsZero() {
		t.Fatalf("unexpected binding: %#v", binding)
	}
}

func TestExternalIdentityBindingRejectsRongCloudActorDrift(t *testing.T) {
	tenant := mustTenantID(t, "ten_alpha")
	actorRef, err := NewActorRef(tenant, mustActorID(t, "usr_alice"))
	if err != nil {
		t.Fatalf("create actor ref: %v", err)
	}
	realm, err := ParseProviderRealmID("rlm_prod")
	if err != nil {
		t.Fatalf("parse realm: %v", err)
	}
	externalRef, err := NewExternalIdentityRef(
		IdentityProviderRongCloud,
		realm,
		"usr_mallory",
	)
	if err != nil {
		t.Fatalf("create external ref: %v", err)
	}

	_, err = NewExternalIdentityBinding(
		externalRef,
		actorRef,
		ExternalIdentityBindingActive,
		1,
	)
	if !errors.Is(err, ErrInvalidAuthority) {
		t.Fatalf("expected fixed authority error, got %v", err)
	}
}

func TestExternalIdentityBindingRejectsInvalidStatusAndRevision(t *testing.T) {
	tenant := mustTenantID(t, "ten_alpha")
	actorRef, err := NewActorRef(tenant, mustActorID(t, "usr_alice"))
	if err != nil {
		t.Fatalf("create actor ref: %v", err)
	}
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
	} {
		t.Run(fixture.name, func(t *testing.T) {
			value, err := NewExternalIdentityBinding(
				externalRef,
				actorRef,
				fixture.status,
				fixture.revision,
			)
			if !errors.Is(err, ErrInvalidAuthority) || !value.IsZero() {
				t.Fatalf("expected zero value and fixed error, got %#v, %v", value, err)
			}
		})
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
		ConversationMembershipActive,
		2,
	)
	if err != nil {
		t.Fatalf("create membership: %v", err)
	}
	if membership.ConversationRef() != conversationRef || membership.ActorRef() != actorRef ||
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
		ConversationMembershipActive,
		1,
	)
	if !errors.Is(err, ErrInvalidAuthority) || !value.IsZero() {
		t.Fatalf("expected cross-tenant membership rejection, got %#v, %v", value, err)
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
