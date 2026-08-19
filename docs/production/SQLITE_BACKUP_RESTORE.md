# SQLite backup and restore runbook

## Supported boundary

The Phase 1 backup module creates a transactionally consistent snapshot of the shared
SQLite database with SQLite's online backup API. It verifies the snapshot, writes a
canonical manifest, and publishes both files without overwriting an existing path.
Restore verifies the source again and publishes a new database atomically.

This runbook supports one service host and one SQLite database. It does not provide
point-in-time recovery, remote replication, automated retention, encryption, or an RPO/
RTO claim. Those remain release gates, not assumptions.

## Why ordinary file copy is unsupported

The service uses WAL mode and separate connections for events, invocation attempts,
artifacts, delivery state, and projections. Copying only `state.sqlite3` while writers
are active can omit committed WAL pages or capture mutually inconsistent files.

Supported backups use `create_sqlite_backup()`, which asks SQLite to copy one consistent
read snapshot into a new database. It then changes the snapshot to a single-file DELETE
journal representation, runs integrity checks, closes it, fsyncs it, and calculates the
final file digest.

## Backup files

Each backup consists of:

```text
snapshot.sqlite3
snapshot.sqlite3.manifest.json
```

Both are created with mode `0600`. Neither path may already exist. The implementation
uses same-directory temporary files and hard-link publication, so a concurrent creator
cannot be silently overwritten between an existence check and final publication.

The manifest format is `qe.sqlite-backup/1` and records:

- opaque backup ID and store-owned UTC timestamp;
- database SHA-256 and byte size;
- SQLite page count and page size;
- counts for known core tables;
- every applied migration version, filename, checksum, and application timestamp.

The manifest contains no credentials or raw artifact/event content.

## Create a backup

Python API:

```python
from quantum_entanglement import create_sqlite_backup

manifest = create_sqlite_backup(
    "/var/lib/quantum-entanglement/state.sqlite3",
    "/var/backups/quantum-entanglement/2026-08-20T020000Z.sqlite3",
)
print(manifest.backup_id)
```

Installed command:

```bash
qe-admin --compact backup \
  --source /var/lib/quantum-entanglement/state.sqlite3 \
  --destination /var/backups/quantum-entanglement/2026-08-20T020000Z.sqlite3
```

Success is JSON on stdout with `ok`, `operation`, `paths`, and the complete manifest.
Operational failures are JSON on stderr with a stable error code and exit status `1`;
argument syntax failures retain argparse's exit status `2`.

Operational procedure:

1. Check service health and current queue/attempt/DLQ/ambiguity counts.
2. Ensure the destination filesystem has enough space for at least the live database
   plus temporary copy and operational reserve.
3. Choose a new, immutable destination name. Never reuse yesterday's path.
4. Run the backup under the service account, not as root.
5. Confirm the API returns successfully.
6. Run explicit verification again from a separate process.
7. Record backup ID, digest, size, schema versions, start/end time, and operator in
   release/operations evidence.
8. Copy the pair to encrypted off-host storage using the approved transport.
9. Verify the off-host copy after transfer.

The application may continue accepting writes while SQLite copies the snapshot. A long
backup can retain WAL pages and increase disk use, so monitor free space and duration.

## Verify a backup

```python
from quantum_entanglement import verify_sqlite_backup

manifest = verify_sqlite_backup(
    "/var/backups/quantum-entanglement/2026-08-20T020000Z.sqlite3"
)
```

```bash
qe-admin --compact verify-backup \
  --backup /var/backups/quantum-entanglement/2026-08-20T020000Z.sqlite3
```

Verification fails closed on:

- missing or symbolic-link database/manifest paths;
- malformed, unknown, or non-canonical manifest structure;
- size or SHA-256 mismatch;
- SQLite `integrity_check` failure;
- any foreign-key violation;
- page geometry mismatch;
- core table count mismatch;
- migration version/filename/checksum/timestamp mismatch.

A successful checksum alone is not sufficient. It can prove that a file still matches
the manifest, but not that an attacker with write access did not replace both. Production
off-host storage must add authenticated encryption, immutable retention, and access
audit.

## Restore to a new path

Restore never overwrites a path. Stop the service and restore to a new filename:

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

The restore function:

1. Rejects an existing/symlink destination.
2. Verifies the backup and manifest in full.
3. Uses SQLite backup into a same-directory `0600` temporary database.
4. Runs integrity, foreign-key, page, count, and migration checks on the restored copy.
5. Rechecks the source backup digest to detect mutation during restore.
6. Fsyncs and publishes the destination without overwrite.
7. Reopens the published destination read-only and verifies it again.
8. Removes the new destination if post-publication verification fails.

