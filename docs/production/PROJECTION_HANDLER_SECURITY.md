# Projection handler database capability boundary

This document defines the SQLite capability given to a projection handler. It protects
framework-owned database state from handler SQL; it is not a Python plugin sandbox and does
not make arbitrary handler code safe to run in the orchestrator process.

## Transaction and capability lifetime

`SQLiteProjectionOffsetStore.apply_event()` invokes the handler inside the same SQLite
transaction that advances the projection receipt and offset. The handler receives a
`ProjectionTransaction`, not the raw connection. That object is thread-affine and becomes
permanently closed before the restricted SQLite authorizer is restored.

On normal return, ordinary exception, `BaseException`, or revoke interruption:

1. the capability is revoked and force-closed;
2. a failed apply transaction is rolled back;
3. the restricted SQLite authorizer is replaced by an explicit framework callback;
4. an escaped transaction or bound method rejects before issuing SQL.

The authorizer installation itself is inside the cleanup boundary. A connection wrapper
may install the restricted callback and then raise before returning; cleanup still restores
framework access. This matters because treating the wrapper exception as proof that SQLite
did nothing can leave the shared connection accidentally restricted.

Python 3.9 cannot reliably clear an authorizer with `None`, so the framework installs an
explicit allow callback. Reinstalling a callback also invalidates statements compiled under
the previous authorization context.

## SQL allowed to handlers

Within its active capability, a handler may:

- read and mutate non-framework tables;
- create, alter, index, reindex, analyze, and drop non-framework tables/indexes;
- return bounded copied statement results to its Python code.

This supports ordinary materialized projection tables. Table naming and inter-handler
ownership remain an application design responsibility; this layer protects framework
tables, not one business projection from another.

## SQL denied to handlers

The authorizer denies:

- `ATTACH`, `DETACH`, `PRAGMA`, transaction control, and savepoints;
- read, insert, update, or delete against framework-owned tables;
- schema changes targeting framework tables, indexes, or `qe_*` objects;
- every create/drop operation for persistent or temporary VIEW and TRIGGER objects;
- every create/drop operation for VIRTUAL TABLE objects.

Framework tables include events, snapshots, inbox/outbox, ambiguity records, invocation
jobs/attempts, artifact blobs/versions, projection offsets/receipts, migration registry,
revocation high-water, and SQLite's sequence table.

Views, triggers, and virtual tables are denied regardless of their name. They persist or
defer executable SQL/module behavior beyond the callback that created them. Allowing a
handler to create one while restricted and execute it later after restoring framework
authority would make authorization depend on SQLite compilation timing and statement cache
behavior. Materialized tables plus explicit handler code are the supported alternative.

## Schema integrity relationship

On construction, the projection store inspects an exact bounded catalog for its two owned
tables and receipt-position index. It validates canonical DDL and stable `table_info`,
`index_list`, `index_info`, and `index_xinfo` fields. Shadow views, extra indexes/triggers,
wrong collations, partial schemas, malformed result shapes, or wrapper-induced transaction
ambiguity fail closed before normal projection work.

The handler authorizer is defense during an apply transaction; exact constructor validation
is defense for durable schema state. Neither silently repairs a modified database.

## Compatibility, migration, and rollback

No database schema migration is introduced by this authorization change. Existing handlers
that create/drop a VIEW, TRIGGER, temporary variant, or VIRTUAL TABLE now receive a SQLite
authorization error and their apply transaction rolls back.

Before upgrading such a deployment:

1. inventory projection handler SQL without printing customer values;
2. replace deferred schema programs with explicit handler logic and materialized tables;
3. replay the projection on a backup copy and compare receipts, offsets, and output digest;
4. deploy only after the replacement passes fault and idempotency tests.

Application rollback needs no down migration, but a rollback re-enables the old deferred
program surface. It is not an approved workaround for an incompatible handler; keep the
database quarantined and migrate the handler forward.

## Verification

Targeted verification:

```bash
PYTHONPATH=src python3 -m unittest tests.test_projections
```

Tests cover framework reads/writes/schema attempts, transaction-control denial, handler
exception and `BaseException`, capability construction/revoke interruption, cross-thread and
escaped use, post-install authorizer failure, deferred VIEW/TRIGGER/VTABLE operations,
receipt idempotency, lease fencing, exact schema validation, and rollback.

At the integrated implementation baseline, 73 projection tests and the repository-wide
655-test suite passed together with locked Ruff 0.16.3, strict mypy, compileall, dependency
lock verification, the deterministic demo, and `git diff --check`.

## Explicit non-guarantees

The SQLite authorizer does not:

- sandbox handler Python, imports, filesystem, network, subprocess, CPU, memory, or threads;
- prevent a handler from corrupting another handler's non-framework table;
- validate business meaning or acceptance criteria of projected data;
- make user-defined SQLite functions or extensions safe;
- provide tenant scope for projection offsets, receipts, or business tables;
- replace process isolation, least-privilege OS identity, action-time policy, or code review.

Projection handlers therefore remain trusted in-process code. Only synthetic/fake data is
permitted until the broader service boundary and worker-isolation gates are closed.
