# Durable invocation attempts: design and operations

Status: storage foundation and caller-owned first-claim primitive implemented; the canonical
admission/atomic-start design is frozen in
[`ATOMIC_INVOCATION_START.md`](./ATOMIC_INVOCATION_START.md), but its codecs, EventStore unit of
work and orchestrator integration are pending.

This component prevents two local worker processes from both believing they own the
same Agent invocation. It also gives a crashed invocation a bounded recovery path instead
of leaving mutable execution state permanently `running`. It does **not** by itself make
Agent side effects exactly once. That requires the runtime integration and action-receipt
transaction described under "Integration boundary".

## Supported operating boundary

- one host and one SQLite database file;
- multiple worker processes or independent SQLite connections on that host, provided every
  process constructs its own store after process creation;
- one automatically claimable attempt, lease heartbeat, expired-owner fencing and terminal CAS;
- fail-closed effect-unknown quarantine after any failed or expired attempt;
- fake or separately approved external connectors only;
- Python 3.9 through 3.13 and a filesystem on which SQLite WAL locking is reliable. Python 3.14
  remains outside the package compatibility window until its context-protocol and SQLite
  `STAT4` regressions have dedicated fixes, locks and CI evidence.

This is a Phase 1 primitive. It is not the Phase 4 distributed worker implementation.
Do not place the SQLite file on NFS or another filesystem without verified POSIX locking,
and do not use this store as a multi-host lease coordinator.

File-backed stores require and enable WAL. The `:memory:` default is supported for isolated
unit tests only; it does not use WAL and cannot share ownership across connections or
processes.

Never create a store before POSIX `fork()` and then use or close that inherited object in the
child. A SQLite connection, Python `RLock`, thread-local control boundary and its nonce are all
process-local capabilities. Every public store entry point checks the creator PID before touching
the lock or connection and rejects inherited use with `InvocationStoreProcessMismatchError`, code
`invocation_store_process_mismatch`. The child must discard the inherited reference without
calling `close()` and construct a new file-backed store for the same path. The parent retains and
closes its own instance. Existing multi-process evidence uses `spawn`, where each worker constructs
its connection, plus a separate `fork` misuse test that proves inherited access fails closed; it
does not authorize sharing one connection object across processes.

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
Persisted decoding enforces the same budget boundary: an attempted `queued` job has remaining
budget, while a `failed` job has consumed exactly `max_attempts`. Contradictory restored rows
fail integrity rather than being normalized into another status.

## Ownership and fencing invariants

Every first claim executes in `BEGIN IMMEDIATE`, conditionally moves one eligible job to
`running`, increments `attempts_started` and `lease_epoch`, and inserts its attempt history
before commit. Eligibility and the final CAS both require `attempts_started = 0` and
`lease_epoch = 0`; independent SQLite connections therefore serialize the ownership decision,
while failed, expired, or partially restored ownership state cannot be reclaimed.

### Caller-owned first-claim composition boundary

Commit `0871711c44e371318532313aae7611b66b2563b1` extracts the existing first-claim body into
package-internal, caller-owned transaction primitives:

- `_select_first_claim_candidate_in_transaction(...)` performs the exact ordered selection,
  attempt-history check, claimable re-read, non-regressing-clock check, budget check and epoch
  bound without mutating the candidate;
- `_claim_first_invocation_in_transaction(...)` repeats that validation, executes the first
  job CAS, inserts the running attempt and returns the opaque lease, but never begins, commits
  or rolls back a transaction, acquires the store lock, samples a clock or allocates identifiers;
- `_InvocationClaimRequest` snapshots worker and optional invocation selection before the
  transaction body uses them.

The public `claim`/`claim_next` wrapper still owns `BEGIN IMMEDIATE`, expiry recovery,
commit-acknowledgement reconciliation and store poisoning. It fully validates that a candidate
exists before calling the attempt-ID and lease-token providers, then binds the second validation
to that exact invocation. Empty, missing, future, effect-unknown/non-first and recovered-only
paths therefore call neither provider; a recovered-only transaction can commit expiry recovery
without a later provider fault rolling it back. The wrapper rechecks process ownership after
each provider. A real fork inside a provider makes the child reject the inherited store without
running SQLite cleanup, while the parent retains its transaction and connection.

This extraction is an internal composition seam, not yet the cross-store unit of work. No
start event or immutable claim/start receipt shares this transaction, and
`OrchestratorKernel` does not use it. The exact future EventStore-owned transaction, canonical
event vocabulary, non-replayable lease rule and commit-ambiguity behavior are frozen in
[`ATOMIC_INVOCATION_START.md`](./ATOMIC_INVOCATION_START.md). Direct callers of the helper own all
locking, transaction lifecycle, deadline calculation, rollback and commit-ambiguity policy;
using it in autocommit mode is outside the supported contract.

