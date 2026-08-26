# Domain-scoped SQLite migration sidecar

Status: **phase-1 bridge planner and atomic applier implemented; release integration still blocked**

Decision scope: preserve the current immutable global migration prefix numbered 1–4,
including invocation-admission migration 4 and its semantic dependency on migration 1;
introduce a domain-aware sidecar and deterministic dependency planner; and make a later
sparse artifact migration possible without forcing an artifact-only database through
unrelated delivery and admission migrations.

Last reviewed against source: 2026-08-26

This document distinguishes committed bridge primitives from the remaining release design.
The implementation status below is authoritative for this checkout; later sections describe
the complete target and are not proof that a native/sparse executor, store integration, or
release gate exists.

### Implementation status

Implemented and tested in `src/quantum_entanglement/domain_migrations.py`:

- a bounded trusted registry for immutable packaged migrations 1–4, their domains,
  dependencies, packaged SQL digests, and owned-schema manifests;
- exact validation and atomic installation of the two format-1 sidecar tables;
- single-snapshot classification of absent-sidecar, empty, unbridged legacy-prefix, and
  bridged-prefix states, with partial/future/holey/drifted states rejected;
- atomic, idempotent legacy metadata bootstrap for an already installed exact sidecar;
- immutable, timestamp-free, registry-bound `SchemaState` inspection;
- a pure deterministic `plan_bridge_migrations` planner whose closed action set contains
  only `INSTALL_SIDECAR`, `BOOTSTRAP_LEGACY_METADATA`, and no-op;
- `apply_bridge_migration_plan`, which validates the complete plan model, obtains one
  `BEGIN IMMEDIATE` writer transaction, re-derives the canonical plan from the locked
  `source_state_sha256`, applies only the two allowlisted bridge actions, checks the exact
  post-write state, commits, and re-reads the committed state before returning.

Not implemented and therefore release-blocking:

- native/domain-sparse migration writes, the proposed artifact migration at ID 5 or later,
  or a sparse-aware legacy runner;
- runtime enforcement of non-migration topology prerequisites by `SchemaState`, the bridge
  reader/planner, or the legacy migration runner;
- operational domain-aware backup-v2 creation, verification, restore, and reconciliation;
- mixed-binary rollout evidence, service/admin integration, and a promoted phase-1 release.

The sidecar installer and metadata bootstrap remain separately callable atomic primitives,
and the bridge applier now composes them into one revalidated transaction. A plan object is
still inert data: only a successful `apply_bridge_migration_plan` result whose committed
`SchemaState` is retained as evidence proves that the bridge transition occurred. The applier
intentionally cannot execute native SQL, write a sparse ledger, or promote a release.

## 1. Current implementation: authoritative facts

The current runner is
[`src/quantum_entanglement/migrations/__init__.py`](../../src/quantum_entanglement/migrations/__init__.py).
It has useful safety properties that the new design must preserve:

- `Migration` contains a positive integer `version` and packaged `.up.sql` filename;
- the packaged registry is immutable in practice through filename and SHA-256 validation;
- `qe_schema_migrations` records version, filename, checksum, and application time;
- each migration body and its ledger row commit together under `BEGIN IMMEDIATE`;
- another initializer is rechecked after obtaining the SQLite write lock;
- `BaseException`, statement failure, validation failure, and commit failure roll back;
- the validator rejects a weak ledger, unknown/newer versions, checksum drift, ledger
  holes, and missing or changed migration-owned tables/indexes;
- schema queries explicitly use `main`, preventing a temporary table from shadowing the
  ledger;
- packaged SQL is read from wheel resources and split with SQLite's completeness parser.

The same implementation also creates the coupling this proposal addresses:

1. `MIGRATIONS` is one global ordered sequence.
2. `_validate_registry` requires exact integer versions `1..N`.
3. `target_versions` must be a continuous prefix of that global sequence.
4. `validate_sqlite_schema` and `current_schema_version` reduce the database to one integer
   head and reject a sparse ledger.
5. Expected objects are inferred from a limited `CREATE TABLE/INDEX` and `DROP
   TABLE/INDEX` parser rather than an explicit domain-owned object manifest.

The current continuous packaged sequence is:

| Global version | Packaged migration | Logical domain | Actual prerequisite |
|---:|---|---|---|
| 1 | [`0001_invocation_attempts.up.sql`](../../src/quantum_entanglement/migrations/0001_invocation_attempts.up.sql) | `attempts` | none |
| 2 | [`0002_artifacts.up.sql`](../../src/quantum_entanglement/migrations/0002_artifacts.up.sql) | `artifacts` | none on migration 1 |
| 3 | [`0003_outbox_ambiguities.up.sql`](../../src/quantum_entanglement/migrations/0003_outbox_ambiguities.up.sql) | `delivery` | the pre-existing `outbox` table created by `SQLiteEventStore` |
| 4 | [`0004_invocation_admissions.up.sql`](../../src/quantum_entanglement/migrations/0004_invocation_admissions.up.sql) | `admission` | migration 1 plus the pre-existing `events` table created by `SQLiteEventStore` |

The version order is historical, not a semantic dependency graph. Migration 2 does not
need the invocation tables from migration 1. Migration 3 does not need artifact objects,
but it does need `outbox`, which is currently created directly by
[`SQLiteEventStore`](../../src/quantum_entanglement/store.py) before the global runner is
called. Migration 4 does have the explicit semantic edge `4 → 1`; its foreign keys into
`events` are represented separately as an event-store topology prerequisite.

Current store selection demonstrates the problem:

- `SQLiteArtifactStore` asks for `target_versions=(1, 2)` and contains a temporary
  `inspect.signature` compatibility branch for the immediately preceding runner;
- `SQLiteInvocationAttemptStore` also asks for `(1, 2)`, even though an attempt-only
  schema does not semantically own artifact tables;
- `SQLiteEventStore` creates its base schema directly and then applies the complete global
  registry through v4;
- a future artifact migration with global ID 5 cannot be selected as `(1, 2, 5)` because
  the current runner would require `(1, 2, 3, 4, 5)`; applying v3 to an artifact-only
  database fails when `outbox` is absent. Migration 4's event-store prerequisite is
  recorded in the topology registry, but the legacy runner can create its empty foreign-key
  table even when `events` is absent; that enforcement gap is itself release-blocking.

