# ADR 0005: Atomic result authority, scope, and migration boundary

- Status: accepted design; writers and worker dispatch remain disabled
- Date: 2026-08-27
- Decision owners: coordination kernel and durable execution boundary
- Related contracts:
  [`ATOMIC_INVOCATION_START.md`](./ATOMIC_INVOCATION_START.md),
  [`HEARTBEAT_SUPERVISED_PURE_WORKER.md`](./HEARTBEAT_SUPERVISED_PURE_WORKER.md),
  [`DOMAIN_SCOPED_MIGRATIONS.md`](../architecture/DOMAIN_SCOPED_MIGRATIONS.md), and
  [`SQLITE_BACKUP_MANIFEST_V2_CODEC.md`](../architecture/SQLITE_BACKUP_MANIFEST_V2_CODEC.md)

> 2026-08-27 sequencing addendum: ADR number `0005` is unchanged, but SQL migration identity 5
> is reserved for the earlier native-IM durable inbox and migration 6 is registered for its
> sandbox-admission provenance. Atomic invocation results therefore move to migration 7;
> native-IM actions use migration 8. This addendum changes ordering, not the result authority
> semantics in this ADR.

> 2026-08-28 implementation checkpoint: Phase 1 now has a private, capability-free
> stored-event-envelope V1 codec, a raw `sqlite3.Row` reconstruction path, frozen golden bytes and
> a Python 3.9/3.12/3.13 read-only verifier. This is only the codec primitive. The store-owned
> `_EventWriteSnapshot` adapter, reserved-event fence, same-transaction write/read comparison,
> result migration 7, writer, `AcceptedV2` mint point and worker dispatch remain absent and disabled.

> 2026-08-28 M2 checkpoint: every caller-controlled generic event append now passes a
> store-owned reserved-vocabulary fence before `BEGIN`, and standalone `complete()` structurally
> rejects scoped durable admissions before reading the clock or issuing DML. M3 stored-event
> snapshot/raw-row comparison, migration 7, writer, `AcceptedV2` and worker dispatch remain absent
> and disabled.

> 2026-08-29 M3 checkpoint: a private, fresh-only adapter now freezes exact typed result/terminal
> snapshot bytes, performs one isolated event INSERT, reads a fixed 11-column raw `sqlite3.Row`
> inside the owning transaction, and requires write/raw fields, canonical bytes and digest to
> match. Trigger replacement/extra-row effects, idempotent replay, storage-class drift, caller
> mutation and classified exception-graph disclosure fail closed. This is still only an insert-time
> adapter: result migration 7, Artifact transaction primitives, atomic pair writer/final readback,
> receipt, `ObservedV2`, `AcceptedV2` and worker dispatch remain absent and disabled.

> 2026-08-29 M4 checkpoint: migration 7 now exists only as an inactive, isolated-rehearsal
> candidate with six result tables, eight explicit indexes and a private backup-topology profile;
> neither legacy bootstrap nor the active migration/backup registries can reach it. A private exact
> owner-transaction handle can write an ordered, bounded Artifact batch on the EventStore-owned
> SQLite transaction, verify every blob/version row, bind all Artifact SQL to `main`, reject any
> unexpected main/TEMP topology, and force rollback-only after any contained write failure. Existing
> version history is streamed in fixed batches: SQLite storage classes and byte bounds are checked
> before each bounded raw row is materialized, then canonical metadata, request digest, scope,
> lineage and UTC-microsecond time are recomputed. A writer-owned random savepoint proves that the
> same owner transaction survives clock sampling; `COMMIT`/`ROLLBACK` followed by a fresh `BEGIN`
> cannot masquerade as continuity. Confirmed rollback and ambiguous outcomes are cleanly
> distinguished; an ambiguous control signal carries `_ResultArtifactCommitAmbiguityError` as its
> direct cause and poisons the store. Exception-graph classification uses base descriptors, does not
> execute subclass attribute hooks, and does not revive a historical control suppressed with
> `from None`. Atomic result request/receipt/event/task/attempt publication, `ObservedV2`,
> `AcceptedV2`, migration registration and worker dispatch remain absent and disabled.

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

M4 extracts Artifact preparation and persistence into private owner-transaction primitives without
exposing the SQLite connection. The future result acceptor must own the same EventStore connection,
lock, transaction, clock sample, fault classification and commit acknowledgement. Passing the same
database path to two store instances is not atomic and is prohibited for this boundary.

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
    exact ScopedInvocationStartClaimed
) -> InvocationResultAccepted | InvocationResultObserved

