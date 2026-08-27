# Local release evidence generator

`scripts/generate_release_evidence.py` executes the repository's fixed local baseline and
writes one machine-readable record to standard output. Its purpose is to bind observed gate
results to a specific Git source identity and runtime without retaining command output or
host secrets.

This record is necessary local evidence. It is not a release attestation, signature,
provenance statement, SBOM, vulnerability scan, clean-host result, phase-release decision,
or proof of any production drill that the generator did not run.

## Run it

Run from any directory; the script resolves the repository from its own location:

```bash
python3 scripts/generate_release_evidence.py
```

Standard output contains only a single canonical JSON document. To retain it, redirect it to
a **new path outside the checkout** and preserve that file as an immutable CI or release
artifact. Shell redirection creates its target before the process starts, so redirecting to
an untracked path inside the checkout correctly makes the recorded source dirty.

```bash
python3 scripts/generate_release_evidence.py \
  > /tmp/quantum-entanglement-release-evidence.json
```

The per-gate timeout defaults to 600 seconds and can be reduced explicitly:

```bash
python3 scripts/generate_release_evidence.py --timeout-seconds 120
```

The process exits:

- `0` only when the JSON says `summary.releasable: true`;
- `1` after emitting valid evidence when source identity, cleanliness, or a gate does not
  satisfy the local release predicate;
- `2` for invalid command-line usage, such as a non-positive timeout.

An interruption or process-level failure may produce no complete JSON. Never treat a
partial or missing record as a pass.

## Verify before consuming

Never promote from an unparsed `releasable` field. The strict consumer reopens the retained
file and binds it to the current checkout:

```bash
python3 scripts/verify_release_evidence.py \
  /tmp/quantum-entanglement-release-evidence.json \
  --repository-root . \
  --expected-commit <full-commit-sha>
```

The verifier exits `0` and prints `release evidence verified` only after all checks pass. It
exits `1` with a fixed, non-sensitive failure code for invalid evidence or repository state,
and `2` for invalid CLI usage. It never prints evidence content, filenames from Git status,
environment values, parser exceptions, or the rejected path.

The verifier enforces:

- a regular, non-symlink file no larger than 1 MiB;
- a stable inode, size, modification time, and change time throughout the bounded read;
- strict UTF-8 JSON with no duplicate keys or non-finite constants;
- byte-for-byte canonical JSON v1 and no unknown or missing schema fields;
- canonical UTC time, runtime value types, SHA formats, and exact before/after source SHA;
- the exact ordered baseline gate names, argv, source-import mode, zero exit codes, passed
  statuses, non-negative durations, counts, and empty reason list;
- clean, stable source identity both while generating and while verifying the evidence;
- the optional externally supplied commit SHA, used by CI to bind `github.sha`.

The evidence file must live outside the repository being checked. Adding or redirecting it
inside the checkout makes the repository dirty and the verifier correctly rejects it.

## What runs

The command list is fixed in code; the CLI does not accept arbitrary commands and does not
invoke a shell:

1. `python3 -m pytest -q`
2. `python3 examples/group_chat_demo.py --compact`
3. `python3 -m compileall -q src tests scripts`
4. `ruff check src tests scripts`
5. `git diff --check`

The first three Python commands execute with the interpreter that launched the generator.
Commands that import the project receive the checkout's `src` directory through the runner;
the evidence records this fact as `repositorySourceImport` rather than persisting an
absolute path or environment value. The recorded Python executable is reduced to its
basename, while every argument is derived directly from the argv that ran.

Each gate has an independent timeout. A failure does not short-circuit later gates, so the
record describes every attempted baseline gate. The generator performs no network request,
upload, Git fetch, Git push, package installation, release creation, or messaging action.

## Source binding and fail-closed decision

The generator captures Git state before and after all gates:

- full `HEAD` commit SHA;
- that commit's tree SHA;
- staged, unstaged, untracked, and submodule dirty state;
- whether commit and tree identity remained stable throughout the run.