The returned `InvocationLease` contains two different controls:

- `lease_token`: a random, opaque ownership capability used only for store CAS;
- `lease_epoch` / `fencing_token`: a monotonically increasing integer scoped to this
  invocation, for receivers that retain fencing state per invocation.

Heartbeat, completion and failure require the lease's session, plan, task, Agent, idempotency,
payload, attempt, budget, worker, claim-time and epoch binding to match the durable job and
current-attempt rows. The opaque token digest, heartbeat and deadline must also agree between
those two rows, with a deadline strictly later than the store-owned current time. At the exact
expiry instant, terminal CAS fails.
Recovery clears the opaque token and moves the job to a queued effect-unknown state (or terminal
failure at its configured limit). The old worker can no longer heartbeat, complete or fail the
job, and no new worker can claim it automatically.

The token is hidden from dataclass `repr`, and both the current job and attempt history
store only its SHA-256 digest. Never add the raw token to events, logs, traces, metrics or
error messages. Read observations expose the digest and canonical heartbeat timestamp so a
single-snapshot recovery coordinator can verify job/attempt ownership without receiving the
raw capability. A
connector should receive the composite `(invocation_id, fencing_token)` and reject an older
epoch only against state retained for that same invocation. Epochs are not globally or
resource-scoped: two different first-attempt invocations both receive epoch 1. This mechanism
therefore fences stale workers for one logical invocation but cannot order two different
invocations that mutate the same shared resource. Such a connector needs a separate
resource-scoped monotonic fence allocator and downstream CAS, neither of which is implemented
here. If a connector cannot enforce per-invocation fencing, it must enforce the stable
invocation idempotency key and is still at-least-once.

All identity and worker text is valid UTF-8 without C0/DEL control characters and is capped at
4,096 encoded bytes. Result references use the same character rule and a 16,384-byte cap.
Failure text is validated against a 16,384-byte input cap and then retained at no more than
4,096 characters. Enqueue/claim/complete/fail boundaries and persisted-row decoders enforce
the same rules, so a successful public write cannot create state that recovery rejects solely
because of text shape. A zero-attempt job cannot carry `last_error`. Every current failed or
expired attempt carries an error exactly equal to the owning queued/failed job's `last_error`;
running and succeeded attempts cannot carry an error.

## Clock boundary

Individual claim, heartbeat, recovery and terminal calls cannot supply a timestamp. They
all read the clock configured when the store is constructed; the default is UTC system
time. The injectable clock exists for deterministic tests and must not be controlled by a
request, Agent or connector in production.

Ownership-sensitive calls sample that clock only after `BEGIN IMMEDIATE` has acquired the
write transaction. Time spent waiting for SQLite ownership therefore cannot be hidden by a
stale pre-lock timestamp. Inputs use strict RFC 3339 syntax and are normalized to UTC with
microsecond precision. A positive lease duration that rounds to the same durable timestamp is
rejected before mutation; every accepted lease deadline is strictly later than its heartbeat.
A syntactically valid offset timestamp whose UTC conversion would underflow year 1 or overflow
year 9999 raises a stable `ValueError`; clock and retry mutations roll back without a write.
A duration too large for floating-point conversion, `timedelta`, or the supported `datetime`
range raises the same stable `ValueError`. Claim and heartbeat perform no durable write on that
path; validation after taking the SQLite write lock rolls the transaction back in full.

Before a first claim writes, the sampled time must be no earlier than the selected job's
creation and last update. Before heartbeat, completion or failure writes, it must be no earlier
than the fully decoded job update and current attempt start/heartbeat. A violation raises
`InvocationClockRegressionError`, a subtype of `InvocationIntegrityError`, and the complete
transaction rolls back. It is intentionally different from a `False` terminal/heartbeat result,
which means the lease is stale or expired. Equal timestamps remain valid at durable microsecond
resolution; the store does not silently clamp a regressed sample.

For current running ownership, persisted reads require job creation, attempt start, heartbeat,
job update and lease deadline to occur in that order, with the deadline strictly later than all
activity. A terminal attempt must finish at or after its heartbeat. An owned success/failure must
finish before its lease deadline; expiry must finish at or after it. Recovery additionally
requires every historical attempt to start no earlier than the job and no earlier than the
preceding attempt's finish. Job/current-attempt finish timestamps and ownership fields are
cross-checked.

All instances on the supported single host share the same system clock. Operators must monitor
clock synchronization. A backward clock jump before durable activity freezes affected ownership
mutations and delays expiry recovery; a forward jump can expire active work. The persisted floor
is per invocation, not a database-wide time authority, so it cannot detect a backward jump while
creating an unrelated new job. Multi-host deployment needs a database-authoritative clock and is
out of scope for the SQLite implementation.

