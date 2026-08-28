# Native IM P0 executable contract

> Status: E1 / Level A `CONTRACT_EXECUTABLE` complete
>
> Evidence source commit: `7620200f8e378507b1f592d6d34744080250d2ea`
>
> Date: 2026-08-28 (Asia/Shanghai)
>
> Safety boundary: provider-neutral models and a zero-network fake only. This document does not
> authorize a sandbox endpoint, credential, webhook, socket connection, external IM read or send.

> Historical checkpoint notice: statements below about E2 being unstarted describe source commit
> `7620200`. E2's current offline-only progress is recorded in evidence 23–25, ending with
> [`25_native_im_e2_adapter_lifecycle_offline_evidence.md`](../../analysis_report/research/25_native_im_e2_adapter_lifecycle_offline_evidence.md).
> No real sandbox network or outbound has been enabled.

## 1. Decision and exact meaning

`IM-P0 CONTRACT_READY` is complete only as the provider-neutral contract/fake milestone. The
frozen V1 wire contract is executable: values can be decoded and encoded strictly, digests and
idempotency identities can be independently reproduced, a provider adapter has one exact port,
and receiver failure semantics can be exercised deterministically without network access.

This milestone deliberately does **not** mean that a native IM has been integrated. At this E1
evidence commit, E2 / Level B `SANDBOX_INBOUND` had not started. There was no real provider adapter,
endpoint, credential, authenticator, webhook receiver, stream client, socket connection, durable
IM inbox, or external IM send path in that candidate. Production Gates A–E remain closed.

The frozen specification remains
[`docs/architecture/NATIVE_IM_CONTRACT_V1.md`](../architecture/NATIVE_IM_CONTRACT_V1.md). This
document records the executable implementation and evidence; it does not amend the V1 wire
contract.

## 2. Delivered implementation

| Boundary | Implementation | Guarantee at this milestone |
|---|---|---|
| Scalar and canonical codec | `src/quantum_entanglement/_native_im_codec.py` | Exact plain values, bounded strict UTF-8 JSON, duplicate-key rejection, NFC/control rules, canonical JSON and domain-separated digest primitives |
| V1 wire values | `src/quantum_entanglement/native_im.py` | 21 public wire model classes plus independent idempotency derivation; exact `schemaVersion=1`, unknown/missing/type/state rejection and binding validation |
| Provider-neutral port | `src/quantum_entanglement/native_im_gateway.py` | Exactly four async operations and four pure result-admission helpers |
| Zero-network fake | `src/quantum_entanglement/native_im_fake.py` | Deterministic capability/read behavior, default-denied outbound, fake receiver ledger and scripted dispatch/query failures |
| Frozen vectors | `tests/fixtures/native_im/v1/` | 23 positive model JSON files plus one manifest with canonical-byte and digest expectations |
| Independent golden verifier | `scripts/verify_native_im_v1_golden.py` | Read-only validation of manifest, file inventory, canonical JSON, model digest and idempotency key |
| Zero-network verifier | `scripts/verify_native_im_zero_network.py` | Fresh-process import/runtime gate that blocks network imports and socket/DNS functions and detects credential-environment access |
| Contract tests | seven `tests/test_native_im_*.py` files | 271 collected cases, including parameterized event/revision/scope/mention/digest and receipt-state matrices |

The 23 golden vectors are a representative positive model inventory. They do not independently
cover every union arm or every invalid state. Exhaustive event-type, message-revision,
cross-scope, mention, digest and receipt-state combinations are covered by parameterized contract
tests; negative decoder, limit and tamper behavior is also test-owned rather than inferred from
the positive fixture set.

## 3. Port contract

`IMGatewayPort` exposes exactly these operations:

```python
async def capability_snapshot(request: IMCapabilityRequestV1) -> IMCapabilitySnapshotV1
async def read_inbound(request: IMInboundReadRequestV1) -> IMInboundPageV1
async def dispatch(request: IMDispatchRequestV1) -> IMActionReceiptV1
async def query_acceptance(query: IMAcceptanceQueryV1) -> IMActionReceiptV1
```

