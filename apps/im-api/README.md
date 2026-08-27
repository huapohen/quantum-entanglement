# WanWork IM API

Go/Fiber backend for the native WanWork IM. This module is intentionally isolated from the existing Python
package and starts with no Clerk, RongCloud, model, tool, database, or outbound network adapter.

## Run the current scaffold

```bash
GOTOOLCHAIN=local go run ./apps/im-api/cmd/im-api
```

The default listener is loopback-only at `127.0.0.1:18080`. Override it only for an explicitly reviewed local
environment:

```bash
WANWORK_IM_LISTEN_ADDRESS=127.0.0.1:19080 \
  GOTOOLCHAIN=local go run ./apps/im-api/cmd/im-api
```

The current immutable configuration reads only `WANWORK_IM_LISTEN_ADDRESS`, accepts only numeric loopback
hosts, fixes auth/IM providers to their fake implementations, and fixes outbound to `disabled`. Clerk,
RongCloud, model, endpoint, and credential environment variables are intentionally not read in this stage.

Current endpoint:

```text
GET /health/live -> HTTP 200 {"status":"ok"}
GET /api/v1/system/ping -> HTTP 200 business envelope
```

Health endpoints use normal HTTP status semantics. Business APIs will use the versioned HTTP 200 envelope
defined in `docs/wanwork_im/ARCHITECTURE.md`; a health response never proves provider delivery, Agent
completion, Artifact acceptance, or Task closure.

Every reachable business endpoint returns a stable `code/data/message/requestId` envelope. Authentication,
authorization, validation, conflict, rate-limit, and dependency-before-effect failures use HTTP 200 with a
non-success business code. Unknown errors, panics, and JSON encoding failures collapse to `50001` without
serializing their causes. Provider `effect_unknown` will be a successful command response containing an honest
Action status; it is not misreported as `50301` and never retried blindly.

## Plugin admission, dependency plan, and reversible lifecycle

`internal/plugins` freezes the first DeepSeek Harness-inspired boundary without allowing arbitrary dynamic Go
code. A plugin supplies a manifest; the host separately owns the package digest, provenance, SBOM, approval,
and revocation record. Registration fails if those records do not match. Required ports resolve to exactly one
provider (or an explicitly pinned provider), and the resulting bindings and topological order are deterministic
regardless of discovery or map iteration order. Missing, ambiguous, invalid, duplicate, self-dependent, and
cyclic compositions fail before any plugin can start.

The host configures every plugin without side effects, then starts and probes readiness in deterministic
topological order. A plugin must register every acquired listener, timer, lease, handle, or route with its
host-owned effect scope. A start/readiness failure, explicit stop, or cleanup failure triggers best-effort
drain, stop, and effect cleanup in reverse dependency/registration order. Cleanup continues after individual
failures and returns their joined error; repeated stop is idempotent. Lifecycle calls have manifest-owned
deadlines and plugins must honor the supplied cancellation context.

Effect scopes transition `open -> closing -> closed`. Shutdown closes every scope before the first Drain call,
so Drain, Stop, concurrent cleanup, and cleanup callbacks cannot register effects outside the cleanup snapshot.
Failed callbacks remain in the closed-for-registration scope and are the only callbacks retried by a later Stop.

Only factories compiled into this binary are supported in this stage. This boundary does not load arbitrary Go
plugins, does not permit plugin manifests to self-attest trust, and does not yet perform live hot reload.

## Effective configuration composition

The pure composition boundary applies exactly one profile, ordered bundles, and an optional tenant-bound
overlay. A later layer can replace an earlier row only by repeating the complete row; deletion requires an
explicit tombstone. Home, CLI, prompt, and ambient-environment patches are not accepted. Layer IDs, row IDs,
tenant scope, admitted plugin version, artifact digest, and host-owned configuration schema are all validated
before dependency resolution. Registering a package does not activate it: only the final selected rows enter the
dependency plan. Registration is a builder phase: `Freeze` revalidates and clones the complete schema, broker,
manifest, and package graph, then permanently closes all registration paths. Resolve, composition, secret claim
admission/revocation, and host construction reject an unfrozen registry.

The result is an immutable snapshot containing source revisions/digests, fully materialized public
configuration, capabilities, egress declarations, non-bearer secret binding views, provider bindings,
manifest/admission trust bindings, canonical bytes, and a domain-separated SHA-256 digest. Raw secret locators
must first pass a registered host broker and are replaced by claim digest/revision before composition. Compose
revalidates tenant, row, plugin, artifact, manifest, admission, schema, logical name, broker, purpose, audience,
policy, and revocation. Raw locators are absent from canonical bytes, getters, diffs, factories, and activation.
Getters return deep copies. Checked-in golden vectors freeze manifest, schema, broker, claim, and effective-v3
encoding. Candidate diffing is row- and plugin-scoped and reports configuration, binding, capability, egress,
secret-binding fingerprint, artifact, schema, manifest, and admission changes. Configuration cannot self-approve
its own expansion.

