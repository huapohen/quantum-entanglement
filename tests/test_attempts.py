import hashlib
import importlib.resources
import multiprocessing
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from quantum_entanglement.attempts import (
    AttemptStatus,
    InvocationConflictError,
    InvocationJobSpec,
    InvocationStatus,
    MigrationDriftError,
    SQLiteInvocationAttemptStore,
    invocation_payload_digest,
)
from quantum_entanglement.events import DomainEvent
from quantum_entanglement.store import SQLiteEventStore

T0 = "2026-08-20T00:00:00Z"


def timestamp(seconds):
    return f"2026-08-20T00:00:{seconds:02d}Z"


class MutableClock:
    def __init__(self, value=T0):
        self.value = value

    def __call__(self):
        return self.value

    def set(self, value):
        self.value = value


def job_spec(**changes):
    values = {
        "session_id": "session-1",
        "plan_id": "plan-1",
        "task_id": "task-1",
        "agent_id": "agent-1",
        "idempotency_key": "invoke:task-1",
        "payload_digest": invocation_payload_digest({"taskId": "task-1", "context": "abc"}),
        "invocation_id": "invocation-1",
        "max_attempts": 3,
    }
    values.update(changes)
    return InvocationJobSpec(**values)


def claim_from_process(path, worker_id, ready_queue, start_event, result_queue):
    store = SQLiteInvocationAttemptStore(path, clock=lambda: T0)
    try:
        ready_queue.put(worker_id)
        if not start_event.wait(timeout=5):
            raise RuntimeError("process claim start barrier timed out")
        lease = store.claim("invocation-1", worker_id, lease_seconds=10)
        result_queue.put((worker_id, lease is not None))
    finally:
        store.close()


class InvocationAttemptStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tempdir.name) / "state.sqlite3")
        self.clock = MutableClock()
        self.store = SQLiteInvocationAttemptStore(self.path, clock=self.clock)

    def tearDown(self):
        self.store.close()
        self.tempdir.cleanup()

    def test_default_in_memory_store_is_usable_without_wal(self):
        with SQLiteInvocationAttemptStore(clock=self.clock) as memory_store:
            self.assertEqual(memory_store.schema_version(), 2)
            queued = memory_store.enqueue(job_spec(invocation_id="memory-invocation"))
            self.assertEqual(queued.status, InvocationStatus.QUEUED)

    def test_migration_is_versioned_reopenable_and_coexists_with_event_store(self):
        self.assertEqual(self.store.schema_version(), 2)
        self.store.close()

        event_store = SQLiteEventStore(self.path)
        stored = event_store.append(DomainEvent("session:s1", "created", {}, "actor"))
        reopened = SQLiteInvocationAttemptStore(self.path, clock=self.clock)

        self.assertEqual(reopened.schema_version(), 2)
        self.assertEqual(stored.sequence, 1)
        self.assertEqual(len(event_store.read_stream("session:s1")), 1)
        reopened.close()
        event_store.close()
        # Keep tearDown idempotent after this test explicitly reopens the store.
        self.store = SQLiteInvocationAttemptStore(self.path, clock=self.clock)

    def test_two_connections_can_initialize_the_same_new_database_concurrently(self):
        path = str(Path(self.tempdir.name) / "concurrent-initialize.sqlite3")
        barrier = threading.Barrier(2)

        def initialize():
            barrier.wait(timeout=2)
            store = SQLiteInvocationAttemptStore(path, clock=self.clock)
            try:
                return store.schema_version()
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            versions = [
                future.result(timeout=3)
                for future in (executor.submit(initialize), executor.submit(initialize))
            ]

        self.assertEqual(versions, [2, 2])

    def test_migration_checksum_drift_fails_closed(self):
        self.store.close()
        connection = sqlite3.connect(self.path)
        connection.execute(
            "UPDATE qe_schema_migrations SET sha256 = ? WHERE version = 1",
            ("0" * 64,),
        )
        connection.commit()
        connection.close()

        with self.assertRaises(MigrationDriftError):
            SQLiteInvocationAttemptStore(self.path, clock=self.clock)

        # Restore the recorded checksum by applying the destructive down migration only
        # against this disposable test database, then let the normal runner rebuild it.
        connection = sqlite3.connect(self.path)
        down = (
            importlib.resources.files("quantum_entanglement.migrations")
            .joinpath("0001_invocation_attempts.down.sql")
            .read_text(encoding="utf-8")
        )
        connection.executescript(down)
        connection.close()
        self.store = SQLiteInvocationAttemptStore(self.path, clock=self.clock)

    def test_enqueue_is_idempotent_but_rejects_changed_identity_or_payload(self):
        spec = job_spec()
        first = self.store.enqueue(spec)
        self.clock.set(timestamp(1))
        retried = self.store.enqueue(
            replace(spec, invocation_id="retry-generated-id"),
        )

        self.assertEqual(first, retried)
        self.assertEqual(first.status, InvocationStatus.QUEUED)
        self.assertEqual(first.created_at, "2026-08-20T00:00:00.000000Z")
        with self.assertRaises(InvocationConflictError):
            self.store.enqueue(replace(spec, agent_id="different-agent"))
        with self.assertRaises(InvocationConflictError):
            self.store.enqueue(replace(spec, available_at=timestamp(10)))
        with self.assertRaises(InvocationConflictError):
            self.store.enqueue(
                job_spec(
                    invocation_id="other-invocation",
                    task_id="other-task",
                    payload_digest=invocation_payload_digest({"changed": True}),
                ),
            )

    def test_claim_next_obeys_availability_then_priority(self):
        self.store.enqueue(
            job_spec(
                invocation_id="low",
                task_id="low",
                idempotency_key="invoke:low",
                priority=1,
            ),
        )
        self.store.enqueue(
            job_spec(
                invocation_id="high",
                task_id="high",
                idempotency_key="invoke:high",
                priority=100,
                available_at=timestamp(5),
            ),
        )

        low = self.store.claim_next("worker", lease_seconds=10)
        self.assertEqual(low.invocation_id, "low")
        self.clock.set(timestamp(1))
        self.assertTrue(self.store.complete(low))
        self.clock.set(timestamp(4))
        self.assertIsNone(self.store.claim_next("worker", lease_seconds=10))
        self.clock.set(timestamp(5))
        high = self.store.claim_next("worker", lease_seconds=10)
        self.assertEqual(high.invocation_id, "high")

    def test_two_independent_connections_have_one_atomic_claim_winner(self):
        self.store.enqueue(job_spec())
        second = SQLiteInvocationAttemptStore(self.path, clock=self.clock)
        barrier = threading.Barrier(2)

        def claim(store, worker):
            barrier.wait(timeout=2)
            return store.claim("invocation-1", worker, lease_seconds=10)

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(claim, self.store, "worker-a"),
                    executor.submit(claim, second, "worker-b"),
                ]
                leases = [future.result(timeout=3) for future in futures]
        finally:
            second.close()

        winners = [lease for lease in leases if lease is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(self.store.get("invocation-1").attempts_started, 1)
        self.assertEqual(len(self.store.attempts("invocation-1")), 1)

    def test_two_processes_have_one_atomic_claim_winner(self):
        self.store.enqueue(job_spec())
        context = multiprocessing.get_context("spawn")
        ready_queue = context.Queue()
        result_queue = context.Queue()
        start_event = context.Event()
        processes = [
            context.Process(
                target=claim_from_process,
                args=(self.path, worker_id, ready_queue, start_event, result_queue),
            )
            for worker_id in ("process-a", "process-b")
        ]

        for process in processes:
            process.start()
        for _ in processes:
            ready_queue.get(timeout=5)
        start_event.set()
        results = [result_queue.get(timeout=5) for _ in processes]
        for process in processes:
            process.join(timeout=5)
            self.assertEqual(process.exitcode, 0)

        self.assertEqual(sum(won for _worker_id, won in results), 1)
        self.assertEqual(self.store.get("invocation-1").attempts_started, 1)
        self.assertEqual(len(self.store.attempts("invocation-1")), 1)

    def test_heartbeat_recovery_and_fencing_reject_stale_worker(self):
        self.store.enqueue(job_spec())
        old = self.store.claim("invocation-1", "worker-a", lease_seconds=10)
        self.assertNotIn(old.lease_token, repr(old))
        connection = sqlite3.connect(self.path)
        stored_digest = connection.execute(
            "SELECT lease_token_digest FROM invocation_jobs WHERE invocation_id = ?",
            (old.invocation_id,),
        ).fetchone()[0]
        connection.close()
        self.assertEqual(
            stored_digest,
            hashlib.sha256(old.lease_token.encode("utf-8")).hexdigest(),
        )
        self.assertNotEqual(stored_digest, old.lease_token)
        self.clock.set(timestamp(5))
        self.assertTrue(self.store.heartbeat(old, lease_seconds=20))

        self.clock.set(timestamp(24))
        before_expiry = self.store.recover_expired()
        self.assertEqual(before_expiry.recovered_count, 0)
        self.clock.set(timestamp(25))
        recovered = self.store.recover_expired()
        self.assertEqual(recovered.requeued, ("invocation-1",))
        self.assertFalse(self.store.heartbeat(old, lease_seconds=10))
        self.assertFalse(self.store.complete(old, result_ref="event:old"))

        new = self.store.claim("invocation-1", "worker-b", lease_seconds=10)
        self.assertGreater(new.fencing_token, old.fencing_token)
        self.assertNotEqual(new.lease_token, old.lease_token)
        self.clock.set(timestamp(26))
        self.assertTrue(self.store.complete(new, result_ref="event:new"))

        job = self.store.get("invocation-1")
        attempts = self.store.attempts("invocation-1")
        self.assertEqual(job.status, InvocationStatus.SUCCEEDED)
        self.assertEqual(job.result_ref, "event:new")
        self.assertEqual(
            [item.status for item in attempts],
            [
                AttemptStatus.EXPIRED,
                AttemptStatus.SUCCEEDED,
            ],
        )
        self.assertNotEqual(attempts[0].lease_token_digest, old.lease_token)

    def test_terminal_cas_rejects_completion_at_exact_expiry(self):
        self.store.enqueue(job_spec())
        lease = self.store.claim("invocation-1", "worker", lease_seconds=10)

        self.clock.set(timestamp(10))
        self.assertFalse(self.store.complete(lease))
        recovered = self.store.recover_expired()
        self.assertEqual(recovered.requeued, ("invocation-1",))

    def test_lease_clock_is_sampled_only_after_write_transaction_begins(self):
        self.store.enqueue(job_spec())
        original_transaction = self.store._transaction

        @contextmanager
        def transaction_at_claim_time():
            with original_transaction() as connection:
                self.clock.set(timestamp(5))
                yield connection

        with patch.object(self.store, "_transaction", transaction_at_claim_time):
            lease = self.store.claim("invocation-1", "worker", lease_seconds=1)

        self.assertEqual(lease.claimed_at, "2026-08-20T00:00:05.000000Z")
        self.assertEqual(lease.lease_expires_at, "2026-08-20T00:00:06.000000Z")

        @contextmanager
        def transaction_at_expiry():
            with original_transaction() as connection:
                self.clock.set(timestamp(6))
                yield connection

        with patch.object(self.store, "_transaction", transaction_at_expiry):
            self.assertFalse(self.store.complete(lease, result_ref="event:late"))

        self.assertEqual(self.store.get("invocation-1").status, InvocationStatus.RUNNING)

    def test_explicit_failure_retries_at_schedule_then_exhausts(self):
        self.store.enqueue(job_spec(max_attempts=2))
        first = self.store.claim("invocation-1", "worker-a", lease_seconds=20)
        self.clock.set(timestamp(1))
        self.assertTrue(self.store.fail(first, "transient", retry_at=timestamp(10)))

        queued = self.store.get("invocation-1")
        self.assertEqual(queued.status, InvocationStatus.QUEUED)
        self.clock.set(timestamp(9))
        self.assertIsNone(self.store.claim("invocation-1", "worker-b", lease_seconds=10))
        self.clock.set(timestamp(10))
        second = self.store.claim("invocation-1", "worker-b", lease_seconds=10)
        self.assertEqual(second.attempt_number, 2)
        self.clock.set(timestamp(11))
        self.assertTrue(self.store.fail(second, "permanent"))

        failed = self.store.get("invocation-1")
        self.assertEqual(failed.status, InvocationStatus.FAILED)
        self.assertEqual(failed.last_error, "permanent")
        self.clock.set(timestamp(12))
        self.assertIsNone(self.store.claim("invocation-1", "worker-c", lease_seconds=10))
        self.assertEqual(
            [item.status for item in self.store.attempts("invocation-1")],
            [AttemptStatus.FAILED, AttemptStatus.FAILED],
        )

    def test_crashed_final_attempt_is_terminally_exhausted(self):
        self.store.enqueue(job_spec(max_attempts=1))
        lease = self.store.claim("invocation-1", "worker-a", lease_seconds=5)

        self.clock.set(timestamp(5))
        summary = self.store.recover_expired()

        self.assertEqual(summary.exhausted, ("invocation-1",))
        self.assertFalse(self.store.complete(lease))
        self.assertEqual(self.store.get("invocation-1").status, InvocationStatus.FAILED)
        self.assertEqual(
            self.store.attempts("invocation-1")[0].status,
            AttemptStatus.EXPIRED,
        )

    def test_invalid_retry_and_lease_inputs_fail_before_state_change(self):
        with self.assertRaises(ValueError):
            job_spec(payload_digest="not-a-digest")
        with self.assertRaises(ValueError):
            job_spec(available_at="2026-08-20 00:00:00Z")
        with self.assertRaises(ValueError):
            job_spec(available_at="2026-08-20T00:00:00-00:00")
        with self.assertRaises(ValueError):
            invocation_payload_digest({"notFinite": float("nan")})
        self.store.enqueue(job_spec())
        for seconds in (0, -1, float("nan"), float("inf")):
            with self.subTest(seconds=seconds):
                with self.assertRaises(ValueError):
                    self.store.claim("invocation-1", "worker", lease_seconds=seconds)
        self.assertEqual(self.store.get("invocation-1").status, InvocationStatus.QUEUED)


if __name__ == "__main__":
    unittest.main()