The current backup implementation is
[`src/quantum_entanglement/backup.py`](../../src/quantum_entanglement/backup.py). Its public
operations—`create_sqlite_backup`, `verify_sqlite_backup`, and
`restore_sqlite_backup`—provide strong no-overwrite, stable-file-identity, online SQLite
backup, digest, page geometry, table-count, integrity, and foreign-key checks. Its schema
evidence is nevertheless global-prefix-only:

- manifest format is exactly `qe.sqlite-backup/1`;
- `_validate_migration_evidence` compares row `i` with global `MIGRATIONS[i]`;
- `_database_evidence` calls the current global `validate_sqlite_schema`;
- manifest `migrations` is a flat continuous prefix;
- `_CORE_TABLES` now includes `invocation_admissions`, but it remains a fixed inventory and
  does not attest the proposed sidecar tables;
- the exact v1 manifest parser has no domain heads, dependency edges, registry digest, or
  `SchemaState` digest.

Migration 4 was therefore added to the global runner, domain registry, exact topology, and
v1 backup table inventory as one coordinated current-prefix change. That does not provide
a native sparse runner or make manifest v2 operational; both remain separate bridge
release work.

The current frozen identities are:

| Identity | SHA-256 |
|---|---|
| Domain-migration registry | `923e8e9c95e2da1844e79515b0a420b3397e343c57296cba561c1e8e99d96650` |
| Backup-topology registry | `d940bea8e2a3c80cd76fc282a042667527632c236cf802fd39dab250005da2aa` |
| Migration-4 topology profile | `5eb9ccd2ced7ac47e27db5911f82f84ff5500ce252634efe26fd3686b6488a6d` |

These digests are compatibility identities, not signatures or production-readiness
attestations.

## 2. Goals

The sidecar design must provide all of the following:

1. Preserve packaged SQL bytes, global IDs 1–4, filenames, checksums, and existing
   `qe_schema_migrations` rows.
2. Reinterpret the global integer as an immutable migration ID, not a database-wide schema
   head.
3. Give every migration exactly one canonical owned domain and a positive, continuous
   version inside that domain.
4. Declare semantic migration dependencies explicitly as an acyclic graph.
5. Permit a database to apply only the requested domain closure, producing a sparse global
   ledger after the fleet is bridge-aware.
6. Validate legacy, bridged-prefix, and domain-sparse states without inferring authority
   from table names alone.
7. Produce a deterministic plan and digest from the same registry, state, and requested
   targets on every process.
8. Apply SQL, the legacy ledger row, domain metadata, dependency evidence, and schema
   postconditions in one SQLite transaction.
9. Preserve fail-closed behavior for checksum drift, unknown/newer migrations, weak
   schemas, races, partial DDL, and commit failure.
10. Make backup and restore evidence congruent with the domain-aware state.
11. Support a bridge-first rollout before any native or sparse-ledger write.
12. Define mixed-version, rollback, recovery, failure-injection, and release-evidence
    requirements before implementation.

## 3. Non-goals

The current bridge foundation does not:

- claim that native/domain-sparse application, backup format v2, or the proposed artifact
  migration at ID 5 or later is implemented;
- change or renumber packaged migrations 1–4;
- turn SQLite into a multi-host consensus database;
- provide PostgreSQL/Alembic/Flyway compatibility or a generic SQL migration framework;
- make destructive down migrations an automatic startup path;
- infer a trustworthy ledger from arbitrary unledgered tables or application data;
- allow branches to rewrite or merge already-applied migration history;
- grant a migration write authority merely because it depends on another domain;
- make a future artifact migration online or zero-downtime by definition;
- solve artifact tenant isolation, event schema versioning, backup encryption, PITR,
  retention, or KMS by architecture text alone;
- replace application-level rolling compatibility tests with planner compatibility;
- use schema migration state as an end-user authorization mechanism.

## 4. Terminology and identifiers

### 4.1 Migration ID

The existing positive integer `qe_schema_migrations.version` becomes `migration_id` in the
architecture vocabulary while retaining its physical column name for compatibility. It is
globally unique, append-only, and never reused. Numeric gaps are valid in a domain-aware
database.

The ID is identity, not ordering. A larger ID is not implicitly newer for every domain and
does not imply dependency on every smaller ID.

### 4.2 Domain and domain version

A domain is a stable lower-snake-case identifier such as `attempts`, `artifacts`,
`delivery`, or `admission`. A `(domain, domain_version)` pair is unique. Versions within
one domain are a continuous prefix starting at 1; different domains advance independently.

### 4.3 Owned schema

Every persistent table, index, trigger, and view managed by the domain registry has one
owner. A dependency permits ordering and validated reads; it does not permit writes to the
dependency's objects. Sidecar/ledger objects are owned by the migration system itself.

The initial logical ownership map is:

| Domain | Owned objects or boundary |
|---|---|
| `migration_system` | `qe_schema_migrations`, both sidecar tables, their exact schemas |
| `attempts` | `invocation_jobs`, `invocation_attempts`, and their indexes |
| `artifacts` | `artifact_blobs`, `artifact_versions`, and their indexes |
| `delivery` | base `outbox`/receipt delivery objects, `outbox_ambiguities`, and related indexes |
| `admission` | `invocation_admissions` and its explicit stream index |

The event store currently creates several base objects outside the migration registry.
The bridge must model those as explicit schema preconditions/owned-state fingerprints; it
must not pretend they were created by a migration that never ran. A later, separately
designed migration may bring base event/delivery schema under the registry.

### 4.4 Dependency

A hard dependency means migration A cannot be applied unless migration B is already
applied or appears earlier in the same plan. The dependency graph contains migration edges
only. Non-migration prerequisites—such as v3's required legacy `outbox` shape and v4's
required event-store `events` shape—are encoded today in the independent backup-topology
registry. Current `SchemaState`, bridge validation, and the legacy runner do **not** consume
those component edges. A future native planner/executor must evaluate and revalidate them;
until then, topology-registry presence is a model assertion, not an admission check.

## 5. Implemented format-1 durable sidecar

The existing `qe_schema_migrations` table remains byte-for-byte and schema-for-schema
unchanged. Two exact-schema sidecar tables augment applied rows.

### 5.1 `qe_schema_migration_metadata`

Implemented format-1 DDL:

```sql
CREATE TABLE qe_schema_migration_metadata (
    migration_version INTEGER PRIMARY KEY,
    domain TEXT NOT NULL,
    domain_version INTEGER NOT NULL,
    metadata_kind TEXT NOT NULL,
    descriptor_sha256 TEXT NOT NULL,
    owned_schema_sha256 TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE(domain, domain_version),
    FOREIGN KEY(migration_version)
        REFERENCES qe_schema_migrations(version) ON DELETE RESTRICT,
    CHECK(migration_version > 0),
    CHECK(domain_version > 0),
    CHECK(
        length(domain) BETWEEN 1 AND 64
        AND substr(domain, 1, 1) GLOB '[a-z]'
        AND domain NOT GLOB '*[^a-z0-9_]*'
    ),
    CHECK(metadata_kind IN ('legacy_bootstrap', 'native')),
    CHECK(
        length(descriptor_sha256) = 64
        AND descriptor_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK(
        length(owned_schema_sha256) = 64
        AND owned_schema_sha256 NOT GLOB '*[^0-9a-f]*'
    )
);
```

Column meaning:

| Column | Meaning |
|---|---|
| `migration_version` | Foreign key to the unchanged legacy ledger; architecturally the migration ID |
| `domain`, `domain_version` | Independent domain coordinate |
| `metadata_kind` | Whether the row was mapped from legacy history or written by the native domain planner |
| `descriptor_sha256` | Canonical digest of ID, filename, SQL digest, domain/version, dependencies, ownership, and preconditions |
| `owned_schema_sha256` | Canonical digest of the domain postcondition manifest immediately after this migration |
| `recorded_at` | Strict RFC 3339 bridge/application timestamp validated by application code |

The latest applied row in a domain supplies the expected current owned-schema digest.
Older rows remain immutable historical postconditions; they are not compared directly
with a schema later changed by another version in the same domain.

### 5.2 `qe_schema_migration_dependencies`

Implemented format-1 DDL:

```sql
CREATE TABLE qe_schema_migration_dependencies (
    migration_version INTEGER NOT NULL,
    depends_on_version INTEGER NOT NULL,
    PRIMARY KEY(migration_version, depends_on_version),
    FOREIGN KEY(migration_version)
        REFERENCES qe_schema_migration_metadata(migration_version)
        ON DELETE RESTRICT,
    FOREIGN KEY(depends_on_version)
        REFERENCES qe_schema_migration_metadata(migration_version)
        ON DELETE RESTRICT,
    CHECK(migration_version <> depends_on_version)
);
```

The packaged registry remains the source of intended dependencies. Durable rows prove
which dependency descriptor was committed with each applied migration. Validation requires
exact equality between packaged and durable edges; neither source is accepted as an
optional superset.

Only applied dependencies are stored, so every `depends_on_version` already has metadata.
Dependencies for unapplied packaged migrations live only in the immutable packaged
registry until application.

### 5.3 Sidecar integrity rules

- `PRAGMA foreign_keys=ON` is mandatory and verified before bridge/application.
- Both tables are absent or both are present with exact normalized DDL.
- A weak pre-created table, view shadow, trigger, custom index, unexpected column, missing
  check, or partial pair fails closed.
- No `CREATE TABLE IF NOT EXISTS` result is trusted without inspecting `main.sqlite_master`.
- Every packaged ledger row has exactly one metadata row after bootstrap.
- Every metadata row has exactly one supported ledger row; no orphan or duplicate
  `(domain, domain_version)` exists.
- Durable dependencies match the registry exactly and form no self-edge or cycle.
- Sidecar installation and metadata bootstrap each commit atomically when called
  separately; `apply_bridge_migration_plan` can compose both locked kernels in one
  revalidated `BEGIN IMMEDIATE` transaction.
- Re-running bootstrap validates the winner; it does not “repair” missing or conflicting
  rows silently.

## 6. Implemented registry foundation and future native model

The committed `DomainMigrationDescriptor` and `DomainMigrationRegistry` implement the
bridge-only subset. The simplified `DomainMigration` below remains illustrative of later
native migration support and is not a public API.

```python
@dataclass(frozen=True)
class DomainMigration:
    migration_id: int
    filename: str
    domain: str
    domain_version: int
    dependencies: tuple[int, ...]
    owned_objects: tuple[SchemaObjectRef, ...]
    preconditions: tuple[SchemaPrecondition, ...]
    postcondition_sha256: str
    apply_enabled: bool = True
```

Registry validation occurs before any database mutation and requires:

1. unique positive migration IDs and filenames;
2. unique `(domain, domain_version)` pairs;
3. a continuous `1..N` sequence inside each domain;
4. dependencies that name known migrations and no self-dependency;
5. an acyclic graph;
6. every dependency's domain version closure to be internally valid;
7. exact packaged SQL digest and canonical descriptor digest;
8. one owner for every declared object;
9. no undeclared cross-domain write;
10. explicit pre/postconditions for DDL the current regex parser cannot model, including
    `ALTER TABLE`, rename/rebuild, triggers, and data-copy migrations;
11. immutable descriptors for every migration already shipped in a release;
12. a bounded registry, dependency count, object count, and canonical serialization.

The bridge mapping is:

| Migration ID | Domain coordinate | Dependencies | Kind | Registered topology requirement |
|---:|---|---|---|---|
| 1 | `attempts@1` | none | `legacy_bootstrap` | legacy v1 owned schema exactly matches packaged expectation |
| 2 | `artifacts@1` | none | `legacy_bootstrap` | legacy v2 owned schema exactly matches packaged expectation |
| 3 | `delivery@1` | none | `legacy_bootstrap` | legacy `outbox` plus the migration-3 result shape |
| 4 | `admission@1` | migration 1 | `legacy_bootstrap` | event-store `events` plus the migration-1 job schema used by admission foreign keys |

The historical fact that legacy row 2 followed row 1 is preserved by timestamps and IDs,
not converted into a false semantic dependency. V3's `outbox` requirement is registered as
a component prerequisite because no ledger migration currently represents base `outbox`
DDL. Migration 4 is different: its use of `invocation_jobs` is the explicit domain edge
`4 → 1`, while its use of event-store `events` is a registered topology prerequisite
because no migration owns that base table. Neither component prerequisite is currently
enforced by the bridge snapshot reader; the migration-4 SQL can also install an empty
foreign-key table without the referenced `events` table. The topology registry must not be
described as runtime enforcement.

## 7. Legacy bootstrap protocol

The exact sidecar installer and legacy metadata bootstrap described here are implemented
as separate atomic primitives. `apply_bridge_migration_plan` implements the ten-step
orchestration below when the deterministic plan contains either or both actions. The planner
remains pure and performs no database writes.

Bootstrap converts supported history into sidecar metadata without changing a legacy SQL
file, ledger row, application table, or global ID.

### 7.1 Accepted starting states

The bridge accepts only:

- a truly empty database with no legacy ledger and no known migration-owned object;
- a current, exactly validated legacy prefix `()`, `(1)`, `(1, 2)`, `(1, 2, 3)`, or
  `(1, 2, 3, 4)`;
- an already bridged state whose sidecar, descriptors, dependencies, and owned schema all
  validate exactly.

It rejects:

- an absent ledger with known invocation/artifact/ambiguity/admission objects;
- ledger holes, unknown versions, duplicate rows, filename/checksum drift, or weak ledger
  DDL;
- an object shape that differs from the packaged legacy postcondition;
- only one sidecar table, partial metadata, extra metadata, conflicting domain mapping, or
  dependency drift;
- an unknown/newer sidecar schema or descriptor digest.

This accepted/rejected classification covers the legacy ledger and migration-owned
objects only. It currently does **not** reject an already-applied v3 database after base
`outbox` is removed, or an already-applied v4 database after base `events` is removed.
Those states violate the registered topology but can still be classified as
`legacy_prefix`/`bridged_prefix`; operators must therefore run the independent exact
topology and foreign-key/integrity controls, and phase-1 release integration remains
blocked until the bridge enforces the component prerequisites directly.

An operator may first use the current legacy runner to adopt a supported exact legacy
shape where that behavior is already tested. The bridge itself does not infer missing
history from object names or data.

### 7.2 Transaction sequence

The atomic bridge plan applier performs the following under its write lock:

1. validates every plan scalar, enum, exact tuple, action transition, bound, digest, and
   release mode before touching SQLite;
2. begins one `BEGIN IMMEDIATE` transaction and reads only `main` objects;
3. inspects the locked exact legacy/sidecar state and compares its
   `source_state_sha256` with the caller's plan;
4. re-derives the canonical plan from that locked state and requires both value equality
   and exact `plan_sha256` equality;
5. creates both exact sidecar tables when the allowlisted action requires it, or validates
   the existing pair;
6. inserts one `legacy_bootstrap` metadata row for each applied packaged ID when the
   allowlisted bootstrap action requires it;
7. inserts the exact semantic dependency set: IDs 1–3 have no edges and ID 4 records
   `4 → 1` when applied;
8. computes and compares the exact expected post-write `SchemaState` before commit;
9. commits; on any `BaseException` while it still owns the transaction, rolls back all DDL
   and rows and verifies that the transaction ended;
10. re-reads and compares the committed state before returning it.

Two concurrent bridge initializers serialize on SQLite's writer lock. A plan created before
the winner commits becomes stale and is rejected under the lock; the caller must inspect and
plan again. An already bridged canonical no-op plan is accepted without DDL or DML.

For a truly empty database, the bridge creates the unchanged legacy ledger schema and both
sidecars but records no applied migration. Later domain plans may apply only their closure.
That sparse behavior must remain disabled during bridge-only phase 1.

## 8. Implemented bridge-only `SchemaState`

`current_schema_version(connection) -> int` cannot represent independent domains. The
committed immutable `SchemaState` is the sole input to the bridge-only planner. Inactive
manifest-v2 codec and single-snapshot evidence checkpoints exist, but active backup-v2
creation/verification/restore, readiness integration, restore reconciliation, and
sparse-state representation remain future work.

Committed fields (model names abbreviated in this example):

```python
@dataclass(frozen=True)
class SchemaState:
    sidecar_format: int
    shape: SchemaShape
    legacy_schema_version: int
    applied_migrations: tuple[AppliedSchemaMigration, ...]
    domain_heads: tuple[SchemaDomainHead, ...]
    dependency_edges: tuple[DomainMigrationDependencyRow, ...]
    owned_schema_digests: tuple[tuple[str, str], ...]
    registry_sha256: str
    state_sha256: str
```

`SchemaShape` distinguishes at least:

| Shape | Meaning |
|---|---|
| `sidecar_absent` | supported exact legacy prefix, including zero rows, without sidecars |
| `empty` | exact sidecars exist and the legacy ledger has no rows |
| `legacy_prefix` | exact sidecars exist but metadata has not yet mapped a non-empty legacy prefix |
| `bridged_prefix` | sidecars exactly map a non-empty legacy continuous prefix |

`domain_sparse` is deliberately absent from the current enum. Unknown, drifted, partial,
newer, native, and sparse states raise an integrity/planning error instead of becoming a
runnable `SchemaState`.

Canonicalization rules:

- migrations sort by numeric migration ID;
- domain heads sort by UTF-8 domain bytes then integer version;
- dependency edges sort by `(migration_version, depends_on_version)`;
- owned-schema digest rows follow the canonical domain-head order;
- JSON uses fixed field names, UTF-8, sorted keys, no insignificant whitespace, no NaN;
- timestamps are excluded from `state_sha256` so a faithful evidence refresh does not
  change semantic state;
- registry and owned-schema digests are included;
- unknown fields/types fail rather than being ignored.

The state digest is content evidence, not a signature. Backup manifests and release
records still require authenticated custody.

The first row below is current. The remaining rows illustrate a possible later sparse
artifact migration using candidate ID 5; no migration-5 SQL or native executor exists in
this checkout:

| Database role | Global ledger IDs | Domain heads |
|---|---|---|
| Current shared database | `1, 2, 3, 4` | `admission@1`, `attempts@1`, `artifacts@1`, `delivery@1` |
| New artifact-only database after candidate v5 | `2, 5` | `artifacts@2` |
| Existing artifact database upgraded to candidate v5 | `1, 2, 5` | `attempts@1`, `artifacts@2` |
| Full shared database after candidate v5 | `1, 2, 3, 4, 5` | `admission@1`, `attempts@1`, `artifacts@2`, `delivery@1` |

The apparently extra `attempts@1` in an existing artifact database is retained legacy
history, not a reason to apply attempts to new artifact-only databases.

## 9. Deterministic dependency planner

### 9.1 Inputs and output

Planner inputs are:

- a fully validated packaged registry;
- an immutable `SchemaState` read from one database snapshot;
- exact requested target heads, for example `{"artifacts": 2}`;
- an explicit deployment policy controlling whether sparse plans are enabled.

The output is an immutable `MigrationPlan` containing:

- source `state_sha256` and registry digest;
- normalized requested targets;
- dependency closure;
- ordered unapplied steps with each step's expected pre/post-state digest;
- each step's SQL and descriptor digests, preconditions, owned objects, and expected
  postcondition;
- canonical `plan_sha256`;
- compatibility classification and minimum reader/backup format.

### 9.2 Planning algorithm

