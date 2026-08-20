# Session recovery resource and publication contract

This document defines the recovery boundary introduced at `8049ac3` and the
subsequent fail-closed publication hardening in the single-process orchestrator.
It is a bounded, streaming reconstruction contract; it is not crash
reconciliation for an Agent invocation or an external effect.

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
session recovery or task-result publication by this slice. The baseline ledger
uses expected session-stream sequence; the integrated advanced ledger preserves
its stronger global-position CAS and exact idempotency lookup. Both profiles can
reconcile only the exact event at the uniquely expected next durable position if
an append wrapper raises after commit. A pre-commit failure never publishes the
candidate artifact to the live ledger.

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

In-memory transition publication now follows durable commit. `TaskGraph.preview_transition`
and `preview_refresh` validate and describe changes without mutation; the kernel atomically
appends the exact transition event(s), reconciles an append wrapper that raised after commit,
then applies the same revision to memory. A pre-commit append failure therefore leaves memory
at the durable previous state instead of creating a false loaded-`RUNNING` quarantine. Ordinary
single runtime events use the same expected-version path: after an ambiguous append exception,
the kernel accepts only a byte-canonical match for the exact event at the expected next stream
position. A missing or different event is not guessed to have committed.

| Transition caller | Durable unit before memory publication |
|---|---|
| plan initialization and initial readiness | one existing atomic initialization batch |
| dependency refresh | one atomic batch of all previewed refresh transitions |
| policy denial | `READY→RUNNING→FAILED` in one atomic batch |
| ordinary dispatch | one reconciled `READY→RUNNING` event before context/Agent work |
| approval request | existing atomic running/waiting/request batch |
| completion or caught failure | one reconciled terminal event before graph mutation |
| approval decision | existing atomic decision/transition batch |

If a terminal transition append fails before commit after an Agent may have run, both durable
and in-memory state remain `RUNNING`; the next call correctly quarantines it as effect unknown.
If the wrapper raises after commit, exact event reconciliation publishes the committed state
once without duplicating the transition.

The Agent call and result publication are separate failure domains. An exception raised while
constructing context, publishing `task.invocation.started`, running the pre-Agent hook, or
awaiting the Agent follows the existing caught-failure contract and attempts a durable `FAILED`
transition. Once the Agent returns, however, an `AFTER_AGENT` hook failure, artifact or
result-event publication failure, or unreconciled completion-transition exception escapes while
the task remains durably and in memory `RUNNING`. The next `run()` call quarantines the session
before another Agent invocation can occur.

Artifacts are currently appended before `task.result.received`, without a shared transaction.
A crash or pre-commit result-event failure can therefore leave a durable artifact version while
the result event is absent. The ledger preserves that version, but `_task_artifacts` is not
published until `task.result.received` is durably committed or exactly reconciled. Consequently,
downstream tasks cannot consume a partial result through the live task projection, and the
owning task remains quarantined rather than being replayed. This is a safety stop, not automated
artifact/result reconciliation, a trusted invocation receipt, or an external-effect transaction.

Parallel ready-task batches are drained before `run()` releases the per-session lock. A normal
sibling exception is collected while every already-started sibling settles; only then is the
first exception re-raised. Thus one task's post-Agent publication failure cannot leave another
task mutating the graph or event stream in an orphan background coroutine after the caller has
observed `run()` return. A sibling may legitimately finish during that drain, while the failed
publisher remains `RUNNING` and quarantined. If the caller cancels the batch, the kernel cancels
unfinished local tasks and awaits all of them before unlocking; cancellation still does not prove
that a remote Agent or connector produced no effect.

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
quarantine, same-process cancellation quarantine without Agent reinvocation, pre/post-commit
transition and ordinary-event fault injection, atomic policy denial, non-mutating dependency
refresh retry, artifact append reconciliation, and post-Agent artifact/result publication
quarantine without task-reference publication. Multi-task coverage also proves a publication
failure cannot release the session lock while sibling dispatches remain live.

At implementation commit `8049ac3`, the repository-wide suite reported 627
passing tests. Locked Ruff 0.16.3 lint/format over `src`, `tests`, and `scripts`,
strict mypy over the 30 source modules, `compileall`, the deterministic group-chat
demo, and `git diff --check` also passed.

At the later invocation-recovery hardening checkpoint `d3b92c3`, the same-process
cancellation case was included in 665 repository-wide tests passing under both the default
Python and Python 3.13. That historical checkpoint has since been superseded by additional
clock, append-reconciliation, and publication-fault coverage. The final evidence for this
slice is recorded only after the current release gates complete. This evidence proves
quarantine behavior only; it does not claim automated reconciliation or safe Agent retry.

## Explicit non-guarantees

This slice does not provide:

- snapshots, compacted streams, upcasters, or sublinear recovery time;
- a wall-clock, CPU, SQLite-page, or per-tenant recovery quota;
- a bound for the separate global artifact-ledger startup replay;
- a distributed recovery lock or protection from two orchestrator processes;
- automatic reconciliation for a task left `RUNNING` after process failure (such a task is
  now explicitly quarantined before projection publication);
- an atomic transaction spanning artifacts, `task.result.received`, and task completion;
- durable attempt fencing, heartbeats, action receipts, or effect-unknown handling;
- an authenticated service endpoint, tenant-complete storage, or safe real connector.

Until those boundaries are implemented and fault-tested, recovery supports the
trusted single-process, fake-effect pre-production topology only.
