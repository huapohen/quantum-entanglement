# Dependency locks and source-bound SBOM gate

Status: implemented build/test/release toolchain lock and SBOM slice; **not a complete
trusted-supply-chain or GA claim**.

This document defines the Python dependency-lock contract, the two CycloneDX documents
produced for a package candidate, the CI ordering that makes them admissible release
evidence, and the controls that remain open. It must be read together with
[`REPRODUCIBLE_BUILDS.md`](./REPRODUCIBLE_BUILDS.md),
[`DISTRIBUTION_INTEGRITY.md`](./DISTRIBUTION_INTEGRITY.md), and
[`RELEASE_GATES.md`](./RELEASE_GATES.md).

## Exact claim boundary

The implemented gate proves all of the following for one exact source commit:

- the repository contains exactly four expected Python lock targets;
- every declared tool root and transitive package is pinned to one version and at least one
  SHA-256 distribution digest;
- the lock inputs and lock files have the exact digests recorded by the canonical lock
  policy;
- `pyproject.toml` build and development declarations agree with the lock roots;
- CI installs tools with pip hash checking, accepts wheels only, and disables dependency
  resolution when installing this project;
- the retained wheel and sdist are bound to a runtime SBOM and a build-toolchain SBOM;
- both SBOMs are regenerated from the verified source/distribution/lock evidence, compared
  byte for byte, structurally checked by the repository verifier, and validated against the
  official CycloneDX 1.6 JSON schema before upload.

It does **not** prove that the package index, runner image, Python distribution, resolver
binary, or build host is trusted. It does not perform vulnerability, malware, or license
policy evaluation. It does not sign the packages, manifest, SBOMs, or provenance. Those
are separate release blockers described below.

## Lock inventory and support matrix

`requirements/lock-policy.json` is the canonical inventory. The current schema has exactly
these targets, in this order:

| Scope | Python | Platform | Direct input | Locked closure | CI consumer |
|---|---|---|---|---|---|
| build | 3.12 | `x86_64-unknown-linux-gnu` | `requirements/build.in` | `requirements/build-py312.lock` | package construction |
| dev | 3.9 | `x86_64-unknown-linux-gnu` | `requirements/dev.in` | `requirements/dev-py39.lock` | minimum-version tests |
| dev | 3.12 | `x86_64-unknown-linux-gnu` | `requirements/dev.in` | `requirements/dev-py312.lock` | primary tests |
| release | 3.12 | `x86_64-unknown-linux-gnu` | `requirements/release.in` | `requirements/release-py312.lock` | release evidence and SBOM validation |

The direct roots are deliberately split by duty:

- build: `build`, `pip`, and the exact `setuptools` backend;
- development: `mypy`, `pip`, `pytest`, `pytest-asyncio`, `ruff`, and `setuptools`;
- release: the build roots plus `cyclonedx-bom` and `ruff`.

The project currently has no mandatory runtime dependency. The optional `langgraph` extra
is intentionally outside this base-installation lock and SBOM. A deployment that enables
that extra must not claim that the base runtime SBOM covers it.

The policy also binds:

- schema format `quantum-entanglement.dependency-locks`, version `1`;
- resolver identity `uv==0.9.27`;
- resolution cutoff `2026-08-20T00:00:00Z`;
- input path and SHA-256 for each target;
- lock path and SHA-256 for each target.

The cutoff prevents a normal regeneration from selecting a distribution uploaded after
that instant. It is not an index snapshot: deletion, yanking, metadata replacement, index
compromise, or different index selection remains outside the guarantee.

## Strict lock-verifier contract

Run the verifier before any installation:

```bash
python scripts/verify_dependency_locks.py --repository-root .
```

`scripts/verify_dependency_locks.py` fails closed unless all of these conditions hold:

1. The repository root, policy, inputs, and locks are stable regular files/directories and
   not symlinks.
2. The bounded policy is canonical ASCII JSON with no duplicate, missing, or unknown key.
3. The policy describes exactly the four supported targets above, with the expected
   resolver version and resolution cutoff.
4. Every recorded input and lock digest matches the bytes on disk.
5. Every non-empty input line is one exact `name==version` pin; root names are unique and
   sorted.
6. Every lock begins with `--only-binary :all:` and contains only exact package/version
   records followed by sorted, unique SHA-256 hashes.
7. Every direct root exists at the same version in the resolved closure.
8. `pyproject.toml` uses exact build-backend pins, and its development extra matches the
   development roots after the explicit `pip`/`setuptools` bootstrap roots are accounted
   for.
