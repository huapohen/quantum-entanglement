# Result Graph Readback Contract (M5 checkpoint)

Status: implemented as a private pre-commit verifier; migration 7, public result writer,
`AcceptedV2`, `ObservedV2` recovery, publication and worker dispatch remain disabled.

This document records the local checkpoint delivered by commit `da7759a`. It is the source of
truth during the remaining implementation work. Notion synchronization is intentionally deferred
until a larger checkpoint is complete; the final upload must include this file and a page-by-page
readback.

## Purpose

The result acceptance transaction can write a complete local graph before it acknowledges its
commit, but an INSERT success alone is not sufficient evidence. A trigger, storage-class drift,
wrong foreign-key coordinate, partial write, or a dependency changing a row can leave a graph that
looks complete through one read model and is not actually the graph that was requested.

The private readback runs after the result/terminal events, result authority rows, Artifact
bindings, and succeeded job/attempt CAS have all been written, but before the owner transaction
is allowed to yield its completed plan. It performs no DML and checks the graph from fixed raw
SQLite projections.

## Fixed readback order

1. **Manifest** — read the exact seven-column projection; require SQLite BLOB storage, exact
   canonical bytes, byte size, schema version, scope, digest, and accepted timestamp. Decode the
   bytes through `ScopedInvocationResultManifestV2` and compare the re-encoded bytes and value.
2. **Request** — read the exact request projection; compare every scalar binding to the prepared
   request, recompute the request digest, and compare the canonical request identity BLOB and its
   byte size. This prevents a digest column from standing in for the request body.
3. **Receipt** — read all receipt columns in a fixed order; compare IDs, scope, attempt/lease
   digests, revisions, result identity, event coordinates, envelope digests, and self-digests.
4. **Event bindings** — require exactly one `result` and one `terminal` row, with the receipt
   scope, role, event type, ID, and global position all matching the receipt.
5. **Raw events** — select the eleven event columns directly from `events`. Reconstruct a fresh
   stored-event envelope from each raw row, decode the typed evidence/terminal payload, compare
   canonical payload bytes, coordinates, causation, idempotency and actor bindings, and recompute
   the envelope digest. The result and terminal rows must be the consecutive pair recorded by the
   receipt.
6. **Artifacts** — require the exact ordered count and contiguous ordinals. Every binding row is
   compared to the prepared candidate and manifest descriptor, including its candidate digest.
   The corresponding `artifact_versions` row and raw `artifact_blobs` row are read independently;
   content, metadata bytes, request digest, version lineage, storage classes and content digest
   are checked. A pre-existing shared blob is valid and is allowed to retain its original
   canonical creation time; the newly inserted version must use this acceptance's `acceptedAt`.
7. **Job and attempt** — decode fixed projections through the existing strict row codecs. Require
   the exact scoped job and attempt to be `succeeded`, carry the accepted `resultRef`, have the
   accepted finish/update timestamps, and have all job lease columns cleared. Require exactly one
   attempt and compare the validated scoped start readback with the original start receipt.
8. **Publication boundary** — require no result publication row and no outbox row triggered by
   either result event. Actual publication is intentionally disabled in this checkpoint.
9. **Global integrity** — reject orphan result bindings/publications, any SQLite foreign-key
   violation, and any failed `PRAGMA integrity_check` result.

The readback captures `connection.total_changes` before and after verification and requires an
open owner transaction throughout. A change counter delta, closed transaction, malformed fixed
row, canonical mismatch or any binding drift raises `_ResultAcceptanceIntegrityError`; the outer
owner transaction then rolls back the complete graph, including Artifact rows and the job/attempt
CAS.

## Quarantine classification

Before a fresh lease is inspected, structural prefixes are classified as read-only quarantine
signals. The private `_ResultAcceptanceQuarantineError` carries a closed category and stable code
`result_acceptance_graph_quarantined`; its category is one of:

| Category | Meaning | Allowed action |
|---|---|---|
| `partial` | A result graph has only a durable prefix or missing required rows. | Preserve evidence, roll back any current owner transaction, and require a reconciler; never repair by guessing. |
| `drift` | Existing identities, coordinates, payloads, digests, storage classes, or terminal state disagree. | Stop acceptance and investigate the exact durable graph; do not replay the Agent. |
| `orphan` | A result binding/publication exists without its receipt authority. | Isolate the source and operator-review both stores; do not delete or synthesize a receipt. |

The category is the only machine-facing diagnostic. Local detail text is bounded to the current
integrity message and is never populated with plaintext lease tokens, credentials, raw provider
responses, or exception graphs. All three categories remain subclasses of the existing private
`_ResultAcceptanceIntegrityError`, so callers cannot accidentally treat quarantine as a successful
result. A quarantined graph is never eligible for a fresh acceptance or an `AcceptedV2` capability.

## Private capability boundary

`_ReadbackFreshResultAcceptancePlanV2` is an exact private, owner-transaction-bound plan. It holds
only a copied capability-free receipt, cannot be copied, deep-copied or serialized, and is
invalidated when its context exits. It is not exported from the package root and does not mint an
accepted result or publication authority. The completed plan is yielded only while this readback
context is still active.

## Tests and release gate

The focused durable-prerequisite suite covers:

- complete graph readback on a normal Artifact result;
- pre-existing shared blob reuse;
- narration-only (zero Artifact) results;
- receipt drift injected during readback, proving full rollback;
- the existing graph path, which short-circuits without writes or terminal CAS;
- existing event, receipt, persistence and CAS fault fences.

Required local commands:

```bash
PYTHONPATH=src pytest -q tests/test_result_acceptance_durable_prerequisites.py
PYTHONPATH=src pytest -q --disable-warnings
PYTHONPATH=src mypy src
python -m ruff check src tests
python -m ruff format --check src/quantum_entanglement/store.py
git diff --check
```

The repository-wide format check still reports historical drift in unrelated pre-existing files;
the modified store and focused test file are formatted and lint-clean. No API key, plaintext lease
token, Feishu/WeCom message, webhook, or external publication is used by this stage.

## Next local stages

1. Add explicit partial/drift graph quarantine classification and stable diagnostics without
   exposing lease material.
2. Add fault injection at each result DML, before/after commit, and acknowledgement-loss path.
3. Add read-only `ObservedV2` replay/reopen/recovery support; keep fresh `AcceptedV2` capability
   separate from observations.
4. Re-run the full release gates and update the local roadmap/checkpoint ledger.
5. Only after the local checkpoint is stable, upload the complete Markdown bundle to Notion and
   read every updated page back before considering the remote copy synchronized.
