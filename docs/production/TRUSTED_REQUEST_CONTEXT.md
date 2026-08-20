# Trusted request-context issuance foundation

Status: implemented and tested process-local primitive; **not an authenticated service or
Gate A completion claim**

Last reviewed: 2026-08-20

This document defines the minimum admission primitive that may distinguish caller-provided
scope claims from a request context issued after a configured authenticator has verified
them. It is a Gate A foundation, not an authenticated service, an OIDC/JWT implementation,
or proof that repositories enforce tenant/workspace scope.

The implementation is `src/quantum_entanglement/request_context.py` and its public symbols
are exported from `quantum_entanglement`. No current runtime, protocol adapter, CLI, or
repository automatically invokes it; composition remains an explicit follow-on gate.

## 1. Existing boundary and finding

The existing domain types intentionally do not authenticate callers:

- `AccessRequest.subject_id`, `tenant_id`, and `resource.workspace_id` are authorization
  inputs. A transport can construct all of them.
- `ActorRef`, `CoordinationEnvelope.sender`, `Authority`, and protocol payloads are routing
  or coordination data. Parsing or round-tripping them creates no identity trust.
- `Member` has no authoritative identity, membership revision, or freshness timestamp.
- `TenantAuthorizer` assumes its caller already supplied the correctly bound subject and a
  current membership record. It does not authenticate a request.
- `ServiceConfig` has no identity-provider configuration and there is no HTTP admission
  composition root.
- `SecretMaterial` is the only current bounded, redacted, explicitly closed material lease.
  It can contain an inbound credential for the duration of an authenticator call, but this
  does not make the file secret provider an identity provider.

Code must therefore keep an explicit type and call boundary between untrusted caller scope
claims and an issued context. Naming a dictionary `request_context`, copying an `ActorRef`,
or directly constructing an authentication-result value must never cross that boundary.

## 2. Threat and failure matrix

| Threat or ambiguity | Required foundation behavior | Still required outside the foundation |
|---|---|---|
| Caller changes subject, tenant, or workspace | Authenticator result must bind the exact caller claim; any mismatch fails closed | Real subject mapping and authoritative membership lookup |
| Caller reuses a valid context for another request or scope | Context is bound to exact request, subject, tenant, workspace, audience, issuer instance, and object identity | Transactional command replay protection and durable receipt |
| Caller fabricates a `RequestContext`-shaped object | Only the issuing instance's live registry can validate the exact object and immutable snapshot | Process/code isolation against arbitrary trusted-host code execution |
| Context is copied, pickled, persisted, or restored | Copy and serialization are rejected; a process restart or issuer replacement invalidates it | Distributed/session credential design if later required |
| Reflection mutates an issued object | Validation compares every field with the issuer-owned snapshot and fails closed | Sandboxing of malicious in-process plugins |
| Credential leaks through errors or retained state | Credential enters as a bounded `SecretMaterial` lease, is always closed, is absent from context/error fields, and translated failures detach the raw exception chain | Admission-buffer wiping, safe logs/traces, provider audit |
| Authenticator throws or returns an unexpected value | Stable redacted failure code; no permissive fallback | Provider health, alerting, retry/rate policy |
| Authenticator returns stale/future/overlong evidence or the host clock rolls back | Service-owned clock, skew bound, expiry, maximum TTL, and one issuer-local monotonic high-water reject revival; an in-skew rollback freezes logical time | Trusted time synchronization/monitoring and durable time policy for persistent authorization |
| Identity or membership changes after issuance | Preserve provider identity, identity revision, scope revision, and evidence fingerprint for action-time refresh | Authoritative action-time reauthentication and membership/policy revision comparison |
| Tenant-wide context becomes a workspace wildcard | Workspace matching is exact; `None` matches only `None` | Explicit separately reviewed tenant-wide operation model |
| Parsed protocol sender is treated as authenticated | Documentation and API keep `ActorRef` outside the issuance boundary | Adapter contract tests and authenticated protocol/API composition |
| Context registry is exhausted | Hard active-context bound and dead/expired-entry pruning; overflow fails closed | Per-tenant admission, rate, concurrency, and memory quotas |

