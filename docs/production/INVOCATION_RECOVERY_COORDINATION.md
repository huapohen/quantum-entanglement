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
| `RUNNING` | caller-provided receipt for current attempt | `BLOCKED_RECEIPT_UNVERIFIED` | future trusted receipt reader | reconcile, invoke the Agent again, or discard the candidate receipt |
| `SUCCEEDED` with no `result_ref` | absent | `BLOCKED_RESULT_UNCOMMITTED` | operator/reconciliation workflow | project `COMPLETED` or retry the Agent |
| `SUCCEEDED` with `result_ref` | missing receipt | `BLOCKED_RESULT_UNCOMMITTED` | receipt reconciler | treat the reference itself as a receipt |
| `SUCCEEDED` with `result_ref` | caller-provided matching receipt | `BLOCKED_RECEIPT_UNVERIFIED` | future trusted receipt reader | project completion or treat the in-memory object as durable proof |
| `FAILED` | absent | `TERMINAL_FAILURE_EFFECT_UNKNOWN` | failure/effect reconciliation workflow | assume failure proves that no external effect occurred |
| `QUEUED` or `FAILED` | caller-provided receipt for the latest attempt | `BLOCKED_RECEIPT_UNVERIFIED` | future trusted receipt reader | reconcile, retry, or erase the candidate receipt |
| `CANCELED` | any | unsupported/integrity failure in the current API | future authorized cancellation reconciler | project cancellation without a durable authorized receipt |

A caller-provided receipt whose invocation binding or attempt ID/number/lease epoch/token differs
from the supplied job snapshot is an integrity failure. Its `result_ref` is compared only when a
`SUCCEEDED` job itself carries a durable result reference. A candidate beside a `RUNNING`,
attempted `QUEUED`, or `FAILED` job has no job result reference to compare and is only
shape-checked before returning `BLOCKED_RECEIPT_UNVERIFIED`. The current boundary also checks
only that `manifest_digest` has canonical SHA-256 shape, `stream_id` has the expected
session-derived shape, and `stream_sequence` is positive. It has no trusted receipt store against
which to prove the result reference, manifest contents, receipt identity, or actual durable
stream position. Those candidate fields are therefore future-contract data, not verified
evidence. Receipt presence on a first-claim queued job is contradictory because no attempt exists
to own it.

`FIRST_CLAIM_READY` means only that the existing durable job has never been attempted and is
eligible for the attempt-store claim protocol. A queued retry is not equivalent: the prior
worker may have performed an effect before losing its lease. A future retry-safety proof may
allow retry only when it durably establishes that the operation is pure, downstream-fenced,
or receiver-idempotent for the exact invocation key.

`WAITING_ACTIVE_LEASE` does not itself evaluate wall-clock expiry. Expiry must use a locked
attempt-store transition so epoch fencing cannot be bypassed, but expiry alone is not retry
authorization. The store may move expired work to `QUEUED` when configured attempts remain,
but `claim()` and `claim_next()` select only `attempts_started = 0`. Their in-transaction expiry
recovery can fence the stale owner but cannot immediately or later reclaim the attempted job.
The job remains `BLOCKED_EFFECT_UNKNOWN` until a future durable receipt reconciler or
retry-safety classifier authorizes a separate, explicit transition.

## Threat matrix

| Threat/failure window | Unsafe interpretation | Required fail-closed response |
|---|---|---|
| task `RUNNING` committed, process dies before durable enqueue | missing job means safe retry | block as effect/job unknown; never synthesize identity during recovery |
| job enqueued, process dies before first claim | call Agent from recovery | permit only the first store-owned claim; do not create another job |
| worker performs effect, dies before receipt | expired lease proves no effect | fence stale ownership and enter effect-unknown reconciliation; do not auto-retry |
| candidate result receipt appears beside `RUNNING` after a crash | an in-memory object proves the result committed | preserve the candidate and return `BLOCKED_RECEIPT_UNVERIFIED`; require a future trusted receipt reader before reconciliation |
| expiry recovery races a candidate receipt | queued/failed means the candidate can be ignored or trusted | preserve the candidate and return `BLOCKED_RECEIPT_UNVERIFIED`; neither receipt nor job authorizes execution |
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

`InvocationRecoveryCoordinator` implements this lifecycle as a synchronous read-only wrapper.
The optional store is borrowed unless `owns_store=True` is explicit. Coordinator close is
idempotent, borrowed stores remain open, and owned stores close exactly once after successful
cleanup. The first close request makes the coordinator permanently unavailable for reads and
re-entry before invoking owned-store cleanup. If cleanup raises after partially releasing a
resource, later `close()` calls retry only that cleanup; they never reopen assessment. Task
status, binding, and receipt shape are validated before the first store call; store read
failures propagate unchanged and are never converted into `BLOCKED_MISSING_JOB`.

The runtime must not acquire a second implicit in-memory store. An in-memory store is not a
durable recovery source across processes or restarts.