## Recovery drill

Run this drill on a disposable host before every Phase 1 release and at the documented
operations cadence:

1. Seed a database with:
   - completed and running invocation attempts;
   - a pending approval;
   - multiple artifact versions;
   - pending, published, DLQ, and ambiguity delivery records;
   - projection offsets and action receipts once those schemas are present.
2. Keep normal SQLite connections open and create an online backup.
3. Record the last accepted command time and backup completion time.
4. Destroy only the disposable live database.
5. Restore to a new path.
6. Open the restored database with the exact release candidate binary.
7. Run migration checksum validation without applying unplanned migrations.
8. Run `integrity_check`, foreign-key check, artifact digest scan, event replay, and
   projection comparison.
9. Confirm invocation lease recovery fences old owners.
10. Confirm ambiguous external effects remain quarantined rather than retried blindly.
11. Compare every manifest count and sample domain object with pre-backup evidence.
12. Measure observed RPO and RTO and attach logs to the release evidence directory.

Do not call the phase complete until the drill includes the actual deployment filesystem,
service account, backup destination, and expected database size.

## Activation after restore

Restoring bytes is not the same as safely resuming work. Before switching the configured
database path:

1. Keep all API and worker processes stopped.
2. Confirm no process still holds the old database or WAL files.
3. Verify the restored database with the release binary.
4. Compare schema versions with the binary's supported registry.
5. Rebuild projections into a disposable namespace and compare heads/offsets.
6. Run artifact scope and digest verification.
7. Recover expired attempts; never accept a completion from a pre-restore lease token.
8. Review DLQ and effect-unknown/ambiguity queues.
9. Start one service instance in read-only/readiness mode.
10. Run smoke reads for sessions, tasks, artifacts, approvals, and audit records.
11. Enable writers, then workers, then explicitly approved connectors.
12. Monitor queue age, errors, WAL growth, and integrity alerts through the recovery
    observation window.

The configuration switch itself is an operator-controlled deployment action. The library
does not rename or replace the live database.

## Failure handling

| Failure | Required response |
|---|---|
| Target already exists | Choose a new destination; never delete automatically |
| Snapshot integrity failure | Quarantine snapshot and investigate live DB health |
| Digest/count/schema mismatch | Treat backup as invalid; do not restore |
| Disk full during creation | Remove only `.partial` files created by this attempt after verifying ownership |
| Manifest publication race | Implementation removes its newly published DB link and leaves the competing path |
| Source changes during restore | Restore aborts and removes its new destination |
| Post-publication verification fails | Restore removes only its newly created destination |
| Older binary rejects schema | Use the matching binary or tested rollback/backup; do not edit the migration ledger |

Never run a destructive down migration on the only backup copy.

## Security and custody

- The database may contain prompts, event payloads, artifact bytes, identities, audit
  details, and connector metadata. Treat both files as confidential.
- `0600` is a local default, not encryption. Store the backup on encrypted media.
- Do not include API keys in backup names, manifests, logs, or tickets.
- Transfer database and manifest together over authenticated transport.
- Apply least privilege to create, read, restore, retain, and delete operations.
- Record every restore and production activation in the immutable audit process.
- Use retention/legal-hold policy before deleting any backup.

## Current limitations and next gates

- no scheduled backup job yet;
- no signed or MAC-authenticated manifest;
- no encryption key metadata or restore-time KMS check;
- no remote object storage, retention, legal hold, or automatic expiry;
- no incremental/PITR support;
- no rate limiting or cancellation for very large backups;
- no measured production RPO/RTO or large-database benchmark;
- core table inventory must be extended as projections and action receipts land;
- no automatic read-only corruption quarantine in the service lifecycle yet.

These are explicit blockers for later commercial release stages. The current code is a
safe, testable Phase 1 primitive, not a complete disaster-recovery product.

## Verification evidence

```bash
PYTHONPATH=src python3 -m unittest tests.test_backup -v
ruff check src/quantum_entanglement/backup.py tests/test_backup.py
mypy --strict --python-version 3.9 --follow-imports=skip \
  src/quantum_entanglement/backup.py
python3 -m compileall -q src tests
git diff --check
```

The committed tests cover live WAL-backed data, manifest/schema/count verification,
permissions, no-overwrite behavior, database and manifest tampering, symlink/path guards,
publication races, restore reopening through attempt/artifact stores, and cleanup of only
newly created paths.