The registry makes contexts non-forgeable through the supported public API. It is not a
cryptographic sandbox. Code that can inspect arbitrary Python process memory, import
private implementation details, replace the configured authenticator, or execute arbitrary
reflection already controls the trusted process and remains outside this primitive's threat
boundary.

## 3. Call and trust matrix

| Caller | Callee | Input trust | Output meaning |
|---|---|---|---|
| Transport/protocol adapter | strict caller-claim parser | Untrusted body/header/envelope fields | Canonical but still untrusted scope claims |
| Admission composition root | request-context issuer | Canonical claims plus one bounded credential lease | No trust until the issuer succeeds |
| Request-context issuer | configured authenticator | Exact caller claims, service audience/time, read-only credential view | Adapter result trusted only as the immediate return of this configured call |
| Request-context issuer | issuer-owned registry | Validated adapter result | One live process-local `RequestContext` handle |
| Protected-operation composition | same issuer instance | Exact context object plus exact `AccessRequest` | Local authenticity/scope check and reauthorization basis only |
| Identity/membership adapters | action-time policy composition | Current provider and membership observations | Inputs to `TenantAuthorizer`; no automatic allow |
| `TenantAuthorizer` | effect transaction | Current member, revocation state, verified capability and concrete request | RBAC/capability decision; still not an effect receipt |

The issuer must call the authenticator itself. It must not expose an overload that accepts a
pre-built authentication result from a caller. Authentication-result and reauthorization
evidence values remain data when received from anywhere else.

## 4. Implemented API and exact meaning

| Type or operation | Implemented guarantee | Must not be inferred |
|---|---|---|
| `CallerRequestContext` | Strict exact-dict parser for request/subject/tenant/workspace claims; unknown fields and coercion fail | Authentication, membership, or authorization |
| `AuthenticatedRequestBinding` | Bounded canonical adapter-return shape with provider principal, exact scope, revisions, evidence fingerprint, and lifetime | A caller-constructible token or portable attestation |
| `RequestAuthenticator` | Synchronous injected port called only by the issuer with a read-only credential view and service audience/time | OIDC, JWT, mTLS, JWKS, session, or membership implementation |
| `RequestContextIssuer.issue` | Reserves bounded capacity, consumes one exact `SecretMaterial`, serializes initial/completion/registration clock snapshots, validates exact binding/time, registers one context, and always attempts credential wipe | Replay defense, full request authorization, durable session issuance, or guaranteed erasure after a wipe primitive fails |
| `RequestContext` | Opaque, non-copyable, non-pickleable handle whose exact object and field snapshot are registered by one issuer | A bearer token, serialized credential, cross-process identity, or authority by property access |
| `prepare_reauthorization` | Same-issuer object check, tamper/expiry/clock check, exact `AccessRequest` request/subject/tenant/workspace match, and a bounded basis for current-state lookup | Current reauthentication, current membership, RBAC allow, approval, or effect permission |
| `ReauthorizationBasis` | Preserves principal/subject/scope, identity/scope revisions, evidence fingerprint and observation/expiry times; representation is redacted | A trusted input when constructed or received independently |
| `retire` / `close` | Invalidate one exact handle or every handle; contexts never survive issuer replacement | Distributed revocation or durable logout |

Successful authentication resamples service time after the adapter returns and again while
holding the registry lock immediately before registration. Every clock sample, high-water
mutation, expiry prune, registration, preparation, retirement, and close operation on the
same issuer is serialized. A slow adapter cannot issue a result that expired while it was
running. A physical clock regression within the configured skew returns the prior logical
high-water instead of moving time backward; a larger regression fails closed. Active plus
in-flight contexts share one hard capacity bound, so a full issuer rejects before another
authenticator call. Dead and expired entries are pruned against logical time before capacity
is reserved and cannot reappear after a physical rollback.

