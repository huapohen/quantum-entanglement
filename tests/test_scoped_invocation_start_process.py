from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import queue
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import quantum_entanglement.store as store_module
from quantum_entanglement.backup import (
    create_sqlite_backup,
    default_manifest_path,
    restore_sqlite_backup,
    verify_sqlite_backup,
)
from quantum_entanglement.invocation_execution import (
    TASK_INVOCATION_STARTED_EVENT_TYPE,
    ScopedInvocationExecutionManifestV2,
    ScopedInvocationStartClaimedV3,
    ScopedInvocationStartObservedV3,
    ScopedTaskInvocationAdmissionRequestV2,
    build_scoped_task_invocation_admission_request_v2,
)
from quantum_entanglement.protocol import TaskStatus
from quantum_entanglement.scheduler import TaskTransition
from quantum_entanglement.store import EventStoreLifecycleError, SQLiteEventStore

ADMITTED_AT = "2026-08-27T12:00:00Z"
REQUESTED_AT = "2026-08-27T12:00:00.000001Z"
RUNNING_AT = "2026-08-27T12:00:00.000002Z"
CLAIMED_AT = "2026-08-27T12:00:01.000000Z"
TENANT_ID = "tenant-scoped-process-1"
WORKSPACE_ID = "workspace-scoped-process-1"
INVOCATION_ID = "invocation-scoped-process-1"


def scoped_request() -> ScopedTaskInvocationAdmissionRequestV2:
    manifest = ScopedInvocationExecutionManifestV2.from_dict(
        {
            "schemaVersion": 2,
            "tenantId": TENANT_ID,
            "workspaceId": WORKSPACE_ID,
            "invocationId": INVOCATION_ID,
            "sessionId": "session-scoped-process-1",
            "planId": "plan-scoped-process-1",
            "taskId": "task-scoped-process-1",
            "agentId": "agent-scoped-process-1",
            "jobIdempotencyKey": "invoke:task-scoped-process-1",
            "taskRevision": 7,
            "correlationId": "correlation-scoped-process-1",
            "causationId": "task-scoped-process-1",
            "envelopeDigest": "a" * 64,
            "contextDigest": "b" * 64,
            "authorizationDigest": "c" * 64,
            "runtimeRevision": "runtime:sha256:" + ("d" * 64),
            "effectClass": "pure",
            "retryClass": "never",
        }
    )
    return build_scoped_task_invocation_admission_request_v2(
        manifest,
        TaskTransition(
            task_id=manifest.task_id,
            previous=TaskStatus.READY,
            current=TaskStatus.RUNNING,
            reason=None,
            revision=manifest.task_revision,
        ),
        execution_requested_event_id="event-scoped-process-requested-1",
        execution_requested_timestamp=REQUESTED_AT,
        task_running_event_id="event-scoped-process-running-1",
        task_running_timestamp=RUNNING_AT,
        job_priority=59,
    )


def spawn_scoped_start_worker(
    path: str,
    worker_id: str,
    ready: Any,
    start: Any,
    results: Any,
    token_calls: Any,
) -> None:
    """Race from a fresh interpreter without serializing plaintext lease authority."""

    store = SQLiteEventStore(path, clock=lambda: CLAIMED_AT)

    def token_provider(_nbytes: int = 32) -> str:
        token_calls.put(worker_id)
        return f"spawn-scoped-raw-token-{worker_id}"

    try:
        ready.put(worker_id)
        if not start.wait(timeout=10):
            raise RuntimeError("spawned scoped-start barrier timed out")
        with patch(
            "quantum_entanglement.store.secrets.token_urlsafe",
            new=token_provider,
        ):
            result = store.claim_scoped_invocation_start_v3(
                TENANT_ID,
                WORKSPACE_ID,
                INVOCATION_ID,
                worker_id,
                lease_seconds=60,
                expected_version=2,
            )
        if type(result) is ScopedInvocationStartClaimedV3:
            typed = cast(ScopedInvocationStartClaimedV3, result)
            kind = "claimed"
        elif type(result) is ScopedInvocationStartObservedV3:
            typed = cast(ScopedInvocationStartObservedV3, result)
            kind = "observed"
        else:  # pragma: no cover - the public result union is closed.
            raise AssertionError("unexpected scoped invocation-start result")
        results.put(
            (
                kind,
                typed.receipt.event_id,
                typed.receipt.evidence.attempt_id,
            )
        )
    finally:
        store.close()


