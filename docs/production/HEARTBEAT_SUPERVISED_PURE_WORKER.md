# Heartbeat-supervised pure/fake worker contract

Status: **contract frozen; dispatch remains disabled**. This document defines the narrow worker
that may be enabled only after atomic result acceptance and receipt-bound recovery are durable.
It does not authorize a model runtime, plugin, MCP server, connector, browser action, Feishu,
WeCom, webhook, or any other external effect.

The worker exists to consume the one non-replayable capability returned by
`SQLiteEventStore.claim_invocation_start(...)` without weakening the authority and crash-safety
boundaries established in [`ATOMIC_INVOCATION_START.md`](./ATOMIC_INVOCATION_START.md). A fake or
pure handler returning in memory is not completion. Completion exists only after one store-owned
transaction accepts the result and every durable terminal projection.

## Promotion invariant

The worker gate must default to off until all of the following are present in the same release:

1. an exact, versioned result manifest and result receipt;
2. one SQLite transaction that revalidates the active lease and start receipt, publishes immutable
   Artifact bytes/metadata, appends the canonical result and terminal task events, and moves the
   job and attempt to success;
3. exact replay/readback after a lost commit acknowledgement;
4. receipt-bound restart recovery that never invokes the handler again after accepted result;
5. stale-worker, timeout, cancellation, graceful-drain, process-kill and two-process race tests;
6. a composition gate that injects only an explicitly allowlisted pure or fake handler.

Until these prerequisites are met, any executable worker API must raise a stable disabled error
before it starts a heartbeat, creates a task/thread/process, calls a handler, samples handler-owned
state, or touches a connector. Tests may exercise pure validation and fake supervision primitives
only when no durable product dispatch is reachable.

## Accepted authority

The dispatch boundary accepts only an **exact** `InvocationStartClaimed`. It rejects subclasses,
`InvocationStartObserved`, serialized receipts, reconstructed leases, generic admission results,
standalone `InvocationLease` values, and caller-authored receipt objects. The boundary snapshots
the claim using library-owned codecs and revalidates all receipt/lease bindings before creating
handler work.

The caller must also provide the exact `InvocationExecutionManifest` used by canonical admission.
The worker verifies:

- its domain-separated canonical digest equals `start.evidence.manifest_digest` and the lease
  payload digest;
- invocation, session, plan, task, Agent, idempotency, envelope, context, authorization, runtime,
  correlation and causation bindings equal the schema-2 start evidence;
- `effectClass` is exactly `pure` and `retryClass` is exactly `never`;
- the start lease is attempt 1, epoch 1, and preserves the canonical single-attempt policy; and
- the configured handler revision equals the immutable `runtimeRevision`.

An effect-class label is admission evidence, not proof that arbitrary Python code is pure. The
production composition root must use a closed allowlist of reviewed handler revisions and must not
inject connector clients, credentials, filesystem mutation handles, subprocess launchers, browser
control, or network transports. A closure or plugin supplied by an Agent/user is never an allowed
pure handler.

## State machine

```text
DISABLED
  -- promotion prerequisites + explicit operator gate --> IDLE

IDLE
  -- exact claimed authority + pure manifest ----------> HEARTBEATING

HEARTBEATING
  -- handler starts -----------------------------------> RUNNING
RUNNING
  -- pure result returned -----------------------------> ACCEPTING_RESULT
ACCEPTING_RESULT
  -- atomic receipt/Artifact/attempt/task commit ------> ACCEPTED
ACCEPTING_RESULT
  -- commit ACK unknown -------------------------------> RECONCILE_ONLY

HEARTBEATING | RUNNING | ACCEPTING_RESULT
  -- heartbeat false / stale fence / expiry ----------> LEASE_LOST
  -- bounded runtime timeout --------------------------> TIMED_OUT
  -- shutdown admission stop --------------------------> DRAINING
  -- trusted cancellation -----------------------------> CANCELED
  -- sanitized handler failure ------------------------> FAILED

LEASE_LOST | TIMED_OUT | CANCELED | FAILED
  -----------------------------------------------> TERMINAL_RECONCILE_ONLY
```

`ACCEPTED` is the only success state. There is deliberately no `SUCCEEDED_IN_MEMORY` durable state.
A returned value that has not passed atomic acceptance is volatile and must disappear on crash.
After `RECONCILE_ONLY`, the handler is never reinvoked; only trusted durable receipts decide the
outcome.

## Heartbeat and timing

All durations are finite, positive, exact built-in numbers snapshotted before dispatch. Booleans,
NaN, infinity, subclasses and values that round below the clock/store precision are rejected.

Required relationships:

```text
heartbeat_interval <= lease_duration / 3
handler_timeout < lease_duration
drain_timeout <= lease_duration - handler_timeout
```

The first heartbeat must succeed before the handler receives control. Heartbeats continue through
result acceptance, not merely until the handler returns. Each successful heartbeat replaces the
worker's expected durable deadline with the store-owned returned/read-back deadline; callers never
invent time. If a heartbeat returns false or raises an ambiguity/integrity/lifecycle error, result
acceptance is permanently disabled for that local run.

The heartbeat loop and handler must not share an inherited SQLite connection. Each process creates
its stores after spawn/exec. The worker never crosses a fork boundary with a lease, connection,
thread lock, event loop, credential provider or handler graph.

## Cancellation, timeout, and drain

The handler receives a library-owned cancellation signal and immutable work snapshot. It never
receives the store, raw SQLite connection, result acceptor, connector registry or authorization
provider. Cancellation is cooperative for in-process fake/pure handlers. The worker ignores every
late return once its run leaves `RUNNING`.