9. Build roots are a subset of release roots; all scopes agree on the pip version; build
   and development scopes agree on the backend version; release includes the SBOM and
   evidence lint tools.

Successful output is compact JSON containing the verified target and package-record counts.
Failure exits `1` with a fixed code suitable for logs. Syntax misuse exits `2`. A failure
must never be bypassed with `continue-on-error`, `|| true`, an unhashed install, or a
temporary policy edit.

## Installation contract

CI installs a selected closure using both pip enforcement flags, even though each lock also
contains the binary-only directive:

```bash
python -m pip install \
  --disable-pip-version-check \
  --require-hashes \
  --only-binary :all: \
  -r requirements/dev-py312.lock
```

Substitute only another target from the table. Then install this project without creating
an implicit PEP 517 environment and without resolving project dependencies:

```bash
python -m pip install \
  --disable-pip-version-check \
  --no-build-isolation \
  --no-deps \
  .
python -m pip check
```

The package workflow similarly runs both builds with `python -m build --no-isolation`.
This is essential: installing a lock and then allowing an isolated build environment to
download its own backend would invalidate the lock claim. A pip cache may accelerate the
operation, but cached files remain subject to `--require-hashes`.

## Reproducing the current locks

Use the exact resolver version recorded in the policy. The following commands were
re-executed on 2026-08-20 and reproduced all four committed lock files byte for byte:

```bash
uv pip compile requirements/build.in \
  --output-file requirements/build-py312.lock \
  --python-version 3.12 \
  --python-platform x86_64-unknown-linux-gnu \
  --generate-hashes \
  --only-binary :all: \
  --emit-build-options \
  --exclude-newer 2026-08-20T00:00:00Z \
  --custom-compile-command 'uv 0.9.27: build lock for CPython 3.12 / x86_64 Linux' \
  --no-sources

uv pip compile requirements/dev.in \
  --output-file requirements/dev-py39.lock \
  --python-version 3.9 \
  --python-platform x86_64-unknown-linux-gnu \
  --generate-hashes \
  --only-binary :all: \
  --emit-build-options \
  --exclude-newer 2026-08-20T00:00:00Z \
  --custom-compile-command 'uv 0.9.27: dev lock for CPython 3.9 / x86_64 Linux' \
  --no-sources

uv pip compile requirements/dev.in \
  --output-file requirements/dev-py312.lock \
  --python-version 3.12 \
  --python-platform x86_64-unknown-linux-gnu \
  --generate-hashes \
  --only-binary :all: \
  --emit-build-options \
  --exclude-newer 2026-08-20T00:00:00Z \
  --custom-compile-command 'uv 0.9.27: dev lock for CPython 3.12 / x86_64 Linux' \
  --no-sources

uv pip compile requirements/release.in \
  --output-file requirements/release-py312.lock \
  --python-version 3.12 \
  --python-platform x86_64-unknown-linux-gnu \
  --generate-hashes \
  --only-binary :all: \
  --emit-build-options \
  --exclude-newer 2026-08-20T00:00:00Z \
  --custom-compile-command 'uv 0.9.27: release lock for CPython 3.12 / x86_64 Linux' \
  --no-sources
```

After regeneration, update only the corresponding input/lock digests in the canonical
policy, then run the strict verifier. Changing the resolver version, cutoff, Python,
platform, index, direct roots, or lock inventory is a policy change and requires explicit
review; it is not routine lock refresh.

### Dependency-update review procedure

One dependency update should be independently reviewable:

1. Change the minimum necessary direct pin or explicitly approve a transitive refresh.
2. Regenerate every affected target with the declared resolver, platform, Python, cutoff,
   and binary-only policy.
3. Review the closure diff, distribution hashes, upstream changelog, security advisories,
   license, Python compatibility, and package ownership signals.
4. Update the canonical policy digests and run the verifier.
5. Install every affected lock into a fresh environment with pip hash mode and run its
   assigned tests/build/evidence commands.
6. Rebuild twice, regenerate the distribution manifest and both SBOMs, validate them, and
   retain the new candidate evidence.
7. Keep the source pin, locks, policy digests, tests, and any risk acceptance in one
   dependency-update commit or an explicitly ordered commit series that never leaves CI
   silently unlocked.

Never fix a digest mismatch by editing only `lock-policy.json`. The expected response is to
explain why lock bytes changed, review the new closure, and regenerate evidence. If a
supported target has no acceptable wheel, do not relax `--only-binary`; either select a
reviewed version with a wheel or make a separate, documented source-build policy decision.

