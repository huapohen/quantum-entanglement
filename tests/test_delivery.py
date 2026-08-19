import sqlite3
import tempfile
import unittest
from pathlib import Path

from quantum_entanglement.delivery import OutboxMessage, OutboxStatus
from quantum_entanglement.events import DomainEvent
from quantum_entanglement.store import SQLiteEventStore

READY = "2026-08-19T00:00:00Z"


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
        idempotency_key="publish:%s" % message_id,
        available_at=available_at,
        created_at=READY,
    )


class TransactionalDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tempdir.name) / "events.sqlite3")
        self.store = SQLiteEventStore(self.path)

    def tearDown(self):
        self.store.close()
        self.tempdir.cleanup()

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
            self.store.append_with_outbox(
                event("dispatch-2"), (duplicate,), expected_version=1
            )

        self.assertEqual(self.store.stream_version("session:s1"), 1)
        self.assertEqual(len(self.store.read_outbox()), 1)

    def test_lease_ack_is_owned_and_idempotent(self):
        self.store.append_with_outbox(event("dispatch-1"), (message(),))
        claimed = self.store.claim_outbox(
            "publisher-a", limit=1, lease_seconds=10, now=READY
        )[0]

        self.assertEqual(claimed.status, OutboxStatus.IN_FLIGHT)
        self.assertEqual(claimed.attempt_count, 1)
        self.assertFalse(self.store.acknowledge_outbox("message-1", "stale-token"))
        self.assertTrue(
            self.store.acknowledge_outbox(
                "message-1", claimed.lease_token, published_at="2026-08-19T00:00:01Z"
            )
        )
        self.assertTrue(self.store.acknowledge_outbox("message-1", claimed.lease_token))
        self.assertEqual(self.store.get_outbox("message-1").status, OutboxStatus.PUBLISHED)

    def test_two_publishers_cannot_claim_the_same_message(self):
        self.store.append_with_outbox(event("dispatch-1"), (message(),))
        other_process = SQLiteEventStore(self.path)
        try:
            first = self.store.claim_outbox("publisher-a", now=READY)
            second = other_process.claim_outbox("publisher-b", now=READY)
        finally:
            other_process.close()

        self.assertEqual(len(first), 1)
        self.assertEqual(second, ())

    def test_expired_lease_is_reclaimed_after_publisher_crash(self):
        self.store.append_with_outbox(event("dispatch-1"), (message(),))
        first = self.store.claim_outbox(
            "publisher-a", lease_seconds=10, now=READY
        )[0]

        # Simulate the publisher process dying with the lease still in flight.
        self.store.close()
        self.store = SQLiteEventStore(self.path)

        self.assertEqual(
            self.store.claim_outbox(
                "publisher-b", lease_seconds=10, now="2026-08-19T00:00:09Z"
            ),
            (),
        )
        reclaimed = self.store.claim_outbox(
            "publisher-b", lease_seconds=10, now="2026-08-19T00:00:10Z"
        )[0]
        self.assertNotEqual(first.lease_token, reclaimed.lease_token)
        self.assertEqual(reclaimed.attempt_count, 2)
        self.assertFalse(self.store.acknowledge_outbox("message-1", first.lease_token))

    def test_nack_schedules_retry_and_can_dead_letter(self):
        self.store.append_with_outbox(event("dispatch-1"), (message(),))
        first = self.store.claim_outbox("publisher", now=READY)[0]
        self.assertTrue(
            self.store.reject_outbox(
                "message-1",
                first.lease_token,
                "broker unavailable",
                retry_at="2026-08-19T00:01:00Z",
            )
        )
        self.assertEqual(
            self.store.claim_outbox("publisher", now="2026-08-19T00:00:59Z"), ()
        )

        second = self.store.claim_outbox(
            "publisher", now="2026-08-19T00:01:00Z"
        )[0]
        self.assertTrue(
            self.store.reject_outbox(
                "message-1", second.lease_token, "permanent", dead_letter=True
            )
        )
        dead = self.store.get_outbox("message-1")
        self.assertEqual(dead.status, OutboxStatus.DEAD_LETTER)
        self.assertEqual(dead.last_error, "permanent")

    def test_future_outbox_message_is_not_claimed_early(self):
        future = message(available_at="2026-08-19T01:00:00Z")
        self.store.append_with_outbox(event("dispatch-1"), (future,))

        self.assertEqual(self.store.claim_outbox("publisher", now=READY), ())
        self.assertEqual(
            len(self.store.claim_outbox("publisher", now="2026-08-19T01:00:00Z")), 1
        )

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
        accepted = self.store.append_inbox(
            "a2a-adapter", "external-1", event("inbound-1")
        )
        self.assertFalse(accepted.duplicate)
        self.assertEqual(self.store.stream_version("session:s1"), 1)


if __name__ == "__main__":
    unittest.main()
