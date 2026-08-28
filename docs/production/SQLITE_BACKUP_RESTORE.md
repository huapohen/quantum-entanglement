# SQLite backup and restore runbook

## Release boundary

The backup module provides a fail-closed, single-host SQLite backup primitive:

- `create_sqlite_backup()` uses SQLite's online backup API so application writers may
  remain active while a consistent snapshot is copied out of the live WAL database;
- `verify_sqlite_backup()` verifies a self-contained backup through stable file
  descriptors, checks its manifest and SHA-256, and validates SQLite integrity,
  foreign keys, migration-owned schema objects, migration history, page geometry, and
  known table counts;
- `restore_sqlite_backup()` copies the exact verified database bytes to a new path,
  verifies the copied SHA-256 and SQLite evidence, and never overwrites a destination.

The descriptor and inode hardening was delivered in these slices:

- `4d693f8`: create-time source, temporary-file, parent-directory, and publication
  inode fencing;
- `9b48d5a`: stable-descriptor backup and manifest verification;
- `e60bd3c`: stable-descriptor, exact-byte restore and restore race fencing.
- `f440f65`: corrected projection checkpoint evidence to count the real
  `projection_offsets` table;
- `a1264a9`: added `projection_receipts` count evidence and fail-closed manifest
  tamper/omission tests;
- `a0a855c`: added `qe_revocation_high_water` count evidence and fail-closed manifest
  tamper/omission tests.

This is not a complete disaster-recovery service. It does not schedule backups,
replicate them, sign manifests, manage retention, encrypt files, implement point-in-time
recovery, or establish a production RPO/RTO. Those remain deployment and release gates.
This runbook does not close any of Gates A–E; all remain closed pending their independent
evidence and approval.

The inert [exact topology registry](../architecture/SQLITE_BACKUP_TOPOLOGY_REGISTRY.md)
freezes the catalog vocabulary required by a future manifest v2 verifier. The separate
[manifest v2 exact codec](../architecture/SQLITE_BACKUP_MANIFEST_V2_CODEC.md) can validate
and canonically round-trip in-memory v2 evidence for compatibility development. The
[single-snapshot derivation checkpoint](../architecture/SQLITE_BACKUP_V2_SNAPSHOT_DERIVATION.md)
can build that evidence from one caller-supplied exact SQLite connection but does not own
the file or process boundary. None of these modules is imported by the active v1
create/verify/restore path, the CLI has no v2 dispatch, and no v2 backup is operationally
readable or writable.

## Supported operating assumptions

The current release boundary is intentionally narrow:

1. One service host owns one local SQLite database.
2. The database, backup destination, and restore destination use a local filesystem with
   the POSIX semantics exercised by release tests: regular files, directory file
   descriptors, `O_EXCL`, `O_NOFOLLOW`, hard links, and directory `fsync`.
3. Each temporary file and its published filename are in the same directory, so the
   hard-link publication does not cross filesystems.
4. Backup and restore directories are writable only by the service account and trusted
   operators. Use mode `0700` or an equivalently restrictive ACL for these directories.
5. Database and manifest files are `0600`. That mode is confidentiality by local access
   control only; it is not encryption.
6. Published backups are treated as immutable. No backup retention, transfer, antivirus,
   or indexing process may rewrite them in place.
7. `/dev/fd` or `/proc/self/fd` is available for immutable SQLite reads through an
   already-open descriptor.

Do not infer support for Windows, containers with incompatible descriptor mounts,
network filesystems, FUSE/object-store mounts, clustered filesystems, or storage that
weakens hard-link or `fsync` semantics. Such environments require their own integration,
crash-consistency, and fault-injection evidence before production use.

Normal application writes through SQLite are supported during backup creation. External
pathname manipulation or direct writes to backup files are not normal operation and are
treated as integrity failures.

## Why an ordinary file copy is unsupported

The live service uses WAL mode and separate connections for events, delivery state,
invocation attempts, artifacts, and projections. Copying only `state.sqlite3` while the
service is running can omit committed WAL pages or combine files from different points
in time.

