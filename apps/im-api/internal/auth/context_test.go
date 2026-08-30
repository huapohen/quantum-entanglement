package auth

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
)

func TestResolveTrustedRequestContextJoinsCurrentTenantAuthority(t *testing.T) {
	t.Parallel()
	fixture := newTrustedContextFixture(t)
	resolved, err := ResolveTrustedRequestContext(
		context.Background(), fixture.profile, fixture.identity, fixture.tenant, fixture.authority, fixture.now,
	)
	if err != nil {
		t.Fatalf("ResolveTrustedRequestContext() error = %v", err)
	}
	if resolved.IsZero() || resolved.Identity() != fixture.identity ||
		resolved.PrincipalID() != fixture.principal.PrincipalID() ||
		resolved.TenantID() != fixture.tenant || resolved.ActorRef() != fixture.actor.Ref() ||
		resolved.Membership().Revision() != fixture.membership.Revision() ||
		resolved.Actor().Revision() != fixture.actor.Revision() {
		t.Fatalf("unexpected trusted context: %#v", resolved)
	}
	// Accessors return values only; no provider token or mutable authority handle is exposed.
	if resolved.Identity().ExternalRef.SubjectID() != "user_alice" {
		t.Fatalf("unexpected verified subject: %#v", resolved.Identity())
	}
}

func TestResolveTrustedRequestContextFailsClosedForEveryAuthorityBoundary(t *testing.T) {
	t.Parallel()
	base := newTrustedContextFixture(t)
	tests := map[string]func(*trustedContextFixture){
		"missing tenant": func(value *trustedContextFixture) { value.tenant = im.TenantID{} },
		"expired identity": func(value *trustedContextFixture) {
			value.identity.ExpiresAt = value.now
		},
		"revoked external binding": func(value *trustedContextFixture) {
			value.binding, _ = im.NewHumanExternalIdentityBinding(
				value.identity.ExternalRef, value.principal.PrincipalID(), im.ExternalIdentityBindingRevoked, 1,
			)
		},
		"suspended principal": func(value *trustedContextFixture) {
			value.principal, _ = im.NewHumanPrincipalSnapshot(
				value.principal.PrincipalID(), im.HumanPrincipalSuspended, value.principal.Revision(),
			)
		},
		"removed membership": func(value *trustedContextFixture) {
			value.membership, _ = im.NewTenantMembershipSnapshot(
				value.tenant, value.principal.PrincipalID(), value.actor.Ref(), value.membership.Role(),
				im.TenantMembershipRemoved, value.membership.Revision(),
			)
		},
		"actor suspended": func(value *trustedContextFixture) {
			value.actor, _ = im.NewActorSnapshot(
				value.actor.Ref(), value.actor.SubjectType(), im.ActorSuspended, value.actor.Revision(),
			)
		},
		"membership actor mismatch": func(value *trustedContextFixture) {
			otherID, _ := im.ParseActorID("usr_bob")
			other, _ := im.NewActorRef(value.tenant, otherID)
			value.membership, _ = im.NewTenantMembershipSnapshot(
				value.tenant, value.principal.PrincipalID(), other, value.membership.Role(),
				value.membership.Status(), value.membership.Revision(),
			)
		},
	}
	for name, mutate := range tests {
		name, mutate := name, mutate
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			fixture := base.clone()
			mutate(&fixture)
			fixture.syncAuthority()
			_, err := ResolveTrustedRequestContext(
				context.Background(), fixture.profile, fixture.identity, fixture.tenant, fixture.authority, fixture.now,
			)
			if !errors.Is(err, ErrContextUnauthorized) && !errors.Is(err, ErrTokenExpired) &&
				!errors.Is(err, ErrInvalidContext) {
				t.Fatalf("resolution error = %v, want unauthorized or expired", err)
			}
		})
	}
}

