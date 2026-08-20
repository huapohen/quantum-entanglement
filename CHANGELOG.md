# Changelog

All notable changes are recorded here. The project follows Semantic Versioning once a
version is promoted; repository commits and passing tests alone do not constitute a
release. Promotion additionally requires the evidence defined in
`docs/production/RELEASE_GATES.md`.

## [Unreleased]

### Added

- Bridge-only domain migration foundation: trusted legacy descriptors, exact sidecar
  install/bootstrap, immutable `SchemaState`, and a digest-bound closed-action planner.
- Commercial production roadmap from `0.2.0` through `1.0.0`.
- Per-commit and per-release gates plus an immutable release-evidence template.
- Ten-dimension production readiness audit and explicit P0/P1/P2 backlog.
- Security policy and production threat model with adversarial verification plan.
- GitHub Actions verification on Python 3.9 and 3.12.
- Weekly Python and GitHub Actions dependency update monitoring.
- Lease-fenced async outbox Publisher with bounded Connector admission, durable ambiguity
  reconciliation, and operator runbook.
- Checksum-verified outbox ambiguity migration with populated legacy-table rebuild and
  destructive rollback rehearsal.

### Changed

- README now distinguishes the runnable `0.1.x` experiment from production guarantees and
  identifies unimplemented ACP, MCP, authentication, and crash-recovery boundaries.
- Outbox lease time is read only after SQLite write-lock acquisition, terminal fencing
  tokens are cleared, and persistent ambiguity history stores token digests only.
- Durable invocation enqueue, claim/recovery, heartbeat, completion, and failure now reconcile
  lost SQLite commit acknowledgements against exact durable state. Uncertain rollback poisons
  and closes the store instance; later access and close failures use stable typed, sanitized
  errors, and owned mutations validate the complete lease/job/attempt binding. Exact process and
  cancellation controls are reissued from clean public boundaries, unsafe `SystemExit` values are
  reduced to a fixed code, confirmed rollback uses `InvocationTransactionError`, and hostile,
  subclassed, grouped, or forged exceptions cannot carry provider state across those boundaries.
- Invocation stores bind to their creator PID and reject every public read, write, recovery,
  context and close operation after POSIX-fork inheritance, before touching the copied lock or
  SQLite connection. Multi-process workers must construct one store per child process.

### In progress — not yet a shipped guarantee

- Reliable outbox publishing with bounded retry, hard callback deadlines, fencing, and
  graceful shutdown.
- Durable invocation attempt leasing, heartbeat, recovery, and terminal compare-and-set.
- Verified capability and multi-tenant authorization boundaries.

## Pre-release kernel baseline (`0.1.x`, not promoted)

- Canonical coordination envelope and append-only SQLite event store.
- Deterministic task graph, context compilation, policy/approval flow, and artifact model.
- Transactional inbox/outbox primitives and event-history state reconstruction.
- A2A data mapping, LangGraph bridge, mention routing, and isolated Harness runtime port.
- Dependency-free three-Agent demo and 54 deterministic baseline tests.

There is intentionally no version tag for this baseline. It is not supported for
internet-facing, multi-tenant, or irreversible production workloads.