Ignored files follow Git's normal ignore rules. A content change that is made and then
perfectly reverted between the two snapshots is outside this local observer's proof; use an
isolated, immutable CI checkout for promotion evidence.

`summary.releasable` is `true` only when all of the following are proven:

1. commit and tree identity are available before and after the gates;
2. both identities are unchanged;
3. the checkout is clean before and after the gates;
4. at least one gate ran;
5. every gate exited successfully.

A dirty source is never made releasable by passing tests. A gate that modifies the checkout
or commits a new `HEAD` also fails the predicate. Missing Git evidence, an unavailable tool,
a timeout, and a non-zero exit all fail closed. Stable `reasonCodes` explain which predicate
was not proven.

## Canonical JSON v1

The top-level contract is:

| Field | Meaning |
|---|---|
| `format` | Always `quantum-entanglement.release-evidence` |
| `schemaVersion` | Integer schema version, currently `1` |
| `generatedAt` | UTC completion timestamp with six fractional digits |
| `source` | Before/after commit, tree, dirty, and identity-stability observations |
| `runtime` | Python implementation/version, SQLite version, OS release, and architecture |
| `gates` | Ordered argv, duration, exit code, fixed failure kind, and result per gate |
| `summary` | Counts, source predicate, reason codes, and final `releasable` boolean |

Canonical JSON v1 means UTF-8 JSON with lexicographically sorted object keys, no
insignificant whitespace, finite JSON values only, and exactly one trailing newline. This
project format is designed for stable hashing; it is not a claim of RFC 8785 compliance and
is not cryptographically signed.

Durations are integer milliseconds measured by a monotonic clock. They are execution
observations, not performance-gate evidence. `exitCode` is `null` for timeout or execution
error, and `failureKind` uses only the fixed vocabulary `timeout`, `execution_error`,
`nonzero_exit`, or `null` on success.

## Redaction boundary

The retained JSON deliberately excludes:

- stdout and stderr from gates;
- exception messages and tool-generated paths;
- repository path, hostname, username, and branch name;
- environment variable names and values;
- Git status filenames;
- credentials, cookies, headers, chat content, or customer data.

Child processes receive only a small operational environment allowlist; the evidence never
serializes its values. If a gate fails, rerun the recorded argv interactively in an
appropriate secured environment to diagnose it. Do not paste raw logs into release evidence
without applying the repository's redaction and retention policy.

## Promotion and retention

Validate the JSON schema version before consuming it. Hash and retain the exact JSON as a
sidecar artifact, then reference its immutable digest from the phase record under
`docs/production/evidence/`. Adding the generated file to Git creates a new tree and cannot
retroactively prove that new tree; rerun gates for every changed source artifact.

Promotion still requires every applicable item in `RELEASE_GATES.md` and the completed human
record in `docs/production/evidence/README.md`, including clean-host/package evidence,
migration and restore rehearsals, security/fault/performance results, unresolved-issue
inventory, reviewer identity, and an explicit promotion decision.

## CI retention behavior

The `Canonical release evidence` job in `.github/workflows/ci.yml` performs this workflow in
a dedicated Python 3.12 checkout:

1. checks out source without persisting Git credentials;
2. installs the declared development toolchain;
3. generates JSON under `runner.temp`, outside the checkout;
4. verifies it against the clean checkout and `${{ github.sha }}`;
5. uploads the file for 14 days with a run/attempt-specific artifact name.

The upload step uses `always()`. This intentionally retains rejected JSON, an empty file, or
a partial file when an earlier step fails, so investigators can distinguish a failed gate
from a generator crash. Artifact existence is therefore **not** proof of success. Only a
green generator step, green verifier step, and verifier exit `0` establish valid local
baseline evidence.

The job does not push, publish, sign, attest, or promote anything. Its package installation
uses the current dependency declarations rather than a complete locked, verified supply
chain; GA still requires pinned toolchains, SBOM, vulnerability/license policy, provenance,
signing, and clean-host reproduction under the broader release gates.
