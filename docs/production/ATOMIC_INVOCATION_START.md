# Canonical invocation admission and atomic start

Status: the receipt-gated atomic first claim/start boundary is implemented, exported and retained
on `main`. It is a narrow durable authority boundary, not a production worker: no
heartbeat-supervised worker or runtime dispatch path consumes the returned authority yet.

Implemented checkpoints:

| Boundary | Commit | Current proof |
|---|---|---|
| frozen contract | `fef1260` | this document and linked admission/attempt runbooks |
| schema-1 manifest + schema-2 start codecs | `c4c4dcc` | strict exact-field/NFC/time/digest decoding; no raw lease field |
| codec negative/security matrix | `b2abfa2` | future schema, bool-as-int, legacy names, secret-canary exception chains |
| canonical request builder | `b8cd5c3` | exact two-event READY-to-RUNNING batch and single-attempt job binding |
| semantic binding matrix | `84d8fd3` | legacy/reordered/extra/tampered event/job rejection |
| EventStore canonical wrapper | `8b0dc83` | v4 atomic admission/receipt reused without schema change |
| wrapper fault/process evidence | `f23d8e1` | replay, forgery, poison, lost ACK, clean control and real fork |
| capability/result models | `1bfab1b` | exact immutable receipt, claimed and observed types; lease omitted from serialization/repr |
| durable observation/readback | `031754f`, `b840b04` | canonical admission/job/attempt/schema-2 event are revalidated as one observation |
| atomic first claim/start | `ffc368c`, `70a59ba` | public `claim_invocation_start(...)` owns one transaction and returns authority only after ACK |
| happy path and replay fence | `5f36448`, `5f5b11e` | one first claim; retry, peer connection and reopen are receipt-only observations |
| transaction/control faults | `1fad819` through `4ff05fe` | rollback, BEGIN/COMMIT/ROLLBACK ACK loss, poison and control-signal isolation |
| connection/process exclusion | `8c4d080`, `f655f83` | two connections and two spawned processes issue exactly one plaintext lease |
| process/capability safety | `c6928f9`, `b2d0f16`, `4439630` | provider fork/PID fencing and SQLite/WAL/backup/restore secret canaries |
| package surface | `a2fc9d6`, `3642178` | atomic-start result, receipt and error contracts exported and package-tested |

