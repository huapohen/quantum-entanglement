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

The hard runtime boundary for every phase is defined in
[`SERVICE_BOUNDARY.md`](./SERVICE_BOUNDARY.md). A phase name or implemented component does
not grant permission to exceed that boundary before its promotion evidence is accepted.

## Commit and release discipline

- One independently reviewable behavior or document change per commit.
- Tests are committed with the behavior they prove.
- Schema changes include forward migration, compatibility notes, and rollback guidance.
- Every phase ends in a version bump, changelog, release checklist, and signed-off evidence.
- The default branch must remain runnable after every commit.

## Current checkpoint (`0.1.x`, not promoted for production)

The repository has moved beyond the original in-memory prototype. Committed components now
include durable invocation attempts with leasing and fencing, a persistent artifact store,
an outbox publisher, durable projection offsets/receipts, domain-scoped migrations,
SQLite backup/restore, tenant authorization primitives, and an approval flow whose request,
decision, transition, and recovery chain are transactionally validated.

They are not yet a production service. The current P0 dependency chain remains:

1. strict service configuration and secret-handle boundaries;
2. safe structured logging and redaction before any service exposure;
3. explicit production schema preflight and migration control;
4. authenticated request context plus mandatory tenant/workspace scope in every repository;
5. a versioned, authenticated loopback API with command idempotency;
6. durable action receipts and a fenced fake connector with reconciliation;
7. lifecycle/readiness/SIGTERM, complete backup coverage, a least-privilege container, and
   measured upgrade/restore evidence.

Until that chain is closed, the supported runtime remains a trusted local or isolated-CI
kernel using synthetic data and fake/no-op/read-only connectors.

## Phase 0 — validated kernel (`0.1.x`, complete as a kernel baseline)

Runnable boundary: dependency-free, single-process coordination kernel and local demo.

Delivered:

- canonical coordination envelope and append-only event store;
- deterministic DAG scheduling, context compilation, policy and approval flow;
- artifact versioning and recovery from event history;
- transactional inbox/outbox storage primitives and an outbox publisher;
- durable invocation attempts, artifact storage, projection state and approval recovery;
- domain-scoped migrations, tenant authorization primitives and SQLite backup/restore;
- A2A boundary mapping, LangGraph bridge, mention routing, and isolated Harness port;
- a deterministic three-Agent demo and an expanding deterministic test suite.

Not a production claim: there is no authenticated service API, mandatory tenant scope on
all storage, durable external-action receipt, complete lifecycle/telemetry, or deployment
package yet.

## Phase 1 — reliable single-node service (`0.2.0`)

Runnable boundary: one-node deployment suitable for controlled internal pilots with fake
or explicitly approved external connectors.

Required deliverables:

1. **Implemented component:** outbox publisher with timeout, retry, jitter and dead-letter.
2. **Implemented component:** durable task attempts with lease, heartbeat, fencing and retry.
3. **Implemented component:** persistent artifact metadata/blob integrity verification.
4. **Implemented component:** projector offsets/receipts and replay safety.
5. **Open gate:** durable external action receipts, fencing and reconciliation state machine.
6. **Partial gate:** versioned SQLite migrations and backup/restore exist; complete manifest
   coverage, restore quarantine and release rehearsal remain required.
7. **Open gate:** service lifecycle, readiness/liveness, graceful admission drain and safe
   structured audit logs.

“Implemented component” is source evidence only. Phase 1 is not promoted until every open
and partial gate passes the release criteria below from a clean supported environment.

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
