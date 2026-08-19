# Durable artifact storage

## Status and supported boundary

`SQLiteArtifactStore` is the Phase 1 single-node persistence boundary for immutable
artifact bytes and version metadata. It is suitable for controlled internal pilots on
one host when callers provide an authenticated tenant/workspace scope and the database
is backed up as one SQLite unit.

This module closes three defects in the original in-memory `ArtifactLedger`:

- content no longer has to live only inside event JSON;
- version allocation is serialized by SQLite across processes;
- metadata and content are committed atomically and verified on every read.

It does **not** by itself make the orchestration runtime production-ready. In particular,
the invocation terminal transition, artifact write, action receipt, and result event are
not yet one atomic transaction. Until that integration exists, an operator may need to
reconcile a successful artifact write after a worker crashes before acknowledging its
attempt.

## Data model

The packaged migrations create two append-only tables.

### `artifact_blobs`

| Column | Meaning |
|---|---|
| `digest` | `sha256:` plus 64 lowercase hexadecimal characters |
| `content` | Exact immutable bytes |
| `byte_size` | Length checked by SQLite |
| `created_at` | Store-owned RFC 3339 UTC time |

Blobs are content addressed. Reusing identical bytes in the same supported storage
scope does not create a second physical copy.

### `artifact_versions`

Every row binds:

- tenant, workspace, session, task, artifact name, and creator;
- monotonic version and immediate parent version;
- media type, byte size, blob digest, and canonical JSON metadata;
- idempotency key and a SHA-256 request fingerprint;
- store-owned creation time.

The uniqueness constraints prevent two writers from claiming the same version and
prevent an idempotency key from being rebound to different work.

## Write algorithm

`write()` performs the following work inside `BEGIN IMMEDIATE`:

1. Check for an existing scoped artifact identity or idempotency key.
2. Return the original record only when its request fingerprint is identical.
3. Read the current scoped artifact head.
4. Enforce the optional `expected_head_version` compare-and-set precondition.
5. Insert the content-addressed blob, or verify the existing blob byte-for-byte.
6. Insert the immutable version row with `parent_version = version - 1`.
7. Read the joined row back and verify all integrity invariants.
8. Commit both blob and metadata together.

Any exception rolls the transaction back. A stale writer therefore cannot leave an
orphaned blob, and a process crash cannot expose only half of a new artifact version.

## Idempotency semantics

The idempotency key is scoped to tenant and workspace. Its request fingerprint covers:

- tenant, workspace, session, and task;
- artifact name and media type;
- blob digest and byte size;
- canonical metadata;
- creating principal.

The generated `artifact_id` is deliberately excluded. A retry that generates a new
opaque ID still receives the original record. Reusing either the original artifact ID
or idempotency key for changed content, metadata, scope, task, or creator raises
`ArtifactConflictError`.

An identical retry is resolved before checking `expected_head_version`. This is
intentional: a client that lost the original response can recover its committed result
even after later versions have advanced the head.

## Concurrency and crash behavior

SQLite permits one writer at a time. Independent processes can calculate versions only
after acquiring the write lock, so concurrent writes produce distinct contiguous
versions. An optional expected-head precondition turns revision into optimistic CAS.

| Failure point | Observable result after restart |
|---|---|
| Before `BEGIN IMMEDIATE` | No state change |
| After idempotency lookup | No state change |
| After blob insert, before version insert | Transaction rollback; no orphan blob |
| After version insert, before commit | Transaction rollback; neither row visible |
| After commit, before caller receives response | Same idempotency key returns original record |
| After a later writer commits | Stale expected head is rejected |

The test suite exercises independent connections and two spawned processes writing the
same artifact name concurrently.

## Integrity verification

Every returned artifact is reconstructed from a metadata/blob join and checked for:

- SHA-256 of the actual bytes equals `blob_digest`;
- blob and version byte counts both equal the actual length;
- metadata is valid, finite JSON with string object keys;
- the request fingerprint recomputed from stored fields is unchanged;
- version and parent version form a valid immediate lineage;
- creation time is a strict, timezone-qualified RFC 3339 timestamp.

Failure raises `ArtifactIntegrityError`; corrupted bytes are never returned as a valid
artifact. This protects against accidental storage damage and unsophisticated tampering.
It is not a keyed authenticity proof against an attacker who can rewrite the database
and recompute every digest. A later secure deployment phase must add encrypted storage,
key management, and tamper-evident audit anchoring.

## Input and resource limits

Defaults:

- maximum content: 16 MiB;
- maximum canonical metadata JSON: 64 KiB;
- maximum identifier/name/idempotency field: 512 characters;
- maximum media type: 255 characters;
- history page: 100 records by default, 1,000 maximum.

