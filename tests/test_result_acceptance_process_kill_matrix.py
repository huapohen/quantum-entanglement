from __future__ import annotations

import base64
import json
import multiprocessing
import os
import signal
import tempfile
import unittest
from pathlib import Path
from typing import Any

import quantum_entanglement.store as store_module
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
)
from quantum_entanglement.store import SQLiteEventStore

_KILL_POINTS = (
    "artifact",
    "event:1",
    "event:2",
    "insert:manifest",
    "insert:request",
    "insert:result event binding",
    "insert:terminal event binding",
    "insert:receipt",
    "insert:Artifact binding 0",
    "update:job terminal CAS",
    "update:attempt terminal CAS",
)


def _accept_result_worker(
    database_path: str,
    prepared_path: str,
    kill_point: str,
    ready: Any,
    start: Any,
) -> None:
    """Kill one child after a selected result-acceptance write boundary."""

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
    event_calls = 0

    def kill_if(point: str) -> None:
        if point == kill_point:
            os.kill(os.getpid(), signal.SIGKILL)

    original_materialize = store_module._materialize_prepared_result_artifacts_in_transaction

    def kill_after_materialize(artifact_plan: Any, accepted_at: str) -> Any:
        result = original_materialize(artifact_plan, accepted_at)
        kill_if("artifact")
        return result

    original_insert = (
        store_module.SQLiteEventStore._insert_exact_result_acceptance_row_in_owner_transaction
    )

    def kill_after_insert(
        owner: SQLiteEventStore,
        connection: Any,
        sql: str,
        values: tuple[object, ...],
        *,
        label: str,
    ) -> None:
        original_insert(owner, connection, sql, values, label=label)
        kill_if(f"insert:{label}")

    original_update = (
        store_module.SQLiteEventStore._update_exact_result_acceptance_row_in_owner_transaction
    )

    def kill_after_update(
        owner: SQLiteEventStore,
        connection: Any,
        sql: str,
        values: tuple[object, ...],
        *,
        label: str,
    ) -> None:
        original_update(owner, connection, sql, values, label=label)
        kill_if(f"update:{label}")

    original_event_insert = (
        store_module.SQLiteEventStore._insert_with_verified_envelope_in_transaction
    )

    def kill_after_event(
        owner: SQLiteEventStore,
        connection: Any,
        snapshot: Any,
        expected_version: int | None,
        expected_global_position: int | None = None,
    ) -> Any:
        nonlocal event_calls
        result = original_event_insert(
            owner,
            connection,
            snapshot,
            expected_version,
            expected_global_position,
        )
        event_calls += 1
        kill_if(f"event:{event_calls}")
        return result

    store_module._materialize_prepared_result_artifacts_in_transaction = kill_after_materialize
    store_module.SQLiteEventStore._insert_exact_result_acceptance_row_in_owner_transaction = (
        kill_after_insert
    )
    store_module.SQLiteEventStore._update_exact_result_acceptance_row_in_owner_transaction = (
        kill_after_update
    )
    store_module.SQLiteEventStore._insert_with_verified_envelope_in_transaction = kill_after_event
    try:
        ready.put(kill_point)
        if not start.wait(timeout=15):
            raise RuntimeError("result acceptance kill-matrix barrier timed out")
        store.accept_scoped_invocation_result_v2(request, claimed)
    finally:
        store.close()


class ResultAcceptanceProcessKillMatrixTests(unittest.TestCase):
    @unittest.skipUnless(
        hasattr(os, "kill") and hasattr(signal, "SIGKILL"),
        "requires POSIX SIGKILL",
    )
    def test_each_precommit_boundary_rolls_back_and_allows_one_retry(self) -> None:
        context = multiprocessing.get_context("spawn")
        for kill_point in _KILL_POINTS:
            with self.subTest(kill_point=kill_point), tempfile.TemporaryDirectory() as directory:
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
                child = context.Process(
                    target=_accept_result_worker,
                    args=(str(database_path), str(prepared_path), kill_point, ready, start),
                )
                child.start()
                try:
                    self.assertEqual(ready.get(timeout=20), kill_point)
                    start.set()
                    child.join(timeout=30)
                finally:
                    start.set()
                    if child.is_alive():
                        child.terminate()
                        child.join(timeout=5)

                self.assertEqual(child.exitcode, -signal.SIGKILL)
                reopened = SQLiteEventStore(
                    str(database_path),
                    clock=lambda: "2026-08-27T10:00:02.000000Z",
                    enable_result_acceptance_schema=True,
                )
                try:
                    for table_name in (
                        "artifact_blobs",
                        "artifact_versions",
                        "invocation_result_manifests",
                        "invocation_result_requests",
                        "invocation_result_event_bindings",
                        "invocation_result_receipts",
                        "invocation_result_artifacts",
                    ):
                        self.assertEqual(
                            reopened._connection.execute(
                                f"SELECT count(*) FROM {table_name}"
                            ).fetchone()[0],
                            0,
                            kill_point,
                        )
                    self.assertEqual(
                        reopened.stream_version(prepared.request.start_receipt.stream_id),
                        prepared.request.expected_stream_version,
                    )
                    accepted = reopened.accept_scoped_invocation_result_v2(
                        prepared.request,
                        prepared.claimed,
                    )
                    self.assertIs(type(accepted), ScopedInvocationResultAcceptedV2)
                    self.assertEqual(
                        reopened._connection.execute(
                            "SELECT count(*) FROM invocation_result_receipts"
                        ).fetchone()[0],
                        1,
                    )
                finally:
                    reopened.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
