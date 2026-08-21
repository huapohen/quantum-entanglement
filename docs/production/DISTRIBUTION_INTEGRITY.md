# Distribution integrity gate

`scripts/distribution_manifest.py` inspects the built wheel and source distribution, binds
their exact contents to one clean Git checkout, and emits a canonical JSON manifest. The
same tool can later reopen the manifest, independently reinspect both archives, and require
exact equivalence with the checkout and an externally supplied commit SHA.

This gate answers: **do these two package files contain exactly the source and packaging
metadata that this clean commit is expected to produce?** It does not establish who built
or signed them, whether the build toolchain is trustworthy, or by itself whether another
build would produce byte-identical files. The separate same-job repeated-build predicate is
defined in [`REPRODUCIBLE_BUILDS.md`](./REPRODUCIBLE_BUILDS.md).

## Generate and verify

Start from a clean checkout and a distribution directory containing exactly the expected
wheel and sdist. Bind build timestamps to the commit before invoking the build frontend:

```bash
export SOURCE_DATE_EPOCH="$(git show -s --format=%ct HEAD)"
python -m build
python scripts/normalize_sdist.py --distribution-directory dist
```

The manifest must be written outside the checkout. Redirecting it into the repository would
make the source dirty and correctly fail inspection or verification.

```bash
qe_manifest_path="$(mktemp "${TMPDIR:-/tmp}/qe-distribution-manifest.XXXXXX.json")"
python scripts/distribution_manifest.py generate \
  --repository-root . \
  --distribution-directory dist \
  > "$qe_manifest_path"

python scripts/distribution_manifest.py verify \
  "$qe_manifest_path" \
  --repository-root . \
  --distribution-directory dist \
  --expected-commit "$(git rev-parse HEAD)"
```

Generation exits `0` only after a complete inspection and writes one canonical JSON record
to standard output. Verification exits `0` and prints `distribution manifest verified` only
after recomputing and matching every enforced value. Validation failures exit `1` with a
fixed, non-sensitive reason code; invalid command-line usage exits `2`.

The verifier requires the same Python implementation and version recorded by generation.
Generate and verify in the same pinned release environment unless a future schema explicitly
defines a portable runtime policy.

## Enforced source and package contract

Generation and verification fail closed unless all of these conditions hold:

- The checkout is clean, has a resolvable full commit and tree SHA, and retains the same
  commit, tree, and clean state throughout inspection or verification.
- `pyproject.toml` contains one supported project name and version, and the version exactly
  matches the package's literal `__version__` declaration.
- `dist/` contains exactly one expected pure-Python wheel and one expected gzip-compressed
  sdist, with filenames derived from that project identity. Missing, stale, or extra files
  are rejected.
- Both artifact paths are bounded regular files. Symlinks, unstable reads, oversized
  archives, excessive member counts or expansion, unsafe paths, duplicate members,
  encrypted wheel members, sparse tar members, links, and special files are rejected.
- Every Git-tracked package file appears in the wheel with byte-for-byte identical content.
  The wheel contains only that package tree plus the expected license and fixed
  `.dist-info` inventory.
- Wheel `METADATA` name and version, the single `py3-none-any` tag, console entry points,
  top-level package declaration, and every SHA-256 digest and size in `RECORD` match the
  inspected files.
- The sdist contains byte-for-byte copies of the tracked package and test trees plus
  `LICENSE`, `MANIFEST.in`, `README.md`, and `pyproject.toml`. `MANIFEST.in` explicitly
  includes the test package marker that setuptools' default `tests/test*.py` discovery does
  not select. Its directory and generated metadata inventory is exact, `SOURCES.txt`
  describes that inventory, and generated `setup.cfg` cannot inject alternate build
  behavior.
- Wheel and sdist package metadata, entry points, and top-level package declaration agree.

Each artifact record includes its filename, kind, compressed byte size and SHA-256, member
and regular-file counts, and a `contentSha256` over canonical records of every extracted
filename, file SHA-256, and file size. The compressed digest identifies the retained file;
the content digest identifies its file payload. Directory ownership, modes, timestamps,
compression parameters, and other container metadata are not included in `contentSha256`.

## Manifest integrity

The manifest format is `quantum-entanglement.distribution-manifest`, schema version `1`. It
records:

| Section | Recorded evidence |
|---|---|
| `source` | Commit/tree SHA before and after inspection, clean state, stable identity |
| `project` | Canonical package name and version |
| `inspectionRuntime` | Python implementation and exact version |
| `artifacts` | Exact wheel/sdist identities, compressed and content digests, sizes and counts |
| `generatedAt` | Canonical UTC generation time with six fractional digits |

The loader accepts only a bounded, stable, regular non-symlink file containing canonical
UTF-8 JSON with one trailing newline. Duplicate keys, non-finite values, unknown or missing
fields, invalid types and malformed digests are rejected. The verifier refuses a manifest
stored anywhere inside the checkout, even if Git ignores it, and can bind verification to
the immutable CI source identity with `--expected-commit`.

The manifest is a digest-bearing evidence record, not a signature. Anyone able to replace
both packages and the unsigned manifest could create a different internally consistent set;
retain it only behind the release system's immutable artifact and access controls.

## CI behavior

The `package` workflow performs the implemented gate in this order:

1. checks out `${{ github.sha }}` without persisted Git credentials;
2. selects Python 3.12, installs the build frontend, and derives `SOURCE_DATE_EPOCH` from the
   commit timestamp;
3. builds exactly one wheel and one sdist and canonicalizes the sdist container metadata;
4. creates a detached worktree at `${{ github.sha }}`, rebuilds into a distinct directory,
   and canonicalizes the second sdist with the same epoch;
5. requires exact filename and byte equality across both wheel/sdist sets;
6. generates the manifest under `runner.temp`, outside the checkout;
7. verifies it against the retained archives, clean checkout, runtime, and
   `${{ github.sha }}`;
8. checks durable migration package data and smoke-installs the wheel in a new virtual
   environment;
9. uploads the two verified distributions and manifest for 14 days.

The package upload does not use `always()`. A failed manifest check or failed smoke test
therefore cannot be retained by that step as an official distribution artifact. Artifact
presence is still not an independent trust proof: promotion evidence must reference the
immutable successful run, manifest digest, and both compressed artifact digests.

## Explicit non-guarantees

Passing this gate does **not** prove or replace any of the following:

- cryptographic signing, trusted identity, SLSA or other signed build provenance;
- an SBOM, dependency lock, dependency or license policy, vulnerability scan, or malware
  analysis;
- hermetic or offline construction, a pinned build frontend/backend, or a trusted runner;
- cross-runner or cross-toolchain reproducibility across hosts, platforms, Python versions,
  setuptools versions, or compression implementations;
- source-review, authorization, deployment, migration, recovery, security, performance, or
  human promotion gates.

The current CI installs `build` from the configured package index and relies on the declared
`setuptools>=77` build requirement. Those floating inputs are not a locked or hermetic
supply chain. Signed provenance, SBOM production, policy scanning, toolchain locking, and
artifact signing remain separate release work.

## Reproducibility relationship

The earlier raw-setuptools sdist drift from checkout modification times and owner/group
metadata is now handled by `scripts/normalize_sdist.py`. The package workflow canonicalizes
both sdists and requires the normalized wheel/sdist sets to be byte-identical before this
manifest gate runs. Local detached-worktree and independent-clone observations at
`e4cbf04` produced identical compressed digests and passed strict manifest verification.

This closes that specific metadata-drift defect under the observed same-job toolchain. It
does not make `contentSha256` a reproducibility proof, because that digest still excludes
container metadata; `scripts/verify_reproducible_distributions.py` compares the complete
files. It also does not establish cross-runner or cross-toolchain reproduction. See
[`REPRODUCIBLE_BUILDS.md`](./REPRODUCIBLE_BUILDS.md) for the exact predicate, recorded local
evidence, and remaining GA supply-chain gates.

## Release evidence

For every candidate package, retain and reference:

1. the full source commit and tree SHA;
2. the exact canonical manifest and its SHA-256;
3. both distribution filenames, byte sizes, compressed SHA-256 values, and content digests;
4. the immutable CI run in which repeated build, exact comparison, generation, strict
   verification, package-data check, and wheel smoke installation all passed;
5. the separate reproducibility result, signed provenance/attestation, SBOM, dependency and
   license policy, vulnerability scan, and signature status, including explicit failures or
   missing evidence.

Never edit the manifest or rebuild one artifact in place after verification. Any source,
toolchain, package, or manifest change creates a new candidate and requires a complete
rebuild, generation, verification, smoke test, and evidence record.
