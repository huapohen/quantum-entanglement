# Release gates

No Quantum Entanglement version is promoted because its implementation is "mostly
complete." Promotion requires recorded evidence for every gate in this document.

## Change gate — every commit

Each commit must be independently reviewable and leave the default branch runnable.

- Scope: one behavior, migration, test group, runbook, or release artifact.
- Tests: changed behavior has a deterministic test in the same commit when applicable.
- Compatibility: public API/schema changes state their compatibility impact.
- Hygiene: `git diff --check` passes and no secret, token, credential or private key is
  introduced.
- Documentation: operator- or user-visible behavior is documented with the change.
- Safety: failure and retry behavior is explicit for any external side effect.

Mechanical formatting may be grouped only when it has no semantic change.

## Runtime-boundary gate

[`SERVICE_BOUNDARY.md`](./SERVICE_BOUNDARY.md) is mandatory. A demo, test count, phase
label, README statement, environment value or operator convenience cannot relax it.
Promotion is cumulative:

| Gate | Required promotion evidence | Maximum permitted runtime after promotion |
|---|---|---|
| A | strict config/secret/redaction/schema control, trusted request context, mandatory repository scope and legacy rehearsal | offline tenant-scoped kernel with synthetic data |
| B | authenticated loopback API, command/action receipts, fenced fake connector, resumable stream and lifecycle | isolated authenticated E2E using fake connectors |
| C | complete backup/restore, least-privilege single-node deployment, upgrade/rollback and measured recovery | controlled private-pilot candidate in the approved topology |
| D | quota/capacity, OTel/alerts, worker isolation, security review and soak | limited commercial candidate inside measured SLOs |
| E | PostgreSQL, HA/Kubernetes, continuous immutable DR and recurring rehearsal | multi-instance GA candidate |

All gates are currently closed. Every promotion record must name the exact source/tree,
supported topology, data class, connector allowlist, measured limits, unresolved findings,
reviewers and rollback trigger. A later gate cannot waive an earlier gate. Any P0,
security-critical issue, tenant escape, data-loss defect, credential leak or unauthorized
irreversible effect immediately withdraws promotion.

No gate in this repository authorizes a real Feishu or WeCom send. Such a connector needs
new explicit authorization and an independent security review; current tests use fake,
no-op or read-only fixtures only.

## Continuous verification gate

The following commands define the current local baseline:

```bash
python3 scripts/verify_dependency_locks.py --repository-root .
PYTHONPATH=src python3 -m pytest -q
ruff check src tests scripts
ruff format --check src tests scripts
PYTHONPATH=src mypy --strict src/quantum_entanglement
PYTHONPATH=src python3 -m compileall -q src tests scripts examples
PYTHONPATH=src python3 examples/group_chat_demo.py
git diff --check
```

The local generator executes this baseline, binds it to source identity and emits redacted
canonical JSON:

```bash
python3 scripts/generate_release_evidence.py
```

See [`LOCAL_RELEASE_EVIDENCE.md`](./LOCAL_RELEASE_EVIDENCE.md) for its exact predicate,
schema, exit codes, redaction boundary and retention rules. The generator records only what
it runs and cannot replace clean-host, package, security, recovery, performance or human
promotion evidence. CI adoption does not remove the clean-host verification gate.

Every retained JSON must pass `scripts/verify_release_evidence.py` against the exact clean
checkout and expected full commit SHA before it is consumed. Artifact presence alone is not
a pass: CI deliberately retains failed or partial output for diagnosis. The verifier's
success also remains local-baseline evidence, not permission to waive another gate.

## Distribution integrity gate

Every candidate wheel and sdist must have a canonical manifest generated and strictly
verified by `scripts/distribution_manifest.py` against the exact clean checkout and expected
full commit SHA. The verifier must prove the exact distribution set, source bytes, package
metadata, wheel `RECORD`, bounded safe archive structure, compressed artifact digests, and
canonical unpacked-content digests. Retain only the verified packages and out-of-tree
manifest, and record their digests and immutable successful CI run in the phase evidence.

See [`DISTRIBUTION_INTEGRITY.md`](./DISTRIBUTION_INTEGRITY.md) for the enforced contract,
commands, CI behavior, evidence fields, and known limitations. This content-integrity check
is not signed provenance, an SBOM, a dependency lock or scan, a trusted build environment,
or proof of byte-for-byte reproducibility.

## Reproducible build gate

Every packaged candidate must be built twice from the same expected full commit in distinct
source and output directories. Both builds use the commit timestamp as `SOURCE_DATE_EPOCH`;
both sdists are canonicalized with `scripts/normalize_sdist.py`; and
`scripts/verify_reproducible_distributions.py` must prove that the wheel and sdist filenames
and complete bytes are identical before manifest generation, smoke installation, or upload.

Record both source identities, the exact runner and toolchain, epoch, build/normalization
commands, both compressed artifact digest sets, comparator result, and immutable successful
CI run. See [`REPRODUCIBLE_BUILDS.md`](./REPRODUCIBLE_BUILDS.md) for the enforced same-job
predicate and its boundary. The Python frontend/backend inputs are now exact and hash
checked, but a same-runner pass is not evidence of reproducibility across independently
provisioned hosts, runner images, platforms, interpreters, bootstrap tools, or compression
implementations.

## Dependency lock and SBOM gate

Every supported CI Python/platform target must map to one exact lock-policy record. Before
installation, `scripts/verify_dependency_locks.py` must validate the canonical inventory,
input/lock digests, direct roots, complete exact-version/hash closure, binary-only policy,
and agreement with `pyproject.toml`. CI installation must use `--require-hashes` and
`--only-binary :all:`. Project installation and both package builds must disable build
isolation/dependency resolution so an implicit environment cannot bypass the verified
closure.