The protocol does not accept an endpoint, credential, arbitrary payload or provider SDK object.
Adapter output is untrusted until admitted by the matching pure helper:

- capability results must match the exact requested tenant/workspace/provider/channel scope;
- inbound pages must match both the read-request digest and the trusted capability revision and
  digest;
- dispatch results must match the dispatch request and may use only dispatch-time states;
- acceptance-query results must match the original request, query and capability, including the
  declared lookup and static negative-finality rules.

Exact model classes are required; subclass or duck-typed values do not cross this boundary.

## 4. Fake adapter safety and receiver semantics

### 4.1 Default behavior

Ordinary `FakeIMAdapter(...)` construction supports deterministic capability snapshots and
cursor/snapshot-bound inbound paging. Both outbound methods reject with
`FakeIMOutboundDisabledError` before inspecting or validating request content. Test tenant,
workspace and channel identifiers must use the reserved `test-` prefix, and the provider is fixed
to `qe.fake-im.v1`.

There is no config or environment switch that enables outbound. The only enabling object is an
exact `FakeIMTestOutboundPermit` supplied through `FakeIMAdapter.for_test(...)`. The permit:

- is bound to the creating process ID and a module-private identity sentinel;
- is immutable and deliberately non-serializable;
- cannot be reconstructed from a string, environment variable, JSON or production config;
- becomes invalid after process inheritance or PID mismatch.

This permit authorizes only an in-memory fake effect. It is not an external-send authority.

### 4.2 Idempotency and collision behavior

The fake receiver keeps two mutually consistent indexes: full scope plus `actionId`, and full scope
plus receiver idempotency key. A repeated matching command returns the single accepted effect.
Reuse of either identity with a different action, intent digest or key fails with
`FakeIMReceiverCollisionError`; it never creates a second effect. `accepted_effect_count` exposes
the fake ledger cardinality for tests.

### 4.3 Dispatch and reconcile matrix

`FakeIMFaultScript` is a finite immutable sequence with a hard 1,000-step limit. It covers:

| Injected observation | Dispatch result | Receiver effect | Required next behavior |
|---|---|---:|---|
| accepted + ACK | `succeeded` | one | validate receipt; no redispatch |
| accepted + ACK loss | `effect_unknown` | one | query acceptance |
| exception after accept | exception at effect boundary | one | create local unknown observation, then query |
| outcome unavailable | `effect_unknown` | zero or previously accepted | query; never guess or blind retry |
| terminal not accepted | `rejected` | zero | terminal handling |
| temporary/rate-limit not accepted | `retryable_not_accepted` with evidence and bounded retry metadata | zero | only a higher durable dispatcher may decide a bounded retry |

Acceptance queries return receiver-ledger truth when available. An accepted effect reconciles to
`reconciled_succeeded`. Absence can become `reconciled_rejected` only when the capability declares
authoritative terminal negative evidence for the exact lookup mode. A not-final observation,
unavailable negative-finality mode or expired retention remains `effect_unknown`.

The fake does not implement the durable Action Plane, scheduler, retry budget, DLQ or operator UI.
Its retryable NACK is evidence for those later layers, not permission for the fake to redispatch.

## 5. Zero-network gate

The dedicated verifier performs both static and runtime checks:

1. direct imports of the fake must exactly equal the standard-library allowlist;
2. network client imports and network-configuration vocabulary are rejected;
3. direct environment credential access is rejected;
4. common credential variables receive a canary value;
5. `socket.socket`, `create_connection`, DNS lookup functions and selected network imports are
   replaced with fail-closed blockers before package import and exercise;
6. capability, inbound paging, default outbound denial, permitted fake dispatch and acceptance
   query execute using the real `quantum_entanglement` package import path;
7. rendered runtime values are checked for credential-canary escape.

The gate proves the exercised P0 import and fake path is zero-network. It is not a universal OS
sandbox and does not authorize later provider code to inherit this claim. At current E2 source
`2bdaea1`, the same verifier also covers direct-import allowlists and runtime blockers for sandbox,
lifecycle and observability modules. A future provider-specific transport still requires its own
review and approval record.

