# Inactive Invocation Results Schema V1

- Status: design frozen for an inactive migration-7 candidate
- Date: 2026-08-29
- Activation: **disabled**
- Public writer/reader: **absent**
- Active legacy migration registry: versions `1..6` only
- Related ADR: [`ADR_0005_ATOMIC_RESULT_AUTHORITY.md`](../production/ADR_0005_ATOMIC_RESULT_AUTHORITY.md)

## 1. Purpose and stop line

This schema is the future durable graph for one scoped, pure invocation result. It exists in M4 so
the SQL, migration descriptor, Artifact foreign keys, event coordinates, fixed publication identity
and backup topology can be reviewed before a writer exists.

The candidate must not be appended to `migrations.MIGRATIONS`, loaded by legacy store bootstrap,
accepted by bridge-only planning, or made reachable from a public API. Explicit tests may use a
private candidate rehearsal on an isolated database. A rehearsal is not activation and is not the
future native migration executor.

M4 does not create `ObservedV2` or `AcceptedV2`, update job/attempt/task state, send an outbox
message, or authorize a worker. A database merely containing these tables has no accepted-result
authority. A later writer must create and verify the complete graph in one owner transaction and
receive an unambiguous COMMIT acknowledgement.

## 2. Frozen migration coordinate

The candidate uses the coordinate frozen by ADR 0005:

```text
migration_id: 7
filename: 0007_invocation_results.up.sql
domain: invocation_results
domain_version: 1
kind: native
dependencies: [1, 2, 4]
enabled: false
```

Migration 1 owns jobs/attempts, migration 2 owns Artifact blobs/versions and migration 4 owns scoped
invocation admission. `events` and `outbox` are EventStore core objects, not migration-3 objects;
their exact topology is a separate component precondition. Migration 7 does not depend on migration
3.

The private candidate manifest records `enabled=false` and the EventStore component precondition.
The existing `DomainMigrationDescriptor` deliberately has neither field, so the descriptor alone
must never be treated as activation permission.

## 3. Durable graph

```mermaid
erDiagram
    INVOCATION_RESULT_MANIFESTS ||--o{ INVOCATION_RESULT_REQUESTS : exact_manifest
    INVOCATION_JOBS ||--|| INVOCATION_RESULT_REQUESTS : invocation
    INVOCATION_ADMISSIONS ||--|| INVOCATION_RESULT_REQUESTS : scoped_start
    INVOCATION_RESULT_REQUESTS ||--|| INVOCATION_RESULT_RECEIPTS : accepted_graph
    INVOCATION_ATTEMPTS ||--|| INVOCATION_RESULT_RECEIPTS : fenced_attempt
    EVENTS ||--o{ INVOCATION_RESULT_RECEIPTS : result_and_terminal
    INVOCATION_RESULT_RECEIPTS ||--o{ INVOCATION_RESULT_ARTIFACTS : ordered_artifacts
    ARTIFACT_VERSIONS ||--|| INVOCATION_RESULT_ARTIFACTS : materialized_version
    INVOCATION_RESULT_RECEIPTS ||--|| INVOCATION_RESULT_PUBLICATIONS : fixed_publication
    OUTBOX ||--|| INVOCATION_RESULT_PUBLICATIONS : exact_outbox_row
```

SQL foreign keys prevent structural orphans. They do not prove that legacy parent rows carry every
tenant/workspace binding, or that child counts equal the receipt count. The future writer and reader
must load fixed raw projections in one snapshot and reject any missing, extra, reordered, advanced or
cross-scope row.

## 4. `invocation_result_manifests`

One canonical manifest preimage per scoped digest:

- composite primary key `(tenant_id, workspace_id, manifest_digest)`;
- exact `schema_version = 2`;
- canonical bytes as SQLite BLOB, exact byte size and bounded creation timestamp;
- maximum canonical size 1 MiB.

Readback must decode the bytes with exact `ScopedInvocationResultManifestV2`, require byte-for-byte
canonical re-encoding, recompute the domain-separated digest and verify every scope field. The row
never stores Artifact content or a capability.

## 5. `invocation_result_requests`

One deterministic acceptance-request identity per scoped invocation:

- scoped composite primary key `(tenant_id, workspace_id, request_digest)`;
- schema version, acceptance idempotency key and exact canonical request-identity bytes;
- invocation/session/plan/task/agent and job idempotency scope;
- start-receipt, execution-manifest and result-manifest digests;
- expected stream version plus running/terminal task revisions;
- correlation, causation and runtime revisions;
- pure effect class and canonical empty action-receipt-set digest;
- result reference, optional primary Artifact ID and exact Artifact count;
- foreign keys to the owning job, admission and scoped manifest.

Uniqueness covers scoped invocation, scoped session/task, scoped acceptance idempotency and scoped
result reference. The future readback still verifies the complete job/admission payload because the
legacy parent tables do not contain tenant/workspace columns.

## 6. `invocation_result_receipts`

