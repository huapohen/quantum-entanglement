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
    InvocationIntegrityError,
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


def persisted_timestamp(seconds):
    return f"2026-08-20T00:00:{seconds:02d}.000000Z"


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

    def _seed_second_running_attempt(self):
        token_digest = "2" * 64
        self.store._connection.execute(
            """
            INSERT INTO invocation_attempts (
                attempt_id, invocation_id, attempt_number, lease_epoch,
                worker_id, lease_token_digest, status, started_at,
                heartbeat_at, lease_expires_at
            ) VALUES (
                'attempt-2', 'invocation-1', 2, 2,
                'worker-2', ?, 'running', ?, ?, ?
            )
            """,
            (
                token_digest,
                persisted_timestamp(0),
                persisted_timestamp(0),
                persisted_timestamp(10),
            ),
        )
        self.store._connection.execute(
            """
            UPDATE invocation_jobs
            SET status = 'running', attempts_started = 2, lease_epoch = 2,
                lease_owner = 'worker-2', lease_token_digest = ?,
                lease_expires_at = ?, heartbeat_at = ?, updated_at = ?,
                finished_at = NULL
            WHERE invocation_id = 'invocation-1'
            """,
            (
                token_digest,
                persisted_timestamp(10),
                persisted_timestamp(0),
                persisted_timestamp(0),
            ),
        )

    def test_default_in_memory_store_is_usable_without_wal(self):
        with SQLiteInvocationAttemptStore(clock=self.clock) as memory_store:
            self.assertEqual(memory_store.schema_version(), 2)
            queued = memory_store.enqueue(job_spec(invocation_id="memory-invocation"))
            self.assertEqual(queued.status, InvocationStatus.QUEUED)

    def test_lease_duration_must_advance_durable_timestamp(self):
        self.store.enqueue(job_spec())
        before = tuple(self.store._connection.iterdump())

        with self.assertRaisesRegex(ValueError, "durable timestamp precision"):
            self.store.claim("invocation-1", "worker", lease_seconds=0.0000001)

        self.assertEqual(tuple(self.store._connection.iterdump()), before)
        self.assertEqual(len(self.store.attempts("invocation-1")), 0)

        lease = self.store.claim("invocation-1", "worker", lease_seconds=0.000001)
        self.assertIsNotNone(lease)
        self.assertGreater(lease.lease_expires_at, lease.claimed_at)

    def test_migration_is_versioned_reopenable_and_coexists_with_event_store(self):
        self.assertEqual(self.store.schema_version(), 2)
        self.store.close()

        event_store = SQLiteEventStore(self.path)
        stored = event_store.append(DomainEvent("session:s1", "created", {}, "actor"))
        reopened = SQLiteInvocationAttemptStore(self.path, clock=self.clock)

        self.assertEqual(reopened.schema_version(), 3)
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

        # Restore the disposable database in strict reverse dependency order, then
        # let the normal runner rebuild a continuous ledger.
        connection = sqlite3.connect(self.path)
        for filename in (
            "0002_artifacts.down.sql",
            "0001_invocation_attempts.down.sql",
        ):
            down = (
                importlib.resources.files("quantum_entanglement.migrations")
                .joinpath(filename)
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

    def test_heartbeat_recovery_fences_stale_worker_and_quarantines_retry(self):
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
        observed = self.store.get(old.invocation_id)
        self.assertEqual(observed.lease_token_digest, stored_digest)
        self.assertEqual(observed.heartbeat_at, "2026-08-20T00:00:00.000000Z")
        self.assertNotIn(old.lease_token, repr(observed))
        self.clock.set(timestamp(5))
        self.assertTrue(self.store.heartbeat(old, lease_seconds=20))
        heartbeat_observation = self.store.get(old.invocation_id)
        self.assertEqual(heartbeat_observation.heartbeat_at, "2026-08-20T00:00:05.000000Z")
        self.assertEqual(heartbeat_observation.lease_token_digest, stored_digest)

        self.clock.set(timestamp(24))
        before_expiry = self.store.recover_expired()
        self.assertEqual(before_expiry.recovered_count, 0)
        self.clock.set(timestamp(25))
        recovered = self.store.recover_expired()
        self.assertEqual(recovered.requeued, ("invocation-1",))
        self.assertFalse(self.store.heartbeat(old, lease_seconds=10))
        self.assertFalse(self.store.complete(old, result_ref="event:old"))

        self.assertIsNone(self.store.claim("invocation-1", "worker-b", lease_seconds=10))
        self.assertIsNone(self.store.claim_next("worker-b", lease_seconds=10))

        job = self.store.get("invocation-1")
        attempts = self.store.attempts("invocation-1")
        self.assertEqual(job.status, InvocationStatus.QUEUED)
        self.assertIsNone(job.result_ref)
        self.assertEqual([item.status for item in attempts], [AttemptStatus.EXPIRED])
        self.assertNotEqual(attempts[0].lease_token_digest, old.lease_token)

    def test_terminal_cas_rejects_completion_at_exact_expiry(self):
        self.store.enqueue(job_spec())
        lease = self.store.claim("invocation-1", "worker", lease_seconds=10)

        self.clock.set(timestamp(10))
        self.assertFalse(self.store.complete(lease))
        recovered = self.store.recover_expired()
        self.assertEqual(recovered.requeued, ("invocation-1",))

    def test_claim_fences_expired_candidate_without_reclaiming_it(self):
        self.store.enqueue(job_spec(max_attempts=3))
        expired = self.store.claim("invocation-1", "worker-a", lease_seconds=1)
        self.clock.set(timestamp(1))

        self.assertIsNone(self.store.claim("invocation-1", "worker-b", lease_seconds=10))

        self.assertEqual(self.store.get("invocation-1").status, InvocationStatus.QUEUED)
        self.assertEqual(
            [attempt.status for attempt in self.store.attempts("invocation-1")],
            [AttemptStatus.EXPIRED],
        )
        self.assertFalse(self.store.complete(expired, result_ref="event:stale"))

    def test_owned_row_query_rejects_incompatible_connection_factory(self):
        self.store.enqueue(job_spec())
        lease = self.store.claim("invocation-1", "worker", lease_seconds=10)
        self.store._connection.row_factory = None
        try:
            with self.assertRaisesRegex(TypeError, "must return sqlite3.Row"):
                self.store._active_owned_row(
                    self.store._connection,
                    lease,
                    timestamp(1),
                )
        finally:
            self.store._connection.row_factory = sqlite3.Row

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

    def test_explicit_failure_schedule_does_not_authorize_retry(self):
        self.store.enqueue(job_spec(max_attempts=2))
        first = self.store.claim("invocation-1", "worker-a", lease_seconds=20)
        self.clock.set(timestamp(1))
        self.assertTrue(self.store.fail(first, "transient", retry_at=timestamp(10)))

        queued = self.store.get("invocation-1")
        self.assertEqual(queued.status, InvocationStatus.QUEUED)
        self.clock.set(timestamp(9))
        self.assertIsNone(self.store.claim("invocation-1", "worker-b", lease_seconds=10))
        self.clock.set(timestamp(10))
        self.assertIsNone(self.store.claim("invocation-1", "worker-b", lease_seconds=10))

        quarantined = self.store.get("invocation-1")
        self.assertEqual(quarantined.status, InvocationStatus.QUEUED)
        self.assertEqual(quarantined.last_error, "transient")
        self.clock.set(timestamp(12))
        self.assertIsNone(self.store.claim("invocation-1", "worker-c", lease_seconds=10))
        self.assertEqual(
            [item.status for item in self.store.attempts("invocation-1")],
            [AttemptStatus.FAILED],
        )

    def test_claim_next_skips_high_priority_effect_unknown_job(self):
        self.store.enqueue(job_spec(max_attempts=3, priority=100))
        attempted = self.store.claim("invocation-1", "worker-a", lease_seconds=10)
        self.assertTrue(self.store.fail(attempted, "unknown effect", retry_at=T0))
        self.store.enqueue(
            job_spec(
                invocation_id="invocation-fresh",
                task_id="task-fresh",
                idempotency_key="invoke:fresh",
                priority=1,
            )
        )

        fresh = self.store.claim_next("worker-b", lease_seconds=10)

        self.assertEqual(fresh.invocation_id, "invocation-fresh")
        self.assertEqual(self.store.get("invocation-1").attempts_started, 1)
        self.assertEqual(len(self.store.attempts("invocation-1")), 1)

    def test_orphan_lease_epoch_cannot_be_observed_or_claimed_as_first_attempt(self):
        self.store.enqueue(job_spec())
        self.store._connection.execute(
            "UPDATE invocation_jobs SET lease_epoch = 1 WHERE invocation_id = ?",
            ("invocation-1",),
        )
        before = tuple(self.store._connection.iterdump())

        with self.assertRaisesRegex(InvocationIntegrityError, "persisted invocation job"):
            self.store.get("invocation-1")
        with self.assertRaisesRegex(InvocationIntegrityError, "persisted invocation job"):
            self.store.recovery_snapshot_for_task("session-1", "task-1")
        with self.assertRaisesRegex(InvocationIntegrityError, "persisted invocation job"):
            self.store.claim("invocation-1", "worker", lease_seconds=10)

        self.assertEqual(tuple(self.store._connection.iterdump()), before)
        self.assertEqual(len(self.store.attempts("invocation-1")), 0)

    def test_queued_partial_lease_cannot_be_observed_recovered_or_claimed(self):
        self.store.enqueue(job_spec())
        self.store._connection.execute("PRAGMA ignore_check_constraints = ON")
        try:
            self.store._connection.execute(
                "UPDATE invocation_jobs SET lease_owner = ? WHERE invocation_id = ?",
                ("orphan-worker", "invocation-1"),
            )
        finally:
            self.store._connection.execute("PRAGMA ignore_check_constraints = OFF")
        before = tuple(self.store._connection.iterdump())

        with self.assertRaisesRegex(InvocationIntegrityError, "persisted invocation job"):
            self.store.get("invocation-1")
        with self.assertRaisesRegex(InvocationIntegrityError, "persisted invocation job"):
            self.store.recovery_snapshot_for_task("session-1", "task-1")
        with self.assertRaisesRegex(InvocationIntegrityError, "persisted invocation job"):
            self.store.claim("invocation-1", "worker", lease_seconds=10)

        self.assertEqual(tuple(self.store._connection.iterdump()), before)
        self.assertEqual(len(self.store.attempts("invocation-1")), 0)

    def test_non_succeeded_result_reference_cannot_cross_first_claim(self):
        self.store.enqueue(job_spec())
        self.store._connection.execute(
            "UPDATE invocation_jobs SET result_ref = ? WHERE invocation_id = ?",
            ("result:unexpected", "invocation-1"),
        )
        before = tuple(self.store._connection.iterdump())

        with self.assertRaisesRegex(InvocationIntegrityError, "persisted invocation job"):
            self.store.get("invocation-1")
        with self.assertRaisesRegex(InvocationIntegrityError, "persisted invocation job"):
            self.store.claim("invocation-1", "worker", lease_seconds=10)

        self.assertEqual(tuple(self.store._connection.iterdump()), before)
        self.assertEqual(len(self.store.attempts("invocation-1")), 0)

    def test_first_claim_cas_rejects_epoch_change_after_candidate_read(self):
        self.store.enqueue(job_spec())
        original = SQLiteInvocationAttemptStore._row_to_job
        changed = False

        def change_epoch_after_decode(row):
            nonlocal changed
            job = original(row)
            if not changed:
                changed = True
                self.store._connection.execute(
                    "UPDATE invocation_jobs SET lease_epoch = 1 WHERE invocation_id = ?",
                    ("invocation-1",),
                )
            return job

        with patch.object(
            SQLiteInvocationAttemptStore,
            "_row_to_job",
            side_effect=change_epoch_after_decode,
        ):
            with self.assertRaisesRegex(InvocationIntegrityError, "candidate changed"):
                self.store.claim("invocation-1", "worker", lease_seconds=10)

        self.assertTrue(changed)
        self.assertEqual(len(self.store.attempts("invocation-1")), 0)
        raw = self.store._connection.execute(
            "SELECT attempts_started, lease_epoch FROM invocation_jobs WHERE invocation_id = ?",
            ("invocation-1",),
        ).fetchone()
        self.assertEqual(tuple(raw), (0, 0))

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
            job_spec(max_attempts=1 << 63)
        with self.assertRaises(ValueError):
            job_spec(available_at="2026-08-20 00:00:00Z")
        with self.assertRaises(ValueError):
            job_spec(available_at="2026-08-20T00:00:00-00:00")
        with self.assertRaises(ValueError):
            invocation_payload_digest({"notFinite": float("nan")})
        with self.assertRaises(ValueError):
            job_spec(agent_id="agent\ncontrol")
        with self.assertRaises(ValueError):
            job_spec(task_id="t" * 4_097)
        self.store.enqueue(job_spec())
        for seconds in (0, -1, float("nan"), float("inf")):
            with self.subTest(seconds=seconds):
                with self.assertRaises(ValueError):
                    self.store.claim("invocation-1", "worker", lease_seconds=seconds)
        for invalid_limit in (True, False, 1.0, "1", None):
            with self.subTest(recovery_limit=invalid_limit):
                with self.assertRaises(TypeError):
                    self.store.recover_expired(limit=invalid_limit)  # type: ignore[arg-type]
        for invalid_limit in (-1, 0, 1_001):
            with self.subTest(recovery_limit=invalid_limit):
                with self.assertRaises(ValueError):
                    self.store.recover_expired(limit=invalid_limit)
        self.assertEqual(self.store.get("invocation-1").status, InvocationStatus.QUEUED)

    def test_write_text_contract_rejects_recovery_poison_before_state_change(self):
        self.store.enqueue(job_spec())
        queued = tuple(self.store._connection.iterdump())
        for worker_id in ("worker\ncontrol", "w" * 4_097):
            with self.subTest(worker_id_length=len(worker_id)):
                with self.assertRaises(ValueError):
                    self.store.claim("invocation-1", worker_id, lease_seconds=10)
                self.assertEqual(tuple(self.store._connection.iterdump()), queued)

        lease = self.store.claim("invocation-1", "worker", lease_seconds=10)
        running = tuple(self.store._connection.iterdump())
        for result_ref in ("result\ncontrol", "r" * 16_385):
            with self.subTest(result_ref_length=len(result_ref)):
                with self.assertRaises(ValueError):
                    self.store.complete(lease, result_ref=result_ref)
                self.assertEqual(tuple(self.store._connection.iterdump()), running)
        for error in ("error\ncontrol", "e" * 16_385):
            with self.subTest(error_length=len(error)):
                with self.assertRaises(ValueError):
                    self.store.fail(lease, error)
                self.assertEqual(tuple(self.store._connection.iterdump()), running)

    def test_exact_text_boundaries_round_trip_through_recovery_snapshot(self):
        self.store.enqueue(job_spec())
        worker_id = "w" * 4_096
        result_ref = "r" * 16_384

        lease = self.store.claim("invocation-1", worker_id, lease_seconds=10)
        self.assertTrue(self.store.complete(lease, result_ref=result_ref))
        snapshot = self.store.recovery_snapshot_for_task("session-1", "task-1")

        self.assertEqual(snapshot.job.result_ref, result_ref)
        self.assertEqual(snapshot.current_attempt.worker_id, worker_id)
        self.assertEqual(snapshot.current_attempt.result_ref, result_ref)

    def test_persisted_job_types_and_timestamps_fail_closed(self):
        self.store.enqueue(job_spec())
        corruptions = (
            ("priority", 50.5),
            ("available_at", T0),
            ("payload_digest", b"0" * 64),
        )
        for column, value in corruptions:
            with self.subTest(column=column):
                self.store._connection.execute(
                    f"UPDATE invocation_jobs SET {column} = ? WHERE invocation_id = ?",
                    (value, "invocation-1"),
                )
                with self.assertRaisesRegex(
                    InvocationIntegrityError,
                    "persisted invocation job is malformed",
                ):
                    self.store.get("invocation-1")
                self.store._connection.execute(
                    f"UPDATE invocation_jobs SET {column} = ? WHERE invocation_id = ?",
                    (
                        {
                            "priority": 50,
                            "available_at": "2026-08-20T00:00:00.000000Z",
                            "payload_digest": job_spec().payload_digest,
                        }[column],
                        "invocation-1",
                    ),
                )

    def test_persisted_job_cross_field_semantics_fail_closed(self):
        self.store.enqueue(job_spec())
        self.store._connection.execute(
            "UPDATE invocation_jobs SET updated_at = ? WHERE invocation_id = ?",
            ("2026-08-19T23:59:59.000000Z", "invocation-1"),
        )
        with self.assertRaisesRegex(InvocationIntegrityError, "persisted invocation job"):
            self.store.get("invocation-1")
        self.store._connection.execute(
            "UPDATE invocation_jobs SET updated_at = ? WHERE invocation_id = ?",
            (persisted_timestamp(0), "invocation-1"),
        )

        self.store._connection.execute(
            """
            UPDATE invocation_jobs SET status = 'failed', finished_at = ?
            WHERE invocation_id = ?
            """,
            (persisted_timestamp(0), "invocation-1"),
        )
        with self.assertRaisesRegex(InvocationIntegrityError, "persisted invocation job"):
            self.store.get("invocation-1")
        self.store._connection.execute(
            """
            UPDATE invocation_jobs SET status = 'queued', finished_at = NULL
            WHERE invocation_id = ?
            """,
            ("invocation-1",),
        )

        self.store._connection.execute(
            """
            UPDATE invocation_jobs
            SET status = 'running', lease_owner = 'worker', lease_token_digest = ?,
                lease_expires_at = ?, heartbeat_at = ?
            WHERE invocation_id = ?
            """,
            ("0" * 64, persisted_timestamp(10), persisted_timestamp(0), "invocation-1"),
        )
        with self.assertRaisesRegex(InvocationIntegrityError, "persisted invocation job"):
            self.store.get("invocation-1")

    def test_persisted_attempt_types_fail_with_stable_integrity_error(self):
        self.store.enqueue(job_spec())
        self.store.claim("invocation-1", "worker", lease_seconds=10)
        self.store._connection.execute(
            "UPDATE invocation_attempts SET attempt_number = ? WHERE invocation_id = ?",
            (b"1", "invocation-1"),
        )

        with self.assertRaisesRegex(
            InvocationIntegrityError,
            "persisted invocation attempt is malformed",
        ):
            self.store.attempts("invocation-1")

    def test_mutations_reject_corrupt_job_scalars_without_state_change(self):
        self.store.enqueue(job_spec())
        self.store._connection.execute(
            "UPDATE invocation_jobs SET lease_epoch = ? WHERE invocation_id = ?",
            (0.5, "invocation-1"),
        )
        before = self.store._connection.execute(
            "SELECT * FROM invocation_jobs WHERE invocation_id = ?",
            ("invocation-1",),
        ).fetchone()

        with self.assertRaisesRegex(
            InvocationIntegrityError,
            "persisted invocation job is malformed",
        ):
            self.store.claim("invocation-1", "worker", lease_seconds=10)

        after = self.store._connection.execute(
            "SELECT * FROM invocation_jobs WHERE invocation_id = ?",
            ("invocation-1",),
        ).fetchone()
        self.assertEqual(tuple(after), tuple(before))
        self.assertEqual(
            self.store._connection.execute("SELECT COUNT(*) FROM invocation_attempts").fetchone()[
                0
            ],
            0,
        )

    def test_active_lease_mutations_validate_the_complete_owned_row(self):
        self.store.enqueue(job_spec())
        lease = self.store.claim("invocation-1", "worker", lease_seconds=10)
        self.store._connection.execute(
            "UPDATE invocation_jobs SET priority = ? WHERE invocation_id = ?",
            (50.5, "invocation-1"),
        )
        before = self.store._connection.execute(
            "SELECT * FROM invocation_jobs WHERE invocation_id = ?",
            ("invocation-1",),
        ).fetchone()

        operations = (
            lambda: self.store.heartbeat(lease, lease_seconds=10),
            lambda: self.store.complete(lease),
            lambda: self.store.fail(lease, "must not persist"),
        )
        for operation in operations:
            with self.subTest(operation=operation):
                with self.assertRaises(InvocationIntegrityError):
                    operation()

        after = self.store._connection.execute(
            "SELECT * FROM invocation_jobs WHERE invocation_id = ?",
            ("invocation-1",),
        ).fetchone()
        self.assertEqual(tuple(after), tuple(before))
        self.assertEqual(
            self.store._connection.execute(
                "SELECT status FROM invocation_attempts WHERE invocation_id = ?",
                ("invocation-1",),
            ).fetchone()[0],
            AttemptStatus.RUNNING.value,
        )

    def test_expiry_recovery_validates_rows_before_changing_attempt_state(self):
        self.store.enqueue(job_spec())
        self.store.claim("invocation-1", "worker", lease_seconds=1)
        self.clock.set(timestamp(1))
        self.store._connection.execute(
            "UPDATE invocation_jobs SET max_attempts = ? WHERE invocation_id = ?",
            (3.5, "invocation-1"),
        )
        before_job = tuple(
            self.store._connection.execute(
                "SELECT * FROM invocation_jobs WHERE invocation_id = ?",
                ("invocation-1",),
            ).fetchone()
        )
        before_attempt = tuple(
            self.store._connection.execute(
                "SELECT * FROM invocation_attempts WHERE invocation_id = ?",
                ("invocation-1",),
            ).fetchone()
        )

        with self.assertRaises(InvocationIntegrityError):
            self.store.recover_expired()

        after_job = tuple(
            self.store._connection.execute(
                "SELECT * FROM invocation_jobs WHERE invocation_id = ?",
                ("invocation-1",),
            ).fetchone()
        )
        after_attempt = tuple(
            self.store._connection.execute(
                "SELECT * FROM invocation_attempts WHERE invocation_id = ?",
                ("invocation-1",),
            ).fetchone()
        )
        self.assertEqual(after_job, before_job)
        self.assertEqual(after_attempt, before_attempt)

    def test_owned_mutations_reject_attempt_ownership_drift_before_writing(self):
        self.store.enqueue(job_spec())
        lease = self.store.claim("invocation-1", "worker", lease_seconds=10)
        self.store._connection.execute(
            "UPDATE invocation_attempts SET worker_id = ? WHERE invocation_id = ?",
            ("different-worker", "invocation-1"),
        )
        before_job = tuple(
            self.store._connection.execute(
                "SELECT * FROM invocation_jobs WHERE invocation_id = ?",
                ("invocation-1",),
            ).fetchone()
        )
        before_attempt = tuple(
            self.store._connection.execute(
                "SELECT * FROM invocation_attempts WHERE invocation_id = ?",
                ("invocation-1",),
            ).fetchone()
        )

        operations = (
            lambda: self.store.heartbeat(lease, lease_seconds=10),
            lambda: self.store.complete(lease),
            lambda: self.store.fail(lease, "must not persist"),
        )
        for operation in operations:
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(InvocationIntegrityError, "ownership records disagree"):
                    operation()

        self.assertEqual(
            tuple(
                self.store._connection.execute(
                    "SELECT * FROM invocation_jobs WHERE invocation_id = ?",
                    ("invocation-1",),
                ).fetchone()
            ),
            before_job,
        )
        self.assertEqual(
            tuple(
                self.store._connection.execute(
                    "SELECT * FROM invocation_attempts WHERE invocation_id = ?",
                    ("invocation-1",),
                ).fetchone()
            ),
            before_attempt,
        )

    def test_recovery_rejects_missing_owned_attempt_without_partial_state_change(self):
        self.store.enqueue(job_spec())
        self.store.claim("invocation-1", "worker", lease_seconds=1)
        self.store._connection.execute(
            "DELETE FROM invocation_attempts WHERE invocation_id = ?",
            ("invocation-1",),
        )
        self.clock.set(timestamp(1))
        before_job = tuple(
            self.store._connection.execute(
                "SELECT * FROM invocation_jobs WHERE invocation_id = ?",
                ("invocation-1",),
            ).fetchone()
        )

        with self.assertRaisesRegex(InvocationIntegrityError, "exactly one owned attempt"):
            self.store.recover_expired()

        self.assertEqual(
            tuple(
                self.store._connection.execute(
                    "SELECT * FROM invocation_jobs WHERE invocation_id = ?",
                    ("invocation-1",),
                ).fetchone()
            ),
            before_job,
        )

    def test_attempt_pages_are_bounded_ordered_and_cursor_based(self):
        self.store.enqueue(job_spec(max_attempts=4))
        self.store._connection.execute(
            """
            UPDATE invocation_jobs SET attempts_started = 3, lease_epoch = 3
            WHERE invocation_id = ?
            """,
            ("invocation-1",),
        )
        self.store._connection.executemany(
            """
            INSERT INTO invocation_attempts (
                attempt_id, invocation_id, attempt_number, lease_epoch,
                worker_id, lease_token_digest, status, started_at,
                heartbeat_at, lease_expires_at, finished_at, error
            ) VALUES (?, 'invocation-1', ?, ?, ?, ?, 'failed', ?, ?, ?, ?, ?)
            """,
            (
                (
                    f"attempt-{number}",
                    number,
                    number,
                    f"worker-{number}",
                    f"{number:064x}",
                    persisted_timestamp(0),
                    persisted_timestamp(0),
                    persisted_timestamp(10),
                    persisted_timestamp(number),
                    f"failure-{number}",
                )
                for number in range(1, 4)
            ),
        )

        first = self.store.attempts_page("invocation-1", limit=2)
        second = self.store.attempts_page(
            "invocation-1",
            after_attempt_number=first[-1].attempt_number,
            limit=2,
        )
        self.assertEqual([item.attempt_number for item in first], [1, 2])
        self.assertEqual([item.attempt_number for item in second], [3])
        self.assertEqual(self.store.attempts_page("invocation-1", 3, 2), ())

    def test_attempt_page_bounds_reject_bool_negative_and_unbounded_limits(self):
        for invalid in (True, False, 1.0, "0", None):
            with self.subTest(cursor=invalid):
                with self.assertRaises(TypeError):
                    self.store.attempts_page(
                        "invocation-1",
                        invalid,  # type: ignore[arg-type]
                        1,
                    )
        for invalid in (-1, 1 << 63):
            with self.subTest(cursor=invalid):
                with self.assertRaises(ValueError):
                    self.store.attempts_page("invocation-1", invalid, 1)
        for invalid in (True, False, 1.0, "1", None):
            with self.subTest(limit=invalid):
                with self.assertRaises(TypeError):
                    self.store.attempts_page(
                        "invocation-1",
                        0,
                        invalid,  # type: ignore[arg-type]
                    )
        for invalid in (-1, 0, 1_001):
            with self.subTest(limit=invalid):
                with self.assertRaises(ValueError):
                    self.store.attempts_page("invocation-1", 0, invalid)

    def test_attempt_page_query_contains_a_sql_limit(self):
        statements = []
        self.store._connection.set_trace_callback(statements.append)
        try:
            self.store.attempts_page("invocation-1", limit=7)
        finally:
            self.store._connection.set_trace_callback(None)

        self.assertTrue(
            any(
                "FROM INVOCATION_ATTEMPTS" in statement.upper() and "LIMIT 7" in statement.upper()
                for statement in statements
            )
        )

    def test_recovery_snapshot_is_bounded_and_cross_validates_current_attempt(self):
        self.store.enqueue(job_spec())
        lease = self.store.claim("invocation-1", "worker", lease_seconds=10)
        statements = []
        self.store._connection.set_trace_callback(statements.append)
        try:
            snapshot = self.store.recovery_snapshot_for_task("session-1", "task-1")
        finally:
            self.store._connection.set_trace_callback(None)

        self.assertEqual(snapshot.job.status, InvocationStatus.RUNNING)
        self.assertEqual(snapshot.current_attempt.attempt_id, lease.attempt_id)
        self.assertEqual(snapshot.attempt_count, 1)
        normalized = tuple(" ".join(statement.upper().split()) for statement in statements)
        self.assertIn("BEGIN", normalized)
        self.assertIn("COMMIT", normalized)
        decoded_queries = tuple(
            statement
            for statement in normalized
            if statement.startswith("SELECT * FROM INVOCATION_")
        )
        self.assertEqual(len(decoded_queries), 2)
        self.assertIn("LIMIT 2", decoded_queries[0])
        self.assertIn("ORDER BY ATTEMPT_NUMBER LIMIT 1001", decoded_queries[1])
        self.assertFalse(any("COUNT(*) AS ATTEMPT_COUNT" in statement for statement in normalized))

    def test_recovery_snapshot_decodes_and_rejects_unsafe_historical_attempts(self):
        self.store.enqueue(job_spec(max_attempts=2))
        first = self.store.claim("invocation-1", "worker-1", lease_seconds=10)
        self.assertTrue(self.store.fail(first, "retry", retry_at=T0))
        self._seed_second_running_attempt()

        self.store._connection.execute(
            """
            UPDATE invocation_attempts
            SET status = 'running', finished_at = NULL
            WHERE invocation_id = ? AND attempt_number = 1
            """,
            ("invocation-1",),
        )

        with self.assertRaisesRegex(InvocationIntegrityError, "historical invocation attempt"):
            self.store.recovery_snapshot_for_task("session-1", "task-1")

    def test_recovery_snapshot_strictly_decodes_historical_attempt_semantics(self):
        self.store.enqueue(job_spec(max_attempts=2))
        first = self.store.claim("invocation-1", "worker-1", lease_seconds=10)
        self.assertTrue(self.store.fail(first, "retry", retry_at=T0))
        self._seed_second_running_attempt()
        corruptions = (
            ("result_ref", "result:unexpected", None),
            ("heartbeat_at", "2026-08-19T23:59:59.000000Z", persisted_timestamp(0)),
            ("lease_expires_at", "2026-08-19T23:59:59.000000Z", persisted_timestamp(10)),
            ("finished_at", "2026-08-19T23:59:59.000000Z", persisted_timestamp(0)),
        )
        for column, corrupted, restored in corruptions:
            with self.subTest(column=column):
                self.store._connection.execute(
                    f"""
                    UPDATE invocation_attempts SET {column} = ?
                    WHERE invocation_id = ? AND attempt_number = 1
                    """,
                    (corrupted, "invocation-1"),
                )
                with self.assertRaisesRegex(
                    InvocationIntegrityError,
                    "persisted invocation attempt is malformed",
                ):
                    self.store.recovery_snapshot_for_task("session-1", "task-1")
                self.store._connection.execute(
                    f"""
                    UPDATE invocation_attempts SET {column} = ?
                    WHERE invocation_id = ? AND attempt_number = 1
                    """,
                    (restored, "invocation-1"),
                )

    def test_recovery_snapshot_rejects_non_monotonic_historical_epoch(self):
        self.store.enqueue(job_spec(max_attempts=2))
        first = self.store.claim("invocation-1", "worker-1", lease_seconds=10)
        self.assertTrue(self.store.fail(first, "retry", retry_at=T0))
        self._seed_second_running_attempt()

        self.store._connection.execute(
            """
            UPDATE invocation_attempts SET lease_epoch = 3
            WHERE invocation_id = ? AND attempt_number = 1
            """,
            ("invocation-1",),
        )

        with self.assertRaisesRegex(InvocationIntegrityError, "strictly increasing"):
            self.store.recovery_snapshot_for_task("session-1", "task-1")

    def test_recovery_snapshot_rejects_histories_above_the_supported_limit(self):
        self.store.enqueue(job_spec(max_attempts=1_001))
        self.store._connection.execute(
            """
            UPDATE invocation_jobs
            SET attempts_started = 1001, lease_epoch = 1001
            WHERE invocation_id = ?
            """,
            ("invocation-1",),
        )
        self.store._connection.executemany(
            """
            INSERT INTO invocation_attempts (
                attempt_id, invocation_id, attempt_number, lease_epoch,
                worker_id, lease_token_digest, status, started_at,
                heartbeat_at, lease_expires_at, finished_at, error
            ) VALUES (?, 'invocation-1', ?, ?, 'worker', ?, 'failed', ?, ?, ?, ?, 'seeded')
            """,
            (
                (
                    f"attempt-{number}",
                    number,
                    number,
                    "0" * 64,
                    "2026-08-20T00:00:00.000000Z",
                    "2026-08-20T00:00:00.000000Z",
                    "2026-08-20T00:00:01.000000Z",
                    "2026-08-20T00:00:01.000000Z",
                )
                for number in range(1, 1_002)
            ),
        )

        with self.assertRaisesRegex(InvocationIntegrityError, "recovery limit"):
            self.store.recovery_snapshot_for_task("session-1", "task-1")

    def test_recovery_snapshot_remains_consistent_while_wal_writer_advances_heartbeat(self):
        self.store.enqueue(job_spec())
        lease = self.store.claim("invocation-1", "worker", lease_seconds=10)
        second_clock = MutableClock(timestamp(5))
        second = SQLiteInvocationAttemptStore(self.path, clock=second_clock)
        original = SQLiteInvocationAttemptStore._row_to_job
        advanced = False

        def advance_after_job_read(row):
            nonlocal advanced
            job = original(row)
            if not advanced:
                advanced = True
                self.assertTrue(second.heartbeat(lease, lease_seconds=20))
            return job

        try:
            with patch.object(
                SQLiteInvocationAttemptStore,
                "_row_to_job",
                side_effect=advance_after_job_read,
            ):
                snapshot = self.store.recovery_snapshot_for_task("session-1", "task-1")
        finally:
            second.close()

        self.assertTrue(advanced)
        self.assertEqual(snapshot.job.heartbeat_at, "2026-08-20T00:00:00.000000Z")
        self.assertEqual(
            snapshot.current_attempt.heartbeat_at,
            "2026-08-20T00:00:00.000000Z",
        )
        self.assertEqual(
            self.store.get("invocation-1").heartbeat_at,
            "2026-08-20T00:00:05.000000Z",
        )

    def test_recovery_snapshot_rejects_cross_row_drift_without_mutation(self):
        self.store.enqueue(job_spec())
        lease = self.store.claim("invocation-1", "worker", lease_seconds=10)
        before = tuple(self.store._connection.iterdump())
        self.store._connection.execute(
            """
            UPDATE invocation_attempts SET heartbeat_at = ?
            WHERE attempt_id = ?
            """,
            ("2026-08-20T00:00:01.000000Z", lease.attempt_id),
        )
        drifted = tuple(self.store._connection.iterdump())
        with self.assertRaisesRegex(InvocationIntegrityError, "ownership differs"):
            self.store.recovery_snapshot_for_task("session-1", "task-1")
        self.assertEqual(tuple(self.store._connection.iterdump()), drifted)
        self.assertNotEqual(before, drifted)

    def test_recovery_snapshot_distinguishes_a_missing_job(self):
        snapshot = self.store.recovery_snapshot_for_task("session-1", "missing-task")
        self.assertIsNone(snapshot.job)
        self.assertIsNone(snapshot.current_attempt)
        self.assertEqual(snapshot.attempt_count, 0)


if __name__ == "__main__":
    unittest.main()