## Transaction acknowledgement and store lifecycle

A successful SQLite `COMMIT` can be followed by a driver, wrapper, tracing hook, process signal,
or cancellation error before the caller receives the acknowledgement. Retrying that mutation
blindly can duplicate ownership or misreport a durable terminal transition as failed. Every
public write therefore records its exact commit candidate before leaving the transaction and,
when the transaction has ended but acknowledgement failed, reads durable state back before it
returns:

| Public operation | Exact durable outcome required for reconciliation |
|---|---|
| `enqueue` | exactly one row bound to the same invocation/session task/idempotency identities and immutable job specification |
| `recover_expired` | the exact recovered invocation set, recovery timestamp, expired attempt state, error, and queued/failed job state |
| `claim` / `claim_next` | the generated attempt ID, full lease binding, token digest, running state, owner, claim time, and initial deadline |
| `heartbeat` | the same running owner plus the exact heartbeat timestamp and resulting deadline in both rows |
| `complete` | succeeded job/attempt, exact result reference, and exact finish/update timestamp |
| `fail` | failed attempt, exact stored error, expected queued/failed target, retry availability, and finish/update timestamp |

The combined claim transaction can expire an old owner without selecting a new claim. Its
recovery outcome is reconciled with the same exact rules. A truly no-op claim, recovery, or
stale-owner CAS may return `None`, an empty summary, or `False` after a lost commit
acknowledgement because that transaction had no candidate mutation. It may do so only when
rollback is not itself uncertain. Concurrent state advance can make an otherwise durable
candidate fail exact readback; that is a safe false negative and returns ambiguity, never a
false success.

The public failure contract is:

- Every public write (`enqueue`, both claim variants, recovery, heartbeat, completion and
  failure) has its own non-bypassable control-signal boundary. If BEGIN, the transaction body,
  or `COMMIT` fails while rollback is confirmed, untrusted ordinary failures become
  `InvocationTransactionError`, code `invocation_transaction_failed`; their original identity,
  message, attributes, notes, chain and traceback do not cross the boundary. Exact
  library-authored validation/integrity failures are reconstructed only when every Python frame
  in the complete traceback belongs to a code-object provenance set frozen before configured
  providers can run. Matching a module or function name, or grafting one trusted frame onto an
  untrusted traceback, is not sufficient. The validated descriptor is carried only by an exact,
  per-invocation nonce-bound internal signal; the public wrapper exits every catch and cleanup
  frame, deletes call references, and then raises a fresh same-class error outside the caught
  exception graph.
- Exact `KeyboardInterrupt`, `GeneratorExit`, `asyncio.CancelledError`, and `SystemExit` remain
  control flow, but a new same-class instance is raised after transaction cleanup. It retains no
  original arguments, attributes, notes, chain or traceback. `SystemExit` retains only `None`, an
  exact `bool`, or an exact integer in `0..255`; all other exit-code objects map to `1`. Subclasses
  of those controls and `BaseExceptionGroup` are untrusted failures, not control flow.
- If `COMMIT` ended the transaction but its acknowledgement failed, the method returns success
  only after the exact readback above. An exact control raised by the lost acknowledgement is
  reissued clean after successful readback. Readback exceptions of any `BaseException` subtype
  fail closed.
- An outcome that cannot be reconciled raises
  `quantum_entanglement.attempts.InvocationCommitAmbiguityError`, with stable code
  `invocation_commit_ambiguous`. Callers must stop automatic retry for that invocation and move
  to durable recovery/operator reconciliation.
- If rollback or transaction-state inspection is not confirmed, the current call raises the
  same commit ambiguity and the store instance is permanently poisoned. Best-effort close is
  attempted immediately; all later data/schema APIs raise `InvocationStorePoisonedError` with
  code `invocation_store_poisoned`. The instance is never automatically unpoisoned. If an exact
  control signal accompanies this ambiguity, the clean control remains the top-level exception,
  its direct cause is a traceback-free `InvocationCommitAmbiguityError`, and poisoning still
  occurs before it crosses the public boundary.
- Transaction-state inspection accepts only an exact built-in `bool`. Exact `True` preserves the
  confirmed-open rollback path and exact `False` preserves lost-ACK reconciliation. `None`,
  integers such as `0`/`1`, strings, subclasses, and arbitrary objects are ambiguity: the store
  is poisoned and closed without ever invoking their `__bool__` or otherwise truth-testing
  attacker-controlled state.
