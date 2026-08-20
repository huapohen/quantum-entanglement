# Workflow initialization durability contract

This document defines the implemented single-node boundary for creating a new
workflow. It is not a claim that task execution, remote effects, or multi-node
admission are end-to-end transactional.

## Atomic boundary

For a previously empty session, the kernel first builds a candidate `TaskGraph`
without publishing it. It then creates one ordered event batch containing:

1. `workflow.plan.created`;
2. the exact `task.created` manifest in plan order;
3. every deterministic initial `PENDING -> READY` transition.

`SQLiteEventStore.append_many` writes that whole batch in one `BEGIN IMMEDIATE`
transaction at an expected stream version. The live `_plans` and `_graphs`
projections are published only after the durable batch has committed. A normal
pre-commit exception therefore leaves no plan event, task event, transition, or
live in-memory candidate.

## Ambiguous return reconciliation

A storage wrapper can commit SQLite and then raise before returning to the
kernel. Treating that exception as proof of rollback would make a retry
ambiguous. The kernel instead reads the attempted sequence interval in pages of
at most 1,000 events and verifies:

- the stream ID and every sequence are contiguous;
- the number of stored events exactly equals the attempted batch;
- every event ID, type, actor, timestamp, payload, correlation, causation, and
  idempotency key matches canonical JSON for the attempted event.

Only a complete exact match is reconciled as committed. A missing, partial,
reordered, cross-stream, or field-modified interval preserves the original
exception and publishes no live plan or graph.

## Observation semantics

After durable commit and live projection publication, `EVENT_APPENDED` callbacks
run in durable sequence order for the stream. Individual callback exceptions
are logged and do not reverse the command or truncate later batch events.

`PLAN_CREATED` is also a post-commit observation. Its exception is logged and
cannot turn the committed initialization into an apparent safe-to-retry
failure. These in-process hooks remain best effort: they are not a durable
subscriber, retry queue, or correctness boundary.

## Crash and retry matrix

| Boundary | Observable result |
|---|---|
| Candidate graph construction fails | no durable or live session state |
| Before or during failed SQLite transaction | no batch member and no live candidate |
| Transaction commits and returns | exact batch and live plan/graph are available |
| Transaction commits, wrapper raises | exact paged reconciliation publishes the same live state |
| Only a prefix or changed batch is observable | original error; no live plan/graph publication |
| Event or plan observer raises | committed command remains successful; error is logged |
| Process exits after commit but before memory publication | restart rebuilds from the exact durable manifest |
| Same plan is submitted again | existing content must match exactly; no duplicate initialization |

## Recovery relationship

Restart recovery independently requires exactly one canonical plan event and an
exact task manifest after that event. It then replays legal task transitions
into a candidate graph and publishes recovered projections only after the full
history validates. Recovery does not repair or delete a partial initialization.

Legacy histories created by the old per-event initialization path remain
readable when their plan, full task manifest, and transitions are complete and
canonical. A torn legacy history fails closed and requires a versioned repair or
migration procedure; operators must not edit event rows manually.

## Verification

Targeted checks:

```bash
PYTHONPATH=src python3 -m unittest \
  tests.test_runtime \
  tests.test_recovery \
  tests.test_approval_atomicity -v
```

The regression suite injects pre-commit failure, commit-then-raise ambiguity,
multi-page exact reconciliation, partial-batch corruption, observer failure,
retry, and restart recovery. Repository-wide release gates still apply.

## Explicit non-guarantees

This boundary does not provide:

- a distributed session lock or cross-process workflow admission service;
- a configured maximum task count, graph depth, or cumulative plan byte budget
  beyond the event store's per-event JSON limits;
- atomicity between later task status, durable attempts, Agent execution,
  artifacts, result events, and external effects;
- reconciliation for a task left `RUNNING` by process failure;
- durable hook delivery, hook timeout, or plugin isolation;
- authenticated tenant identity or action-time connector authorization.

Until those boundaries are integrated and fault-tested, this guarantee supports
trusted single-process workflows with fake or read-only effects only.
