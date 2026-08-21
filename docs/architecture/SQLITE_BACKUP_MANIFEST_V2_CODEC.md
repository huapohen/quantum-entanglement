# SQLite backup manifest v2 exact codec

## Status and release boundary

This checkpoint defines and validates the exact in-memory and canonical-JSON form of
`qe.sqlite-backup/2`. It is compatibility infrastructure, not an operational backup or
restore path.

The active production-facing surface remains version 1:

- `create_sqlite_backup()` writes only `qe.sqlite-backup/1`;
- `verify_sqlite_backup()` and `restore_sqlite_backup()` accept only the exact v1
  `BackupManifest`;
- the admin CLI has no v2 command or version dispatch;
- `backup.py` contains no v2 import, symbol, or format string;
- after explicit versioned-submodule initialization, codec construction, parsing, encoding,
  and decoding open no database, file, directory, descriptor, or transaction and perform no
  migration.

The package root deliberately exports no v2 symbol. Compatibility code must explicitly
import `quantum_entanglement.backup_manifest_v2`, whose initialization imports the topology
and domain-migration registries and reads the packaged `*.up.sql` resources to cross-bind
their identities. A cold `import quantum_entanglement` does not perform those reads.
Supplying a v2 value or JSON document to a v1 API remains an error. No operator should
create, publish, verify, or restore a v2 backup at this stage.

This code may be deployed as an inert explicit-submodule codec. Its operations are pure
after the documented registry initialization boundary. It does not close Gate C,
establish an RPO/RTO, or make a v2 artifact recoverable.

The later [single-snapshot derivation checkpoint](SQLITE_BACKUP_V2_SNAPSHOT_DERIVATION.md)
uses the codec's exact factories to construct schema and topology models from one SQLite
read view. It remains internal and does not change this operational boundary.

## Why the codec is a separate stage

Manifest bytes are a stored compatibility and security boundary. A writer must not be
activated before readers agree on one exact representation, and a reader must not infer
schema ownership from a loose collection of table names. Separating the codec from
snapshot derivation and filesystem publication provides three useful properties:

1. every accepted byte sequence has one canonical re-encoding;
2. the schema-state and catalog-topology evidence can be reviewed without live SQLite or
   pathname races obscuring the model;
3. v1 operational behavior stays unchanged while v2 rejection, limits, and cross-binding
   are exercised on all supported Python versions.

The codec is deliberately not a generic JSON schema library. It recognizes exactly the
current bridge-only migration vocabulary and the frozen topology registry shipped by the
same binary.

## Exact top-level model

The top-level object has exactly these nine fields and rejects missing or additional
fields:

| Field | Exact contract |
|---|---|
| `formatVersion` | exact built-in string `qe.sqlite-backup/2` |
| `backupId` | `backup_` followed by 32 lowercase hexadecimal characters |
| `createdAt` | canonical UTC RFC 3339 with exactly six fractional digits |
| `databaseSha256` | 64 lowercase hexadecimal characters |
| `byteSize` | exact positive integer no greater than `2^63 - 1` |
| `pageCount` | exact positive integer no greater than SQLite's supported maximum |
| `pageSize` | exact integer power of two from 512 through 65,536 |
| `schemaState` | exact current `SchemaState` evidence described below |
| `registryTopology` | exact present-profile, catalog-object and row-count evidence |

`byteSize` must equal `pageCount * pageSize`; the geometry cannot merely be plausible in
isolation. Booleans are not admitted as integers, subclasses are not admitted as exact
scalars or top-level containers, and digest or identifier casing is not normalized.

The dataclasses are frozen and all collection fields become detached tuples. Construction
and serialization take bounded snapshots of direct-model iterables. This protects the
codec even if a caller supplies a hostile or infinite iterable, or uses low-level mutation
to alter a frozen object after initial validation.

## Schema-state evidence

`schemaState` carries the exact bridge planner state rather than only the legacy migration
number:

- sidecar format and recognized shape;
- legacy schema version;
- ordered applied migrations, including packaged SQL, descriptor, owned-schema and
  timestamp evidence;
