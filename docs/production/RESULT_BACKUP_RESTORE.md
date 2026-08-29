# Migration-7 result backup and restore

Status: **implemented as an explicit, result-specific local backup contract.** The legacy
`create_sqlite_backup` / `verify_sqlite_backup` / `restore_sqlite_backup` APIs remain unchanged and
continue to reject databases newer than their supported migration registry. The APIs in this
document are the only current path for an active migration-7 rehearsal.

The implementation is local-first. Markdown and Git/GitHub are the source of truth during the
current development batch; Notion synchronization is intentionally deferred until the batch is
closed and then requires a page-by-page readback. No Feishu, WeCom, Yuque, webhook, IM or external
connector is contacted.

## API and format

```python
from quantum_entanglement import SQLiteEventStore
from quantum_entanglement.result_backup import (
    create_result_backup,
    recover_result_backup_publication,
    restore_result_backup,
    verify_result_backup,
)

store = SQLiteEventStore("event-store.sqlite3", enable_result_acceptance_schema=True)
store.close()

manifest = create_result_backup(
    "event-store.sqlite3",
    "backup/event-store.sqlite3",
)
verify_result_backup("backup/event-store.sqlite3")
restore_result_backup(
    "backup/event-store.sqlite3",
    "restored/event-store.sqlite3",
)
```

The manifest format is `qe.result-backup/1`. It binds:

- backup ID, canonical UTC creation timestamp and SHA-256 of the exact backup bytes;
- page count, page size and byte geometry;
- active migration-7 state digest and domain-registry digest;
- the exact present profile set, every schema object/DDL digest and every table row count;
- a canonical topology digest over all of the above.

`ResultBackupManifest.to_json_bytes()` emits one canonical UTF-8 JSON representation. Parsing
rejects duplicate keys, non-finite/decimal values, unknown or missing fields, non-canonical JSON,
wrong digests, unsupported migration versions and any topology object not in the trusted
registry.

## Create contract

`create_result_backup(source, target)` requires a regular, non-symbolic source file and an absent
target/manifest. It opens the source read-only, derives active migration-7 topology evidence, and
uses SQLite's online backup API to copy a consistent database into a same-directory temporary
file. The temporary copy is independently opened read-only and checked for:

1. SQLite integrity and foreign-key success;
2. exact migration-7 ledger, sidecar metadata and dependency state;
3. complete catalog classification against the 12-profile trusted topology registry;
4. identical topology evidence to the source;
5. canonical page geometry and byte SHA-256.

The manifest is written and fsynced to a private temporary file. Manifest and database are then
published with no-overwrite hard-link creation; an error removes any target published by this
call. Existing files are never replaced. The returned manifest is only returned after a final
read-only verification of the published backup. A dual-connection writer may continue to hold an
uncommitted transaction: the online backup captures the last committed SQLite snapshot, never
uncommitted rows.

## Verify and restore contract

`verify_result_backup` reads the canonical manifest, hashes the backup bytes, checks page geometry,
and derives topology again from a read-only SQLite connection. It returns the exact parsed manifest
only when every comparison succeeds.

`restore_result_backup` first verifies the source backup, requires an absent destination, copies
bytes to a same-directory fsynced temporary file, publishes without overwrite, and runs the same
byte/geometry/topology verification against the restored file before returning. The restored file
is independent of the backup inode; the temporary hard link is removed after publication.

Neither operation runs migrations, claims leases, reconciles invocations, starts workers, emits
events, publishes outbox messages or opens external network connections. A restored database is
still a recovery input: an operator must reopen it with
`SQLiteEventStore(enable_result_acceptance_schema=True)`, inspect the result graph, and run the
receipt-bound non-emitting reconciliation workflow before any future worker is considered.

## Crash publication recovery

Publication consists of two independent no-overwrite links (manifest and database), so a process
kill can leave one target, both targets, or private temporary files. A target pair is never
assumed complete from its filenames. After all backup/restore writers are stopped, run:

