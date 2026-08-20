# Exact SQLite backup manifest v2 codec

## Status and release boundary

Commit `834c1d7` adds a pure, registry-bound codec for the exact format
`qe.sqlite-backup/2`. It is a compatibility-development checkpoint, not an operational
backup feature.

The active functions remain unchanged:

- `create_sqlite_backup()` creates only `qe.sqlite-backup/1`;
- `verify_sqlite_backup()` accepts only the existing exact v1 `BackupManifest`;
- `restore_sqlite_backup()` accepts only a v1 manifest and never dispatches to v2;
- the admin CLI therefore has no create, verify, or restore path for v2;
- no codec function opens SQLite, reads or writes a path, installs a migration, or starts
  a transaction.

The new public names are deliberately version-specific:

```python
from quantum_entanglement import (
    BACKUP_MANIFEST_V2_FORMAT,
    BackupManifestV2,
    decode_backup_manifest_v2,
    encode_backup_manifest_v2,
    parse_backup_manifest_v2,
)
```

`BackupManifest` continues to mean v1. There is no unversioned decoder that could make a
v2 document silently enter the v1 verifier or restore path.

Native migrations and `domain_sparse` state remain unsupported. The codec accepts only
the current canonical bridge-only `SchemaState` shapes and the exact trusted packaged
registry. Adding a future enum value or descriptor does not make it accepted by an older
binary.

## Exact document schema

The top-level dictionary contains exactly these keys:

```json
{
  "formatVersion": "qe.sqlite-backup/2",
  "backupId": "backup_<32 lowercase hex>",
  "createdAt": "2026-08-20T00:00:00.000000Z",
  "databaseSha256": "<64 lowercase hex>",
  "byteSize": 8192,
  "pageCount": 2,
  "pageSize": 4096,
  "schemaState": {},
  "registryTopology": {}
}
```

`schemaState` contains exactly:

```json
{
  "sidecarFormat": 1,
  "shape": "bridged_prefix",
  "legacySchemaVersion": 3,
  "appliedMigrations": [],
  "domainHeads": [],
  "dependencyEdges": [],
  "ownedSchemaDigests": [],
  "registrySha256": "<64 lowercase hex>",
  "stateSha256": "<64 lowercase hex>"
}
```

Each `appliedMigrations` item contains exactly:

```json
{
  "migrationId": 1,
  "filename": "0001_invocation_attempts.up.sql",
  "sqlSha256": "<64 lowercase hex>",
  "domain": "attempts",
  "domainVersion": 1,
  "kind": "legacy_bootstrap",
  "descriptorSha256": "<64 lowercase hex>",
  "ownedSchemaSha256": "<64 lowercase hex>",
  "metadataRecorded": true,
  "appliedAt": "2026-08-20T00:00:00Z"
}
```

The timestamp remains ledger evidence. It is intentionally absent from the timestamp-free
domain `stateSha256`, but is still covered by the canonical manifest bytes and must later
match the quarantined database ledger exactly.

Each `domainHeads` item contains exactly `domain`, `domainVersion`, `migrationId`,
`ownedSchemaSha256`, and exact-boolean `metadataRecorded`. Each `dependencyEdges` item
contains exactly `migrationId` and `dependsOnMigrationId`. Each `ownedSchemaDigests` item
contains exactly `domain` and `ownedSchemaSha256`.

`registryTopology` contains exactly:

```json
{
  "format": "qe.sqlite-backup-registry-topology/1",
  "registrySha256": "<same schemaState registry digest>",
  "stateSha256": "<same schemaState state digest>",
  "ownedObjects": [],
  "tableCounts": [],
  "topologySha256": "<canonical topology evidence digest>"
}
```

Each `ownedObjects` row contains exactly `migrationId`, `domain`, `objectType`, `name`,
and `ddlSha256`. The full tuple must equal the objects derived from the applied trusted
registry prefix, including order and DDL digests. A caller cannot omit an index, invent a
table, use a future descriptor, or claim an object under another domain.

Each `tableCounts` row contains exactly `name` and non-negative exact-integer `rowCount`.
The tuple:

- must be unique and ordered by UTF-8 table name;
- must include every applied registry-owned table;
- must include `qe_schema_migrations` when the applied prefix is non-empty;
- must include both `qe_schema_migration_metadata` and
  `qe_schema_migration_dependencies` when `sidecarFormat` is 1;
- may include only the bounded, explicitly named pre-registry compatibility tables;
- cannot include a table from an unapplied or future registry descriptor.

The row counts are snapshot-dependent and are not compared to hard-coded values by the
codec. They are bound into `topologySha256`; the future database verifier must compare
them with the same quarantined SQLite snapshot.

## Registry and SchemaState binding

Parsing reconstructs the existing immutable domain `SchemaState` and passes it through
the current pure bridge-only planner validation. This proves all of the following before
a v2 value is returned:

1. `registrySha256` equals the trusted packaged registry digest.
2. Applied migration IDs are an exact continuous registry prefix.
3. Filename, SQL digest, domain coordinate, kind, descriptor digest, owned-schema digest,
   and metadata state equal that prefix.
4. Domain heads, dependency edges, and per-domain owned-schema digests use canonical
   order and equal the registry-derived state.
5. `stateSha256` equals the canonical timestamp-free representation.
6. The sidecar format and shape combination is a supported bridge-only state.

The valid shape matrix is:

| Shape | Sidecar | Applied prefix | Metadata recorded | Native/sparse |
|---|---:|---|---|---|
| `sidecar_absent` | 0 | empty or current legacy prefix | false | rejected |
| `empty` | 1 | empty only | not applicable | rejected |
| `legacy_prefix` | 1 | non-empty current legacy prefix | false | rejected |
| `bridged_prefix` | 1 | non-empty current legacy prefix | true | rejected |

The test suite freezes every currently valid prefix/shape state digest. Unknown shapes,
sidecar formats, future migration IDs, native kinds, dependency drift, holes, reordering,
duplicate coordinates, and digest changes all fail closed.

## Canonical JSON and admission limits

`encode_backup_manifest_v2()` produces exactly one representation:

- UTF-8;
- `ensure_ascii=False`;
- lexicographically sorted object keys;
- no insignificant whitespace;
- no NaN or infinity;
- exactly one final LF byte;
- at most 1 MiB including that LF.

`decode_backup_manifest_v2()` accepts only exact `bytes` and requires a byte-for-byte
match with that representation after parsing. It rejects a BOM, alternate key order,
extra whitespace, omitted or extra LF, invalid UTF-8, non-finite constants, malformed or
over-deep JSON, and duplicate keys at any nesting level. Duplicate keys are rejected by
the JSON object-pairs hook before a dictionary can erase the evidence.

Every decoded object must be an exact built-in `dict`, and every decoded collection must
be an exact built-in `list`. The immutable models store bounded tuple snapshots. Direct
model construction also consumes only `maximum + 1` elements, so an infinite or hostile
iterable cannot be retained or exhausted. Serializers return fresh dictionaries and
lists; mutating caller input or a prior output cannot alter the model.

Scalars are also exact: booleans cannot stand in for integers, integer fields are
range-bounded, SHA-256 values use 64 lowercase hexadecimal characters, domains and SQLite
names use bounded ASCII grammars, page size is a supported power of two, and byte size
must equal page count times page size.

## Threats this checkpoint closes

- extending v1 with optional domain fields and calling the result compatible;
- accepting a future manifest through a permissive version dispatcher;
- presenting a modified migration descriptor or packaged SQL as trusted state;
- omitting sidecar tables or applied registry-owned tables from count evidence;
- smuggling duplicate JSON keys, bool-as-int values, duplicate rows, or alternate order;
- retaining mutable caller mappings, lists, generators, or serializer outputs;
- accidentally activating native/sparse migration through a codec enum value;
- accidentally routing v2 into the current create, verify, or restore implementation.

## Deliberately open gates

This checkpoint does not prove a usable v2 backup. The following must be separate,
reviewable stages:

1. derive `SchemaState`, registry-owned objects, and table counts from one stable copied
   SQLite snapshot;
2. create and publish an exact v2 manifest while retaining all existing descriptor,
   inode, mode, fsync, and no-overwrite controls;
3. verify database bytes, catalog DDL, row counts, ledger timestamps, sidecar rows,
   foreign keys, integrity, registry topology, and `SchemaState` in quarantine;
4. restore v2 exact bytes to a new path without migration and repeat quarantine checks;
5. rehearse v1-to-v2 bridge recovery, mixed-binary rejection, rollback, RPO/RTO, and
   external-effect reconciliation;
6. add authenticated custody or a signature/MAC policy so an attacker cannot replace the
   database and regenerate an internally consistent manifest;
7. only after those gates, separately design and default-deny native/domain-sparse
   migration execution.

Until those stages have retained evidence, use the existing v1 operations only and keep
all production gates that depend on v2 closed.

## Verification

Run from a clean source checkout with the locked development environments:

```bash
PYTHONPATH=src python -m unittest discover -s tests \
  -p 'test_backup_manifest_v2.py' -v
PYTHONPATH=src python -m unittest discover -s tests -q
ruff check .
ruff format --check .
mypy --strict --python-version 3.9 --follow-imports=skip \
  src/quantum_entanglement
python -m compileall -q src tests
git diff --check
```

Retain the exact commit, clean tree, Python 3.9/3.12 test results, canonical round-trip
fixture, and explicit evidence that `src/quantum_entanglement/backup.py` has no v2 import
or format reference.
