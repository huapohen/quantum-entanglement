from __future__ import annotations

import base64
import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path
from typing import Any

import tests.test_result_acceptance_durable_prerequisites as durable_prerequisites
from quantum_entanglement.attempts import InvocationLease
from quantum_entanglement.invocation_execution import (
    ScopedInvocationStartClaimedV3,
    ScopedInvocationStartReceiptV3,
)
from quantum_entanglement.invocation_results import (
    ScopedInvocationResultAcceptanceRequestV2,
    ScopedInvocationResultAcceptedV2,
    ScopedInvocationResultArtifactCandidateV2,
    ScopedInvocationResultManifestV2,
    ScopedInvocationResultObservedV2,
)
from quantum_entanglement.store import SQLiteEventStore


def _accept_result_worker(
    database_path: str,
    prepared_path: str,
    worker_id: str,
    ready: Any,
    start: Any,
    results: Any,
) -> None:
    """Accept one identical request from a fresh spawned interpreter."""

    raw = json.loads(Path(prepared_path).read_text(encoding="utf-8"))
    start_receipt = ScopedInvocationStartReceiptV3.from_dict(raw["start_receipt"])
    evidence = start_receipt.evidence
    candidates = tuple(
        ScopedInvocationResultArtifactCandidateV2.from_content_metadata(
            tenant_id=item["tenant_id"],
            workspace_id=item["workspace_id"],
            session_id=item["session_id"],
            task_id=item["task_id"],
            artifact_id=item["artifact_id"],
            name=item["name"],
            media_type=item["media_type"],
            content=base64.b64decode(item["content"]),
            metadata=item["metadata"],
            created_by=item["created_by"],
            idempotency_key=item["idempotency_key"],
            expected_head_version=item["expected_head_version"],
        )
        for item in raw["artifact_candidates"]
    )
    request = ScopedInvocationResultAcceptanceRequestV2(
        schema_version=raw["schema_version"],
        acceptance_idempotency_key=raw["acceptance_idempotency_key"],
        start_receipt=start_receipt,
        manifest=ScopedInvocationResultManifestV2.from_dict(raw["manifest"]),
        artifact_candidates=candidates,
        expected_stream_version=raw["expected_stream_version"],
    )
    lease = InvocationLease(
        invocation_id=evidence.invocation_id,
        session_id=evidence.session_id,
        plan_id=evidence.plan_id,
        task_id=evidence.task_id,
        agent_id=evidence.agent_id,
        idempotency_key=evidence.job_idempotency_key,
        payload_digest=evidence.manifest_digest,
        attempt_id=evidence.attempt_id,
        attempt_number=evidence.attempt_number,
        max_attempts=1,
        lease_epoch=evidence.lease_epoch,
        worker_id=evidence.worker_id,
        lease_token=raw["lease_token"],
        claimed_at=evidence.claimed_at,
        lease_expires_at=evidence.lease_expires_at,
    )
    claimed = ScopedInvocationStartClaimedV3(start_receipt, lease)
    store = SQLiteEventStore(
        database_path,
        clock=lambda: "2026-08-27T10:00:02.000000Z",
        enable_result_acceptance_schema=True,
    )
    try:
        ready.put(worker_id)
        if not start.wait(timeout=15):
            raise RuntimeError("result acceptance race barrier timed out")
        result = store.accept_scoped_invocation_result_v2(request, claimed)
        if type(result) is ScopedInvocationResultAcceptedV2:
            kind = "accepted"
        elif type(result) is ScopedInvocationResultObservedV2:
            kind = "observed"
        else:  # pragma: no cover - the public result union is closed.
            raise AssertionError("unexpected result acceptance classification")
        results.put((worker_id, kind, result.receipt.receipt_id))
    except BaseException as error:
        results.put((worker_id, "error", type(error).__name__, str(error)))
    finally:
        store.close()


class ResultAcceptanceProcessCompetitionTests(unittest.TestCase):
    def test_two_spawned_processes_yield_one_acceptance_and_one_observation(self) -> None:
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database_path = root / "event-store.sqlite3"
            prepared_path = root / "prepared-input.json"
            store = SQLiteEventStore(
                str(database_path),
                clock=lambda: "2026-08-27T10:00:00.000000Z",
                enable_result_acceptance_schema=True,
            )
            helper = durable_prerequisites.ResultAcceptanceDurablePrerequisiteTests(
                methodName="runTest"
            )
            helper.store = store
            prepared = helper.fresh_prepared()
            store.close()
            payload = {
                "schema_version": prepared.request.schema_version,
                "acceptance_idempotency_key": prepared.request.acceptance_idempotency_key,
                "start_receipt": prepared.request.start_receipt.to_dict(),
                "manifest": prepared.request.manifest.to_dict(),
                "artifact_candidates": [
                    {
                        "tenant_id": candidate.tenant_id,
                        "workspace_id": candidate.workspace_id,
                        "session_id": candidate.session_id,
                        "task_id": candidate.task_id,
                        "artifact_id": candidate.artifact_id,
                        "name": candidate.name,
                        "media_type": candidate.media_type,
                        "content": base64.b64encode(candidate.content).decode("ascii"),
                        "metadata": candidate.metadata_dict(),
                        "created_by": candidate.created_by,
                        "idempotency_key": candidate.idempotency_key,
                        "expected_head_version": candidate.expected_head_version,
                    }
                    for candidate in prepared.request.artifact_candidates
                ],
                "expected_stream_version": prepared.request.expected_stream_version,
                "lease_token": prepared.claimed.lease.lease_token,
            }
            prepared_path.write_text(json.dumps(payload), encoding="utf-8")

            ready = context.Queue()
            start = context.Event()
            results = context.Queue()
            worker_ids = ("result-accept-left", "result-accept-right")
            processes = tuple(
                context.Process(
                    target=_accept_result_worker,
                    args=(
                        str(database_path),
                        str(prepared_path),
                        worker_id,
                        ready,
                        start,
                        results,
                    ),
                )
                for worker_id in worker_ids
            )
            for process in processes:
                process.start()
            try:
                self.assertEqual(
                    {ready.get(timeout=20), ready.get(timeout=20)},
                    set(worker_ids),
                )
                start.set()
                outcomes = (results.get(timeout=30), results.get(timeout=30))
            finally:
                start.set()
                for process in processes:
                    process.join(timeout=30)
                    if process.is_alive():
                        process.terminate()
                        process.join(timeout=5)

            self.assertEqual([process.exitcode for process in processes], [0, 0])
            self.assertEqual({item[1] for item in outcomes}, {"accepted", "observed"})
            self.assertEqual(len({item[2] for item in outcomes}), 1)

            reopened = SQLiteEventStore(
                str(database_path),
                clock=lambda: "2026-08-27T10:00:03.000000Z",
                enable_result_acceptance_schema=True,
            )
            try:
                self.assertEqual(
                    reopened._connection.execute(
                        "SELECT count(*) FROM invocation_result_receipts"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    reopened._connection.execute(
                        "SELECT count(*) FROM invocation_result_event_bindings"
                    ).fetchone()[0],
                    2,
                )
                self.assertEqual(
                    reopened._connection.execute(
                        "SELECT count(*) FROM invocation_result_artifacts"
                    ).fetchone()[0],
                    len(prepared.request.artifact_candidates),
                )
                statuses = {
                    row[0]
                    for row in reopened._connection.execute(
                        "SELECT status FROM invocation_jobs UNION ALL "
                        "SELECT status FROM invocation_attempts"
                    ).fetchall()
                }
                self.assertEqual(statuses, {"succeeded"})
            finally:
                reopened.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
