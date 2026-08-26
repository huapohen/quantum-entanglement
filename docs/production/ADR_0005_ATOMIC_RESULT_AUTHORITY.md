# ADR 0005: Atomic result authority, scope, and migration boundary

- Status: accepted design; writers and worker dispatch remain disabled
- Date: 2026-08-27
- Decision owners: coordination kernel and durable execution boundary
- Related contracts:
  [`ATOMIC_INVOCATION_START.md`](./ATOMIC_INVOCATION_START.md),
  [`HEARTBEAT_SUPERVISED_PURE_WORKER.md`](./HEARTBEAT_SUPERVISED_PURE_WORKER.md),
  [`DOMAIN_SCOPED_MIGRATIONS.md`](../architecture/DOMAIN_SCOPED_MIGRATIONS.md), and
  [`SQLITE_BACKUP_MANIFEST_V2_CODEC.md`](../architecture/SQLITE_BACKUP_MANIFEST_V2_CODEC.md)

## Context

Atomic admission and first claim/start now commit a queued invocation, running attempt and
schema-2 start event in one SQLite transaction. They do not prove completion. The current runtime
uses an event-backed `ArtifactLedger`, appends `task.result.received`, and appends
`task.status.changed -> COMPLETED` through separate transactions. The standalone attempt-store
`complete()` changes only job/attempt rows and permits a missing `result_ref`.

Neither path can answer the restart question “was this exact result durably accepted with all of
its Artifacts and terminal task evidence?” A crash can expose a prefix of those writes. A caller
supplied `InvocationResultReceipt`, a succeeded attempt, a result event, or a COMPLETED task is not
individually trusted completion evidence.

The existing execution manifest schema 1 and start evidence schema 2 are also unscoped: they do
not carry explicit tenant and workspace identities. An opaque `authorizationDigest` cannot be
decoded later to prove that an Artifact/result row used the authorized scope. Legacy unscoped
evidence must not be silently upgraded into scoped authority.

## Decision

### 1. Canonical Artifact authority

The tenant/workspace-scoped SQLite tables owned by `SQLiteArtifactStore` are the canonical durable
Artifact authority for accepted invocation results:

```text
artifact_blobs
artifact_versions
```

The event-backed `ArtifactLedger` remains a legacy/demo projection. It may render historical
events but is not written in parallel by the atomic result boundary and cannot authorize worker
completion. There will be one durable write path, not two truth sources.

Artifact preparation and persistence will be extracted into caller-owned transaction primitives.
The future result acceptor owns the single SQLite connection, lock, transaction, clock sample,
fault classification and commit acknowledgement. Passing the same database path to two store
instances is not atomic and is prohibited for this boundary.

### 2. Scope-bearing execution evidence

A new exact execution-manifest schema 2 will add `tenantId` and `workspaceId` to the complete
domain-separated digest. A corresponding start-evidence schema 3 will copy and bind those fields.
The version numbers do not rewrite the existing schemas:

| Evidence | Existing | New scoped form | Promotion status |
|---|---:|---:|---|
| execution manifest | 1 | 2 | only schema 2 is eligible |
| invocation start event | 2 | 3 | only schema 3 is eligible |
| result accepted event | none/legacy `task.result.received` | 2 | only exact schema 2 is eligible |
| result manifest | none | 2 | only exact schema 2 is eligible |
| result receipt | caller value object only | 2 | only store-owned schema 2 is eligible |

Existing schema-1/schema-2 start evidence remains readable for audit and the current disabled
scaffold. It is `legacy_unscoped` for result acceptance and worker promotion. No decoder, migration,
repair job or current authorization state may invent tenant/workspace values for it.

Scope is part of every identity, digest, unique key, foreign-key relationship and query predicate:

```text
tenant + workspace + session + plan + task + agent
+ invocation + job idempotency
+ attempt ID/number + lease epoch/token digest + worker
+ start receipt coordinates
+ execution/result manifest digests
```

### 3. Canonical result authority

The only completion authority is a store-owned `InvocationResultAccepted` bundle reconstructed
from a valid result receipt and its complete durable graph. The proposed API is:

```text
accept_invocation_result(
    exact ResultAcceptanceRequest,
    exact ScopedInvocationStartClaimed,
    expected_stream_version
) -> InvocationResultAccepted | InvocationResultObserved

read_invocation_result(scoped invocation identity)
    -> Optional[InvocationResultObserved]

read_invocation_recovery_bundle(scoped task identity)
    -> one-snapshot verified durable graph
```

`Accepted` may contain the still-live lease only inside the acknowledged call if a later internal
step requires it; it is never serializable or replayable. `Observed` is capability-free. Lost
commit acknowledgement, reopen, peer-process replay and recovery can return only an observation.

The result transaction makes this set visible all-or-nothing:

```text
active scoped lease/start revalidation
+ Artifact blobs and versions
+ canonical bounded result manifest bytes
+ durable result receipt
+ task.invocation.result.accepted schema-2 event
+ invocation job and attempt success CAS
+ exact RUNNING -> COMPLETED task.status.changed event
+ result-ready outbox rows
```

The receipt records both stored-event coordinate sets. Task has no SQL authority row; its durable
terminal authority is the exact terminal event bound into this transaction and receipt.

### 4. Result versus action receipt

An Agent result receipt proves only that this system accepted the Agent output. It does not prove
that an external receiver applied an action. The result manifest includes an exact effect class
and action-receipt-set digest:

- `pure`: the action receipt set must be the canonical empty set;
- other effect classes remain ineligible until durable external action receipts exist;
- an unknown receiver outcome becomes `effect_unknown`, never accepted success or automatic retry.

Feishu, WeCom, bots, webhooks and all real connectors remain prohibited throughout this phase.

### 5. Migration identity and topology

Legacy migration IDs 1–4 are immutable. The next global migration identity is reserved only when
the native executor is ready:

```text
migration_id: 5
domain: invocation_results
domain_version: 1
kind: native
```

This is a design coordinate, not permission to add `0005` to the legacy bootstrap registry today.
Before registration, the release must include:

1. native/sparse dependency planning and a default-deny executor;
2. bridge and mixed-binary fleet-floor enforcement before mutation;
3. exact installed-wheel SQL/descriptor/down-guard evidence;
4. backup manifest v2 create/verify/quarantine-restore support;
5. topology registry coverage for every result object and relation;
6. non-empty receipt-graph restore reconciliation.

The minimum native topology is:

```text
invocation_result_manifests
  PK (tenant_id, workspace_id, manifest_digest)
  schema_version, canonical_bytes, byte_size, created_at

invocation_result_receipts
  receipt identity and schema version
  complete scoped invocation/start/attempt/fence binding
  result ref and manifest digest
  result-event and terminal-event coordinates
  accepted_at, idempotency key and request digest
  UNIQUE scoped invocation
  UNIQUE attempt
  UNIQUE scoped idempotency key

invocation_result_artifacts
  PK (receipt_id, ordinal)
  scoped artifact identity, version, digest, byte size, media type and name
  FK to receipt and artifact version
```

Dependencies are explicit on the attempts, Artifact and admission domains plus the EventStore base
schema precondition. Runtime `CREATE TABLE IF NOT EXISTS`, fake ledger rows, empty migration files,
or appending ID 5 to legacy bootstrap are forbidden.

## Exact replay and ambiguity

One scoped logical invocation and one attempt each have at most one accepted result. An exact retry
must match every request field, canonical manifest byte and Artifact input. A differing reuse of an
invocation, receipt, Artifact, event or idempotency identity is conflict, not last-write-wins.

If `COMMIT` may be durable but its acknowledgement is lost, the store reopens/reads the exact
candidate graph. It returns observation only after every row, digest, event coordinate, Artifact,
job and attempt matches. Any partial, advanced, missing or contradictory graph causes ambiguity,
store poisoning and reconcile-only operation. The handler is never invoked to resolve ambiguity.

## Standalone API fences

Before the writer is promoted:

- canonical scoped jobs must be structurally rejected by standalone `complete()`;
- the generic runtime must be unable to append canonical result/COMPLETED events directly;
- result receipt constructors and decoded wire values are observations, never trusted authority;
- no public `trusted=True`, feature boolean, caller connection or caller transaction escape hatch is
  introduced.

Compatibility APIs for legacy/demo workflows stay clearly named and cannot create scoped canonical
evidence.

## Required proof before writer enablement

The retained matrix must cover:

- exact codecs: future/legacy schemas, unknown/missing fields, bool-as-int, NFC, timestamps, size,
  ordering, mutation and cyclic metadata;
- every manifest and scope binding mismatch;
- every SQL statement, BEGIN/COMMIT/ROLLBACK acknowledgement and exact interpreter control signal;
- stale/expired lease, late result, stream drift, duplicate/conflicting replay;
- two connections and two spawned processes accepting one result;
- kill before/during/after Artifact, receipt, result event, attempt CAS, terminal event and commit;
- raw lease/credential canaries across repr, JSON, exceptions, SQLite/WAL/SHM, backup and restore;
- full and sparse migration, old/new binary, rollback/down guard and installed-wheel package data;
- non-empty v2 backup/restore plus orphan, partial and tampered receipt graphs;
- receipt-bound restart projection that never reruns an accepted handler.

Passing unit tests does not itself enable the worker or close a production Gate. Promotion is an
isolated commit after source-bound packages and all required evidence are retained.

## Consequences

Positive:

- completion has one auditable authority and one atomic visibility boundary;
- tenant/workspace confused-deputy risk is addressed in evidence rather than inferred later;
- ACK loss and process death have an exact recovery answer;
- fake/pure worker enablement can be independently reviewed and rolled back.

Costs:

- result delivery waits for domain-native migration and backup-v2 readiness;
- schema-1 starts cannot be reused as production dispatch authority;
- ArtifactStore transaction code requires careful extraction and new process/ambiguity hardening;
- the legacy runtime remains a non-production demo until it is cut over.

## Rejected alternatives

- **Call `complete()` then write the result:** may create succeeded/no-result state.
- **Write Artifacts first on another connection:** exposes a committed prefix and cannot roll back.
- **Treat `task.result.received` as a receipt:** caller/generic event coordinates do not prove the
  complete graph.
- **Infer scope from current authorization:** current state can drift and the digest is opaque.
- **Dual-write ArtifactLedger and SQLiteArtifactStore:** creates two authorities and new split brain.
- **Add migration 5 to legacy bootstrap:** bypasses sparse dependency, fleet-floor and backup-v2
  release safety.
- **Enable the worker because handlers are pure:** a label or code-review claim is not durable
  completion or retry evidence.