`create_sqlite_backup()` instead asks SQLite to produce one consistent snapshot. It
then changes the snapshot to a single-file DELETE-journal representation. A published
backup is therefore self-contained: verification deliberately opens the stable backup
descriptor with `immutable=1` and does not admit an adjacent, digest-external `-wal` or
`-shm` file into the verified state.

## Backup pair and manifest

Every backup is a pair:

```text
snapshot.sqlite3
snapshot.sqlite3.manifest.json
```

Neither path may already exist, including a dangling symbolic link. The manifest format
is `qe.sqlite-backup/1` and records:

- an opaque backup ID and UTC creation timestamp;
- database SHA-256 and exact byte size;
- SQLite page count and page size;
- counts for known tables that exist in the snapshot, including projection checkpoints,
  projection receipts, durable revocation high-water state, and the migration-4
  `invocation_admissions` receipt table, migration-5 native-IM inbox graph, and migration-6
  `native_im_inbound_provenance` table when those components have initialized their tables;
- each applied migration version, filename, packaged SQL checksum, and application
  timestamp.

The manifest does not contain credentials or raw artifact/event content. It is not
cryptographically authenticated, however, so custody controls must protect the database
and manifest together.

For every known table in the fixed v1 inventory—including `projection_offsets`,
`projection_receipts`, `qe_revocation_high_water`, `invocation_admissions`, the native-IM inbox
tables and `native_im_inbound_provenance`—creation records a count whenever the table exists in the
copied snapshot. Verification derives the same evidence from the opened database: changing one of
these counts or removing its manifest entry while the table remains present fails with a table-count
mismatch. This closes silent omission of projection idempotency, authorization anti-rollback state,
atomic-admission receipt inventory and native-IM admission provenance from a normally created pair.

The active v1 implementation recognizes the exact continuous migration prefix through
`0006_native_im_sandbox_provenance.up.sql`. The retained focused downgrade test gives the current
validator a registry ending at v3; it raises `MigrationVersionError` for ledger row 4 and leaves the
database unchanged. This proves the earlier validator boundary, not a historical wheel in an
independent process. Installed-wheel/process evidence for the migration-5 inbox and migration-6
provenance boundaries remains open. Never delete ledger rows or durable safety tables to make an old
binary accept the database; use the matching tested binary and an evidence-backed rollback or
restore decision.

Format version 1 intentionally remains readable for databases created before one of
these self-initializing components was enabled, so it does not require a component table
that is absent from both database and manifest. Counts are not row digests, schema
ownership proofs, or an authenticated inventory. An actor able to rewrite the database,
its SHA-256, and the manifest together can still produce a different internally
consistent pair. Preserve authenticated custody and validate component schemas and
domain watermarks before activation.

## Create path and inode fencing

Creation performs the following sequence:

1. Open the source database with `O_NOFOLLOW`, retain that descriptor as the expected
   device/inode identity, and reject non-regular files.
2. Open and retain descriptors for the backup and manifest parent directories. Reject a
   symbolic-link parent at this boundary.
3. Refuse existing target entries, including symbolic links.
4. Create unpredictable, `0600`, same-directory temporary files with `O_CREAT | O_EXCL`
   relative to the retained directory descriptors.
5. Reopen the source pathname through SQLite and use the online backup API. The pathname
   reopen is required so SQLite can find and coordinate the live source `-wal` and
   `-shm`. Source pathname-to-inode checks run immediately around this work and again
   before publication.
6. Convert the copied database to DELETE journal mode, run integrity and schema evidence
   checks, close SQLite, `fsync` the database, and calculate its SHA-256 from the retained
   temporary-file descriptor.
7. Serialize and `fsync` the manifest through its retained descriptor.
8. Publish each file with a hard link relative to the retained parent descriptor. Verify
   that every published entry still has the expected device/inode and `fsync` the parent
   directories.
9. Run full public verification, then recheck both published entries and both parent
   directory identities before returning success.
