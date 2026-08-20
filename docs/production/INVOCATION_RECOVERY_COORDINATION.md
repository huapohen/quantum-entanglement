# Invocation-attempt recovery coordination

Status: fail-closed coordination foundation; **not an invocation worker or an exactly-once
effect guarantee**

This document defines the minimum safe contract for reconciling a workflow task whose
durable event projection ends in `RUNNING` with the mutable durable invocation job owned by
`SQLiteInvocationAttemptStore`. The first implementation is deliberately a side-effect-free
decision boundary. It must land before the orchestrator is allowed to enqueue, claim, retry,
or project attempt state during session recovery.

## Why this boundary exists

The workflow event stream and the invocation-attempt store answer different questions:

- the event stream records the accepted workflow and its durable business projection;
- the attempt store records which worker may execute one logical invocation now;
- a result receipt records that an invocation result crossed the business commit boundary.

None of these records can stand in for another. In particular:

- `RUNNING` does not prove that a job exists;
- an expired lease does not prove that the old worker produced no external effect;
- `InvocationStatus.SUCCEEDED` does not prove that artifacts and `task.result.received` were
  durably committed;
- `result_ref` is a reference, not proof that the referenced receipt exists and is bound to
  the same invocation;
- an in-process `AgentRuntimePort` call cannot be described as exactly once when the Agent or
  connector may perform an unfenced external side effect.

The coordinator therefore returns a decision. It does not invoke an Agent, recover a lease,
append a task transition, accept an artifact, or edit either durable source.

## Trusted inputs and binding

Recovery must begin with an immutable expected invocation binding derived from durable,
canonical workflow and invocation-start evidence. The binding contains:

| Field | Required relationship |
|---|---|
| `invocation_id` | exactly identifies this logical invocation and must never be regenerated during recovery |
| `session_id` | equals the recovered workflow session |
| `plan_id` | equals the exact recovered plan, not merely the current session |
| `task_id` | equals the task whose projection is `RUNNING` |
| `agent_id` | equals the immutable task assignment |
| `idempotency_key` | equals the invocation boundary accepted for that task |
| `payload_digest` | equals the canonical digest committed before execution |

The payload digest must cover every execution-relevant immutable input, including the task,
coordination envelope, context digest, and invocation schema version. The recovery coordinator
accepts the already committed digest; it must not silently reconstruct a changed payload from
ambient state, current plugin configuration, or a newly compiled context.

The job must match every field byte for byte. Matching only `session_id`/`task_id`, or looking
up the first job for a task, is insufficient because a stale plan, changed Agent, or reused
idempotency key could otherwise be attached to different work. Malformed types, impossible
counter relationships, contradictory lease fields, and a result reference on a non-successful
job are integrity failures, not recoverable status cases.

## Durable result receipt

A completion-capable receipt must be durable and bind at least:

- the complete invocation binding above;
- the exact `result_ref` stored by the fenced successful attempt;
- the attempt number and lease epoch that committed the result;
- the artifact/result manifest digest and the durable event position or equivalent receipt
  identity.

The worker contract in `DURABLE_INVOCATION_ATTEMPTS.md` intentionally accepts this receipt
*before* executing the terminal attempt CAS. A crash can therefore leave an exact receipt
beside a `RUNNING` job; later lease recovery can leave that same receipt beside a `QUEUED` or
`FAILED` job. Those are reconciliation states, not automatically integrity failures. Only a
design that commits receipt, artifact/result state, and attempt success in one proven atomic
transaction could declare those combinations impossible.

The current `task.result.received` event does not carry the full attempt identity above.
Therefore the existing event schema cannot be treated as a completion-capable invocation
receipt. A future event/schema migration and receipt-bound reconciliation CAS must be
implemented and fault-injected before automatic completion projection is enabled.

## State matrix for a `RUNNING` task

Every row assumes the job passed complete identity, payload, and structural validation. A
binding or integrity mismatch fails before this matrix is consulted.

