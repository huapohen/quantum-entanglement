# Protected-operation authorization composition foundation

Status: implemented and tested process-local composition primitive; **fake-only, not wired
to a repository, not production-ready, and not a Gate A completion claim**

Last reviewed: 2026-08-20

This document defines the narrow boundary implemented in
`src/quantum_entanglement/operation_authorization.py`. The boundary composes one trusted
`RequestContextIssuer`, one injected current-state provider, and one exact
`TenantAuthorizer` into a short-lived, one-time `AuthorizedOperation` handle.

The implementation closes an important false-authorization gap: a canonical
`AccessRequest`, directly constructed `ReauthorizationBasis`, current-state value, or
`AuthorizationDecision` can no longer be mistaken for permission by a caller that uses
this composition. It does **not** connect that permission to any repository or external
effect. Gate A remains closed.

The module is intentionally not exported from the package root in this slice. Callers must
opt in through `quantum_entanglement.operation_authorization` while the integration and
production-adapter contracts remain under review.

## 1. Exact call path and authority transition

The supported path has a preliminary issuance decision and a mandatory action-time
decision:

```text
RequestContext + AccessRequest
        |
        v
same RequestContextIssuer.prepare_reauthorization
        |  ReauthorizationBasis (non-authorizing)
        v
configured CurrentAuthorizationStateProvider.load_current_state
        |  CurrentAuthorizationState (non-authorizing)
        v
exact identity/scope/revision/freshness comparison
        |
        v
exact TenantAuthorizer.evaluate
        |  AuthorizationDecision
        v
explicit AuthorizationOutcome.ALLOW only
        |
        v
composer-owned bounded registry -> AuthorizedOperation
        |
        v
same composer.consume(operation, same context, exact request)
        |
        +-> same RequestContextIssuer.prepare_reauthorization
        +-> registry preflight: live, unmodified, unexpired, exact actor/request scope
        +-> CurrentAuthorizationStateProvider.load_current_state again
        +-> exact identity/scope/revision/freshness comparison again
        +-> TenantAuthorizer.evaluate again -> explicit ALLOW only
        +-> same issuer final context check
        |  atomic compare-and-retire inside this process only after fresh ALLOW
        v
future effect adapter boundary (not implemented in this slice)
```

Before any issuer, composer, or registry lock, state table, clock, provider, authenticator,
or authorizer is touched, the implementation verifies that each process-local object still
belongs to its creating process. The issuer, composer, and registry capture both the
creator PID and a process epoch. A forked child receives new module epochs and cannot enter
this call path with inherited objects. Composer construction and every authorization path
also verify that the configured issuer belongs to the current process.

Authority is created only at the registry issuance step and only after every preceding
check succeeds. The provider state and authorizer decision remain ordinary value objects.
Direct construction, deserialization, subclassing, truthiness, or possession of those
values grants nothing.

`consume` first repeats same-issuer context validation to obtain the exact basis required by
the registry scope comparison. It then proves that the handle is still live and bound to
that exact actor/request before reloading current identity, membership, revocation, and
capability state, repeating exact revision/freshness comparisons, and invoking the exact
authorizer again. It performs a final same-issuer context check and atomically removes the
handle only after that action-time decision is an explicit `ALLOW`. A successful handle can
therefore be consumed once. Failed scope, state, revision, dependency, or policy checks do
not consume it and grant no effect permission. Two concurrent correct consumers may both
perform fresh checks, but exactly one can complete the local consume.

## 2. Trust and data matrix