## 5. Required issued fields and invariants

An issued context preserves only bounded non-secret facts:

- random context identifier and exact inbound request identifier;
- configured authenticator identifier and audience;
- authenticated provider principal and mapped application subject;
- exact tenant and exact optional workspace;
- opaque identity revision and scope/membership-policy revision;
- SHA-256 evidence fingerprint, never the credential or raw attestation;
- authenticator observation time, issuer time, and hard expiry.

Every identifier and revision uses a bounded canonical alphabet. Times are timezone-aware
UTC values. The issuer accepts only exact result/context classes, snapshots all values, and
uses a service-owned clock. It rejects an expired result, an observation too far in the
future, a lifetime above the configured maximum, a scope mismatch, or an audience/provider
mismatch. Time comparisons use differences rather than unchecked upper-bound additions, so
valid UTC values near `datetime.max` fail or succeed by policy instead of leaking an
`OverflowError`.

The time high-water is intentionally process-local to the issuer. Contexts are also
process-local and invalid after issuer replacement, so no old handle crosses a reset. This
does not provide a durable time authority for capability, key, receipt, database, or
multi-process decisions. Operators must not recreate an issuer merely to bypass a rollback
alarm; stop admission, repair trusted time, invalidate old handles, and perform the wider
authorization-state reconciliation required by the service runbook.

Local context validation is not action-time authorization and not a reservation. It proves
only that this issuer produced this unchanged handle for this exact request scope and that
its local expiry has not passed. Before every protected operation, the future composition
root must use the preserved identity/revisions/evidence to obtain current authentication
and membership state, then run `TenantAuthorizer` for the concrete action/resource and bind
that decision to the effect transaction and durable receipt.

The basis returned by `prepare_reauthorization` is deliberately constructible data. A
future trusted identity/membership adapter may use it as lookup input, but no policy or
effect component may treat possession of the basis as proof. The current code does not yet
define that adapter or compare current provider/membership revisions.

## 6. Credential, configuration, and logging rules

- No ambient environment variable, API key, cookie, or token is read by this primitive.
- The authenticator is injected explicitly by the composition root. This phase provides
  fake test adapters only and defines no `QE_OIDC_*`, JWKS, JWT, mTLS, or HTTP settings.
- Credential close/wipe is attempted on success, validation failure, adapter failure, and
  unexpected result type. A wipe exception becomes one redacted stable failure and no
  context is returned; the implementation cannot claim that a failed underlying wipe
  succeeded. Translated authenticator, clock, result-validation, credential-access, and wipe
  failures have neither a retained `__cause__` nor `__context__`; `from None` alone is not a
  sufficient redaction boundary. Python also cannot wipe copies retained by a buggy
  authenticator or by the transport before constructing the lease.
- Context `repr`, exceptions, evidence, ordinary logs, events, artifacts, and reports must
  not contain the credential, raw attestation, tenant, workspace, subject, or principal.
- Stable failure codes are suitable for bounded metrics. Raw scope identifiers must not be
  metric labels.
- `evidence_fingerprint` must identify verified provider evidence with domain separation;
  do not directly hash a bearer token, password, or low-entropy credential and then retain
  that digest as evidence.

Python cannot guarantee erasure of the transport's original buffer, copies made by an
authenticator, interpreter temporaries, swap, crash dumps, or administrator-readable
memory. The credential lease narrows normal ownership; it is not a hardware secret enclave.

## 7. Minimal composition shape

The following shape is intentionally adapter-neutral. `authenticate` must be implemented by
a reviewed trusted adapter; a test fake returning a binding is not a production identity
provider.

```python
from quantum_entanglement import CallerRequestContext, RequestContextIssuer


def admit(*, issuer, raw_scope, credential_lease):
    claims = CallerRequestContext.from_dict(raw_scope)  # canonical, still untrusted
    return issuer.issue(claims, credential_lease)


def prepare_identity_refresh(*, issuer, context, access_request):
    basis = issuer.prepare_reauthorization(context, access_request)
    # A future trusted adapter must fetch current provider identity and exact
    # membership/policy revision here. `basis` itself cannot allow the action.
    return basis
```

