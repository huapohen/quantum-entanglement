# SQLite backup v2 single-snapshot evidence derivation

## Status and release boundary

This checkpoint derives manifest-ready v2 schema and topology evidence from one SQLite
read transaction. It remains internal compatibility infrastructure and does not activate
manifest v2 for backup or recovery.

The implementation is intentionally isolated in `backup_snapshot_v2.py`:

- it accepts an already-open exact `sqlite3.Connection`;
- it opens and owns one deferred read transaction, derives all evidence, and rolls that
  transaction back before returning;
- it opens no path or descriptor and writes no database, backup, manifest, or sidecar;
- it does not publish a file, calculate the database-file SHA-256, or make a restore
  decision;
- it is not exported from the package root;
- `backup.py`, `create_sqlite_backup()`, `verify_sqlite_backup()`,
  `restore_sqlite_backup()`, and the admin CLI remain unaware of it and continue to
  support only `qe.sqlite-backup/1`.

Explicitly importing this versioned submodule initializes the manifest/topology/domain
registry stack. That initialization reads packaged migration `*.up.sql` resources to
cross-bind trusted descriptors and topology. A cold `import quantum_entanglement` does not
import the v2 stack or read those resources, and calls to the initialized derivation
function itself perform no filesystem I/O.

This stage proves that one trusted SQLite view can produce the exact models defined by
the [manifest v2 codec](SQLITE_BACKUP_MANIFEST_V2_CODEC.md). It does not prove that the
connection points at the same inode and bytes that a future writer will publish. That
binding belongs to the descriptor-owning v2 writer and verifier stages.

## Internal API

```python
derive_backup_manifest_v2_snapshot(connection) -> BackupManifestV2Snapshot
```

The result contains:

- exact positive `page_count` and supported power-of-two `page_size`;
- `BackupManifestV2SchemaState`, including exact applied timestamps;
- `BackupManifestV2RegistryTopology`, including present profiles, all exact catalog
  objects, and one row count for every present trusted table.

`BackupManifestV2Snapshot` reconstructs a complete synthetic `BackupManifestV2` during
validation. This reuses the top-level codec binding and prevents direct construction of a
snapshot whose schema state and registry topology are independently valid but belong to
different durable states.

The result does not contain `backupId`, creation time, database byte size, or database
SHA-256. A future writer must derive those from its retained temporary-file descriptor and
combine them with this result only after proving exact file identity and page geometry.

## Connection preconditions

The function rejects before `BEGIN` unless all of the following are true:

1. the value has exact type `sqlite3.Connection`, not a subclass or wrapper;
2. `row_factory` is `None` or the built-in `sqlite3.Row`;
3. `text_factory` is the exact built-in `str` factory;
4. no caller transaction is active.

Rejecting subclasses avoids touching caller-controlled descriptors before the boundary
is established. Restricting row and text factories prevents database values from being
materialized through arbitrary Python callbacks. Rejecting an existing transaction lets
the function prove which snapshot it opened and ended; it never commits or rolls back a
caller-owned transaction.

After those preconditions succeed, the function conservatively adopts transaction
ownership before it executes `BEGIN`. Both cleanup frames are already installed when the
SQLite call begins. A denied or non-opening `BEGIN` is accepted as unowned only after the
exact connection still reports no transaction. If SQLite has entered a transaction before
the call raises or before Python reaches its next line, the cleanup frames treat that
transaction as owned and roll it back.

The exact-connection check is necessary but not sufficient for future production use. A
caller can install authorizer, progress, trace, or conversion callbacks on an exact
connection, and this function cannot recover the PID/epoch in which the connection was
created. The eventual operational boundary must therefore:

- capture process ownership before creating the connection;
- create a fresh private read-only/immutable connection in the current process;
- install only approved callbacks and factories;
- guard process identity before touching the connection;
- close that connection under the same owner and lifecycle contract.

Until that owner is implemented, passing an inherited or externally configured connection
is outside the supported operational boundary even if the type checks succeed.

## One-snapshot sequence

The derivation order is fixed:

1. enter two nested structured cleanup frames, execute trusted literal `BEGIN`, and confirm
   that SQLite reports an active transaction;
2. run bounded `PRAGMA main.integrity_check(1)` and require the single exact result `ok`;
3. run `PRAGMA main.foreign_key_check` and require no result row;
4. read `main.page_count` and `main.page_size` inside the same transaction;
5. call `inspect_schema_state()`; because the outer transaction is active, its bridge
   reader reuses this snapshot and performs no independent rollback;
