# Changelog

All notable changes are recorded here. The project follows Semantic Versioning once a
version is promoted; repository commits and passing tests alone do not constitute a
release. Promotion additionally requires the evidence defined in
`docs/production/RELEASE_GATES.md`.

## [0.1.0-local-trial.2] - 2026-08-21

This is a non-promoted, loopback-only synthetic trial checkpoint. It does not open any
production gate and does not authorize a real connector or external message.

### Fixed

- Made the source distribution inventory exact by explicitly packaging the tracked
  `tests/__init__.py` marker and binding `MANIFEST.in` into the source manifest.
- Accepted POSIX sticky writable ancestors only when every existing ancestor is owned by
  root or the effective service user, while rejecting attacker-owned intermediate paths.
- Fenced a restored temporary SQLite file by inode identity before reopening its descriptor,
  preserving the correct path-replacement failure on Linux.
- Removed a Python 3.9 fork-probe ordering dependency by collecting unrelated stale store
  cycles while the parent process still owns their resources.

### Verified

- CPython 3.9, 3.12, and 3.13 each pass all 1,109 tests; Python 3.9 has one expected
  version-capability skip.
- Ruff lint/format, strict mypy, dependency locks, compileall, shell syntax, and strict Git
  object validation pass on the checkpoint tree.
- Two clean, independent locked builds produce byte-identical wheels and normalized sdists;
  the source-bound distribution manifest, SBOM verification, CycloneDX 1.6 schema check,
  and fresh-environment wheel smoke test pass.

### Supersedes

- `v0.1.0-local-trial.1` remains immutable for audit history but is superseded because its
  first Linux CI run exposed the path-validation and sdist-inventory defects fixed here.

## [Unreleased]

### Added

- A private, capability-free stored-event envelope V1 codec with an exact 12-field canonical body,
  domain-separated SHA-256 digest, bounded canonical JSON, UTC-microsecond coordinates, and exact
  raw `sqlite3.Row` reconstruction without exporting a writer or authority API.
- Frozen stored-event envelope Golden bytes/manifest, a read-only verifier exercised on Python
  3.9/3.12/3.13, exhaustive scalar/payload/storage mutation tests, and a three-version stdlib-only
  CI job.
- Native IM V1 provider-neutral executable contract with strict bounded codecs, 21 public wire
  models, domain-separated canonical digests and stable idempotency derivation.
- An exact four-method `IMGatewayPort` plus pure request/result admission gates for capability,
  inbound page, dispatch receipt and acceptance-query receipt bindings.
- A zero-network, read-only-by-default fake IM adapter with a process-local non-serializable test
  permit, receiver action/key idempotency ledger, collision rejection, ACK-loss/post-accept
  exception reconciliation, retryable NACK evidence and retention-expired query behavior.
- Twenty-three frozen native-IM V1 golden vectors, an independent read-only oracle, exhaustive
  event/revision/scope/mention/digest contract matrices and a fresh-process socket/DNS/network
  import/environment-credential gate.
- Exact native-IM provider profiles, inbound-only config/secret-reference boundaries, raw-body
  signature/timestamp verification, migration-backed inbox tables, profile-bound durable nonce
  claims, and checkpoint-bound replayable inbound-read preparation.
- Atomic native-IM page admission that commits nonce evidence, canonical events, verification and
  read-event links, prepared-read CAS, cursor/snapshot checkpoint, and independent durable-graph
  readback in one transaction, with exact replay, ACK-loss poison/reopen reconciliation, tamper
  detection, conflicting-race rollback, and zero gateway/Agent/network/outbound side effects.
- Default-off native-IM sandbox composition, an explicitly injected inbound-only adapter, separate
  signed-provider and canonical-page byte domains, and a bounded parser that revalidates scope,
  request, capability, authentication, conversation, event and transport evidence.
- A process-bound, non-serializable native-IM lifecycle and one-way kill switch with atomic final
  admission fencing, cancellation-resumable prepared reads, retryable graceful close and stable
  outbound rejection before request or secret inspection.
- Typed body-free native-IM lifecycle/health/read/kill-switch observations, fixed no-label counters,
  end-to-end message/trace/secret/nonce/signature canary containment, and recorded zero-effect probes
  for disconnect/resume, duplicate, out-of-order and conflicting pages.
- Reusable offline native-IM provider Mapper and Transport compatibility kits with fixed redacted
  rejection contracts, exact scripted exchanges, deterministic accepted/rejected vectors, and
  fresh-process/hash-seed verifiers.
- Per-read exchange evidence that binds request ID/digest, cursor/sequence/snapshot, received time,
  provider intent and exchange-security evidence while keeping stable event-source evidence outside
  transient network identity.
- Exchange-enhanced sandbox admission provenance with legacy canonical-JSON compatibility and
  migration-v6 durable storage/readback, plus a zero-network provider-bundle test that closes signed
  wire response through HMAC verification, pure mapping and atomic SQLite admission.
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
  stream enter and every iterator resume, transaction context enter/exit, owner-aware
  transaction/migration/constructor cleanup, complete inherited-graph quarantine on mismatch or
  ordinary child GC, and one stable clean lifecycle mismatch error.
- Fork-before-initialization, spawn and forkserver fresh-connection evidence for global-position
  contention, idempotent event-plus-outbox admission, outbox lease fencing, ambiguity resolution,
  SQLite integrity, foreign keys and exact migration schema.

### Changed

- CI and canonical local release evidence now execute the full pytest suite instead of treating
  incomplete `unittest discover` collection as repository-wide evidence.
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
  paths and remain unsuppressed at inherited store/stream/transaction exit boundaries. Nested
  trusted public mismatches are re-cleaned without authorizing caller-forged lookalikes; a current
  fresh outer store rolls back and remains usable after a stale dependency fails.
- Event-store mismatch workers must stop admission and use `os._exit`/exec. Ordinary `sys.exit`
  or interpreter teardown is not a safe destruction path for a quarantined inherited native graph.
- The native-IM fresh-process zero-network verifier now covers sandbox, lifecycle and observability
  import allowlists plus socket, DNS and asyncio connection blockers, while the package API exports
  the stable integration types without registering a real network transport.

### Fixed

- Python 3.9 package imports no longer evaluate PEP 604 type aliases at runtime in native-IM
  configuration/provenance modules; the source-bound provider-bundle suite digest was refreshed
  and independently reverified.
- Stored-event JSON number decoding now bounds integer and float lexemes before Python numeric
  conversion, validates cheap row scalars before payload parsing, and keeps payload key/value
  canaries out of exception text.
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

- Atomic Result Authority has completed only M1 private envelope codec. The reserved generic-append
  fence, store-owned snapshot/raw-row transaction adapter, result migration 7, atomic writer,
  Observed recovery, Accepted mint point, and worker promotion remain disabled.
- Native IM E2 sandbox inbound-only: the offline profile/auth/durable atomic inbox, default-off
  adapter/lifecycle/observability/recorded-probe and synthetic provider-bundle TCK nodes now exist,
  but no real provider contract/profile/mapper, production exchange, endpoint, credential material,
  webhook/socket transport or approved sandbox read exists yet. Native IM external outbound remains
  unimplemented and unauthorized; production Gates A–E remain closed.
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

This baseline is not a promoted release. Annotated `v0.1.0-local-trial.*` tags may record
local trial checkpoints, but they do not authorize internet-facing, multi-tenant, or
irreversible production workloads.
