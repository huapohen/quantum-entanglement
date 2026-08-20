# Reproducible build gate

Quantum Entanglement requires two builds of the same source commit to produce the same
wheel and normalized source distribution, byte for byte, before either artifact can become
a release candidate. The `package` workflow enforces this lower-bound reproducibility gate
inside one CI job and then applies the separate source-bound distribution integrity gate.

This document distinguishes three claims that must not be collapsed:

1. **same-job reproducibility**: two checkout directories built by the same runner and
   toolchain produce identical files;
2. **cross-environment reproducibility**: independently provisioned, pinned environments
   produce identical files;
3. **trusted supply chain**: the source, dependencies, builder identity, provenance, SBOM,
   policy results, and signatures are independently verifiable.

The first claim is currently enforced with an exact, hash-checked Python build toolchain.
The second claim is not yet enforced. The third has source-bound manifest, dependency-lock,
and SBOM controls, but remains incomplete until the open trust, policy, provenance, and
signature gates below are closed.

## Enforced CI predicate

The package job executes the following sequence without `continue-on-error`:

1. Check out `${{ github.sha }}` without persisting Git credentials.
2. Select Python 3.12, strictly verify all lock inputs, and install
   `requirements/build-py312.lock` with pip hash and binary-only enforcement.
3. Set `SOURCE_DATE_EPOCH` to the Git commit timestamp.
4. Build one wheel and one sdist in the primary checkout with
   `python -m build --no-isolation`.
5. Canonicalize that sdist with `scripts/normalize_sdist.py`.
6. Create a distinct detached worktree at exactly `${{ github.sha }}` under
   `runner.temp`.
7. Build a second wheel and sdist with `--no-isolation` into a distinct output directory
   with the same locked Python process environment and `SOURCE_DATE_EPOCH`.
8. Canonicalize the second sdist with the same normalizer.
9. Run `scripts/verify_reproducible_distributions.py` and require identical filenames,
   lengths, and bytes for both wheel/sdist sets.
10. Generate and strictly verify the source-bound distribution manifest for the primary
    set.
11. Install the hash-checked release lock, generate and byte-verify the exact runtime/build
    SBOM set outside the checkout, and validate both documents against CycloneDX 1.6.
12. Verify package data, smoke-install the wheel, and only then upload the packages,
    manifest, and two SBOMs.

The second checkout is independent as a filesystem worktree, not as a Git object database,
host, runner image, network, interpreter, dependency mirror, or trust root. The build and
release Python packages are now exact and hash checked, but both builds still reuse the same
installed environment. The byte comparator proves only what it reads from the two output
directories. The workflow supplies the stronger source condition by creating the detached
worktree from `${{ github.sha }}`; the later manifest and SBOM verification bind the
retained primary artifacts back to that same full commit.

## Canonical sdist contract

Raw setuptools sdists inherit metadata that is not part of the source payload, including
checkout modification times and local owner/group identity. `scripts/normalize_sdist.py`
rewrites the single `.tar.gz` in a distribution directory in place so that this container
metadata is deterministic.

Before rewriting, the normalizer requires:

- a regular, non-symlink distribution directory containing exactly one `.tar.gz` candidate;
- a regular, non-symlink archive with a stable inode and metadata throughout a bounded read;
- a canonical non-negative `SOURCE_DATE_EPOCH` no greater than `2^32 - 1`;
- a gzip-compressed tar with bounded compressed size, expansion, member size/count, and
  total file bytes;
- safe ASCII member paths under the one root derived from the archive filename;
- exactly one explicit root directory and at least one regular file;
- no duplicate path, traversal, link, sparse member, device, FIFO, or other special type.

It preserves every regular file's exact bytes and only the executable/non-executable
distinction. It then emits members in path order with:

| Container field | Canonical value |
|---|---|
| gzip filename | empty |
| gzip compression level | `9` |
| gzip and tar member time | `SOURCE_DATE_EPOCH` |
| tar format | USTAR |
| `uid` / `gid` | `0` / `0` |
| `uname` / `gname` | empty / empty |
| PAX headers | empty |
| directory mode | `0755` |
| executable regular-file mode | `0755` |
| other regular-file mode | `0644` |