10. On failure, unlink a temporary or published entry only if it still has the
    device/inode created by this attempt. A replacement entry is left untouched for the
    operator to investigate.

The source descriptor anchors identity but is not the descriptor SQLite reads from;
CPython's `sqlite3` API cannot both adopt an already-open descriptor and retain the live
source pathname needed for WAL discovery. This leaves a narrow pathname ABA boundary:
an actor with directory write permission could theoretically replace the source and put
the original pathname back between checks. Exclusive directory permissions are therefore
a production prerequisite, not an optional defense.

### Create command

```python
from quantum_entanglement import create_sqlite_backup

manifest = create_sqlite_backup(
    "/var/lib/quantum-entanglement/state.sqlite3",
    "/var/backups/quantum-entanglement/2026-08-20T020000Z.sqlite3",
)
print(manifest.backup_id)
```

```bash
qe-admin --compact backup \
  --source /var/lib/quantum-entanglement/state.sqlite3 \
  --destination /var/backups/quantum-entanglement/2026-08-20T020000Z.sqlite3
```

The installed admin commands return compact JSON on stdout after success, including the
operation, paths, and manifest. Operational failures return structured JSON on stderr
with exit status `1`; argument syntax errors retain argparse's exit status `2`. Capture
stdout, stderr, and the process exit status as separate evidence streams.

Operational sequence:

1. Record service health, the latest durable event/global position, queue age, running
   attempts, DLQ count, and open ambiguity count.
2. Confirm restrictive ownership/permissions on the live, backup, and temporary-file
   directories.
3. Confirm free space for a complete snapshot, manifest, retained WAL growth during the
   online copy, and operational reserve.
4. Choose a new destination name. Never delete or reuse an old target to make a command
   succeed.
5. Run the command as the service account, not as root.
6. Record start time, completion time, source watermark observations, backup ID, byte
   size, SHA-256, and migration versions.
7. Verify the pair again from a separate process.
8. Transfer the pair to approved encrypted, immutable off-host storage.
9. Verify the transferred pair at its destination and retain that evidence.

A long online backup can retain WAL pages and increase live-disk consumption. Monitor
free space, WAL size, copy duration, and writer latency throughout the operation.

## Stable-descriptor verification

```python
from quantum_entanglement import verify_sqlite_backup

manifest = verify_sqlite_backup("/var/backups/quantum-entanglement/2026-08-20T020000Z.sqlite3")
```

```bash
qe-admin --compact verify-backup \
  --backup /var/backups/quantum-entanglement/2026-08-20T020000Z.sqlite3
```

Verification does not perform a check-then-reopen sequence for backup content. It:

1. Opens stable backup/manifest parent directory descriptors.
2. Opens each regular file once, relative to its retained parent, with `O_NOFOLLOW`.
3. Reads and parses the bounded manifest from the stable manifest descriptor.
4. Checks exact byte size and SHA-256 from the stable backup descriptor.
5. Opens SQLite through `/dev/fd/<n>` or `/proc/self/fd/<n>` using read-only immutable
   mode, so a pathname replacement or adjacent WAL cannot change the database being
   checked.
6. Runs `integrity_check`, `foreign_key_check`, exact packaged migration schema-object
   validation, ledger/version/checksum validation, page geometry checks, and known table
   counts.
7. Rehashes the backup, rereads the manifest, and rechecks file and parent identities
   before returning.

Verification fails closed on, among other cases:

- a missing, non-regular, or symbolic-link database/manifest;
- a symbolic-link or replaced parent directory;
- path replacement after either file was opened;
- in-place content change observed between checks;
- an unknown, malformed, oversized, or non-canonical manifest;
- byte-size or SHA-256 drift;
- invalid SQLite structure or a foreign-key violation;
- a missing or weakened migration-owned table/index despite a plausible ledger;
- a future, gapped, renamed, or checksum-drifted migration history;
- page geometry, known table count, or migration evidence drift, including a changed or
  omitted projection-receipt or revocation-high-water count for a table present in the
  backup.

