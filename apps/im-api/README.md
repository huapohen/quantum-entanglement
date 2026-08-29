# WanWork IM API

Go/Fiber backend for the native WanWork IM. This module is intentionally isolated from the existing Python
package. The default service now includes deterministic, zero-network Clerk-shaped and RongCloud-shaped fake
adapters plus an `@Agent` child-group acceptance slice. An explicit runtime mode composes a strictly admitted
PostgreSQL URL, an attested runtime-only pool, exact readiness, a controlled Unit of Work, and an API route
barrier. Production IaC/cutover/credential rotation, real Clerk/RongCloud network adapters, model, tool, and
production outbound reconciliation are still not delivered.

## Run the local IM acceptance surface

From the repository root:

```bash
./scripts/start_im_demo.sh
```

Then open `http://127.0.0.1:18080/demo/im`. The default listener is loopback-only. To select a different local
port:

```bash
./scripts/start_im_demo.sh --port 19080
```

The underlying API script refuses to start if PostgreSQL runtime variables are already present unless
`WANWORK_IM_ALLOW_RUNTIME_COMPOSITION=1` is set explicitly. In default mode the immutable configuration accepts
only a numeric loopback listener. The local acceptance composition instantiates in-memory fake adapters with
synthetic fixtures; they make no network calls and contain no production credentials. Its fake outbound path is
enabled only inside this process so the child-group reply flow can be observed.

Verify the default mode:

```bash
curl --fail http://127.0.0.1:18080/health/live
curl --fail http://127.0.0.1:18080/api/v1/system/ping
curl --fail http://127.0.0.1:18080/api/v1/demo/im
```

Expected responses:

```text
{"status":"ok"}
{"code":200,"data":{"status":"ok"},"message":"ok","requestId":"req_..."}
```

See [`docs/wanwork_im/LOCAL_IM_ACCEPTANCE_GUIDE.md`](../../docs/wanwork_im/LOCAL_IM_ACCEPTANCE_GUIDE.md) for the
browser flow, arbitrary instruction API, expected invariants, architecture diagrams, and exact production
boundary.

## Explicit PostgreSQL runtime mode

Use only a disposable local database or an explicitly reviewed environment. Never put a real URL, password,
or manifest into a tracked script, shell history, report, screenshot, Git commit, or Notion page.

```bash
export WANWORK_IM_POSTGRES_RUNTIME_URL='postgresql://<runtime-login>:<secret>@127.0.0.1:55488/wanwork_im?sslmode=disable'
export WANWORK_IM_POSTGRES_AUTHORITY_MANIFEST='{
  "databaseName":"wanwork_im",
  "databaseOwnerRole":"wanwork_im_provisioner",
  "ownerRole":"wanwork_im_owner",
  "migratorRole":"wanwork_im_migrator",
  "runtimeRole":"wanwork_im_runtime",
  "migrationLoginRoles":["<migration-login>"],
  "runtimeLoginRoles":["<runtime-login>"]
}'
export WANWORK_IM_POSTGRES_ALLOW_INSECURE_LOCAL_TEST=true
export WANWORK_IM_ALLOW_RUNTIME_COMPOSITION=1
./scripts/start_im_api.sh
```

Remote connections do not accept the insecure-local exception and must pass authenticated TLS policy. The
strict connection policy requires an explicit remote password; rejects implicit endpoint/identity fields,
`sslmode=require/prefer`, multi-host or fallback endpoints, raw service/pass files, session parameters, and
pgx/pgxpool query/cache/lifecycle overrides; and rejects presence of every pgx-recognized `PG*` environment
variable plus `SSL_CERT_FILE/SSL_CERT_DIR`, including empty values. It rejects malformed raw query pairs instead
of silently dropping them. Its canonical parse suppresses default `.pgpass`/client-certificate adoption,
compares the final host/port/database/login/password exactly, and binds the configured timeout to `DialFunc`.
Without explicit `sslrootcert`, remote TLS uses the reviewed host OS trust store; that store is host TCB, not an
application-attested exact CA digest, and still requires a production remote-TLS E2E before deployment.

Runtime endpoints:

```text
GET /health/live  -> process liveness only
GET /health/ready -> HTTP 200 exact database ready; HTTP 503 otherwise
GET /api/v1/*     -> readiness failure stays HTTP 200 envelope with code 50301
```

### One-shot migrator

The API reads only the runtime URL and refuses to start if the migration variable is present, even when empty.
Schema migration uses a different process and variable:

```bash
export WANWORK_IM_POSTGRES_MIGRATION_URL='postgresql://<migration-login>:<secret>@127.0.0.1:55488/wanwork_im?sslmode=disable'
GOTOOLCHAIN=local go run ./apps/im-api/cmd/im-migrate
```

`im-migrate` verifies the migration login/database, selects the exact owner role, applies the checksummed
catalog, and finally validates the exact authority manifest. On a first deployment it can apply schema and then
fail the final validator until the separate DBA ownership/grant cutover is complete; rerun after cutover. This
command is not yet a complete production bootstrap or IaC replacement.

## Current PostgreSQL authority subset

At code baseline `53dd38b`, `internal/platform/postgres` contains checksummed migrations `0001..0005`, 22
authority tables, 17 FORCE RLS tables, tenant-bound repositories/UoW, and five fixed `SECURITY DEFINER` write
functions. Conversation, provider-binding, membership, access, and command-receipt writes go through those
functions. The tested `NOINHERIT` runtime login can explicitly `SET ROLE` only to its exact runtime group; that
runtime role has only the required reads and function executions and is denied raw table mutation, `MAINTAIN`,
schema/object creation, elevated role settings, and unlisted routines.

The exact validator, runtime pool, startup/readiness route barrier, controlled Unit of Work, and one-shot
migration process are now real code paths. The role provision helper remains a test fixture, not production IaC;
first-deploy ownership/grant cutover, credential rotation/old-session drain, restore/crash exercises, trusted
Clerk tenant context, active authority resolution, PostgreSQL event/outbox/projection checkpoints, and provider
reconciliation remain unimplemented. See `docs/wanwork_im/W2_POSTGRES_RUNTIME_CHECKPOINT.md` and
`analysis_report/research/35_postgres_attested_runtime_composition_checkpoint.md` for the exact boundary.

The exported `runtimepool.Pool.Acquire` is a trusted low-level escape hatch: it returns a session-guarded
connection but still permits SQL within the runtime database role. The underlying `*pgxpool.Pool` is not
exposed and production `UnitOfWork` construction does not accept an arbitrary raw pool, but neither fact makes
`Acquire` a tenant or action-time authorization boundary.

Current endpoint:

```text
GET /health/live -> HTTP 200 {"status":"ok"}
GET /health/ready -> runtime mode only; HTTP 200 or 503
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

## IM identity, conversation, and provider metadata contracts

`internal/im` now freezes syntax-level identity and conversation values. Stable tenant-scoped `ActorRef` and
`ConversationRef` values are separate from revision-bearing snapshots. External Clerk/RongCloud subjects are
scoped by a non-secret provider realm so the same subject in different apps/environments cannot collapse into
one mapping. Actor ID prefixes bind to `human | agent | system | service`, but prefix agreement is not proof of
registry existence, installation, membership, provider authentication, or authorization.

Conversation snapshots allow `direct | group | agent_thread`. Direct/group values forbid thread topology;
Agent threads require parent conversation, root message, and invocation together and reject self-parenting.
This lineage is not Task state, membership, ACL inheritance, or proof that a provider group exists.

`internal/immetadata` owns the RongCloud `ext_info` projection boundary. It accepts exactly four bounded flat
V1 shapes: human user, Agent user, ordinary group, and Agent thread. The 1024-byte decoder requires valid
UTF-8/NFC, exact types, lexicographically ordered allowlisted keys, and byte-for-byte canonical re-encoding.
It rejects duplicate/escaped duplicate/unknown/missing/null fields, trailing values, noncanonical whitespace or
escapes, Unicode ambiguity, oversized input, and authority/secret/content/evidence fields. Metadata values are
display/reconciliation hints with zero authority; inbound adapters must still verify the callback and resolve
provider realm/ID bindings, platform membership, Actor/Conversation status, Agent installation, and policy.

Current tests include four golden vectors, all 852 key permutations (four canonical, 848 rejected), 44 classes
of forbidden fields, Unicode/control/invalid-UTF-8 corpora, seeded fuzz properties, and a 128-goroutine race
fixture. They prove the local codec contract only. They do not prove RongCloud's actual size limit, byte
preservation, callback authenticity, stable readback, dedupe/resume semantics, or sandbox compatibility; those
remain W3 provider gates and real outbound stays disabled.

```bash
GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off GOFLAGS=-mod=readonly \
  go test -race ./apps/im-api/internal/im/... ./apps/im-api/internal/immetadata/... -count=1
```

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