6. read the ordered legacy migration IDs and `applied_at` values when a migration prefix
   exists, then build exact v2 schema-state evidence;
7. read and classify the complete main-schema catalog;
8. count every table belonging to every classified present profile;
9. build and cross-bind exact registry-topology evidence;
10. roll back the owned read transaction and require that it is no longer active;
11. return only after cleanup succeeds.

All SQL statements are constant trusted literals except table-count statements. Those
identifiers come only from the frozen topology registry, whose ASCII identifier grammar
and digests are validated at import. No database- or caller-provided scalar is bound back
into SQL, so SQLite adapters cannot reinterpret durable values during this derivation.

The transaction is deferred, but the first integrity query establishes a read view before
later state, catalog, and count reads. In WAL mode, a concurrent writer may commit while
derivation is in progress; every later read continues to observe the original snapshot.

## Exact catalog classification

The reader fetches at most the total trusted object count plus one supported statistics
table. Each row must provide exact built-in SQLite values for:

- object type;
- object name;
- owning table name;
- SQL text or SQL `NULL`.

Explicit DDL is canonicalized only with the narrow trusted topology canonicalizer and then
SHA-256 hashed. It normalizes exactly SQLite's ASCII token whitespace outside protected
regions, preserves non-token Unicode whitespace and vertical tab, and preserves quoted
tokens plus line/block comments byte for byte. SQL `NULL` remains valid only where the
trusted registry expects a SQLite-created autoindex.

Every non-statistics coordinate must exist in exactly one trusted profile and match its
exact table coordinate and DDL digest. A profile is atomic: observing none of its objects
means absent; observing its complete object set means present; any partial set fails. An
unknown object, duplicate coordinate, missing object, weakened DDL, or profile dependency
drift fails closed.

The only catalog object outside the frozen application registry admitted by this
checkpoint is the exact SQLite-managed table:

```text
table sqlite_stat1 sqlite_stat1 CREATE TABLE sqlite_stat1(tbl,idx,stat)
```

Its canonical DDL digest must match. Other `sqlite_stat*` forms, including builds that
materialize `sqlite_stat4`, are currently unsupported and fail closed. Statistics rows are
query-planner metadata and are not application table-count evidence. Supporting another
SQLite build requires an explicit topology/compatibility change and test evidence; a broad
`name LIKE 'sqlite_stat%'` exclusion is forbidden.

## Table-count binding

For every table in every present profile, the function executes `COUNT(*)` using the
trusted registry identifier and records an exact non-negative SQLite integer no greater
than `2^63 - 1`. The resulting table-name set must be exactly the set of table objects in
the classified profiles.

The manifest model additionally requires:

- the migration-ledger row count to equal the number of applied migrations;
- sidecar metadata rows to agree with the schema shape;
- sidecar dependency rows to equal the durable dependency edges;
- topology registry/state digests to equal the schema-state digests.

Counts bind presence and cardinality, not row contents. Database-file SHA-256 and
authenticated custody remain necessary.

## Bounds and fail-closed behavior

Reads are bounded before constructing manifest models:

- integrity evidence: one row;
- foreign-key evidence: zero rows accepted, first violation is sufficient to fail;
- page geometry: one row per pragma;
- migration timestamps: the domain-migration maximum;
- catalog: trusted registry object count plus one exact `sqlite_stat1` table;
- each row shape: the exact expected column count.

The codec then re-applies all scalar, collection, digest, ordering, profile, state, and
topology limits. Unexpected row factories, text factories, scalar subclasses, oversized
results, malformed rows, unknown objects, invalid schema state, or a prematurely ended
transaction become a stable `BackupManifestV2SnapshotError` code.

Public errors use a fresh-error trampoline that clears `__context__`, including when the
function is called inside an active `except` or `__exit__` region. Exact originating
`KeyboardInterrupt`, `SystemExit`, `GeneratorExit`, or `asyncio.CancelledError` takes
priority over a cleanup control signal. The same originating control object and its
traceback are propagated with a bare re-raise. A cleanup control is used only when no
originating exact control exists. Subclasses are not accepted as exact controls.

Control provenance is captured by the lifecycle's own `except` boundary. An unrelated
control currently being handled by a caller is not evidence that the transaction body
originated that control and cannot suppress or reclassify a cleanup failure.

The inner cleanup owns the normal path. A second cleanup frame surrounds the complete
`BEGIN`-through-inner-cleanup interval and retries state inspection plus rollback after one
transient cleanup error or control. Runtime-state injection tests cover a single exact
control after `BEGIN` has taken effect, after body success or failure, at cleanup entry, and
after the real rollback has returned. These tests locate boundaries from exact connection
state and observed transaction effects, not source line numbers.

