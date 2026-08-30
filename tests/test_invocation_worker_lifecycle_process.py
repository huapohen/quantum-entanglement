from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from quantum_entanglement.attempts import AttemptStatus, InvocationStatus
from quantum_entanglement.invocation_execution import (
    ScopedInvocationStartClaimedV3,
)
from quantum_entanglement.store import SQLiteEventStore
from tests.test_scoped_task_invocation_admission import CLAIMED_AT, STORE_TIME, scoped_request


class ScopedPureWorkerLifecycleProcessTests(unittest.TestCase):
    def test_sigkill_during_pure_handler_recovers_as_expired_without_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database_path = root / "lifecycle.sqlite3"
            payload_path = root / "claim.json"
            started_path = root / "handler-started"
            store = SQLiteEventStore(
                str(database_path),
                clock=lambda: STORE_TIME,
                enable_result_acceptance_schema=True,
            )
            request = scoped_request()
            store.append_scoped_task_invocation_admission_v2(request, expected_version=0)
            store._clock = lambda: CLAIMED_AT
            claimed = store.claim_scoped_invocation_start_v3(
                request.manifest.tenant_id,
                request.manifest.workspace_id,
                request.manifest.invocation_id,
                "worker-lifecycle-process-kill",
                lease_seconds=0.30,
                expected_version=2,
            )
            self.assertIs(type(claimed), ScopedInvocationStartClaimedV3)
            assert type(claimed) is ScopedInvocationStartClaimedV3
            payload_path.write_text(
                json.dumps(
                    {
                        "manifest": request.manifest.to_dict(),
                        "receipt": claimed.receipt.to_dict(),
                        "lease": {
                            "invocation_id": claimed.lease.invocation_id,
                            "session_id": claimed.lease.session_id,
                            "plan_id": claimed.lease.plan_id,
                            "task_id": claimed.lease.task_id,
                            "agent_id": claimed.lease.agent_id,
                            "idempotency_key": claimed.lease.idempotency_key,
                            "payload_digest": claimed.lease.payload_digest,
                            "attempt_id": claimed.lease.attempt_id,
                            "attempt_number": claimed.lease.attempt_number,
                            "max_attempts": claimed.lease.max_attempts,
                            "lease_epoch": claimed.lease.lease_epoch,
                            "worker_id": claimed.lease.worker_id,
                            "lease_token": claimed.lease.lease_token,
                            "claimed_at": claimed.lease.claimed_at,
                            "lease_expires_at": claimed.lease.lease_expires_at,
                        },
                    }
                ),
                encoding="utf-8",
            )
            store.close()

            child_code = f"""
import asyncio
import json
from pathlib import Path
from quantum_entanglement.attempts import InvocationLease
from quantum_entanglement.invocation_execution import (
    ScopedInvocationExecutionManifestV2,
    ScopedInvocationStartClaimedV3,
    ScopedInvocationStartReceiptV3,
)
from quantum_entanglement.invocation_worker import (
    InvocationWorkerConfiguration,
    PureWorkerContext,
)
from quantum_entanglement.invocation_worker_lifecycle import ScopedPureWorkerLifecycle
from quantum_entanglement.store import SQLiteEventStore

database_path = {str(database_path)!r}
payload_path = {str(payload_path)!r}
started_path = {str(started_path)!r}
raw = json.loads(Path(payload_path).read_text(encoding="utf-8"))
receipt = ScopedInvocationStartReceiptV3.from_dict(raw["receipt"])
lease = InvocationLease(**raw["lease"])
claimed = ScopedInvocationStartClaimedV3(receipt, lease)
manifest = ScopedInvocationExecutionManifestV2.from_dict(raw["manifest"])
store = SQLiteEventStore(
    database_path,
    clock=lambda: "2026-08-27T10:00:01.000000Z",
    enable_result_acceptance_schema=True,
)
lifecycle = ScopedPureWorkerLifecycle(store)

async def handler(_context: PureWorkerContext) -> object:
    Path(started_path).touch()
    while True:
        await asyncio.sleep(1)

async def acceptor(_request: object, _claim: object) -> object:
    return object()

asyncio.run(
    lifecycle.run_and_accept(
        claimed,
        manifest,
        InvocationWorkerConfiguration(
            lease_seconds=0.30,
            heartbeat_interval_seconds=0.02,
            handler_timeout_seconds=0.10,
            drain_timeout_seconds=0.05,
        ),
        handler,
        acceptor=acceptor,
    )
)
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
                while not started_path.exists() and child.poll() is None:
                    if time.monotonic() >= deadline:
                        stdout, stderr = child.communicate(timeout=5)
                        self.fail(
                            "lifecycle child did not reach pure handler: "
                            f"returncode={child.returncode}, stdout={stdout!r}, stderr={stderr!r}"
                        )
                    time.sleep(0.01)
                self.assertTrue(started_path.exists())
                child.kill()
                child.wait(timeout=5)
                self.assertEqual(child.returncode, -9)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)

            reopened = SQLiteEventStore(
                str(database_path),
                clock=lambda: "2026-08-27T10:00:03.000000Z",
                enable_result_acceptance_schema=True,
            )
            try:
                recovered = reopened.recover_expired_scoped_invocations()
                self.assertEqual(
                    recovered.exhausted,
                    (request.manifest.invocation_id,),
                )
                snapshot = reopened.recovery_snapshot_for_task(
                    request.manifest.session_id,
                    request.manifest.task_id,
                )
            finally:
                reopened.close()

            self.assertIsNotNone(snapshot.job)
            self.assertIsNotNone(snapshot.current_attempt)
            assert snapshot.job is not None
            assert snapshot.current_attempt is not None
            self.assertEqual(snapshot.job.status, InvocationStatus.FAILED)
            self.assertEqual(snapshot.current_attempt.status, AttemptStatus.EXPIRED)
            self.assertEqual(
                snapshot.current_attempt.error,
                "lease expired before terminal acknowledgement",
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