The composition root owns issuer lifetime. Stop admission first, call `close` to invalidate
live handles and fence pending registrations, then wait for the surrounding calls to fail
or finish their credential cleanup. Closing during an authenticator call prevents that call
from registering a context; this primitive does not provide a drain/wait API.

## 8. Migration, rollback, and compatibility

This foundation is additive. Existing direct Python callers continue to construct
`AccessRequest`, but they remain inside the trusted-development boundary and do not become
authenticated. A later service composition must migrate one protected entry point at a
time:

1. parse caller scope into the explicitly untrusted claim type;
2. place the inbound credential in a bounded lease;
3. issue a context through the configured authenticator;
4. require the same issuer to validate exact request/scope at the operation boundary;
5. refresh current identity/membership revisions and evaluate policy;
6. bind allow evidence to an idempotent effect/receipt transaction.

Rollback is code-only because this primitive writes no database schema or persistent
context. Stop admission, drain or fence protected work, destroy the issuer (invalidating all
live contexts), deploy the prior code, and keep the service in its prior non-production
boundary. Never deserialize or grandfather a context across rollback/restart.

## 9. Verification

Run the dedicated and source gates from the repository root:

```bash
PYTHONPATH=src /usr/bin/python3 -m unittest tests.test_request_context -v
ruff check src/quantum_entanglement/request_context.py tests/test_request_context.py
ruff format --check src/quantum_entanglement/request_context.py tests/test_request_context.py
PYTHONPATH=src mypy --strict src/quantum_entanglement/request_context.py
python3 -m compileall -q \
  src/quantum_entanglement/request_context.py tests/test_request_context.py
git diff --check
```

The negative suite covers strict parsing, scope/result mismatch, future/stale/overlong
authentication, slow-authenticator expiry, issuer-local high-water expiry revival, in-skew
logical-time freeze, failed and concurrent clock samples, UTC upper-bound comparisons,
credential wiping, detached adapter/clock/result/wipe exception chains, compound
authentication-plus-wipe failure, capacity and in-flight reservation, foreign issuer,
direct construction, copy/pickle, exact request/subject/tenant/workspace matching,
tenant-wide non-wildcard behavior, reflective mutation quarantine, expiry, serialized
prepare/retire, concurrent close/registration, retirement, and issuer shutdown.

The implementation history starts at `fa0c422`; public export is `ba072c3`. Exact full-suite
and release evidence must be regenerated after this documentation commit is part of the
candidate tree. Test counts in this runbook are observations, never promotion criteria.

## 10. Residual Gate A blockers

Even after this primitive is implemented and its tests pass, Gate A remains closed until at
least all repositories enforce tenant/workspace scope, legacy data has a rehearsed
migration/rollback path, current identity and membership revision adapters are composed at
action time, and retained release evidence proves the complete boundary. Gates B-E remain
closed as defined by `SERVICE_BOUNDARY.md`.

Specific residual limitations of this slice are:

- no real authenticator, identity provider, session store, authenticated transport, or API;
- no action-time identity/membership refresh adapter and no revision comparison;
- no mandatory connection between `RequestContextIssuer`, `TenantAuthorizer`, runtime,
  approval, artifact, outbox, attempt, or effect-receipt paths;
- no tenant/workspace scope on every repository or legacy-data migration rehearsal;
- no cryptographic/distributed context, restart continuity, cross-process sharing, or
  defense against arbitrary code already executing inside the trusted Python process;
- no trusted infrastructure time/offset SLO or durable high-water policy outside the
  process-local request-context issuer;
- no body/decompression/rate/per-tenant quota admission boundary; and
- no proof that a future authenticator's evidence, identity mapping, membership freshness,
  key custody, failure behavior, or observability is correct.
