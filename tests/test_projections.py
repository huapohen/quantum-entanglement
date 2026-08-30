import math
import os
import sqlite3
import tempfile
import threading
import unittest
from collections.abc import Mapping, MutableMapping
from copy import copy, deepcopy
from decimal import Decimal
from operator import attrgetter, setitem
from pathlib import Path
from typing import Any, Callable, Optional, cast
from unittest.mock import patch

from quantum_entanglement.events import DomainEvent, StoredEvent
from quantum_entanglement.projections import (
    MAX_EVENT_PAYLOAD_DEPTH,
    MAX_EVENT_PAYLOAD_NODES,
    MAX_PROJECTION_BATCH_SIZE,
    MAX_PROJECTION_BUSY_TIMEOUT_SECONDS,
    MAX_PROJECTION_IDENTIFIER_LENGTH,
    MAX_PROJECTION_LEASE_SECONDS,
    MIN_PROJECTION_SQLITE_VERSION,
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
    ProjectionOffsetProcessMismatchError,
    ProjectionSchemaError,
    ProjectionSourceIntegrityError,
    ProjectionStatementResult,
    ProjectionTransaction,
    ProjectionTransactionClosedError,
    ProjectionTransactionThreadError,
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


def invalid_projection_lease_seconds() -> tuple[object, ...]:
    return (
        True,
        False,
        "30",
        Decimal("30"),
        None,
        object(),
        math.nan,
        math.inf,
        -math.inf,
        0,
        -0.0,
        -1,
        MAX_PROJECTION_LEASE_SECONDS + 1,
        float(MAX_PROJECTION_LEASE_SECONDS) + 0.5,
        10**100,
    )


def invalid_projection_busy_timeout_seconds() -> tuple[object, ...]:
    return (
        True,
        False,
        "5",
        Decimal("5"),
        None,
        object(),
        math.nan,
        math.inf,
        -math.inf,
        0,
        -0.0,
        -1,
        MAX_PROJECTION_BUSY_TIMEOUT_SECONDS + 1,
        float(MAX_PROJECTION_BUSY_TIMEOUT_SECONDS) + 0.5,
        10**100,
    )


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


class StaticEventSource:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[tuple[int, int]] = []

    def read_all(
        self,
        after_position: int = 0,
        limit: int = 1000,
    ) -> tuple[StoredEvent, ...]:
        self.calls.append((after_position, limit))
        return cast(tuple[StoredEvent, ...], self.result)


class CorruptingSchemaCursor:
    def __init__(
        self,
        cursor: sqlite3.Cursor,
        row_transform: Callable[[list[tuple[Any, ...]]], object],
        description_transform: Callable[[object], object],
        fetch_sizes: list[int],
    ) -> None:
        self._cursor = cursor
        self._row_transform = row_transform
        self._fetch_sizes = fetch_sizes
        self.description = description_transform(cursor.description)

    def fetchmany(self, size: int) -> Any:
        self._fetch_sizes.append(size)
        rows = [tuple(row) for row in self._cursor.fetchmany(size)]
        return self._row_transform(rows)

    def close(self) -> None:
        self._cursor.close()


class CorruptingSchemaConnection:
    def __init__(
        self,
        connection: sqlite3.Connection,
        target: str,
        row_transform: Callable[[list[tuple[Any, ...]]], object],
        description_transform: Callable[[object], object],
    ) -> None:
        self._connection = connection
        self._target = target.casefold()
        self._row_transform = row_transform
        self._description_transform = description_transform
        self.statements: list[str] = []
        self.fetch_sizes: list[int] = []

    @property
    def in_transaction(self) -> bool:
        return self._connection.in_transaction

    def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
        self.statements.append(sql)
        cursor = self._connection.execute(sql, parameters)
        normalized = " ".join(sql.split()).casefold()
        if self._target not in normalized:
            return cursor
        return cast(
            sqlite3.Cursor,
            CorruptingSchemaCursor(
                cursor,
                self._row_transform,
                self._description_transform,
                self.fetch_sizes,
            ),
        )


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

    def exact_projection_path(self, label: str) -> str:
        path = str(Path(self.tempdir.name) / f"{label}.sqlite3")
        store = SQLiteProjectionOffsetStore(path, clock=self.clock)
        store.close()
        return path

    def rewrite_projection_table(
        self,
        path: str,
        table_name: str,
        transform: Callable[[str], str],
    ) -> None:
        connection = sqlite3.connect(path)
        try:
            row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table_name,),
            ).fetchone()
            self.assertIsNotNone(row)
            original_sql = row[0]
            self.assertIsInstance(original_sql, str)
            rewritten_sql = transform(cast(str, original_sql))
            self.assertNotEqual(rewritten_sql, original_sql)
            connection.execute(f'DROP TABLE "{table_name}"')
            connection.execute(rewritten_sql)
            connection.commit()
        finally:
            connection.close()

    def test_invalid_busy_timeouts_fail_before_filesystem_connection_or_sql(self) -> None:
        for index, invalid in enumerate(invalid_projection_busy_timeout_seconds()):
            with self.subTest(busy_timeout_seconds=invalid):
                parent = Path(self.tempdir.name) / f"invalid-timeout-{index}"
                path = parent / "projection.sqlite3"
                self.assertFalse(parent.exists())
                with patch("quantum_entanglement.projections.os.makedirs") as makedirs:
                    with patch("quantum_entanglement.projections.sqlite3.connect") as connect:
                        with patch.object(
                            SQLiteProjectionOffsetStore,
                            "_enable_wal",
                        ) as enable_wal:
                            with self.assertRaisesRegex(
                                ValueError,
                                "exact finite int or float",
                            ):
                                SQLiteProjectionOffsetStore(
                                    str(path),
                                    clock=self.clock,
                                    busy_timeout_seconds=cast(Any, invalid),
                                )
                makedirs.assert_not_called()
                connect.assert_not_called()
                enable_wal.assert_not_called()
                self.assertFalse(parent.exists())
                self.assertFalse(path.exists())

    def test_unsupported_sqlite_fails_before_filesystem_connection_or_sql(self) -> None:
        parent = Path(self.tempdir.name) / "unsupported-sqlite"
        path = parent / "projection.sqlite3"
        unsupported = (
            MIN_PROJECTION_SQLITE_VERSION[0],
            MIN_PROJECTION_SQLITE_VERSION[1],
            MIN_PROJECTION_SQLITE_VERSION[2] - 1,
        )
        with patch(
            "quantum_entanglement.projections.sqlite3.sqlite_version_info",
            unsupported,
        ):
            with patch("quantum_entanglement.projections.os.makedirs") as makedirs:
                with patch("quantum_entanglement.projections.sqlite3.connect") as connect:
                    with self.assertRaisesRegex(ProjectionSchemaError, "SQLite 3.8.9 or newer"):
                        SQLiteProjectionOffsetStore(str(path), clock=self.clock)

        makedirs.assert_not_called()
        connect.assert_not_called()
        self.assertFalse(parent.exists())
        self.assertFalse(path.exists())

    def test_fork_inherited_offset_store_rejects_before_touching_sqlite(self) -> None:
        read_fd, write_fd = os.pipe()
        child_pid = os.fork()
        if child_pid == 0:
            os.close(read_fd)
            try:
                self.store.load("task-list")
            except ProjectionOffsetProcessMismatchError:
                os.write(write_fd, b"ok")
            except BaseException:
                os.write(write_fd, b"unexpected")
            else:
                os.write(write_fd, b"missing")
            finally:
                os.close(write_fd)
            os._exit(0)
        os.close(write_fd)
        try:
            result = os.read(read_fd, 32)
        finally:
            os.close(read_fd)
        _, status = os.waitpid(child_pid, 0)
        self.assertEqual(status, 0)
        self.assertEqual(result, b"ok")

    def test_busy_timeout_boundaries_round_up_to_milliseconds_and_reopen(self) -> None:
        smallest_positive_float = math.nextafter(0.0, math.inf)
        cases = (
            (smallest_positive_float, 1),
            (0.001, 1),
            (0.0010000001, 2),
            (5, 5_000),
            (MAX_PROJECTION_BUSY_TIMEOUT_SECONDS, 300_000),
            (float(MAX_PROJECTION_BUSY_TIMEOUT_SECONDS), 300_000),
        )
        for index, (timeout_seconds, expected_ms) in enumerate(cases):
            with self.subTest(busy_timeout_seconds=timeout_seconds):
                path = str(Path(self.tempdir.name) / f"valid-timeout-{index}.sqlite3")
                self.assertEqual(
                    SQLiteProjectionOffsetStore._validate_busy_timeout_seconds(timeout_seconds),
                    expected_ms,
                )
                store = SQLiteProjectionOffsetStore(
                    path,
                    clock=self.clock,
                    busy_timeout_seconds=timeout_seconds,
                )
                try:
                    configured_ms = store._connection.execute("PRAGMA busy_timeout").fetchone()[0]
                    journal_mode = store._connection.execute("PRAGMA journal_mode").fetchone()[0]
                    self.assertEqual(configured_ms, expected_ms)
                    self.assertEqual(journal_mode, "wal")
                    lease = store.claim(f"timeout-{index}", "worker-a")
                    store.advance(lease, expected_position=0, new_position=1)
                    store.release(lease)
                finally:
                    store.close()

                reopened = SQLiteProjectionOffsetStore(
                    path,
                    clock=self.clock,
                    busy_timeout_seconds=timeout_seconds,
                )
                try:
                    reopened_ms = reopened._connection.execute("PRAGMA busy_timeout").fetchone()[0]
                    self.assertEqual(reopened_ms, expected_ms)
                    self.assertEqual(
                        reopened.load(f"timeout-{index}").last_global_position,
                        1,
                    )
                finally:
                    reopened.close()

    def test_busy_timeout_normalization_drives_connect_pragma_and_wal(self) -> None:
        path = str(Path(self.tempdir.name) / "normalized-timeout.sqlite3")
        real_connect = sqlite3.connect
        observed_timeouts: list[float] = []
        initial_busy_timeouts: list[int] = []

        def tracked_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            observed_timeouts.append(cast(float, kwargs["timeout"]))
            connection = cast(sqlite3.Connection, real_connect(*args, **kwargs))
            initial_busy_timeouts.append(
                cast(int, connection.execute("PRAGMA busy_timeout").fetchone()[0])
            )
            return connection

        with patch(
            "quantum_entanglement.projections.sqlite3.connect",
            side_effect=tracked_connect,
        ):
            with patch.object(SQLiteProjectionOffsetStore, "_enable_wal") as enable_wal:
                store = SQLiteProjectionOffsetStore(
                    path,
                    clock=self.clock,
                    busy_timeout_seconds=1.001,
                )
        try:
            configured_ms = store._connection.execute("PRAGMA busy_timeout").fetchone()[0]
            self.assertEqual(configured_ms, 1001)
        finally:
            store.close()

        self.assertEqual(observed_timeouts, [math.nextafter(1.001, math.inf)])
        self.assertEqual(initial_busy_timeouts, [1001])
        enable_wal.assert_called_once()
        self.assertEqual(enable_wal.call_args.args[1], 1001)

    def test_exact_schema_revalidation_uses_the_read_only_fast_path(self) -> None:
        statements: list[str] = []
        self.store._connection.set_trace_callback(statements.append)
        try:
            self.store._initialize()
        finally:
            self.store._connection.set_trace_callback(None)

        normalized = tuple(statement.lstrip().upper() for statement in statements)
        self.assertTrue(any(statement.startswith("SELECT") for statement in normalized))
        self.assertFalse(
            any(
                statement.startswith(("BEGIN", "CREATE", "COMMIT", "ROLLBACK"))
                for statement in normalized
            )
        )

    def test_schema_inspection_rejects_malformed_exact_rows_before_writes(self) -> None:
        def identity_rows(rows: list[tuple[Any, ...]]) -> object:
            return rows

        def identity_description(description: object) -> object:
            return description

        def replace_cell(
            row_index: int,
            column_index: int,
            value: object,
        ) -> Callable[[list[tuple[Any, ...]]], object]:
            def transform(rows: list[tuple[Any, ...]]) -> object:
                changed = list(rows)
                row = list(changed[row_index])
                row[column_index] = value
                changed[row_index] = tuple(row)
                return changed

            return transform

        def remove_column(rows: list[tuple[Any, ...]]) -> object:
            return [rows[0][:-1], *rows[1:]]

        def add_column(rows: list[tuple[Any, ...]]) -> object:
            return [(*rows[0], "unexpected"), *rows[1:]]

        def duplicate_first(rows: list[tuple[Any, ...]]) -> object:
            return [rows[0], *rows]

        def reverse_rows(rows: list[tuple[Any, ...]]) -> object:
            return list(reversed(rows))

        def list_row(rows: list[tuple[Any, ...]]) -> object:
            return [list(rows[0]), *rows[1:]]

        def scalar_row(rows: list[tuple[Any, ...]]) -> object:
            return ["malformed", *rows[1:]]

        def tuple_batch(rows: list[tuple[Any, ...]]) -> object:
            return tuple(rows)

        def description_tuple(description: object) -> tuple[Any, ...]:
            return cast(tuple[Any, ...], description)

        def remove_description_column(description: object) -> object:
            return description_tuple(description)[:-1]

        def add_description_column(description: object) -> object:
            return (
                *description_tuple(description),
                ("unexpected", None, None, None, None, None, None),
            )

        def rename_description_column(description: object) -> object:
            columns = list(description_tuple(description))
            column = list(cast(tuple[Any, ...], columns[0]))
            column[0] = "unexpected"
            columns[0] = tuple(column)
            return tuple(columns)

        def bytes_description_name(description: object) -> object:
            columns = list(description_tuple(description))
            column = list(cast(tuple[Any, ...], columns[0]))
            column[0] = b"cid"
            columns[0] = tuple(column)
            return tuple(columns)

        def short_description_entry(description: object) -> object:
            columns = list(description_tuple(description))
            columns[0] = ("cid",)
            return tuple(columns)

        def list_description(description: object) -> object:
            return list(description_tuple(description))

        catalog = "from main.sqlite_master"
        table_info = 'pragma main.table_info("projection_offsets")'
        index_list = 'pragma main.index_list("projection_receipts")'
        index_info = 'pragma main.index_info("idx_projection_receipts_position")'
        index_xinfo = 'pragma main.index_xinfo("idx_projection_receipts_position")'
        cases: tuple[
            tuple[
                str,
                str,
                Callable[[list[tuple[Any, ...]]], object],
                Callable[[object], object],
                Optional[int],
            ],
            ...,
        ] = (
            ("catalog-missing-column", catalog, remove_column, identity_description, 17),
            ("catalog-extra-column", catalog, add_column, identity_description, 17),
            ("catalog-bytes-name", catalog, replace_cell(0, 1, b"index"), identity_description, 17),
            ("catalog-none-name", catalog, replace_cell(0, 1, None), identity_description, 17),
            (
                "catalog-noncanonical-name",
                catalog,
                replace_cell(0, 1, " idx_projection_receipts_position"),
                identity_description,
                17,
            ),
            ("catalog-bool-sql", catalog, replace_cell(0, 3, True), identity_description, 17),
            ("catalog-duplicate", catalog, duplicate_first, identity_description, 17),
            ("catalog-out-of-order", catalog, reverse_rows, identity_description, 17),
            ("table-missing-column", table_info, remove_column, identity_description, 7),
            ("table-extra-column", table_info, add_column, identity_description, 7),
            ("table-bool-cid", table_info, replace_cell(0, 0, False), identity_description, 7),
            ("table-float-cid", table_info, replace_cell(0, 0, 0.0), identity_description, 7),
            ("table-string-cid", table_info, replace_cell(0, 0, "0"), identity_description, 7),
            ("table-negative-cid", table_info, replace_cell(0, 0, -1), identity_description, 7),
            ("table-huge-cid", table_info, replace_cell(0, 0, 2**100), identity_description, 7),
            (
                "table-bytes-name",
                table_info,
                replace_cell(0, 1, b"projection_name"),
                identity_description,
                7,
            ),
            (
                "table-noncanonical-name",
                table_info,
                replace_cell(0, 1, " projection_name"),
                identity_description,
                7,
            ),
            ("table-none-type", table_info, replace_cell(0, 2, None), identity_description, 7),
            ("table-bool-not-null", table_info, replace_cell(0, 3, True), identity_description, 7),
            ("table-duplicate", table_info, duplicate_first, identity_description, 7),
            ("table-out-of-order", table_info, reverse_rows, identity_description, 7),
            ("index-list-missing-column", index_list, remove_column, identity_description, 4),
            ("index-list-extra-column", index_list, add_column, identity_description, 4),
            ("index-list-bool-seq", index_list, replace_cell(0, 0, False), identity_description, 4),
            (
                "index-list-negative-seq",
                index_list,
                replace_cell(0, 0, -1),
                identity_description,
                4,
            ),
            (
                "index-list-huge-seq",
                index_list,
                replace_cell(0, 0, 2**100),
                identity_description,
                4,
            ),
            (
                "index-list-bytes-name",
                index_list,
                replace_cell(0, 1, b"index"),
                identity_description,
                4,
            ),
            (
                "index-list-noncanonical-name",
                index_list,
                replace_cell(0, 1, " idx_projection_receipts_position"),
                identity_description,
                4,
            ),
            (
                "index-list-none-origin",
                index_list,
                replace_cell(0, 3, None),
                identity_description,
                4,
            ),
            (
                "index-list-bool-partial",
                index_list,
                replace_cell(0, 4, False),
                identity_description,
                4,
            ),
            ("index-list-duplicate", index_list, duplicate_first, identity_description, 4),
            ("index-info-missing-column", index_info, remove_column, identity_description, 3),
            ("index-info-extra-column", index_info, add_column, identity_description, 3),
            ("index-info-bool-seq", index_info, replace_cell(0, 0, False), identity_description, 3),
            (
                "index-info-negative-seq",
                index_info,
                replace_cell(0, 0, -1),
                identity_description,
                3,
            ),
            (
                "index-info-huge-cid",
                index_info,
                replace_cell(0, 1, 2**100),
                identity_description,
                3,
            ),
            ("index-info-none-name", index_info, replace_cell(0, 2, None), identity_description, 3),
            (
                "index-info-noncanonical-name",
                index_info,
                replace_cell(0, 2, " projection_name"),
                identity_description,
                3,
            ),
            ("index-info-duplicate", index_info, duplicate_first, identity_description, 3),
            ("index-info-out-of-order", index_info, reverse_rows, identity_description, 3),
            ("index-xinfo-missing-column", index_xinfo, remove_column, identity_description, 4),
            ("index-xinfo-extra-column", index_xinfo, add_column, identity_description, 4),
            (
                "index-xinfo-bool-seq",
                index_xinfo,
                replace_cell(0, 0, False),
                identity_description,
                4,
            ),
            (
                "index-xinfo-negative-seq",
                index_xinfo,
                replace_cell(0, 0, -1),
                identity_description,
                4,
            ),
            (
                "index-xinfo-invalid-cid",
                index_xinfo,
                replace_cell(0, 1, -2),
                identity_description,
                4,
            ),
            (
                "index-xinfo-none-name",
                index_xinfo,
                replace_cell(0, 2, None),
                identity_description,
                4,
            ),
            (
                "index-xinfo-noncanonical-name",
                index_xinfo,
                replace_cell(0, 2, " projection_name"),
                identity_description,
                4,
            ),
            (
                "index-xinfo-bool-desc",
                index_xinfo,
                replace_cell(0, 3, False),
                identity_description,
                4,
            ),
            (
                "index-xinfo-bytes-collation",
                index_xinfo,
                replace_cell(0, 4, b"BINARY"),
                identity_description,
                4,
            ),
            (
                "index-xinfo-bool-key",
                index_xinfo,
                replace_cell(0, 5, True),
                identity_description,
                4,
            ),
            ("index-xinfo-duplicate", index_xinfo, duplicate_first, identity_description, 4),
            ("index-xinfo-out-of-order", index_xinfo, reverse_rows, identity_description, 4),
            ("row-list", table_info, list_row, identity_description, 7),
            ("row-scalar", table_info, scalar_row, identity_description, 7),
            ("batch-tuple", table_info, tuple_batch, identity_description, 7),
            (
                "description-missing-column",
                table_info,
                identity_rows,
                remove_description_column,
                None,
            ),
            (
                "description-extra-column",
                table_info,
                identity_rows,
                add_description_column,
                None,
            ),
            (
                "description-renamed-column",
                table_info,
                identity_rows,
                rename_description_column,
                None,
            ),
            (
                "description-bytes-name",
                table_info,
                identity_rows,
                bytes_description_name,
                None,
            ),
            (
                "description-short-entry",
                table_info,
                identity_rows,
                short_description_entry,
                None,
            ),
            (
                "description-list",
                table_info,
                identity_rows,
                list_description,
                None,
            ),
        )

        for label, target, row_transform, description_transform, expected_fetch_size in cases:
            with self.subTest(label=label):
                connection = CorruptingSchemaConnection(
                    self.store._connection,
                    target,
                    row_transform,
                    description_transform,
                )
                candidate = object.__new__(SQLiteProjectionOffsetStore)
                candidate._lock = threading.RLock()
                candidate._connection = cast(sqlite3.Connection, connection)

                with self.assertRaises(ProjectionSchemaError):
                    candidate._initialize()

                if expected_fetch_size is None:
                    self.assertEqual(connection.fetch_sizes, [])
                else:
                    self.assertEqual(connection.fetch_sizes, [expected_fetch_size])
                normalized = tuple(
                    statement.lstrip().upper() for statement in connection.statements
                )
                self.assertFalse(
                    any(
                        statement.startswith(
                            (
                                "BEGIN",
                                "CREATE",
                                "INSERT",
                                "UPDATE",
                                "DELETE",
                                "REPLACE",
                                "DROP",
                                "ALTER",
                                "COMMIT",
                                "ROLLBACK",
                            )
                        )
                        for statement in normalized
                    )
                )
                self.assertFalse(connection.in_transaction)
                self.assertEqual(self.store.load("schema-write-probe").last_global_position, 0)

    def test_index_list_internal_sequence_and_row_order_do_not_define_schema(self) -> None:
        def identity_description(description: object) -> object:
            return description

        def reverse_rows(rows: list[tuple[Any, ...]]) -> object:
            return list(reversed(rows))

        def reassign_internal_sequence(rows: list[tuple[Any, ...]]) -> object:
            return [(len(rows) - index - 1, *row[1:]) for index, row in enumerate(rows)]

        for label, transform in (
            ("presentation-order", reverse_rows),
            ("internal-sequence-assignment", reassign_internal_sequence),
        ):
            with self.subTest(label=label):
                connection = CorruptingSchemaConnection(
                    self.store._connection,
                    'pragma main.index_list("projection_receipts")',
                    transform,
                    identity_description,
                )
                candidate = object.__new__(SQLiteProjectionOffsetStore)
                candidate._lock = threading.RLock()
                candidate._connection = cast(sqlite3.Connection, connection)

                candidate._initialize()

                self.assertEqual(connection.fetch_sizes, [4])
                self.assertFalse(connection.in_transaction)
                normalized = tuple(
                    statement.lstrip().upper() for statement in connection.statements
                )
                self.assertFalse(
                    any(
                        statement.startswith(("BEGIN", "CREATE", "COMMIT", "ROLLBACK"))
                        for statement in normalized
                    )
                )

    def test_empty_database_initialization_serializes_concurrent_installers(self) -> None:
        path = str(Path(self.tempdir.name) / "concurrent-empty.sqlite3")
        barrier = threading.Barrier(2)
        opened: list[SQLiteProjectionOffsetStore] = []
        failures: list[BaseException] = []

        def initialize() -> None:
            try:
                barrier.wait()
                opened.append(SQLiteProjectionOffsetStore(path, clock=self.clock))
            except BaseException as exc:  # pragma: no cover - assertion reports details
                failures.append(exc)

        threads = tuple(threading.Thread(target=initialize) for _ in range(2))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        for store in opened:
            store.close()

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertFalse(failures)
        self.assertEqual(len(opened), 2)
        verified = SQLiteProjectionOffsetStore(path, clock=self.clock)
        verified.close()

    def test_table_replaced_by_view_fails_before_write_and_closes_connection(
        self,
    ) -> None:
        path = str(Path(self.tempdir.name) / "view-shadow.sqlite3")
        real_connect = sqlite3.connect
        connection = real_connect(path)
        try:
            connection.execute(
                "CREATE VIEW projection_offsets AS SELECT 'shadow' AS projection_name"
            )
            connection.commit()
            before = tuple(
                connection.execute(
                    "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
                ).fetchall()
            )
        finally:
            connection.close()

        traces: list[str] = []

        class TrackingConnection(sqlite3.Connection):
            was_closed = False

            def close(self) -> None:
                self.was_closed = True
                super().close()

        tracked: list[TrackingConnection] = []

        def tracked_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            kwargs["factory"] = TrackingConnection
            candidate = cast(TrackingConnection, real_connect(*args, **kwargs))
            candidate.set_trace_callback(traces.append)
            tracked.append(candidate)
            return candidate

        with patch(
            "quantum_entanglement.projections.sqlite3.connect",
            side_effect=tracked_connect,
        ):
            with self.assertRaises(ProjectionSchemaError):
                SQLiteProjectionOffsetStore(path, clock=self.clock)

        self.assertEqual(len(tracked), 1)
        self.assertTrue(tracked[0].was_closed)
        with self.assertRaises(sqlite3.ProgrammingError):
            tracked[0].execute("SELECT 1")
        normalized = tuple(statement.lstrip().upper() for statement in traces)
        self.assertFalse(any(statement.startswith(("BEGIN", "CREATE")) for statement in normalized))

        connection = real_connect(path)
        try:
            after = tuple(
                connection.execute(
                    "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
                ).fetchall()
            )
        finally:
            connection.close()
        self.assertEqual(after, before)

    def test_case_variant_catalog_collisions_fail_before_transaction_or_write(self) -> None:
        cases = (
            ("table", "CREATE TABLE Projection_Offsets (value INTEGER)"),
            ("view", "CREATE VIEW Projection_Receipts AS SELECT 1 AS value"),
        )
        real_connect = sqlite3.connect
        for label, statement in cases:
            with self.subTest(label=label):
                path = str(Path(self.tempdir.name) / f"case-collision-{label}.sqlite3")
                connection = real_connect(path)
                try:
                    connection.execute(statement)
                    connection.commit()
                    before = tuple(
                        connection.execute(
                            "SELECT type, name, tbl_name, sql "
                            "FROM sqlite_master ORDER BY type, name, tbl_name"
                        ).fetchall()
                    )
                finally:
                    connection.close()

                traces: list[str] = []

                def tracked_connect(
                    *args: Any,
                    _traces: list[str] = traces,
                    **kwargs: Any,
                ) -> sqlite3.Connection:
                    candidate = cast(sqlite3.Connection, real_connect(*args, **kwargs))
                    candidate.set_trace_callback(_traces.append)
                    return candidate

                with patch(
                    "quantum_entanglement.projections.sqlite3.connect",
                    side_effect=tracked_connect,
                ):
                    with self.assertRaises(ProjectionSchemaError):
                        SQLiteProjectionOffsetStore(path, clock=self.clock)

                normalized = tuple(sql.lstrip().upper() for sql in traces)
                self.assertFalse(
                    any(
                        sql.startswith(
                            (
                                "BEGIN",
                                "CREATE",
                                "INSERT",
                                "UPDATE",
                                "DELETE",
                                "REPLACE",
                                "DROP",
                                "ALTER",
                                "COMMIT",
                                "ROLLBACK",
                            )
                        )
                        for sql in normalized
                    )
                )
                connection = real_connect(path)
                try:
                    after = tuple(
                        connection.execute(
                            "SELECT type, name, tbl_name, sql "
                            "FROM sqlite_master ORDER BY type, name, tbl_name"
                        ).fetchall()
                    )
                finally:
                    connection.close()
                self.assertEqual(after, before)

    def test_column_and_table_constraint_drift_fail_closed(self) -> None:
        cases: tuple[tuple[str, str, Callable[[str], str]], ...] = (
            (
                "missing_column",
                "projection_offsets",
                lambda sql: sql.replace(",\n    updated_at TEXT NOT NULL", ""),
            ),
            (
                "added_column",
                "projection_offsets",
                lambda sql: sql.replace(
                    "updated_at TEXT NOT NULL,\n    CHECK",
                    "updated_at TEXT NOT NULL,\n    untrusted_extra TEXT,\n    CHECK",
                ),
            ),
            (
                "declared_type",
                "projection_offsets",
                lambda sql: sql.replace(
                    "owner_epoch INTEGER NOT NULL",
                    "owner_epoch TEXT NOT NULL",
                ),
            ),
            (
                "not_null",
                "projection_offsets",
                lambda sql: sql.replace("owner_id TEXT NOT NULL", "owner_id TEXT"),
            ),
            (
                "default",
                "projection_offsets",
                lambda sql: sql.replace("DEFAULT 0", "DEFAULT 1"),
            ),
            (
                "check",
                "projection_offsets",
                lambda sql: sql.replace(
                    "CHECK(owner_epoch > 0)",
                    "CHECK(owner_epoch >= 0)",
                ),
            ),
            (
                "primary_key",
                "projection_offsets",
                lambda sql: sql.replace("TEXT PRIMARY KEY", "TEXT UNIQUE"),
            ),
            (
                "unique_constraint",
                "projection_receipts",
                lambda sql: sql.replace(
                    "UNIQUE(projection_name, global_position),\n    ",
                    "",
                ),
            ),
            (
                "receipt_check",
                "projection_receipts",
                lambda sql: sql.replace(
                    "CHECK(global_position > 0)",
                    "CHECK(global_position >= 0)",
                ),
            ),
        )
        for label, table_name, transform in cases:
            with self.subTest(label=label):
                path = self.exact_projection_path(label)
                self.rewrite_projection_table(path, table_name, transform)

                with self.assertRaises(ProjectionSchemaError):
                    SQLiteProjectionOffsetStore(path, clock=self.clock)

    def test_explicit_index_column_unique_partial_and_object_type_drift_fail_closed(
        self,
    ) -> None:
        statements = (
            (
                "columns",
                "CREATE INDEX idx_projection_receipts_position "
                "ON projection_receipts(global_position, projection_name)",
            ),
            (
                "unique",
                "CREATE UNIQUE INDEX idx_projection_receipts_position "
                "ON projection_receipts(projection_name, global_position)",
            ),
            (
                "partial",
                "CREATE INDEX idx_projection_receipts_position "
                "ON projection_receipts(projection_name, global_position) "
                "WHERE global_position > 0",
            ),
            (
                "view_shadow",
                "CREATE VIEW idx_projection_receipts_position AS "
                "SELECT projection_name, global_position FROM projection_receipts",
            ),
        )
        for label, replacement in statements:
            with self.subTest(label=label):
                path = self.exact_projection_path(f"index-{label}")
                connection = sqlite3.connect(path)
                try:
                    connection.execute("DROP INDEX idx_projection_receipts_position")
                    connection.execute(replacement)
                    connection.commit()
                finally:
                    connection.close()

                with self.assertRaises(ProjectionSchemaError):
                    SQLiteProjectionOffsetStore(path, clock=self.clock)

    def test_extra_index_and_trigger_attached_to_owned_tables_fail_closed(self) -> None:
        statements = (
            (
                "extra-index",
                "CREATE INDEX projection_offsets_owner_extra ON projection_offsets(owner_id)",
            ),
            (
                "extra-trigger",
                "CREATE TRIGGER projection_offsets_update_extra "
                "AFTER UPDATE ON projection_offsets BEGIN SELECT 1; END",
            ),
        )
        for label, statement in statements:
            with self.subTest(label=label):
                path = self.exact_projection_path(label)
                connection = sqlite3.connect(path)
                try:
                    connection.execute(statement)
                    connection.commit()
                finally:
                    connection.close()

                with self.assertRaises(ProjectionSchemaError):
                    SQLiteProjectionOffsetStore(path, clock=self.clock)

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

    def test_lease_duration_boundaries_are_valid_and_normalized_to_float(self) -> None:
        smallest_positive_float = math.nextafter(0.0, math.inf)
        cases = (
            (smallest_positive_float, "2026-08-20T00:00:00.000001Z"),
            (30, "2026-08-20T00:00:30Z"),
            (MAX_PROJECTION_LEASE_SECONDS, "2026-08-21T00:00:00Z"),
        )
        for index, (lease_seconds, expected_deadline) in enumerate(cases):
            with self.subTest(lease_seconds=lease_seconds):
                normalized = self.store._validate_lease_seconds(lease_seconds)
                self.assertIs(type(normalized), float)
                self.assertEqual(normalized, float(lease_seconds))

                lease = self.store.claim(
                    f"valid-lease-{index}",
                    "worker-a",
                    lease_seconds=lease_seconds,
                )
                self.assertEqual(lease.lease_expires_at, expected_deadline)
                renewed = self.store.renew(
                    lease,
                    lease_seconds=lease_seconds,
                )
                self.assertEqual(renewed.lease_expires_at, expected_deadline)
                self.store.release(renewed)

    def test_invalid_lease_durations_fail_before_sql_clock_and_mutation(self) -> None:
        active = self.store.claim("active-duration", "worker-a", lease_seconds=30)
        connection = sqlite3.connect(self.path)
        try:
            active_before = connection.execute(
                "SELECT * FROM projection_offsets WHERE projection_name = ?",
                ("active-duration",),
            ).fetchone()
        finally:
            connection.close()

        for index, invalid in enumerate(invalid_projection_lease_seconds()):
            with self.subTest(lease_seconds=invalid):
                statements: list[str] = []
                self.store._connection.set_trace_callback(statements.append)
                try:
                    with patch.object(self.store, "_clock", wraps=self.clock) as clock:
                        with self.assertRaisesRegex(
                            ValueError,
                            "exact finite int or float",
                        ):
                            self.store.claim(
                                f"invalid-duration-{index}",
                                "worker-a",
                                lease_seconds=cast(Any, invalid),
                            )
                        with self.assertRaisesRegex(
                            ValueError,
                            "exact finite int or float",
                        ):
                            self.store.renew(
                                active,
                                lease_seconds=cast(Any, invalid),
                            )
                    clock.assert_not_called()
                finally:
                    self.store._connection.set_trace_callback(None)
                self.assertEqual(statements, [])

        connection = sqlite3.connect(self.path)
        try:
            active_after = connection.execute(
                "SELECT * FROM projection_offsets WHERE projection_name = ?",
                ("active-duration",),
            ).fetchone()
            invalid_rows = connection.execute(
                "SELECT COUNT(*) FROM projection_offsets "
                "WHERE projection_name LIKE 'invalid-duration-%'",
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(active_after, active_before)
        self.assertEqual(invalid_rows, 0)
        self.store.release(active)

    def test_lease_deadline_revalidates_and_stabilizes_datetime_overflow(self) -> None:
        for invalid in (True, Decimal("1"), math.nan, MAX_PROJECTION_LEASE_SECONDS + 1):
            with self.subTest(lease_seconds=invalid):
                with self.assertRaisesRegex(ValueError, "exact finite int or float"):
                    self.store._lease_deadline(
                        "2026-08-20T00:00:00Z",
                        invalid,
                    )

        self.clock.value = "9999-12-31T23:59:59.999999Z"
        with self.assertRaisesRegex(
            ValueError,
            "lease deadline exceeds the supported datetime range",
        ):
            self.store.claim("overflow-duration", "worker-a", lease_seconds=1)
        self.assertFalse(self.store._connection.in_transaction)
        self.assertEqual(
            self.store.load("overflow-duration").last_global_position,
            0,
        )

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

    def test_begin_after_success_failures_roll_back_release_lock_and_allow_retry(self) -> None:
        class RaiseAfterBeginConnection(sqlite3.Connection):
            failure: Optional[BaseException] = None
            armed_probe_failure: Optional[BaseException] = None
            probe_failure: Optional[BaseException] = None

            @property
            def in_transaction(self) -> bool:
                failure = self.probe_failure
                if failure is not None:
                    self.probe_failure = None
                    raise failure
                return super().in_transaction

            def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
                cursor = super().execute(sql, parameters)
                failure = self.failure
                if " ".join(sql.split()).upper() == "BEGIN IMMEDIATE" and failure is not None:
                    self.failure = None
                    self.probe_failure = self.armed_probe_failure
                    self.armed_probe_failure = None
                    cursor.close()
                    raise failure
                return cursor

        real_connect = sqlite3.connect
        failures: tuple[tuple[str, BaseException, Optional[BaseException]], ...] = (
            ("exception", RuntimeError("raise after successful BEGIN"), None),
            ("base-exception", KeyboardInterrupt("interrupt after successful BEGIN"), None),
            (
                "begin-and-probe",
                KeyboardInterrupt("interrupt after successful BEGIN"),
                RuntimeError("transaction ownership probe failed"),
            ),
        )
        for label, injected_failure, probe_failure in failures:
            with self.subTest(label=label):
                path = str(Path(self.tempdir.name) / f"begin-after-success-{label}.sqlite3")
                opened: list[RaiseAfterBeginConnection] = []

                def tracked_connect(
                    *args: Any,
                    _opened: list[RaiseAfterBeginConnection] = opened,
                    **kwargs: Any,
                ) -> sqlite3.Connection:
                    kwargs["factory"] = RaiseAfterBeginConnection
                    connection = cast(
                        RaiseAfterBeginConnection,
                        real_connect(*args, **kwargs),
                    )
                    _opened.append(connection)
                    return connection

                with patch(
                    "quantum_entanglement.projections.sqlite3.connect",
                    side_effect=tracked_connect,
                ):
                    store = SQLiteProjectionOffsetStore(path, clock=self.clock)
                try:
                    self.assertEqual(len(opened), 1)
                    connection = opened[0]
                    prior = store.claim(f"prior-{label}", "worker-a")
                    store.release(prior)

                    traces: list[str] = []
                    connection.set_trace_callback(traces.append)
                    connection.failure = injected_failure
                    connection.armed_probe_failure = probe_failure
                    try:
                        store.claim(f"failed-{label}", "worker-a")
                    except BaseException as raised:
                        self.assertIs(raised, injected_failure)
                    else:  # pragma: no cover - assertion reports missing fault injection
                        self.fail("raise-after-BEGIN fault was not propagated")
                    finally:
                        connection.set_trace_callback(None)

                    normalized = tuple(statement.lstrip().upper() for statement in traces)
                    self.assertIn("BEGIN IMMEDIATE", normalized)
                    self.assertIn("ROLLBACK", normalized)
                    self.assertFalse(connection.in_transaction)

                    observer = real_connect(path, isolation_level=None, timeout=0.1)
                    try:
                        failed_rows = observer.execute(
                            "SELECT COUNT(*) FROM projection_offsets WHERE projection_name = ?",
                            (f"failed-{label}",),
                        ).fetchone()[0]
                        observer.execute("BEGIN IMMEDIATE")
                        observer.execute("ROLLBACK")
                    finally:
                        observer.close()
                    self.assertEqual(failed_rows, 0)

                    retried = store.claim(f"failed-{label}", "worker-b")
                    self.assertEqual(retried.owner_epoch, 1)
                    store.release(retried)
                    self.assertFalse(connection.in_transaction)
                finally:
                    store.close()

                reopened = SQLiteProjectionOffsetStore(path, clock=self.clock)
                try:
                    reopened_lease = reopened.claim(f"reopened-{label}", "worker-c")
                    reopened.release(reopened_lease)
                finally:
                    reopened.close()

    def test_rollback_failures_close_connection_release_lock_and_preserve_primary(self) -> None:
        class RaiseDuringRollbackConnection(sqlite3.Connection):
            primary_failure: Optional[BaseException] = None
            rollback_failure: Optional[BaseException] = None
            rollback_after_success = False
            close_calls = 0

            def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
                normalized = " ".join(sql.split()).upper()
                rollback_failure = self.rollback_failure
                if normalized == "ROLLBACK" and rollback_failure is not None:
                    self.rollback_failure = None
                    if not self.rollback_after_success:
                        raise rollback_failure

                cursor = super().execute(sql, parameters)
                primary_failure = self.primary_failure
                if normalized.startswith("INSERT INTO PROJECTION_OFFSETS") and primary_failure:
                    self.primary_failure = None
                    cursor.close()
                    raise primary_failure
                if normalized == "ROLLBACK" and rollback_failure is not None:
                    cursor.close()
                    raise rollback_failure
                return cursor

            def close(self) -> None:
                self.close_calls += 1
                super().close()

        real_connect = sqlite3.connect
        cases: tuple[tuple[str, BaseException, BaseException, bool], ...] = (
            (
                "before-success",
                RuntimeError("primary transaction failure"),
                KeyboardInterrupt("rollback failed before execution"),
                False,
            ),
            (
                "after-success",
                KeyboardInterrupt("primary transaction interrupt"),
                RuntimeError("rollback failed after execution"),
                True,
            ),
        )
        for label, primary_failure, rollback_failure, after_success in cases:
            with self.subTest(label=label):
                path = str(Path(self.tempdir.name) / f"rollback-failure-{label}.sqlite3")
                opened: list[RaiseDuringRollbackConnection] = []

                def tracked_connect(
                    *args: Any,
                    _opened: list[RaiseDuringRollbackConnection] = opened,
                    **kwargs: Any,
                ) -> sqlite3.Connection:
                    kwargs["factory"] = RaiseDuringRollbackConnection
                    connection = cast(
                        RaiseDuringRollbackConnection,
                        real_connect(*args, **kwargs),
                    )
                    _opened.append(connection)
                    return connection

                with patch(
                    "quantum_entanglement.projections.sqlite3.connect",
                    side_effect=tracked_connect,
                ):
                    store = SQLiteProjectionOffsetStore(path, clock=self.clock)
                try:
                    self.assertEqual(len(opened), 1)
                    connection = opened[0]
                    connection.primary_failure = primary_failure
                    connection.rollback_failure = rollback_failure
                    connection.rollback_after_success = after_success
                    traces: list[str] = []
                    connection.set_trace_callback(traces.append)

                    try:
                        store.claim(f"rollback-failure-{label}", "worker-a")
                    except BaseException as raised:
                        self.assertIs(raised, primary_failure)
                    else:  # pragma: no cover - assertion reports missing fault injection
                        self.fail("transaction fault was not propagated")

                    self.assertEqual(connection.close_calls, 1)
                    normalized = tuple(statement.lstrip().upper() for statement in traces)
                    self.assertIn("BEGIN IMMEDIATE", normalized)
                    self.assertTrue(
                        any(
                            statement.startswith("INSERT INTO PROJECTION_OFFSETS")
                            for statement in normalized
                        )
                    )
                    if after_success:
                        self.assertIn("ROLLBACK", normalized)
                    else:
                        self.assertNotIn("ROLLBACK", normalized)
                    with self.assertRaises(sqlite3.ProgrammingError):
                        connection.execute("SELECT 1")

                    observer = real_connect(path, isolation_level=None, timeout=0.1)
                    try:
                        failed_rows = observer.execute(
                            "SELECT COUNT(*) FROM projection_offsets WHERE projection_name = ?",
                            (f"rollback-failure-{label}",),
                        ).fetchone()[0]
                        observer.execute("BEGIN IMMEDIATE")
                        observer.execute("ROLLBACK")
                    finally:
                        observer.close()
                    self.assertEqual(failed_rows, 0)
                finally:
                    store.close()

                reopened = SQLiteProjectionOffsetStore(path, clock=self.clock)
                try:
                    lease = reopened.claim(f"rollback-failure-{label}", "worker-b")
                    reopened.release(lease)
                finally:
                    reopened.close()


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

    @staticmethod
    def source_event(position: Any, label: str) -> StoredEvent:
        return StoredEvent(
            DomainEvent(
                "session:source",
                "task.created",
                {"name": label},
                "source-test",
                event_id=f"evt-source-{label}",
            ),
            sequence=1,
            global_position=cast(int, position),
        )

    def assert_source_batch_rejected(
        self,
        label: str,
        source_result: object,
        *,
        limit: int = 10,
        starting_position: int = 0,
    ) -> None:
        projection_name = f"bad-source-{label}"
        if starting_position > 0:
            seed_lease = self.offsets.claim(projection_name, "seed-owner")
            self.offsets.advance(
                seed_lease,
                expected_position=0,
                new_position=starting_position,
            )
            self.offsets.release(seed_lease)

        source = StaticEventSource(source_result)
        handler_calls = 0

        def handler(_transaction: ProjectionTransaction, _event: UpcastedEvent) -> None:
            nonlocal handler_calls
            handler_calls += 1

        projector = DurableProjector(
            projection_name,
            "worker-a",
            source,
            self.offsets,
            self.registry,
            handler,
        )
        with patch.object(
            self.offsets,
            "renew",
            wraps=self.offsets.renew,
        ) as renew:
            with patch.object(
                self.registry,
                "upcast",
                wraps=self.registry.upcast,
            ) as upcast:
                with patch.object(
                    self.offsets,
                    "apply_event",
                    wraps=self.offsets.apply_event,
                ) as apply_event:
                    with self.assertRaises(ProjectionSourceIntegrityError) as raised:
                        projector.run_once(limit=limit)

        self.assertIs(type(raised.exception), ProjectionSourceIntegrityError)
        self.assertEqual(source.calls, [(starting_position, limit)])
        renew.assert_not_called()
        upcast.assert_not_called()
        apply_event.assert_not_called()
        self.assertEqual(handler_calls, 0)
        self.assertEqual(
            self.offsets.load(projection_name).last_global_position,
            starting_position,
        )
        connection = sqlite3.connect(self.path)
        try:
            receipt_count = connection.execute(
                "SELECT COUNT(*) FROM projection_receipts WHERE projection_name = ?",
                (projection_name,),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(receipt_count, 0)

        takeover = self.offsets.claim(projection_name, "worker-b")
        self.assertEqual(takeover.owner_id, "worker-b")
        self.offsets.release(takeover)

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

    def test_projector_constructor_rejects_invalid_lease_before_dependencies(self) -> None:
        source = StaticEventSource(())
        statements: list[str] = []
        self.offsets._connection.set_trace_callback(statements.append)
        try:
            for invalid in invalid_projection_lease_seconds():
                with self.subTest(lease_seconds=invalid):
                    with patch.object(self.offsets, "claim") as claim:
                        with patch.object(self.registry, "require_sealed") as require_sealed:
                            with self.assertRaisesRegex(
                                ValueError,
                                "exact finite int or float",
                            ):
                                DurableProjector(
                                    "invalid-constructor-duration",
                                    "worker-a",
                                    source,
                                    self.offsets,
                                    self.registry,
                                    lambda _transaction, _event: None,
                                    lease_seconds=cast(Any, invalid),
                                )
                        require_sealed.assert_not_called()
                    claim.assert_not_called()
        finally:
            self.offsets._connection.set_trace_callback(None)

        self.assertEqual(statements, [])
        self.assertEqual(source.calls, [])

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

    def test_limit_is_an_exact_bounded_integer_before_claim_source_or_sql(self) -> None:
        source = StaticEventSource(())
        projector = DurableProjector(
            "invalid-limit",
            "worker-a",
            source,
            self.offsets,
            self.registry,
            lambda _transaction, _event: None,
        )
        statements: list[str] = []
        self.offsets._connection.set_trace_callback(statements.append)
        try:
            invalid_limits: tuple[object, ...] = (
                True,
                False,
                0,
                -1,
                1.0,
                "1",
                None,
                MAX_PROJECTION_BATCH_SIZE + 1,
                2**63,
            )
            for invalid_limit in invalid_limits:
                with self.subTest(limit=invalid_limit):
                    with patch.object(self.offsets, "claim") as claim:
                        with self.assertRaisesRegex(
                            ValueError,
                            "exact integer between 1 and 1000",
                        ):
                            projector.run_once(limit=cast(Any, invalid_limit))
                    claim.assert_not_called()
        finally:
            self.offsets._connection.set_trace_callback(None)

        self.assertEqual(source.calls, [])
        self.assertEqual(statements, [])

    def test_untrusted_source_batches_are_fully_rejected_before_processing(self) -> None:
        first = self.source_event(1, "first")
        second = self.source_event(2, "second")
        third = self.source_event(3, "third")
        duplicate = self.source_event(1, "duplicate")
        descending = self.source_event(1, "descending")
        bool_position = self.source_event(True, "bool-position")
        float_position = self.source_event(2.0, "float-position")
        zero_position = self.source_event(0, "zero-position")
        negative_position = self.source_event(-1, "negative-position")
        overflow_position = self.source_event(2**63, "overflow-position")
        generator = (event for event in (first,))
        cases: tuple[tuple[str, object, int, int], ...] = (
            ("oversized", (first, second), 1, 0),
            ("list", [first], 10, 0),
            ("generator", generator, 10, 0),
            ("late-invalid-item", (first, object()), 10, 0),
            ("first-gap", (second,), 10, 0),
            ("late-gap", (first, third), 10, 0),
            ("duplicate", (first, duplicate), 10, 0),
            ("descending", (first, second, descending), 10, 0),
            ("bool-position", (bool_position,), 10, 0),
            ("late-float-position", (first, float_position), 10, 0),
            ("zero-position", (zero_position,), 10, 0),
            ("negative-position", (negative_position,), 10, 0),
            ("overflow-position", (overflow_position,), 10, 0),
            ("cross-batch-nonadvancing", (first,), 10, 1),
        )
        for label, source_result, limit, starting_position in cases:
            with self.subTest(label=label):
                self.assert_source_batch_rejected(
                    label,
                    source_result,
                    limit=limit,
                    starting_position=starting_position,
                )

        self.assertIs(next(generator), first)

    def test_source_integrity_error_is_not_masked_by_release_failure(self) -> None:
        source = StaticEventSource([self.source_event(1, "list-release-failure")])
        projector = DurableProjector(
            "source-and-release-failure",
            "worker-a",
            source,
            self.offsets,
            self.registry,
            lambda _transaction, _event: None,
        )
        with patch.object(
            self.offsets,
            "release",
            side_effect=RuntimeError("release failed"),
        ) as release:
            with self.assertRaises(ProjectionSourceIntegrityError) as raised:
                projector.run_once(limit=1)

        self.assertIs(type(raised.exception), ProjectionSourceIntegrityError)
        release.assert_called_once()
        replacement = self.offsets.claim("source-and-release-failure", "worker-a")
        self.offsets.release(replacement)

    def test_exact_empty_and_full_source_batches_remain_compatible(self) -> None:
        source = StaticEventSource(
            (
                self.source_event(1, "valid-first"),
                self.source_event(2, "valid-second"),
            )
        )
        handler_calls = 0

        def handler(_transaction: ProjectionTransaction, _event: UpcastedEvent) -> None:
            nonlocal handler_calls
            handler_calls += 1

        projector = DurableProjector(
            "valid-source-boundaries",
            "worker-a",
            source,
            self.offsets,
            self.registry,
            handler,
            lease_seconds=30,
        )
        self.assertIs(type(projector.lease_seconds), float)
        self.assertEqual(projector.lease_seconds, 30.0)
        full = projector.run_once(limit=2)
        source.result = ()
        empty = projector.run_once(limit=MAX_PROJECTION_BATCH_SIZE)

        self.assertEqual((full.scanned_count, full.applied_count), (2, 2))
        self.assertEqual(full.last_global_position, 2)
        self.assertEqual((empty.scanned_count, empty.applied_count), (0, 0))
        self.assertEqual(empty.last_global_position, 2)
        self.assertEqual(handler_calls, 2)
        self.assertEqual(
            source.calls,
            [(0, 2), (2, MAX_PROJECTION_BATCH_SIZE)],
        )

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

    def test_handler_transaction_capability_is_revoked_after_success(self) -> None:
        escaped: list[ProjectionTransaction] = []
        escaped_copies: list[ProjectionTransaction] = []
        escaped_execute: list[Callable[..., ProjectionStatementResult]] = []
        escaped_executemany: list[Callable[..., ProjectionStatementResult]] = []
        statement_results: list[ProjectionStatementResult] = []

        def handler(transaction: ProjectionTransaction, _event: UpcastedEvent) -> None:
            escaped.append(transaction)
            escaped_copies.extend((copy(transaction), deepcopy(transaction)))
            escaped_execute.append(transaction.execute)
            escaped_executemany.append(transaction.executemany)
            statement_results.append(transaction.execute("SELECT ? AS answer", (42,)))

        run = DurableProjector(
            "escaped-success",
            "worker-a",
            self.events,
            self.offsets,
            self.registry,
            handler,
        ).run_once(limit=1)

        self.assertEqual(run.applied_count, 1)
        self.assertIs(escaped_copies[0], escaped[0])
        self.assertIs(escaped_copies[1], escaped[0])
        self.assertEqual(statement_results[0].rows, ((42,),))

        statements: list[str] = []
        self.offsets._connection.set_trace_callback(statements.append)
        failures: list[BaseException] = []

        def use_from_background_thread() -> None:
            try:
                escaped[0].execute("DELETE FROM projection_receipts")
            except BaseException as exc:  # pragma: no branch - asserted below
                failures.append(exc)

        try:
            attempts: tuple[tuple[str, Callable[[], object]], ...] = (
                (
                    "capability",
                    lambda: escaped[0].execute(
                        "UPDATE projection_offsets SET last_global_position = 777 "
                        "WHERE projection_name = ?",
                        ("escaped-success",),
                    ),
                ),
                ("bound-execute", lambda: escaped_execute[0]("DROP TABLE projection_receipts")),
                (
                    "bound-executemany",
                    lambda: escaped_executemany[0](
                        "UPDATE projection_offsets SET last_global_position = ? "
                        "WHERE projection_name = ?",
                        ((777, "escaped-success"),),
                    ),
                ),
                ("shallow-copy", lambda: escaped_copies[0].execute("SELECT 1")),
                ("deep-copy", lambda: escaped_copies[1].execute("SELECT 1")),
            )
            for label, attempt in attempts:
                with self.subTest(label=label):
                    with self.assertRaisesRegex(
                        ProjectionTransactionClosedError,
                        "no longer active",
                    ):
                        attempt()

            thread = threading.Thread(target=use_from_background_thread)
            thread.start()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        finally:
            self.offsets._connection.set_trace_callback(None)

        self.assertEqual(len(failures), 1)
        self.assertIs(type(failures[0]), ProjectionTransactionClosedError)
        self.assertEqual(statements, [])
        self.assertEqual(self.offsets.load("escaped-success").last_global_position, 1)
        connection = sqlite3.connect(self.path)
        try:
            receipt_table = connection.execute(
                "SELECT type FROM sqlite_master WHERE name = 'projection_receipts'"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(receipt_table, ("table",))

    def test_handler_transaction_capability_is_revoked_after_base_exception(self) -> None:
        escaped: list[ProjectionTransaction] = []
        escaped_execute: list[Callable[..., ProjectionStatementResult]] = []

        def interrupted_handler(
            transaction: ProjectionTransaction,
            _event: UpcastedEvent,
        ) -> None:
            escaped.append(transaction)
            escaped_execute.append(transaction.execute)
            transaction.execute("CREATE TABLE escaped_failure_view (value INTEGER)")
            raise KeyboardInterrupt("simulated capability interruption")

        projector = DurableProjector(
            "escaped-failure",
            "worker-a",
            self.events,
            self.offsets,
            self.registry,
            interrupted_handler,
        )
        with self.assertRaisesRegex(KeyboardInterrupt, "capability interruption"):
            projector.run_once(limit=1)

        statements: list[str] = []
        self.offsets._connection.set_trace_callback(statements.append)
        try:
            with self.assertRaises(ProjectionTransactionClosedError):
                escaped[0].execute("UPDATE projection_offsets SET last_global_position = 777")
            with self.assertRaises(ProjectionTransactionClosedError):
                escaped_execute[0]("DROP TABLE projection_receipts")
        finally:
            self.offsets._connection.set_trace_callback(None)

        self.assertEqual(statements, [])
        self.assertEqual(self.offsets.load("escaped-failure").last_global_position, 0)
        connection = sqlite3.connect(self.path)
        try:
            escaped_table = connection.execute(
                "SELECT type FROM sqlite_master WHERE name = 'escaped_failure_view'"
            ).fetchone()
            receipt_count = connection.execute(
                "SELECT COUNT(*) FROM projection_receipts WHERE projection_name = ?",
                ("escaped-failure",),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertIsNone(escaped_table)
        self.assertEqual(receipt_count, 0)

    def test_transaction_capability_construction_failure_restores_restrictions(self) -> None:
        lease = self.offsets.claim("capability-construction", "worker-a")
        event = self.registry.upcast(self.events.read_all(limit=1)[0])
        handler_calls = 0

        def handler(_transaction: ProjectionTransaction, _event: UpcastedEvent) -> None:
            nonlocal handler_calls
            handler_calls += 1

        with patch(
            "quantum_entanglement.projections.ProjectionTransaction",
            side_effect=KeyboardInterrupt("simulated capability construction interruption"),
        ):
            with self.assertRaisesRegex(KeyboardInterrupt, "construction interruption"):
                self.offsets.apply_event(
                    lease,
                    expected_position=0,
                    event=event,
                    handler=handler,
                )

        self.assertEqual(handler_calls, 0)
        self.assertFalse(self.offsets._connection.in_transaction)
        self.assertEqual(self.offsets.load("capability-construction").last_global_position, 0)
        contender = SQLiteProjectionOffsetStore(self.path, clock=self.clock)
        try:
            self.assertEqual(contender.load("capability-construction").last_global_position, 0)
        finally:
            contender.close()

    def test_authorizer_install_then_raise_still_restores_framework_access(self) -> None:
        connection = self.offsets._connection

        class InstallThenRaiseConnection:
            def __init__(self) -> None:
                self.authorizer_calls = 0

            def set_authorizer(self, callback: object) -> None:
                self.authorizer_calls += 1
                connection.set_authorizer(callback)  # type: ignore[arg-type]
                if self.authorizer_calls == 1:
                    raise KeyboardInterrupt("simulated post-install interruption")

            def __getattr__(self, name: str) -> object:
                return getattr(connection, name)

        wrapped = InstallThenRaiseConnection()
        with self.assertRaisesRegex(KeyboardInterrupt, "post-install interruption"):
            with self.offsets._handler_transaction(wrapped):  # type: ignore[arg-type]
                self.fail("authorizer installation failure must not yield a capability")

        self.assertEqual(wrapped.authorizer_calls, 2)
        row = connection.execute(
            "SELECT COUNT(*) FROM projection_offsets /* authorizer restored */"
        ).fetchone()
        self.assertIsNotNone(row)

    def test_revoke_interruption_force_closes_capability_before_authorizer_restore(self) -> None:
        lease = self.offsets.claim("revoke-interruption", "worker-a")
        event = self.registry.upcast(self.events.read_all(limit=1)[0])
        escaped: list[ProjectionTransaction] = []

        def handler(transaction: ProjectionTransaction, _event: UpcastedEvent) -> None:
            escaped.append(transaction)
            transaction.execute("CREATE TABLE revoke_interrupted_view (value INTEGER)")

        with patch.object(
            ProjectionTransaction,
            "_revoke",
            side_effect=KeyboardInterrupt("simulated revoke interruption"),
        ):
            with self.assertRaisesRegex(KeyboardInterrupt, "revoke interruption"):
                self.offsets.apply_event(
                    lease,
                    expected_position=0,
                    event=event,
                    handler=handler,
                )

        statements: list[str] = []
        self.offsets._connection.set_trace_callback(statements.append)
        try:
            with self.assertRaises(ProjectionTransactionClosedError):
                escaped[0].execute("DROP TABLE projection_receipts")
        finally:
            self.offsets._connection.set_trace_callback(None)

        self.assertEqual(statements, [])
        self.assertFalse(self.offsets._connection.in_transaction)
        self.assertEqual(self.offsets.load("revoke-interruption").last_global_position, 0)
        connection = sqlite3.connect(self.path)
        try:
            interrupted_table = connection.execute(
                "SELECT type FROM sqlite_master WHERE name = 'revoke_interrupted_view'"
            ).fetchone()
            receipt_table = connection.execute(
                "SELECT type FROM sqlite_master WHERE name = 'projection_receipts'"
            ).fetchone()
        finally:
            connection.close()
        self.assertIsNone(interrupted_table)
        self.assertEqual(receipt_table, ("table",))

    def test_active_handler_transaction_rejects_cross_thread_use_before_sql(self) -> None:
        escaped: list[ProjectionTransaction] = []
        background_failures: list[BaseException] = []
        background_statements: list[str] = []
        owner_results: list[ProjectionStatementResult] = []

        def handler(transaction: ProjectionTransaction, _event: UpcastedEvent) -> None:
            escaped.append(transaction)

            def use_from_background_thread() -> None:
                attempts: tuple[Callable[[], object], ...] = (
                    lambda: transaction.execute("SELECT 1 AS escaped_background_sql"),
                    lambda: transaction.executemany(
                        "INSERT INTO task_view (event_id, title) VALUES (?, ?)",
                        (("escaped", "background"),),
                    ),
                )
                for attempt in attempts:
                    try:
                        attempt()
                    except BaseException as exc:  # pragma: no cover - assertion reports details
                        background_failures.append(exc)

            self.offsets._connection.set_trace_callback(background_statements.append)
            try:
                thread = threading.Thread(target=use_from_background_thread)
                thread.start()
                thread.join(timeout=5)
                if thread.is_alive():
                    raise RuntimeError("cross-thread capability call did not terminate")
            finally:
                self.offsets._connection.set_trace_callback(None)
            owner_results.append(transaction.execute("SELECT 42 AS owner_sql"))

        run = DurableProjector(
            "thread-affine-capability",
            "worker-a",
            self.events,
            self.offsets,
            self.registry,
            handler,
        ).run_once(limit=1)

        self.assertEqual(run.applied_count, 1)
        self.assertEqual(len(background_failures), 2)
        self.assertTrue(
            all(
                type(failure) is ProjectionTransactionThreadError for failure in background_failures
            )
        )
        self.assertEqual(background_statements, [])
        self.assertEqual(owner_results[0].rows, ((42,),))
        with self.assertRaises(ProjectionTransactionClosedError):
            escaped[0].execute("SELECT 1")

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

    def test_handler_cannot_persist_deferred_schema_programs(self) -> None:
        deferred_actions = (
            sqlite3.SQLITE_CREATE_TEMP_TRIGGER,
            sqlite3.SQLITE_CREATE_TEMP_VIEW,
            sqlite3.SQLITE_CREATE_TRIGGER,
            sqlite3.SQLITE_CREATE_VIEW,
            sqlite3.SQLITE_CREATE_VTABLE,
            sqlite3.SQLITE_DROP_TEMP_TRIGGER,
            sqlite3.SQLITE_DROP_TEMP_VIEW,
            sqlite3.SQLITE_DROP_TRIGGER,
            sqlite3.SQLITE_DROP_VIEW,
            sqlite3.SQLITE_DROP_VTABLE,
        )
        for action in deferred_actions:
            with self.subTest(action=action):
                self.assertEqual(
                    self.offsets._projection_handler_authorizer(
                        action,
                        "handler_object",
                        "handler_target",
                        "main",
                        None,
                    ),
                    sqlite3.SQLITE_DENY,
                )

        statements = (
            "CREATE VIEW handler_deferred_view AS SELECT 1 AS value",
            "CREATE TEMP VIEW handler_deferred_temp_view AS SELECT 1 AS value",
            "CREATE TRIGGER handler_deferred_trigger AFTER INSERT ON handler_schema_target "
            "BEGIN UPDATE handler_schema_target SET value = NEW.value; END",
            "CREATE TEMP TRIGGER handler_deferred_temp_trigger "
            "AFTER INSERT ON handler_schema_target "
            "BEGIN UPDATE handler_schema_target SET value = NEW.value; END",
            "CREATE VIRTUAL TABLE handler_deferred_virtual USING fts5(value)",
        )

        def handler(transaction: ProjectionTransaction, _event: UpcastedEvent) -> None:
            transaction.execute(
                "CREATE TABLE IF NOT EXISTS handler_schema_target (value INTEGER NOT NULL)"
            )
            for statement in statements:
                with self.assertRaises(sqlite3.DatabaseError):
                    transaction.execute(statement)

        result = DurableProjector(
            "deferred-schema-boundary",
            "worker-a",
            self.events,
            self.offsets,
            self.registry,
            handler,
        ).run_once(limit=1)

        self.assertEqual(result.applied_count, 1)
        names = {
            row[0]
            for row in self.offsets._connection.execute(
                "SELECT name FROM main.sqlite_master UNION ALL SELECT name FROM temp.sqlite_master"
            ).fetchall()
        }
        self.assertFalse(
            names
            & {
                "handler_deferred_view",
                "handler_deferred_temp_view",
                "handler_deferred_trigger",
                "handler_deferred_temp_trigger",
                "handler_deferred_virtual",
            }
        )

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

    def test_load_and_event_source_base_exceptions_release_the_claimed_lease(self) -> None:
        load_projector = DurableProjector(
            "load-interrupt",
            "worker-a",
            self.events,
            self.offsets,
            self.registry,
            lambda _transaction, _event: None,
        )
        with patch.object(
            self.offsets,
            "load",
            side_effect=KeyboardInterrupt("load interrupted"),
        ):
            with self.assertRaisesRegex(KeyboardInterrupt, "load interrupted"):
                load_projector.run_once(limit=1)
        load_takeover = self.offsets.claim("load-interrupt", "worker-b")
        self.assertEqual(load_takeover.owner_epoch, 2)
        self.offsets.release(load_takeover)

        source_projector = DurableProjector(
            "source-interrupt",
            "worker-a",
            self.events,
            self.offsets,
            self.registry,
            lambda _transaction, _event: None,
        )
        with patch.object(
            self.events,
            "read_all",
            side_effect=KeyboardInterrupt("event source interrupted"),
        ):
            with self.assertRaisesRegex(KeyboardInterrupt, "event source interrupted"):
                source_projector.run_once(limit=1)
        source_takeover = self.offsets.claim("source-interrupt", "worker-b")
        self.assertEqual(source_takeover.owner_epoch, 2)
        self.offsets.release(source_takeover)

    def test_release_failure_never_masks_a_primary_base_exception(self) -> None:
        projector = DurableProjector(
            "dual-failure",
            "worker-a",
            self.events,
            self.offsets,
            self.registry,
            lambda _transaction, _event: None,
        )
        with patch.object(
            self.offsets,
            "load",
            side_effect=KeyboardInterrupt("primary interruption"),
        ):
            with patch.object(
                self.offsets,
                "release",
                side_effect=RuntimeError("cleanup failed"),
            ):
                with self.assertRaisesRegex(KeyboardInterrupt, "primary interruption") as raised:
                    projector.run_once(limit=1)

        self.assertIsInstance(raised.exception, KeyboardInterrupt)
        replacement = self.offsets.claim("dual-failure", "worker-a")
        self.offsets.release(replacement)

    def test_release_failure_without_primary_error_propagates_except_lease_loss(self) -> None:
        failed_release = DurableProjector(
            "release-failure",
            "worker-a",
            self.events,
            self.offsets,
            self.registry,
            lambda _transaction, _event: None,
        )
        with patch.object(
            self.offsets,
            "release",
            side_effect=RuntimeError("release storage failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "release storage failed"):
                failed_release.run_once(limit=1)
        replacement = self.offsets.claim("release-failure", "worker-a")
        self.offsets.release(replacement)

        lost_release = DurableProjector(
            "release-lost",
            "worker-a",
            self.events,
            self.offsets,
            self.registry,
            lambda _transaction, _event: None,
        )
        with patch.object(
            self.offsets,
            "release",
            side_effect=ProjectionLeaseLostError("lease already lost"),
        ):
            result = lost_release.run_once(limit=1)
        self.assertEqual(result.applied_count, 1)
        lost_replacement = self.offsets.claim("release-lost", "worker-a")
        self.offsets.release(lost_replacement)

    def test_successful_run_releases_lease_for_a_different_owner(self) -> None:
        result = DurableProjector(
            "normal-release",
            "worker-a",
            self.events,
            self.offsets,
            self.registry,
            lambda _transaction, _event: None,
        ).run_once(limit=1)

        self.assertEqual(result.applied_count, 1)
        takeover = self.offsets.claim("normal-release", "worker-b")
        self.assertEqual(takeover.owner_epoch, 2)
        self.offsets.release(takeover)

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
        takeover = self.offsets.claim("future-view", "worker-b")
        self.assertEqual(takeover.owner_id, "worker-b")
        self.offsets.release(takeover)


if __name__ == "__main__":
    unittest.main()
