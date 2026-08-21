# Inactive backup v2 combination evidence

## Decision

This checkpoint is a **local integration candidate only**. It does not open Gate C, does
not activate backup manifest v2, and does not authorize production use. The active package
root, v1 backup implementation, and admin CLI remain unable to reach the v2 codec or
snapshot derivation.

The independently reviewed backup-v2 candidate was replayed onto the current canonical
foundation without changing either source branch, then repaired after a combination
reviewer found a transaction-lifecycle window. The repaired local subject was:

- branch: `codex/backup-v2-on-canonical-v1`;
- subject commit: `f799efae8a540e3b20faebfbb65c4ca22976ae84`;
- subject tree: `37f9f0d75673015e63e1b3ca9b5705a138851513`;
- canonical parent: `967b4364c36e84c2c54c51528ab717da615222ac`;
- source candidate: `3946847b1eceec85beac5b8f1e2031b870cedaa5`;
- worktree state during the recorded gates: clean.

This document is a later evidence-only commit and therefore is not included in the subject
tree above. The final combined HEAD must be rechecked after this document is committed and
must receive a fresh independent read-only review before integration. Passing the original
backup-v2 review does not waive review of the lifecycle repair.

## Replay map

Sixteen source commits were replayed in their original order. Two source commits were
intentionally omitted because the canonical branch already contains more complete,
independently verified equivalents:

- omitted `1fe01516d7c467c38eff2a352ef452ad6e9b490a`; canonical
  `3f856e9e5d2fed8ab4e5e1601c416e91aaeff7dd` already closes the backup fixture connection
  with explicit commit, rollback, and close ownership;
- omitted `427618ad187802788b649d0fee38b97f19a43a33`; canonical
  `dd846f876ae02385b9049df6ca03a5fee2c4ea3e` already anchors the local `tests` package.

The retained source commits were:

```text
e0b5495788ec81c76c70ecf89e63b3ad660a3468
c4f49844cfde9865f7741a5ccd65dd8d3be382d6
ddd1dd74f5cd9850c9321ddb979f854269fbd222
5a1e94df49ee673e696a8717f41b189a48ea27a8
bc95912a7407a023abab122e0c91e3aa94c8ca71
c40ef86964ede66dd2aa270a1fab06bca631ea62
f8556f467ae82084a41393cd5e765a0d1e097067
8fc08f3dac15d53fc07e70bfbfb41775aa9e4142
0d39deb7cea9b895f9329c910e7a11fbab5e06bc
c6b02d3665a69dbe509e6d9ab21974167f4226f7
40d5f625c6856b318cfba4d30e721a04d99341ea
a97a30f0616c43327a235ab50bf25fc6ddafd934
801cac7bab2dfe7ff118050c84bb7bbf1c47a30c
5da0b1810d16d47a12c6374bd7d8193e1fb38bc1
052ac9756aeb77d4fb1c662046da34bbfe23da5a
3946847b1eceec85beac5b8f1e2031b870cedaa5
```

Only `CHANGELOG.md` required manual conflict resolution. The resolution retained all
canonical invocation-store, process-binding, and test-isolation entries and added all
backup topology, codec, snapshot, cold-import, and remaining-work entries. No whole-file
`ours` or `theirs` resolution was used.

At the recorded subject, the branch differed from the canonical parent in exactly twelve
paths: `CHANGELOG.md`, five backup documents, three new source modules,
and three new test modules. The package root, `tests/__init__.py`, and
`tests/test_backup.py` retained their canonical blobs.

## Transaction-lifecycle repair

The combination reviewer used a real exact `sqlite3.Connection` and one-shot
`sys.settrace` controls to expose two P1 windows in the replayed snapshot derivation:

- SQLite had completed `BEGIN`, but the Python ownership boolean had not yet been updated;
- the derivation body had completed, but Python had not entered the separate cleanup block.

Both cases propagated the same pre-created `KeyboardInterrupt` object while leaving
`connection.in_transaction == true`. The repair did not encode the reviewer's historical
line numbers. Its tests dynamically arm on observed `BEGIN`, body completion, real
`ROLLBACK`, and exact connection transaction state.

