# Durable invocation attempts: design and operations

Status: storage foundation implemented; orchestrator integration pending.

This component prevents two local worker processes from both believing they own the
same Agent invocation. It also gives a crashed invocation a bounded recovery path instead
of leaving mutable execution state permanently `running`. It does **not** by itself make
Agent side effects exactly once. That requires the runtime integration and action-receipt
transaction described under "Integration boundary".

## Supported operating boundary

- one host and one SQLite database file;
- multiple worker processes or independent SQLite connections on that host;
- one automatically claimable attempt, lease heartbeat, expired-owner fencing and terminal CAS;
- fail-closed effect-unknown quarantine after any failed or expired attempt;
- fake or separately approved external connectors only;
- Python 3.9 or newer and a filesystem on which SQLite WAL locking is reliable.

This is a Phase 1 primitive. It is not the Phase 4 distributed worker implementation.
Do not place the SQLite file on NFS or another filesystem without verified POSIX locking,
and do not use this store as a multi-host lease coordinator.

File-backed stores require and enable WAL. The `:memory:` default is supported for isolated
unit tests only; it does not use WAL and cannot share ownership across connections or
processes.

## Durable model

`invocation_jobs` contains one mutable row for a logical `(session_id, task_id)` invocation.
The `(session_id, idempotency_key)` constraint is a second deduplication boundary. An
identical enqueue returns the original row even if the retry generated a new candidate
`invocation_id`; changed task, Agent, payload digest, schedule, priority or retry policy is
rejected with `InvocationConflictError`.

`invocation_attempts` is the append-only execution history. The first successful claim inserts
one row. An explicit runtime failure and an expired lease both consume that attempt. Attempt
rows move once from `running` to `succeeded`, `failed` or `expired`. The schema retains
`max_attempts` and can read legacy multi-attempt histories, but the current claim API never
creates a later attempt: retry requires durable effect/receipt reconciliation that is not yet
implemented.

The job state machine is:

```text
queued (zero attempts) --claim--> running --complete CAS--> succeeded
                                    |
                                    +--failure/expiry, attempts remain-->
                                    |     queued (effect unknown; claim blocked)
                                    |
                                    +--failure/expiry at limit--> failed
```

`succeeded`, `failed` and `canceled` are terminal. Cancellation is reserved in schema but
is not exposed until the cancellation/action-receipt contract is implemented.

## Ownership and fencing invariants

Every first claim executes in `BEGIN IMMEDIATE`, conditionally moves one eligible job to
`running`, increments `attempts_started` and `lease_epoch`, and inserts its attempt history
before commit. Eligibility and the final CAS both require `attempts_started = 0` and
`lease_epoch = 0`; independent SQLite connections therefore serialize the ownership decision,
while failed, expired, or partially restored ownership state cannot be reclaimed.

The returned `InvocationLease` contains two different controls:

- `lease_token`: a random, opaque ownership capability used only for store CAS;
- `lease_epoch` / `fencing_token`: a monotonically increasing integer for downstream
  resources that support fencing.

Heartbeat, completion and failure require the same invocation ID, worker ID, opaque token,
epoch and a deadline strictly later than the store-owned current time. At the exact expiry
instant, terminal CAS fails. Recovery clears the opaque token and moves the job to a queued
effect-unknown state (or terminal failure at its configured limit). The old worker can no
longer heartbeat, complete or fail the job, and no new worker can claim it automatically.

The token is hidden from dataclass `repr`, and both the current job and attempt history
store only its SHA-256 digest. Never add the raw token to events, logs, traces, metrics or
error messages. Read observations expose the digest and canonical heartbeat timestamp so a
single-snapshot recovery coordinator can verify job/attempt ownership without receiving the
raw capability. A
connector that can mutate a fenced resource should receive the integer fencing token and
reject an epoch below the resource's last accepted epoch. If a connector cannot enforce
fencing, it must enforce the stable invocation idempotency key and is still at-least-once.

## Clock boundary

Individual claim, heartbeat, recovery and terminal calls cannot supply a timestamp. They
all read the clock configured when the store is constructed; the default is UTC system
time. The injectable clock exists for deterministic tests and must not be controlled by a
request, Agent or connector in production.

Ownership-sensitive calls sample that clock only after `BEGIN IMMEDIATE` has acquired the
write transaction. Time spent waiting for SQLite ownership therefore cannot be hidden by a
stale pre-lock timestamp. Inputs use strict RFC 3339 syntax and are normalized to UTC.

All instances on the supported single host share the same system clock. Operators must
monitor clock synchronization. A backward clock jump delays recovery; a forward jump can
expire active work. Multi-host deployment needs a database-authoritative clock and is out
of scope for the SQLite implementation.

## Worker loop contract

A worker integration should use this sequence:

1. Reconstruct an invocation only from the recorded plan, task and `context.compiled`
   event, then compute and compare its canonical payload digest.
2. Idempotently enqueue the logical invocation before recording it as dispatched.
3. Atomically claim it and record `attempt_id`, attempt number and lease epoch in
   `task.invocation.started`; never record the opaque lease token.
4. Start a heartbeat loop before calling the Agent. Set the heartbeat interval to no more
   than one third of the lease duration and keep the Agent timeout below the lease.
5. Pass the stable idempotency key and fencing token to capable connectors; neither mechanism
   by itself authorizes retry in the current implementation.
6. If heartbeat returns false, cancel local result acceptance. A late Agent result is stale
   and must not write artifacts, action receipts or task status.
7. On failure, call `fail` with a redacted reason. A scheduled `retry_at` may place the job in
   queued state for future reconciliation, but `claim` and `claim_next` will not select it. On
   graceful shutdown, stop admission and quarantine unfinished leases.