- An explicitly closed instance raises `InvocationStoreClosedError`, code
  `invocation_store_closed`. A close acknowledgement failure is translated to that fixed,
  sanitized error and `close()` can be retried. An exact close control is reissued clean with a
  traceback-free `InvocationStoreClosedError` as its direct cause, after logical closed state is
  fixed. During context-manager exit, an already-active body exception or control always has
  priority; a simultaneous close failure/control is discarded without being attached to the
  body exception graph. With no body exception, the close result follows the explicit contract.
- A store used from a PID other than its creator raises
  `InvocationStoreProcessMismatchError`, code `invocation_store_process_mismatch`, before any
  inherited lock, connection, poison flag or control-boundary state is read or mutated. This is a
  programming/deployment error, not transaction ambiguity; create a new child-process instance.

Write and recovery read-transaction rollback failures use the same quarantine rule. Translated
public errors sever the raw driver exception graph before crossing the boundary; fault messages,
SQL, database paths, and opaque lease tokens are not retained through `__cause__`, `__context__`,
exception arguments, notes, or custom driver attributes. Do not log a caught raw exception before
translation or add raw exceptions back as notes. Production traceback logging must keep
`capture_locals=False`; enabling local capture can serialize method arguments, store objects, raw
lease capabilities, database paths or provider state even when the exception graph is clean.

This hardening is deliberately scoped to public writes, reconciliation snapshots, and close.
Ordinary read APIs such as `get`, `get_for_task`, `attempts`, `attempts_page`, and
`schema_version` reject a closed/poisoned instance but do not yet provide a general driver-error
sanitizer for arbitrary SQLite/provider faults. Closing that read-boundary gap is P1 security
work. Until it lands, callers must treat raw read failures as process-internal, avoid attaching
them to user-visible responses or telemetry with locals, and terminate/quarantine the affected
request path. This document does not claim that every read API is sanitized.

For a file-backed database, construct a new store instance after quarantine, run migration and
integrity verification, then read the job/attempt snapshot. A successful close rolls back an
open uncommitted SQLite transaction; a failed close can retain a database lock until a later
explicit close succeeds or the process exits. Never continue business operations through the old
instance. A process killed after durable `COMMIT` but before Python can run readback produces no
in-process ambiguity exception at all; restart recovery must discover that state. This component
still does not prove whether an external Agent/tool effect occurred.

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
   by itself authorizes retry in the current implementation. Pass the invocation ID with the
   epoch; never interpret the integer as a global or shared-resource fencing token.
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
integrity failure, never a fresh job. The reverse is equally unsafe: a zero-counter job with
any retained attempt row is not fresh. First claim checks for an empty history inside the same
write transaction, and the final job CAS repeats that `NOT EXISTS` predicate as defense in depth.

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

The integration change must make these durable boundaries reconcilable. The first boundary is
now split explicitly into canonical semantic admission followed by receipt-gated atomic first
claim/start; neither a generic admission nor a standalone queued job authorizes dispatch:

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
validation, post-lock clock sampling, cross-connection clock-regression rollback, complete
job/attempt time causality, sub-microsecond lease rejection, strict timestamp parsing, token
non-disclosure, oversized lease normalization without mutation, orphan-history first-claim
rejection, invalid lease inputs, committed-then-error reconciliation for every public mutation,
pre-commit and post-commit `BaseException` behavior, exact readback interruption, no-op safety,
full lease-binding forgery rejection, caller-owned helper rollback/composition equivalence,
zero-provider no-candidate paths, provider fault/control cleaning, provider PID drift and real
fork behavior, read/write rollback poisoning, close retry, hostile driver exceptions,
exception-graph redaction, persistent reopen, and waiting-thread quarantine:

```bash
PYTHONPATH=src python3 -m unittest tests.test_attempts -v
```

The current direct suite contains 129 tests. The matrix includes exact control-signal cleaning,
safe `SystemExit` codes, hostile
exception objects, control subclasses/groups, forged internal sentinels, whole-traceback
provenance and grafting attacks, exact-`bool` transaction-state inspection, hostile `__bool__`
objects, claim readback rejection after concurrent heartbeat/deadline drift, and a real POSIX-fork
probe covering inherited reads, writes, recovery, context entry, close and identifier-provider
fork. The exact source commit above was also verified in an isolated detached worktree with
1,197 repository tests, locked Ruff lint/format, strict mypy over 41 source modules and the
4-target/74-record dependency-lock verifier. This is local reproducible evidence, not a
production promotion.

The Phase 1 release still requires process-kill fault injection at every integration
boundary, a real runtime heartbeat/cancellation test, backup/restore rehearsal and retained
release evidence. The unit suite is necessary evidence for the storage primitive, not a
production-readiness declaration.