| Durable job observation | Receipt observation | Coordination decision | Permitted next actor | Forbidden behavior |
|---|---|---|---|---|
| missing | absent | `BLOCKED_MISSING_JOB` | operator/recovery repair workflow | regenerate a job, call the Agent, or infer that no effect occurred |
| missing | present | integrity/partial-restore failure | operator after preserving both sources | ignore the orphan receipt or synthesize its missing job |
| `QUEUED`, zero attempts | absent | `FIRST_CLAIM_READY` | durable worker using the store CAS | direct orchestrator invocation or duplicate enqueue |
| `QUEUED`, one or more attempts | absent | `BLOCKED_EFFECT_UNKNOWN` | receipt/effect reconciliation workflow | automatically retry without durable retry-safety proof |
| `RUNNING` | absent | `WAITING_ACTIVE_LEASE` | current fenced worker; later, store-owned expiry reconciliation | steal the lease or accept a stale worker result |
| `RUNNING` | exact receipt for current attempt | `RESULT_ACCEPTED_PENDING_ATTEMPT_CAS` | receipt-bound attempt reconciler | invoke the Agent again or discard the accepted result |
| `SUCCEEDED` with no `result_ref` | absent | `BLOCKED_RESULT_UNCOMMITTED` | operator/reconciliation workflow | project `COMPLETED` or retry the Agent |
| `SUCCEEDED` with `result_ref` | missing receipt | `BLOCKED_RESULT_UNCOMMITTED` | receipt reconciler | treat the reference itself as a receipt |
| `SUCCEEDED` with `result_ref` | exact completion-capable receipt | `COMPLETION_READY` | future idempotent result projector | bypass artifact integrity, causality, or transition checks |
| `FAILED` | absent | `TERMINAL_FAILURE_EFFECT_UNKNOWN` | failure/effect reconciliation workflow | assume failure proves that no external effect occurred |
| `QUEUED` or `FAILED` | exact receipt for the latest attempt | `RESULT_ACCEPTED_JOB_DIVERGED` | receipt-bound attempt reconciler | invoke the Agent again or erase the receipt |
| `CANCELED` | any | unsupported/integrity failure in the current API | future authorized cancellation reconciler | project cancellation without a durable authorized receipt |

A receipt whose invocation binding, result reference, attempt ID/number, lease epoch, manifest,
or durable position differs is an integrity failure. Receipt presence on a first-claim queued
job is also contradictory because no attempt exists to own it.

`FIRST_CLAIM_READY` means only that the existing durable job has never been attempted and is
eligible for the attempt-store claim protocol. A queued retry is not equivalent: the prior
worker may have performed an effect before losing its lease. A future retry-safety proof may
allow retry only when it durably establishes that the operation is pure, downstream-fenced,
or receiver-idempotent for the exact invocation key.

`WAITING_ACTIVE_LEASE` does not itself evaluate wall-clock expiry. Expiry must use a locked
attempt-store transition so epoch fencing cannot be bypassed, but expiry alone is not retry
authorization. The current store automatically requeues expired work and `claim()` can recover
and immediately reclaim it in one transaction. That behavior is an unresolved P0 for work
whose external effect is not proven retry-safe; runtime integration must not call that path
until it is gated by effect reconciliation or a durable retry-safety classification.

## Threat matrix

| Threat/failure window | Unsafe interpretation | Required fail-closed response |
|---|---|---|
| task `RUNNING` committed, process dies before durable enqueue | missing job means safe retry | block as effect/job unknown; never synthesize identity during recovery |
| job enqueued, process dies before first claim | call Agent from recovery | permit only the first store-owned claim; do not create another job |
| worker performs effect, dies before receipt | expired lease proves no effect | fence stale ownership and enter effect-unknown reconciliation; do not auto-retry |
| result receipt commits, process dies before attempt CAS | receipt beside `RUNNING` is corrupt | reconcile the exact current attempt from its accepted result without Agent reinvocation |
| expiry recovery races an already accepted receipt | queued/failed means receipt can be ignored | return job-diverged reconciliation; receipt wins only after exact attempt/manifest validation |
| attempt success commits, receipt is absent/missing | success equals task completion | block; investigate forbidden ordering or partial restore |
| result event commits, task completion transition does not | replay Agent to finish | idempotently project only from a completion-capable receipt after artifact validation |
| stale worker publishes after lease recovery | latest response wins | downstream write and receipt must compare the lease epoch/token; reject stale ownership |
| same task ID appears under a changed plan | task lookup is sufficient | compare session, plan, task, Agent, invocation ID, idempotency key, and digest |
| persisted job row is forged or partially corrupt | coerce values and continue | raise a stable integrity error without a state change |
| two jobs exist for one task | choose the newest/first row | fail integrity; do not guess which invocation owns the workflow task |
| result reference points to missing/wrong receipt | non-empty reference is enough | block or fail integrity; dereference and validate exact receipt identity |
| coordinator closes a caller-owned store | convenient cleanup | close only a store explicitly marked as coordinator-owned |
| coordinator is used after close | reopen implicitly | reject deterministically; lifecycle state must be monotonic |

