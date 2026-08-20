# Quantum Entanglement production roadmap

This roadmap turns the validated coordination kernel into a commercially operable
human–agent collaboration service. A phase is complete only when its code, tests,
operational documentation, migration path, rollback path, and release evidence are
all present in the repository.

## Production definition

The project may call itself production-ready only when all of the following are true:

- accepted work survives process and host failure without duplicate external effects;
- every stored object is tenant- and workspace-scoped, with default-deny authorization;
- irreversible or external actions require an auditable, action-time capability check;
- public inputs are authenticated, rate-limited, size-limited, and treated as untrusted;
- protocol adapters pass versioned contract suites and preserve unknown extensions;
- metrics, logs, traces, health checks, alerts, backup, restore, upgrade, and rollback are
  documented and exercised;
- release artifacts are reproducible, dependency-scanned, signed where supported, and
  covered by an SBOM and provenance record;
- load, fault-injection, recovery, security, and end-to-end suites meet published gates;
- a release candidate completes a documented soak period with no unresolved P0/P1 issue.

Passing unit tests alone is necessary but not sufficient.

## Commit and release discipline

- One independently reviewable behavior or document change per commit.
- Tests are committed with the behavior they prove.
- Schema changes include forward migration, compatibility notes, and rollback guidance.
- Every phase ends in a version bump, changelog, release checklist, and signed-off evidence.
- The default branch must remain runnable after every commit.

## Phase 0 — validated kernel (`0.1.x`, complete)

Runnable boundary: dependency-free, single-process coordination kernel and local demo.

Delivered:

- canonical coordination envelope and append-only event store;
- deterministic DAG scheduling, context compilation, policy and approval flow;
- artifact versioning and recovery from event history;
- transactional inbox/outbox storage primitives;
- A2A boundary mapping, LangGraph bridge, mention routing, and isolated Harness port;
- 54 passing tests and a deterministic three-Agent demo.

Not a production claim: there is no service API, tenant boundary, persistent artifact blob
store, distributed execution, operational telemetry, or deployment package yet.

## Phase 1 — reliable single-node service (`0.2.0`)

Runnable boundary: one-node deployment suitable for controlled internal pilots with fake
or explicitly approved external connectors.

Required deliverables:

1. Outbox publisher with timeout, retry, jitter, dead-letter and graceful shutdown.
2. Durable task attempts with lease, heartbeat, timeout and retry policy.
3. Persistent artifact metadata/blob transaction and integrity verification.
4. Projector offsets, idempotent replay, schema versions and upcasters.
5. External action receipts and compensation state machine.
6. Versioned SQLite migrations, backup/restore command and corruption checks.
7. Service lifecycle, readiness/liveness checks and structured local audit logs.

Release gates:

- crash-at-every-boundary fault tests show no duplicate accepted effect;
- backup restore reproduces event, task, approval and artifact heads;
- publisher and worker shutdown drain or safely relinquish leased work;
- migration from the previous tagged release is automated and rollback is documented;
- runbook demonstrates install, start, stop, recover and upgrade on a clean host.

## Phase 2 — secure multi-tenant core (`0.3.0`)

Runnable boundary: multiple internal organizations can share a deployment without data or
authority crossing tenant boundaries.

Required deliverables:

1. Tenant, workspace, member, role and service-principal domain model.
2. Scoped, expiring, non-escalating capabilities with action/resource binding.
3. Tenant-aware storage keys and mandatory isolation filters on every repository method.
4. Immutable authorization decisions and tamper-evident audit chain.
5. Secret-handle interface; plaintext credentials never enter events, prompts or logs.
6. SSRF-safe URL policy, DNS/IP revalidation and outbound network allowlists.
7. Input size/type validation, redaction, retention and deletion workflows.

Release gates:

- cross-tenant property tests and adversarial authorization tests all fail closed;
- delegated authority can only narrow action, resource, time and data scope;
- logs and persisted events pass secret/canary scans;
- threat model and abuse-case review have no unresolved P0/P1 finding.

## Phase 3 — authenticated service and protocol interoperability (`0.4.0`)

Runnable boundary: authenticated API clients, MCP tools and A2A agents can participate
through versioned, observable adapters.

Required deliverables:

1. Versioned HTTP API with request IDs, idempotency, pagination and error contracts.
2. OIDC/JWT authentication port and policy-enforced service identities.
3. Streaming event API with resumable cursors, backpressure and disconnect recovery.
4. MCP client/tool/resource adapter with consent and data-classification gates.
5. A2A 1.x SDK/TCK compatibility, Agent Card verification and remote status reconciliation.
6. Provider-neutral webhook ingress with signature, replay and deduplication protection.
7. Minimal operations console for sessions, tasks, artifacts, approvals and audit timeline.

Release gates:

- OpenAPI and protocol contract tests are version-pinned and backward-compatible;
- fuzzed and malformed requests cannot bypass authentication or exhaust unbounded memory;
- streaming resumes without gaps or duplicates after reconnect;
- no real Feishu or WeCom send is performed in this project without a new explicit user
  authorization; connector tests use fakes and captured read-only fixtures.

## Phase 4 — distributed operation and observability (`0.5.0`)

Runnable boundary: horizontally scaled workers and API instances with documented SLOs.

Required deliverables:

1. PostgreSQL storage implementation and migration from single-node SQLite.
2. Distributed task leasing, fencing, cancellation and backpressure.
3. OpenTelemetry traces, metrics and structured logs with tenant-safe cardinality.
4. Dashboards and alerts for availability, latency, queue age, error rate and cost.
5. Load, endurance, chaos, failover and noisy-neighbor test suites.
6. Capacity model and autoscaling guidance.

Initial SLO targets:

- API availability: 99.9% monthly for the supported deployment topology;
- accepted command durability: 99.999% after successful commit acknowledgement;
- RPO: 5 minutes or better; RTO: 30 minutes or better;
- no unresolved tenant-isolation or unauthorized-side-effect incident.

Release gates use measured evidence, not architectural expectation.

## Phase 5 — general availability (`1.0.0`)

Runnable boundary: supported commercial release with repeatable deployment and incident
response.

Required deliverables:

1. Container and reference deployment manifests with least-privilege defaults.
2. CI release pipeline, locked dependencies, SBOM, vulnerability policy and provenance.
3. Upgrade, rollback, backup, restore, disaster-recovery and incident runbooks.
4. Compatibility matrix, deprecation policy, support policy and data lifecycle contract.
5. Security assessment, performance report and release-candidate soak evidence.
6. User/admin documentation and an operational acceptance checklist.

GA is blocked by any unresolved P0, security-critical issue, data-loss defect, tenant escape,
or undocumented irreversible migration.

### Current Phase 5 supply-chain checkpoint (2026-08-20)

The following pre-GA primitives are implemented and enforced for the declared Python/Linux
CI matrix:

- build, development, and release tool roots and transitive closures use exact versions and
  SHA-256 hashes;
- the canonical lock policy binds the four supported scope/Python/platform targets, source
  inputs, lock files, resolver version, and resolution cutoff;
- CI verifies locks before installation, uses pip hash/binary-only mode, disables project
  dependency resolution and build isolation, and pins external actions by commit;
- the package job builds twice, normalizes sdists, requires byte-identical wheel/sdist sets,
  and strictly verifies a source-bound distribution manifest;
- deterministic runtime and build CycloneDX 1.6 SBOMs bind the source commit/tree and exact
  distributions; internal byte/profile verification and official schema validation run
  before upload.
- canonical vulnerability/license policy and result contracts plus a strict source-bound
  offline verifier are implemented, but the committed policy intentionally disables
  promotion and no real scanner/database/legal-policy approval or scan pass is claimed.

This is a partial completion of Phase 5 deliverable 2, not completion of Phase 5. The next
supply-chain milestones, in release-blocking order, are:

1. replace mutable runner/interpreter/resolver bootstrap identities with immutable verified
   digests and reproduce packages on an independently provisioned Linux runner;
2. introduce a reviewed offline wheelhouse or immutable dependency mirror and record its
   trust/update/compromise-recovery policy;
3. select and approve the scanner, database snapshot, and legal license allowlist; activate
   and retain the implemented vulnerability/license gate for an exact candidate; add
   malware and maintainer-risk controls separately;
4. cover optional runtime extras, interpreter/OS/container packages, and deployment
   manifests in resolved SBOM evidence;
5. issue and verify signed build provenance from a trusted isolated builder, then sign or
   jointly attest the packages, distribution manifest, and SBOMs;
6. exercise dependency revocation, emergency rebuild, key compromise, evidence retention,
   and deployment-time verification runbooks.

The exact implemented contracts and remaining boundaries are maintained in
[`DEPENDENCY_LOCKS_AND_SBOM.md`](./DEPENDENCY_LOCKS_AND_SBOM.md) and
[`DEPENDENCY_RISK_PROMOTION.md`](./DEPENDENCY_RISK_PROMOTION.md). Phase promotion still
requires an enabled reviewed policy and immutable retained evidence for the candidate; a
green CI run, synthetic contract test, or locally recorded digest is not itself a promotion
decision.

## Parallel product track

The kernel becomes commercially useful only through a visible business workflow. In
parallel with Phases 1–4, the product track must deliver:

- group chat, task graph, artifact, Needs You and audit views from the same event source;
- a three-to-five Agent workflow with explicit inputs, outputs and acceptance criteria;
- human review, revision and takeover paths;
- usage, latency, cost and accepted-artifact metrics;
- accessibility, localization and desktop/web deployment validation.

The first pilot is allowed to be narrow. It is not allowed to bypass the production gates
for data integrity, authority or external side effects.