1. Reject an unsupported state or registry.
2. Normalize targets by domain; reject unknown domains, duplicate targets, non-integers,
   versions below zero, and a requested downgrade below an applied head.
3. Resolve every requested `(domain, version)` to its descriptor.
4. Compute the transitive hard-dependency closure.
5. Require continuous selected versions inside every involved domain.
6. Remove already-applied migrations only after proving their durable metadata,
   dependencies, descriptor digest, and owned head are exact.
7. Run Kahn's topological algorithm over remaining steps.
8. When multiple nodes are ready, choose the smallest tuple
   `(domain UTF-8 bytes, domain_version, migration_id)`. Never use set/dictionary iteration
   order, filesystem order, locale, or timestamp.
9. Evaluate static ownership conflicts and required schema preconditions.
10. Serialize the complete plan canonically and calculate `plan_sha256`.

A cycle, unknown dependency, missing domain prefix, conflicting owner, changed descriptor,
or unsatisfied prerequisite is a planning error with no database mutation.

### 9.3 Locked application

The executor preserves the current per-migration transaction boundary. For each unapplied
step in plan order it:

1. executes `BEGIN IMMEDIATE`;
2. rebuilds `SchemaState` under the write lock;
3. requires its digest to equal that step's expected pre-state digest; otherwise rolls back
   and requires a fresh plan;
4. verifies dependencies and preconditions again;
5. executes packaged SQL statement-by-statement;
6. validates only declared writes plus the complete affected-domain postcondition;
7. inserts the unchanged legacy ledger row;
8. inserts metadata and exact dependency rows;
9. validates the whole expected post-`SchemaState`, foreign keys, and registry congruence;
10. commits, then re-reads the durable state and requires the expected post-state digest
    before starting another step.

One migration's SQL body, ledger, metadata, dependencies, and postcondition are an atomic
unit. A process must never commit SQL and “fill in” sidecar evidence later. If a process
stops after a committed step, the database remains a valid dependency-closed state; a
fresh planner resumes from that state rather than replaying the stale remainder.

SQLite still serializes all writers even when domains are independent. Domain scoping
removes false migration prerequisites; it does not promise parallel DDL execution.

## 10. Ownership and schema postconditions

The current regex-derived expected-object model is retained only for validating immutable
legacy behavior. Native domain migrations require an explicit owned-object manifest and
postcondition validator.

For each affected domain, validation includes:

- exact `sqlite_master` type and normalized DDL for every owned table/index/trigger/view;
- required table columns, order, affinities, nullability, defaults, primary keys, unique
  constraints, checks, and foreign keys;
- absence of unexpected custom triggers/indexes on security-critical owned tables;
- declared data invariants and row-count/digest reconciliation for rebuilds;
- no undeclared object creation, drop, rename, or mutation;
- dependency objects inspected read-only unless the same domain owns them;
- `PRAGMA foreign_key_check` and an operation-appropriate integrity check.

Migration 3 is treated as `delivery` because both the base `outbox` state it scrubs and the
ambiguity table belong to the delivery boundary. The separately created base table remains
a registered topology prerequisite, not invented migration history; current bridge state
inspection does not yet enforce that prerequisite.

Ownership transfer is not part of sidecar format 1. If a future refactor must transfer an
object between domains, it requires a separately reviewed protocol and format/version
change; adding both owners temporarily is forbidden.

## 11. Two-stage release: bridge, then a future sparse migration

No release may combine first sidecar deployment and the first native/sparse write. Current
migration 4 is already part of the continuous global prefix; it is not the sparse phase-2
migration described here.

### 11.1 Phase 1 — bridge-only release

The bridge release must include:

- exact sidecar DDL and legacy bootstrap;
- domain registry descriptors for packaged IDs 1–4, including the exact `4 → 1` edge;
- `SchemaState`, validator, deterministic planner, and plan digest;
- a feature gate that **disables native/sparse application**;
- domain-aware backup manifest/parser support while retaining strict verification of
  legacy format v1;
- admin/readiness inspection that reports state shape, domain heads, registry digest, and
  backup compatibility without exposing sensitive data;
- mixed old/bridge binary tests against every supported legacy prefix;
- clean wheel/package-data and restore rehearsals.

During this phase the ledger remains a legacy prefix. Current binaries generally ignore
unexpected sidecar tables because their validator checks expected objects rather than
rejecting every extra table, but this observation is not sufficient evidence. The exact
old/bridge binary matrix must pass before rollout.

Old backup code may physically copy sidecar tables yet omit them from manifest evidence.
Such a backup is not sufficient for phase-2 promotion. Before enabling any sparse
migration, every backup
writer/verifier/restore job must use domain-aware evidence and a new verified backup must
be retained.

Phase-1 exit criteria:

1. 100% of application, worker, admin, migration, backup, and restore binaries are
   bridge-aware;
2. every database is `bridged_prefix`, or explicitly empty and bridge-initialized;
3. no old backup writer or restore path remains schedulable;
4. registry/state digests match the deployment inventory;
5. backup v2 restore and rollback rehearsals pass;
6. the fleet has remained healthy for the recorded soak interval;
7. a release owner signs the sparse-ledger enablement decision.

### 11.2 Phase 2 — future artifact migration (candidate ID 5)

Only a later release may package and enable a native migration, illustrated here as
candidate migration ID 5 / `artifacts@2`. It must:

- depend explicitly on migration ID 2 / `artifacts@1`, not on global ID 3;
- be rehearsed on every observed legacy artifact shape and representative data volume;
- stop or fence artifact writers for the required SQLite rebuild window;
- prove tenant/workspace physical identity and blob isolation postconditions;
- emit domain-aware backup and plan/state evidence;
- reject any non-bridge binary before it can open a sparse or unknown-migration database;
- keep a tested sparse-aware application rollback target or use verified pre-migration
  restore.

The current migration-2 artifact schema still uses a global `artifact_id` primary key and
global blob digest key. A possible future goal is composite tenant/workspace identity and
composite blob foreign keys, but the exact ID, DDL, orphan policy, data-copy algorithm,
metadata limits, and rollback are separate implementation work. This document does not
claim they exist.

## 12. Mixed-version compatibility

### 12.1 Safe bridge window

Old and bridge binaries may mix only while all of these remain true:

- global ledger rows are still a supported continuous legacy prefix;
- no native migration metadata row or sparse ID has been written;
- sidecar tables are exact and old binaries have been tested to ignore them safely;
- application table shapes remain legacy-compatible;
- backup ownership is pinned to the bridge-aware implementation;
- the bridge does not issue a policy/schema state an old writer misunderstands.