| Value or component | Trust at entry | Implemented check | Authority after check |
|---|---|---|---|
| `AccessRequest` | Canonical but caller-controllable | Exact snapshot; request/subject/tenant/workspace matched by the issuer; action/resource later bound to the handle | None |
| `RequestContext` | Opaque candidate | Same exact issuer registry, object identity, tamper snapshot, request scope, service time, and expiry | Identity/scope evidence only |
| `ReauthorizationBasis` | Trusted only as the immediate issuer return | Exact concrete type and exact request scope | None; lookup input only |
| `CurrentAuthorizationStateProvider` | Trusted configured adapter boundary | Must expose `load_current_state`; every exception fails closed | None |
| `CurrentAuthorizationState` | Trusted adapter data, not a grant | Exact class/types, canonical snapshots, required workspace, bounded capabilities, identity/scope/revision/time match | None |
| `Member` / `RevocationSnapshot` | Current-state inputs | Exact canonical snapshots and exact tenant/subject relationships; authorizer rechecks policy and freshness | None by construction |
| `VerifiedCapability` | Verification cache hint | Exact bounded tuple; `TenantAuthorizer` re-verifies each envelope in its own trust domain | None by construction |
| `TenantAuthorizer` | Exact configured policy engine | Exact concrete instance; exceptions fail closed; returned decision is canonically rebuilt | Decision only |
| `AuthorizationDecision` | Policy result | Exact request equality, service-time bound, and explicit `AuthorizationOutcome.ALLOW` | Eligible for local issuance or final consume only inside the current call |
| `AuthorizedOperation` | Composer-owned preliminary authority | Exact registered object, complete snapshot match, actor/request match, fresh provider/policy re-evaluation, final context check, TTL, one-time consume | Permission to enter a future effect boundary once |

The current-state provider is trusted configuration, but its output is never accepted by
shape alone. A production adapter must load authoritative state. The test suite supplies
only fakes and deliberately malicious fakes; this repository does not yet contain a real
IdP, session, directory, membership, workspace-policy, or capability-source adapter.

## 3. Current-state provider contract

`CurrentAuthorizationStateProvider.load_current_state(basis, request)` receives a basis
that the same issuer just prepared and a canonical snapshot of the concrete request. The
provider is called during issuance and again during every consume attempt that passes token
preflight. It must return one exact `CurrentAuthorizationState` containing:

- context identifier, configured authenticator identifier, and audience;
- exact request identifier, provider principal, mapped subject, tenant, and required
  workspace;
- current identity revision and current scope/membership-policy revision;
- a provider-owned observation timestamp;
- the current member, or `None` when no current membership exists;
- a complete exact-tenant revocation snapshot; and
- at most 64 exact `VerifiedCapability` values.

The composer compares every identity/scope field with the issuer basis. Identity and scope
revisions are compared exactly, with distinct stable stale-revision failures. A missing
workspace is rejected; `None` is never treated as a tenant-wide wildcard. The observation
must not be older than `max_state_age` and must not be farther in the future than
`max_clock_skew` relative to the composer clock.

A real provider must additionally satisfy requirements that this port cannot prove:

1. Query with an exact `(tenant_id, workspace_id, subject_id)` composite key. Do not query
   by a globally reused subject, session, resource, or workspace string.
2. Derive principal-to-subject mapping from the authenticated provider, not request body,
   model output, forwarded header, or chat metadata.
3. Read membership status, role bindings, identity revision, scope revision, and
   revocation state from authoritative stores with one documented consistency model.
4. Use provider/service observation time. Never copy a client timestamp into
   `observed_at`.
5. Return a complete bounded snapshot or fail. Never truncate roles, revocations, or
   capabilities to make a request fit.
6. Fail closed on timeout, partial read, replica-lag uncertainty, revision conflict,
   malformed data, dependency outage, or cancellation before a complete snapshot exists.
7. Keep credentials, raw assertions, directory responses, and tenant identifiers out of
   exception messages, trace attributes, metrics labels, and ordinary logs.
8. Provide contract, consistency, failover, freshness, and tenant-collision tests before
   being considered for a protected runtime.

Echoing fields from `ReauthorizationBasis` proves only that an adapter can copy data. The
fake provider in tests does that intentionally and is not a production implementation.

## 4. Exact actor, tenant, action, and resource binding

An issued handle records an immutable registry snapshot of:

- random operation identifier;
- request-context identifier, authenticator identifier, and audience;
- request identifier, provider principal, and application subject;
- exact tenant and exact required workspace;
- exact concrete action, resource type, and resource identifier;
- canonical authorization decision digest;
- exact identity and scope revisions; and
- service-owned issuance and expiry times.

