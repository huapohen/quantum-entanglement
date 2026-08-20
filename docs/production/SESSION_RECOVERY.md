# Session recovery resource and publication contract

This document defines the recovery boundary implemented by the single-process
orchestrator at `8049ac3`. It is a bounded, streaming reconstruction contract;
it is not crash reconciliation for an Agent invocation or an external effect.

## Durable source and live publication

For a session that is not already loaded, the event stream is the durable source
of the workflow plan, task graph, task statuses, and approval state. Recovery
builds a candidate graph and candidate approval indexes without inserting them
into the live kernel.

The candidate is published to `_plans`, `_graphs`, the approval queue, and the
task-scoped approved set only after the entire stream has been read and every
recognized state transition has passed validation. A corrupt first page and a
corrupt last page therefore have the same fail-closed result: the caller receives
`SessionRecoveryError` and no partial session projection becomes observable.

Artifact references are attached only after that validation succeeds. The
`ArtifactLedger` has its own startup replay contract; it is not made atomic with
session recovery by this slice.

## Bounded page source

Recovery reads by exclusive stream-sequence cursor. Each source call may return
at most 1,000 decoded `StoredEvent` objects and must return an immutable tuple.
Every event must belong to the requested stream and sequences must be contiguous.
A short page ends the replay. Reaching the exact event limit triggers a one-event
probe so a history at the limit is accepted while a history beyond it is rejected.

The default cumulative limits are:

| Resource | Limit | Measurement |
|---|---:|---|
| Events | 1,000,000 | decoded events in this session replay |
| Canonical event bytes | 256 MiB | UTF-8 bytes of each domain event envelope and payload |
| JSON allocation nodes | 5,000,000 | containers, values, and object keys in the decoded event JSON |

All three limits apply across the complete session, not independently to each
page. The event store's per-payload JSON limit remains an additional boundary.
Unknown event types do not affect the workflow projection, but they still consume
the event, byte, and node budgets because they occupy durable history and memory.

Exceeding a limit fails before the event is applied to the candidate. Recovery
does not truncate history, silently skip an event, or publish the valid prefix.

## Streaming replay

The orchestrator no longer assembles every decoded page into a session-sized
tuple. It validates one page, measures each event, and yields events directly to
the candidate state machine. The implementation retains at most the current
decoded source page plus bounded projection state such as task IDs and approval
indexes.

The stream contract validates:

- exactly one canonical `workflow.plan.created` event;
- an exact, canonical `task.created` manifest in plan order;
- task creation after the plan and before transition history;
- contiguous, legal task revisions and state edges;
- approval request, decision, and transition adjacency and envelope binding;
- the requested plan ID and complete requested plan content;
- cumulative resource limits even for event types the projection does not use.

The regression suite records page reads and transition applications and requires
the first transition application to occur before the final page read. This guards
against accidentally restoring a hidden fetch-all accumulator while preserving
late-page atomic publication tests.

## Failure and operator behavior

Recovery errors are integrity or capacity signals, not a request to retry in a
tight loop. An operator should stop admission for the affected session, preserve
the database, record a redacted error code and source version, and investigate on
a copy. The implementation does not edit, delete, compact, or repair the source
history.

Histories above a cumulative limit require an explicit future snapshot/archive
or capacity migration design. Raising constants ad hoc can turn a deterministic
failure into process memory exhaustion and is not an approved production repair.

## Compatibility, migration, and rollback

This change adds no table, column, or event-schema migration. A database below
all limits remains compatible. A previously accepted oversized history now fails
closed; this is an intentional resource-safety compatibility boundary.

Application rollback requires no database down migration because no durable bytes
changed. However, rolling back to a build before `2d3a3bd` removes cumulative
budgets, and rolling back before `8049ac3` restores session-sized accumulation.
Such a rollback is safe only for a quarantined, measured database and must not be
used as a general production workaround.

## Unreconciled invocation quarantine

If complete replay leaves any task durably `RUNNING`, recovery now raises
`SessionRecoveryError` before publishing the candidate plan, graph, approvals, or artifacts.
The current invocation-start/result events do not contain the full invocation ID, payload
digest, attempt ID/number, lease epoch, token digest, and result-receipt identity required by
`INVOCATION_RECOVERY_COORDINATION.md`. The runtime therefore cannot construct trusted recovery
evidence from that history.

The same assertion runs on every `OrchestratorKernel.run()` call even when that session is
already loaded in memory. This covers cancellation and other `BaseException` exits after the
`RUNNING` transition: the loaded graph remains quarantined, the second call appends no guessed
transition, and the Agent is not invoked again.

This quarantine is an intentional availability tradeoff. It converts the former silent stuck
projection into an explicit operator-visible integrity boundary, while guaranteeing that
session recovery neither calls the Agent again nor appends a guessed failure/completion. A
future versioned event decoder, durable receipt source, and receipt-bound projector must land
before any matrix decision is acted on by `OrchestratorKernel`.

## Verification evidence

Targeted verification:

```bash
PYTHONPATH=src python3 -m unittest tests.test_recovery
```

The suite covers exact and exceeded event counts, exact and exceeded cumulative
byte/node budgets, bounded pages, cursor continuity, stream-boundary violations,
interleaved streaming application, invalid late pages, no partial publication, restart
quarantine, and same-process cancellation quarantine without Agent reinvocation.

At implementation commit `8049ac3`, the repository-wide suite reported 627
passing tests. Locked Ruff 0.16.3 lint/format over `src`, `tests`, and `scripts`,
strict mypy over the 30 source modules, `compileall`, the deterministic group-chat
demo, and `git diff --check` also passed.

## Explicit non-guarantees

This slice does not provide:

- snapshots, compacted streams, upcasters, or sublinear recovery time;
- a wall-clock, CPU, SQLite-page, or per-tenant recovery quota;
- a bound for the separate global artifact-ledger startup replay;
- a distributed recovery lock or protection from two orchestrator processes;
- automatic reconciliation for a task left `RUNNING` after process failure (such a task is
  now explicitly quarantined before projection publication);
- durable attempt fencing, heartbeats, action receipts, or effect-unknown handling;
- an authenticated service endpoint, tenant-complete storage, or safe real connector.

Until those boundaries are implemented and fault-tested, recovery supports the
trusted single-process, fake-effect pre-production topology only.
