from __future__ import annotations

import inspect
import unittest
from collections.abc import Callable
from typing import Any

from quantum_entanglement.attempts import InvocationJobSpec
from quantum_entanglement.delivery import OutboxMessage
from quantum_entanglement.events import DomainEvent
from quantum_entanglement.store import ReservedResultEventError, SQLiteEventStore

T0 = "2026-08-28T12:00:00.000000Z"
RESULT_EVENT_TYPE = "task.invocation.result.accepted"
STATUS_EVENT_TYPE = "task.status.changed"
RESERVED_ERROR = "generic event append cannot write reserved result authority"
RESERVED_KEYS = (
    "transitionKind",
    "resultReceiptId",
    "resultEventId",
    "resultEvidenceDigest",
    "runningTaskRevision",
    "terminalTaskRevision",
)


def event(
    event_type: str,
    payload: dict[str, object],
    *,
    event_id: str = "event-reserved",
) -> DomainEvent:
    return DomainEvent(
        stream_id="session:reserved",
        event_type=event_type,
        payload=payload,
        actor_id="orchestrator",
        event_id=event_id,
        timestamp=T0,
        correlation_id="correlation-reserved",
        causation_id="event-start",
        idempotency_key="event:" + event_id,
    )


def terminal_event(key: str) -> DomainEvent:
    return event(
        STATUS_EVENT_TYPE,
        {
            "taskId": "task-reserved",
            "previous": "running",
            "current": "completed",
            "reason": None,
            "revision": 2,
            key: "reserved-marker",
        },
    )


def full_terminal_event() -> DomainEvent:
    payload: dict[str, object] = {
        "schemaVersion": 2,
        "taskId": "task-reserved",
        "previous": "running",
        "current": "completed",
        "reason": None,
        "revision": 2,
    }
    payload.update({key: None for key in RESERVED_KEYS})
    return event(STATUS_EVENT_TYPE, payload)


def legacy_status_event() -> DomainEvent:
    return event(
        STATUS_EVENT_TYPE,
        {
            "taskId": "task-reserved",
            "previous": "ready",
            "current": "running",
            "reason": None,
            "revision": 1,
        },
        event_id="event-legacy-status",
    )


def job_spec() -> InvocationJobSpec:
    return InvocationJobSpec(
        invocation_id="invocation-reserved",
        session_id="reserved",
        plan_id="plan-reserved",
        task_id="task-reserved",
        agent_id="agent-reserved",
        idempotency_key="invoke:task-reserved",
        payload_digest="a" * 64,
        max_attempts=1,
    )


def outbox_message() -> OutboxMessage:
    return OutboxMessage(
        destination="test-runtime",
        payload={"resultRef": "result-reserved"},
        headers={"trace": "reserved-boundary"},
        message_id="message-reserved",
        idempotency_key="outbox:reserved",
        available_at=T0,
        created_at=T0,
    )


def append_surface(
    store: SQLiteEventStore,
    surface: str,
    candidate: DomainEvent,
) -> object:
    operations: dict[str, Callable[[], object]] = {
        "append": lambda: store.append(candidate, expected_version=0),
        "append_many": lambda: store.append_many(
            candidate.stream_id,
            (candidate,),
            expected_version=0,
        ),
        "append_with_outbox": lambda: store.append_with_outbox(
            candidate,
            (outbox_message(),),
            expected_version=0,
        ),
        "append_inbox": lambda: store.append_inbox(
            "consumer-reserved",
            "inbox-message-reserved",
            candidate,
            result={"safe": True},
            received_at=T0,
            expected_version=0,
        ),
        "append_invocation_admission": lambda: store.append_invocation_admission(
            (candidate,),
            job_spec(),
            expected_version=0,
        ),
    }
    return operations[surface]()