These hashes identify implementation checkpoints, not a production release. Clean source candidate
`a1fd35569fd093c93956294a644eb416a15e2c06` contains the complete boundary and passed the retained
[CI run 33009646365](https://github.com/huapohen/quantum-entanglement/actions/runs/33009646365):
Python 3.9 and 3.12 each ran 1,320 tests, and canonical release evidence passed. Its independent
[package run 33009646336](https://github.com/huapohen/quantum-entanglement/actions/runs/33009646336)
passed reproducible wheel/sdist, source-bound distribution-manifest, CycloneDX 1.6 SBOM and wheel
smoke gates. The Python 3.9 SQLite-authorizer fixture correction is retained at `9bb2cc7`; it changes
test cleanup, not the production transaction contract. On 2026-08-27, a local Python 3.13 full-suite
rerun completed 1,320 tests successfully (the run emitted the existing CPython fork deprecation
warnings); a focused rerun of `test_invocation_execution`, `test_invocation_start_controls` and
`test_invocation_start_store` completed 75 tests successfully.

This document is the authoritative contract for the implemented execution-spine boundary. It does
not claim that a production worker is enabled, that an Agent can yet run end to end through this
authority, or that external effects are exactly once. The implemented scope is:

```text
canonical execution admission receipt
  -> receipt-gated first claim
  -> running attempt + immutable start evidence in one SQLite transaction
  -> one non-replayable lease capability returned only after acknowledged commit
```

The standalone attempt store already has heartbeat and terminal-CAS primitives, but no
heartbeat-supervised worker is wired to `InvocationStartClaimed`. Atomic Artifact/result/attempt/task
terminal acceptance, durable external action receipts, `effect_unknown` reconciliation, production
connector actions and automatic retry remain outside this boundary. The existing deterministic
demo/legacy runtime path is not evidence that these production integrations exist.

## Why the existing admission receipt is insufficient

`SQLiteEventStore.append_invocation_admission(...)` proves that an ordered caller-supplied event
batch, one queued `invocation_jobs` row and one immutable `invocation_admissions` receipt committed
atomically. It intentionally does not interpret the event batch.

That generic boundary cannot authorize execution:

- it accepts any event type on `session:<session_id>`;
- it does not prove a `READY -> RUNNING` task transition;
- it does not bind an exact execution envelope, context snapshot, authorization snapshot or
  effect/retry class;
- existing examples use the legacy underscore name `task.status_changed`, while the runtime and
  recovery projection use `task.status.changed`;
- the public attempt-store `claim` API can claim a standalone queued job without proving an
  admission receipt.

A worker must therefore never treat a generic admission result or a standalone invocation job as
dispatch authority. Only the canonical API and the exact transaction defined below may mint that
authority.

## Terminology and durable event vocabulary

This contract uses exactly three event names:

| Event | Schema | Meaning |
|---|---:|---|
| `task.execution.requested` | 1 | immutable execution manifest accepted for one logical invocation |
| `task.status.changed` | existing canonical task transition | the same task moved exactly `READY -> RUNNING` |
| `task.invocation.started` | 2 | one first attempt was fenced and started by one worker |

`task.status_changed` and schema-1/legacy `task.invocation.started` are historical, semantically
unbound observations. They must remain readable for audit/recovery but can never be upgraded,
backfilled or interpreted as claim authority.

All schema/version fields are exact built-in integers; booleans are rejected. Every codec rejects
unknown fields, missing fields, duplicate identities, unsupported/future versions, non-canonical
timestamps, non-canonical lowercase SHA-256 values, C0/DEL control characters and over-budget
UTF-8 text. Decoders must not silently normalize a durable defect.

## Execution manifest

`InvocationExecutionManifest` is the immutable input to dispatch. Schema 1 contains:

| Field | Binding |
|---|---|
| `schemaVersion` | exact integer `1` |
| `invocationId` | queued job and admission receipt identity |
| `sessionId` / `planId` / `taskId` / `agentId` | exact job and workflow identities |
| `jobIdempotencyKey` | exact queued-job idempotency boundary |
| `taskRevision` | revision created by the canonical `READY -> RUNNING` transition |
| `correlationId` / `causationId` | exact workflow causal chain; causation is the task ID |
| `envelopeDigest` | canonical coordination-envelope bytes |
| `contextDigest` | immutable context bundle bytes |
| `authorizationDigest` | principal, tenant/workspace, capability and policy/approval revision snapshot |
| `runtimeRevision` | exact Agent/runtime implementation revision selected for the attempt |
| `effectClass` | `pure`, `idempotent`, `receipt_reconciled`, or `non_retriable` |
| `retryClass` | `never` in this slice; later values require a separate recovery contract |

The manifest digest is not the existing generic payload digest. It is:

```text
SHA-256(
  UTF8("quantum-entanglement.invocation-execution-manifest/1\n")
  || canonical-JSON(manifest)
)
```

Canonical JSON is UTF-8, no BOM, NFC strings, sorted object keys, compact separators, JSON scalar
rules from the event store and no NaN/Infinity. The domain separator is part of the digest input.
The queued job `payload_digest` must equal this execution-manifest digest.

`authorizationDigest` is evidence of the exact admission-time decision, not an evergreen
capability. A future action-time policy gate must still re-authorize each real tool/connector
effect and bind its result to an action receipt.

## Canonical admission batch

`append_task_invocation_admission(...)` is the only semantic API intended to create a future
claimable job. It builds, snapshots and atomically appends exactly this ordered pair:

1. `task.execution.requested` schema 1 with the complete execution manifest and its digest;
2. `task.status.changed` for the same task, exactly `READY -> RUNNING`, with the same revision,
   actor, correlation and causation identities.

The request owns stable event IDs and idempotency keys. Recommended keys are:

```text
execution-request:<invocation_id>
task-running:<task_id>:<task_revision>
```

The canonical builder must prove before opening the transaction that:

- every manifest identity equals the `InvocationJobSpec` identity;
- `payload_digest` equals the domain-separated manifest digest;
- stream ID is exactly `session:<session_id>`;
- event actor is the trusted orchestrator service principal;
- both events share the requested correlation ID and use `task_id` as causation ID;
- the transition payload names the same task and exact adjacent revision;
- no caller-supplied extra event is accepted.

`TaskInvocationAdmissionRequest` and `build_task_invocation_admission_request(...)` implement the
pure construction boundary. The current schema fixes the canonical actor to `orchestrator`, binds
the job payload digest to the domain-separated manifest digest, forces `max_attempts=1`, requires
stable caller-supplied event IDs/timestamps and rejects a RUNNING timestamp earlier than the
execution-request timestamp. Every component access rebuilds fresh events/job and revalidates the
complete binding.

`SQLiteEventStore.append_task_invocation_admission(...)` performs the PID/poison check before
touching request state, requires an exact nonnegative stream version, rebuilds and revalidates the
components through class-owned methods, then delegates persistence to the existing generic v4
admission UoW. It intentionally has no second control sanitizer: the delegated admission boundary
already owns transaction/control cleanup, and nesting it would lose the ambiguity cause on a
clean control signal.

Inside the existing admission transaction, normal v4 receipt validation remains authoritative.
An exact replay returns the original events/job. Any generic/legacy event batch, standalone job,
partial event/job/receipt state or binding mismatch fails closed and never becomes canonical by
being replayed through the new API.

## Atomic first-claim/start transaction

The EventStore owns this unit of work because the event log, admission receipt, invocation job and
attempt rows must share the same SQLite connection, lock, process owner and commit outcome.

Implemented public boundary:

```python
SQLiteEventStore.claim_invocation_start(
    invocation_id: str,
    worker_id: str,
    *,
    lease_seconds: float,
    expected_version: int,
) -> InvocationStartClaimed | InvocationStartObserved
```

After all caller inputs are snapshotted and creator-PID ownership is rechecked, the method owns one
`BEGIN IMMEDIATE` transaction and performs, in order:

1. select the v4 admission receipt by invocation identity;
2. strictly revalidate the receipt, its exact event range and the immutable queued job binding;
3. decode the exact canonical execution-request/RUNNING pair and revalidate every semantic
   binding;
4. require the current stream version to equal `expected_version`;
5. require a zero-attempt queued job with no prior attempt or start evidence;
6. allocate attempt ID and opaque lease token only after eligibility is proven, then recheck
   creator PID;
7. call `_claim_first_invocation_in_transaction(...)` to CAS the job and insert the running
   attempt;
8. append `task.invocation.started` schema 2 on the same stream and connection;
9. read back the job, attempt and start event inside the transaction and validate the complete
   cross-binding;
10. commit once.

The start event idempotency key is:

```text
invocation-start:<invocation_id>:<attempt_number>
```

Schema 2 binds:

| Field | Required value |
|---|---|
| `schemaVersion` | exact integer `2` |
| job/workflow identities | invocation, session, plan, task, Agent and job idempotency key |
| attempt identities | attempt ID, attempt number and lease epoch |
| worker | exact worker ID |
| lease proof | SHA-256 digest only; never the raw token |
| time | claimed-at and lease-expires-at, equal to job/attempt rows |
| execution proof | manifest, envelope, context, authorization and runtime digests/revision |
| causal proof | correlation/causation identities inherited from canonical admission |

The immutable schema-2 event is the start receipt for this slice. No migration 5 or parallel
receipt table is added. This avoids bypassing the currently disabled native/domain migration and
operational backup-v2 release boundary. A dedicated table may be introduced only after its
migration executor, topology, writer, verifier, restore reconciliation and rollback evidence are
production-enabled together.

## Return types and non-replayable authority

`InvocationStartReceipt` is a read-only observation of the validated schema-2 event plus its
sequence/global position. It never contains the plaintext lease token.

`InvocationStartClaimed` contains:

- the validated start receipt; and
- the single freshly minted `InvocationLease`, whose plaintext token is hidden from `repr`.

It is returned only when this call received a normal acknowledgement for the transaction that
created the attempt/start evidence. It is the only value that may authorize a future dispatch.

`InvocationStartObserved` contains only the receipt. It is returned when exact durable start
evidence already exists, including after reopening a store. Observation is not dispatch
authority. The store must never regenerate, recover, serialize, log or reissue the plaintext
lease token.

## Commit acknowledgement and ambiguity

This boundary is stricter than the standalone attempt-store claim reconciliation. If commit is
durable but its acknowledgement is lost, readback may prove that start happened but cannot prove
that the caller did not receive or act on the plaintext lease before interruption.

Therefore:

- the current store instance is poisoned;
- the call raises a dedicated, sanitized start-commit ambiguity error;
- no `InvocationStartClaimed` or raw lease is returned;
- automatic dispatch and retry stop;
- after reopen, `read_invocation_start(...)` or the exact retry may return only
  `InvocationStartObserved`;
- rollback-confirmed BEGIN/body/COMMIT failure returns a sanitized transaction error and leaves no
  job, attempt or start mutation;
- exact control signals are reissued clean only after cleanup, with ambiguity represented by a
  traceback-free fixed cause;
- fork/PID mismatch is rejected before touching an inherited lock, SQLite connection, clock or
  ID/token provider.

This fail-closed false negative is intentional. It is safer than issuing two capabilities for one
attempt.

## Retained fault and concurrency evidence

The committed `test_invocation_execution`, `test_invocation_start_controls` and
`test_invocation_start_store` suites retain the following evidence:

- exact manifest/start codecs reject unknown or missing fields, future/legacy schemas,
  bool-as-int values, legacy underscore event names, malformed digests/times and forged model
  instances;
- standalone jobs, generic or semantically forged admissions, wrong-stream start markers,
  event/job/attempt/receipt tampering, partial row deletion, attempt-without-start and
  start-without-attempt all fail closed;
- injected failures after job CAS, start append and fresh readback roll back job, attempt and event
  together;
- BEGIN, COMMIT and ROLLBACK faults, acknowledgement loss, rollback failure and exact control
  signals have sanitized outcomes; ambiguous outcomes poison the current store instance;
- first claim, retry, peer connection and reopen preserve one receipt while only the first normally
  acknowledged call receives `InvocationStartClaimed`;
- two SQLite connections and two spawned processes produce exactly one claim, one observation, one
  attempt, one schema-2 event and one lease-token provider call;
- replay version is anchored to the stream version immediately before the start event, and a wrong
  replay version is rejected before any clock, ID or token provider runs;
- a real fork during each configurable start provider is rejected in the child, while the parent
  retains the only claim;
- plaintext lease canaries are absent from schema-2 payloads, receipt/observation JSON, `repr`,
  exception chains, SQLite rows, database/WAL/SHM bytes, backup database, backup manifest and
  restored database; only the SHA-256 token digest is durable;
- backup v1 round-trip retains and revalidates the schema-2 event without introducing a new schema
  migration or receipt table.

The clean-runner and package evidence linked at the top covers the whole repository, not only these
focused suites. It proves this implementation for the recorded Python/Linux matrix; it does not
prove service-level crash safety or production readiness.

The following evidence is deliberately still open:

- a receipt-bound worker that heartbeats the exact lease/attempt, handles cancel, timeout, expiry,
  graceful drain and crash recovery, and dispatches only pure/fake handlers;
- one atomic acceptance boundary for Artifact/result, attempt terminal state and task terminal
  projection, including stale-worker and ACK-loss matrices;
- receipt-bound recovery classification for the complete worker/result state machine;
- durable external action receipt plus `effect_unknown` reconciliation; and
- authenticated service/fake-connector E2E, supported OS/SQLite matrix, load/chaos/soak and the
  remaining production Gates A–E.

## Explicit non-goals and next order

This implemented slice does not:

- switch `OrchestratorKernel` to the durable path;
- invoke an Agent, plugin tool, MCP server, connector or external message API;
- run the heartbeat loop or call complete/fail on the returned attempt authority;
- write Artifact/result/terminal task state;
- enable retry or reinterpret a legacy receipt;
- claim exactly-once external effects.

The required next order is:

```text
canonical semantic admission + atomic start
  -> heartbeat-supervised pure/fake worker
  -> atomic Artifact/result/attempt/terminal acceptance
  -> receipt-bound recovery and reconciliation
  -> action receipts and real connectors
```

Real connectors remain prohibited until the final action-receipt boundary exists. In particular,
no Feishu or WeCom person, group, bot or webhook may be used as a test target.
