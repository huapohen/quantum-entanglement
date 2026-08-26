# Exact SQLite backup topology registry

## Status and release boundary

This checkpoint defines an immutable registry of the SQLite catalog topology that the
current binary knows how to attest. A later checkpoint adds the separate
[exact manifest v2 codec](SQLITE_BACKUP_MANIFEST_V2_CODEC.md). Together they remain
compatibility-development infrastructure, not an operational v2 backup feature.

The active backup surface is unchanged:

- `create_sqlite_backup()` still writes only `qe.sqlite-backup/1`;
- `verify_sqlite_backup()` and `restore_sqlite_backup()` still accept only the exact
  v1 manifest;
- the admin CLI has no v2 dispatch;
- a cold package-root import does not import `backup_topology.py` or read packaged migration
  SQL;
- explicitly importing `backup_topology.py` initializes the domain-migration registry and
  reads its packaged `*.up.sql` resources for cross-binding, but opens no database, starts
  no transaction, and performs no migration;
- after that initialization, canonicalization and registry/model operations perform no
  filesystem access;
- initialized v2 codec operations are pure and remain unreachable from the active v1
  module;
- domain-sparse/native migration execution remains unavailable.

Consequently this stage is safe to deploy as inert package data, but it does not close the
backup/restore, disaster-recovery, or Gate C requirements.

## Exact registry identity

The topology profile is `qe.sqlite-topology/bridge-v1`; the registry format is
`qe.sqlite-topology-registry/1`. The current registry digest is:

```text
d940bea8e2a3c80cd76fc282a042667527632c236cf802fd39dab250005da2aa
```

It contains nine profiles and 63 exact `sqlite_schema` objects:

| Profile | Objects | Presence rule |
|---|---:|---|
| `qe.event-store-core/1` | 17 | optional as one exact component profile |
| `qe.projection-store/1` | 6 | optional as one exact component profile |
| `qe.revocation-guard/1` | 2 | optional as one exact component profile |
| `qe.legacy-migration-ledger/1` | 2 | required when a migration is applied |
| `qe.domain-migration-0001/1` | 14 | required for applied migration 1 |
| `qe.domain-migration-0002/1` | 9 | required for applied migration 2 |
| `qe.domain-migration-0003/1` | 4 | required for applied migration 3; depends on event-store core |
| `qe.domain-migration-0004/1` | 5 | required for applied migration 4; depends on migration 1 and event-store core |
| `qe.domain-migration-sidecar/1` | 4 | required when sidecar format 1 is present |

The independently frozen identities that bind the new admission schema are:

| Identity | SHA-256 |
|---|---|
| Domain-migration registry | `923e8e9c95e2da1844e79515b0a420b3397e343c57296cba561c1e8e99d96650` |
| Backup-topology registry | `d940bea8e2a3c80cd76fc282a042667527632c236cf802fd39dab250005da2aa` |
| `qe.domain-migration-0004/1` profile | `5eb9ccd2ced7ac47e27db5911f82f84ff5500ce252634efe26fd3686b6488a6d` |

Migration 4 is the current packaged `0004_invocation_admissions.up.sql`, owned by
`admission@1`. Its domain descriptor has the semantic migration dependency `4 → 1`.
Its topology profile additionally depends on `qe.event-store-core/1`, because the receipt
table has foreign keys into `events`; that component prerequisite is not another durable
domain-migration edge.

Every object binds:

- its topology profile and logical owner;
- exact object type, name, and `tbl_name`;
- a canonical DDL SHA-256 for explicit tables, indexes, views, and triggers;
- SQL `NULL` for SQLite-created autoindexes, with their coordinate still included in
  the profile digest.

The registry digest binds the canonical ordered profile-name/profile-digest pairs. Each
profile digest independently binds its migration ID, dependencies, presence mode, and
full ordered object set. Profile dependencies must refer to known profiles and form an
acyclic graph. Object coordinates are globally unique.

## Domain-migration cross-binding

The four migration profiles are checked against the packaged
`DOMAIN_MIGRATION_REGISTRY` when the module loads:

1. the migration-ID sets must be identical;
2. each explicit table/index coordinate must be identical;
3. every explicit DDL digest must be identical.

SQLite-created autoindexes are additional topology evidence and therefore do not appear
in a domain migration descriptor's explicit owned-object set. Any package change that
updates migration SQL or descriptors without updating the topology registry fails closed
at import or in the catalog conformance test.

This cross-binding does not authorize a migration. The bridge planner remains the
authority for supported schema state, and the v2 codec/verifier must bind both the domain
registry digest and this independent topology-registry digest.

