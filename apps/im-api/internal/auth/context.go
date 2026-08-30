package auth

import (
	"context"
	"errors"
	"reflect"
	"time"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
)

var (
	ErrInvalidContext          = errors.New("invalid trusted request context")
	ErrContextUnauthorized     = errors.New("trusted request context unauthorized")
	ErrContextAuthorityMissing = errors.New("trusted request context authority not found")
	ErrContextUnavailable      = errors.New("trusted request context authority unavailable")
	ErrContextIntegrity        = errors.New("trusted request context authority integrity failure")
)

// IdentityAuthority is the narrow, read-only platform authority needed to turn an authenticated
// Clerk subject into a tenant-scoped Actor. Implementations must read current revisions from one
// consistent snapshot; none of these methods accepts a caller-supplied principal or actor.
type IdentityAuthority interface {
	CurrentHumanIdentityBinding(
		context.Context,
		im.ExternalIdentityRef,
	) (im.HumanExternalIdentityBinding, error)
	CurrentHumanPrincipal(
		context.Context,
		im.HumanPrincipalID,
	) (im.HumanPrincipalSnapshot, error)
	CurrentTenantMembership(
		context.Context,
		im.TenantID,
		im.HumanPrincipalID,
	) (im.TenantMembershipSnapshot, error)
	CurrentActor(
		context.Context,
		im.ActorRef,
	) (im.ActorSnapshot, error)
}

// TrustedRequestContext is the only authenticated request identity allowed into business
// authorization. It binds one verified provider subject to a fresh global principal, an active
// tenant membership, and an active human Actor. The snapshots are read-only values and are not
// reusable authorization leases; high-risk operations must resolve their resource authority again.
type TrustedRequestContext struct {
	identity   VerifiedIdentity
	principal  im.HumanPrincipalSnapshot
	membership im.TenantMembershipSnapshot
	actor      im.ActorSnapshot
}

// ResolveTrustedRequestContext performs all identity and tenant joins at request time. Inactive,
// missing, or cross-tenant authority is intentionally collapsed to ErrContextUnauthorized so a
// caller cannot use response differences to enumerate platform identity state.
func ResolveTrustedRequestContext(
	ctx context.Context,
	profile ProviderProfile,
	identity VerifiedIdentity,
	tenantID im.TenantID,
	authority IdentityAuthority,
	now time.Time,
) (TrustedRequestContext, error) {
	if ctx == nil || ctx.Err() != nil || isNilIdentityAuthority(authority) || tenantID.IsZero() ||
		now.IsZero() || now.Location() != time.UTC {
		return TrustedRequestContext{}, ErrInvalidContext
	}
	if err := identity.Validate(profile, now); err != nil {
		return TrustedRequestContext{}, err
	}
	binding, err := authority.CurrentHumanIdentityBinding(ctx, identity.ExternalRef)
	if err != nil {
		return TrustedRequestContext{}, mapAuthorityError(err)
	}
	if binding.IsZero() || binding.Status() != im.ExternalIdentityBindingActive ||
		binding.ExternalRef() != identity.ExternalRef || binding.PrincipalID().IsZero() {
		return TrustedRequestContext{}, ErrContextUnauthorized
	}
	principal, err := authority.CurrentHumanPrincipal(ctx, binding.PrincipalID())
	if err != nil {
		return TrustedRequestContext{}, mapAuthorityError(err)
	}
	if principal.IsZero() || principal.PrincipalID() != binding.PrincipalID() ||
		principal.Status() != im.HumanPrincipalActive {
		return TrustedRequestContext{}, ErrContextUnauthorized
	}
	membership, err := authority.CurrentTenantMembership(ctx, tenantID, binding.PrincipalID())
	if err != nil {
		return TrustedRequestContext{}, mapAuthorityError(err)
	}
	if membership.IsZero() || membership.Status() != im.TenantMembershipActive ||
		membership.TenantID() != tenantID || membership.PrincipalID() != binding.PrincipalID() ||
		membership.ActorRef().TenantID() != tenantID {
		return TrustedRequestContext{}, ErrContextUnauthorized
	}
	actor, err := authority.CurrentActor(ctx, membership.ActorRef())
	if err != nil {
		return TrustedRequestContext{}, mapAuthorityError(err)
	}
	if actor.IsZero() || actor.Ref() != membership.ActorRef() ||
		actor.SubjectType() != im.SubjectHuman || actor.Status() != im.ActorActive {
		return TrustedRequestContext{}, ErrContextUnauthorized
	}
	return TrustedRequestContext{
		identity: identity, principal: principal, membership: membership, actor: actor,
	}, nil
}

// Interfaces can contain a typed nil pointer. Treat that as absent before invoking the
// repository methods; this keeps a miswired runtime from panicking and preserves fail-closed
// behavior at the authentication boundary.
func isNilIdentityAuthority(authority IdentityAuthority) bool {
	if authority == nil {
		return true
	}
	value := reflect.ValueOf(authority)
	switch value.Kind() {
	case reflect.Chan, reflect.Func, reflect.Interface, reflect.Map, reflect.Pointer, reflect.Slice:
		return value.IsNil()
	default:
		return false
	}
}

func mapAuthorityError(err error) error {
	switch {
	case errors.Is(err, ErrContextAuthorityMissing):
		return ErrContextUnauthorized
	case errors.Is(err, ErrContextUnauthorized):
		return ErrContextUnauthorized
	case errors.Is(err, ErrContextIntegrity):
		return ErrContextIntegrity
	case errors.Is(err, ErrContextUnavailable):
		return ErrContextUnavailable
	default:
		return ErrContextUnavailable
	}
}

func (request TrustedRequestContext) Identity() VerifiedIdentity { return request.identity }

func (request TrustedRequestContext) Principal() im.HumanPrincipalSnapshot {
	return request.principal
}

func (request TrustedRequestContext) PrincipalID() im.HumanPrincipalID {
	return request.principal.PrincipalID()
}

func (request TrustedRequestContext) TenantID() im.TenantID {
	return request.membership.TenantID()
}

func (request TrustedRequestContext) Actor() im.ActorSnapshot { return request.actor }
func (request TrustedRequestContext) ActorRef() im.ActorRef   { return request.actor.Ref() }

func (request TrustedRequestContext) Membership() im.TenantMembershipSnapshot {
	return request.membership
}

func (request TrustedRequestContext) IsZero() bool {
	return request.identity.ExternalRef.IsZero() && request.principal.IsZero() &&
		request.membership.IsZero() && request.actor.IsZero()
}