A successful verification proves that the bytes observed through the stable descriptors
matched the manifest and passed the implemented SQLite checks. It does not authenticate
who created the pair, and it cannot prevent a writer from modifying a file after the
final check. An in-place writer that changes and restores identical content entirely
between two observations is also a theoretical content ABA boundary.

## Exact-byte restore

Restore always targets a new filename. Keep service traffic stopped while restoring:

```python
from quantum_entanglement import restore_sqlite_backup

restore_sqlite_backup(
    "/var/backups/quantum-entanglement/2026-08-20T020000Z.sqlite3",
    "/var/lib/quantum-entanglement/restore-2026-08-20.sqlite3",
)
```

```bash
qe-admin --compact restore-backup \
  --backup /var/backups/quantum-entanglement/2026-08-20T020000Z.sqlite3 \
  --destination /var/lib/quantum-entanglement/restore-2026-08-20.sqlite3
```

The restore implementation:

1. Retains stable descriptors and identities for the backup parent, manifest parent,
   destination parent, backup file, and manifest file.
2. Refuses an existing or symbolic-link destination and a symbolic-link destination
   parent.
3. Runs full public verification and then proves the paths still refer to the files that
   restore opened before verification.
4. Parses the manifest again from the stable descriptor and requires it to equal the
   public verification result. It rechecks source size, SHA-256, SQLite integrity, schema,
   migration evidence, geometry, and counts through the stable backup descriptor.
5. Creates a `0600`, `O_EXCL`, same-directory restore temporary file.
6. Copies the exact verified backup bytes from the stable backup descriptor. It checks
   the copy-time byte count and SHA-256, `fsync`s the file, rehashes it, and validates its
   SQLite evidence.
7. Rehashes/rereads the source backup and manifest before publication to detect observed
   in-place mutation during restore.
8. Publishes the destination with a no-overwrite hard link relative to the retained
   destination parent, checks the published inode, `fsync`s the directory, and performs
   final source/manifest/destination content and identity checks.
9. Removes a temporary or published destination on failure only when its device/inode
   still matches the file created by this restore. It never deliberately unlinks a
   replacement operator file.

At publication, the destination database bytes and SHA-256 are exactly equal to the
verified backup database. Restore no longer reserializes the database with another
SQLite online-backup pass, so table counts and page geometry are not being used as a
substitute for full content identity.

## RPO, RTO, and capacity planning

No production RPO or RTO is established by this module.

RPO depends on backup frequency and the durable application watermark captured by the
SQLite snapshot. The manifest `createdAt` value is generated after the database copy; it
is not a transaction commit watermark and must not be used alone to calculate lost work.
Record durable event/global positions immediately before and after backup and compare
them during a drill.

RTO includes all of the following:

- locating and authorizing the selected backup pair;
- stable verification, including full-file hashing and SQLite integrity scans;
- one complete byte copy to the restore filesystem;
- destination rehashing and SQLite evidence validation;
- domain-level reconciliation, projection checks, lease fencing, smoke tests, and staged
  traffic activation.

The fail-closed implementation intentionally performs multiple sequential full-file and
integrity passes. Do not estimate RTO from database size divided by raw storage bandwidth.
Benchmark the largest expected database on the actual deployment filesystem and service
account. Record median, p95, and worst observed create/verify/restore/activation times,
peak temporary space, source WAL growth, and application latency during online backup.

Until measured otherwise, reserve at least one full destination copy plus WAL growth and
operational headroom for creation, and one full destination copy plus headroom for
restore. Hard-link publication does not create a second copy of the restore temporary
file, but the verified source backup remains separately allocated.

## Fault-injection matrix

The release evidence must distinguish automated coverage from drills still required in
the deployment environment.