On timeout or graceful shutdown:

1. stop accepting new claims;
2. signal cancellation exactly once;
3. keep heartbeating while waiting for the bounded drain window;
4. if the handler exits, route only to failure/cancellation acceptance, never success acceptance;
5. if it does not exit, abandon the local pure/fake computation and enter reconcile-only;
6. close process-local resources without serializing or logging the lease token.

Python cannot safely kill an arbitrary thread. Therefore a non-cooperative in-process handler is
permitted only while it is mechanically isolated from effects. Future model runtimes or connector
workers require a spawned subprocess protocol with OS-enforced termination and a separate durable
action-receipt state machine.

## Result acceptance boundary

The worker must call one future store API with a capability-bearing, non-serializable command. The
exact public name is deferred until the result schema is implemented; its transaction must make
the following set visible all-or-nothing:

```text
validated schema-2 start receipt
+ active invocation lease/attempt/epoch/token digest/deadline
+ immutable result manifest containing narration, metadata and a stable logical resultRef
+ zero or more immutable Artifact versions and blobs
+ optional primaryArtifactId bound to one of those versions
+ accepted-result receipt
+ canonical task.invocation.result.accepted event
+ succeeded invocation job and attempt
+ exact RUNNING -> COMPLETED task.status.changed event
+ any result-ready outbox record
```

No caller-supplied receipt is authority. The store constructs the receipt from rows and stored-event
coordinates written in the same transaction. Exact replay returns the original accepted bundle.
Conflicting replay, partial rows, stale lease, expired deadline, stream revision drift, task-scope
drift, manifest drift or Artifact drift fails closed without publishing any subset.

`resultRef` is not an Artifact ID. It remains the job/attempt's stable logical result identity even
for a narration-only result. The manifest accepts zero through 256 ordered Artifact descriptors and
an optional `primaryArtifactId`; a non-null primary ID must name exactly one descriptor. The exact
acceptance request separately binds every raw Artifact content candidate and expected head version
to its descriptor. The request digest also covers the expected event-stream version. The worker
cannot request acceptance for any effect class other than `pure` with the canonical empty action
receipt set.

A decoded manifest and a successfully constructed request remain capability-free values. The
worker and store each re-snapshot and independently revalidate the exact pure/empty-set binding; no
codec success, `is_valid` result or caller-authored value is dispatch or completion authority.

`SQLiteInvocationAttemptStore.complete(...)` is not this boundary and must not be called by the
worker. It does not validate result or Artifact evidence and can create a succeeded attempt whose
result is unrecoverable. The current `OrchestratorKernel` path that writes Artifacts, a result event
and task completion in separate transactions is also not eligible for this worker.

## Failure matrix

| Fault point | Required durable interpretation | Handler may run again? |
|---|---|---:|
| before first heartbeat | no dispatch | no local run occurred |
| first heartbeat false | stale authority; no dispatch | no |
| crash during pure handler, no accepted receipt | lease expires; reconcile as unaccepted pure work | only after a future explicit retry policy; current schema says no |
| heartbeat false while handler runs | late result is discarded | no |
| handler timeout/cancel | no success publication; terminal reconciliation required | no |
| crash before result transaction begins | no accepted result | no under current `retryClass=never` |
| crash inside result transaction before commit | rollback means no accepted result | no under current `retryClass=never` |
| commit durable, acknowledgement lost | reopen and exact-read accepted receipt | never |
| crash after accepted receipt, before caller observes it | restart projects/reports accepted result | never |
| stale worker submits after a newer fence | zero writes | never |
| receipt/Artifact/event/job/attempt partial or tampered | integrity quarantine | never |

The table is intentionally stricter than the theoretical safety of pure work. Retry remains
disabled in schema 1 because purity also depends on the trusted handler allowlist and complete
composition evidence; a label alone cannot authorize automatic replay.

## Observability and secrecy

Allowed identifiers are invocation ID, attempt ID, attempt number, lease epoch, worker ID, state,
bounded reason code, manifest digest and stored receipt coordinates. The raw lease token,
credentials, authorization material, exception objects, tracebacks and closure locals are forbidden
in repr, observability JSON, logs, metrics, events, Artifact metadata and error chains. Handler
inputs and outputs are also forbidden at those observability surfaces; their bounded canonical
bytes may exist only inside the access-controlled result manifest or Artifact blob authority.
Events and observability carry references and digests only.

Handler failures map to a closed enum of redacted reason codes. Unknown `BaseException` subclasses,
exception groups and cancellation subclasses are untrusted failures. Exact interpreter control
signals must cross only after heartbeat/handler cleanup and without retaining caller-controlled
arguments or traceback state.

## Implementation and commit order

Every step remains default-off and independently releasable:

1. exact worker configuration and authority/manifest validator;
2. disabled composition gate and negative package-surface tests;
3. pure/fake handler context, cancellation and late-result sink tests;
4. versioned result manifest/receipt codecs;
5. migration, backup/restore and schema-topology support for accepted results;
6. store-owned atomic result acceptance with statement/commit/control fault injection;
7. receipt-bound recovery and non-emitting projection;
8. heartbeat supervisor with lease-loss/timeout/cancel/drain tests;
9. process-kill and two-process result-acceptance race matrix;
10. isolated promotion commit that enables only the allowlisted fake/pure path.

No step enables real connectors. External action execution requires a later durable action receipt,
action-time authorization and `succeeded | rejected | effect_unknown` reconciliation contract.