The repair was split into these commits:

```text
431e51a3a555e4daa1652e9a2ddbaf45620b0d2f test: expose backup snapshot lifecycle windows
e6ccca8dc5be672b0cb9f17ea2b213d385ab788d fix: structure backup snapshot cleanup
955bab894f9aa6cd27036a49f935d4c66abf3560 test: reject ambient snapshot controls
6f9c9496e6e877b57ee3aab5574e4f771c55b573 fix: authenticate snapshot control origins
846865ac3fbd52f7e26e07198f0422ceb6e19fab test: prove snapshot begin failures stay unowned
f799efae8a540e3b20faebfbb65c4ca22976ae84 docs: define snapshot cleanup guarantee
```

The first test commit failed against the original implementation in 19 subtests on both
Python 3.12 and 3.13: every exact control left the post-`BEGIN` or post-body transaction
open, transient cleanup was attempted only once, `CancelledError` origin precedence was
missing, and a post-rollback control acquired a synthetic second public-frame traceback.
The ambient-control regression then failed four subtests against the first repair because
thread-local handled-exception state was incorrectly accepted as lifecycle provenance.

The final implementation installs two structured cleanup frames before `BEGIN`, derives
ownership conservatively from exact live transaction state, retries after one interrupted
or failed cleanup, and captures origin only at its own lifecycle `except` boundaries. Exact
`KeyboardInterrupt`, `SystemExit`, `GeneratorExit`, and `CancelledError` use the same object
and bare traceback-preserving re-raise. Denied/non-opening `BEGIN`, pre-existing caller
transactions, and closed connections remain fail-closed.

This is a bounded Python guarantee, not an atomic signal mask. One injected asynchronous
control, or one transient cleanup fault followed by a successful fallback, is covered.
Repeated asynchronous controls or persistent state-inspection/rollback failure can exhaust
both attempts; no result is returned and the future descriptor/process owner must
quarantine and close that connection. This inactive caller-connection helper still cannot
perform that quarantine itself.

## Exact content checks

The following unchanged subject blobs matched the independently accepted backup candidate
exactly:

| Path | Git blob |
| --- | --- |
| `docs/architecture/SQLITE_BACKUP_MANIFEST_V2_CODEC.md` | `2c86ddfa8bf519fab5cfafac54b6981658ae00bd` |
| `docs/architecture/SQLITE_BACKUP_TOPOLOGY_REGISTRY.md` | `70755780fe8366a6da83c4963e39234b76e1f7cc` |
| `docs/production/SQLITE_BACKUP_RESTORE.md` | `a9a469bdf05de3bad7c7ecb13e71dc30ff72e655` |
| `src/quantum_entanglement/backup_manifest_v2.py` | `cf0dc9532caa8ef1fb28678c97882b9a01cc17f4` |
| `src/quantum_entanglement/backup_topology.py` | `7bf3465d752841813002c71152a9df88993819f3` |
| `tests/test_backup_manifest_v2.py` | `5b4b8c15f36e375107548581321bc34ecdee3623` |
| `tests/test_backup_topology.py` | `2499925f75d9c306d32adfa53e1dbf2b67b15a67` |

The repaired subject fixed and documented these three blobs, which therefore require the
fresh independent review called out above:

| Path | Git blob |
| --- | --- |
| `docs/architecture/SQLITE_BACKUP_V2_SNAPSHOT_DERIVATION.md` | `ea4fb99e27771a729882276ea8e70f7acb0051e8` |
| `src/quantum_entanglement/backup_snapshot_v2.py` | `a91ac3e3cd4d759fc8835d2c960e79010296b873` |
| `tests/test_backup_snapshot_v2.py` | `0ac5c28370f3324f1e9c4a9fedeaddb7a07da268` |

The following subject blobs matched canonical exactly:

| Path | Git blob |
| --- | --- |
| `src/quantum_entanglement/__init__.py` | `54b59841efa8242679e2fae3439863d1632e54ea` |
| `tests/__init__.py` | `3cfb49e62d96c2b682b540cacd653859ba12c6af` |
| `tests/test_backup.py` | `90aab29de71bfda4befd2ac9b4c6a1f79b33c4ae` |