| Fault | Current automated expectation | Production evidence required |
|---|---|---|
| Source is a symlink | Create rejects it | Permission and path-policy check |
| Source pathname is replaced during create | Create aborts before publication | Repeat on deployment filesystem |
| Backup/manifest parent is a symlink | Operation rejects it | Directory ownership/ACL evidence |
| Destination already exists or is a dangling symlink | No overwrite | Operator collision drill |
| Temporary or published create entry is replaced | Abort; cleanup leaves mismatched inode | Alert and forensic procedure |
| Manifest publication collides | Abort; remove only the owned database link | Concurrent creator drill |
| Backup/manifest path is replaced during verify | Verify continues on stable FD then fails identity check | Repeat under deployment mount |
| Backup/manifest is changed in place during verify | Digest/reread check fails | Repeat under deployment mount |
| Backup parent is replaced during verify | Parent identity check fails | Repeat under deployment mount |
| Migration ledger is future/gapped/drifted, including current v6 opened by an older registry | Backup/verify fails closed before mutation | Upgrade/rollback compatibility drill |
| Migration-owned schema is missing/weakened | Schema congruence check fails | Corruption quarantine exercise |
| Projection receipt count is changed or omitted from a normally created manifest | Verification fails with table-count drift | Compare receipt/checkpoint identities and positions after restore |
| Revocation high-water count is changed or omitted from a normally created manifest | Verification fails with table-count drift | Compare every tenant revision and digest before authorization is enabled |
| Backup/manifest is replaced after restore verification | Restore detects anchored-inode mismatch | Repeat under deployment mount |
| Backup/manifest changes in place during copy | Restore aborts; no destination is published | Repeat under deployment mount |
| Destination appears before hard-link publication | Atomic link fails; operator file remains | Concurrent restore drill |
| Restore temp or published destination is replaced | Abort; cleanup refuses mismatched inode | Forensic and cleanup drill |
| Destination parent is replaced or is a symlink | Restore rejects it | Directory isolation drill |
| Event, in-flight outbox, open ambiguity, running attempt, and artifact coexist | Exact-byte restore preserves all tested rows | Release-candidate domain rehearsal |
| Disk becomes full | Operation must fail; partial entries require ownership-aware handling | Mandatory ENOSPC drill; not yet automated |
| Process is killed between file and manifest publication | Pair may be incomplete and must not verify | Mandatory kill-point and orphan cleanup drill |
| Power loss around file/directory `fsync` | Depends on real filesystem guarantees | Mandatory crash/power-loss storage qualification |

Network filesystem behavior is deliberately absent from the automated guarantee. Do not
turn a deployment-specific successful test into a general network-filesystem claim.

## Recovery drill

The committed automated rehearsal covers one database containing:

- a domain event;
- an in-flight transactional outbox message;
- an open outbox ambiguity record;
- a running invocation and its attempt row;
- a durable artifact blob/version.

It asserts exact source/destination database bytes before stores reopen the destination,
then reopens event/delivery, invocation-attempt, and artifact stores and verifies the
records remain readable.

A focused migration-4 rehearsal separately seeds a non-empty durable
invocation-admission receipt bound to its job and event batch. It proves backup/restore
preserves the receipt, exact replay still returns it, and its foreign keys remain valid.

Focused manifest tests additionally seed a real projection offset and receipt plus a
durable tenant revocation high-water row. They assert that backup creation records these
counts and that verification rejects changed or omitted receipt/high-water entries. Those
tests verify backup evidence behavior; they do not replace a deployment recovery drill
that compares row identities, positions, revisions, and digests after restore.

Before every production release, extend the drill on a disposable deployment-equivalent
host:

1. Seed representative pending, running, successful, failed, canceled, approval, DLQ,
   published, ambiguity, invocation-admission, artifact-version, inbox-receipt,
   projection-offset, projection-receipt, action-receipt, and tenant
   revocation-high-water states that exist in that release.
2. Record domain counts, artifact digests, event/global positions, projection offsets,
   projection receipt event IDs/positions, active lease epochs, every tenant revocation
   revision/state digest, open ambiguity IDs, and the release binary revision.
3. Keep normal WAL-backed connections active and create the online backup.
4. Verify the local pair and its off-host copy.
5. Simulate loss only in the disposable environment.
6. Restore to a new path with the exact release candidate binary.
7. Confirm the destination SHA-256 equals the manifest and source backup SHA-256 before
   opening any read/write store.
