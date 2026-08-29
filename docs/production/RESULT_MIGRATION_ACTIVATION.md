# Migration 7 activation runbook

Status: **implemented as an explicit candidate-schema activation kernel; default startup,
public result writing, AcceptedV2, worker dispatch and publication remain disabled.**

This runbook records the first migration gate after the M5 private result graph checkpoint.
It is intentionally local-first: Markdown and Git are the source of truth during development;
Notion synchronization is deferred until this checkpoint is closed and then verified by a
page-by-page readback.

## 1. Activation boundary

`activate_result_acceptance_migration(...)` in
`src/quantum_entanglement/result_migration_activation.py` is the only activation kernel.
It accepts either a fresh base database (the kernel first applies the exact active legacy
prefix) or an exact v6 database. It rejects a newer ledger, checksum drift, missing legacy
objects, altered sidecar, partial result catalog, and any active caller transaction.

The transition is explicit and opt-in:

```python
from quantum_entanglement.store import SQLiteEventStore

store = SQLiteEventStore(
    "event-store.sqlite3",
    enable_result_acceptance_schema=True,
)
```

The constructor first applies packaged migrations 1--6, then the activation kernel performs
one `BEGIN IMMEDIATE` transaction containing:

1. exact sidecar installation when absent;
2. legacy metadata/dependency bootstrap for migrations 1--6;
3. migration-7 DDL and its `qe_schema_migrations` ledger row;
4. native `invocation_results@1` metadata and dependency edges `7 -> 1, 2, 4`;
5. active-schema, sidecar, metadata, dependency, foreign-key and integrity validation;
6. commit followed by a fresh readback of the immutable state evidence.

The active registry digest is:

```text
a6d3433d53a19a35299b8968f00dd51d68a8f6785f8ab4913809cf9cc811fb02
```

The successful candidate state contains migration IDs `(1, 2, 3, 4, 5, 6, 7)` and dependency
edges `(4,1)`, `(6,5)`, `(7,1)`, `(7,2)`, `(7,4)`. The returned
`ResultAcceptanceMigrationState.state_sha256` is timestamp-free and can be retained in release
evidence.

## 2. Idempotent reopen

An already activated database is not rewritten. The candidate constructor validates the active
registry, exact migration-7 schema, sidecar DDL, all seven metadata rows and all dependency
edges, then returns a fresh `ResultAcceptanceMigrationState`. A repeated activation read does
not increase `connection.total_changes`.

The default constructor intentionally still uses the legacy registry. Opening an activated
database with `enable_result_acceptance_schema=False` raises the existing newer-schema gate;
this prevents an old binary from silently serving a database it cannot verify.

## 3. Safe rollback

Rollback is explicit and never part of ordinary startup:

```python
from quantum_entanglement.result_migration_activation import (
    rollback_result_acceptance_migration,
)

evidence = rollback_result_acceptance_migration(store._connection)
```

The down script has a durable empty-data guard. If any result manifest, request, event binding,
receipt, Artifact binding or publication exists, rollback aborts and confirms the transaction
rollback; it does not drop a prefix or delete result data. A dependent future migration also
blocks rollback. On an empty schema, rollback removes migration-7 objects and metadata, leaves
the exact bridged sidecar plus legacy metadata 1--6, and returns a timestamp-free
`ResultAcceptanceMigrationRollbackState`.

Rollback is not a downgrade authorization for a live service. Before a real deployment uses
it, the operator must stop writers/workers, take a verified backup, preserve the returned state
evidence, and prove restore and compatibility on a clean host.

## 4. Failure classification

- A failure while the writer transaction is still open is rolled back and reported as
  `ResultAcceptanceMigrationTransactionError`.
- A `COMMIT` exception after SQLite has closed the transaction is reported as
  `ResultAcceptanceMigrationCommitAmbiguityError`; the database must be reopened with the
  candidate mode and read back before any retry.
- A malformed or contradictory source/target is reported as
  `ResultAcceptanceMigrationIntegrityError`; the kernel never repairs it by guessing.

Error messages contain no API key, plaintext lease, provider response or raw result body.

## 5. What this gate does not prove

Migration activation makes the result tables reopenable; it does not promote result authority.
The following remain release blockers:

1. active backup/restore topology and migration-7 backup evidence;
2. crash-at-every-boundary, `kill -9`, dual-connection race, stale-worker fencing and restore
   replay evidence;
3. heartbeat-supervised pure/fake worker gate;
4. `AcceptedV2`, publication, real IM and all external outbound paths.

Receipt-bound, non-emitting reconciliation is implemented separately in
[`RESULT_RECONCILIATION.md`](./RESULT_RECONCILIATION.md). It remains opt-in and does not close
these release gates.

The current candidate still permits only the private result graph and capability-free
`ObservedV2` readback. No Feishu, WeCom, Notion, Yuque, webhook or external connector is called
by activation or rollback.

## 6. Verification

Focused checks:

```bash
PYTHONPATH=src pytest -q \
  tests/test_result_migration_activation.py \
  tests/test_event_store_recovery_snapshot.py \
  tests/test_scoped_invocation_recovery.py \
  tests/test_result_acceptance_durable_prerequisites.py
python -m ruff check src/quantum_entanglement/result_migration_activation.py \
  src/quantum_entanglement/store.py \
  src/quantum_entanglement/invocation_recovery.py
PYTHONPATH=src python -m mypy \
  src/quantum_entanglement/result_migration_activation.py \
  src/quantum_entanglement/store.py \
  src/quantum_entanglement/invocation_recovery.py
```

These checks prove the candidate transition and its local readback contract only. They are not
an approval to connect a production IM or enable outbound effects.