`SecretBindingView` is identity/audit metadata, not a bearer capability, and this stage has no secret material
resolver. Action-time JIT leases/token exchange, trusted credential-bearing executors, provider receipts,
durable last-known-good promotion, and live reload remain later stages. `NewHost` already revalidates the current
package/manifest/admission/revocation and claim records, then freezes only the selected factories, configs, and
timeouts for deterministic activation.

## Third-party execution isolation contract

`internal/isolation` is a data and IPC contract for a future separately deployed privileged supervisor. It is
not another Plugin Host and does not execute third-party code in this API process. `LaunchCommand` carries only
host-owned versioned package/profile/grant references, request identity, a previous-generation CAS expectation,
an input manifest digest, and a deadline. It cannot carry raw argv, shell commands, ambient environment values,
host paths, mounts, runtime sockets, secrets, process handles, or callbacks.

The supervisor owns `ProcessInstance` generation and fence advancement. Termination evidence is deliberately
split into cancel, kill-tree, exact exit, descendant reap, and runtime-resource release receipts. A kill receipt
proves neither process exit nor external-effect finality. Any effectful execution remains
`dispatched_unknown/reconcileRequired` after process release until the Action Plane obtains provider receipt or
readback evidence.

`internal/isolation/fake` is deterministic but explicitly reports `durability=volatile`, `isolation=none`, and
`executesCode=false`. It tests idempotency, generation CAS, stale fencing, receipt validation, and quarantine;
it is not a process, container, or microVM sandbox. Real supervisor IPC, durable operation state, signed
receipts, process/container/microVM backends, and OS-level hostile conformance remain W4/W7 gates.

## Volatile event-store contract fake

`internal/events` defines the `EventStore` port and `VolatileMemoryStore`. The fake atomically appends one exact
tenant/workspace/stream batch, owns sequence/global position/recorded time, checks expected revision, returns
the original stored facts for an exact retry, and rejects identity or request drift. Stream and global pages use
opaque cursors bound to a caller-provided deterministic namespace, query kind, exact tenant/workspace scope,
stream, and position.
Input, append/replay results, and pages are detached snapshots.

The port exposes `StoreCharacteristics`, so `ValidateStoreRequirements` rejects empty, unknown,
contradictory, typed-nil, durable restart-persistence, or tamper-evidence admissions for this fake. The current
application has no EventStore production composition yet; every future production factory must invoke the
guard and pass provider-specific crash/restore conformance. Action receipts deliberately remain a separate
Action Plane port and are not an EventStore characteristic. The fake's actual guarantees are limited:

```text
durability              = volatile
persistsAcrossRestart   = false
tamperEvident           = false
```

The cursor digest is a strict fake checksum, not a signature, MAC, authentication mechanism, authorization
grant, or public bearer token. Base64 fields are not confidential, and reusing the same namespace with the same
rebuilt events intentionally reproduces cursor values; supply a new namespace when old cursors must fail.
Deterministic fixture backfill is not deterministic Agent/model/tool execution,
SSE live replay, or external-effect recovery. PostgreSQL transactions, projection checkpoints,
crash/reopen/kill-9, backup/restore, retention/encryption, and tamper-evident evidence remain W2/W7 gates. Do
not use `VolatileMemoryStore` as a production event source of truth.

## Offline verification after dependencies are cached

```bash
GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off GOFLAGS=-mod=readonly \
  go test ./apps/im-api/...
GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off GOFLAGS=-mod=readonly \
  go test -race ./apps/im-api/internal/isolation/...
GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off GOFLAGS=-mod=readonly \
  go test -race ./apps/im-api/internal/events/...
GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off GOFLAGS=-mod=readonly \
  go vet ./apps/im-api/...
GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off \
  go -C apps/im-api mod verify
GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off \
  go -C apps/im-api mod tidy -diff
```

The module pins Fiber v3.5.0 and Go 1.25.0 syntax; `.go-version` records the locally validated Go 1.25.6
toolchain. `go.mod` and `go.sum` are committed supply-chain inputs. No dependency version is an independent
security certification; later production gates add vulnerability, license, provenance, SBOM and upgrade review.