`SQLiteInvocationAttemptStore.recovery_snapshot_for_task` implements the read boundary. It
opens one deferred read transaction, decodes at most one job, and streams at most 1,001 attempt
rows in attempt-number order. Recovery supports at most 1,000 attempts and rejects the 1,001st
row without allocating an unbounded history. Every row is fully decoded and validated; attempt
numbers must be contiguous, lease epochs must be strictly increasing, every attempt must start
no earlier than its job or its predecessor's finish, and heartbeat/lease/finish timestamps must
preserve causal order. An owned success/failure finishes before its deadline, while expiry
finishes at or after it. Only a succeeded attempt may carry `result_ref`, and every non-current
attempt must be `FAILED` or `EXPIRED`. Only the current attempt is retained for the returned
snapshot. The snapshot then cross-checks current attempt identity, epoch, owner, token digest,
heartbeat, lease deadline, finish, terminal status, and result reference.
Concurrent WAL writers may advance the live job while this read is in progress, but every row
returned to the coordinator comes from the same pre-advance database snapshot.

A job with zero attempts must also have lease epoch zero. A nonzero epoch with no history is
partial-restore or tampering evidence, not proof that the invocation is fresh. The persisted
job decoder and recovery snapshot reject it, while first-claim selection and its final CAS both
require epoch zero as defense in depth. Conversely, a zero-counter job with any retained attempt
history is partial-restore evidence: first claim raises an integrity error and its final CAS also
requires that no attempt row exists. The same decoder rejects running or succeeded/failed
jobs without an attempt, non-succeeded jobs with `result_ref`, partial lease fields on any
non-running job, and job/attempt causal timestamps that move backward or collapse an active
lease to zero duration. A fresh zero-attempt job cannot carry `last_error`; failed or expired
attempt errors must be present and exactly match the current queued/failed job, while running or
succeeded attempts cannot carry an error. The claim and recovery APIs therefore cannot normalize
contradictory restored state or silently discard a prior-effect warning.

The attempt-store write boundary shares the coordinator's 4,096-byte identity/worker and
16,384-byte result-reference limits and rejects C0/DEL control characters before opening a
write transaction. Exact-boundary worker and result values round-trip through the atomic
snapshot; one-byte-over and control-character inputs leave the job and attempt unchanged.
Ownership mutations also compare the transaction-sampled clock with the selected job/attempt
activity floor. A regressed sample raises `InvocationClockRegressionError` and rolls back instead
of corrupting timestamps, normalizing restored state, or masquerading as a stale lease.

## Pure decision API

`invocation_recovery.assess_invocation_recovery` implements the corrected matrix without a
write path. Callers must provide:

1. the exact `TaskStatus.RUNNING` projection;
2. an `InvocationBinding` decoded from committed, versioned invocation-start evidence;
3. one `InvocationRecoverySnapshot` from the atomic store read above;
4. an optional candidate `InvocationResultReceipt`; the current API has no trusted durable
   receipt source and therefore never treats this caller-provided object as authoritative.

The boundary revalidates frozen objects on every call because Python callers can mutate a
frozen dataclass with low-level reflection. It validates bounded UTF-8 text, control
characters, canonical digests/timestamps and their causal order, job and attempt
counters/statuses, cross-row lease ownership/finish, all seven binding fields, and receipt
attempt ID/number/epoch/token digest. It
accepts the schema-compatible `lease_epoch >= attempts_started` relationship only when a
zero-attempt job also has epoch zero, and does not reject a requested availability timestamp
merely because it predates enqueue time.

Constructing an `InvocationResultReceipt` object in memory does not make a result durable or
authentic. The type describes the fields a future trusted receipt decoder must produce. The
current API validates candidate shape, invocation binding, attempt identity, and any succeeded
job result reference, then returns `BLOCKED_RECEIPT_UNVERIFIED`. It exposes no executable
receipt decision. A future change may add one only together with a durable receipt store,
authenticated decoder, receipt-bound reconciliation CAS, fault injection, and a new decision
API that cannot be reached with caller-constructed evidence. Current `task.result.received`
events are not completion-capable receipts.

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
- valid caller-provided receipts remaining `BLOCKED_RECEIPT_UNVERIFIED` beside
  running/queued/failed/succeeded jobs, plus malformed and mismatched receipt rejection;
- stale attempt ID/number/epoch/token digest, unsafe historical attempt status, non-monotonic
  historical epoch, and the 1,000-attempt recovery bound;
- missing job versus store failure;
- owned and borrowed store closure, double close, failed-close cleanup retry, and permanent
  read rejection from the first close request;
- explicit failure and in-claim expiry fencing without automatic second claim;
- no mutation of either source for every pure decision and failure path.

Targeted deterministic verification is:

```bash
PYTHONPATH=src python3 -m unittest \
  tests.test_attempts tests.test_invocation_recovery -v
```

At implementation checkpoint `d3b92c3`, 665 repository-wide tests passed under both the
default Python and Python 3.13. The 34 attempt-store, 25 coordinator, and 20 session-recovery
tests also passed under Python 3.13 with `ResourceWarning` promoted to an error. Locked Ruff
0.16.3 lint/format, strict mypy over 31 source modules, `compileall`, dependency-lock
verification, the deterministic group-chat demo, and canonical local release-evidence
generation/verification passed. The local evidence is a baseline, not production promotion.

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
