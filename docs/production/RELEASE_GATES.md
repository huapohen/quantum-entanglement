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

[`SERVICE_BOUNDARY.md`](./SERVICE_BOUNDARY.md) is mandatory and cannot be relaxed by a demo,
test count, phase label, README statement, environment variable, or operator convenience.
Promotion is cumulative:

| Gate | Promotion evidence required | Maximum permitted runtime after promotion |
|---|---|---|
| A | strict config/redaction/schema control, trusted request context, mandatory repository scope and legacy contract rehearsal | offline tenant-scoped kernel with synthetic data |
| B | authenticated loopback API, command receipts, durable action receipts, fenced fake connector, resumable stream and lifecycle | isolated authenticated E2E using fake connector |
| C | complete backup/restore, least-privilege single-node container, upgrade/rollback and measured recovery evidence | controlled private-pilot candidate within the approved topology |
| D | quota/capacity, OTel/alerts, worker isolation, security review and soak | limited commercial candidate within measured SLOs |
| E | PostgreSQL, HA/Kubernetes, continuous immutable DR and recurring rehearsal | multi-instance GA candidate |

All gates are currently closed. Every promotion record must name the exact source commit,
supported topology, data class, connector allowlist, measured limits, unresolved findings,
reviewers and rollback trigger. A later gate cannot waive an earlier gate. Any P0,
security-critical issue, tenant escape, data-loss defect or unauthorized irreversible effect
immediately withdraws promotion.

No gate in this repository authorizes a real Feishu or WeCom send. Such a connector requires
new explicit authorization and an independent security review; current tests must use fake,
no-op or read-only fixtures.

## Continuous verification gate

The following commands define the current local baseline:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -q
PYTHONPATH=src python3 examples/group_chat_demo.py --compact
python3 -m compileall -q src tests scripts
ruff check src tests scripts
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
predicate and its boundary. A same-runner pass is not evidence of reproducibility across
unpinned frontends/backends, different hosts, runner images, platforms, interpreters, or
compression implementations.

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
rebuild now close the previously observed setuptools `mtime` and owner/group metadata drift
within one CI job. At `e4cbf04`, two detached worktrees and two independent local clones
produced byte-identical wheel/sdist sets and passed strict manifest verification. The
workflow nevertheless installs an unpinned `build` frontend and permits floating
`setuptools>=77`; cross-runner/toolchain reproduction, locked and hashed build inputs, SBOM,
vulnerability/license policy, signed provenance, and artifact signatures remain open GA
gates. The production supply chain is therefore not complete.

## Rollback rule

Promotion stops immediately when a gate regresses. Rollback must prefer a compatible
application downgrade. If a data migration is not reversible, the release must provide a
tested restore-and-forward-fix procedure before promotion; "restore from backup" without a
rehearsal is not an acceptable rollback plan.