Static reachability scans found zero v2 format, type, or module references in each of the
package root, active `backup.py`, and `admin_cli.py`. A cold package-root import audit found
zero packaged `.up.sql` opens. An explicit versioned-submodule import is intentionally
allowed to initialize the trusted migration registry and read those resources.

## Executed gates

All test runs used repository source through `PYTHONPATH=src`. The focused and full suites
also used warnings-as-errors.

| Gate | Python 3.9.6 | Python 3.12.12 | Python 3.13.9 |
| --- | ---: | ---: | ---: |
| backup topology + manifest-v2 + snapshot-v2 | 66/66 | 66/66 | 66/66 |
| full unittest discovery | 971/971, 1 expected skip | 971/971 | 971/971 |
| compileall: `src`, `tests`, `scripts`, `examples` | pass | pass | pass |
| compact deterministic group-chat demo | pass | pass | pass |

The historical `897/897` values in the source-candidate architecture documents describe
that earlier branch. They are not the repaired combined count; the measured count is
971/971 on each interpreter above.

The following original adversarial regressions passed explicitly on Python 3.13.9:

1. NBSP and vertical-tab DDL mutations cannot collide with SQLite token whitespace.
2. Line-comment newline removal and block-comment content changes cannot collide.
3. A cold package-root import performs zero migration-SQL opens.
4. The active v1 module has no v2 reachability.
5. Unknown, partial, malformed, or drifted catalog DDL fails closed.
6. Public failures detach active exception context.
7. An originating exact control takes precedence over a cleanup control.
8. A concurrent WAL commit cannot mix table counts into one derived snapshot.

Six repaired lifecycle regressions also passed explicitly on Python 3.13.9:

1. every exact control after effective `BEGIN` rolls back and preserves connection reuse;
2. every exact control after body success or failure rolls back;
3. one transient cleanup error/control is retried without hiding an exact origin;
4. an ambient handled control cannot authenticate cleanup priority;
5. a control after real rollback preserves identity/traceback and WAL reader reuse;
6. denied or non-opening `BEGIN` remains transaction-free and reusable.

The original eight plus these six selectors passed together as 14/14.

One initial manual selector used a nonexistent unittest class name for two of these eight
tests. That command failed at test loading. The corrected fully qualified selector passed
all eight, and the three-version focused and full discovery runs were unaffected.

The process-related combination checks passed on Python 3.13.9:

- process identity, including fork and spawn-style coverage: 17/17;
- durable invocation attempts, including inherited-store coverage: 117/117;
- artifact store, including spawn coverage: 27/27.

The static and repository gates passed:

- dependency locks: 4 targets and 74 package records;
- Ruff 0.16.3 lint: pass;
- Ruff 0.16.3 format: 98 files already formatted;
- mypy 1.19.1 strict: 38 source files, no issues;
- `git diff --check`: pass;
- candidate-diff, commit-message, and newly reachable blob credential scans: zero matches;
- ancestry, exact path set, exact blob set, and clean-worktree checks: pass.

An evidence file generated outside the checkout at clean subject
`f799efae8a540e3b20faebfbb65c4ca22976ae84`, tree
`37f9f0d75673015e63e1b3ca9b5705a138851513`, passed all five fixed local gates and strict
verification against that exact commit:

- unit tests;
- compact deterministic demo;
- compileall;
- Ruff;
- Git diff check.

The evidence JSON reported `releasable=true` only for that fixed local predicate. It is not
a production release approval and cannot waive clean-host, distribution, security,
recovery, performance, operational, or human-approval gates.

## Remaining boundary

This checkpoint supplies inactive compatibility infrastructure only: an exact topology
registry, a strict manifest-v2 codec, trusted evidence-model construction, and read-only
snapshot derivation. It does not supply an active v2 writer, atomic publication protocol,
quarantine verifier, exact-byte restore path, operator CLI, production drills, or approval.

Gate C therefore remains closed. Gates A through E remain closed overall, and the project
remains **NO-GO** for production promotion.
