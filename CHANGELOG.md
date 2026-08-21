# Changelog

All notable changes are recorded here. The project follows Semantic Versioning once a
version is promoted; repository commits and passing tests alone do not constitute a
release. Promotion additionally requires the evidence defined in
`docs/production/RELEASE_GATES.md`.

## [Unreleased]

### Added

- Bridge-only domain migration foundation: trusted legacy descriptors, exact sidecar
  install/bootstrap, immutable `SchemaState`, and a digest-bound closed-action planner.
- Atomic bridge-plan application with locked source-state revalidation, an allowlisted
  two-action executor, rollback verification, and committed-state readback.
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
- Lock-free PID plus opaque-epoch process-owner foundation with at-fork rotation, independent
  PID-drift fallback, non-serializable owner descriptors, nested-fork/parent-continuity tests,
  and fresh spawn/forkserver construction evidence.
- Inert exact SQLite backup-topology registry binding eight current component profiles,
  58 catalog objects, migration descriptors, canonical DDL digests, and acyclic dependencies.
- Inactive exact SQLite backup manifest v2 model and bounded canonical JSON codec binding
  current bridge-only `SchemaState`, topology profiles, all catalog objects, and table counts
  while remaining unreachable from v1 create, verify, restore, and CLI paths.
- Inactive backup manifest v2 evidence derivation from one owned read transaction on a
  caller-supplied exact SQLite connection, including integrity, foreign-key, page-geometry,
  bridge-state, exact catalog, and per-table-count evidence without activating a writer,
  verifier, restore path, or CLI.
- Process-bound `SQLiteEventStore` lifecycle with all ordinary read/write/close/context paths,
  stream enter and every iterator resume, owner-aware transaction/migration/constructor cleanup,
  child connection quarantine, and one stable clean lifecycle mismatch error.
- Fork-before-initialization, spawn and forkserver fresh-connection evidence for global-position
  contention, idempotent event-plus-outbox admission, outbox lease fencing, ambiguity resolution,
  SQLite integrity, foreign keys and exact migration schema.

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
  subclassed, grouped, grafted, or forged exceptions cannot carry provider state across those
  boundaries. Validation provenance covers every Python traceback frame and uses a nonce-bound
  descriptor that is reissued only after leaving the caught exception graph. Transaction-state
  inspection accepts exact booleans only and quarantines all other values without truth-testing
  them.
- Invocation stores bind to their creator PID and reject every public read, write, recovery,
  context and close operation after POSIX-fork inheritance, before touching the copied lock or
  SQLite connection. Multi-process workers must construct one store per child process.
- Test discovery is isolated from third-party packages named `tests`, and backup fixtures close
  their SQLite connections so strict `ResourceWarning` release gates stay deterministic.
- Backup topology DDL canonicalization now follows SQLite's exact ASCII token-whitespace
  boundary and preserves comments, NBSP, vertical tab, quoted regions, and other semantic
  token content to prevent cross-schema digest collisions.
- Backup v2 remains reachable only through explicit versioned submodule imports, preserving
  zero packaged-migration SQL reads during a cold package-root import while retaining
  migration/topology cross-binding during explicit v2 initialization.
- Inactive backup-v2 snapshot derivation now establishes nested conservative cleanup before
  `BEGIN`, retries one interrupted cleanup, verifies the final transaction state, preserves
  exact `KeyboardInterrupt`, `SystemExit`, `GeneratorExit`, and `CancelledError` identity and
  traceback, and rejects ambient handled controls as originating-control evidence.
- Event-store caller values and iterables are copied outside SQLite locks into exact built-in
  event/message/JSON/cursor/timestamp/lease snapshots, so caller `__conform__` adapters cannot run
  inside DB-API binding. Live store, transaction, stream context and iterator objects reject
  copy, deepcopy and serialization.
- Event-store clock and migration callbacks are guarded before and after execution; fork child
  cleanup never inspects, rolls back, commits, closes or unlocks inherited SQLite state. Exact
  originating process controls take precedence over cleanup controls in current-process failure
  paths.

### In progress — not yet a shipped guarantee

- Reliable outbox publishing with bounded retry, hard callback deadlines, fencing, and
  graceful shutdown.
- Durable invocation attempt leasing, heartbeat, recovery, and terminal compare-and-set.
- Verified capability and multi-tenant authorization boundaries.
- Remaining per-component process-owner migration for artifact/projection/revocation and other
  stores, authorization, secrets, plugins, runtimes, connectors, and the final worker composition
  root. The shared foundation plus event-store candidate is not a system-wide fork-safety or
  secret-isolation guarantee.
- Backup manifest v2 writer/publication, quarantine verification, exact-byte restore,
  mixed-version rehearsal, and authenticated custody; the topology registry, codec, and
  caller-connection snapshot derivation do not make v2 operationally readable or writable.

## Pre-release kernel baseline (`0.1.x`, not promoted)

- Canonical coordination envelope and append-only SQLite event store.
- Deterministic task graph, context compilation, policy/approval flow, and artifact model.
- Transactional inbox/outbox primitives and event-history state reconstruction.
- A2A data mapping, LangGraph bridge, mention routing, and isolated Harness runtime port.
- Dependency-free three-Agent demo and 54 deterministic baseline tests.

There is intentionally no version tag for this baseline. It is not supported for
internet-facing, multi-tenant, or irreversible production workloads.