class ReservedResultEventBoundaryTests(unittest.TestCase):
    surfaces = (
        "append",
        "append_many",
        "append_with_outbox",
        "append_inbox",
        "append_invocation_admission",
    )

    def durable_state(self, store: SQLiteEventStore) -> dict[str, tuple[tuple[Any, ...], ...]]:
        return {
            table: tuple(
                tuple(row)
                for row in store._connection.execute(
                    f"SELECT * FROM {table} ORDER BY rowid"
                ).fetchall()
            )
            for table in (
                "events",
                "outbox",
                "inbox_receipts",
                "invocation_jobs",
                "invocation_attempts",
                "invocation_admissions",
                "sqlite_sequence",
            )
        }

    def assert_rejected_before_begin(
        self,
        store: SQLiteEventStore,
        operation: Callable[[], object],
    ) -> None:
        before = self.durable_state(store)
        total_changes = store._connection.total_changes
        statements: list[str] = []
        store._connection.set_trace_callback(statements.append)
        try:
            with self.assertRaisesRegex(ReservedResultEventError, RESERVED_ERROR) as raised:
                operation()
        finally:
            store._connection.set_trace_callback(None)
        self.assertIs(type(raised.exception), ReservedResultEventError)
        self.assertFalse(
            any(
                statement.lstrip()
                .upper()
                .startswith(("BEGIN", "INSERT", "UPDATE", "DELETE", "REPLACE"))
                for statement in statements
            ),
            statements,
        )
        self.assertFalse(store._connection.in_transaction)
        self.assertEqual(store._connection.total_changes, total_changes)
        self.assertEqual(self.durable_state(store), before)

    def test_every_generic_surface_rejects_result_and_terminal_vocabulary_before_begin(
        self,
    ) -> None:
        candidates = (
            event(RESULT_EVENT_TYPE, {"schemaVersion": 2}),
            full_terminal_event(),
        ) + tuple(terminal_event(key) for key in RESERVED_KEYS)
        for surface in self.surfaces:
            for candidate in candidates:
                with self.subTest(surface=surface, event_type=candidate.event_type):
                    with SQLiteEventStore(":memory:", clock=lambda: T0) as store:
                        self.assert_rejected_before_begin(
                            store,
                            lambda surface=surface, candidate=candidate: append_surface(
                                store,
                                surface,
                                candidate,
                            ),
                        )

    def test_terminal_reserved_keys_reject_exact_and_near_canonical_spellings(self) -> None:
        spellings: list[str] = []
        for key in RESERVED_KEYS:
            separated = "".join(
                ("_" + character.lower()) if character.isupper() else character for character in key
            )
            fullwidth = "".join(
                chr(ord(character) + 0xFEE0) if "!" <= character <= "~" else character
                for character in key
            )
            midpoint = len(key) // 2
            spellings.extend(
                (
                    key,
                    separated,
                    separated.replace("_", "-"),
                    separated.replace("_", "."),
                    separated.replace("_", " "),
                    separated.replace("_", "/"),
                    key.upper(),
                    fullwidth,
                    key[:midpoint] + "\u200b" + key[midpoint:],
                    key[:midpoint] + "\u0301" + key[midpoint:],
                    key[:midpoint] + "🚀" + key[midpoint:],
                )
            )
        with SQLiteEventStore(":memory:", clock=lambda: T0) as store:
            for spelling in spellings:
                with self.subTest(spelling=spelling):
                    self.assert_rejected_before_begin(
                        store,
                        lambda spelling=spelling: store.append(terminal_event(spelling)),
                    )

    def test_accepted_type_rejects_every_valid_json_payload_shape(self) -> None:
        payloads = (
            {},
            {"schemaVersion": 1},
            {"schemaVersion": 2, "future": True},
            {"resultCanary": "result-secret-canary"},
            {"nested": {"resultReceiptId": "nested"}},
        )
        with SQLiteEventStore(":memory:", clock=lambda: T0) as store:
            for index, payload in enumerate(payloads):
                with self.subTest(index=index):
                    self.assert_rejected_before_begin(
                        store,
                        lambda payload=payload: store.append(event(RESULT_EVENT_TYPE, payload)),
                    )

    def test_reserved_batch_position_never_exposes_a_prefix(self) -> None:
        for position in range(3):
            batch = [
                event("ordinary.event", {"position": index}, event_id=f"ordinary-{index}")
                for index in range(3)
            ]
            batch[position] = full_terminal_event()
            with self.subTest(position=position):
                with SQLiteEventStore(":memory:", clock=lambda: T0) as store:
                    self.assert_rejected_before_begin(
                        store,
                        lambda batch=tuple(batch): store.append_many(
                            "session:reserved",
                            batch,
                            expected_version=0,
                        ),
                    )

    def test_fence_is_exact_to_result_event_and_terminal_payload_namespace(self) -> None:
        allowed = (
            event("task.invocation.result.accepted.v2", {"schemaVersion": 2}),
            event("task.note.added", {"resultReceiptId": "ordinary-reference"}),
            terminal_event("resultReceiptIdentifier"),
            event(
                STATUS_EVENT_TYPE,
                {
                    "taskId": "task-reserved",
                    "previous": "running",
                    "current": "completed",
                    "reason": "resultEventId is ordinary narration here",
                    "revision": 2,
                    "metadata": {"resultEventId": "nested-reference"},
                },
            ),
            event("task.result.received", {"resultEventId": "legacy-reference"}),
        )
        with SQLiteEventStore(":memory:", clock=lambda: T0) as store:
            for index, candidate in enumerate(allowed):
                candidate = DomainEvent(
                    stream_id=candidate.stream_id,
                    event_type=candidate.event_type,
                    payload=candidate.payload,
                    actor_id=candidate.actor_id,
                    event_id=f"event-allowed-{index}",
                    timestamp=candidate.timestamp,
                    idempotency_key=f"event:allowed:{index}",
                )
                with self.subTest(event_type=candidate.event_type):
                    store.append(candidate, expected_version=index)
            self.assertEqual(store.stream_version("session:reserved"), len(allowed))

    def test_legacy_five_field_status_remains_valid_across_generic_surfaces(self) -> None:
        for surface in self.surfaces:
            with self.subTest(surface=surface):
                with SQLiteEventStore(":memory:", clock=lambda: T0) as store:
                    append_surface(store, surface, legacy_status_event())
                    self.assertEqual(store.stream_version("session:reserved"), 1)

    def test_legacy_five_field_status_variants_remain_valid(self) -> None:
        transitions = (
            ("ready", "running", None),
            ("running", "completed", None),
            ("running", "failed", "resultEvidenceDigest is only reason text"),
            ("waiting_input", "ready", "input received"),
            ("waiting_approval", "ready", "approved"),
        )
        with SQLiteEventStore(":memory:", clock=lambda: T0) as store:
            for index, (previous, current, reason) in enumerate(transitions):
                candidate = event(
                    STATUS_EVENT_TYPE,
                    {
                        "taskId": f"task-{index}",
                        "previous": previous,
                        "current": current,
                        "reason": reason,
                        "revision": index + 1,
                    },
                    event_id=f"legacy-transition-{index}",
                )
                store.append(candidate, expected_version=index)
            self.assertEqual(store.stream_version("session:reserved"), len(transitions))

    def test_rejection_leaves_the_same_store_usable_and_error_content_free(self) -> None:
        candidate = event(
            STATUS_EVENT_TYPE,
            {
                "resultEventId": "payload-secret-canary",
                "credential": "credential-secret-canary",
            },
            event_id="event-secret-canary",
        )
        with SQLiteEventStore(":memory:", clock=lambda: T0) as store:
            with self.assertRaises(ReservedResultEventError) as raised:
                store.append(candidate)
            rendered = repr(raised.exception) + str(raised.exception)
            for canary in (
                "event-secret-canary",
                "payload-secret-canary",
                "credential-secret-canary",
                "resultEventId",
            ):
                self.assertNotIn(canary, rendered)
            stored = store.append(
                event("ordinary.event", {"safe": True}, event_id="event-after-rejection"),
                expected_version=0,
            )
            self.assertEqual(stored.sequence, 1)

    def test_idempotent_retry_cannot_read_around_the_fence(self) -> None:
        with SQLiteEventStore(":memory:", clock=lambda: T0) as store:
            store.append(
                event("ordinary.event", {"safe": True}, event_id="event-retry"),
                expected_version=0,
            )
            candidate = event(RESULT_EVENT_TYPE, {}, event_id="event-retry")
            self.assert_rejected_before_begin(store, lambda: store.append(candidate))

    def test_upgrade_row_cannot_be_replayed_through_generic_append(self) -> None:
        with SQLiteEventStore(":memory:", clock=lambda: T0) as store:
            store._connection.execute(
                """
                INSERT INTO events (
                    stream_id, sequence, event_id, event_type, actor_id, timestamp,
                    payload_json, correlation_id, causation_id, idempotency_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "session:reserved",
                    1,
                    "event-upgrade-reserved",
                    RESULT_EVENT_TYPE,
                    "orchestrator",
                    T0,
                    "{}",
                    "correlation-reserved",
                    "event-start",
                    "event:event-upgrade-reserved",
                ),
            )
            candidate = event(
                RESULT_EVENT_TYPE,
                {},
                event_id="event-upgrade-reserved",
            )
            self.assert_rejected_before_begin(store, lambda: store.append(candidate))

    def test_instance_method_shadow_cannot_disable_the_fence(self) -> None:
        with SQLiteEventStore(":memory:", clock=lambda: T0) as store:
            store._snapshot_generic_event = store._snapshot_event  # type: ignore[method-assign]
            store._reject_generic_reserved_result_event = lambda _event: None  # type: ignore[method-assign]
            self.assert_rejected_before_begin(
                store,
                lambda: store.append(event(RESULT_EVENT_TYPE, {})),
            )

    def test_known_reserved_event_rejects_before_secondary_caller_inputs(self) -> None:
        class ExplodingIterable:
            def __iter__(self) -> object:
                raise AssertionError("outbox iterable must not be consumed")

        candidate = event(RESULT_EVENT_TYPE, {})
        with SQLiteEventStore(":memory:", clock=lambda: T0) as store:
            self.assert_rejected_before_begin(
                store,
                lambda: store.append_with_outbox(candidate, ExplodingIterable()),  # type: ignore[arg-type]
            )
            self.assert_rejected_before_begin(
                store,
                lambda: store.append_inbox(
                    "consumer",
                    "message",
                    candidate,
                    result=object(),  # type: ignore[arg-type]
                ),
            )
            self.assert_rejected_before_begin(
                store,
                lambda: store.append_invocation_admission(
                    (candidate,),
                    object(),  # type: ignore[arg-type]
                ),
            )

    def test_public_append_inventory_has_no_reserved_escape_hatch(self) -> None:
        expected = {
            "append",
            "append_many",
            "append_with_outbox",
            "append_inbox",
            "append_invocation_admission",
            "append_task_invocation_admission",
            "append_scoped_task_invocation_admission_v2",
        }
        actual = {
            name
            for name, member in vars(SQLiteEventStore).items()
            if name.startswith("append") and callable(member)
        }
        self.assertEqual(actual, expected)
        for name in expected:
            parameters = inspect.signature(getattr(SQLiteEventStore, name)).parameters
            self.assertTrue(
                {"trusted", "allow_reserved", "connection", "transaction"}.isdisjoint(parameters),
                name,
            )
        for name in (
            "append",
            "append_many",
            "append_with_outbox",
            "append_inbox",
            "append_invocation_admission",
        ):
            self.assertIn(
                "_snapshot_generic_event",
                inspect.getsource(getattr(SQLiteEventStore, name)),
                name,
            )
        self.assertEqual(inspect.getsource(SQLiteEventStore).count("INSERT INTO events ("), 2)


if __name__ == "__main__":
    unittest.main()