The handle exposes only its non-authorizing correlation identifier and issuance/expiry
times. Its tenant, workspace, actor, action, resource, decision, and revision fields are not
public properties and never appear in `str` or `repr`.

Consumption requires the original issuer to validate a live `RequestContext`, both before
the current-state lookup and immediately after the action-time policy decision. A context
retired while the provider or authorizer runs cannot complete consumption. A new context
with identical request, subject, tenant, workspace, and revision strings still has a
different context identifier and cannot consume the handle. A handle for tenant A cannot
be used for tenant B even when request ID, subject ID, workspace string, resource type, and
resource ID collide exactly. Action, resource type, and resource ID substitutions also
fail.

The decision digest is bound into the internal snapshot, but it is not a portable receipt
or signature. An audit or effect system must not accept a digest string in place of the
live handle.

## 5. Opaque handle and replay boundary

`AuthorizedOperation` is intentionally process-local:

- its public constructor rejects caller construction;
- only the issuing composer registry accepts the exact object identity;
- a different composer rejects it even with identical configuration;
- reflection that changes any stored field is detected and quarantines the handle;
- `copy`, `deepcopy`, and pickle serialization are rejected;
- handles expire after the minimum of `operation_ttl`, request-context expiry, and
  current-state freshness expiry;
- active handles have a hard configured capacity and dead/expired entries are pruned;
- `retire` invalidates one handle and `close` invalidates all handles; and
- successful `consume` reloads and reauthorizes current state, then compares and removes
  the handle under the same registry lock;
- the composer and registry require the exact creator PID and fork epoch before touching
  an inherited lock or registry record; and
- the canonical issuer independently requires its creator PID and request-context process
  epoch before issue, reauthorization preparation, retirement, close, or context entry.

The operation ID is a correlation value, not the authority. Reconstructing or replaying an
ID cannot reconstruct the registered Python object. The registry uses object identity plus
a complete immutable snapshot, so object-ID reuse after garbage collection does not match
an earlier weak reference.

This is not a cryptographic bearer token and must never be serialized into HTTP, a queue,
an event, an artifact, a database field, or a cross-process RPC. A process restart or new
composer invalidates every outstanding handle. Arbitrary code already executing in the
trusted Python process can inspect private module state, monkey-patch classes, or use
reflection; Python privacy is not a sandbox. Untrusted plugins and effect workers require
process isolation and a separately reviewed protocol.

### 5.1 Fork, spawn, and prefork boundary

POSIX `fork` copies Python memory, including an issuer and its contexts, an active operation
handle, registry tables, closed flags, clock high-water marks, and lock objects. Without an
explicit process fence, a child could issue or refresh contexts with inherited trusted
dependencies, build a new composer around that issuer, or let parent and child each consume
their private copy of one nominally one-time handle. The request-context and operation
modules therefore register lock-free `os.register_at_fork(after_in_child=...)` callbacks
when the platform provides them. Each callback replaces its module process epoch and
records the child PID without acquiring any application lock. Every issuer, composer, and
registry public path that could reach a lock or authorization state compares both values
first. Inherited issuer calls return `request_context_process_mismatch`; composition and
operation calls return `protected_operation_process_mismatch`.

Platforms without `register_at_fork`, or where hook registration is unavailable, use an
independent safe fallback: every check samples `os.getpid()` and lazily replaces the module
epoch when the PID differs. The inherited object still carries the parent PID and parent
epoch, so it remains rejected. This fallback also covers a fork mechanism that bypasses
Python's registered hook. The check intentionally requires no lock; a child therefore
fails immediately even if another parent thread owned the composer or registry lock at the
instant of fork.

`spawn` and `forkserver` do not create a transfer mechanism. `RequestContextIssuer`,
`AuthorizedOperation`, `ProtectedOperationComposer`, and the registry reject copy, deep
copy, and pickle; real multiprocessing start attempts with the operation handle, composer,
or registry fail serialization. Do not add a custom reducer, manager proxy, inherited
global, forkserver preload, or IPC wrapper for these objects.

Prefork deployment has one mandatory construction order:

1. The master process must not create an issuer, provider, authorizer, composer, context,
   or handle for worker use.
2. Fork workers first.
3. Inside each worker, independently create the complete issuer/provider/authorizer/clock/
   composer composition root and authenticate new request contexts.
4. Drain and discard every worker-local handle before that worker exits or reloads.

Creating only a new composer around a prefork-inherited issuer is actively rejected before
the inherited provider or authorizer can be reached. Providers and authorizers are still
outside this primitive's process fence and must also be constructed worker-locally. A
forked child cannot "adopt," close, retire, or migrate inherited contexts or handles. The
parent remains usable and retains its original registry and one-time semantics.

This fence invalidates inherited authority objects; it does not erase bytes already copied
into the child address space. Credential-bearing, signing, connector, or untrusted-plugin
workers must establish a `spawn`/`exec` topology before loading secrets or capabilities, or
fetch them afterward from a separately reviewed broker. A forked child that inherited key
or credential material is not a security sandbox merely because issuer/composer calls now
fail closed.

### 5.2 Important effect-atomicity limitation

Fresh action-time authorization and one-time local consumption prevent use after an
observed membership/revision/revocation change and prevent a second successful composer
consume. They do **not** make the provider read, final context check, registry consume,
repository transaction, and external side effect one atomic operation:

```text
consume succeeds -> process crashes -> effect may not start
consume succeeds -> effect starts -> process crashes -> effect outcome may be unknown
```

There is no repository call in this slice, no durable operation ledger, no idempotency key
store, no command receipt, and no recovery reconciliation. Calling a real effect after
`consume` would therefore create an unclosed crash gap. Until a durable transactional or
idempotent receipt boundary is designed and tested, this composition is permitted only
with fake, no-op, or read-only effect adapters.

State may also change immediately after the final provider read. A real adapter and effect
boundary need a durable revision predicate, lock/fence, transactional authorization check,
or equivalent design that proves the state used by the decision still governs the effect.
The current double evaluation is fail-closed against changes it observes; it is not a
cross-store serializable transaction.

## 6. Time, capacity, and concurrency semantics

The composer registry owns a service clock with a monotonic process-local high-water mark.
A rollback within configured skew freezes logical time at the previous observation; a
larger rollback fails closed. Clock exceptions and non-aware timestamps become stable
redacted errors. Context preparation uses the issuer's independent high-water, and the
authorizer uses its configured service clock.

Before both issuance evaluation and action-time consume evaluation, the composer rejects a
basis prepared too far in the future or a context already expired against composer time.
It rejects a stale/future provider observation. After each policy evaluation, it resamples
time, rechecks context expiry and provider-state age, and requires the canonical decision
timestamp to be within configured clock skew. Issuance resamples the registry clock while
holding the registry lock and refuses a non-positive or overlong expiry.

Capacity is a hard count of live registered handles. Concurrent issuers cannot exceed it:
the registry checks capacity, allocates the random ID, constructs the handle, and registers
the snapshot under one reentrant lock. The expensive provider and policy calls occur before
that lock and may still consume compute during saturation. Production still needs admission
rate limits, per-tenant quotas, dependency concurrency budgets, and overload metrics.

Two concurrent consumers may both prepare a valid context, reload state, and receive an
`ALLOW` before reaching the registry. The final compare-and-remove step is serialized, so
one succeeds and the other receives a stable untrusted-handle failure. This guarantee is
local to one composer instance and process.

## 7. Failure and redaction contract

Public composition failures use `OperationAuthorizationError` with one bounded code. Raw
provider, authorizer, clock, validation, context, and registry exceptions are never
returned. Each potentially faulting call runs inside a narrow inner boundary. That boundary
classifies only exact built-in control-signal types, obtains the interpreter-owned
traceback through `sys.exc_info`, clears completed callback/dependency frames, and returns
only a trusted descriptor. It never reads or writes the caught object's `args`, notes,
custom attributes, cause, context, or traceback attributes. The outer public method deletes
actor/request/handle arguments and raises a fresh allow-listed code-only exception.
Provider/authorizer exception graphs and their frame locals are therefore not reachable
through the public failure, even when a hostile exception rejects attribute reads or
writes by throwing another secret-bearing exception.

