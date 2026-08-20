# Artifact ledger replay and admission contract

Status: implemented for the trusted local/CI kernel boundary; **not a production
artifact service or a process-memory guarantee**.

This document defines the event-backed `ArtifactLedger` contract implemented at
`2ba0caf` and its event-store prerequisites. It is distinct from
`SQLiteArtifactStore`: the ledger reconstructs orchestration-era text artifacts from
`artifact.versioned` events, while `SQLiteArtifactStore` is the tenant/workspace-scoped
repository intended to replace event-embedded bodies on a future service path.

## Durable source and atomic publication

The append-only global event log is the ledger's durable source. Startup rebuilds an
isolated candidate containing version chains, duplicate-artifact-ID detection, cumulative
usage, and the last observed global position. The live ledger receives one frozen state
pointer only after every page, payload, lineage edge, digest, URI, duplicate check, and
resource budget succeeds.

A malformed or over-budget late row therefore leaves the previous versions, usage, and
global high-water mark unchanged. Rebuild never publishes a valid prefix, truncates the
source, skips a malformed artifact event, or repairs durable bytes.

## Row-streamed global replay

`SQLiteEventStore.stream_all_page()` reads global positions by exclusive keyset cursor.
Each row is fetched under the store lock, its cursor is closed immediately, and the row is
decoded and consumed after releasing that lock. A caller-controlled iterator is never
yielded while the shared writer lock is held.

This prevents a 1,000-row page from being fetched and JSON-decoded before artifact budgets
run. If the first artifact exceeds a budget, the second row is not decoded. The source
still enforces at most 1,000 positions per requested page, strictly increasing positive
positions, a one-row exact-limit probe, and the event store's separate per-payload JSON
contract.

The stream is append-consistent, not a fixed read snapshot. An event committed immediately
after an empty read may be absent from that rebuild. A later artifact write supplies the
rebuild high-water mark as an atomic global-position compare-and-set, so it cannot append
from that stale projection.

## Cumulative limits

Defaults apply across the complete global replay:

| Resource | Limit | Measurement |
|---|---:|---|
| Global events | 1,000,000 | every decoded event position, including non-artifact events |
| Artifact versions | 100,000 | accepted `artifact.versioned` records |
| Artifact content | 256 MiB | cumulative UTF-8 content bytes |
| Canonical metadata | 64 MiB | cumulative sorted compact JSON UTF-8 bytes |
| Metadata nodes | 1,000,000 | containers, scalar values, and object keys |
| State data | 384 MiB | content, canonical metadata, and retained descriptor UTF-8 bytes |

The state-data limit is a logical data budget. It does not measure Python object headers,
dict/tuple slots, allocator fragmentation, the old state retained during copy-on-write, or
SQLite page-cache memory, so it must not be described as a 384 MiB RSS ceiling.

Per-version validation remains additive: content is at most 16 MiB, canonical metadata at
most 1 MiB, metadata at most 10,000 allocation nodes and 64 nesting levels, and text fields
have explicit character limits. Metadata accepts only finite JSON values with plain dicts,
lists, and text keys. Digest, canonical URI, task binding, parent version, timestamp, and
strict payload/ref shapes are revalidated during every replay.

Changing these constants is a compatibility and capacity decision. Raising them without
benchmarks can convert a deterministic rejection into memory or latency exhaustion;
lowering them can make an existing database fail startup.

## Durable write admission

`record()` captures and canonicalizes caller input before SQL, then uses this sequence:

1. Resolve an existing stream-local idempotency key before quota admission.
2. Verify that a retry exactly matches the durable request.
3. Build the next version against the current frozen ledger state.
4. Calculate the next cumulative usage and reject an over-limit write before append.
5. Append with the exact replayed global position as a transaction-local CAS.
6. On CAS conflict, rebuild from position zero, recompute usage, and retry at most eight
   times.
7. After commit, publish versions, usage, and high-water mark with one state-pointer swap.

Inside `BEGIN IMMEDIATE`, the event store checks an existing idempotency record first,
then verifies `MAX(global_position)`, then calculates the stream sequence and inserts. Two
ledgers using the same store or independent SQLite connections therefore cannot both admit
from the same stale usage snapshot. A racing exact retry can still recover its original
result even when its supplied high-water mark is stale.