Migration 4 also establishes a validator-level downgrade fence. The current retained test
passes a v3-only registry to the current validator; durable ledger row 4 is then newer and
the validator raises `MigrationVersionError` before mutation. This is an in-process
registry simulation, not yet a packaged historical-v3-wheel/process result. Removing the
receipt table or editing the ledger to bypass the fence is not a supported rollback, and
the mixed-wheel/process matrix remains release-blocking.

## Catalog SQL canonicalization

`canonicalize_backup_schema_sql()` exists only to compare trusted DDL with SQLite's
catalog representation. Its normalization is deliberately narrow:

1. require an exact built-in non-empty `str` of at most 64 KiB;
2. recognize only SQLite tokenizer whitespace: ASCII space, tab, LF, form feed, and CR;
3. collapse that whitespace only outside quoted regions and comments, remove it at the
   outer edges, and remove one final semicolon only when the semicolon is a plain token;
4. preserve non-token Unicode whitespace such as NBSP, plus vertical tab, byte for byte so
   SQLite identifiers cannot collide with token-separated SQL;
5. preserve single-quoted, double-quoted, backtick-quoted, and bracketed content byte for
   byte, including doubled quote delimiters;
6. preserve `--` comments, including the terminating LF, and `/* ... */` comments byte for
   byte;
7. remove `IF NOT EXISTS` only when it is in the leading
   `CREATE [UNIQUE] TABLE|INDEX|TRIGGER|VIEW` clause;
8. reject an unterminated quoted region or block comment, and reject input with no
   canonical token content.

It does not case-fold quoted or unquoted SQL and does not erase arbitrary comments or an
`IF NOT EXISTS` sequence inside a literal/identifier. The regression suite executes real
SQLite DDL to prove that NBSP-bearing identifiers and newline-sensitive `--` comments do
not share a digest with different token/constraint semantics.

## Current conformance proof

The deterministic catalog test materializes one database containing:

- event-store core schema;
- packaged migrations 1–4 and the legacy ledger, including the invocation-admission
  receipt table and its four indexes;
- projection offsets and receipts;
- durable revocation high-water state;
- the exact bridge sidecar and bootstrapped metadata.

It then reads every non-statistics `sqlite_schema` row and compares the complete
`(type, name, tbl_name, DDL digest)` mapping to all nine profiles. The current result is
63 actual objects = 63 trusted objects, with no missing, extra, or drifted coordinate.

The model tests additionally cover:

- exact scalar types and immutable bounded snapshots;
- infinite iterable rejection;
- canonical ordering and duplicate rejection;
- unknown/self/cyclic profile dependencies;
- global coordinate uniqueness;
- migration-registry equality;
- quoted SQL preservation and leading-clause-only normalization;
- SQLite-token-exact ASCII whitespace, NBSP/vertical-tab separation, and exact line/block
  comment preservation.

The frozen counts and digests are source-level conformance assertions. A passing local
test remains neither immutable CI evidence nor a production promotion. Gates A–E remain
closed until their separate release evidence and approvals exist.

## Required update procedure

Any schema-producing change must keep every intermediate commit runnable:

1. change the owning component or packaged migration;
2. update its exact topology profile and expected digests in the same behavior commit;
3. update the frozen registry/profile digest assertions;
4. run the complete catalog conformance test, all schema-owner tests, and all supported
   Python gates;
5. state compatibility impact in the migration/runbook documentation;
6. do not reuse an already published profile identifier for an incompatible topology.

Before a v2 writer is activated, the registry may evolve through reviewable versioned
checkpoints. After a v2 manifest using this identity has been published, incompatible
changes require a new topology/profile version plus an explicit reader compatibility
matrix; silently rewriting `bridge-v1` would make stored evidence ambiguous.

## Rollback

At this inactive stage, rollback is an application-code revert: no database or manifest
was written by the new module. Remove the registry and tests only while no public codec or
stored v2 artifact depends on them.

Once a v2 manifest is writable, rollback must retain a reader for every supported stored
profile. A binary that does not recognize the manifest/topology version must refuse it; it
must never reinterpret v2 evidence as v1 or migrate a backup while verifying it.

## Open work

The exact canonical manifest v2 codec and single-transaction SQLite evidence derivation
are now implemented as inactive checkpoints. The following remain separate fail-closed
stages:

1. v2 creation/publication with all existing descriptor, inode, mode, no-overwrite, and
   fsync controls;
2. quarantine verification against database bytes and exact catalog topology;
3. exact-byte restore followed by the same quarantine checks;
4. v1-to-v2 bridge rehearsal, mixed-binary rejection, RPO/RTO and effect reconciliation;
5. authenticated custody/signature policy.

Until those stages have independent evidence, production documentation and readiness
must continue to describe backup manifest v2 as unavailable.
