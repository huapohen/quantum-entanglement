import sqlite3
import tempfile
import threading
import unittest
from collections.abc import Mapping, MutableMapping
from operator import attrgetter, setitem
from pathlib import Path
from typing import Any, Callable, cast

from quantum_entanglement.events import DomainEvent, StoredEvent
from quantum_entanglement.projections import (
    MAX_EVENT_PAYLOAD_DEPTH,
    MAX_EVENT_PAYLOAD_NODES,
    MAX_PROJECTION_IDENTIFIER_LENGTH,
    SCHEMA_VERSION_FIELD,
    DurableProjector,
    EventSchemaDecoderError,
    EventSchemaRegistrySealedError,
    EventUpcasterRegistry,
    FutureEventSchemaVersionError,
    InvalidDecoderResultError,
    InvalidEventPayloadError,
    InvalidEventSchemaVersionError,
    InvalidUpcastResultError,
    MissingUpcasterError,
    ProjectionIntegrityError,
    ProjectionLeaseConflictError,
    ProjectionLeaseLostError,
    ProjectionOffsetConflictError,
    ProjectionStatementResult,
    ProjectionTransaction,
    SQLiteProjectionOffsetStore,
    UnknownEventTypeError,
    UnsealedEventSchemaRegistryError,
    UpcastedEvent,
)
from quantum_entanglement.store import SQLiteEventStore


def stored(payload: Mapping[str, Any], *, event_type: str = "task.created") -> StoredEvent:
    return StoredEvent(
        DomainEvent("session:s1", event_type, payload, "user", event_id="evt-1"),
        sequence=1,
        global_position=1,
    )