`raise ... from None` only suppresses presentation of Python's implicit exception chain;
it does not make the programmatically readable `__context__` field `None`. Every public
registry/composer rethrow therefore raises its fresh exception inside the same public
frame, catches that exact fresh object, clears `__context__`, and uses a bare re-raise. This
also applies to constructor process rejection and context-manager lifecycle failure. A
failure raised while the caller is already handling another exception—including a real
`with composer:` body exception—cannot retain that caller exception, its request, provider,
authorizer, key ring, handle, or other attached state. Completed internal frames are
cleared, while the remaining library traceback contains only the public entry frame (and
the bounded control-signal helper for a reissued signal).

Configured dependencies are treated as hostile exception boundaries, including custom
classes that inherit directly from `BaseException`. A non-control `BaseException` becomes
the same stable fail-closed category as an ordinary dependency exception. Exact
`KeyboardInterrupt`, `SystemExit`, `GeneratorExit`, and `asyncio.CancelledError` retain
control-flow semantics, but the original third-party object and all of its message,
argument, note, custom-attribute, cause/context, and frame-local state stay behind the
boundary. The public method raises a different exact signal with no cause/context chain.
Fresh `KeyboardInterrupt`, `GeneratorExit`, and `CancelledError` signals have empty
arguments. Fresh `SystemExit` preserves only a bounded non-secret status: `None`, exact
`bool`, or an exact `int` from 0 through 255; negative/out-of-range integers, strings,
objects, and integer subclasses become exact status `1`. A subclass merely shaped like one
of these control signals is treated as a hostile dependency failure. This policy applies
to constructor and registry/composer authorization and lifecycle boundaries, preventing a
dependency from smuggling tenant or credential material through a control-shaped exception
while preserving async-worker cancellation and ordinary bounded exit status.

Composer and registry constructors statically bind their exact class initializer before
entering the boundary; neither resolves `_initialize` through the supplied instance. Only an
exact `ProtectedOperationComposer` and exact internal registry may initialize. A subclass or
other receiver fails closed inside the boundary rather than gaining a virtual descriptor path
before containment.

Inside that boundary, provider `load_current_state` and clock `now` lookup deliberately uses
attribute access without a default. A missing attribute and an `AttributeError` raised by a
descriptor, proxy, or `__getattribute__` therefore both enter the same inner dependency
boundary, have their completed frames cleared, and map to
`protected_operation_state_unavailable` or `protected_operation_clock_unavailable`. A
non-callable result maps to the same dependency-specific code. An inherited issuer retains
the distinct `protected_operation_process_mismatch` code; other invalid constructor state
without an already supported stable code becomes `protected_operation_internal_failure`.

All fallible validation, registry creation, and lock creation complete in locals before the
exact object's slots are published. If any of them fails, an externally retained object made
with `object.__new__` remains uninitialized rather than retaining a clock, registry, issuer,
provider, or authorizer. Before the public construction failure escapes, the constructor also
deletes every supplied dependency and configuration argument from its public frame. Exact
control signals are reissued through the bounded policy above; other failures become fresh
code-only errors.

Representative codes are grouped below. Callers must treat every code as denial and must
not retry an irreversible effect without a new reviewed idempotency policy.

| Category | Stable codes |
|---|---|
| Context/request | `protected_operation_context_rejected`, `protected_operation_request_invalid`, `protected_operation_workspace_required`, `protected_operation_context_time_invalid`, `protected_operation_context_expired` |
| Current state | `protected_operation_state_unavailable`, `protected_operation_state_invalid`, `protected_operation_state_mismatch`, `protected_operation_identity_revision_stale`, `protected_operation_scope_revision_stale`, `protected_operation_state_time_invalid`, `protected_operation_state_stale` |
| Policy | `protected_operation_authorizer_failed`, `protected_operation_decision_invalid`, `protected_operation_decision_time_invalid`, `protected_operation_denied` |
| Handle | `protected_operation_untrusted`, `protected_operation_tampered`, `protected_operation_scope_mismatch`, `protected_operation_expired`, `protected_operation_expiry_invalid`, `protected_operation_capacity_exceeded` |
| Lifecycle/time | `protected_operation_composer_closed`, `protected_operation_registry_closed`, `protected_operation_process_mismatch`, `protected_operation_clock_unavailable`, `protected_operation_time_regressed` |
| Containment | `protected_operation_binding_invalid`, `protected_operation_id_unavailable`, `protected_operation_internal_failure` |

