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

Only factories compiled into this binary are supported in this stage. This boundary does not load arbitrary Go
plugins, does not permit plugin manifests to self-attest trust, and does not yet perform live hot reload. The
next configuration-composition stage will promote a candidate only after its complete plan is ready, leaving
the last-known-good digest unchanged on failure.

## Offline verification after dependencies are cached

```bash
GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off GOFLAGS=-mod=readonly \
  go test ./apps/im-api/...
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