## SBOM document set

`scripts/sbom.py` generates exactly two deterministic CycloneDX 1.6 JSON files:

| File | Root component | Covered components | Explicit exclusion |
|---|---|---:|---|
| `quantum-entanglement-runtime.cdx.json` | published Python library | base runtime dependencies; currently 0 | optional extras, deployment image, interpreter |
| `quantum-entanglement-build.cdx.json` | build-toolchain application | unique packages aggregated across all four locks; currently 51 | runner OS/image and resolver binary |

Both documents bind the full source commit and tree SHA plus the filename, byte size, and
SHA-256 of the one verified wheel and one verified sdist. The build document additionally
binds each lock/input digest, every target label, every exact package version, and the
available artifact hashes recorded for that component. The runtime document fails closed
if mandatory project dependencies become non-empty before runtime-lock support is added.

The documents intentionally omit `timestamp` and `serialNumber`. Given identical verified
source, artifacts, locks, and generator code, their bytes are identical. This makes the
SBOM digest meaningful without pretending that generation time is a source property.

### Generate and verify

Build and verify the canonical distribution manifest first. Create an empty directory
outside the checkout, then bind all evidence to an independently supplied full commit SHA:

```bash
qe_expected_commit="$(git rev-parse HEAD)"
qe_manifest_path="$(mktemp "${TMPDIR:-/tmp}/qe-manifest.XXXXXX.json")"
qe_sbom_directory="$(mktemp -d "${TMPDIR:-/tmp}/qe-sbom.XXXXXX")"

python scripts/distribution_manifest.py generate \
  --repository-root . \
  --distribution-directory dist \
  > "$qe_manifest_path"

python scripts/distribution_manifest.py verify \
  "$qe_manifest_path" \
  --repository-root . \
  --distribution-directory dist \
  --expected-commit "$qe_expected_commit"

python scripts/sbom.py generate \
  --repository-root . \
  --distribution-directory dist \
  --distribution-manifest "$qe_manifest_path" \
  --sbom-directory "$qe_sbom_directory" \
  --expected-commit "$qe_expected_commit"

python scripts/sbom.py verify \
  --repository-root . \
  --distribution-directory dist \
  --distribution-manifest "$qe_manifest_path" \
  --sbom-directory "$qe_sbom_directory" \
  --expected-commit "$qe_expected_commit"
```

Generation requires the destination to exist, be empty, be a stable non-symlink directory,
and resolve outside the repository. It writes each file exclusively, flushes file and
directory state, and refuses an unexpected document set. Verification regenerates the
expected bytes from source evidence, strictly reads exactly those two regular files, and
requires byte equality.

### Internal validation boundary

Before output or acceptance, the repository validator rejects:

- oversized, empty, noncanonical, duplicate-key, non-finite, or malformed JSON;
- missing or unknown fields and wrong CycloneDX/spec/document versions;
- invalid root/tool/package components, duplicate components, unsafe strings, absolute
  local-path leakage, duplicate/unsorted properties, and excessive counts or lengths;
- dependency references outside the declared component set or a noncanonical graph;
- symlink, special-file, unstable-read, directory-identity, unexpected-file, overwrite, and
  in-repository output attacks;
- source, tree, artifact, manifest, input, lock, component-hash, or regenerated-byte drift.

CLI validation failures exit `1` with a fixed non-sensitive code. The tool does not print
filesystem paths on failure. Invalid command syntax exits `2`.

This custom strict profile is followed by an independent standards check using the locked
`cyclonedx-python-lib` validator:

```python
from cyclonedx.schema import SchemaVersion
from cyclonedx.validation.json import JsonStrictValidator

validator = JsonStrictValidator(SchemaVersion.V1_6)
assert validator.validate_str(sbom_text) is None
```

Both checks are required. Official schema validity alone would allow fields or graph shapes
outside this repository's deterministic evidence profile; repository validation alone
would not independently confirm CycloneDX conformance.

## Package CI ordering and retention

The package workflow enforces this fail-closed sequence:

1. verify the lock policy and all lock inputs;
2. install the hashed build lock;
3. build twice with `--no-isolation` from distinct checkouts at `${{ github.sha }}`;
4. canonicalize both sdists and require byte-identical wheel/sdist sets;
5. generate and verify the source-bound distribution manifest;
6. install the hashed release lock;
7. create the empty SBOM directory under `runner.temp`;
8. generate and strictly verify both source-bound SBOMs;
9. validate the exact two-file set against CycloneDX 1.6;
10. verify migration package data and smoke-install the wheel;
11. upload the distributions, manifest, and two exact SBOM paths for 14 days.