The replacement is written to a same-directory temporary regular file, flushed and
`fsync`ed, installed with `os.replace`, and followed by a directory `fsync`. The original
archive identity must remain unchanged until replacement. Normalization is idempotent: the
same payload, epoch, and executable bits produce the same bytes even when the input order,
timestamps, owner/group fields, PAX metadata, or non-semantic permission bits differ.

Successful CLI output is a compact JSON summary with archive name, byte size, member count,
SHA-256, and epoch. A validation or write failure exits `1` with a fixed reason code and does
not print filesystem paths. Invalid command-line syntax exits `2` through `argparse`.

## Exact distribution-set comparison

`scripts/verify_reproducible_distributions.py` compares two distinct distribution
directories. It fails closed unless:

- the directories have different device/inode identities and retain stable metadata;
- each directory contains exactly one wheel and one sdist and no other entry;
- every entry has a bounded safe filename and is a stable regular non-symlink file;
- the two filename sets are equal; and
- each same-named file has exactly the same length and bytes.

Success emits compact JSON containing `byteIdentical: true` plus filename, byte size, and
SHA-256 for both artifacts. The comparator does not inspect Git, invoke a build, infer the
toolchain, or accept content-level equivalence in place of file equality. A mismatch or
invalid input exits `1` with a fixed non-sensitive code; command-line misuse exits `2`.

This comparison is intentionally stronger than matching the distribution manifest's
`contentSha256`: container metadata contributes to the compressed artifact bytes and
therefore must match too.

## Local verification recorded for `e4cbf04`

On 2026-08-20, the gate was exercised locally against:

- source commit: `e4cbf040579bf1f33c2b7692d2fbd6944d837952`;
- source tree: `0a879ae5351bdc3747a00cf4277ee6460df62d15`;
- platform: Darwin arm64, macOS `26.5.2`;
- runtime: CPython `3.12.13`;
- backend: setuptools `84.0.0`;
- `SOURCE_DATE_EPOCH`: `1787182129`.

Two complementary repeated-build checks ran:

1. two distinct clean detached worktrees at the exact source commit; and
2. two independent local `git clone --no-local` repositories, each detached at the exact
   source commit.

All build outputs lived outside their source checkouts, and all four source checkouts
remained Git-clean after building. The local environment did not have the PyPA `build`
frontend installed, so these observations invoked the declared `setuptools.build_meta`
backend directly with the same CPython/setuptools runtime for `build_wheel` and
`build_sdist`. This is transparent supporting evidence, not a claim that the local command
was identical to CI's `python -m build` frontend invocation.

After canonical normalization, all four builds produced these exact files:

| Artifact | Bytes | SHA-256 |
|---|---:|---|
| `quantum_entanglement-0.1.0-py3-none-any.whl` | 155207 | `adfc19e7c0c7434fe9d16f24562b43ff3c230682183d93830b94025ea2f220ac` |
| `quantum_entanglement-0.1.0.tar.gz` | 254440 | `118011508d547dc45da728115acc9ee46a42714aa1490aceb3de2ae43bd34888` |

The exact comparator returned `byteIdentical: true` for the worktree pair and for the clone
pair. A canonical distribution manifest was independently generated outside each of the
four source checkouts and strictly verified against the full expected commit; every
verification printed `distribution manifest verified`.

This closes the previously observed **specific** defect in which setuptools sdist output
varied with checkout `mtime`, `uid`, `gid`, `uname`, and `gname`. It also demonstrates that
the normalized wheel/sdist set can be reproduced across different local checkout paths
under this one fixed runtime. The observation is committed documentation, not an immutable
phase-release artifact; a promoted candidate must retain the corresponding CI run, logs,
digests, and manifest under the release evidence policy.

## Locked-toolchain verification recorded for `99fb825`

