# Receipt-bound result reconciliation

Status: **implemented as an explicit migration-7 opt-in, non-emitting recovery primitive.**
The API is available only on `SQLiteEventStore(enable_result_acceptance_schema=True)`. It does
not enable production worker dispatch, publication, real IM connectivity, or any external side
effect. The separate result-acceptance API may return process-bound `AcceptedV2` only for a fresh
COMMIT ACK; this reconciliation path always returns a capability-free observation.

This document is a local-first release artifact. Markdown and Git/GitHub are the source of truth
while the branch is being developed; the corresponding Notion page remains pending until the
checkpoint is closed and a page-by-page remote readback is performed.

## Why this boundary exists

A process can commit the durable result graph and then die before the caller receives its
acknowledgement. The graph contains the result manifest, request, receipt, raw result/terminal
events, Artifact bindings and blob/version rows, while the owning invocation job and attempt may
still be `running`. Retrying the Agent would risk duplicate work or an irreversible external
effect. Reconciliation therefore consumes only a complete, receipt-bound graph and closes the
existing owner rows by compare-and-set (CAS).

The receipt is evidence, not a new lease. The reconciler never accepts caller-supplied result
content, never refreshes a lease, and never invokes an Agent, plugin, publisher, connector or
network client.

## API

```python
from quantum_entanglement.store import (
    ResultReconciliationConflictError,
    ResultReconciliationOutcome,
    SQLiteEventStore,
)

store = SQLiteEventStore(
    "event-store.sqlite3",
    enable_result_acceptance_schema=True,
)
outcome = store.reconcile_scoped_invocation_result(
    tenant_id,
    workspace_id,
    invocation_id,
)
```

The return value is either `None` (the requested scope has no result graph) or an immutable
`ResultReconciliationResult`:

| Outcome | Meaning | Durable writes |
| --- | --- | --- |
| `RECONCILED` | A complete graph was bound to the still-running owner and both exact CAS operations succeeded. | One job update and one attempt update; no event/outbox/publication row. |
| `ALREADY_RECONCILED` | The exact graph was already reflected by a succeeded job and attempt. | None. |

`ResultReconciliationResult.observed` is a capability-free, independently reconstructed receipt
observation. It does not contain a plaintext lease token and cannot be used as a write authority.
`ResultReconciliationConflictError` is raised for a missing/malformed owner, stale lease binding,
non-running owner, failed CAS, trigger side effect, or any other owner-boundary conflict. Durable
graph drift and partial/orphan prefixes retain the existing stable result-quarantine exception and
category (`partial`, `drift`, or `orphan`); the reconciler never repairs them by guessing.

## Transaction contract

One `BEGIN IMMEDIATE` transaction covers the complete operation:

1. Load exactly one job and one attempt and validate the bounded recovery snapshot.
2. Reconstruct the complete result graph from fixed raw projections, allowing only the exact
   pre-CAS `running` state or the already-terminal state for readback.
3. Verify tenant/workspace/invocation scope, start receipt, worker, lease epoch, lease digest,
   result reference, accepted timestamp and every manifest/request/Artifact/event digest.
4. If already terminal with the same result reference, return `ALREADY_RECONCILED` without DML.
5. Otherwise require both owner rows to be `running` and match the receipt's worker, epoch and
   lease digest. Execute a job CAS followed by an attempt CAS. Each statement must change exactly
   one row; `rowcount`, `changes()` and `total_changes` are checked to detect trigger or adapter
   surprises.
6. Re-run the full terminal readback before the transaction yields. Any exception rolls back both
   owner updates and all trigger side effects. A post-CAS failure therefore cannot leave a mixed
   owner state.

No new event, snapshot, outbox message, publication record, lease, capability or result body is
created. Event and outbox counts are invariant across both successful reconciliation and the
`ALREADY_RECONCILED` replay.

## Failure and concurrency matrix

| Situation | Result | Required handling |
| --- | --- | --- |
| No receipt/request/artifact prefix in scope | `None` | Keep the task on the normal recovery path. |
| Complete graph + matching `RUNNING` owner | `RECONCILED` | Continue with ordinary read-only observation; no Agent retry. |
| Repeated call after the CAS | `ALREADY_RECONCILED` | Treat as an idempotent replay. |
| Lease digest/epoch/worker no longer matches | `ResultReconciliationConflictError` or owner quarantine | Fence the stale owner; do not accept the result or retry blindly. |
| Another writer wins either CAS | `ResultReconciliationConflictError` | Roll back the whole transaction and re-read the owner before deciding. |
| Trigger/dependency changes more than one row | `ResultReconciliationConflictError` | Roll back; inspect trigger/schema drift. |
| Partial, orphan or tampered result graph | quarantine with stable category | Preserve evidence and require operator/recovery workflow. |
| Migration-7 feature disabled | schema-unavailable error | Explicitly activate the candidate migration; default startup remains legacy-safe. |

The CAS is scoped by invocation identity, session/task/agent binding, attempt number, lease epoch,
worker and lease digest. An update that affects zero or more than one row is never treated as
success. The result graph is not deleted on conflict, so an operator can reopen an opt-in store
and perform a capability-free readback.

## Verification evidence

`tests/test_result_reconciliation.py` covers:

- explicit migration-7 opt-in gating and empty-scope `None`;
- successful running-owner reconciliation and exact terminal fields;
- idempotent `ALREADY_RECONCILED` replay;
- stale/malformed owner rejection without mutation;
- a competing attempt CAS, proving rollback of the first CAS and the competing write;
- trigger side effects, proving `total_changes` detects the extra write and the complete
  transaction rolls back;
- event and outbox counts remaining unchanged.

Focused commands:

```bash
PYTHONPATH=src pytest -q tests/test_result_reconciliation.py
python -m ruff check \
  src/quantum_entanglement/store.py \
  src/quantum_entanglement/__init__.py \
  tests/test_result_reconciliation.py
PYTHONPATH=src python -m mypy \
  src/quantum_entanglement/store.py \
  src/quantum_entanglement/__init__.py
git diff --check
```

These are local SQLite fault and integrity tests. They do not prove crash-at-every-boundary,
`kill -9`, dual-process scheduling, backup/restore replay, capacity, SLO/RPO/RTO, production
tenant isolation, or a safe connection to any real IM. Those remain separate release gates.

## Next release gates

Before enabling a worker or connecting the native IM, the branch still needs:

1. the existing store-owned result acceptance seam wired into a promoted receipt-aware pure worker
   gate, including process-kill, lease-loss-during-acceptance and two-process evidence;
2. business projection and action receipt semantics separate from result reconciliation;
3. compatibility/rollback runbooks and a release evidence bundle;
4. only then, an independently approved provider contract and production exchange for the native
   IM. No Feishu, WeCom, Notion, Yuque or external connector is called by this API.