After exact package reproduction and distribution-manifest verification, generate the
runtime and build-toolchain CycloneDX 1.6 SBOMs outside the checkout. Require the exact
two-file set to pass repository structural/graph/source-byte verification and the official
CycloneDX strict schema before smoke installation or artifact upload. Phase evidence must
retain both SBOM digests and bind vulnerability/license policy results to those same bytes.

See [`DEPENDENCY_LOCKS_AND_SBOM.md`](./DEPENDENCY_LOCKS_AND_SBOM.md) for the supported target
matrix, regeneration commands, deterministic SBOM profile, local end-to-end observation,
failure handling, and explicit trust boundary. Locks and SBOMs are necessary evidence; by
themselves they are not vulnerability clearance, signed provenance, or a trusted builder.
The source-bound offline risk contract is defined in
[`DEPENDENCY_RISK_PROMOTION.md`](./DEPENDENCY_RISK_PROMOTION.md). Its committed policy has
promotion disabled and empty scanner, database, and license approvals, so it is currently
a fail-closed contract rather than a passed promotion gate.

## Phase release gate

Every phase release requires a file under `docs/production/evidence/<version>.md` with:

1. source commit and repository tree digest;
2. supported Python, database, operating-system and adapter versions;
3. exact test, integration, fault, security and performance commands;
4. pass/fail metrics and links to retained artifacts;
5. database migration rehearsal and rollback result;
6. backup/restore and disaster-recovery result when storage is affected;
7. known limitations, accepted risks and their owner/expiry;
8. deployment, readiness, smoke-test and rollback steps;
9. unresolved issue inventory by severity;
10. reviewer and promotion decision.

Evidence must describe what ran. Architectural expectation is not evidence.

## Severity policy

| Severity | Definition | Promotion rule |
|---|---|---|
| P0 | data loss, tenant escape, credential exposure, unauthorized irreversible effect | blocks every release |
| P1 | unavailable core workflow, unrecoverable queue, incorrect authorization, incompatible upgrade | blocks phase/GA release |
| P2 | degraded non-critical feature with documented workaround | requires owner and target version |
| P3 | cosmetic, documentation or low-impact optimization | may ship when recorded |

Security findings use the higher of exploit impact and data/authority impact.

## Reliability gates

- Crash injection at each durable boundary cannot produce a lost accepted command.
- At-least-once delivery may duplicate transport attempts but not accepted receiver effects.
- Lease expiry and fencing prevent stale workers from acknowledging newer ownership.
- Replay from the latest supported backup reconstructs task, approval, artifact and receipt
  state deterministically.
- Graceful shutdown stops admission, drains safe work, and relinquishes incomplete leases.
- Retry limits and dead-letter transitions are bounded, observable and operable.

## Security gates

- Every public operation has an authenticated actor and tenant/workspace scope.
- Authorization defaults to deny and is evaluated at action time.
- Delegation can only narrow action, resource, data, tenant and time scope.
- Cross-tenant, confused-deputy, replay, SSRF and secret-canary tests pass.
- Logs, traces, events, error responses and artifacts contain no plaintext credential.
- Threat model has no unresolved P0/P1 issue.

## Protocol and API gates

- Version-pinned contract tests cover success, error, cancellation, retry and unknown fields.
- Idempotency behavior and status reconciliation are documented.
- Request body, attachment, concurrency and stream-buffer limits are enforced.
- Resumable streams prove no gap and no accepted-event duplication across reconnect.
- Deprecation and compatibility windows are published before breaking changes.

## Performance and operations gates

- Capacity test records workload, data size, concurrency, latency percentiles and resources.
- Endurance test detects unbounded cache, task, session, connection or file growth.
- Alerts exist for availability, latency, queue age, dead letters, projector lag, storage,
  auth denial anomalies and cost.
- On-call runbooks cover degraded dependency, stuck lease, corrupt projection, restore,
  credential incident and rollback.
- Stated SLOs are backed by measured results and an error-budget policy.

## General availability gate

`1.0.0` additionally requires:

- reproducible build, locked dependencies, SBOM and vulnerability-policy pass;
- reference deployment with least-privilege and network-deny defaults;
- clean install, upgrade from every supported version, rollback and restore rehearsal;
- published support, compatibility, deprecation, retention and incident policies;
- release-candidate soak with no unresolved P0/P1 issue;
- formal operational acceptance recorded in the release evidence.

**Current status:** canonical sdist normalization and an independent detached-worktree
rebuild close the observed setuptools `mtime` and owner/group metadata drift within one CI
job. Build, development, and release Python tools are now exact-version and hash locked for
the declared CPython/x86_64-Linux matrix; CI verifies those locks, installs them in pip hash
mode, and performs both builds without isolation. At `99fb825`, two detached worktrees
produced byte-identical locked-toolchain wheel/sdist sets, passed strict manifest
verification, and produced source-bound runtime/build SBOMs that passed byte verification
and the CycloneDX 1.6 schema.

The remaining GA supply-chain gaps are independently provisioned immutable-runner
reproduction, verified interpreter/resolver bootstrap, offline or immutable dependency
mirror, optional-extra/deployment SBOM coverage, an approved real scanner/database/license
policy and result, malware/maintainer-risk controls, CI promotion wiring, signed provenance,
trusted builder identity, and artifact signatures. The implemented package job and disabled
offline evaluator satisfy contract rows only; the production supply chain is still not
complete.

## Rollback rule

Promotion stops immediately when a gate regresses. Rollback must prefer a compatible
application downgrade. If a data migration is not reversible, the release must provide a
tested restore-and-forward-fix procedure before promotion; "restore from backup" without a
rehearsal is not an acceptable rollback plan.
