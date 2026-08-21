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

### Fixed

- Process-inherited request-context issuers, protected-operation composers, and operation
  registries now fail closed on creator PID/process-epoch mismatch before inherited locks
  or authorization dependencies; a forked child cannot adopt an issuer by constructing a
  fresh composer, while the parent remains usable.
- Public request-context and protected-operation boundary failures now detach completed
  internal traceback locals and explicitly clear any active caller exception context,
  including real context-manager body failures, before a code-only error escapes.
- Protected-operation composer and registry construction now contain provider/clock
  descriptor failures inside the same hostile-dependency boundary, delete constructor
  inputs before public rethrow, and reissue only bounded clean control signals.
- Protected-operation constructors now route descriptor-raised `AttributeError` through
  frame cleanup, bind exact initializers without instance lookup, reject subclasses, and
  publish one fully assembled internal state through a single slot write; interrupted
  construction removes that slot instead of exposing partial dependencies.
- Protected-operation registry and composer wrappers now bind base-class callbacks without
  hostile instance lookup, and `with composer:` preserves a genuine exact originating
  control signal over every cleanup outcome while rejecting direct-argument spoofing and
  ignoring cleanup return values.
- Protected-operation context exits now require a process/thread-bound one-time descriptor
  lease before the current exception triple can receive originating-control precedence;
  manual exits and stale callbacks cannot claim it, while nested/concurrent entry and
  inherited fork leases fail closed.
- Context-exit leases now strongly retain and identity-compare a library-owned per-thread
  opaque token instead of trusting recyclable integer identifiers or runtime-managed
  `Thread`/`_DummyThread` wrappers. A successor cannot bind, activate, consume, reconcile,
  finalize, or discard an orphaned lease after identifier or wrapper reuse; explicit
  composer close remains the separate fail-closed handle-retirement path when an owner
  terminates before exit.
- Context exits now retain a consumed lease commit across an interrupted helper-return
  window, reconcile exact process/thread/token state with an at-most-two-attempt budget,
  and run cleanup plus finalization structurally at most once per exit path. One
  reconciliation interruption preserves a genuine originating control signal; repeated
  interruption exhausts deterministically, while wrong-thread, replayed, and forked
  callbacks cannot take over the owner's cleanup.
- Exact operation control signals are reissued without dependency exception state;
  `SystemExit` preserves only `None`, exact booleans, or exact integer status 0 through 255
  and maps every other status to `1`.

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