def decode_mapping(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    return dict(payload)


def nested_payload(depth: int) -> Mapping[str, Any]:
    value: Any = "leaf"
    for _ in range(depth):
        value = {"child": value}
    return cast(Mapping[str, Any], value)


class EventUpcasterRegistryTests(unittest.TestCase):
    def test_contiguous_chain_upcasts_legacy_v1_without_mutating_event(self) -> None:
        registry = EventUpcasterRegistry()
        registry.register_event_type(
            "task.created",
            current_version=3,
            decoder=decode_mapping,
        )

        def one_to_two(payload: Mapping[str, Any]) -> Mapping[str, Any]:
            return {"title": payload["name"], "metadata": payload["metadata"]}

        def two_to_three(payload: Mapping[str, Any]) -> Mapping[str, Any]:
            payload["metadata"]["normalized"] = True
            return {"title": payload["title"], "metadata": payload["metadata"]}

        registry.register_upcaster("task.created", from_version=1, upcaster=one_to_two)
        registry.register_upcaster("task.created", from_version=2, upcaster=two_to_three)
        registry.seal()
        original = stored({"name": "ship", "metadata": {"source": "human"}})

        result = registry.upcast(original)

        self.assertEqual(result.source_schema_version, 1)
        self.assertEqual(result.schema_version, 3)
        self.assertEqual(
            dict(result.payload),
            {"title": "ship", "metadata": {"source": "human", "normalized": True}},
        )
        self.assertEqual(
            original.event.payload,
            {"name": "ship", "metadata": {"source": "human"}},
        )

    def test_current_payload_strips_reserved_schema_metadata(self) -> None:
        registry = EventUpcasterRegistry()
        registry.register_event_type(
            "task.created",
            current_version=2,
            decoder=decode_mapping,
        )
        registry.register_upcaster(
            "task.created",
            from_version=1,
            upcaster=decode_mapping,
        )
        registry.seal()

        result = registry.upcast(stored({SCHEMA_VERSION_FIELD: 2, "title": "ship"}))

        self.assertEqual(dict(result.payload), {"title": "ship"})
        self.assertEqual(result.source_schema_version, 2)

    def test_unknown_event_type_fails_closed(self) -> None:
        registry = EventUpcasterRegistry()
        registry.register_event_type(
            "task.created",
            current_version=1,
            decoder=decode_mapping,
        )
        registry.seal()

        with self.assertRaisesRegex(UnknownEventTypeError, "unregistered event type"):
            registry.upcast(stored({}, event_type="task.deleted"))

    def test_future_schema_version_fails_closed(self) -> None:
        registry = EventUpcasterRegistry()
        registry.register_event_type(
            "task.created",
            current_version=2,
            decoder=decode_mapping,
        )
        registry.register_upcaster(
            "task.created",
            from_version=1,
            upcaster=decode_mapping,
        )
        registry.seal()

        with self.assertRaisesRegex(FutureEventSchemaVersionError, "newer than supported"):
            registry.upcast(stored({SCHEMA_VERSION_FIELD: 3}))

    def test_non_integer_schema_version_fails_closed(self) -> None:
        registry = EventUpcasterRegistry()
        registry.register_event_type(
            "task.created",
            current_version=2,
            decoder=decode_mapping,
        )
        registry.register_upcaster(
            "task.created",
            from_version=1,
            upcaster=decode_mapping,
        )
        registry.seal()

        for invalid in (True, 0, "2", None):
            with self.subTest(invalid=invalid):
                with self.assertRaises(InvalidEventSchemaVersionError):
                    registry.upcast(stored({SCHEMA_VERSION_FIELD: invalid}))

    def test_seal_rejects_a_missing_upcaster_chain_before_any_event(self) -> None:
        registry = EventUpcasterRegistry()
        registry.register_event_type(
            "task.created",
            current_version=3,
            decoder=decode_mapping,
        )
        registry.register_upcaster("task.created", from_version=2, upcaster=lambda payload: payload)

        with self.assertRaisesRegex(MissingUpcasterError, "v1 -> v2"):
            registry.seal()

        self.assertFalse(registry.is_sealed)
        registry.register_upcaster(
            "task.created",
            from_version=1,
            upcaster=decode_mapping,
        )
        registry.seal()
        self.assertTrue(registry.is_sealed)

    def test_upcaster_must_return_mapping_without_reserved_metadata(self) -> None:
        registry = EventUpcasterRegistry()
        registry.register_event_type(
            "task.created",
            current_version=2,
            decoder=decode_mapping,
        )
        registry.register_upcaster(
            "task.created",
            from_version=1,
            upcaster=lambda _payload: [],  # type: ignore[arg-type, return-value]
        )
        registry.seal()
        with self.assertRaisesRegex(InvalidUpcastResultError, "must return a mapping"):
            registry.upcast(stored({}))

        other = EventUpcasterRegistry()
        other.register_event_type(
            "task.created",
            current_version=2,
            decoder=decode_mapping,
        )
        other.register_upcaster(
            "task.created",
            from_version=1,
            upcaster=lambda _payload: {SCHEMA_VERSION_FIELD: 2},
        )
        other.seal()
        with self.assertRaisesRegex(InvalidUpcastResultError, "reserved field"):
            other.upcast(stored({}))

    def test_registration_rejects_invalid_or_duplicate_contracts(self) -> None:
        registry = EventUpcasterRegistry()
        with self.assertRaises(ValueError):
            registry.register_event_type("", current_version=1, decoder=decode_mapping)
        with self.assertRaises(ValueError):
            registry.register_event_type(
                "task.created",
                current_version=True,
                decoder=decode_mapping,
            )
        with self.assertRaises(TypeError):
            registry.register_event_type(
                "task.created",
                current_version=1,
                decoder=None,  # type: ignore[arg-type]
            )

        registry.register_event_type(
            "task.created",
            current_version=2,
            decoder=decode_mapping,
        )
        with self.assertRaises(ValueError):
            registry.register_event_type(
                "task.created",
                current_version=2,
                decoder=decode_mapping,
            )
        with self.assertRaises(UnknownEventTypeError):
            registry.register_upcaster("task.deleted", from_version=1, upcaster=dict)
        with self.assertRaises(TypeError):
            registry.register_upcaster(
                "task.created",
                from_version=1,
                upcaster=None,  # type: ignore[arg-type]
            )
        registry.register_upcaster("task.created", from_version=1, upcaster=dict)
        with self.assertRaises(ValueError):
            registry.register_upcaster("task.created", from_version=1, upcaster=dict)

    def test_sealed_registry_stably_rejects_all_later_registration(self) -> None:
        registry = EventUpcasterRegistry()
        registry.register_event_type(
            "task.created",
            current_version=1,
            decoder=decode_mapping,
        )
        registry.seal()
        registry.seal()

        with self.assertRaises(EventSchemaRegistrySealedError):
            registry.register_event_type(
                "task.deleted",
                current_version=1,
                decoder=decode_mapping,
            )
        with self.assertRaises(EventSchemaRegistrySealedError):
            registry.register_upcaster(
                "task.created",
                from_version=1,
                upcaster=decode_mapping,
            )

    def test_unsealed_registry_rejects_upcast(self) -> None:
        registry = EventUpcasterRegistry()
        registry.register_event_type(
            "task.created",
            current_version=1,
            decoder=decode_mapping,
        )

        with self.assertRaisesRegex(UnsealedEventSchemaRegistryError, "must be sealed"):
            registry.upcast(stored({"title": "ship"}))

    def test_current_decoder_runs_for_direct_and_upcast_payloads(self) -> None:
        calls: list[dict[str, Any]] = []

        def decoder(payload: Mapping[str, Any]) -> Mapping[str, Any]:
            calls.append(dict(payload))
            return {"title": str(payload["title"]).strip()}

        registry = EventUpcasterRegistry()
        registry.register_event_type(
            "task.created",
            current_version=2,
            decoder=decoder,
        )
        registry.register_upcaster(
            "task.created",
            from_version=1,
            upcaster=lambda payload: {"title": payload["name"]},
        )
        registry.seal()

        upgraded = registry.upcast(stored({"name": " upgraded "}))
        direct = registry.upcast(stored({SCHEMA_VERSION_FIELD: 2, "title": " direct "}))

        self.assertEqual(calls, [{"title": " upgraded "}, {"title": " direct "}])
        self.assertEqual(upgraded.payload["title"], "upgraded")
        self.assertEqual(direct.payload["title"], "direct")

    def test_decoder_input_output_are_isolated_and_result_is_deeply_read_only(self) -> None:
        decoder_results: list[dict[str, Any]] = []

        def decoder(payload: Mapping[str, Any]) -> Mapping[str, Any]:
            payload["metadata"]["decoderTouched"] = True
            candidate = {
                "title": payload["title"],
                "metadata": payload["metadata"],
                "labels": ["current"],
            }
            decoder_results.append(candidate)
            return candidate

        registry = EventUpcasterRegistry()
        registry.register_event_type(
            "task.created",
            current_version=1,
            decoder=decoder,
        )
        registry.seal()
        original = stored({"title": "ship", "metadata": {"source": "human"}})

        result = registry.upcast(original)
        decoder_results[0]["metadata"]["afterReturn"] = True
        decoder_results[0]["labels"].append("mutated")

        self.assertEqual(original.event.payload["metadata"], {"source": "human"})
        self.assertEqual(
            dict(result.payload["metadata"]),
            {"source": "human", "decoderTouched": True},
        )
        self.assertEqual(result.payload["labels"], ("current",))
        with self.assertRaises(TypeError):
            setitem(
                cast(MutableMapping[str, Any], result.payload),
                "title",
                "changed",
            )
        with self.assertRaises(TypeError):
            setitem(result.payload["metadata"], "handlerTouched", True)

    def test_payload_depth_and_node_limits_are_inclusive_and_bounded(self) -> None:
        registry = EventUpcasterRegistry()
        registry.register_event_type(
            "task.created",
            current_version=1,
            decoder=decode_mapping,
        )
        registry.seal()

        at_depth_limit = registry.upcast(stored(nested_payload(MAX_EVENT_PAYLOAD_DEPTH)))
        self.assertEqual(at_depth_limit.schema_version, 1)
        with self.assertRaises(InvalidEventPayloadError) as too_deep:
            registry.upcast(stored(nested_payload(MAX_EVENT_PAYLOAD_DEPTH + 1)))
        self.assertIsNotNone(too_deep.exception.__cause__)

        exact_node_payload = {
            f"field-{index}": index for index in range(MAX_EVENT_PAYLOAD_NODES - 1)
        }
        at_node_limit = registry.upcast(stored(exact_node_payload))
        self.assertEqual(len(at_node_limit.payload), MAX_EVENT_PAYLOAD_NODES - 1)
        over_node_payload = {f"field-{index}": index for index in range(MAX_EVENT_PAYLOAD_NODES)}
        with self.assertRaises(InvalidEventPayloadError) as too_many_nodes:
            registry.upcast(stored(over_node_payload))
        self.assertIsNotNone(too_many_nodes.exception.__cause__)

    def test_cyclic_or_unsupported_persisted_payload_is_a_stable_schema_error(self) -> None:
        registry = EventUpcasterRegistry()
        registry.register_event_type(
            "task.created",
            current_version=1,
            decoder=decode_mapping,
        )
        registry.seal()
        cyclic: dict[str, Any] = {}
        cyclic["self"] = cyclic

        for payload in (
            cyclic,
            {"unsupported": {"set-value"}},
            {"unsupported": {1: "non-string-key"}},
            {"unsupported": float("nan")},
            {"unsupported": float("inf")},
        ):
            with self.subTest(payload_type=type(next(iter(payload.values()))).__name__):
                with self.assertRaises(InvalidEventPayloadError) as raised:
                    registry.upcast(stored(payload))
                self.assertIsNotNone(raised.exception.__cause__)
                self.assertNotIsInstance(raised.exception, RecursionError)

    def test_cyclic_deep_or_unsupported_decoder_result_is_stably_wrapped(self) -> None:
        def cyclic_decoder(_payload: Mapping[str, Any]) -> Mapping[str, Any]:
            candidate: dict[str, Any] = {}
            candidate["self"] = candidate
            return candidate

        decoders = (
            cyclic_decoder,
            lambda _payload: nested_payload(MAX_EVENT_PAYLOAD_DEPTH + 1),
            lambda _payload: {"unsupported": {"set-value"}},
        )
        for index, decoder in enumerate(decoders):
            with self.subTest(decoder=index):
                registry = EventUpcasterRegistry()
                registry.register_event_type(
                    "task.created",
                    current_version=1,
                    decoder=decoder,
                )
                registry.seal()

                with self.assertRaises(InvalidDecoderResultError) as raised:
                    registry.upcast(stored({}))
                self.assertIsNotNone(raised.exception.__cause__)
                self.assertNotIsInstance(raised.exception, RecursionError)

    def test_invalid_decoder_results_fail_closed(self) -> None:
        not_a_mapping = EventUpcasterRegistry()
        not_a_mapping.register_event_type(
            "task.created",
            current_version=1,
            decoder=lambda _payload: [],  # type: ignore[arg-type, return-value]
        )
        not_a_mapping.seal()
        with self.assertRaisesRegex(InvalidDecoderResultError, "must return a mapping"):
            not_a_mapping.upcast(stored({}))

        reserved = EventUpcasterRegistry()
        reserved.register_event_type(
            "task.created",
            current_version=1,
            decoder=lambda _payload: {SCHEMA_VERSION_FIELD: 1},
        )
        reserved.seal()
        with self.assertRaisesRegex(InvalidDecoderResultError, "reserved field"):
            reserved.upcast(stored({}))

    def test_decoder_exception_is_wrapped_with_cause_but_base_exception_propagates(self) -> None:
        def failing_decoder(_payload: Mapping[str, Any]) -> Mapping[str, Any]:
            raise ValueError("invalid current payload")

        registry = EventUpcasterRegistry()
        registry.register_event_type(
            "task.created",
            current_version=1,
            decoder=failing_decoder,
        )
        registry.seal()

        with self.assertRaises(EventSchemaDecoderError) as raised:
            registry.upcast(stored({}))
        self.assertIsInstance(raised.exception.__cause__, ValueError)

        def interrupted_decoder(_payload: Mapping[str, Any]) -> Mapping[str, Any]:
            raise KeyboardInterrupt("decoder interrupted")

        interrupted = EventUpcasterRegistry()
        interrupted.register_event_type(
            "task.created",
            current_version=1,
            decoder=interrupted_decoder,
        )
        interrupted.seal()
        with self.assertRaisesRegex(KeyboardInterrupt, "decoder interrupted"):
            interrupted.upcast(stored({}))


class MutableClock:
    def __init__(self, value: str = "2026-08-20T00:00:00Z") -> None:
        self.value = value

    def __call__(self) -> str:
        return self.value


class SQLiteProjectionOffsetStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tempdir.name) / "events.sqlite3")
        self.clock = MutableClock()
        self.store = SQLiteProjectionOffsetStore(self.path, clock=self.clock)

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    @staticmethod
    def offset_row(**overrides: object) -> sqlite3.Row:
        values: dict[str, object] = {
            "projection_name": "task-list",
            "last_global_position": 7,
            "owner_id": "worker-a",
            "owner_epoch": 2,
            "lease_expires_at": "2026-08-20T00:00:30Z",
            "updated_at": "2026-08-20T00:00:00.123456Z",
        }
        values.update(overrides)
        return cast(sqlite3.Row, values)

    @staticmethod
    def receipt_row(**overrides: object) -> sqlite3.Row:
        values: dict[str, object] = {
            "projection_name": "task-list",
            "event_id": "evt-1",
            "global_position": 1,
            "applied_at": "2026-08-20T00:00:00Z",
        }
        values.update(overrides)
        return cast(sqlite3.Row, values)

    def test_persisted_offset_decoder_rejects_all_coercion_and_noncanonical_data(
        self,
    ) -> None:
        decoded = self.store._row_to_offset(self.offset_row())
        self.assertEqual(decoded.projection_name, "task-list")
        self.assertEqual(decoded.last_global_position, 7)
        self.assertEqual(decoded.owner_epoch, 2)

        cases: tuple[tuple[str, object], ...] = (
            ("projection_name", None),
            ("projection_name", ""),
            ("projection_name", " task-list"),
            ("projection_name", "task-list "),
            ("projection_name", "p" * (MAX_PROJECTION_IDENTIFIER_LENGTH + 1)),
            ("last_global_position", True),
            ("last_global_position", 7.0),
            ("last_global_position", "7"),
            ("last_global_position", -1),
            ("last_global_position", (2**63)),
            ("owner_id", None),
            ("owner_id", ""),
            ("owner_id", " worker-a"),
            ("owner_id", "worker-a "),
            ("owner_id", "o" * (MAX_PROJECTION_IDENTIFIER_LENGTH + 1)),
            ("owner_epoch", True),
            ("owner_epoch", 2.0),
            ("owner_epoch", "2"),
            ("owner_epoch", 0),
            ("owner_epoch", (2**63)),
            ("lease_expires_at", None),
            ("lease_expires_at", "2026-08-20T00:00:30+00:00"),
            ("lease_expires_at", "2026-08-20T08:00:30+08:00"),
            ("lease_expires_at", "2026-08-20 00:00:30Z"),
            ("lease_expires_at", "2026-08-20T00:00:30.000000Z"),
            ("updated_at", "2026-08-20T00:00:00+00:00"),
        )
        for field_name, value in cases:
            with self.subTest(field_name=field_name, value=value):
                with self.assertRaises(ProjectionIntegrityError):
                    self.store._row_to_offset(self.offset_row(**{field_name: value}))

        with self.assertRaisesRegex(ProjectionIntegrityError, "incomplete"):
            self.store._row_to_offset(cast(sqlite3.Row, {"projection_name": "task-list"}))

    def test_persisted_receipt_decoder_validates_every_field_strictly(self) -> None:
        decoded = self.store._row_to_receipt(self.receipt_row())
        self.assertEqual(decoded.global_position, 1)
        cases: tuple[tuple[str, object], ...] = (
            ("projection_name", None),
            ("projection_name", " task-list"),
            ("event_id", ""),
            ("event_id", "evt-1 "),
            ("event_id", "e" * (MAX_PROJECTION_IDENTIFIER_LENGTH + 1)),
            ("global_position", True),
            ("global_position", 1.0),
            ("global_position", "1"),
            ("global_position", 0),
            ("global_position", (2**63)),
            ("applied_at", None),
            ("applied_at", "2026-08-20T00:00:00+00:00"),
            ("applied_at", "2026-08-20T08:00:00+08:00"),
            ("applied_at", "2026-08-20T00:00:00.000000Z"),
        )
        for field_name, value in cases:
            with self.subTest(field_name=field_name, value=value):
                with self.assertRaises(ProjectionIntegrityError):
                    self.store._row_to_receipt(self.receipt_row(**{field_name: value}))

    def test_every_offset_operation_rejects_corrupt_persisted_timestamp_before_mutation(
        self,
    ) -> None:
        lease = self.store.claim("task-list", "worker-a", lease_seconds=30)
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                "UPDATE projection_offsets SET lease_expires_at = ? WHERE projection_name = ?",
                ("2026-08-20T00:00:30+00:00", "task-list"),
            )
            connection.commit()
        finally:
            connection.close()

        handler_calls = 0

        def handler(_transaction: ProjectionTransaction, _event: UpcastedEvent) -> None:
            nonlocal handler_calls
            handler_calls += 1

        event = UpcastedEvent(
            stored({"title": "research"}),
            {"title": "research"},
            1,
            1,
        )
        operations: tuple[tuple[str, Callable[[], object]], ...] = (
            ("load", lambda: self.store.load("task-list")),
            ("claim", lambda: self.store.claim("task-list", "worker-a")),
            ("renew", lambda: self.store.renew(lease)),
            (
                "advance",
                lambda: self.store.advance(lease, expected_position=0, new_position=1),
            ),
            (
                "apply_event",
                lambda: self.store.apply_event(
                    lease,
                    expected_position=0,
                    event=event,
                    handler=handler,
                ),
            ),
            ("release", lambda: self.store.release(lease)),
        )
        for operation_name, operation in operations:
            with self.subTest(operation=operation_name):
                with self.assertRaises(ProjectionIntegrityError):
                    operation()

        self.assertEqual(handler_calls, 0)
        connection = sqlite3.connect(self.path)
        try:
            offset = connection.execute(
                "SELECT last_global_position, lease_expires_at FROM projection_offsets "
                "WHERE projection_name = ?",
                ("task-list",),
            ).fetchone()
            receipt_count = connection.execute(
                "SELECT COUNT(*) FROM projection_receipts WHERE projection_name = ?",
                ("task-list",),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(offset, (0, "2026-08-20T00:00:30+00:00"))
        self.assertEqual(receipt_count, 0)

    def test_corrupt_receipt_fails_before_deduplication_or_handler(self) -> None:
        lease = self.store.claim("task-list", "worker-a", lease_seconds=30)
        event = UpcastedEvent(
            stored({"title": "research"}),
            {"title": "research"},
            1,
            1,
        )
        self.store.apply_event(
            lease,
            expected_position=0,
            event=event,
            handler=lambda _transaction, _event: None,
        )
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("PRAGMA ignore_check_constraints=ON")
            connection.execute(
                "UPDATE projection_receipts SET global_position = ? "
                "WHERE projection_name = ? AND event_id = ?",
                (1.5, "task-list", "evt-1"),
            )
            connection.commit()
        finally:
            connection.close()

        handler_calls = 0

        def handler(_transaction: ProjectionTransaction, _event: UpcastedEvent) -> None:
            nonlocal handler_calls
            handler_calls += 1

        with self.assertRaises(ProjectionIntegrityError):
            self.store.apply_event(
                lease,
                expected_position=1,
                event=event,
                handler=handler,
            )
        self.assertEqual(handler_calls, 0)
        self.assertEqual(self.store.load("task-list").last_global_position, 1)
        connection = sqlite3.connect(self.path)
        try:
            receipt = connection.execute(
                "SELECT typeof(global_position), global_position "
                "FROM projection_receipts WHERE projection_name = ? AND event_id = ?",
                ("task-list", "evt-1"),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(receipt, ("real", 1.5))

    def test_identifier_and_position_inputs_fail_before_sqlite_binding(self) -> None:
        overlong = "x" * (MAX_PROJECTION_IDENTIFIER_LENGTH + 1)
        for projection_name, owner_id in (
            (overlong, "worker-a"),
            (" task-list", "worker-a"),
            ("task-list", overlong),
            ("task-list", "worker-a "),
        ):
            with self.subTest(projection_name=projection_name, owner_id=owner_id):
                with self.assertRaises(ValueError):
                    self.store.claim(projection_name, owner_id)

        lease = self.store.claim("task-list", "worker-a")
        with self.assertRaisesRegex(ValueError, "64-bit SQLite integer"):
            self.store.advance(
                lease,
                expected_position=0,
                new_position=(2**63),
            )

        handler_calls = 0

        def handler(_transaction: ProjectionTransaction, _event: UpcastedEvent) -> None:
            nonlocal handler_calls
            handler_calls += 1

        too_far = UpcastedEvent(
            StoredEvent(
                DomainEvent(
                    "session:s1",
                    "task.created",
                    {},
                    "user",
                    event_id="evt-too-far",
                ),
                sequence=1,
                global_position=(2**63),
            ),
            {},
            1,
            1,
        )
        with self.assertRaisesRegex(ValueError, "64-bit SQLite integer"):
            self.store.apply_event(
                lease,
                expected_position=0,
                event=too_far,
                handler=handler,
            )
        long_id = UpcastedEvent(
            StoredEvent(
                DomainEvent(
                    "session:s1",
                    "task.created",
                    {},
                    "user",
                    event_id=overlong,
                ),
                sequence=1,
                global_position=1,
            ),
            {},
            1,
            1,
        )
        with self.assertRaises(ValueError):
            self.store.apply_event(
                lease,
                expected_position=0,
                event=long_id,
                handler=handler,
            )
        self.assertEqual(handler_calls, 0)

    def test_offset_survives_close_and_reopen_on_existing_database(self) -> None:
        lease = self.store.claim("task-list", "worker-a", lease_seconds=30)
        advanced = self.store.advance(lease, expected_position=0, new_position=7)
        self.assertEqual(advanced.last_global_position, 7)
        self.store.close()

        self.store = SQLiteProjectionOffsetStore(self.path, clock=self.clock)

        self.assertEqual(self.store.load("task-list").last_global_position, 7)

    def test_unseen_projection_has_virtual_zero_checkpoint(self) -> None:
        offset = self.store.load("new-view")
        self.assertEqual(offset.last_global_position, 0)
        self.assertEqual(offset.owner_epoch, 0)
        self.assertIsNone(offset.owner_id)

    def test_active_owner_blocks_takeover_then_expiry_increments_epoch(self) -> None:
        first = self.store.claim("task-list", "worker-a", lease_seconds=30)
        with self.assertRaises(ProjectionLeaseConflictError):
            self.store.claim("task-list", "worker-b", lease_seconds=30)

        self.clock.value = "2026-08-20T00:00:31Z"
        second = self.store.claim("task-list", "worker-b", lease_seconds=30)

        self.assertEqual(second.owner_epoch, first.owner_epoch + 1)
        with self.assertRaises(ProjectionLeaseLostError):
            self.store.advance(first, expected_position=0, new_position=1)
        advanced = self.store.advance(second, expected_position=0, new_position=1)
        self.assertEqual(advanced.last_global_position, 1)

    def test_reclaim_by_same_owner_fences_its_old_incarnation(self) -> None:
        first = self.store.claim("task-list", "worker-a", lease_seconds=30)
        second = self.store.claim("task-list", "worker-a", lease_seconds=30)

        self.assertGreater(second.owner_epoch, first.owner_epoch)
        with self.assertRaises(ProjectionLeaseLostError):
            self.store.renew(first)
        renewed = self.store.renew(second)
        self.assertEqual(renewed.owner_epoch, second.owner_epoch)

    def test_offset_advance_is_monotonic_compare_and_swap(self) -> None:
        lease = self.store.claim("task-list", "worker-a", lease_seconds=30)
        self.store.advance(lease, expected_position=0, new_position=5)

        with self.assertRaises(ProjectionOffsetConflictError):
            self.store.advance(lease, expected_position=0, new_position=6)
        with self.assertRaises(ValueError):
            self.store.advance(lease, expected_position=5, new_position=5)
        self.assertEqual(self.store.load("task-list").last_global_position, 5)

    def test_expired_lease_cannot_renew_advance_or_release(self) -> None:
        lease = self.store.claim("task-list", "worker-a", lease_seconds=30)
        self.clock.value = "2026-08-20T00:00:30Z"

        with self.assertRaises(ProjectionLeaseLostError):
            self.store.renew(lease)
        with self.assertRaises(ProjectionLeaseLostError):
            self.store.advance(lease, expected_position=0, new_position=1)
        with self.assertRaises(ProjectionLeaseLostError):
            self.store.release(lease)

    def test_concurrent_claims_have_exactly_one_owner(self) -> None:
        contender = SQLiteProjectionOffsetStore(self.path, clock=self.clock)
        barrier = threading.Barrier(2)
        winners: list[str] = []
        conflicts: list[str] = []
        failures: list[BaseException] = []

        def claim(store: SQLiteProjectionOffsetStore, owner: str) -> None:
            try:
                barrier.wait()
                store.claim("task-list", owner, lease_seconds=30)
                winners.append(owner)
            except ProjectionLeaseConflictError:
                conflicts.append(owner)
            except BaseException as exc:  # pragma: no cover - assertion reports details
                failures.append(exc)

        threads = (
            threading.Thread(target=claim, args=(self.store, "worker-a")),
            threading.Thread(target=claim, args=(contender, "worker-b")),
        )
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        contender.close()

        self.assertFalse(failures)
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(conflicts), 1)
        self.assertFalse(any(thread.is_alive() for thread in threads))


class DurableProjectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tempdir.name) / "events.sqlite3")
        self.clock = MutableClock()
        self.events = SQLiteEventStore(self.path)
        self.offsets = SQLiteProjectionOffsetStore(self.path, clock=self.clock)
        self.registry = EventUpcasterRegistry()
        self.registry.register_event_type(
            "task.created",
            current_version=2,
            decoder=decode_mapping,
        )
        self.registry.register_upcaster(
            "task.created",
            from_version=1,
            upcaster=lambda payload: {"title": payload["name"]},
        )
        self.registry.seal()
        self.events.append(
            DomainEvent(
                "session:s1",
                "task.created",
                {"name": "research"},
                "user",
                event_id="evt-1",
            )
        )
        self.events.append(
            DomainEvent(
                "session:s1",
                "task.created",
                {SCHEMA_VERSION_FIELD: 2, "title": "ship"},
                "user",
                event_id="evt-2",
            )
        )

    def tearDown(self) -> None:
        self.offsets.close()
        self.events.close()
        self.tempdir.cleanup()

    @staticmethod
    def create_view(transaction: ProjectionTransaction) -> None:
        transaction.execute(
            """
            CREATE TABLE IF NOT EXISTS task_view (
                event_id TEXT PRIMARY KEY,
                title TEXT NOT NULL
            )
            """
        )

    def read_view(self) -> list[tuple[str, str]]:
        connection = sqlite3.connect(self.path)
        try:
            rows = connection.execute(
                "SELECT event_id, title FROM task_view ORDER BY event_id"
            ).fetchall()
            return [(str(row[0]), str(row[1])) for row in rows]
        except sqlite3.OperationalError:
            return []
        finally:
            connection.close()

    def test_projector_constructor_rejects_unsealed_registry(self) -> None:
        unsealed = EventUpcasterRegistry()
        unsealed.register_event_type(
            "task.created",
            current_version=1,
            decoder=decode_mapping,
        )

        with self.assertRaisesRegex(UnsealedEventSchemaRegistryError, "must be sealed"):
            DurableProjector(
                "unsealed",
                "worker-a",
                self.events,
                self.offsets,
                unsealed,
                lambda _transaction, _event: None,
            )

    def test_projector_upcasts_and_checkpoints_a_bounded_batch(self) -> None:
        def handler(transaction: ProjectionTransaction, event: UpcastedEvent) -> None:
            self.create_view(transaction)
            transaction.execute(
                "INSERT INTO task_view (event_id, title) VALUES (?, ?)",
                (event.stored_event.event.event_id, event.payload["title"]),
            )

        projector = DurableProjector(
            "task-list",
            "worker-a",
            self.events,
            self.offsets,
            self.registry,
            handler,
        )

        first = projector.run_once(limit=1)
        second = projector.run_once(limit=10)
        empty = projector.run_once(limit=10)

        self.assertEqual((first.scanned_count, first.applied_count), (1, 1))
        self.assertEqual((second.scanned_count, second.applied_count), (1, 1))
        self.assertEqual((empty.scanned_count, empty.applied_count), (0, 0))
        self.assertEqual(first.last_global_position, 1)
        self.assertEqual(second.last_global_position, 2)
        self.assertEqual(self.read_view(), [("evt-1", "research"), ("evt-2", "ship")])

    def test_handler_failure_rolls_back_view_receipt_and_offset_then_replays(self) -> None:
        failed_once = False

        def flaky_handler(transaction: ProjectionTransaction, event: UpcastedEvent) -> None:
            nonlocal failed_once
            self.create_view(transaction)
            transaction.execute(
                "INSERT INTO task_view (event_id, title) VALUES (?, ?)",
                (event.stored_event.event.event_id, event.payload["title"]),
            )
            if event.stored_event.event.event_id == "evt-2" and not failed_once:
                failed_once = True
                raise RuntimeError("simulated crash after read-model write")

        projector = DurableProjector(
            "task-list",
            "worker-a",
            self.events,
            self.offsets,
            self.registry,
            flaky_handler,
        )

        with self.assertRaisesRegex(RuntimeError, "simulated crash"):
            projector.run_once(limit=10)

        self.assertEqual(self.offsets.load("task-list").last_global_position, 1)
        self.assertEqual(self.read_view(), [("evt-1", "research")])

        recovered = DurableProjector(
            "task-list",
            "worker-b",
            self.events,
            self.offsets,
            self.registry,
            flaky_handler,
        ).run_once(limit=10)

        self.assertEqual(recovered.applied_count, 1)
        self.assertEqual(recovered.last_global_position, 2)
        self.assertEqual(self.read_view(), [("evt-1", "research"), ("evt-2", "ship")])

    def test_handler_cannot_commit_partial_view_writes(self) -> None:
        def handler(transaction: ProjectionTransaction, event: UpcastedEvent) -> None:
            self.create_view(transaction)
            event_id = event.stored_event.event.event_id
            transaction.execute(
                "INSERT INTO task_view (event_id, title) VALUES (?, ?)",
                (event_id, event.payload["title"]),
            )
            with self.assertRaises(sqlite3.DatabaseError):
                transaction.execute("COMMIT")
            transaction.execute(
                "INSERT INTO task_view (event_id, title) VALUES (?, ?)",
                (f"{event_id}-after-commit", event.payload["title"]),
            )
            raise RuntimeError("simulated failure after rejected handler commit")

        projector = DurableProjector(
            "commit-boundary",
            "worker-a",
            self.events,
            self.offsets,
            self.registry,
            handler,
        )

        with self.assertRaisesRegex(RuntimeError, "rejected handler commit"):
            projector.run_once(limit=1)

        self.assertEqual(self.read_view(), [])
        self.assertEqual(self.offsets.load("commit-boundary").last_global_position, 0)
        connection = sqlite3.connect(self.path)
        try:
            receipt_count = connection.execute(
                "SELECT COUNT(*) FROM projection_receipts WHERE projection_name = ?",
                ("commit-boundary",),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(receipt_count, 0)

    def test_statement_result_does_not_expose_cursor_connection(self) -> None:
        observed: list[ProjectionStatementResult] = []

        def handler(transaction: ProjectionTransaction, _event: UpcastedEvent) -> None:
            result = transaction.execute("SELECT ? AS answer", (42,))
            self.assertIsInstance(result, ProjectionStatementResult)
            self.assertNotIsInstance(result, sqlite3.Cursor)
            with self.assertRaises(AttributeError):
                attrgetter("connection")(result)
            observed.append(result)

        result = DurableProjector(
            "connection-free-result",
            "worker-a",
            self.events,
            self.offsets,
            self.registry,
            handler,
        ).run_once(limit=1)

        self.assertEqual(result.applied_count, 1)
        self.assertEqual(observed[0].columns, ("answer",))
        self.assertEqual(observed[0].rows, ((42,),))

    def test_handler_sql_control_statements_are_denied_without_blocking_finalize(self) -> None:
        statements = (
            "ROLLBACK",
            "SAVEPOINT handler_scope",
            "PRAGMA user_version",
            "ATTACH DATABASE ':memory:' AS handler_db",
            "DETACH DATABASE handler_db",
        )

        for index, statement in enumerate(statements):
            with self.subTest(statement=statement):
                projection_name = f"sql-control-{index}"

                def handler(
                    transaction: ProjectionTransaction,
                    _event: UpcastedEvent,
                    sql: str = statement,
                ) -> None:
                    with self.assertRaises(sqlite3.DatabaseError):
                        transaction.execute(sql)

                run = DurableProjector(
                    projection_name,
                    "worker-a",
                    self.events,
                    self.offsets,
                    self.registry,
                    handler,
                ).run_once(limit=1)

                self.assertEqual(run.applied_count, 1)
                self.assertEqual(
                    self.offsets.load(projection_name).last_global_position,
                    1,
                )

    def test_handler_cannot_access_or_change_framework_tables(self) -> None:
        statements = (
            "SELECT * FROM projection_offsets",
            "UPDATE projection_offsets SET last_global_position = 99",
            "DELETE FROM projection_receipts",
            "DROP TABLE projection_receipts",
            "SELECT * FROM qe_schema_migrations",
            "CREATE INDEX handler_index ON projection_offsets(last_global_position)",
        )

        def handler(transaction: ProjectionTransaction, _event: UpcastedEvent) -> None:
            for statement in statements:
                with self.assertRaises(sqlite3.DatabaseError):
                    transaction.execute(statement)

        result = DurableProjector(
            "framework-boundary",
            "worker-a",
            self.events,
            self.offsets,
            self.registry,
            handler,
        ).run_once(limit=1)

        self.assertEqual(result.applied_count, 1)
        self.assertEqual(self.offsets.load("framework-boundary").last_global_position, 1)

    def test_handler_authorizer_is_restored_after_base_exception(self) -> None:
        def interrupted_handler(
            transaction: ProjectionTransaction,
            _event: UpcastedEvent,
        ) -> None:
            transaction.execute(
                "CREATE TABLE IF NOT EXISTS interrupted_view (event_id TEXT PRIMARY KEY)"
            )
            raise KeyboardInterrupt("simulated handler interruption")

        interrupted = DurableProjector(
            "base-exception",
            "worker-a",
            self.events,
            self.offsets,
            self.registry,
            interrupted_handler,
        )
        with self.assertRaisesRegex(KeyboardInterrupt, "handler interruption"):
            interrupted.run_once(limit=1)

        calls = 0

        def recovered_handler(
            transaction: ProjectionTransaction,
            event: UpcastedEvent,
        ) -> None:
            nonlocal calls
            calls += 1
            self.create_view(transaction)
            transaction.execute(
                "INSERT INTO task_view (event_id, title) VALUES (?, ?)",
                (event.stored_event.event.event_id, event.payload["title"]),
            )

        recovered = DurableProjector(
            "base-exception",
            "worker-b",
            self.events,
            self.offsets,
            self.registry,
            recovered_handler,
        ).run_once(limit=1)

        self.assertEqual(calls, 1)
        self.assertEqual(recovered.last_global_position, 1)
        self.assertEqual(self.read_view(), [("evt-1", "research")])

    def test_receipt_makes_direct_concurrent_replay_idempotent(self) -> None:
        calls = 0

        def handler(transaction: ProjectionTransaction, event: UpcastedEvent) -> None:
            nonlocal calls
            calls += 1
            self.create_view(transaction)
            transaction.execute(
                "INSERT INTO task_view (event_id, title) VALUES (?, ?)",
                (event.stored_event.event.event_id, event.payload["title"]),
            )

        lease = self.offsets.claim("task-list", "worker-a", lease_seconds=30)
        event = self.registry.upcast(self.events.read_all(limit=1)[0])
        barrier = threading.Barrier(2)
        results: list[bool] = []
        failures: list[BaseException] = []

        def apply() -> None:
            try:
                barrier.wait()
                result = self.offsets.apply_event(
                    lease,
                    expected_position=0,
                    event=event,
                    handler=handler,
                )
                results.append(result.applied)
            except BaseException as exc:  # pragma: no cover - assertion reports details
                failures.append(exc)

        threads = (threading.Thread(target=apply), threading.Thread(target=apply))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertFalse(failures)
        self.assertCountEqual(results, [True, False])
        self.assertEqual(calls, 1)
        self.assertEqual(self.read_view(), [("evt-1", "research")])
        self.assertFalse(any(thread.is_alive() for thread in threads))

    def test_future_schema_never_invokes_handler_or_advances(self) -> None:
        future = self.events.append(
            DomainEvent(
                "session:s1",
                "task.created",
                {SCHEMA_VERSION_FIELD: 3, "title": "future"},
                "user",
                event_id="evt-3",
            )
        )
        calls = 0

        def handler(_transaction: ProjectionTransaction, _event: UpcastedEvent) -> None:
            nonlocal calls
            calls += 1

        projector = DurableProjector(
            "future-view",
            "worker-a",
            self.events,
            self.offsets,
            self.registry,
            handler,
        )
        projector.run_once(limit=2)
        with self.assertRaises(FutureEventSchemaVersionError):
            projector.run_once(limit=10)

        self.assertEqual(calls, 2)
        self.assertEqual(
            self.offsets.load("future-view").last_global_position,
            future.global_position - 1,
        )


if __name__ == "__main__":
    unittest.main()