Content must be immutable `bytes`; implicit text encoding and mutable `bytearray` inputs
are rejected. Metadata must be a plain dictionary composed only of JSON primitives,
lists, and dictionaries with string keys. NaN and infinities fail closed. Limits are
checked before the write transaction and raise `ArtifactTooLargeError`.

These defaults are safety ceilings, not a final capacity claim. Operators should lower
them for workflows that do not need large artifacts and move large binary assets to a
future encrypted object-store implementation.

## Scope and isolation

Every read and write requires both `tenant_id` and `workspace_id`. Lookup with the wrong
scope returns no record rather than revealing whether an artifact exists elsewhere.
History and integrity scans are also scope-bound.

The initial Phase 1 schema was introduced before the full Phase 2 isolation migration.
The production release gate requires the following additional proof before declaring a
shared multi-tenant boundary:

- artifact identity is a composite scoped key, not a globally conflicting ID;
- blob rows and foreign keys carry tenant/workspace scope;
- equal bytes in different tenants do not create an observable cross-tenant dedup edge;
- randomized duplicate IDs work independently across tenants and workspaces;
- backup, metrics, errors, and integrity scans reveal no other tenant cardinality.

Until that forward migration and its tests are present, run this store only in the
single-tenant Phase 1 boundary even though the API already requires scope fields.

## API example

```python
from quantum_entanglement import ArtifactWrite, SQLiteArtifactStore

with SQLiteArtifactStore("/var/lib/quantum-entanglement/state.sqlite3") as store:
    artifact = store.write(
        ArtifactWrite(
            tenant_id="tenant-123",
            workspace_id="workspace-456",
            session_id="session-789",
            task_id="task-review",
            name="review.md",
            content=b"# Review\n\nApproved.",
            media_type="text/markdown",
            metadata={"sourceCount": 12},
            created_by="agent-reviewer",
            idempotency_key="task-review:review.md:final",
        ),
        expected_head_version=0,
    )
```

Callers must persist and reuse the same idempotency key for retries. They must not derive
it from mutable wall-clock time or a newly generated request ID.

## Migration and rollback

Migration SQL is shipped inside the wheel and recorded in `qe_schema_migrations` with a
SHA-256 checksum. Startup refuses checksum drift and database versions unknown to the
binary. Artifact-only startup selects its domain migrations and still validates any
other already-applied registry entries in a shared database.

The `.down.sql` files are destructive operational tools, not an automatic startup path.
Before rollback:

1. stop all writers and verify no invocation owns an active lease;
2. create and verify a transactionally consistent backup;
3. export artifact metadata and bytes with digests;
4. check the target schema's uniqueness preconditions;
5. execute the down migration on a restored copy first;
6. verify `PRAGMA integrity_check`, foreign keys, counts, and sample digests;
7. only then schedule the production rollback.

Dropping the artifact schema destroys artifact data. Do not run a down migration merely
to make an older binary start; prefer restoring the pre-upgrade backup.

## Backup and recovery requirements

Artifact rows share the service SQLite database and WAL. Copying only the main `.sqlite3`
file while writers are active is not a supported backup. The Phase 1 backup command must
use SQLite's online backup API or a coordinated checkpoint and must include:

- database schema and migration ledger;
- artifact blobs and version rows;
- events, attempts, inbox/outbox, approvals, and action receipts;
- a manifest with database digest, schema versions, counts, and backup time.

Restore is successful only after integrity check, foreign-key check, artifact scope scan,
event/projection rebuild, and count/digest comparison with that manifest.

## Verification commands

```bash
PYTHONPATH=src python3 -m unittest tests.test_artifact_store -v
ruff check src/quantum_entanglement/artifact_store.py tests/test_artifact_store.py
mypy --strict --python-version 3.9 --follow-imports=skip \
  src/quantum_entanglement/artifact_store.py
python3 -m compileall -q src tests
git diff --check
```

The committed suite covers atomic versioning, blob deduplication, idempotent retries,
payload conflicts, expected-head CAS, independent connection/process concurrency,
scope-bound reads, content and metadata tampering, size/type validation, bounded history,
and the public package export.

## Remaining integration work

- apply the full tenant/workspace blob and identity migration;
- write result artifact, invocation terminal CAS, result event, receipt, and outbox in one
  transaction or through a documented recovery protocol;
- replace event-embedded content on the service path with artifact references;
- implement online backup/restore and corruption quarantine;
- add encrypted object storage and tenant key management;
- add retention, legal hold, export, and deletion state machines;
- add capacity benchmarks for large histories and binary payloads;
- expose artifacts through authenticated, paginated API and streaming projections.