Error strings, representations, logs, and metrics must not include tenant, workspace,
subject, principal, revisions, raw state, decision evidence, capabilities, credentials, or
provider exception text. Codes are suitable for bounded metrics; identifiers are not.

## 8. Reference use for tests and future composition

The following shape demonstrates the enforced order. `provider` is intentionally omitted:
there is no reviewed production adapter in this repository.

```python
from datetime import timedelta

from quantum_entanglement.operation_authorization import ProtectedOperationComposer


composer = ProtectedOperationComposer(
    issuer=issuer,
    state_provider=provider,  # reviewed authoritative adapter required
    authorizer=authorizer,
    clock=service_clock,
    operation_ttl=timedelta(seconds=10),
    max_state_age=timedelta(seconds=15),
    max_clock_skew=timedelta(seconds=2),
    max_active_operations=1_000,
)

operation = composer.authorize(request_context, access_request)

# Fake/no-op/read-only adapter only in the current gate state. A future effect adapter
# must close the durable consume/effect/receipt crash gap before enabling writes.
composer.consume(operation, request_context, access_request)
fake_effect_adapter.execute(access_request)
```

The composition root owns issuer, provider, authorizer, clock, and composer lifetimes. On
shutdown, stop admission and effects first, fence/drain callers, close the composer to
invalidate outstanding operations, then close the issuer. Recreating either object is an
authorization reset and must not be used to bypass a clock or integrity alarm.

For prefork or worker-reload hosts, perform that construction and shutdown sequence wholly
inside each worker. Never initialize the composition root in a master and inherit it into
workers. `protected_operation_process_mismatch` is a non-retryable local-object error:
discard the inherited object and rebuild the entire composition root in the current
process; never catch the code and retry the same handle.

## 9. Threat and residual-risk matrix

| Threat | Implemented behavior | Residual requirement |
|---|---|---|
| Caller fabricates request identity/scope | Same issuer validates exact request/subject/tenant/workspace | Real authenticated transport and principal mapping |
| Caller constructs state/basis/decision | Values remain non-authorizing; only composer registry issues handles | Keep effect entry points inaccessible without the composer |
| Provider is unavailable or throws a secret-bearing chain | Stable denial; chain and internal traceback frames detached | Alerting, timeout, circuit, retry, and provider SLO |
| Provider returns another tenant/workspace or stale revision | Exact comparison and distinct fail-closed codes | Strong-consistency/freshness proof from real stores |
| Membership, identity/scope revision, or capability revocation changes after issuance | `consume` reloads state and re-runs exact authorizer; observed downgrade/revision/revocation denies before consume | Atomic revision/fence with the future effect transaction |
| Same IDs collide across tenants | Tenant and workspace are part of issuance and consume scope | Tenant/workspace columns and predicates in every repository |
| Handle is forged, copied, pickled, moved, or mutated | Exact local registry identity, copy/pickle rejection, snapshot quarantine | Process isolation against arbitrary trusted-host code |
| Handle is replayed serially or concurrently | Successful consume atomically removes it; later consume fails | Durable distributed replay ledger for multi-process effects |
| Parent and forked child reuse an issuer or consume a copied handle | Independent request-context and operation PID/epoch guards reject inherited issuer/composer/registry paths before copied locks/state; a new composer cannot adopt the issuer; parent remains usable and one-time | Construct the complete composition root independently inside each worker |
| Context is replaced by a new context for the same actor | Exact context ID/principal/revision snapshot fails | Durable session/revocation semantics if cross-process is required |
| Service clock rolls backward | Local high-water freezes within skew and fails beyond it | Trusted time monitoring and incident runbook |
| Dependency throws a mutation-hostile `BaseException` or control-shaped secret | Raw exception attrs are never touched; non-control failures become stable denial; exact four-way control signals are replaced with fresh empty-argument signals | Cancellation/termination integration tests in the real service host |
| Process crashes between consume and effect/receipt | No permissive recovery; handle is process-local | Transactional effect receipt or durable idempotency/reconciliation |
| Registry is exhausted | Hard capacity; no over-issuance under concurrency | Per-tenant quotas, rate limits, load shedding, capacity evidence |
| Arbitrary Python code runs in process | Explicitly outside this primitive's isolation claim | Sandboxed workers/plugins and least-privilege deployment |