```python
from quantum_entanglement.result_backup import recover_result_backup_publication

recovery = recover_result_backup_publication(
    "backup",
    backup_path="backup/event-store.sqlite3",
    manifest_path="backup/event-store.sqlite3.manifest.json",
)
print(recovery.state.value)
```

The scan removes only regular files with the module-owned `.qe-result-backup-*`,
`.qe-result-manifest-*` and `.qe-result-restore-*` prefixes, and compares device/inode/size again
before each unlink. Symlinks, directories and entries replaced during the scan are preserved.
Published targets are never deleted or overwritten. With both target paths supplied, `complete`
means a fresh `verify_result_backup` succeeded; `incomplete` means only one target exists;
`unverified` means both exist but verification failed and they require forensic/operator review;
`absent` means neither exists. The function is deliberately an operator recovery primitive and
must not run concurrently with a publisher.

## Topology evidence

`derive_result_backup_topology(connection)` is a read-only, no-DML primitive. It requires an exact
`sqlite3.Connection` with no caller transaction, owns a bounded `BEGIN`/`ROLLBACK` snapshot, and
rejects:

- a legacy or partially activated migration-7 database;
- missing or extra catalog objects, DDL drift, partial profiles and missing dependencies;
- unsupported SQLite statistics objects (canonical `sqlite_stat1` created by `ANALYZE` is ignored);
- integrity/foreign-key failures and malformed table counts;
- an active caller transaction or a connection subclass.

The result profile is `qe.domain-migration-0007/1`; its 45 table/index objects are combined with
the trusted legacy profiles. Optional projection and revocation profiles are included only when
they are actually present, while migration-7 and all of its dependencies must be present.

## Failure handling and recovery

| Failure | Behavior |
| --- | --- |
| source/manifest/target is a symlink or non-regular file | fail closed; no target write |
| legacy, partial or drifted schema | `ResultBackupIntegrityError`; no target write |
| target already exists | `ResultBackupExistsError`; existing bytes unchanged |
| copy, fsync, parse or post-publish verification error | clean up this call's temporary/published files; never overwrite an existing file |
| manifest or database bytes changed after creation | verification fails on digest or topology mismatch |
| restored topology differs from the manifest | restore fails and removes the newly published destination |

The backup manifest is not a write capability and does not certify external effects. Keep the
database, manifest and the returned topology digest together as one evidence unit. Before a real
deployment, copy them to independent storage, verify on a clean host, record RPO/RTO and test
restore with workers/connectors stopped.

## Evidence tests

`tests/test_result_backup_topology.py` covers exact active catalog derivation, statistics handling,
legacy gating, caller-transaction rejection, unknown/partial catalog drift and no-DML behavior.
`tests/test_result_backup.py` covers non-empty migration-7 create/verify/restore, canonical
manifest round-trip and tamper rejection, no-overwrite behavior, legacy-source refusal,
clean-process restore/reconciliation, dual-connection committed-snapshot consistency, and real
`SIGKILL` at both publication links followed by guarded temporary cleanup.

```bash
PYTHONPATH=src pytest -q \
  tests/test_result_backup_topology.py \
  tests/test_result_backup.py
python -m ruff check \
  src/quantum_entanglement/result_backup_topology.py \
  src/quantum_entanglement/result_backup.py \
  tests/test_result_backup_topology.py \
  tests/test_result_backup.py
PYTHONPATH=src python -m mypy \
  src/quantum_entanglement/result_backup_topology.py \
  src/quantum_entanglement/result_backup.py
git diff --check
```

These tests are deterministic local SQLite and POSIX-process evidence. They do not prove cross-host
object-store retention, encryption/key rotation, capacity, SLO/RPO/RTO, production tenant
isolation, or permission to connect to a real IM. Those remain release gates.

## Next gates

1. Add fleet compatibility/rollback policy and independent-host evidence retention.
2. Complete heartbeat/fencing, business projection and `AcceptedV2` gates; keep publication and
   real IM outbound disabled until separately authorized.