func TestResolveTrustedRequestContextDoesNotLeakAuthorityStateAndHonorsCancellation(t *testing.T) {
	t.Parallel()
	fixture := newTrustedContextFixture(t)
	fixture.authority.errors = authorityErrors{binding: ErrContextAuthorityMissing}
	if _, err := ResolveTrustedRequestContext(
		context.Background(), fixture.profile, fixture.identity, fixture.tenant, fixture.authority, fixture.now,
	); !errors.Is(err, ErrContextUnauthorized) {
		t.Fatalf("missing binding error = %v, want unauthorized", err)
	}
	fixture.authority.errors = authorityErrors{principal: ErrContextIntegrity}
	if _, err := ResolveTrustedRequestContext(
		context.Background(), fixture.profile, fixture.identity, fixture.tenant, fixture.authority, fixture.now,
	); !errors.Is(err, ErrContextIntegrity) {
		t.Fatalf("integrity error = %v, want integrity", err)
	}
	fixture.authority.errors = authorityErrors{membership: ErrContextUnavailable}
	if _, err := ResolveTrustedRequestContext(
		context.Background(), fixture.profile, fixture.identity, fixture.tenant, fixture.authority, fixture.now,
	); !errors.Is(err, ErrContextUnavailable) {
		t.Fatalf("unavailable error = %v, want unavailable", err)
	}
	canceled, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := ResolveTrustedRequestContext(
		canceled, fixture.profile, fixture.identity, fixture.tenant, fixture.authority, fixture.now,
	); !errors.Is(err, ErrInvalidContext) {
		t.Fatalf("canceled context error = %v, want invalid context", err)
	}
}

func TestTrustedRequestContextContextHelpersRejectZeroAndRoundTripSnapshot(t *testing.T) {
	t.Parallel()
	if got := WithTrustedRequestContext(nil, TrustedRequestContext{}); got != nil {
		t.Fatalf("WithTrustedRequestContext(nil) = %#v, want nil", got)
	}
	if _, ok := TrustedRequestContextFromContext(context.Background()); ok {
		t.Fatal("unbound context unexpectedly contained trusted request context")
	}
	fixture := newTrustedContextFixture(t)
	request, err := ResolveTrustedRequestContext(
		context.Background(), fixture.profile, fixture.identity, fixture.tenant, fixture.authority, fixture.now,
	)
	if err != nil {
		t.Fatal(err)
	}
	bound := WithTrustedRequestContext(context.Background(), request)
	got, ok := TrustedRequestContextFromContext(bound)
	if !ok || got.PrincipalID() != request.PrincipalID() || got.TenantID() != request.TenantID() ||
		got.ActorRef() != request.ActorRef() {
		t.Fatalf("context round trip = %#v, %v", got, ok)
	}
}

type trustedContextFixture struct {
	now        time.Time
	profile    ProviderProfile
	identity   VerifiedIdentity
	tenant     im.TenantID
	principal  im.HumanPrincipalSnapshot
	binding    im.HumanExternalIdentityBinding
	membership im.TenantMembershipSnapshot
	actor      im.ActorSnapshot
	authority  *fakeIdentityAuthority
}

func newTrustedContextFixture(t *testing.T) trustedContextFixture {
	t.Helper()
	now := time.Date(2026, 8, 29, 12, 0, 0, 0, time.UTC)
	realm, err := im.ParseProviderRealmID("rlm_auth")
	if err != nil {
		t.Fatal(err)
	}
	profile, err := NewProviderProfile(
		im.IdentityProviderClerk, realm, "clerk.example", "wanwork-web", []Capability{CapabilityVerify}, 1024,
	)
	if err != nil {
		t.Fatal(err)
	}
	external, err := im.NewExternalIdentityRef(im.IdentityProviderClerk, realm, "user_alice")
	if err != nil {
		t.Fatal(err)
	}
	principalID, err := im.ParseHumanPrincipalID("hpr_alice")
	if err != nil {
		t.Fatal(err)
	}
	principal, err := im.NewHumanPrincipalSnapshot(principalID, im.HumanPrincipalActive, 1)
	if err != nil {
		t.Fatal(err)
	}
	binding, err := im.NewHumanExternalIdentityBinding(
		external, principalID, im.ExternalIdentityBindingActive, 1,
	)
	if err != nil {
		t.Fatal(err)
	}
	tenant, err := im.ParseTenantID("ten_alpha")
	if err != nil {
		t.Fatal(err)
	}
	actorID, err := im.ParseActorID("usr_alice")
	if err != nil {
		t.Fatal(err)
	}
	actorRef, err := im.NewActorRef(tenant, actorID)
	if err != nil {
		t.Fatal(err)
	}
	actor, err := im.NewActorSnapshot(actorRef, im.SubjectHuman, im.ActorActive, 1)
	if err != nil {
		t.Fatal(err)
	}
	membership, err := im.NewTenantMembershipSnapshot(
		tenant, principalID, actorRef, im.TenantMembershipMember, im.TenantMembershipActive, 1,
	)
	if err != nil {
		t.Fatal(err)
	}
	authority := &fakeIdentityAuthority{}
	authority.bindings = map[im.ExternalIdentityRef]im.HumanExternalIdentityBinding{external: binding}
	authority.principals = map[im.HumanPrincipalID]im.HumanPrincipalSnapshot{principalID: principal}
	authority.memberships = map[string]im.TenantMembershipSnapshot{tenant.String() + "\x00" + principalID.String(): membership}
	authority.actors = map[im.ActorRef]im.ActorSnapshot{actorRef: actor}
	return trustedContextFixture{
		now: now, profile: profile,
		identity: VerifiedIdentity{ExternalRef: external, SessionID: "sess_1", IssuedAt: now.Add(-time.Minute), ExpiresAt: now.Add(time.Hour)},
		tenant:   tenant, principal: principal, binding: binding, membership: membership, actor: actor, authority: authority,
	}
}