def probe_scoped_start_provider_fork(
    path: str,
    stage: str,
    result_connection: Any,
) -> None:
    """Fork in one provider and report the child's immediate process-guard result."""

    store = SQLiteEventStore(path, clock=lambda: ADMITTED_AT)
    request = scoped_request()
    store.append_scoped_task_invocation_admission_v2(request, expected_version=0)
    parent_pid = os.getpid()
    forked = False

    def fork_value(child_value: str, parent_value: str) -> str:
        nonlocal forked
        if forked:
            raise RuntimeError("scoped start provider fork repeated")
        forked = True
        child_pid = os.fork()
        if child_pid == 0:
            return child_value
        waited_pid, status = os.waitpid(child_pid, 0)
        if waited_pid != child_pid or not os.WIFEXITED(status) or os.WEXITSTATUS(status) != 0:
            raise RuntimeError("scoped start provider child did not exit cleanly")
        return parent_value

    def clock_provider() -> str:
        if stage == "clock":
            return fork_value(CLAIMED_AT, CLAIMED_AT)
        return CLAIMED_AT

    def id_provider(prefix: str = "evt") -> str:
        if stage == "attempt-id" and prefix == "attempt":
            return fork_value("attempt-scoped-child", "attempt-scoped-parent")
        if stage == "event-id" and prefix == "evt":
            return fork_value("event-scoped-child", "event-scoped-parent")
        return f"{prefix}-{stage}-scoped-stable"

    def token_provider(_nbytes: int = 32) -> str:
        if stage == "lease-token":
            return fork_value("raw-scoped-token-child", "raw-scoped-token-parent")
        return f"raw-scoped-token-{stage}-stable"

    try:
        with (
            patch.object(store, "_now", new=clock_provider),
            patch.object(store_module, "new_id", new=id_provider),
            patch(
                "quantum_entanglement.store.secrets.token_urlsafe",
                new=token_provider,
            ),
        ):
            try:
                result = store.claim_scoped_invocation_start_v3(
                    request.manifest.tenant_id,
                    request.manifest.workspace_id,
                    request.manifest.invocation_id,
                    "worker-scoped-provider-fork",
                    lease_seconds=60,
                    expected_version=2,
                )
            except BaseException as error:
                if os.getpid() == parent_pid:
                    raise
                result_connection.send(
                    (
                        "child",
                        stage,
                        type(error).__name__,
                        getattr(error, "code", None),
                    )
                )
                result_connection.close()
                os._exit(0)
        if type(result) is not ScopedInvocationStartClaimedV3:
            raise AssertionError("provider-fork parent did not retain the scoped claim")
        claimed = cast(ScopedInvocationStartClaimedV3, result)
        result_connection.send(
            (
                "parent",
                stage,
                type(claimed).__name__,
                claimed.receipt.event_id,
                claimed.receipt.evidence.attempt_id,
            )
        )
    finally:
        store.close()
        result_connection.close()


def sqlite_files(path: Path) -> tuple[Path, ...]:
    return tuple(
        candidate
        for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm"))
        if candidate.exists()
    )