Rollback failure blocks a result. The function does not claim that a connection with
failed cleanup is reusable; the future owner must quarantine and close it. The nested
Python cleanup frames are not an atomic signal mask: repeated asynchronous controls or a
persistent state-inspection/rollback failure can interrupt or exhaust both cleanup
attempts. No result is returned in those cases, but this caller-connection checkpoint does
not own enough lifecycle state to quarantine the connection itself. The operational writer
must do so. The precise automated guarantee here is one injected asynchronous control, or
one transient cleanup fault followed by a successful fallback cleanup.

## Supported schema-state matrix

The derivation tests materialize and round-trip all eleven canonical bridge-only states:

| Applied legacy migrations | Supported shapes |
|---:|---|
| 0 | `sidecar_absent`, `empty` |
| 1 | `sidecar_absent`, `legacy_prefix`, `bridged_prefix` |
| 2 | `sidecar_absent`, `legacy_prefix`, `bridged_prefix` |
| 3 | `sidecar_absent`, `legacy_prefix`, `bridged_prefix` |

Migration 3's topology dependency requires the exact event-store core profile. The tests
materialize that owner before accepting the three-migration state. Native, sparse,
future, holey, partial-sidecar, and registry-drifted states remain rejected.

## Verification evidence

The focused suite currently covers:

- the full eight-profile, 58-object materialized database;
- all eleven current schema-state prefix/shape combinations;
- exact `sqlite_stat1` admission after `ANALYZE`;
- unknown objects, partial optional profiles, malformed ledger timestamps, and DDL drift;
- exact connection type, row/text factory, closed-connection, and caller-transaction
  rejection;
- active-exception public error detachment;
- exact `KeyboardInterrupt`, `SystemExit`, `GeneratorExit`, and `CancelledError` at the
  post-`BEGIN`, body-success, body-failure, cleanup-entry, and post-rollback boundaries;
- originating-control identity and traceback preservation, including precedence over a
  cleanup control;
- rejection of ambient handled controls as originating-control evidence;
- denied and non-opening `BEGIN`, transient cleanup error/control retry, final transaction
  state, and subsequent connection reuse;
- authorizer evidence that derivation performs no row or schema write;
- a real WAL race in which a writer commits a new projection row while the reader is
  paused before table counts: the result retains the old snapshot count while a later
  connection observes the committed new row;
- continued absence of v2 snapshot imports or symbols in the active v1 module;
- zero packaged-migration SQL reads during a cold package-root import, while explicit v2
  submodule initialization remains the documented cross-binding boundary.

Observed local focused verification:

| Gate | Result |
|---|---|
| Python 3.9 / 3.12 / 3.13 codec factory tests, warnings as errors | 34/34 each |
| Python 3.9 / 3.12 / 3.13 snapshot tests, warnings as errors | 15/15 each |
| Python 3.9 / 3.12 / 3.13 topology tests, warnings as errors | 17/17 each |
| Ruff lint and format | pass |
| strict mypy for codec + snapshot modules | pass |

These are local source checks. Independent adversarial review, a clean combined checkout,
full-suite gates, immutable CI evidence, file/inode binding, and operational recovery
rehearsal remain required before integration or promotion.

## Rollback

No database or backup format is written by this stage. Rollback removes the internal
snapshot module, tests, model factories, and documentation together. Existing v1 database
and backup bytes require no migration or restoration.

Once a writer publishes v2 bytes, this rollback ceases to be safe: releases must retain a
compatible reader/quarantine verifier for stored v2 identities or block promotion until
those artifacts leave the recovery set under approved retention policy.

## Remaining activation sequence

The next fail-closed stages are:

1. descriptor-owned v2 creation and no-overwrite publication, binding database SHA-256,
   exact byte size, inode identity, permissions, temporary files, and directory `fsync` to
   this snapshot evidence;
2. independent quarantine verification of canonical manifest bytes and exact database
   bytes through retained descriptors;
3. exact-byte restore to a new path followed by the same quarantine checks;
4. process-owner integration, fork/spawn/forkserver evidence, callback allowlisting, and
   connection quarantine on ambiguity;
5. mixed v1/v2 binary, crash-point, rollback/forward-fix, reconciliation, and measured
   RPO/RTO rehearsal;
6. authenticated custody/signature and key-rotation policy.

Until those stages pass independently and in combination, manifest v2 remains unavailable
for operational backup and recovery.
