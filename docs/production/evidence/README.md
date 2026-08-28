# Release evidence

Each promoted version has one immutable evidence record in this directory. Start from the
template below, replace every placeholder, and commit the completed record with the release
candidate. A missing, assumed, skipped, or unexplained result is a failed gate.

Non-release, pre-promotion checkpoint summaries live under `checkpoints/`. They must identify
their exact source commit/tree, state whether raw logs were retained, and list every scope
limitation. A checkpoint summary is supporting local evidence only; it never satisfies or
waives the release template below.

Use `vMAJOR.MINOR.PATCH.md` as the filename. If a candidate is rejected, retain its evidence
under `rejected/` with the source commit and rejection reason so later work cannot silently
reuse the same unproven artifact.

Evidence may contain sanitized commands, versions, counts, timings, digests, and links to
retained CI artifacts. It must not contain raw credentials, cookies, authorization headers,
private chat content, customer data, or secret environment values.

Generate the machine-readable local baseline described in
[`LOCAL_RELEASE_EVIDENCE.md`](../LOCAL_RELEASE_EVIDENCE.md), retain its exact JSON and digest
as an immutable sidecar, and link it from the completed record below. A JSON value of
`releasable: true` proves only that fixed local baseline; it does not fill or waive any
template field, production drill, reviewer decision, or promotion gate.

Before linking that sidecar, run `scripts/verify_release_evidence.py` against the exact clean
source checkout and record the verifier's successful immutable CI run. The CI artifact is
uploaded even after failure for diagnosis; its existence, filename, or downloadability is
not proof that verification passed.

For a packaged candidate, also retain the canonical out-of-tree distribution manifest
described in [`DISTRIBUTION_INTEGRITY.md`](../DISTRIBUTION_INTEGRITY.md). Record the
manifest digest, both compressed artifact digests, and the immutable successful package CI
run. Keep content integrity, byte-for-byte reproducibility, SBOM, signed provenance, and
artifact signature as separate results: none may be inferred from another. The same-job
predicate in [`REPRODUCIBLE_BUILDS.md`](../REPRODUCIBLE_BUILDS.md) requires an exact second
build and comparison before manifest verification, but it does not prove reproduction with
a different runner or toolchain. Record floating or missing build locks, cross-environment
evidence, SBOM, provenance, policy, and signatures explicitly as passed, failed, or pending.

## Template

```markdown
# Release evidence: vX.Y.Z

Decision: `promote | reject | pending`

Evidence captured at: `YYYY-MM-DDTHH:MM:SSZ`

Reviewer: `name or accountable role`

## Source identity

- Repository: `owner/repository`
- Branch: `main`
- Source commit: `<full SHA>`
- Tree SHA: `<full SHA>`
- Version: `X.Y.Z`
- Build artifact digests: `<algorithm:digest>`
- Rebuild artifact digests: `<algorithm:digest>`
- Repeated-build verifier run: `<immutable URL>`
- Distribution manifest digest: `<sha256:digest>`
- Distribution manifest verifier run: `<immutable URL>`
- SBOM digest: `<algorithm:digest | missing>`
- Signed provenance/attestation: `<immutable identity and URL | missing>`
- Artifact signature: `<identity and digest | missing>`
- CI run: `<immutable URL>`

## Supported boundary

- Python versions:
- Operating systems/architectures:
- Database engines and versions:
- Deployment topology:
- Supported protocol/adapter versions:
- Explicitly unsupported usage:

## Clean build and test environment

- Host/runner image:
- CPU and memory:
- Toolchain versions:
- Primary/rebuild source commit and tree:
- Build frontend/backend versions:
- SOURCE_DATE_EPOCH:
- Build and normalization commands:
- Dependency lock digest:
- Environment preparation commands:

## Verification results

| Gate | Exact command or immutable run | Result | Count/metric | Artifact |
|---|---|---|---|---|
| Unit tests | | | | |
| Integration tests | | | | |
| Fault injection | | | | |
| Cross-tenant/security | | | | |
| Protocol contracts | | | | |
| Static analysis | | | | |
| Dependency/license scan | | | | |
| Secret scan | | | | |
| Distribution content integrity | | | | |
| Package/install smoke | | | | |
| Repeated-build reproducibility | | | | |
| SBOM/provenance/signature | | | | |
| End-to-end workflow | | | | |

## Migration and compatibility

- Source version/database state:
- Forward migration command and duration:
- Schema checksums before/after:
- Mixed-version compatibility result:
- Data integrity queries/result:
- Rollback or restore-and-forward command/result:
- Breaking API/protocol/configuration changes:

## Backup, restore, and disaster recovery

- Backup command, start/end, size, digest:
- Restore target and command:
- Integrity/smoke checks after restore:
- Pending inbox/outbox/attempt reconciliation result:
- Revocation and lease freshness result:
- Measured RPO/RTO:

## Reliability and external-effect safety

- Crash points exercised:
- Lease takeover/fencing result:
- Retry/dead-letter result:
- Duplicate receiver-attempt result:
- Accepted-but-unconfirmed reconciliation result:
- Graceful and forced shutdown result:
- External systems used: `fake only | explicitly authorized list`

## Security

- Threat-model revision:
- Capability/authorization adversarial result:
- Cross-tenant property result:
- SSRF/egress result:
- Secret-canary scan result:
- Vulnerability inventory by severity:
- Security reviewer decision:

## Performance and endurance

- Workload shape and data size:
- Concurrency and duration:
- Throughput:
- Latency p50/p95/p99/max:
- Queue age and projector lag:
- CPU, RSS, disk, connection, and model/token cost:
- Endurance growth/leak result:
- Capacity limit and overload behavior:

## Deployment and rollback rehearsal

- Install/deploy commands:
- Readiness/liveness and smoke result:
- Upgrade command/result:
- Rollback trigger and exact procedure:
- Rollback duration and post-checks:
- Operator runbook revision:

## Known limitations and accepted risks

| ID | Severity | Limitation/risk | Owner | Expiry/version | Mitigation |
|---|---|---|---|---|---|
| | | | | | |

## Unresolved issues

- P0: `none | list`
- P1: `none | list`
- P2/P3 with owner and target:

## Promotion decision

State which release gates passed, which did not, why the recorded evidence is sufficient,
and the exact artifact approved for deployment. A promotion requires no unresolved P0/P1.
```

## Retention and integrity

- Prefer immutable CI URLs and content digests over screenshots alone.
- Store large logs, SBOMs, profiles, and test reports as retained CI/release artifacts and
  link them from the evidence file.
- Record the source and tree SHA before testing; rerun affected gates after any source,
  dependency, build, migration, or configuration change.
- Never edit a promoted record to describe a different artifact. Add an amendment that
  identifies the original record and explains the correction.
- A remote green check without exact commands, test scope, and artifact identity is not
  sufficient evidence.