read_invocation_result(scoped invocation identity)
    -> Optional[InvocationResultObserved]

read_invocation_recovery_bundle(scoped task identity)
    -> one-snapshot verified durable graph
```

`ResultAcceptanceRequest` includes the expected stream version inside its canonical request digest;
it is never an unbound side parameter. `Accepted` is a non-serializable proof that this call made a
fresh commit, but it does not retain the lease after the success CAS clears it. All post-commit work
is represented by outbox rows in the same transaction. `Observed` is capability-free. Lost commit
acknowledgement, reopen, peer-process replay and recovery can return only an observation.

The stored coordinate for each result-authority event is additionally bound to a private canonical
stored-event envelope with the following exact V1 body:

```text
schemaVersion, eventId, streamId, eventType, actorId, timestamp,
correlationId, causationId, idempotencyKey, payload, sequence, globalPosition
```

Its digest is
`SHA-256("quantum-entanglement.stored-event-envelope/1\n" || canonical-json-body)`. The codec
accepts exact canonical JSON object payload text, exact positive signed-64-bit coordinates and UTC
microsecond timestamps; it rejects duplicate keys, non-finite numbers, alternate JSON bytes,
SQLite storage-class drift, subclasses and malformed raw rows. It is private and serializable only
as capability-free data: neither constructing an envelope nor presenting its digest proves an
INSERT, COMMIT acknowledgement, acceptance or authorization.

Phase 1 intentionally does not dispatch on result event type. The future private writer adapter
must first validate the exact typed result/terminal payload contract, then build the write-side
envelope from the store-owned frozen snapshot. It must independently reconstruct the read-side
envelope from an explicit raw-row projection inside the same transaction and compare them. Generic
historical event rows are not silently upgraded: their older timestamp/text contract can be wider
than the new reserved result-event path.

The schema-2 result manifest preserves the complete provider-neutral result body rather than only
describing materialized Artifacts:

```text
resultRef                 stable logical result identity; never an Artifact alias
narration                 bounded canonical UTF-8 result text
metadata                  bounded canonical JSON object (at most 65,536 bytes)
primaryArtifactId         optional; when present, names one descriptor
artifacts                 ordered tuple containing zero through 256 descriptors
```

A narration-only result is valid and does not create a synthetic Artifact. `resultRef` is the
stable value written to the invocation job and attempt; it remains unchanged whether the result has
no Artifact, one primary Artifact or several supporting Artifacts. Artifact IDs, names and
idempotency keys are unique within one result, and the first writer version forbids two descriptors
with the same name even when their proposed versions differ. The manifest carries only immutable
post-derivation descriptors. Raw bytes, canonical metadata bytes, expected head versions and
Artifact request identities live in exact content candidates covered by the acceptance request.

The value codec may decode future effect-bearing manifests for audit compatibility, but the first
acceptor mechanically admits only `effectClass=pure` with the canonical empty action-receipt set.
No label, caller boolean or non-empty receipt digest relaxes this promotion boundary.

Constructing or decoding a manifest proves only that a capability-free value has the exact
canonical shape: `codec-valid` is not `request-eligible`, and neither is `durably accepted`. The
acceptance request rejects non-pure input before persistence, and the store independently
revalidates the pure/empty-set binding while holding the exact active start capability.

The result transaction makes this set visible all-or-nothing:

```text
active scoped lease/start revalidation
+ zero or more Artifact blobs and versions
+ canonical bounded result manifest bytes
+ durable result receipt
+ task.invocation.result.accepted schema-2 event
+ invocation job and attempt success CAS
+ exact RUNNING -> COMPLETED task.status.changed event
+ result-ready outbox rows
```

The receipt records both stored-event coordinate sets and both envelope digests. Readback must
recompute each digest from the exact raw row rather than trust a caller, a read model or a digest
column on `events`; no such column is added. Task has no SQL authority row; its durable terminal
authority is the exact terminal event bound into this transaction and receipt.

### 4. Result versus action receipt

An Agent result receipt proves only that this system accepted the Agent output. It does not prove
that an external receiver applied an action. The result manifest includes an exact effect class
and action-receipt-set digest:

- `pure`: the action receipt set must be the canonical empty set;
- other effect classes remain ineligible until durable external action receipts exist;
- an unknown receiver outcome becomes `effect_unknown`, never accepted success or automatic retry.

Feishu, WeCom, bots, webhooks and all real connectors remain prohibited throughout this phase.

### 5. Migration identity and topology

Legacy migration IDs 1–6 are immutable. The early native-IM integration path uses migration 5 for
`native_im_inbox` and migration 6 for `native_im_sandbox_provenance`. The result graph described by
this ADR uses the next identity only when the native executor is ready:

```text
migration_id: 7
domain: invocation_results
domain_version: 1
kind: native
```

This is a design coordinate, not permission to add `0007` to the legacy bootstrap registry today.
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
  result-event and terminal-event coordinates plus both stored-event envelope digests
  accepted_at, idempotency key and request digest
  UNIQUE scoped invocation
  UNIQUE attempt
  UNIQUE scoped idempotency key
  UNIQUE (tenant_id, workspace_id, result_ref)

invocation_result_artifacts
  PK (receipt_id, ordinal)
  scoped artifact identity, version, digest, byte size, media type and name
  FK to receipt and artifact version
```