- ordered domain heads and metadata-recorded flags;
- ordered dependency edges;
- ordered per-domain owned-schema digests;
- the domain migration registry digest and the complete state digest.

Every accepted value is reconstructed as the shared immutable `SchemaState` model and
passed through `plan_bridge_migrations()`. Therefore a syntactically valid but future,
native, sparse, gapped, reordered, descriptor-drifted, or otherwise non-canonical state is
not accepted by this inactive reader. Current canonical prefixes in the recognized
`sidecar_absent`, `legacy_prefix`, `bridged_prefix`, and empty states are covered by
round-trip tests.

This is intentionally strict binary affinity. It is not yet a policy for reading every
future v2 state. Before any v2 artifact is published, the project must define which older
reader versions can restore which topology and migration-registry identities.

## Registry-topology evidence

`registryTopology` binds four layers of evidence:

1. `qe.sqlite-topology/bridge-v1` and the exact topology-registry SHA-256;
2. the same domain-registry and state SHA-256 values carried by `schemaState`;
3. the canonical ordered set of present profiles and every exact catalog object belonging
   to those profiles;
4. one canonical row count for every table in the present exact profiles.

The nested `topologySha256` is SHA-256 over canonical JSON for the topology evidence
excluding the digest field itself. It binds profile presence, profile digests, object
coordinates and DDL digests, row counts, and both registry/state identities.

Validation requires:

- every profile to exist in the shipped trusted registry and carry its exact digest;
- only migration-applicable profiles plus the three optional initialized component
  profiles;
- every applied migration's profile and the legacy ledger profile;
- the sidecar profile when sidecar format 1 is present;
- every transitive direct profile dependency;
- the exact complete catalog object tuple for the present profiles, with no extra,
  missing, duplicate, or reordered coordinate;
- exactly one row count for every table in those profiles and no unknown count;
- the legacy ledger count to equal the number of applied migrations;
- sidecar metadata and dependency counts to agree with the schema-state shape and edges.

Dynamic table counts are evidence, not row-content digests. An attacker able to replace
the database bytes and an unauthenticated manifest together can still create a different
internally consistent pair. Authenticated custody remains required.

## Canonical JSON contract

`encode_backup_manifest_v2()` produces exactly one byte representation:

- UTF-8 without a byte-order mark;
- JSON object keys sorted lexically;
- no insignificant whitespace;
- non-ASCII characters emitted directly, although admitted identifiers are currently
  restricted to the canonical ASCII vocabularies;
- no `NaN`, infinities, decimals, or floats;
- exactly one trailing line feed.

`decode_backup_manifest_v2()` accepts exact built-in `bytes` only. After parsing and model
validation it re-encodes the value and compares all input bytes. Missing final LF, extra
LF, leading/trailing whitespace, alternate key ordering, escaped-equivalent spellings, or
pretty-printed JSON therefore fail closed rather than being silently normalized.

JSON duplicate keys are rejected during object construction. Integer tokens are admitted
lexically before Python integer conversion: at most 19 digits, canonical leading-zero
form, and the signed 64-bit range. This avoids turning a small manifest into an unbounded
big-integer conversion workload before field-level bounds run.

## Resource limits and hostile input

The decoder refuses empty input and documents larger than 1 MiB before UTF-8 decoding.
Malformed UTF-8, malformed or excessively nested JSON, duplicate keys, non-finite values,
and decimal tokens fail closed. Nested collections are exact JSON lists and have explicit
limits inherited from the migration registry or the codec:

- applied migrations and dependency rows: the domain-migration maximum;
- domain heads and owned-schema digests: the migration-domain maximum;
- topology profiles: 64;
- catalog objects: 8,192;
- table counts: 4,096.

Direct dataclass construction uses the same limits and consumes only `limit + 1` values
when determining that an iterable is oversized. Serialization rebuilds every nested exact
model into a bounded snapshot before walking it and refuses any low-level-mutated model
that differs from that canonical snapshot.

These limits constrain codec memory and CPU exposure but are not a complete service-layer
request budget. An eventual admin/API entry point must also impose request deadlines,
rate limits, file-descriptor limits, and bounded quarantine verification.

## Compatibility matrix at this checkpoint