Eight repeated global conflicts fail closed with a fixed admission error. The method does
not spin indefinitely under a busy global event log.

## Exact idempotency

The durable retry comparison binds:

- session, task, artifact name, and exact content;
- media type and canonical metadata JSON bytes;
- actor/creator, correlation ID, and causation ID;
- explicit trigger, or the default `create`/`revise` derived from the persisted version.

Canonical JSON byte equality deliberately distinguishes JSON numbers and booleans that
Python equality conflates: `1`, `1.0`, and `true` are different, as are `0.0` and `-0.0`.
Changing any bound field raises a fixed `ArtifactRecordError`; it never silently returns
the old artifact as though the request were identical.

The opaque generated artifact ID and store-owned creation time are not caller request
fields. An exact retry returns the original ID after later versions have advanced the
chain.

## Failure and recovery behavior

| Failure point | Durable and live result |
|---|---|
| Invalid caller input | rejected before event lookup or append |
| Existing key with changed request | fixed conflict error; no new event |
| Quota exceeded | rejected before append; current state unchanged |
| Another event wins before append | transaction CAS rejects; bounded rebuild and retry |
| Process exits before SQLite commit | transaction rollback |
| Commit succeeds before state publication | durable retry resolves the original event; a later CAS forces rebuild |
| Late replay corruption or budget failure | complete old frozen state remains published |

An operator must treat replay failure as integrity or capacity quarantine, preserve the
database and backup chain, and investigate a copy. Tight retry loops, history deletion,
or temporarily running an unbounded old binary are not approved repairs.

## Compatibility, migration, and rollback

This slice adds no table, column, migration, or event-payload field. Databases whose
histories satisfy the strict decoder and cumulative limits remain compatible. Behavior is
intentionally stricter in three cases:

- an oversized existing history now fails closed;
- a changed idempotent request now conflicts instead of returning an old result;
- concurrent writers must rebuild after any global-position change.

No database down migration is required for code rollback. Rolling back before `2ba0caf`
removes cumulative admission, exact retry comparison, and atomic ledger-state publication.
Rolling back before `fa0f51f` also restores page-wide decoded materialization. Such a
rollback is allowed only on a stopped, backed-up, measured database after confirming that
the target binary can read the exact history. It must not be used to bypass a safety limit.

## Verification evidence

The implementation is split into independently reviewable commits:

- `2eb75bd`: linear replay accumulation and one-time tuple freeze;
- `fa0f51f`, `ba7aa9a`, `40e5bbf`: row streaming, lock-order repair, and eager cursor close;
- `d213b77`, `c34c9a1`: durable idempotency lookup and global admission CAS;
- `2ba0caf`: cumulative budgets, exact retry, bounded conflict recovery, and atomic state.

Targeted verification:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_store_artifacts.py' -v
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_store_read_bounds.py' -v
```

At `2ba0caf` plus its prerequisite commits, the repository-wide Python 3.9 suite reported
671 passing tests. Locked Ruff 0.16.3 lint/format, strict mypy over 32 source modules,
`compileall`, the deterministic 25-event/3-artifact demo, and `git diff --check` also
passed. These are local synthetic verification results, not production capacity evidence.

## Remaining boundaries

- Bypassing `ArtifactLedger` and directly appending `artifact.versioned` can bypass write
  admission; the event type remains a trusted internal ownership boundary.
- Stream replay is not a fixed snapshot, and each conflict performs a full rebuild. High
  global write rates can cause bounded starvation and expensive repeated work.
- Each successful new chain key copy-on-writes the versions dict; live appends to one chain
  still concatenate its tuple. Capacity near 100,000 versions needs benchmarks or a
  persistent index.
- The frozen state contains an internal mutable dict. Repository code follows copy-on-write,
  but trusted in-process Python is not a sandbox against private-attribute mutation.
- CAS detects append high-water changes, not unsupported UPDATE/DELETE or manual insertion
  into historical gaps. Database files and migration authority remain trusted.
- The ledger has no tenant/workspace scope and retains text bodies in event JSON. The
  tenant-scoped `SQLiteArtifactStore`, authenticated repository composition, retention,
  backup/restore evidence, and artifact/result/attempt terminal transaction remain the
  target service path.

These limits keep every production Gate closed. This work establishes a bounded,
fail-closed local ledger contract; it does not make the complete product production-ready.
