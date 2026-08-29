from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from quantum_entanglement.invocation_results import ScopedInvocationResultAcceptedV2
from quantum_entanglement.store import SQLiteEventStore
import tests.test_result_acceptance_durable_prerequisites as durable_prerequisites


class ResultAcceptanceProcessRecoveryTests(unittest.TestCase):
    def test_sigkill_after_artifact_write_rolls_back_then_retries_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database_path = root / "event-store.sqlite3"
            signal_path = root / "artifact-write-started"
            payload_path = root / "prepared-input.json"
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
            payload_path.write_text(json.dumps(payload), encoding="utf-8")

            child_code = f"""
import base64
import json
import time
from pathlib import Path
import quantum_entanglement.store as store_module
from quantum_entanglement.attempts import InvocationLease
from quantum_entanglement.invocation_execution import (
    ScopedInvocationStartClaimedV3,
    ScopedInvocationStartReceiptV3,
)
from quantum_entanglement.invocation_results import (
    ScopedInvocationResultAcceptanceRequestV2,
    ScopedInvocationResultArtifactCandidateV2,
    ScopedInvocationResultManifestV2,
)
from quantum_entanglement.store import SQLiteEventStore

database_path = {str(database_path)!r}
signal_path = {str(signal_path)!r}
prepared_path = {str(payload_path)!r}
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
original = store_module._materialize_prepared_result_artifacts_in_transaction

def block_after_artifact_write(artifact_plan, accepted_at):
    result = original(artifact_plan, accepted_at)
    Path(signal_path).touch()
    while True:
        time.sleep(1)

store_module._materialize_prepared_result_artifacts_in_transaction = block_after_artifact_write
store.accept_scoped_invocation_result_v2(request, claimed)
"""
            environment = os.environ.copy()
            environment["PYTHONPATH"] = os.pathsep.join(
                (
                    str(Path(__file__).resolve().parents[1] / "src"),
                    environment.get("PYTHONPATH", ""),
                )
            ).rstrip(os.pathsep)
            child = subprocess.Popen(
                [sys.executable, "-c", child_code],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                deadline = time.monotonic() + 5
                while not signal_path.exists() and child.poll() is None:
                    if time.monotonic() >= deadline:
                        stdout, stderr = child.communicate(timeout=5)
                        self.fail(
                            "result child did not reach the uncommitted artifact boundary: "
                            f"returncode={child.returncode}, stdout={stdout!r}, stderr={stderr!r}"
                        )
                    time.sleep(0.01)
                if not signal_path.exists():
                    stdout, stderr = child.communicate(timeout=5)
                    self.fail(
                        "result child exited before the uncommitted artifact boundary: "
                        f"returncode={child.returncode}, stdout={stdout!r}, stderr={stderr!r}"
                    )
                child.kill()
                child.wait(timeout=5)
                self.assertEqual(child.returncode, -9)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)

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
                        table_name,
                    )
                accepted = reopened.accept_scoped_invocation_result_v2(
                    prepared.request,
                    prepared.claimed,
                )
                self.assertIs(type(accepted), ScopedInvocationResultAcceptedV2)
                assert type(accepted) is ScopedInvocationResultAcceptedV2
                self.assertEqual(
                    accepted.receipt.evidence.invocation_id,
                    "invocation-scoped-store-1",
                )
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