The existing `inspect.signature` fallback in `SQLiteArtifactStore` is a narrow rolling
bridge for `target_versions`; it is not a general domain-migration negotiation mechanism.
The final API should be explicit and versioned, and the compatibility shim should be
removed only in its own tested commit after the fleet floor advances.

### 12.2 Current v4 downgrade fence

A database with ledger row 4 requires a v4-aware binary. At validator level, passing a
registry that ends at version 3 makes the current validator see row 4 as newer, raise
`MigrationVersionError`, and leave the ledger and admission receipt schema unchanged.
The retained test is this in-process v3-registry simulation; it is not yet evidence from a
packaged historical v3 wheel in an independent process. Operators must not delete row 4 or
`invocation_admissions` to evade the fence, and the real old-wheel/process matrix remains a
release gate.

Current v4 remains a continuous global prefix and is supported by the current legacy
runner and active v1 backup implementation. This downgrade fence is distinct from the
stronger incompatibility of a future sparse ledger.

### 12.3 Future sparse boundary

After any database records a native sparse migration such as candidate ID 5, or any other
sparse ledger:

- current legacy `validate_sqlite_schema`, `current_schema_version`, and backup format v1
  are incompatible by design;
- old binaries must fail readiness before mutation;
- routing must prevent them from receiving that database;
- application rollback is limited to a binary that understands and safely operates the
  resulting `SchemaState` and new table shapes;
- rollback to a pre-bridge binary requires restoring a verified pre-migration backup, not
  editing the ledger to look old.

Mixed-version tests must exercise two physical processes/wheels, not only monkey-patched
functions in one interpreter.

## 13. Upgrade runbook

### 13.1 Inventory and rehearsal

For every database before bridge deployment:

1. record path identity, application role, owning service, binary commit, SQLite/Python
   versions, and active writers;
2. run current `validate_sqlite_schema`, `PRAGMA integrity_check`, and
   `PRAGMA foreign_key_check` read-only;
3. export the exact ledger rows and packaged checksum comparison;
4. classify the state as empty or supported legacy prefix;
5. quarantine unknown, weak, unledgered, or drifted objects instead of bootstrapping;
6. create and independently verify a no-overwrite online backup and manifest;
7. restore the backup to a rehearsal path and run the matching old binary smoke test;
8. run bridge bootstrap on the rehearsal copy and retain before/after `SchemaState`.

### 13.2 Bridge deployment

1. stop automatic schema changes but keep schema-compatible application traffic according
   to the rollout plan;
2. deploy bridge-aware readers/validators and backup tooling first;
3. bootstrap one canary database under writer fencing;
4. validate sidecar DDL, metadata mapping, state digest, old/bridge read compatibility,
   backup v2, and restore;
5. roll through the fleet, recording each database state digest;
6. keep sparse/native application disabled;
7. remove or disable old backup/restore jobs;
8. meet phase-1 exit criteria and soak before approving any native/sparse migration.

### 13.3 Future sparse artifact deployment (candidate v5)

1. prove all binary identities satisfy the bridge floor;
2. disable/fence artifact writers and drain active artifact transactions;
3. create and verify a fresh domain-aware backup;
4. inspect and retain the deterministic candidate-v5 plan and digest;
5. apply under `BEGIN IMMEDIATE` with postcondition validation;
6. verify domain heads, sidecar edges, owned object DDL, row counts, blob/content digests,
   tenant/workspace collision cases, foreign keys, and integrity;
7. run artifact and end-to-end smoke tests before reopening writers;
8. monitor lock time, migration duration, storage expansion, denials/errors, and backup
   success;
9. promote gradually and retain the exact evidence bundle.

## 14. Rollback and down migrations

### 14.1 Bridge-only rollback

If phase 1 changes no application schema and writes no sparse/native migration, application
code may roll back to a tested old binary while leaving exact sidecar tables in place.
Do not drop the sidecar merely to make the database look untouched; doing so destroys
evidence and can invalidate bridge-created backups.

If old-binary compatibility was not proven, restore the verified pre-bridge backup under
full writer shutdown. Never copy ledger rows manually.

### 14.2 Existing `.down.sql` limitation after bridge

Legacy down files directly delete their `qe_schema_migrations` row. Once metadata exists,
the proposed `ON DELETE RESTRICT` foreign key intentionally prevents running those scripts
directly. This is a safety feature, not an error to bypass with
`PRAGMA foreign_keys=OFF`.

A future sidecar-aware destructive rollback tool would have to:

1. require an explicit operator target and verified backup;
2. prove no applied migration depends on the target;
3. stop all writers and obtain the write lock;
4. validate exact current state and down-script checksum;
5. remove the target's dependency/metadata evidence inside the same transaction;
6. execute the packaged destructive down body, including its ledger deletion;
7. validate the resulting domain state, foreign keys, and integrity;
8. commit and re-read, or roll back every change on failure.

That tool is not part of the current code or this sidecar proposal's initial delivery.
Prefer schema-compatible application rollback or restore-and-forward-fix.

### 14.3 After current v4 or a future sparse migration

Do not send a v4 database to a v3-only runner. Roll application code back only to a tested
v4-aware version. The packaged v4 down migration drops durable admission receipts and is
therefore destructive safety-state loss, not an automatic downgrade mechanism; prefer a
forward fix or a verified pre-v4 restore with effect/work reconciliation.

For a future native/sparse migration, roll back only to a tested sparse-aware version. If
its data layout is not reversibly compatible, restore the pre-migration backup in
reconciliation mode or forward-fix. Restoring loses post-backup writes, so every release
must define export/replay/reconciliation and measured RPO/RTO before promotion.

## 15. Backup and restore format evolution

### 15.1 Preserve current file-safety controls

Domain-aware backup work must retain the current implementation's:

- SQLite online backup rather than live main-file copying;
- no-overwrite destination semantics;
- symlink/regular-file and directory identity checks;
- stable descriptor reads and inode/device revalidation;
- `0600` temporary/final file handling and directory fsync;
- database digest, page geometry, table counts, integrity, and foreign-key checks;
- paired manifest publication and cleanup limited to owned temporary/created entries.

### 15.2 Proposed manifest v2 evidence

Use a new exact format, for example `qe.sqlite-backup/2`; do not add optional fields to v1
and call it compatible. V2 must include a canonical block equivalent to:

```json
{
  "schemaState": {
    "sidecarFormat": 1,
    "shape": "domain_sparse",
    "registrySha256": "<lowercase sha256>",
    "stateSha256": "<lowercase sha256>",
    "domainHeads": [{"domain": "artifacts", "version": 2}],
    "migrations": [
      {
        "migrationId": 2,
        "domain": "artifacts",
        "domainVersion": 1,
        "filename": "0002_artifacts.up.sql",
        "sqlSha256": "<lowercase sha256>",
        "descriptorSha256": "<lowercase sha256>",
        "ownedSchemaSha256": "<lowercase sha256>",
        "appliedAt": "2026-08-20T00:00:00.000000Z"
      },
      {
        "migrationId": 5,
        "domain": "artifacts",
        "domainVersion": 2,
        "filename": "0005_artifact_tenant_isolation.up.sql",
        "sqlSha256": "<lowercase sha256>",
        "descriptorSha256": "<lowercase sha256>",
        "ownedSchemaSha256": "<lowercase sha256>",
        "appliedAt": "2026-08-20T00:01:00.000000Z"
      }
    ],
    "dependencies": [{"migrationId": 5, "dependsOnMigrationId": 2}]
  }
}
```

The migration-5 entry is only a shape illustration, not a packaged migration or the
implemented `BackupManifest` schema. The final parser must have exact fields, strict
types/count/byte limits, canonical ordering, known format/registry validation, and
manifest authentication/custody policy.

Table-count evidence must derive from the validated owned-object registry rather than only
the current fixed `_CORE_TABLES`, and must include both sidecar tables. The manifest and
database `SchemaState` must match exactly.

### 15.3 Compatibility and restore rules

- A domain-aware binary may verify a v1 backup only as a supported legacy-prefix backup.
- After restoring v1, bootstrap sidecar in reconciliation mode and issue a verified v2
  backup before enabling sparse migration.
- A v1 verifier must never accept a v2 manifest or sparse ledger.
- A v2 restore first validates files and `SchemaState` in quarantine; it does not migrate
  while proving the backup.
- Unknown migration IDs, registry digests, domain versions, dependencies, or owned-schema
  digests fail closed as newer/unsupported or drifted state.
- Restore activation requires matching binary, planner/registry digest, table counts,
  foreign keys, integrity, application smoke, and effect/lease reconciliation.
- Backups created by old tooling during the bridge window are physically plausible but do
  not attest sidecar evidence; they block phase-2 promotion.

## 16. Failure-injection matrix

Implementation is incomplete until deterministic tests cover every row.

| Injection/failure | Required invariant |
|---|---|
| Crash before bridge transaction | Database remains exact legacy state |
| Failure after first sidecar DDL | Both sidecar tables and all bootstrap rows roll back |
| Commit failure during bootstrap | No partial DDL/metadata; write lock released; retry validates cleanly |
| Two concurrent bootstrappers | One commits; the other validates identical state |
| Weak/pre-created sidecar table or view | Fail before row access or mutation |
| Missing/extra/conflicting metadata | Fail closed; never auto-repair |
| Missing/extra/self/cyclic/unknown dependency | Registry/state rejected before SQL |
| Ledger filename/checksum/version drift | Preserve current `MigrationDriftError`/newer-version behavior |
| Unledgered known application table | Quarantine; no inferred bootstrap |
| Planner called twice with same inputs | Byte-identical order and plan digest |
| Concurrent state change before executor lock | Source digest mismatch; no stale plan executes |
| SQL statement N fails | All body, ledger, metadata, and edges roll back |
| Owned-schema postcondition fails | Whole migration rolls back |
| Metadata/dependency insert fails | SQL body and ledger roll back |
| Commit fails or is denied | Transaction rolls back and competing writer can acquire lock |
| Executor interrupted by `BaseException` | No partial state and lock released |
| Attempt to write another domain's object | Preflight/postcondition rejects and rolls back |
| V3 without valid `outbox` precondition | Fail before destructive rename/data copy |
| Candidate v5 duplicate cross-tenant IDs | Conversion proves independent composite identity or rolls back |
| Candidate v5 shared/orphan blob edge case | Explicit duplication/quarantine policy; no silent loss or cross-tenant sharing |
| Old binary opens bridged prefix | Only supported in tested phase-1 matrix |
| Current validator with a v3-only registry opens v4 state | In-process simulation raises `MigrationVersionError` before mutation; ledger remains `1, 2, 3, 4` |
| Packaged historical v3 wheel opens current v4 state | Independent-process matrix must prove the same fail-closed result before release |
| Legacy binary opens future sparse state | Fails readiness before mutation |
| V1 backup of bridged DB | Classified insufficient for phase 2 |
| Manifest state differs from database sidecar | Backup/restore verification fails |
| Restore rolls schema state backward | Reconciliation blocks activation |
| Down script run directly after sidecar | Foreign-key restriction blocks unsafe ledger deletion |

Fault tests must inspect transaction state, application objects, all three ledger tables,
locks from an independent connection/process, backup files, and retry outcome—not merely
the raised exception type.

## 17. Observability and readiness

Safe fixed-cardinality signals include:

- schema shape (`legacy_prefix`, `bridged_prefix`, `domain_sparse`, `unsupported`);
- domain and target version from a bounded registry vocabulary;
- plan/apply/validate outcome class and duration;
- lock wait and busy timeout;
- migration ID, registry digest prefix, state digest prefix, and plan digest prefix;
- bootstrap/migration/restore rollback count;
- backup manifest format and schema-evidence match;
- fleet binary compatibility floor and percentage bridged.

Do not log SQL bodies, row data, artifact bytes, secrets, raw connector fields, fencing
tokens, or arbitrary SQLite exception text. Full digests and detailed evidence belong in a
restricted release/audit artifact, not unbounded metric labels.

Readiness is false when:

- registry validation fails;
- state is unsupported, partial, drifted, or newer than the binary;
- an applied descriptor/dependency/owned-schema digest differs;
- required base preconditions are absent;
- a database is sparse but any scheduled binary or backup job is legacy-only;
- backup/restore compatibility for the current state is unproven;
- a migration is in progress, ambiguous, or failed postcondition;
- phase-2 enablement lacks a recorded phase-1 exit decision.

The current bridge cannot itself prove the fourth item because it does not consume the
topology registry's component prerequisites. Consequently it cannot close readiness on its
own even when every check it currently implements passes.

## 18. Required invariants

These are release-blocking:

1. Legacy migration IDs, filenames, SQL bytes, and checksums never change.
2. `qe_schema_migrations` schema remains compatible through the bridge.
3. Migration ID and `(domain, domain_version)` are unique and never reused.
4. Domain versions are continuous even when global IDs are sparse.
5. Every applied ledger row has exactly one matching metadata row after bridge.
6. Durable dependency rows exactly equal the packaged descriptor.
7. The dependency graph is known and acyclic.
8. A migration executes only after its complete dependency closure is applied.
9. Dependencies grant no cross-domain write ownership.
10. Every managed schema object has one owner and an exact postcondition.
11. The planner is deterministic and its plan is bound to source state and registry
    digests.
12. The executor revalidates state after acquiring the write lock.
13. SQL body, ledger, metadata, dependencies, and postcondition commit atomically.
14. Unknown/newer/drifted/partial state fails before application traffic.
15. No bridge bootstrap infers history from unledgered objects.
16. No sparse/native migration is written until all binaries and backup paths are
    bridge-aware.
17. No v3-only binary touches a v4 database, and no legacy runner touches a future sparse
    database.
18. Backup manifest evidence and database `SchemaState` are congruent.
19. Restore never lowers applied security/data state silently.
20. Destructive down/restore is explicit, rehearsed, and never an automatic readiness fix.

## 19. Implemented interfaces and compatibility seam

The phase-1 foundation exposes these concrete operations:

```python
def install_domain_migration_sidecar(connection) -> DomainMigrationSidecarSchemaState: ...


def bootstrap_legacy_domain_migration_metadata(
    connection,
    *,
    clock=utc_now,
) -> DomainMigrationBridgeState: ...


def inspect_schema_state(connection) -> SchemaState: ...


def plan_bridge_migrations(state: SchemaState) -> BridgeMigrationPlan: ...


def apply_bridge_migration_plan(
    connection,
    plan: BridgeMigrationPlan,
    *,
    clock=utc_now,
) -> SchemaState: ...
```

The native/sparse executor remains a design candidate and must not be inferred from the
bridge applier API:

```python
def apply_native_domain_migration_plan(
    connection,
    plan,
    *,
    registry,
    clock,
) -> SchemaState: ...
```

During phase 1, existing `apply_sqlite_migrations`, `validate_sqlite_schema`, and
`current_schema_version` remain available for legacy callers, but bridge-aware stores use
the new state API. Deprecation must publish an explicit compatibility window. Removal
occurs only after no supported database or binary relies on a single global integer.

The public/admin representation must serialize `SchemaState`, not expose Python object
identity. Parsers use exact fields and bounds; deserialization never grants permission to
apply a plan. Plans are constructed by the local trusted registry/planner and revalidated
under lock.

## 20. Verification and release evidence

### 20.1 Current baseline commands

These commands verify the legacy runner, committed bridge planner/applier foundation, and
current backup behavior; they do not exercise a native/sparse migration:

```bash
PYTHONPATH=src python3 -m unittest tests.test_domain_migrations -v
PYTHONPATH=src python3 -m unittest tests.test_migrations -v
PYTHONPATH=src python3 -m unittest tests.test_invocation_admission_migration -v
PYTHONPATH=src python3 -m unittest tests.test_migration_targets -v
PYTHONPATH=src python3 -m unittest tests.test_backup -v
ruff check src/quantum_entanglement/migrations tests/test_migrations.py \
  tests/test_migration_targets.py
mypy --strict --python-version 3.9 --follow-imports=skip \
  src/quantum_entanglement/migrations
python3 -m compileall -q src tests
git diff --check
```

They are evidence of the implemented bridge-only baseline to preserve, not evidence that
native/sparse migrations, backup v2, or production promotion exist.

### 20.2 Remaining implementation test groups

The current suite already covers exact sidecar/bootstrap models, deterministic bridge
planning, locked source revalidation, canonical plan comparison, no-op behavior,
concurrency, and DDL/DML/commit/rollback fault injection. Remaining atomic commits must add
deterministic tests for:

- independent domain selection and sparse ledgers;
- v3 base precondition and legacy upgrade shapes;
- future artifact migration clean, populated, collision, shared-blob, orphan, rollback,
  and large-data cases;
- v3/v4 downgrade-fence process tests plus the old/bridge/future-sparse wheel matrix;
- backup manifest v1/v2 parsing, creation, verification, restore, tamper, and downgrade;
- clean wheel package-data, CLI/admin inspection, and deployment smoke.

### 20.3 Retained phase evidence

Every bridge and later native/sparse promotion record must include:

1. source commits, clean tree/archive digest, wheel digest, and installed SQL/registry
   digests;
2. Python, SQLite, OS/filesystem, store role, and binary compatibility matrix;
3. before/after ledger, metadata, dependency, domain-head, owned-schema, state, and plan
   digests;
4. exact database inventory and observed legacy shapes;
5. test/failure-injection commands, counts, exit status, and retained logs/artifacts;
6. independent-process concurrency and mixed-wheel results;
7. migration lock/duration, database growth, row counts, content/blob digests, foreign-key
   and integrity results;
8. backup v1/v2 verification, restore rehearsal, RPO/RTO, and reconciliation outcome;
9. bridge rollback, current-v4 downgrade-fence evidence, and post-sparse rollback/restore
   rehearsal;
10. unresolved risks with severity, owner, expiry, and promotion impact;
11. phase-1 fleet adoption proof and signed sparse-enable decision;
12. reviewer identity and explicit promote/reject decision under
    [`RELEASE_GATES.md`](../production/RELEASE_GATES.md).

Architectural plausibility, a green narrow unit test, or an unchanged exception is not
release evidence. Phase 2 remains blocked until the bridge, backup format, fleet floor,
future artifact migration implementation, and restore path all have direct retained proof.

## 21. Decision summary

Adopt a sidecar rather than rewriting legacy history:

- keep `qe_schema_migrations` and v1–v4 immutable;
- add exact `qe_schema_migration_metadata` and
  `qe_schema_migration_dependencies` tables;
- map global IDs to independent domain versions;
- store exact applied dependency/descriptor/owned-schema evidence;
- replace the single schema integer with canonical `SchemaState`;
- plan the dependency closure deterministically and recheck it under the write lock;
- deploy the bridge and domain-aware backup first, with sparse writes disabled;
- enable a future artifact migration only after every process and restore path crosses the
  bridge floor;
- prohibit old binaries and backup v1 once a future sparse state exists;
- prefer compatible rollback or verified restore/forward-fix over direct down migration.

This is the minimum safe bridge from the current continuous global prefix through v4 to
genuinely domain-owned migrations. It is a design contract for the next implementation
stages, not a production-readiness claim. Gates A–E remain closed.
