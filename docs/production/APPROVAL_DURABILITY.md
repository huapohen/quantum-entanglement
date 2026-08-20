# Approval durability and recovery contract

This document defines the approval guarantees implemented by the single-node
orchestrator. It is an executable pre-production boundary, not a claim that the
service has authenticated approvers or safe real-world connector effects.

## Supported topology

- One orchestrator process using one `SQLiteEventStore`.
- Trusted in-process callers.
- Synthetic or fake actions only.
- No real Feishu or WeCom send, reply, comment, mention, or upload.

The event log is the durable source of approval state. `NeedsYouQueue` is an
in-memory projection rebuilt from that log after restart.

## State and event contract

An action that needs a person crosses two durable batch boundaries.

```text
READY
  -- one append_many transaction -->
RUNNING
WAITING_APPROVAL
approval.requested

WAITING_APPROVAL
  -- one append_many transaction -->
approval.decided
READY | WAITING_INPUT | CANCELED
```

The first batch prevents a durable waiting state without its request. The
second prevents a durable decision without the task transition it authorizes.
Both batches use an expected stream version. The in-memory graph, approval
queue, and task-scoped approved set change only after the corresponding batch
commits.

## Authority snapshot isolation

`ApprovalRequest` is frozen. The queue also deep-copies its `ActionIntent` on
create, restore, get, pending, and decide. A caller may deliberately bypass the
dataclass guard with `object.__setattr__`, but such mutation affects only the
detached snapshot that caller owns; it cannot retarget the live approval to a
different task, session, action, or destination.

The queue replaces a pending request with a new decided value rather than
mutating the stored object in place.

## Durable event binding

Every approval request is bound to:

- the workflow session and known task;
- the exact `ActionIntent` stored in the task definition;
- the reason and canonical UTC creation time;
- the workflow correlation ID;
- system actor, task causation ID, and task-derived idempotency key.

Every approval decision is additionally bound to:

- the original request ID and unchanged request identity;
- the deciding actor string;
- the decision, optional comment, and decision-derived idempotency key;
- a task transition whose causation ID is that same request ID.

An approval changes task state as follows:

| Decision | Resulting task state |
|---|---|
| `approve` | `ready` and task-scoped approval becomes active |
| `revise` | `waiting_input` and no task-scoped approval is active |
| `reject` | `canceled` and no task-scoped approval is active |

## Recovery validation

Recovery reads bounded, contiguous event pages and builds new local projections.
It publishes those projections to the live kernel only after the complete
history passes validation.

Approval recovery rejects:

- missing, extra, coerced, oversized, non-canonical, or non-JSON payload fields;
- non-canonical UTC timestamps or a request time later than its event;
- an unknown task, wrong session, or intent different from the task action;
- wrong actor, correlation, causation, or idempotency envelope fields;
- a request without the immediately preceding waiting transition;
- a decision without exactly one prior request;
- a decision that changes request identity;
- a decision whose resulting transition is absent, non-adjacent, or inconsistent;
- duplicate approval histories for one task;
- a pending approval attached to a task that is not waiting.

Failure is closed: `SessionRecoveryError` is raised and the candidate kernel
does not expose a partial plan, graph, pending queue, or approved-task set.
Recovery does not silently repair or delete the stored history.

## Crash and concurrency matrix

| Boundary | Required result |
|---|---|
| Before request batch commit | task remains `ready`; no approval exists |
| Request batch commit succeeds | running, waiting, and request records all exist |
| Request batch fails | no in-memory waiting state or approval is granted |
| Before decision batch commit | task and request remain pending |
| Decision batch fails | no decision, transition, or in-memory authority is granted |
| Decision batch succeeds | decision and transition are contiguous and recover together |
| Two decisions in one process | per-session lock serializes the batches |
| Hook fails after commit | durable batch remains committed; caller sees hook failure |
| Corrupt or incomplete history | restart fails closed before publishing projection state |

The hook-after-commit case deliberately separates durable state from plugin
notification. A production service still needs a durable subscriber/projector
retry path so a hook exception cannot become an unobservable operational gap.

## Compatibility boundary

The strict recovery contract intentionally rejects old decision histories whose
task transition was written separately without the request correlation and
causation binding. No automatic rewrite is provided. An operator with such a
database must keep it quarantined until a versioned, checksum-bound migration or
explicit repair tool is implemented and rehearsed. Editing event rows manually
would destroy the audit contract.

This is a fail-closed compatibility break and therefore blocks promotion of an
existing non-empty database until the migration path exists.

## Verification

Targeted checks:

```bash
PYTHONPATH=src python3 -m unittest \
  tests.test_policy \
  tests.test_runtime \
  tests.test_approval_atomicity \
  tests.test_recovery -v
```

The adversarial matrix covers detached-snapshot mutation, injected
`append_many` failure, concurrent decision batches, payload coercion, event
envelope tampering, missing request/decision/transition records, incorrect
decision outcomes, and recovery without partial publication.

The phase must also pass the repository-wide unit, demo, compile, lint, and diff
gates from [`RELEASE_GATES.md`](./RELEASE_GATES.md) in an exact clean checkout.

## Explicit non-guarantees

This slice does **not** provide:

- authenticated identity for `actor_id`;
- current approver membership or authorization checks;
- tenant/workspace scope on the event and approval repositories;
- an action digest, policy revision, approval revision, expiry, or revocation
  check at the real tool/connector boundary;
- a durable action receipt or receiver acceptance proof;
- multi-process orchestration or a distributed session lock;
- repair tooling for rejected legacy/corrupt histories;
- a user interface or authenticated approval API.

Until those boundaries are implemented, an approval record cannot authorize a
real external side effect. The supported use remains a trusted, single-node,
fake-effect pre-production workflow.
