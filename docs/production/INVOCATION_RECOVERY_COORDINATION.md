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

The current `task.result.received` event does not carry that full attempt identity. Therefore
the existing event schema cannot be treated as a completion-capable invocation receipt. A
future event/schema migration and atomic result-commit design must be implemented and
fault-injected before automatic completion projection is enabled.

## State matrix for a `RUNNING` task

Every row assumes the job passed complete identity, payload, and structural validation. A
binding or integrity mismatch fails before this matrix is consulted.

| Durable job observation | Receipt observation | Coordination decision | Permitted next actor | Forbidden behavior |
|---|---|---|---|---|
| missing | any | `BLOCKED_MISSING_JOB` | operator/recovery repair workflow | regenerate a job, call the Agent, or infer that no effect occurred |
| `QUEUED` | absent | `RESUMABLE_QUEUED` | the durable attempt worker may claim under the store CAS | direct orchestrator invocation or creating a duplicate job |
| `RUNNING` | absent | `WAITING_ACTIVE_LEASE` | current fenced worker, or attempt-store expiry recovery after the lease deadline | stealing/replacing the lease in the coordinator or accepting a stale worker result |
| `SUCCEEDED` with no `result_ref` | absent | `BLOCKED_RESULT_UNCOMMITTED` | operator/reconciliation workflow | project `COMPLETED` or retry the Agent |
| `SUCCEEDED` with `result_ref` | absent | `BLOCKED_RESULT_UNCOMMITTED` | receipt reconciler | treat the reference itself as a receipt or project `COMPLETED` |
| `SUCCEEDED` with `result_ref` | exact completion-capable receipt | `COMPLETION_READY` | future idempotent result projector | bypass artifact integrity, causality, or task-transition checks |
| `FAILED` | absent | `TERMINAL_FAILURE` | future idempotent failure projector/operator | retry beyond the persisted policy or change the payload |
| `CANCELED` | absent | `TERMINAL_CANCELED` | future idempotent cancellation projector/operator | execute or revive the invocation |

Receipt presence for `QUEUED`, `RUNNING`, `FAILED`, or `CANCELED` contradicts the job state and
is an integrity failure. A receipt whose binding, result reference, attempt number, or lease
epoch differs is also an integrity failure.

`RESUMABLE_QUEUED` means only that the existing durable job is eligible for the attempt-store
claim protocol. It does not authorize the session recovery path to invoke the Agent directly.
`WAITING_ACTIVE_LEASE` likewise does not evaluate wall-clock expiry: expiry and requeue must use
the attempt store's locked `recover_expired` transition so epoch fencing cannot be bypassed.

## Threat matrix

| Threat/failure window | Unsafe interpretation | Required fail-closed response |
|---|---|---|
| task `RUNNING` committed, process dies before durable enqueue | missing job means safe retry | block as effect/job unknown; never synthesize identity during recovery |
| job enqueued, process dies before claim | call Agent from recovery | return queued/resumable; only the store claim protocol may grant ownership |
| worker performs effect, dies before success CAS | expired lease proves no effect | let the store fence/recover ownership, but keep external effect state explicitly unknown |
| worker succeeds attempt, process dies before business result commit | attempt success equals task completion | require an exact durable result receipt; otherwise block |
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
explicit observations, but a `RUNNING` task with no supplied job must remain blocked. When a
store is supplied:

- ownership is explicit and defaults to caller-owned;
- `close()` is idempotent;
- only an explicitly coordinator-owned store is closed;
- a caller-owned store remains usable after coordinator shutdown;
- all coordinator operations after shutdown fail deterministically;
- context-manager exit follows the same ownership rule;
- store exceptions and integrity errors propagate as fail-closed errors, never as a missing
  job.

The runtime must not acquire a second implicit in-memory store. An in-memory store is not a
durable recovery source across processes or restarts.

## Integration sequence

The safe delivery order is:

1. land the pure binding, shape, receipt, and state decision model with adversarial tests;
2. add a read-only coordinator wrapper with explicit optional-store ownership;
3. define and migrate the invocation-start/result-receipt event schemas;
4. atomically bind enqueue acceptance to the durable start record, or make their split-brain
   state explicitly repairable without re-execution;
5. implement a worker that claims, heartbeats, fences downstream resources, and publishes a
   completion-capable result receipt;
6. implement idempotent business projection from verified receipt to artifacts/result/task
   status;
7. run process-kill fault injection at every boundary and retain release evidence.

Steps 1 and 2 are safe foundations. They do not authorize enabling durable attempt execution
in `OrchestratorKernel`. Skipping directly to automatic replay would turn an observable stuck
task into a possible duplicate external effect.

## Verification and release evidence

The coordination foundation requires deterministic coverage for:

- every state-matrix row;
- mismatched session, plan, task, Agent, invocation, idempotency key, and payload digest;
- malformed/forged job scalars and contradictory lease/terminal fields;
- succeeded-without-reference, reference-without-receipt, and mismatched receipt;
- stale attempt number/epoch and a receipt attached to a non-success state;
- missing job versus store failure;
- owned and borrowed store closure, double close, and use after close;
- no mutation of either source for every decision and failure path.

Before runtime integration can be promoted, retained evidence must additionally include
process-kill tests for enqueue, claim, heartbeat, Agent return, artifact commit, result receipt,
attempt success, and task projection; stale-worker fault injection; backup/restore of both
sources; and a connector-specific proof for every externally observable effect.

## Explicit non-guarantees

This foundation does not provide:

- an invocation worker, scheduler integration, heartbeat loop, or cancellation propagation;
- an atomic event-store/attempt-store/result-store transaction;
- automatic repair or projection of a recovered `RUNNING` task;
- exactly-once Agent execution or exactly-once external effects;
- a distributed lock, PostgreSQL implementation, KMS identity, or production connector;
- evidence that current direct `AgentRuntimePort` callbacks are safe to retry.

Until the remaining integration and fault-injection gates pass, a recovered task with unknown
effect state must remain blocked and visible to an operator.