## 6. Verification evidence

### 6.1 Source-bound evidence at `7620200`

The following observations were made on the clean source candidate:

| Evidence | Result |
|---|---|
| Full pytest, Python 3.13 | 1,775 passed |
| Full pytest, Python 3.12.12 | 1,775 passed |
| Full pytest, Python 3.9.6 | passed with one existing platform-capability skip |
| Locked Ruff lint and format | passed across 152 files |
| Strict mypy | passed across 49 source files |
| Native IM focused suite | 271 tests collected and passed |
| Golden verifier | 23 vectors passed |
| Zero-network verifier, Python 3.9.6 | passed |
| Zero-network verifier, Python 3.12.12 | passed |
| Canonical local release evidence | 5/5 gates passed; clean and source-stable |

Canonical evidence recorded:

- commit before/after gates:
  `7620200f8e378507b1f592d6d34744080250d2ea`;
- tree before/after gates: `b1c9b4ed103d6b9327551bce88ee16f61b21dfb2`;
- `dirtyBeforeGates=false`, `dirtyAfterGates=false`, `identityStable=true`;
- `gateCount=5`, `passedCount=5`, `releasable=true` for the fixed **local baseline predicate**.

`releasable=true` in this local evidence format does not mean a production release or any Gate
A–E promotion. It only means the repository's fixed local gate predicate passed for that exact
source candidate.

### 6.2 Reproduction commands

Run from the repository root with the supported environment installed:

```bash
PYTHONPATH=src python -m pytest \
  tests/test_native_im_codec_primitives.py \
  tests/test_native_im_contract.py \
  tests/test_native_im_contract_matrix.py \
  tests/test_native_im_fake.py \
  tests/test_native_im_gateway.py \
  tests/test_native_im_golden_vectors.py \
  tests/test_native_im_zero_network_gate.py
python scripts/verify_native_im_v1_golden.py
python scripts/verify_native_im_zero_network.py
python scripts/generate_release_evidence.py > /tmp/quantum-entanglement-release-evidence.json
python scripts/verify_release_evidence.py \
  /tmp/quantum-entanglement-release-evidence.json
```

Release evidence must remain outside the checkout. Re-run it after every source or documentation
commit; results from `7620200` do not automatically attest a later tree.

## 7. Compatibility and rollback

- `docs/architecture/NATIVE_IM_CONTRACT_V1.md` is frozen. Any field, enum, nullability, canonical
  encoding, digest-domain or idempotency-domain change requires V2 rather than a silent V1 edit.
- All additions are default-off and disconnected from the existing service composition. No
  migration, config, endpoint or credential is introduced by E1.
- The E1 commit range starts after `b26dca6` and ends at `7620200`. Reverting the range removes the
  executable contract without changing the older runtime or database schema.
- The preferred operational rollback is branch/ref selection, not deletion or history rewriting.
  Preserve the current review branch until the user accepts the stage.

## 8. E2 handoff and hard stop

At the E1 evidence commit, E2 / Level B was planned to begin only after an inbound-only sandbox
provider profile and approval inputs became available. Current E2 work has since completed both the
offline atomic page/cursor admission boundary and the default-off adapter/lifecycle, bounded parser,
kill switch, typed observability, canary and recorded probe node. The current hard stop is a
provider-specific approved transport/pure mapper plus a narrow `SERVICE_BOUNDARY.md` revision. Real
transport still requires the recorded approval inputs.

During Level B:

- outbound operation registration and allowlists remain empty;
- `dispatch` and `query_acceptance` must reject before request inspection or secret access;
- accepted events create observations only and cannot invoke `MentionRouter`, an Agent, tool,
  browser, subprocess or connector;
- no real transport is used until the dedicated sandbox endpoint class, tenant, data class,
  credential reference, method/path allowlist and expiry are recorded and approved;
- any request to send externally requires a later, separate and explicit user authorization after
  the durable Action Plane is complete.

Until those conditions are met, the current E2 offline node is the correct stopping point:
executable provider-neutral contract, deterministic fake/recorded probes, atomic durable
observation, zero real network, and no external effect.