class ScopedInvocationStartProcessTests(unittest.TestCase):
    @unittest.skipUnless(hasattr(os, "fork"), "POSIX fork is unavailable")
    def test_every_provider_fork_rejects_child_and_preserves_one_parent_claim(self) -> None:
        context = multiprocessing.get_context("spawn")
        stages = ("clock", "attempt-id", "event-id", "lease-token")
        with tempfile.TemporaryDirectory() as tempdir:
            for stage in stages:
                with self.subTest(stage=stage):
                    path = str(Path(tempdir) / f"provider-fork-{stage}.sqlite3")
                    receive_connection, send_connection = context.Pipe(duplex=False)
                    process = context.Process(
                        target=probe_scoped_start_provider_fork,
                        args=(path, stage, send_connection),
                    )
                    process.start()
                    send_connection.close()
                    outcomes: set[tuple[object, ...]] = set()
                    try:
                        for _ in range(2):
                            self.assertTrue(
                                receive_connection.poll(15),
                                f"scoped provider fork {stage} did not report both outcomes",
                            )
                            outcomes.add(receive_connection.recv())
                    finally:
                        receive_connection.close()
                        process.join(timeout=15)
                        if process.is_alive():
                            process.terminate()
                            process.join(timeout=5)

                    self.assertEqual(process.exitcode, 0)
                    child = next(item for item in outcomes if item[0] == "child")
                    parent = next(item for item in outcomes if item[0] == "parent")
                    self.assertEqual(
                        child,
                        (
                            "child",
                            stage,
                            EventStoreLifecycleError.__name__,
                            EventStoreLifecycleError.code,
                        ),
                    )
                    self.assertEqual(
                        parent[0:3],
                        ("parent", stage, ScopedInvocationStartClaimedV3.__name__),
                    )

                    reopened = SQLiteEventStore(path, clock=lambda: ADMITTED_AT)
                    try:
                        observed = reopened.read_scoped_invocation_start_v3(
                            TENANT_ID,
                            WORKSPACE_ID,
                            INVOCATION_ID,
                        )
                        self.assertIs(type(observed), ScopedInvocationStartObservedV3)
                        typed = cast(ScopedInvocationStartObservedV3, observed)
                        self.assertEqual(typed.receipt.event_id, parent[3])
                        self.assertEqual(typed.receipt.evidence.attempt_id, parent[4])
                        self.assertEqual(typed.receipt.evidence.schema_version, 3)
                        self.assertFalse(hasattr(typed, "lease"))
                        self.assertEqual(
                            reopened._connection.execute(
                                "SELECT COUNT(*) FROM invocation_attempts"
                            ).fetchone()[0],
                            1,
                        )
                    finally:
                        reopened.close()

    def test_two_spawned_processes_race_to_one_claim_and_one_observation(self) -> None:
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as tempdir:
            path = str(Path(tempdir) / "spawn-race.sqlite3")
            request = scoped_request()
            store = SQLiteEventStore(path, clock=lambda: ADMITTED_AT)
            try:
                store.append_scoped_task_invocation_admission_v2(
                    request,
                    expected_version=0,
                )
            finally:
                store.close()

            ready = context.Queue()
            start = context.Event()
            results = context.Queue()
            token_calls = context.Queue()
            worker_ids = ("worker-scoped-spawn-left", "worker-scoped-spawn-right")
            processes = tuple(
                context.Process(
                    target=spawn_scoped_start_worker,
                    args=(path, worker_id, ready, start, results, token_calls),
                )
                for worker_id in worker_ids
            )
            for process in processes:
                process.start()
            try:
                self.assertEqual(
                    {ready.get(timeout=15), ready.get(timeout=15)},
                    set(worker_ids),
                )
                start.set()
                outcomes = (results.get(timeout=20), results.get(timeout=20))
            finally:
                start.set()
                for process in processes:
                    process.join(timeout=20)
                    if process.is_alive():
                        process.terminate()
                        process.join(timeout=5)

            self.assertEqual([process.exitcode for process in processes], [0, 0])
            self.assertEqual({item[0] for item in outcomes}, {"claimed", "observed"})
            self.assertEqual(len({item[1] for item in outcomes}), 1)
            self.assertEqual(len({item[2] for item in outcomes}), 1)
            token_worker = token_calls.get(timeout=5)
            self.assertIn(token_worker, worker_ids)
            with self.assertRaises(queue.Empty):
                token_calls.get(timeout=0.2)

            reopened = SQLiteEventStore(path, clock=lambda: CLAIMED_AT)
            try:
                observed = reopened.read_scoped_invocation_start_v3(
                    TENANT_ID,
                    WORKSPACE_ID,
                    INVOCATION_ID,
                )
                with patch(
                    "quantum_entanglement.store.secrets.token_urlsafe",
                    side_effect=AssertionError("reopen must not mint another lease token"),
                ):
                    replay = reopened.claim_scoped_invocation_start_v3(
                        TENANT_ID,
                        WORKSPACE_ID,
                        INVOCATION_ID,
                        "worker-scoped-reopened",
                        lease_seconds=60,
                        expected_version=2,
                    )
                self.assertIs(type(observed), ScopedInvocationStartObservedV3)
                self.assertIs(type(replay), ScopedInvocationStartObservedV3)
                typed_observed = cast(ScopedInvocationStartObservedV3, observed)
                typed_replay = cast(ScopedInvocationStartObservedV3, replay)
                self.assertEqual(typed_replay.receipt, typed_observed.receipt)
                self.assertEqual(typed_observed.receipt.event_id, outcomes[0][1])
                self.assertFalse(hasattr(typed_observed, "lease"))
                self.assertFalse(hasattr(typed_replay, "lease"))
                self.assertEqual(reopened.stream_version(request.stream_id), 3)
                self.assertEqual(
                    reopened._connection.execute(
                        "SELECT COUNT(*) FROM invocation_attempts WHERE invocation_id = ?",
                        (INVOCATION_ID,),
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    reopened._connection.execute(
                        "SELECT COUNT(*) FROM events WHERE event_type = ?",
                        (TASK_INVOCATION_STARTED_EVENT_TYPE,),
                    ).fetchone()[0],
                    1,
                )
                spawned_token = f"spawn-scoped-raw-token-{token_worker}"
                self.assertNotIn(
                    spawned_token,
                    "\n".join(reopened._connection.iterdump()),
                )
            finally:
                reopened.close()
                for channel in (ready, results, token_calls):
                    channel.close()
                    channel.join_thread()

    def test_raw_token_is_absent_from_source_backup_restore_and_reopen_results(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source_path = root / "source.sqlite3"
            backup_path = root / "backup" / "scoped-start.sqlite3"
            restored_path = root / "restore" / "scoped-start.sqlite3"
            request = scoped_request()
            canary = "raw-scoped-lease-backup-secret-canary"
            canary_bytes = canary.encode("utf-8")
            token_digest = hashlib.sha256(canary_bytes).hexdigest()

            store = SQLiteEventStore(str(source_path), clock=lambda: ADMITTED_AT)
            try:
                store.append_scoped_task_invocation_admission_v2(
                    request,
                    expected_version=0,
                )
                with (
                    patch.object(store, "_now", return_value=CLAIMED_AT),
                    patch(
                        "quantum_entanglement.store.secrets.token_urlsafe",
                        return_value=canary,
                    ),
                ):
                    result = store.claim_scoped_invocation_start_v3(
                        TENANT_ID,
                        WORKSPACE_ID,
                        INVOCATION_ID,
                        "worker-scoped-backup",
                        lease_seconds=60,
                        expected_version=2,
                    )

                self.assertIs(type(result), ScopedInvocationStartClaimedV3)
                claimed = cast(ScopedInvocationStartClaimedV3, result)
                self.assertEqual(claimed.lease.lease_token, canary)
                self.assertEqual(claimed.receipt.evidence.schema_version, 3)
                self.assertEqual(claimed.receipt.evidence.lease_token_digest, token_digest)
                self.assertNotIn(canary, repr(claimed))
                self.assertNotIn(canary, repr(claimed.lease))
                capability_free = ScopedInvocationStartObservedV3(claimed.receipt)
                self.assertNotIn(
                    canary,
                    json.dumps(capability_free.to_dict(), sort_keys=True),
                )
                self.assertNotIn(canary, "\n".join(store._connection.iterdump()))
                for candidate in sqlite_files(source_path):
                    self.assertNotIn(canary_bytes, candidate.read_bytes())

                created = create_sqlite_backup(
                    source_path,
                    backup_path,
                    clock=lambda: CLAIMED_AT,
                )
                manifest_path = default_manifest_path(backup_path)
                self.assertEqual(verify_sqlite_backup(backup_path), created)
                self.assertEqual(created.table_counts["invocation_admissions"], 1)
                self.assertEqual(created.table_counts["invocation_attempts"], 1)
                self.assertNotIn(canary_bytes, backup_path.read_bytes())
                self.assertNotIn(canary_bytes, manifest_path.read_bytes())
            finally:
                store.close()

            reopened = SQLiteEventStore(str(source_path), clock=lambda: CLAIMED_AT)
            try:
                observed = reopened.read_scoped_invocation_start_v3(
                    TENANT_ID,
                    WORKSPACE_ID,
                    INVOCATION_ID,
                )
                with patch(
                    "quantum_entanglement.store.secrets.token_urlsafe",
                    side_effect=AssertionError("reopen must remain capability-free"),
                ):
                    replay = reopened.claim_scoped_invocation_start_v3(
                        TENANT_ID,
                        WORKSPACE_ID,
                        INVOCATION_ID,
                        "worker-scoped-reopen-canary",
                        lease_seconds=60,
                        expected_version=2,
                    )
                self.assertIs(type(observed), ScopedInvocationStartObservedV3)
                self.assertIs(type(replay), ScopedInvocationStartObservedV3)
                typed_observed = cast(ScopedInvocationStartObservedV3, observed)
                typed_replay = cast(ScopedInvocationStartObservedV3, replay)
                self.assertEqual(typed_replay.receipt, typed_observed.receipt)
                self.assertEqual(
                    typed_observed.receipt.evidence.lease_token_digest,
                    token_digest,
                )
                self.assertFalse(hasattr(typed_observed, "lease"))
                self.assertFalse(hasattr(typed_replay, "lease"))
                self.assertNotIn(canary, repr(typed_observed))
                self.assertNotIn(canary, "\n".join(reopened._connection.iterdump()))
            finally:
                reopened.close()

            restored = restore_sqlite_backup(
                backup_path,
                restored_path,
                manifest_path=manifest_path,
            )
            self.assertEqual(restored, created)
            restored_store = SQLiteEventStore(str(restored_path), clock=lambda: CLAIMED_AT)
            try:
                restored_observation = restored_store.read_scoped_invocation_start_v3(
                    TENANT_ID,
                    WORKSPACE_ID,
                    INVOCATION_ID,
                )
                self.assertIs(
                    type(restored_observation),
                    ScopedInvocationStartObservedV3,
                )
                typed_restored = cast(
                    ScopedInvocationStartObservedV3,
                    restored_observation,
                )
                self.assertEqual(
                    typed_restored.receipt.evidence.lease_token_digest,
                    token_digest,
                )
                self.assertFalse(hasattr(typed_restored, "lease"))
                self.assertNotIn(canary, repr(typed_restored))
                self.assertNotIn(
                    canary,
                    "\n".join(restored_store._connection.iterdump()),
                )
            finally:
                restored_store.close()

            for candidate in (*sqlite_files(source_path), *sqlite_files(restored_path)):
                self.assertNotIn(canary_bytes, candidate.read_bytes())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