## 10. Compatibility, rollback, and operations

This slice is additive. It adds one module and dedicated tests; it changes no schema,
runtime path, repository, connector, CLI, protocol, or package-root export. Existing direct
callers continue to work but remain outside the protected composition and gain no new
production claim.

Rejecting an issuer, composer, or registry outside its creator process is an intentional
fail-closed compatibility tightening. Any prefork deployment that previously initialized
these objects in its master must move the complete composition root into the worker. There
is no compatibility mode for inherited contexts or handles.

Composer and internal registry subclass initialization is also intentionally unsupported.
Composition roots must construct the exact reviewed classes; extension belongs in the
provider and future effect-adapter ports, not in subclass overrides of the security boundary.

Rollback is code-only because no persistent state is written. Stop the candidate process,
close composer/issuer instances, discard all outstanding process-local handles, and deploy
the prior tree. Never try to preserve, pickle, migrate, or grandfather a handle across
rollback or restart.

Any attempt to connect this module to a write repository or external connector is a new
security behavior. It requires its own small commit, threat review, exact tenant/workspace
repository tests, idempotency/crash tests, operator documentation, and release evidence.
Nothing in this document authorizes a Feishu or WeCom send.

## 11. Verification

Run the dedicated boundary and adjacent security regression tests:

```bash
PYTHONPATH=src python -m pytest -q \
  tests/test_operation_authorization.py \
  tests/test_request_context.py \
  tests/test_tenancy.py
ruff check src tests scripts
ruff format --check src tests scripts
MYPYPATH=src mypy --strict src
python -m compileall -q src tests scripts examples
git diff --check
```

### 11.1 Observed local constructor-hardening evidence

At clean checkpoint `76b24d767d28e9a8db5287a5c340a2c8b3546ebe` (tree
`067c1f13049cf183505a51f4f5733b297a406273`) on 2026-08-21, the following local synthetic
checks passed:

| Check | Observed result |
|---|---|
| Constructor regression | Raw provider/clock descriptor faults reproduced before the fix; committed adversarial cases pass after `e79212b` |
| Authorization target | 148 tests passed independently on CPython 3.9.6, 3.12.12, and 3.13.9 |
| Repository suite | 887 tests passed independently on CPython 3.9.6, 3.12.12, and 3.13.9 |
| Static source gates | Locked Ruff 0.16.3 lint/format and strict mypy 1.19.1 over 35 source files passed |
| Supply-chain baseline | Four dependency-lock targets and 74 exact package records verified |
| Parse and smoke | Python 3.9 `compileall`, the deterministic 25-event/3-artifact demo, and `git diff --check` passed |

The adversarial constructor cases cover provider and clock lookup through
`__getattribute__`, a mutation-hostile custom `BaseException`, exact `KeyboardInterrupt`,
`SystemExit`, `GeneratorExit`, and `asyncio.CancelledError`, unsafe exit-status collapse,
active caller exceptions, constructor argument deletion, completed-frame clearing, normal
dependency identity, and default denial. The CPython 3.12/3.13 fork deprecation warning is
an interpreter warning about multithreaded POSIX `fork`; the tested child still failed
closed and the parent remained usable.

These results are a same-host development checkpoint, not retained release evidence,
clean-host proof, production adapter evidence, or Gate A promotion. The blockers in
section 12 remain unchanged.

