import tempfile
import unittest
from pathlib import Path

from quantum_entanglement.delivery import OutboxMessage
from quantum_entanglement.events import DomainEvent
from quantum_entanglement.store import (
    EventStoreJsonError,
    EventStoreJsonTooLargeError,
    EventStoreJsonTypeError,
    SQLiteEventStore,
)

T0 = "2026-08-20T00:00:00Z"


def event(event_id: str, payload: dict[str, object]) -> DomainEvent:
    return DomainEvent(
        stream_id="session:json",
        event_type="json.checked",
        payload=payload,
        actor_id="tester",
        event_id=event_id,
        timestamp=T0,
        idempotency_key=f"event:{event_id}",
    )


class SQLiteEventStoreJsonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tempdir.name) / "events.sqlite3")
        self.store = SQLiteEventStore(self.path, clock=lambda: T0)

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def test_event_payload_rejects_non_finite_numbers_without_state_change(self) -> None:
        for index, invalid in enumerate((float("nan"), float("inf"), float("-inf"))):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(EventStoreJsonError, "non-finite"):
                    self.store.append(event(f"event-{index}", {"value": invalid}))

        self.assertEqual(self.store.stream_version("session:json"), 0)
        self.assertEqual(self.store.read_all(), ())

    def test_outbox_json_failure_rolls_back_its_triggering_event(self) -> None:
        message = OutboxMessage(
            destination="fake-runtime",
            payload={"value": float("nan")},
            headers={"trace": "safe"},
            message_id="message-json",
            idempotency_key="outbox:json",
            available_at=T0,
            created_at=T0,
        )

        with self.assertRaisesRegex(EventStoreJsonError, "non-finite"):
            self.store.append_with_outbox(event("event-outbox", {}), (message,))

        self.assertEqual(self.store.stream_version("session:json"), 0)
        self.assertEqual(self.store.read_outbox(), ())

    def test_inbox_result_json_failure_rolls_back_event_and_receipt(self) -> None:
        with self.assertRaisesRegex(EventStoreJsonError, "non-finite"):
            self.store.append_inbox(
                "consumer-json",
                "message-json",
                event("event-inbox", {}),
                result={"value": float("inf")},
                received_at=T0,
            )

        self.assertEqual(self.store.stream_version("session:json"), 0)
        self.assertIsNone(self.store.get_inbox_receipt("consumer-json", "message-json"))

    def test_append_many_and_snapshot_fail_atomically_on_non_finite_json(self) -> None:
        with self.assertRaisesRegex(EventStoreJsonError, "non-finite"):
            self.store.append_many(
                "session:json",
                (
                    event("event-batch-1", {"value": 1}),
                    event("event-batch-2", {"value": float("nan")}),
                ),
                expected_version=0,
            )
        self.assertEqual(self.store.stream_version("session:json"), 0)

        with self.assertRaisesRegex(EventStoreJsonError, "non-finite"):
            self.store.save_snapshot(
                "session:json",
                0,
                {"value": float("-inf")},
                at=T0,
            )
        self.assertIsNone(self.store.load_snapshot("session:json"))

    def test_structural_limits_reject_cycles_depth_width_and_oversized_scalars(self) -> None:
        cyclic: dict[str, object] = {}
        cyclic["self"] = cyclic

        too_deep: object = "leaf"
        for _ in range(65):
            too_deep = {"child": too_deep}

        invalid_payloads: tuple[dict[str, object], ...] = (
            cyclic,
            {"deep": too_deep},
            {"wide": [None] * 10_000},
            {"k" * 513: "value"},
            {"value": "x" * 65_537},
            {"value": 1 << 4_096},
            {"value": {"unsupported"}},
        )
        for index, payload in enumerate(invalid_payloads):
            with self.subTest(index=index):
                with self.assertRaises(EventStoreJsonError):
                    self.store.append(event(f"event-structure-{index}", payload))

        self.assertEqual(self.store.stream_version("session:json"), 0)
        self.assertEqual(self.store.read_all(), ())

    def test_unsupported_json_types_preserve_the_type_error_contract(self) -> None:
        with self.assertRaises(EventStoreJsonTypeError) as raised:
            self.store.append(event("event-set", {"value": {"unsupported"}}))
        self.assertIsInstance(raised.exception, TypeError)
        self.assertNotIsInstance(raised.exception, ValueError)
        self.assertEqual(self.store.stream_version("session:json"), 0)

    def test_encoded_byte_limit_is_configurable_and_checked_before_commit(self) -> None:
        bounded_path = str(Path(self.tempdir.name) / "bounded-json.sqlite3")
        with self.assertRaises(ValueError):
            SQLiteEventStore(bounded_path, clock=lambda: T0, max_json_bytes=0)
        with self.assertRaises(TypeError):
            SQLiteEventStore(bounded_path, clock=lambda: T0, max_json_bytes=True)

        with SQLiteEventStore(
            bounded_path,
            clock=lambda: T0,
            max_json_bytes=32,
        ) as bounded:
            with self.assertRaisesRegex(EventStoreJsonTooLargeError, "encoded bytes"):
                bounded.append(event("event-encoded-limit", {"value": "x" * 64}))
            self.assertEqual(bounded.stream_version("session:json"), 0)


if __name__ == "__main__":
    unittest.main()
