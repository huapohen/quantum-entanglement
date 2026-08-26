import hashlib
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from quantum_entanglement.delivery import OutboxMessage, OutboxStatus
from quantum_entanglement.events import DomainEvent
from quantum_entanglement.migrations import (
    MIGRATIONS,
    MigrationDriftError,
    apply_sqlite_migrations,
    current_schema_version,
    migration_text,
    validate_sqlite_schema,
)
from quantum_entanglement.store import SQLiteEventStore

READY = "2026-08-19T00:00:00Z"

LEGACY_AMBIGUITY_SCHEMA = """
CREATE TABLE outbox_ambiguities (
    message_id TEXT NOT NULL,
    lease_token TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    attempt_count INTEGER NOT NULL,
    marked_at TEXT NOT NULL,
    resolution TEXT,
    resolved_at TEXT,
    PRIMARY KEY(message_id, lease_token),
    FOREIGN KEY(message_id)
        REFERENCES outbox(message_id) ON DELETE RESTRICT,
    CHECK(reason_code IN (
        'callback_timeout', 'caller_cancelled',
        'ack_failed', 'lease_expired_after_accept'
    )),
    CHECK(resolution IS NULL OR resolution IN (
        'published', 'retry', 'dead_letter'
    )),
    CHECK(attempt_count > 0)
);
CREATE INDEX idx_outbox_ambiguities_open
    ON outbox_ambiguities(message_id, resolved_at);
"""


class ManualClock:
    def __init__(self, value=READY):
        self.value = value

    def __call__(self):
        return self.value

    def set(self, value):
        self.value = value


def event(key, payload=None):
    return DomainEvent(
        "session:s1",
        "task.dispatch.requested",
        payload or {"taskId": key},
        "orchestrator",
        idempotency_key=key,
    )


def message(message_id="message-1", payload=None, available_at=READY):
    return OutboxMessage(
        "agent-runtime",
        payload or {"taskId": "task-1"},
        headers={"traceparent": "trace-1"},
        message_id=message_id,
        idempotency_key=f"publish:{message_id}",
        available_at=available_at,
        created_at=READY,
    )


class TransactionalDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tempdir.name) / "events.sqlite3")
        self.clock = ManualClock()
        self.store = SQLiteEventStore(self.path, clock=self.clock)

    def tearDown(self):
        self.store.close()
        self.tempdir.cleanup()

    def run_after_write_lock_wait(self, operation, while_blocked):
        """Run an operation only after proving its BEGIN IMMEDIATE is blocked."""

        blocker = sqlite3.connect(self.path, isolation_level=None)
        blocker.execute("BEGIN IMMEDIATE")
        begin_attempted = threading.Event()
        outcome = {}

        def trace(statement):
            if statement.strip().upper() == "BEGIN IMMEDIATE":
                begin_attempted.set()

        def invoke():
            try:
                outcome["result"] = operation()
            except BaseException as caught:
                outcome["error"] = caught

        self.store._connection.set_trace_callback(trace)
        worker = threading.Thread(target=invoke, daemon=True)
        worker.start()
        try:
            self.assertTrue(begin_attempted.wait(1), "operation never attempted its write lock")
            self.assertTrue(worker.is_alive(), "operation did not wait for the held write lock")
            while_blocked()
        finally:
            if blocker.in_transaction:
                blocker.execute("COMMIT")
            blocker.close()
            worker.join(1)
            self.store._connection.set_trace_callback(None)

        self.assertFalse(worker.is_alive(), "operation did not finish after releasing write lock")
        if "error" in outcome:
            raise outcome["error"]
        return outcome["result"]

    def create_legacy_v2_store(self, path):
        def apply_only_v2(connection, *, clock, _process_guard=None):
            return apply_sqlite_migrations(
                connection,
                migrations=MIGRATIONS[:2],
                clock=clock,
                _process_guard=_process_guard,
            )

        with mock.patch(
            "quantum_entanglement.store.apply_sqlite_migrations",
            side_effect=apply_only_v2,
        ):
            legacy = SQLiteEventStore(path, clock=self.clock)
        legacy._connection.executescript(LEGACY_AMBIGUITY_SCHEMA)
        return legacy

    def test_event_and_outbox_commit_together_and_retry_idempotently(self):
        outgoing = message()
        first_event, first_messages = self.store.append_with_outbox(
            event("dispatch-1"), (outgoing,), expected_version=0
        )
        retried_event, retried_messages = self.store.append_with_outbox(
            event("dispatch-1"), (outgoing,), expected_version=0
        )

        self.assertEqual(first_event.global_position, retried_event.global_position)
        self.assertEqual(first_messages, retried_messages)
        self.assertEqual(len(self.store.read_outbox()), 1)
        self.assertEqual(self.store.stream_version("session:s1"), 1)

    def test_outbox_ambiguity_forward_migration_upgrades_v2_database(self):
        upgrade_path = str(Path(self.tempdir.name) / "upgrade-from-v2.sqlite3")
        previous = sqlite3.connect(upgrade_path, isolation_level=None)
        previous.row_factory = sqlite3.Row
        apply_sqlite_migrations(previous, migrations=MIGRATIONS[:2], clock=self.clock)
        self.assertEqual(current_schema_version(previous), 2)
        previous.close()

        upgraded = SQLiteEventStore(upgrade_path, clock=self.clock)
        try:
            self.assertEqual(current_schema_version(upgraded._connection), len(MIGRATIONS))
            columns = {
                row["name"]
                for row in upgraded._connection.execute(
                    "PRAGMA table_info(outbox_ambiguities)"
                ).fetchall()
            }
            self.assertIn("lease_token_digest", columns)
            self.assertNotIn("lease_token", columns)
            self.assertEqual(
                upgraded._connection.execute("PRAGMA foreign_key_check").fetchall(),
                [],
            )
            self.assertEqual(
                upgraded._connection.execute("PRAGMA integrity_check").fetchone()[0],
                "ok",
            )
        finally:
            upgraded.close()

    def test_schema_validator_covers_outbox_ambiguity_migration(self):
        self.assertEqual(validate_sqlite_schema(self.store._connection), len(MIGRATIONS))

        self.store._connection.execute("DROP INDEX idx_outbox_ambiguities_opened")
        with self.assertRaisesRegex(
            MigrationDriftError,
            "idx_outbox_ambiguities_opened.*missing",
        ):
            validate_sqlite_schema(self.store._connection)

    def test_outbox_migration_rebuilds_legacy_rows_without_plaintext_tokens(self):
        upgrade_path = str(Path(self.tempdir.name) / "upgrade-legacy-outbox.sqlite3")
        legacy = self.create_legacy_v2_store(upgrade_path)
        legacy.append_with_outbox(event("legacy-open"), (message("legacy-open"),))
        legacy.append_with_outbox(event("legacy-resolved"), (message("legacy-resolved"),))
        claimed = {
            item.message.message_id: item
            for item in legacy.claim_outbox("legacy-publisher", limit=2, lease_seconds=10)
        }
        open_token = claimed["legacy-open"].lease_token
        resolved_token = claimed["legacy-resolved"].lease_token
        legacy._connection.execute(
            """
            INSERT INTO outbox_ambiguities (
                message_id, lease_token, reason_code, attempt_count, marked_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            ("legacy-open", open_token, "callback_timeout", 1, READY),
        )
        legacy._connection.execute(
            """
            INSERT INTO outbox_ambiguities (
                message_id, lease_token, reason_code, attempt_count, marked_at,
                resolution, resolved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-resolved",
                resolved_token,
                "ack_failed",
                1,
                READY,
                "retry",
                "2026-08-19T00:00:01Z",
            ),
        )
        legacy._connection.execute(
            """
            UPDATE outbox SET status = ?, lease_token = NULL, lease_expires_at = NULL
            WHERE message_id = ?
            """,
            (OutboxStatus.PENDING.value, "legacy-resolved"),
        )
        legacy.close()

        upgraded = SQLiteEventStore(upgrade_path, clock=self.clock)
        try:
            rows = {
                item.message_id: item for item in upgraded.read_outbox_ambiguities(open_only=False)
            }
            self.assertEqual(current_schema_version(upgraded._connection), len(MIGRATIONS))
            self.assertEqual(
                rows["legacy-open"].lease_token_digest,
                hashlib.sha256(open_token.encode("utf-8")).hexdigest(),
            )
            self.assertEqual(
                rows["legacy-resolved"].lease_token_digest,
                hashlib.sha256(resolved_token.encode("utf-8")).hexdigest(),
            )
            self.assertIsNone(rows["legacy-open"].resolution)
            self.assertEqual(rows["legacy-resolved"].resolution, "retry")
            columns = {
                row["name"]
                for row in upgraded._connection.execute(
                    "PRAGMA table_info(outbox_ambiguities)"
                ).fetchall()
            }
            self.assertNotIn("lease_token", columns)
            self.assertEqual(
                upgraded._connection.execute("PRAGMA foreign_key_check").fetchall(),
                [],
            )
            self.assertEqual(
                upgraded._connection.execute("PRAGMA integrity_check").fetchone()[0],
                "ok",
            )
            upgraded._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            upgraded.close()

        persisted_bytes = b"".join(
            candidate.read_bytes()
            for candidate in Path(self.tempdir.name).glob("upgrade-legacy-outbox.sqlite3*")
        )
        self.assertNotIn(resolved_token.encode("utf-8"), persisted_bytes)

    def test_legacy_rebuild_failure_rolls_back_schema_and_ledger_atomically(self):
        corrupt_path = str(Path(self.tempdir.name) / "corrupt-legacy-outbox.sqlite3")
        legacy = self.create_legacy_v2_store(corrupt_path)
        legacy.append_with_outbox(event("legacy-corrupt"), (message("legacy-corrupt"),))
        claimed = legacy.claim_outbox("legacy-publisher", lease_seconds=10)[0]
        for token in (claimed.lease_token, "second-unresolved-legacy-token"):
            legacy._connection.execute(
                """
                INSERT INTO outbox_ambiguities (
                    message_id, lease_token, reason_code, attempt_count, marked_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                ("legacy-corrupt", token, "callback_timeout", 1, READY),
            )
        legacy.close()

        with self.assertRaises(sqlite3.IntegrityError):
            SQLiteEventStore(corrupt_path, clock=self.clock)

        connection = sqlite3.connect(corrupt_path)
        connection.row_factory = sqlite3.Row
        try:
            self.assertEqual(current_schema_version(connection), 2)
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(outbox_ambiguities)").fetchall()
            }
            self.assertIn("lease_token", columns)
            self.assertNotIn("lease_token_digest", columns)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM outbox_ambiguities").fetchone()[0],
                2,
            )
            leftover = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                ("outbox_ambiguities_legacy_v3",),
            ).fetchone()
            self.assertIsNone(leftover)
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            connection.execute("BEGIN IMMEDIATE")
            self.assertTrue(connection.in_transaction)
            connection.execute("ROLLBACK")
        finally:
            connection.close()

    def test_two_connections_serialize_non_idempotent_legacy_rebuild(self):
        concurrent_path = str(Path(self.tempdir.name) / "concurrent-legacy-outbox.sqlite3")
        legacy = self.create_legacy_v2_store(concurrent_path)
        legacy.append_with_outbox(event("legacy-concurrent"), (message("legacy-concurrent"),))
        claimed = legacy.claim_outbox("legacy-publisher", lease_seconds=10)[0]
        legacy._connection.execute(
            """
            INSERT INTO outbox_ambiguities (
                message_id, lease_token, reason_code, attempt_count, marked_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                "legacy-concurrent",
                claimed.lease_token,
                "callback_timeout",
                1,
                READY,
            ),
        )
        legacy.close()
        start = threading.Barrier(2)

        def migrate():
            connection = sqlite3.connect(
                concurrent_path,
                check_same_thread=False,
                isolation_level=None,
                timeout=2,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            try:
                start.wait(timeout=1)
                version = apply_sqlite_migrations(
                    connection,
                    clock=self.clock,
                )
                return version, connection.in_transaction
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = [
                future.result(timeout=3)
                for future in (executor.submit(migrate), executor.submit(migrate))
            ]

        self.assertEqual(results, [(len(MIGRATIONS), False), (len(MIGRATIONS), False)])
        verified = SQLiteEventStore(concurrent_path, clock=self.clock)
        try:
            ambiguity = verified.read_outbox_ambiguities()[0]
            self.assertEqual(
                ambiguity.lease_token_digest,
                hashlib.sha256(claimed.lease_token.encode("utf-8")).hexdigest(),
            )
            self.assertEqual(
                verified._connection.execute("PRAGMA integrity_check").fetchone()[0],
                "ok",
            )
        finally:
            verified.close()

    def test_outbox_ambiguity_migration_checksum_drift_fails_closed(self):
        self.store.close()
        connection = sqlite3.connect(self.path)
        connection.execute(
            "UPDATE qe_schema_migrations SET sha256 = ? WHERE version = 3",
            ("0" * 64,),
        )
        connection.commit()
        connection.close()

        with self.assertRaises(MigrationDriftError):
            SQLiteEventStore(self.path, clock=self.clock)

        digest = hashlib.sha256(
            migration_text("0003_outbox_ambiguities.up.sql").encode("utf-8")
        ).hexdigest()
        connection = sqlite3.connect(self.path)
        connection.execute(
            "UPDATE qe_schema_migrations SET sha256 = ? WHERE version = 3",
            (digest,),
        )
        connection.commit()
        connection.close()
        self.store = SQLiteEventStore(self.path, clock=self.clock)

    def test_outbox_ambiguity_down_migration_is_rehearsable(self):
        rollback_path = str(Path(self.tempdir.name) / "rollback-v3.sqlite3")

        def apply_only_v3(connection, *, clock, _process_guard=None):
            return apply_sqlite_migrations(
                connection,
                migrations=MIGRATIONS[:3],
                clock=clock,
                _process_guard=_process_guard,
            )

        with mock.patch(
            "quantum_entanglement.store.apply_sqlite_migrations",
            side_effect=apply_only_v3,
        ):
            initialized = SQLiteEventStore(rollback_path, clock=self.clock)
        initialized.close()
        connection = sqlite3.connect(rollback_path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.executescript(migration_text("0003_outbox_ambiguities.down.sql"))
        try:
            self.assertEqual(current_schema_version(connection), 2)
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                ("outbox_ambiguities",),
            ).fetchone()
            self.assertIsNone(table)
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            connection.close()

    def test_changed_outbox_on_event_retry_is_rejected(self):
        self.store.append_with_outbox(event("dispatch-1"), (message(),))

        changed = message(payload={"taskId": "different"})
        with self.assertRaisesRegex(ValueError, "changed its transactional outbox"):
            self.store.append_with_outbox(event("dispatch-1"), (changed,))

        self.assertEqual(self.store.read_outbox()[0].message.payload["taskId"], "task-1")

    def test_outbox_constraint_failure_rolls_back_domain_event(self):
        duplicate = message()
        self.store.append_with_outbox(event("dispatch-1"), (duplicate,), expected_version=0)

        with self.assertRaises(sqlite3.IntegrityError):
            self.store.append_with_outbox(event("dispatch-2"), (duplicate,), expected_version=1)

        self.assertEqual(self.store.stream_version("session:s1"), 1)
        self.assertEqual(len(self.store.read_outbox()), 1)

    def test_lease_ack_is_owned_and_idempotent(self):
        self.store.append_with_outbox(event("dispatch-1"), (message(),))
        claimed = self.store.claim_outbox("publisher-a", limit=1, lease_seconds=10)[0]

        self.assertEqual(claimed.status, OutboxStatus.IN_FLIGHT)
        self.assertEqual(claimed.attempt_count, 1)
        self.assertFalse(self.store.acknowledge_outbox("message-1", "stale-token"))
        self.assertTrue(
            self.store.acknowledge_outbox(
                "message-1", claimed.lease_token, published_at="2026-08-19T00:00:01Z"
            )
        )
        self.assertFalse(self.store.acknowledge_outbox("message-1", claimed.lease_token))
        self.assertEqual(self.store.get_outbox("message-1").status, OutboxStatus.PUBLISHED)
        self.assertIsNone(self.store.get_outbox("message-1").lease_token)

    def test_two_publishers_cannot_claim_the_same_message(self):
        self.store.append_with_outbox(event("dispatch-1"), (message(),))
        other_process = SQLiteEventStore(self.path, clock=self.clock)
        try:
            first = self.store.claim_outbox("publisher-a")
            second = other_process.claim_outbox("publisher-b")
        finally:
            other_process.close()

        self.assertEqual(len(first), 1)
        self.assertEqual(second, ())

    def test_public_outbox_rendering_never_exposes_lease_token(self):
        self.store.append_with_outbox(event("lease-redaction"), (message(),))
        claimed = self.store.claim_outbox("publisher-a", lease_seconds=10)[0]

        rendered = claimed.to_dict()

        self.assertIsNotNone(claimed.lease_token)
        self.assertNotIn("leaseToken", rendered)
        self.assertNotIn("lease_token", rendered)
        self.assertNotIn(claimed.lease_token, repr(claimed))

    def test_expired_lease_is_reclaimed_after_publisher_crash(self):
        self.store.append_with_outbox(event("dispatch-1"), (message(),))
        first = self.store.claim_outbox("publisher-a", lease_seconds=10)[0]

        # Simulate the publisher process dying with the lease still in flight.
        self.store.close()
        self.store = SQLiteEventStore(self.path, clock=self.clock)

        self.clock.set("2026-08-19T00:00:09Z")
        self.assertEqual(self.store.claim_outbox("publisher-b", lease_seconds=10), ())
        self.clock.set("2026-08-19T00:00:10Z")
        reclaimed = self.store.claim_outbox("publisher-b", lease_seconds=10)[0]
        self.assertNotEqual(first.lease_token, reclaimed.lease_token)
        self.assertEqual(reclaimed.attempt_count, 2)
        self.assertFalse(self.store.acknowledge_outbox("message-1", first.lease_token))

    def test_ack_and_nack_fail_atomically_at_exact_lease_expiry(self):
        self.store.append_with_outbox(event("dispatch-1"), (message(),))
        claimed = self.store.claim_outbox("publisher", lease_seconds=10)[0]
        expired_at = "2026-08-19T00:00:10Z"
        self.clock.set(expired_at)

        self.assertFalse(
            self.store.acknowledge_outbox(
                "message-1",
                claimed.lease_token,
                published_at=expired_at,
            )
        )
        self.assertFalse(
            self.store.reject_outbox(
                "message-1",
                claimed.lease_token,
                "transport_unavailable",
            )
        )
        self.assertEqual(
            self.store.get_outbox("message-1").status,
            OutboxStatus.IN_FLIGHT,
        )

    def test_ack_rechecks_time_after_write_lock_wait_crosses_expiry(self):
        self.store.append_with_outbox(event("dispatch-1"), (message(),))
        claimed = self.store.claim_outbox("publisher", lease_seconds=10)[0]

        acknowledged = self.run_after_write_lock_wait(
            lambda: self.store.acknowledge_outbox(
                "message-1",
                claimed.lease_token,
                published_at="2026-08-19T00:00:01Z",
            ),
            lambda: self.clock.set("2026-08-19T00:00:10Z"),
        )

        self.assertFalse(acknowledged)
        self.assertEqual(self.store.get_outbox("message-1").status, OutboxStatus.IN_FLIGHT)

    def test_nack_rechecks_time_after_write_lock_wait_crosses_expiry(self):
        self.store.append_with_outbox(event("dispatch-1"), (message(),))
        claimed = self.store.claim_outbox("publisher", lease_seconds=10)[0]

        rejected = self.run_after_write_lock_wait(
            lambda: self.store.reject_outbox(
                "message-1",
                claimed.lease_token,
                "transport_unavailable",
            ),
            lambda: self.clock.set("2026-08-19T00:00:10Z"),
        )

        self.assertFalse(rejected)
        self.assertEqual(self.store.get_outbox("message-1").status, OutboxStatus.IN_FLIGHT)

    def test_claim_rechecks_time_after_write_lock_wait_crosses_expiry(self):
        self.store.append_with_outbox(event("dispatch-1"), (message(),))
        first = self.store.claim_outbox("publisher-a", lease_seconds=10)[0]

        reclaimed = self.run_after_write_lock_wait(
            lambda: self.store.claim_outbox("publisher-b", lease_seconds=10),
            lambda: self.clock.set("2026-08-19T00:00:10Z"),
        )[0]

        self.assertNotEqual(first.lease_token, reclaimed.lease_token)
        self.assertEqual(reclaimed.attempt_count, 2)

    def test_takeover_fences_both_old_publisher_ack_and_nack(self):
        self.store.append_with_outbox(event("dispatch-1"), (message(),))
        old = self.store.claim_outbox("publisher-old", lease_seconds=10)[0]
        takeover_at = "2026-08-19T00:00:10Z"
        self.clock.set(takeover_at)
        current = self.store.claim_outbox("publisher-current", lease_seconds=10)[0]

        self.assertFalse(
            self.store.acknowledge_outbox(
                "message-1",
                old.lease_token,
                published_at=takeover_at,
            )
        )
        self.assertFalse(
            self.store.reject_outbox(
                "message-1",
                old.lease_token,
                "stale_worker",
            )
        )
        self.clock.set("2026-08-19T00:00:11Z")
        self.assertTrue(
            self.store.acknowledge_outbox(
                "message-1",
                current.lease_token,
                published_at="2026-08-19T00:00:11Z",
            )
        )

    def test_open_ambiguity_blocks_takeover_until_operator_resolution(self):
        self.store.append_with_outbox(event("dispatch-1"), (message(),))
        claimed = self.store.claim_outbox("publisher", lease_seconds=10)[0]
        self.assertTrue(
            self.store.mark_outbox_ambiguous(
                "message-1",
                claimed.lease_token,
                "callback_timeout",
                marked_at="2026-08-19T00:00:10Z",
            )
        )

        self.assertFalse(
            self.store.acknowledge_outbox(
                "message-1",
                claimed.lease_token,
                published_at="2026-08-19T00:00:01Z",
            )
        )
        self.assertFalse(
            self.store.reject_outbox(
                "message-1",
                claimed.lease_token,
                "late_nack",
            )
        )

        self.clock.set("2026-08-19T00:01:00Z")
        self.assertEqual(self.store.claim_outbox("publisher-takeover"), ())
        ambiguity = self.store.read_outbox_ambiguities()[0]
        self.assertEqual(ambiguity.attempt_count, 1)
        self.assertEqual(len(ambiguity.lease_token_digest), 64)
        self.assertNotEqual(ambiguity.lease_token_digest, claimed.lease_token)
        self.assertTrue(
            self.store.resolve_outbox_ambiguity(
                "message-1",
                ambiguity.lease_token_digest,
                "retry",
                resolved_at="2026-08-19T00:01:00Z",
                retry_at="2026-08-19T00:01:00Z",
            )
        )
        reclaimed = self.store.claim_outbox("publisher-takeover")[0]
        self.assertEqual(reclaimed.attempt_count, 2)
        self.assertEqual(self.store.read_outbox_ambiguities(), ())

    def test_nack_schedules_retry_and_can_dead_letter(self):
        self.store.append_with_outbox(event("dispatch-1"), (message(),))
        first = self.store.claim_outbox("publisher")[0]
        self.assertTrue(
            self.store.reject_outbox(
                "message-1",
                first.lease_token,
                "broker unavailable",
                retry_at="2026-08-19T00:01:00Z",
            )
        )
        self.clock.set("2026-08-19T00:00:59Z")
        self.assertEqual(self.store.claim_outbox("publisher"), ())

        self.clock.set("2026-08-19T00:01:00Z")
        second = self.store.claim_outbox("publisher")[0]
        self.assertTrue(
            self.store.reject_outbox(
                "message-1",
                second.lease_token,
                "permanent",
                dead_letter=True,
            )
        )
        dead = self.store.get_outbox("message-1")
        self.assertEqual(dead.status, OutboxStatus.DEAD_LETTER)
        self.assertEqual(dead.last_error, "permanent")

    def test_future_outbox_message_is_not_claimed_early(self):
        future = message(available_at="2026-08-19T01:00:00Z")
        self.store.append_with_outbox(event("dispatch-1"), (future,))

        self.assertEqual(self.store.claim_outbox("publisher"), ())
        self.clock.set("2026-08-19T01:00:00Z")
        self.assertEqual(len(self.store.claim_outbox("publisher")), 1)

    def test_invalid_outbox_timestamp_is_rejected_before_it_can_become_stuck(self):
        with self.assertRaisesRegex(ValueError, "available_at must be an RFC 3339"):
            message(available_at="tomorrow")

    def test_inbox_receipt_and_event_are_deduplicated_together(self):
        first = self.store.append_inbox(
            "a2a-adapter",
            "external-1",
            event("inbound-1"),
            result={"accepted": True},
            received_at=READY,
            expected_version=0,
        )
        retried = self.store.append_inbox(
            "a2a-adapter",
            "external-1",
            event("a-different-event-that-must-not-run"),
            result={"accepted": False},
            expected_version=0,
        )

        self.assertFalse(first.duplicate)
        self.assertTrue(retried.duplicate)
        self.assertEqual(first.event.global_position, retried.event.global_position)
        self.assertEqual(retried.receipt.result, {"accepted": True})
        self.assertEqual(self.store.stream_version("session:s1"), 1)

    def test_failed_inbox_event_does_not_consume_deduplication_key(self):
        invalid = event("inbound-1", payload={"notJson": {"a-set"}})
        with self.assertRaises(TypeError):
            self.store.append_inbox("a2a-adapter", "external-1", invalid)

        self.assertIsNone(self.store.get_inbox_receipt("a2a-adapter", "external-1"))
        accepted = self.store.append_inbox("a2a-adapter", "external-1", event("inbound-1"))
        self.assertFalse(accepted.duplicate)
        self.assertEqual(self.store.stream_version("session:s1"), 1)


if __name__ == "__main__":
    unittest.main()