On 2026-08-20, the current workflow sequence was repeated locally at
`99fb8255fec6d7234763b916bc345605e86614e0` with CPython 3.12.12. A fresh environment
installed `requirements/build-py312.lock` using `--require-hashes --only-binary :all:`.
Two distinct detached worktrees were built with `python -m build --no-isolation` and
`SOURCE_DATE_EPOCH=1787186318`; both sdists were normalized and the exact comparator
returned `byteIdentical: true`.

| Artifact | Bytes | SHA-256 |
|---|---:|---|
| `quantum_entanglement-0.1.0-py3-none-any.whl` | 158926 | `f5510c79e22a407dfcb475f89004c6a31cf9936c6df2293d04764ddf85937b3b` |
| `quantum_entanglement-0.1.0.tar.gz` | 269933 | `6747a0128aa19c39e8110571c2194970ea1958c87af0f544e0b707e0fc79a095` |

The source-bound manifest then passed strict verification. The runtime and build SBOMs
were regenerated, byte-verified, and accepted by the CycloneDX 1.6 strict schema validator.
All four locks were also independently regenerated with the recorded `uv==0.9.27`, cutoff,
Python, and Linux target settings and matched the tracked files byte for byte. Exact
manifest/SBOM digests and the reproduction commands are recorded in
[`DEPENDENCY_LOCKS_AND_SBOM.md`](./DEPENDENCY_LOCKS_AND_SBOM.md).

This closes the stale **floating Python build frontend/backend** gap for the supported CI
target. It does not convert a local macOS observation into Linux, immutable-runner, or
independent-builder evidence.

## Remaining GA gates

The successful same-toolchain result must not be described as a completed production supply
chain. At least the following remain open:

- Both CI builds run on the same runner, interpreter, environment, network, and job. No
  cross-runner, clean-host, immutable-container-image, operating-system, architecture, or
  independently installed toolchain comparison is retained.
- `ubuntu-latest`, the CPython distribution, and the `uv` bootstrap binary are not bound to
  immutable verified digests; the configured package index is not an immutable snapshot.
- The detached worktree shares the primary checkout's Git object database and trust root.
- The wheel has no post-build canonicalizer; its equality is observed and enforced only for
  the two builds using the current same-job toolchain.
- The hashed Python closure is not a hermetic/offline wheelhouse and does not model the
  interpreter, runner OS packages, or deployment image.
- Runtime/build SBOMs are produced, but optional runtime extras, vulnerability/license
  policy, signed provenance, artifact signatures, and trusted builder identity remain open.
- The normalizer and comparator are repository code in the build trust boundary. Their
  source is bound by the commit but their execution is not independently attested.

GA therefore still requires repeated builds in independently provisioned immutable
environments, retained cross-environment digest evidence, dependency risk-policy results,
signed provenance, and artifact signing. A green package job satisfies the same-job
reproducibility, distribution-integrity, lock, and base/build-SBOM rows for that exact
candidate; it does not satisfy the complete trusted-supply-chain gate.

## Release evidence requirements

For each promoted package candidate, retain:

1. the full source commit and tree SHA for both build checkouts;
2. the runner image, OS/architecture, Python, `build`, setuptools/backend, compression,
   lock-policy, and normalizer versions or immutable digests;
3. the exact `SOURCE_DATE_EPOCH` and both build/normalization commands;
4. the comparator's successful output or immutable log and both sets of compressed artifact
   SHA-256 values;
5. the canonical distribution manifest, its digest, strict verifier result, both SBOMs and
   schema results, package smoke result, and immutable package CI run;
6. separate results for cross-environment reproduction, vulnerability/license policy,
   signed provenance, and artifact signatures.

A missing, skipped, unexplained, or non-identical repeated build is a failed package gate.
Do not select one of two mismatching outputs, regenerate only the manifest, or publish an
artifact from a failed comparison. Diagnose the nondeterminism, change the source or pinned
toolchain, and rerun the entire candidate pipeline.