## Optional store lifecycle

The decision layer may be constructed without an attempt store. In that mode it can validate
an explicit immutable observation, but a `RUNNING` task with no supplied job must remain
blocked. When a store is supplied:

- ownership is explicit and defaults to caller-owned;
- `close()` is idempotent;
- only an explicitly coordinator-owned store is closed;
- a caller-owned store remains usable after coordinator shutdown;
- all coordinator operations after shutdown fail deterministically;
- context-manager exit follows the same ownership rule;
- store exceptions and integrity errors propagate as fail-closed errors, never as a missing
  job;
- the job, current/latest attempt, attempt count, and lease-token digest are read in one
  bounded database snapshot so recovery never combines rows from different epochs.

The runtime must not acquire a second implicit in-memory store. An in-memory store is not a
durable recovery source across processes or restarts.

## Integration sequence

The safe delivery order is:

1. land a bounded single-transaction job/attempt recovery snapshot;
2. land the pure binding, shape, receipt, and corrected state decision model with adversarial
   tests;
3. add a read-only coordinator wrapper with explicit optional-store ownership;
4. define and migrate the invocation-start/result-receipt event schemas;
5. atomically bind enqueue acceptance to the durable start record, or make their split-brain
   state explicitly repairable without re-execution;
6. implement a worker that claims, heartbeats, fences downstream resources, and publishes a
   completion-capable result receipt;
7. implement receipt-bound attempt reconciliation plus idempotent business projection from
   verified receipt to artifacts, result, and task status;
8. run process-kill fault injection at every boundary and retain release evidence.

Steps 1 and 2 are safe foundations. They do not authorize enabling durable attempt execution
in `OrchestratorKernel`. Skipping directly to automatic replay would turn an observable stuck
task into a possible duplicate external effect.

## Verification and release evidence

The coordination foundation requires deterministic coverage for:

- every corrected state-matrix row, including first claim versus queued retry;
- mismatched session, plan, task, Agent, invocation, idempotency key, and payload digest;
- malformed/forged job scalars and contradictory lease/terminal fields;
- receipt accepted before attempt CAS, expiry after receipt acceptance, succeeded without a
  reference, reference without a receipt, and mismatched receipt;
- stale attempt ID/number/epoch/token digest and exact receipts beside running/queued/failed;
- missing job versus store failure;
- owned and borrowed store closure, double close, and use after close;
- no mutation of either source for every decision and failure path.

Before runtime integration can be promoted, retained evidence must additionally include
process-kill tests for enqueue, claim, heartbeat, Agent return, artifact commit, result receipt,
attempt success, and task projection; stale-worker fault injection; backup/restore of both
sources; and a connector-specific proof for every externally observable effect.

## Explicit non-guarantees

This foundation does not provide:

- an invocation worker, retry-safety classifier, scheduler integration, heartbeat loop, or
  cancellation propagation;
- an atomic event-store/attempt-store/result-store transaction;
- automatic repair or projection of a recovered `RUNNING` task;
- exactly-once Agent execution or exactly-once external effects;
- a distributed lock, PostgreSQL implementation, KMS identity, or production connector;
- evidence that current direct `AgentRuntimePort` callbacks are safe to retry.

Until the remaining integration and fault-injection gates pass, a recovered task with unknown
effect state must remain blocked and visible to an operator.