8. On success, first durably accept the result/action receipt at the boundary described
   below, then execute terminal CAS and project task completion.

Call `recover_expired()` on startup and periodically while workers are active. Recovery is
bounded by `limit` so an operator can avoid one long write transaction. A claim also fences an
expired lease for its candidate before making the ownership decision, but the
combined `attempts_started = 0` and `lease_epoch = 0` eligibility predicate prevents immediate
or later automatic reclaim. A nonzero epoch without attempt history is a partial-restore
integrity failure, never a fresh job.

Suggested pilot timing defaults, to be validated by fault and capacity tests rather than
copied blindly, are a 60-second lease, 15–20 second heartbeat, 30-second runtime timeout, and a
recovery scan every five seconds. `max_attempts` is compatibility metadata in this slice; a
value greater than one does not enable retries.

## Integration boundary and unresolved P0

The current `OrchestratorKernel` still invokes registered runtimes directly and only holds
an in-process session lock. It does not yet call `SQLiteInvocationAttemptStore`. A bounded
single-snapshot job/attempt reader and the fail-closed matrix in
`INVOCATION_RECOVERY_COORDINATION.md` now exist, and legacy recovery explicitly quarantines a
durably `RUNNING` task instead of silently publishing a stuck graph. Those foundations do
**not** close the readiness audit finding: the task remains unavailable until trusted start
evidence, result receipts, worker fencing, and receipt-bound projection are integrated.

The integration change must make these durable boundaries reconcilable:

1. enqueue/claim ownership;
2. `task.invocation.started` with attempt identity and epoch;
3. Agent result plus persistent artifact/action receipt acceptance;
4. terminal attempt CAS;
5. `task.status.changed` to completed or failed.

A crash between result acceptance and attempt completion must reconcile from the accepted
result without invoking the Agent again. A crash before accepted result may retry only after a
future durable proof establishes that the operation is pure, downstream-fenced, or
receiver-idempotent for this exact invocation. Calling `complete()` before persisting the
result is forbidden because it could leave a succeeded attempt with no recoverable result.
Calling it after a non-idempotent side effect without an action receipt is also forbidden
because a crash can repeat the effect. The artifact/result transaction and action-receipt
state machine are separate Phase 1 deliverables and must land before a commercial production
claim.

## Schema migration

Forward migration is packaged as
`src/quantum_entanglement/migrations/0001_invocation_attempts.up.sql`. Store construction:

1. enables foreign keys, WAL and the configured busy timeout;
2. creates `qe_schema_migrations` when absent;
3. computes SHA-256 over the packaged migration;
4. applies schema and migration record together under `BEGIN IMMEDIATE`;
5. fails closed with `MigrationDriftError` if the applied filename or checksum differs.

The SQL files are package data, so a built wheel and a source checkout use the same bytes.
Before promotion, rehearse installation from the wheel on a copy of the previous release
database and retain the database checksum, migration row and integrity-check output as
release evidence.

Read-only verification queries:

```sql
SELECT version, filename, sha256, applied_at
FROM qe_schema_migrations ORDER BY version;

PRAGMA foreign_key_check;
PRAGMA integrity_check;
```

## Rollback

`0001_invocation_attempts.down.sql` drops both attempt tables and deletes migration record
1. It is intentionally destructive and is provided for a rehearsed application rollback,
not routine recovery.

Before applying it:

1. stop new workflow admission;
2. stop workers and verify there are no active leases;
3. create and verify a SQLite online backup;
4. retain attempt rows needed for audit or incident review;
5. run the down migration only against the exact database file selected for rollback;
6. start the prior application, run integrity checks and its smoke test;
7. restore the backup immediately if verification fails.

If any invocation job has been accepted in production, prefer a compatible application
rollback that leaves the new tables in place. Dropping execution history can violate audit
and recovery obligations. The phase release evidence must record whether rollback was
schema-preserving or destructive and include the rehearsal result.

## Operations and diagnostics

Useful read-only inspections are:

```sql
SELECT status, COUNT(*) FROM invocation_jobs GROUP BY status;

SELECT invocation_id, task_id, lease_owner, lease_epoch, lease_expires_at
FROM invocation_jobs
WHERE status = 'running'
ORDER BY lease_expires_at;

SELECT invocation_id, attempt_number, worker_id, status, started_at, finished_at, error
FROM invocation_attempts
ORDER BY started_at DESC;
```

Required metrics for runtime integration are queued count and oldest age, running count,
claim conflicts, heartbeat failures, expired recovery count, attempts per invocation,
exhausted invocations, terminal CAS conflicts and SQLite busy duration. Alert on repeated
expiry, any exhausted high-risk invocation, oldest queued age above SLO, or sustained busy
errors.

`last_error` and attempt `error` are capped at 4,096 characters but are not a secret
redactor. Runtime adapters must redact credentials and sensitive payloads before calling
`fail`; raw exception serialization is not permitted at the production boundary.

## Verification

The deterministic suite covers migration/reopen/coexistence, migration checksum drift,
idempotent enqueue conflict detection, availability/priority, two-connection atomic claim,
two-process atomic claim, heartbeat extension, exact-boundary expiry, stale-worker fencing,
explicit-failure and expiry quarantine without second claim, fresh-job selection past a
higher-priority effect-unknown job, terminal exhaustion, complete bounded recovery-history
validation, post-lock clock sampling, strict timestamp parsing, token non-disclosure and
invalid lease inputs:

```bash
PYTHONPATH=src python3 -m unittest tests.test_attempts -v
```

The Phase 1 release still requires process-kill fault injection at every integration
boundary, a real runtime heartbeat/cancellation test, backup/restore rehearsal and retained
release evidence. The unit suite is necessary evidence for the storage primitive, not a
production-readiness declaration.