| Caller/artifact | v1 active APIs | explicit v2 codec | Operationally supported |
|---|---:|---:|---:|
| canonical `qe.sqlite-backup/1` pair | read/write/restore | rejected as v2 | yes, within the v1 runbook boundary |
| canonical in-memory v2 dictionary | rejected by v1 model | parse/encode | no |
| canonical `qe.sqlite-backup/2` JSON bytes | rejected by v1 model/path | decode/round-trip | no |
| non-canonical or future v2 | rejected | rejected | no |

There is no downgrade conversion. V2 evidence cannot be discarded and reinterpreted as a
v1 manifest, and v1 evidence cannot be promoted by changing only `formatVersion`.

## Versioned submodule API and reachability invariant

The unqualified package-root `BackupManifest` remains the exact v1 type for source
compatibility, and the package root exposes no `BackupManifestV2`, parser, encoder, or
decoder. The v2 names exist only in the explicitly imported, versioned
`backup_manifest_v2` submodule. Tests inspect both the package root and active `backup.py`
source/function globals to require that v2 names, imports, and the v2 format string remain
unreachable from create, verify, and restore.

Any future activation must be explicit: a new version-dispatch boundary and separate v2
writer/verifier/restore implementation, with tests proving that a version is selected only
after bounded canonical parsing. Importing this codec from the current v1 operational
module would violate this checkpoint.

## Verification evidence

The codec suite covers:

- all current bridge-only schema prefixes and shapes;
- exact v1/v2 public type separation and active-path non-reachability;
- immutable detached models and bounded hostile iterables;
- exact fields, scalar types, identifiers, timestamps, hashes, geometry, ordering and
  duplicate rejection at every nested layer;
- migration-registry, schema-state, topology-profile, catalog-object, dependency and table
  count cross-binding;
- duplicate-key, invalid UTF-8, malformed/deep/oversized JSON, float, non-finite,
  oversized-integer and non-canonical-byte rejection;
- low-level frozen-model mutation before serialization;
- no SQLite, filesystem, or migration side effect from codec operations after explicit
  submodule initialization;
- zero packaged-migration SQL reads during a cold package-root import.

Observed local source verification for this checkpoint:

| Gate | Result |
|---|---|
| Python 3.9 / 3.12 / 3.13 codec tests with warnings as errors | 34/34 each |
| Python 3.9 / 3.12 / 3.13 topology tests with warnings as errors | 17/17 each |
| Python 3.9 / 3.12 / 3.13 full unittest | 897/897 each |
| Ruff lint and format | pass |
| strict mypy | 38 source files pass |
| dependency locks | 4 targets / 74 package records verified |

These are local checks, not immutable CI evidence, independent release review, or
production promotion. The exact commit identity must be added after committing and the
candidate must be reviewed from a clean checkout before integration.

## Rollback

Because no v2 file is created or consumed operationally, rollback is an application-code
rollback: remove the explicitly imported versioned submodules, tests, and documentation
together. The package root and v1 database/backup bytes require no change or data rollback.

That simple rollback rule expires the moment any v2 bytes are published. From that point,
every supported release must retain a reader and quarantine verifier for every accepted
stored v2 identity, or explicitly refuse startup/promotion while those artifacts remain in
the recovery set. A writer must never be enabled behind a flag that can outlive its reader.

## Remaining activation sequence

The exact single-transaction SQLite evidence derivation is now implemented as another
inactive checkpoint. The next stages remain separate fail-closed changes:

1. publish the database and v2 manifest with the existing descriptor, inode, permission,
   no-overwrite, cleanup and directory-`fsync` controls;
2. verify v2 in quarantine against exact database bytes, catalog topology, schema state,
   integrity, foreign keys and row-count evidence;
3. restore exact bytes to a new path and repeat all quarantine checks before activation;
4. rehearse v1/v2 mixed binaries, crash points, rollback/forward-fix, effect reconciliation,
   and measured RPO/RTO;
5. add authenticated custody/signature and key-rotation policy.

Until every stage has independent and combined evidence, readiness documentation must
continue to report manifest v2 as unavailable for backup or recovery.