One capability-free receipt graph per request:

- scoped receipt and request identity;
- exact attempt ID/number, lease epoch, worker and lease-token digest;
- accepted timestamp, result reference, Artifact count and evidence/transition/receipt digests;
- result and terminal event IDs, types, timestamps, stream sequences, global positions and envelope
  digests;
- foreign keys to the request, job attempt and both exact event coordinate tuples.

Migration 7 owns a composite unique event index over
`(event_id, stream_id, sequence, global_position, event_type, timestamp)`. Each receipt event foreign
key targets that tuple, preventing a receipt from combining coordinates from different event rows.
The result event must be `task.invocation.result.accepted`; the terminal event must be
`task.status.changed`, immediately follow it in stream/global order and share `accepted_at`.

Receipt uniqueness covers the scoped invocation, attempt, scoped acceptance idempotency and scoped
result reference. The receipt stores no plaintext lease token, `fresh` flag, accepted boolean or
other durable capability.

## 7. `invocation_result_artifacts`

One row per ordered Artifact descriptor/candidate:

- scoped primary key `(tenant_id, workspace_id, receipt_id, ordinal)`, with ordinal `0..255`;
- exact Artifact ID/name/version/parent/media type/blob digest/byte size;
- metadata, Artifact request and in-process candidate digests;
- created-by and Artifact idempotency identities;
- foreign keys to the receipt and the exact scoped `artifact_versions` row.

Migration 7 owns a composite unique index over
`artifact_versions(tenant_id, workspace_id, artifact_id, version)` so the child foreign key binds
scope, identity and version to one raw parent row. Continuous ordinals, exact child count, primary
Artifact binding and equality with both manifest descriptors and raw Artifact rows remain mandatory
whole-graph readback checks.

## 8. `invocation_result_publications`

Publication is a closed, fixed one-to-one graph edge, not an optional set:

- one row per receipt, with exact `publication_kind = 'result_terminal_outbox_v1'`;
- scoped receipt identity, outbox message ID, destination and idempotency key;
- canonical payload/header digests, triggering terminal event ID/global position and creation time;
- foreign keys to the receipt and one exact outbox identity tuple.

Migration 7 owns a composite unique outbox index over message ID, destination, idempotency,
triggering event coordinates and creation time. The receipt codec does not currently self-digest
publication identity, so a receipt row alone must never prove the complete graph. M5 must derive the
fixed publication deterministically and final-read both the publication and raw outbox row before
COMMIT. Missing or extra publication is a partial graph, not success. Actual publishing remains
disabled.

## 9. SQL and codec invariants

- all identifiers are non-empty bounded SQLite TEXT with exact storage class checks;
- every SHA-256 field is exactly 64 lowercase hexadecimal characters; blob digests use canonical
  `sha256:<hex>`;
- schema versions are exact `2`; Artifact count is `0..256`;
- coordinates and revisions are exact positive signed-64 integers at the Python boundary;
- canonical JSON/preimage columns are bounded SQLite BLOB; Artifact content exists only in
  `artifact_blobs`;
- timestamps are canonical UTC microseconds at the Python/write boundary and stored as 27-byte
  SQLite TEXT;
- no permanent trigger is introduced;
- all candidate foreign keys use `ON UPDATE RESTRICT ON DELETE RESTRICT`;
- Artifact scope, event coordinates and outbox identity use candidate-owned composite unique indexes;
- the down script first uses a temporary CHECK guard to reject any non-empty candidate graph, then
  drops only candidate-owned objects. It cannot delete accepted rows silently.

## 10. Registration and compatibility fences

- active `MIGRATIONS`, `LEGACY_DOMAIN_MIGRATIONS`, `DOMAIN_MIGRATION_REGISTRY`, active backup
  topology and backup-v1 core tables remain byte-for-byte unchanged;
- default `SQLiteEventStore` installs schema 1–6 only and creates no result table;
- the independent known-candidate registry is metadata, not an executor;
- existing bridge planner/applier cannot receive the candidate;
- an old binary opening a rehearsed v7 database fails closed before mutation;
- generic result-event and standalone scoped-completion fences remain unchanged;
- no package-root export, public writer, connection parameter or transaction escape hatch is added.

## 11. Activation gates

Migration 7 remains inactive until all are retained:

1. exact descriptor, DDL and topology Golden evidence;
2. empty/non-empty candidate rehearsal, rollback/down guard and foreign-key evidence;
3. default-deny sparse/native executor with sidecar metadata, dependency and fleet-floor checks;
4. backup-v2 snapshot, restore, tamper and reconcile support for every new object;
5. Artifact same-transaction primitives and rollback/orphan/crash evidence;
6. M5 atomic writer with final whole-graph readback and fault injection;
7. M6 replay/reopen/ACK-loss recovery returning only `ObservedV2`;
8. an isolated migration-registration commit after compatibility review.

Registering migration 7, opening a writer or enabling worker dispatch in the same commit is
forbidden.
