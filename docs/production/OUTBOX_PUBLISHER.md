# Transactional outbox publisher: production contract and operations

This document defines the Phase 1 production contract for
`quantum_entanglement.publisher.OutboxPublisher` and the SQLite outbox state it
owns. It is a release-candidate boundary, not a claim that every GA gate in
`RELEASE_GATES.md` is complete. Gates A–E all remain closed.

## Safety invariants

1. A domain event and its outgoing message are committed in one SQLite
   transaction.
2. A Publisher may ACK or NACK only the exact live lease it claimed. The SQL
   mutation fences on status, lease token, and an unexpired deadline in one
   statement.
3. The Store reads its trusted clock only after `BEGIN IMMEDIATE` acquires the
   write lock. A lock wait that crosses the deadline therefore cannot commit
   using a stale, pre-lock timestamp.
4. A Connector must be a native async callable and must return an explicit
   `PublishReceipt`. Truthy values, `None`, `False`, and stringly typed receipt
   states never authorize an ACK.
5. Timeout, caller cancellation, ACK uncertainty, and acceptance after lease
   expiry are quarantined in `outbox_ambiguities`. They are never automatically
   retried until an operator records evidence-backed resolution.
6. Delivery remains at-least-once. Every downstream transport must durably
   deduplicate `PublishRequest.idempotency_key` before causing a non-idempotent
   effect.

## Connector isolation and admission

Each Connector invocation runs in a daemon thread with its own asyncio loop.
This keeps a broken `async def` that calls blocking code from freezing the
Publisher loop. It also means Python cannot forcibly terminate a Connector that
ignores cancellation.

`max_callback_tasks` is the hard admission bound across active and abandoned
callbacks. A timed-out thread continues to occupy capacity until it actually
returns. Thread startup failure is converted to a normal failed attempt and its
reserved capacity is released. Shutdown reports abandoned callbacks and never
labels that state clean.

Connectors must:

- create loop-bound clients inside their invocation, or use a process-safe
  transport boundary;
- propagate the stable idempotency key to the broker;
- return `PublishReceipt.accepted(receipt_id)` only after durable downstream
  acceptance;
- return `PublishReceipt.rejected(reason_code)` for a definite rejection;
- avoid embedding secrets or user content in reason codes.

No built-in Connector sends to Feishu, WeCom, or any other external system.
Tests use local callbacks only.

## Lease time and SQLite contention

The Store owns the authoritative lease clock. Tests may inject a deterministic
Store clock at construction, but an individual ACK/NACK/claim request cannot
control the authoritative time. During the rolling-upgrade window the Store
still accepts the legacy `now=` keyword, but deliberately ignores it. The
transaction order is:

1. acquire the process lock;
2. execute `BEGIN IMMEDIATE` and wait for SQLite's write lock;
3. read and normalize the Store clock;
4. execute the fenced claim, ACK, or NACK;
5. commit.

The regression suite holds a real write lock with a second SQLite connection,
advances the Store clock across the lease deadline while the operation is
blocked, then proves claim uses the new time and ACK/NACK fail at expiry.

Configure `lease_seconds` greater than `publish_timeout`. Capacity planning must
also reserve time for local scheduling and ambiguity persistence. A process can
still crash after external acceptance and before local ACK; downstream
idempotency is the final duplicate barrier.

## Ambiguity reconciliation

Open ambiguities block takeover, ACK, and NACK. Read them with
`read_outbox_ambiguities()` and correlate the message's idempotency key with the
downstream broker's durable record. Only then choose one resolution:

- `published`: downstream evidence proves the effect committed;
- `retry`: downstream evidence proves no effect committed, or retry is safe by
  durable idempotency;
- `dead_letter`: policy forbids retry or the message requires incident review.

Resolution is an operator safety gate, not an automated timeout retry. The
current Store API does not itself authenticate operators; the service boundary
must authorize this call, retain actor/evidence in its audit log, and never
expose it directly to an untrusted client.

### Fencing-token data classification

The active `outbox.lease_token` is an internal write-fencing capability. It is
required only while a row is in flight and must not appear in application logs,
metrics, traces, generic exports, UI, or support bundles. ACK and terminal
operator resolution clear it. `PublishRequest`, its representation, and
`PublishRequest.to_dict()` deliberately omit it; Connector code never needs the
SQLite fencing capability.

Persistent ambiguity rows store only lowercase SHA-256
`lease_token_digest`. The digest is an internal correlation identifier, not an
authentication credential; it may be shown only in restricted reconciliation
views. The migration hashes legacy raw tokens in-process through the fixed
`qe_sha256` SQLite function. Neither input values nor exceptions containing
those values are formatted or logged.

## Schema migration 0003

Forward migration is packaged as:

- `src/quantum_entanglement/migrations/0003_outbox_ambiguities.up.sql`
- `src/quantum_entanglement/migrations/0003_outbox_ambiguities.down.sql`

The common runner validates every known ledger checksum. Stores select only
migrations whose schema dependencies they own; an attempt-only database does
not execute outbox migration 3, while it can still validate migration 3 if a
shared database already contains it.

Migration 3 deliberately rebuilds the table. It supports both:

- a version-2 database with no ambiguity table; and
- databases created by the pre-migration Publisher commits, whose table used a
  raw `lease_token` column and could contain open and resolved history.

Under the same `BEGIN IMMEDIATE` transaction it:

1. enables SQLite secure deletion;
2. materializes the exact legacy shape when the table is absent;
3. renames the legacy table;
4. creates the constrained digest-only table;
5. hashes and copies every open/resolved row;
6. clears plaintext fencing tokens from terminal outbox rows;
7. drops the legacy table and creates the open-row indexes;
8. records version, filename, checksum, and application time in the ledger;
9. commits atomically.

Invalid legacy state, including two unresolved rows for one message, fails the
unique open-row constraint. The entire table rebuild and ledger insert roll
back, leaving the version-2 legacy table recoverable for operator repair.

Before promotion, run the migration against a verified copy of every supported
database shape and retain:

```sql
SELECT version, filename, sha256, applied_at
FROM qe_schema_migrations ORDER BY version;

PRAGMA table_info(outbox_ambiguities);
PRAGMA foreign_key_check;
PRAGMA integrity_check;
PRAGMA wal_checkpoint(TRUNCATE);
```

Expected schema version is 4 after current Event Store initialization: migration 3 still
owns the ambiguity schema, while migration 4 adds the separate durable
`invocation_admissions` receipt table. The ambiguity table must contain
`lease_token_digest` and must not contain `lease_token`.

Ledger row 4 is also a validator-level downgrade fence. In the current retained test, the
current validator is given a v3-only registry; it treats row 4 as newer, raises
`MigrationVersionError`, and leaves the ledger and schema unchanged. This is not yet a
historical v3 wheel running in an independent process; that mixed-wheel/process matrix is
still required. Deleting row 4 or the admission table to make a v3 binary start is
unsupported.
After all processes using the copied database are stopped, checkpoint/truncate
the WAL and verify backup/support-export policy no longer carries obsolete raw
tokens. Existing backups cannot be retroactively scrubbed; apply retention and
access-control policy to them.

## Rollback

Prefer a forward fix or a schema-compatible application rollback. Migration 3
is intentionally one-way for token data: SHA-256 digests cannot reconstruct raw
lease tokens.

The supplied down migration drops `outbox_ambiguities` and deletes ledger row 3. This
discards reconciliation history and is acceptable only in a rehearsed rollback after all
open ambiguities have been resolved or exported to a secured incident record. It must not
run while ledger row 4 remains: deleting row 3 beneath row 4 creates a rejected migration
hole. Reaching an exact v3 rollback target from a current v4 database first requires a
separately approved admission-state rollback/restore decision; this outbox runbook does
not authorize dropping durable admission receipts.

Required destructive-rollback sequence:

1. stop admission, publishers, and reconciliation workers;
2. verify there are no active Publisher processes or live leases;
3. create and validate an online backup;
4. export the ambiguity count and evidence-backed disposition without raw
   fencing tokens;
5. prove the exact selected database has no migration row later than 3, then execute the
   packaged down migration;
6. run foreign-key and integrity checks;
7. start the prior binary and run its smoke test;
8. restore the backup or forward-fix immediately if verification fails.

The deterministic suite rehearses the down migration from an exact v3 state and checks
schema version, table removal, foreign keys, and database integrity. That test is evidence
of mechanics, not authorization to run a destructive rollback in production.

## Monitoring and alerts

Export counts and durations, never payloads, headers, receipt IDs, lease tokens,
or exception strings. Minimum signals are:

- claim cycles, empty polls, messages claimed/published/retried/dead-lettered;
- callback timeout and admission rejection;
- lease conflict, lease expiry, ACK failure, and Store error;
- open ambiguity count and oldest age;
- reconciliation persistence failure;
- active/abandoned Connector and active DB task counts;
- shutdown state and `shutdown_clean`.

Page on any reconciliation persistence failure, sustained Store errors, or
unbounded oldest ambiguity age. Alert on abandoned-callback capacity saturation,
dead-letter growth, and repeated lease expiry. Logs use fixed safe categories
such as `transport_unavailable`, `connector_failure`, and
`invalid_publish_receipt`; exception text is neither logged nor persisted.
Custom classifiers may return only a constructor-time allowlisted fixed code and
are never coerced to text. Operational failures use the typed event catalog in
[`LOGGING_AND_REDACTION.md`](./LOGGING_AND_REDACTION.md), with worker/message
identifiers hashed and no traceback. Public outbox serialization and repr omit
the lease token; the Store/Publisher CAS path retains typed internal access.

## Release verification

The phase gate requires all of the following before promotion:

- focused delivery and Publisher tests pass;
- full unit suite passes;
- Publisher timing suite passes repeatedly to detect races;
- format, syntax, lint, and `git diff --check` pass for changed files;
- clean v2 upgrade, populated legacy upgrade, checksum drift, atomic failed
  rebuild, and down migration tests pass;
- wheel contains every migration SQL file;
- a downstream idempotency integration test and deployment smoke test pass in a
  non-production environment;
- an operator runbook names the owner for open ambiguities and dead letters.

## Deliberate residual limits

- SQLite is a single-node durable boundary; multi-node production deployment
  requires a database implementation with equivalent CAS and migration tests.
- At-least-once delivery cannot remove the downstream deduplication requirement.
- Python daemon threads cannot be force-killed. Admission is bounded and leaks
  are visible, but a hostile Connector should run behind a process boundary.
- Authentication, authorization, and audit evidence for operator reconciliation
  belong to the enclosing service and remain a release blocker until integrated.