8. Run independent `integrity_check`, foreign-key, schema/migration, artifact digest,
   event replay, projection checkpoint/receipt comparison, and revocation high-water
   comparison checks. Reopen the projection and revocation-guard components with the
   release binary so their owned-schema validators run.
9. Confirm pre-restore invocation and delivery owners cannot resume unsafe work. Wait for
   or explicitly fence all old leases using the release's approved recovery procedure.
10. Confirm open ambiguities remain quarantined and are not retried automatically.
11. Run application-level smoke reads without enabling external connectors.
12. Measure observed RPO and every RTO phase, and attach commands, versions, logs, counts,
    timings, and operator sign-off to release evidence.

Do not accept a laptop or temporary-filesystem result as production storage evidence.

## Activation after restore

Restoring valid bytes is only the first recovery phase. Keep ingress, workers, schedulers,
and external connectors disabled until all steps pass:

1. Stop every process that can open the old database, including ad hoc workers and admin
   shells. Confirm no process still uses its main, WAL, or shared-memory files.
2. Preserve the old database, `-wal`, and `-shm` as a read-only forensic set. Never keep
   only the old main file when WAL may contain committed state.
3. Verify the selected backup pair with the exact release binary and record its SHA-256,
   byte size, migration evidence, and domain counts.
4. Restore to a new filename. Never restore over the configured live path.
5. Verify the new destination independently and confirm its SHA-256 equals the manifest.
6. Point an offline diagnostic process at the restored path. Do not apply unplanned
   migrations during validation.
7. Run schema/migration checks, artifact verification, event replay, projection rebuild
   and receipt comparison, revocation high-water comparison, queue/DLQ/ambiguity
   inspection, and audit sampling. Any lower or conflicting tenant revision is an
   activation blocker.
8. Establish that every lease issued before the snapshot/incident is expired or fenced.
   Do not accept a completion solely because it carries a token preserved in the backup.
9. Reconcile each effect-unknown/open ambiguity with external evidence before retrying or
   marking it published/dead-letter.
10. Change the configured path through the deployment's audited configuration mechanism.
11. Start one instance with ingress and connectors still disabled. Run readiness and
    smoke reads for sessions, tasks, events, artifacts, attempts, admission receipts,
    outbox, ambiguities, projections, and audit data.
12. Enable internal writers first, then workers, then explicitly approved external
    connectors. Observe errors, queue age, duplicate-effect signals, WAL growth, and
    integrity alerts through the documented recovery window.

If any check fails, do not open traffic. Preserve the restored candidate and evidence,
then follow the rollback decision below.

## Rollback and failure handling

Restore is non-destructive: it creates a new path and never renames or replaces the live
database. Rollback is therefore an operator-controlled configuration decision, not a
library filesystem action.

Before any traffic is admitted on the restored database, rollback may select the old
database only if its main/WAL/SHM set is intact and the incident commander confirms that
it is the authoritative timeline. Stop all processes before changing the configured
path.

After the restored database has accepted writes or triggered effects, a simple path
switch can discard new durable work or duplicate external effects. At that point treat
rollback as incident reconciliation: freeze traffic, preserve both timelines, compare
event/outbox/ambiguity/audit evidence, and approve a forward repair. Never merge SQLite
files or edit the migration ledger manually.

| Failure | Required response |
|---|---|
| Target already exists | Choose a new destination; never remove the competing entry automatically |
| Live snapshot integrity/schema failure | Quarantine the attempted backup and investigate the live database |
| Digest, content, count, geometry, or migration mismatch | Mark the pair invalid and do not restore it |
| Disk full during create/restore | Stop retry loops; identify only owned partials by path and inode before cleanup |
| Pair contains only database or only manifest | Treat it as incomplete; do not synthesize the missing member |
| Path/inode replacement is detected | Abort, preserve the replacement, and investigate directory access |
| Post-publication restore check fails | Remove only the destination inode created by that restore |
| An older binary rejects current v6 migration history | Use the matching tested v6 binary/backup; never edit checksums, ledger rows, inbox tables, or provenance |
| Activation validation fails before traffic | Keep traffic closed; preserve candidate and select an evidence-backed rollback path |
| Validation fails after traffic/effects | Freeze both timelines and perform incident reconciliation; do not blindly switch back |