Dependencies are explicit on the attempts, Artifact and admission domains plus the EventStore base
schema precondition. Runtime `CREATE TABLE IF NOT EXISTS`, fake ledger rows, empty migration files,
or appending ID 7 without its release evidence are forbidden.

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

M2 freezes the generic event rule more precisely:

- exact `task.invocation.result.accepted` is always rejected, regardless of payload;
- for exact `task.status.changed`, only root payload keys are examined; each key is normalized with
  `NFKC`, `casefold`, then `NFKD`, and reduced to ASCII alphanumerics;
- any one skeleton equal to `transitionkind`, `resultreceiptid`, `resulteventid`,
  `resultevidencedigest`, `runningtaskrevision`, or `terminaltaskrevision` is rejected before typed
  decoding, so malformed, partial and near-canonical shapes cannot fall through;
- nested keys, string values and different event types are outside this terminal namespace; the
  legacy five-field status event remains writable but does not gain Result Authority;
- `_snapshot_event` is the private pure snapshot primitive consumed by M3; caller-controlled paths
  use the separate class-qualified `_snapshot_generic_event`, so M3 needs no public bypass flag.

M3 freezes the private insert contract more precisely:

- only exact typed `ScopedInvocationResultEvidenceV2` and
  `ScopedInvocationResultTerminalTransitionV2` payload bytes are accepted;
- the result event binds stream, orchestrator actor, `acceptedAt` timestamp, non-null
  correlation/causation and acceptance idempotency; the terminal event binds stream, actor,
  correlation, result-event causation and task/revision idempotency;
- one verified insert must be fresh and have zero trigger/FK side effects: top-level `changes()` is
  exactly 1 and connection `total_changes` increases by exactly 1;
- after INSERT, a fixed raw-row projection is independently decoded and must equal the write-side
  envelope by fields, canonical bytes and digest;
- classified contract, integrity and concurrency failures are reissued from a clean boundary with
  no payload-bearing inner traceback; no public writer, capability or wildcard-visible symbol is
  added.

The M3 check occurs immediately after each event INSERT. The future atomic writer must repeat final
verification of both result and terminal rows after every Artifact/receipt/outbox/attempt/task DML
and before COMMIT. Pair-level timestamp, coordinates, evidence digest and receipt binding are not
proven by the single-event adapter.

Standalone completion does not infer scope from `max_attempts`, identity prefixes or an opaque
digest. Inside the same write transaction it loads the durable job and classifies bounded candidate
admission/start events through receipt coordinates, canonical idempotency and exact payload
`invocationId`. Exact schema-2 execution or schema-3 start evidence raises
`InvocationCompletionPathReservedError`; scoped-like partial or drifted structure raises
`InvocationIntegrityError`. The check happens before clock access and DML. Exact schema-1 evidence
and databases that only own attempt migrations retain their legacy behavior.

## Required proof before writer enablement

The retained matrix must cover:

- exact codecs: future/legacy schemas, unknown/missing fields, bool-as-int, NFC, timestamps, size,
  ordering, mutation and cyclic metadata;
- stored-event envelope golden bytes/digest on Python 3.9/3.12/3.13, exact raw `sqlite3.Row`
  projection/storage classes, every coordinate/payload-leaf mutation, typed result payload
  re-encoding and write-snapshot/raw-row equality inside the owning transaction;
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
- **Add result migration 7 to legacy bootstrap:** bypasses sparse dependency, fleet-floor and backup-v2
  release safety.
- **Enable the worker because handlers are pure:** a label or code-review claim is not durable
  completion or retry evidence.