The dedicated suite covers exact state types/snapshots, redacted representations,
structural provider injection, hostile provider/clock descriptor lookup during construction,
descriptor-raised `AttributeError`, static initializer binding, exact subclass rejection,
zero-slot lock-failure rollback, constructor argument/frame detachment, workspace requirements,
same-ID cross-tenant isolation, every actor/scope/revision substitution, provider observation
freshness/future time,
provider and authorizer exception-chain/frame detachment, hostile `BaseException` and safe
control-signal replacement across construction, authorization, and lifecycle boundaries,
the complete safe `SystemExit` status matrix, exact `asyncio.CancelledError` propagation,
fail-closed control subclasses, explicit deny,
malformed decisions, membership downgrade, post-issuance identity/scope revision change,
post-issuance capability revocation, context retirement during refresh, every final
reauthorization binding-field drift, foreign issuer/composer, direct construction,
forgery, reflective tampering, copy/deepcopy/pickle, operation and composer lifecycle,
expiry, service-clock rollback, hard concurrent issuance capacity, serial replay,
concurrent replay, action-time reauthorization, and exact one-time consumption.

Process-mismatch tests inspect every public composer and registry traceback after a failure
raised inside an already active secret-bearing caller exception. They prove that the public
constructor failure cannot retain its issuer/provider/authorizer/key ring, that all public
wrappers delete request/context/handle/lifecycle arguments, and that `__cause__`, `__context__`,
notes, dynamic attributes, and completed internal locals are detached. Separate construction
tests prove that lock failure leaves an externally retained exact object with no initialized
slots. Lifecycle tests
exercise constructor, composer close/enter/exit, and registry close with all four exact
control signals; the exit path uses an actual context-manager body exception rather than a
synthetic direct call.

The suite also runs real POSIX-fork probes: child issue/prepare/retire/close/enter against an
inherited issuer, construction of a new composer from that issuer, parent/child
double-consume, every inherited composer and registry public path including composer exit,
forks while other threads own issuer or operation locks, authorization-time issuer
revalidation, and exact parent usability afterward. Actual `spawn` and `forkserver` process
starts prove that
handle, composer, and registry transfer is rejected. Separate fallback tests prove PID
drift refreshes both module epochs when no at-fork callback is available.

Test counts are observations, never promotion evidence. A release candidate must run the
complete baseline in `RELEASE_GATES.md` on the exact clean source tree and retain the
required evidence.

## 12. Why Gate A remains closed

This slice is a composition foundation, not a production authorization system. Gate A
remains closed because at least these blockers remain:

1. No real authenticator, authenticated API/session boundary, IdP adapter, or authoritative
   current-state provider exists.
2. Current-state reads have no proven cross-store snapshot/consistency contract and no
   production freshness/failover evidence.
3. The composer is not mandatory at runtime, and no repository/effect adapter requires an
   `AuthorizedOperation`.
4. Events, snapshots, inbox, outbox, attempts, projections, artifacts, approvals, and
   recovery paths do not yet all prove exact tenant/workspace repository isolation and
   legacy migration/rollback rehearsal.
5. One-time process-local consume is not durable and is not atomic with an effect or
   receipt. Crash recovery, idempotency, and reconciliation are unimplemented.
6. Outstanding handles cannot cross processes or restarts; no distributed replay CAS or
   durable authorization ledger exists.
7. Test composition uses in-memory/fake components and does not prove production key,
   revision-guard, time, directory, database, quota, monitoring, or incident behavior.
8. Arbitrary in-process code is outside the isolation model; worker/plugin sandboxing and
   least privilege remain open.
9. No retained clean-host, migration, fault-injection, performance, security-review, or
   promotion evidence covers this new boundary.
10. Real prefork service integration has not yet proved worker-local construction,
    graceful reload/drain, cancellation, and monitoring of process-mismatch failures.

Until those items are closed with independently reviewable commits and exact evidence, the
maximum permitted use remains local/offline development with synthetic data and fake,
no-op, or read-only adapters. Passing tests must not be described as production readiness.