Never run a destructive down migration on the only backup copy, delete the previous
database before the recovery observation window ends, or clean wildcard `*.partial`
paths without verifying ownership and inode identity.

## Security and custody

- Treat the database as confidential: it may contain prompts, event payloads, artifact
  bytes, identities, audit details, and connector metadata.
- Keep database and manifest together on encrypted media and transfer them over an
  authenticated channel.
- Add immutable retention, legal hold, and audited deletion outside this library.
- Restrict directory write access. Stable descriptors prevent ordinary path replacement
  from changing the opened object, but they do not make same-account direct writes safe.
- Do not place credentials, tokens, or personal data in filenames, manifests, command
  transcripts, or release tickets.
- Record every create, transfer, verification, restore, activation, rollback decision,
  and deletion in the approved immutable audit system.

The SHA-256 manifest is an integrity comparison, not a signature or MAC. An actor able to
replace both files can create a new internally consistent pair. Off-host custody must add
authentication and independent access audit.

## Residual race boundaries

The implementation materially narrows path races but does not claim a hostile
same-account filesystem sandbox:

- create must reopen the source pathname through SQLite to retain live WAL semantics;
- device/inode cleanup is a guarded stat-then-unlink sequence, not a kernel-provided
  compare-and-unlink primitive;
- an in-place writer can theoretically perform content ABA entirely between two digest
  observations;
- any external writer can modify an inode after the function's final check and return;
- authenticated storage is still required to distinguish an approved pair from a
  maliciously regenerated database and manifest;
- crash durability ultimately depends on the actual filesystem, mount, controller, and
  power-loss semantics.

Use exclusive service-account directories, immutable off-host retention, deployment
filesystem qualification, and post-operation monitoring to control these boundaries.

## Release evidence

Run at least the following from a clean worktree:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_backup.py' -v
PYTHONPATH=src python3 -m unittest discover -s tests -q
ruff check src/quantum_entanglement/backup.py tests/test_backup.py
ruff format --check src/quantum_entanglement/backup.py tests/test_backup.py
mypy --strict --python-version 3.9 --follow-imports=skip \
  src/quantum_entanglement/backup.py
python3 -m compileall -q src tests
git diff --check
```

Attach the following to the stage/release record:

- Git commit and clean-tree status;
- OS/kernel, Python, SQLite, filesystem/mount, storage, and container/runtime versions;
- service account, directory owner/group/mode/ACL, and available-space evidence;
- source and backup byte sizes, manifest, SHA-256, migration evidence, projection
  checkpoint/receipt evidence, revocation revision/state digests, and other durable domain
  watermarks;
- focused and full test output, including the fault-injection cases;
- create/verify/restore timings, source WAL growth, peak disk use, and observed workload
  latency;
- exact-byte/domain recovery drill output and activation checklist sign-off;
- off-host transfer verification and retention/authentication evidence;
- measured RPO/RTO result or an explicit release blocker if no approved target exists.

Do not translate unit-test success into a cross-platform or production RPO/RTO claim.

## Open production gates

- scheduled backup orchestration and alerting;
- signed or MAC-authenticated manifests;
- encryption/KMS metadata and restore-time key policy;
- remote immutable object storage, retention, legal hold, and expiry;
- incremental backup and point-in-time recovery;
- cancellation/rate controls and benchmark evidence for the largest supported database;
- measured, approved RPO/RTO targets and recurring recovery drills;
- automated ENOSPC, kill-point, and deployment-filesystem power-loss testing;
- automatic service-level read-only quarantine after corruption detection;
- qualification for every supported OS/filesystem/runtime combination.

Until those gates are met, describe this implementation as a hardened local SQLite
backup/restore primitive with tested race defenses, not as a complete commercial
disaster-recovery system.
