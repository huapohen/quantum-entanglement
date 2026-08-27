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

Current endpoint:

```text
GET /health/live -> HTTP 200 {"status":"ok"}
```

Health endpoints use normal HTTP status semantics. Business APIs will use the versioned HTTP 200 envelope
defined in `docs/wanwork_im/ARCHITECTURE.md`; a health response never proves provider delivery, Agent
completion, Artifact acceptance, or Task closure.

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