func (fixture trustedContextFixture) clone() trustedContextFixture {
	clone := fixture
	clone.authority = &fakeIdentityAuthority{
		bindings:    map[im.ExternalIdentityRef]im.HumanExternalIdentityBinding{fixture.identity.ExternalRef: clone.binding},
		principals:  map[im.HumanPrincipalID]im.HumanPrincipalSnapshot{clone.principal.PrincipalID(): clone.principal},
		memberships: map[string]im.TenantMembershipSnapshot{clone.tenant.String() + "\x00" + clone.principal.PrincipalID().String(): clone.membership},
		actors:      map[im.ActorRef]im.ActorSnapshot{clone.membership.ActorRef(): clone.actor},
	}
	return clone
}

func (fixture *trustedContextFixture) syncAuthority() {
	fixture.authority.bindings = map[im.ExternalIdentityRef]im.HumanExternalIdentityBinding{
		fixture.identity.ExternalRef: fixture.binding,
	}
	fixture.authority.principals = map[im.HumanPrincipalID]im.HumanPrincipalSnapshot{
		fixture.principal.PrincipalID(): fixture.principal,
	}
	fixture.authority.memberships = map[string]im.TenantMembershipSnapshot{
		fixture.tenant.String() + "\x00" + fixture.principal.PrincipalID().String(): fixture.membership,
	}
	fixture.authority.actors = map[im.ActorRef]im.ActorSnapshot{
		fixture.membership.ActorRef(): fixture.actor,
	}
}

type authorityErrors struct {
	binding, principal, membership, actor error
}

type fakeIdentityAuthority struct {
	bindings    map[im.ExternalIdentityRef]im.HumanExternalIdentityBinding
	principals  map[im.HumanPrincipalID]im.HumanPrincipalSnapshot
	memberships map[string]im.TenantMembershipSnapshot
	actors      map[im.ActorRef]im.ActorSnapshot
	errors      authorityErrors
}

func (authority *fakeIdentityAuthority) CurrentHumanIdentityBinding(
	_ context.Context, reference im.ExternalIdentityRef,
) (im.HumanExternalIdentityBinding, error) {
	if authority.errors.binding != nil {
		return im.HumanExternalIdentityBinding{}, authority.errors.binding
	}
	value, ok := authority.bindings[reference]
	if !ok {
		return im.HumanExternalIdentityBinding{}, ErrContextAuthorityMissing
	}
	return value, nil
}

func (authority *fakeIdentityAuthority) CurrentHumanPrincipal(
	_ context.Context, principalID im.HumanPrincipalID,
) (im.HumanPrincipalSnapshot, error) {
	if authority.errors.principal != nil {
		return im.HumanPrincipalSnapshot{}, authority.errors.principal
	}
	value, ok := authority.principals[principalID]
	if !ok {
		return im.HumanPrincipalSnapshot{}, ErrContextAuthorityMissing
	}
	return value, nil
}

func (authority *fakeIdentityAuthority) CurrentTenantMembership(
	_ context.Context, tenantID im.TenantID, principalID im.HumanPrincipalID,
) (im.TenantMembershipSnapshot, error) {
	if authority.errors.membership != nil {
		return im.TenantMembershipSnapshot{}, authority.errors.membership
	}
	value, ok := authority.memberships[tenantID.String()+"\x00"+principalID.String()]
	if !ok {
		return im.TenantMembershipSnapshot{}, ErrContextAuthorityMissing
	}
	return value, nil
}

func (authority *fakeIdentityAuthority) CurrentActor(
	_ context.Context, reference im.ActorRef,
) (im.ActorSnapshot, error) {
	if authority.errors.actor != nil {
		return im.ActorSnapshot{}, authority.errors.actor
	}
	value, ok := authority.actors[reference]
	if !ok {
		return im.ActorSnapshot{}, ErrContextAuthorityMissing
	}
	return value, nil
}