The upload step has neither `always()` nor a wildcard for the SBOM directory. A failed,
partial, extra-file, drifted, or schema-invalid SBOM set therefore cannot be uploaded by
that step as successful release evidence. Retention alone is still not promotion: phase
evidence must reference the immutable successful run and exact digests.

## Recorded local end-to-end observation

On 2026-08-20, commit `99fb8255fec6d7234763b916bc345605e86614e0` was checked out into
two distinct detached worktrees. CPython 3.12.12 installed
`requirements/build-py312.lock` in pip hash/binary-only mode. Both checkouts were built with
`python -m build --no-isolation` and `SOURCE_DATE_EPOCH=1787186318`, then normalized.

The comparator returned `byteIdentical: true`:

| Artifact | Bytes | SHA-256 |
|---|---:|---|
| `quantum_entanglement-0.1.0-py3-none-any.whl` | 158926 | `f5510c79e22a407dfcb475f89004c6a31cf9936c6df2293d04764ddf85937b3b` |
| `quantum_entanglement-0.1.0.tar.gz` | 269933 | `6747a0128aa19c39e8110571c2194970ea1958c87af0f544e0b707e0fc79a095` |

The canonical distribution manifest was strictly verified against that full commit and had
SHA-256 `7b5d51bc6e460dc75b9ca664e524460b296606b09806ba6601bfd4c841c64378`.
SBOM generation and byte verification then produced:

| SBOM | Components | Bytes | SHA-256 | CycloneDX 1.6 schema |
|---|---:|---:|---|---|
| runtime | 0 | 2238 | `1472c6cad491fa0a807cc22efde64d775472d2ed955c631326306685ebfa02ca` | pass |
| build | 51 | 79753 | `9c432cb0b4703734b9548d733f2027cc0d87c320d2591fddff6840ff7389df34` | pass |

The four documented `uv==0.9.27` commands also reproduced all four tracked lock files byte
for byte at the declared cutoff. These are local engineering observations for the named
commit, not retained phase-release evidence and not a clean-host or Linux-runner attestation.

## Release evidence requirements

For every promoted candidate, retain and review:

1. full source commit and tree SHA;
2. canonical lock policy plus all input/lock digests;
3. resolver version, resolution cutoff, selected index identity, and dependency-review
   decision;
4. target interpreter and platform matrix;
5. both compressed package digests and the verified distribution manifest digest;
6. both exact SBOM files and their SHA-256 values;
7. internal SBOM verifier result and independent CycloneDX schema result;
8. vulnerability, malware, and license policy reports bound to the same SBOM/package
   digests;
9. immutable successful CI run and builder identity;
10. provenance/signature verification result, exceptions, owner, and expiry.

If source, lock, toolchain, package, manifest, or SBOM bytes change, the candidate identity
changes and the entire chain must be rebuilt and reverified. Do not edit an SBOM after
generation or reuse one from another candidate.

## Open production supply-chain gates

The following remain explicit GA blockers or deployment-specific requirements:

- replace mutable `ubuntu-latest` with an immutable, attestable runner/base-image identity;
- retain and verify the CPython distribution and `uv` bootstrap binary by digest;
- bind the configured package-index/snapshot identity and defend against dependency
  confusion; ideally build from a reviewed offline wheelhouse or immutable dependency
  mirror;
- add vulnerability, malware, maintainer-risk, and license policy with severity thresholds,
  exception owners, expiry, and fail-closed CI;
- produce signed build provenance from an isolated builder and verify it at promotion and
  deployment;
- sign the distributions, manifest, and SBOMs or bind them under one signed release
  attestation;
- reproduce the candidate on an independently provisioned Linux runner and retain both
  digest sets;
- model the interpreter, OS packages, container image, and deployment dependencies;
- add a resolved runtime lock/SBOM for every supported optional extra, including
  `langgraph`, before enabling it in a supported deployment;
- operate dependency freshness, emergency revocation, compromised-package response, and
  artifact-retention runbooks.

Until those controls pass with retained evidence, the implemented lock and SBOM slice is a
strong pre-production gate, not authorization to describe the release as trusted,
hermetic, signed, vulnerability-cleared, or generally available.
