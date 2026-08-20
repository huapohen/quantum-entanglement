import asyncio
import io
import json
import logging
import unittest

from quantum_entanglement.service import (
    LogEventSchema,
    LogField,
    LogFieldKind,
    SafeLogCatalog,
    SafeLogger,
)


class ExplodingLogger(logging.Logger):
    def handle(self, record: logging.LogRecord) -> None:
        raise RuntimeError("logging backend secret canary")


class CancellingLogger(logging.Logger):
    def handle(self, record: logging.LogRecord) -> None:
        raise asyncio.CancelledError("logging cancellation secret canary")


class SafeLoggerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stream = io.StringIO()
        self.logger = logging.Logger("safe-logging-test")
        handler = logging.StreamHandler(self.stream)
        handler.setFormatter(logging.Formatter("%(message)s"))
        self.logger.addHandler(handler)
        self.catalog = SafeLogCatalog(
            (
                LogEventSchema(
                    "qe.test.operation_completed",
                    logging.INFO,
                    (
                        LogField("ready", LogFieldKind.BOOLEAN),
                        LogField("count", LogFieldKind.COUNT),
                        LogField("duration_ms", LogFieldKind.DURATION_MS),
                        LogField(
                            "outcome",
                            LogFieldKind.CODE,
                            allowed_codes=("completed", "idle"),
                        ),
                        LogField("worker_id", LogFieldKind.IDENTIFIER_HASH),
                        LogField("state_digest", LogFieldKind.DIGEST, required=False),
                    ),
                ),
                LogEventSchema("qe.test.no_fields", logging.WARNING),
            )
        )
        self.safe_logger = SafeLogger(self.logger, self.catalog)

    def records(self) -> list[dict[str, object]]:
        return [json.loads(line) for line in self.stream.getvalue().splitlines()]

    def test_emits_canonical_typed_event_and_hashes_identifier(self) -> None:
        worker = "tenant-sensitive-worker-id"

        emitted = self.safe_logger.emit(
            "qe.test.operation_completed",
            {
                "ready": True,
                "count": 3,
                "duration_ms": 12.34567,
                "outcome": "completed",
                "worker_id": worker,
                "state_digest": "a" * 64,
            },
        )

        self.assertTrue(emitted)
        record = self.records()[0]
        self.assertEqual(record["event"], "qe.test.operation_completed")
        fields = record["fields"]
        self.assertEqual(fields["duration_ms"], 12.346)
        self.assertEqual(fields["count"], 3)
        self.assertTrue(fields["worker_id"].startswith("sha256:"))
        self.assertNotIn(worker, self.stream.getvalue())

    def test_rejects_unknown_event_without_rendering_event_canary(self) -> None:
        canary = "qe.test.secret_canary_event"

        emitted = self.safe_logger.emit(canary, {"payload": "do-not-log"})

        self.assertFalse(emitted)
        self.assertEqual(
            self.records(),
            [{"event": "qe.logging.event_rejected", "fields": {}}],
        )
        self.assertNotIn(canary, self.stream.getvalue())
        self.assertNotIn("do-not-log", self.stream.getvalue())

    def test_rejects_missing_unknown_and_wrongly_typed_fields(self) -> None:
        valid = {
            "ready": True,
            "count": 1,
            "duration_ms": 1.0,
            "outcome": "completed",
            "worker_id": "worker",
        }
        invalid = (
            {key: value for key, value in valid.items() if key != "count"},
            {**valid, "payload": "payload-secret-canary"},
            {**valid, "ready": 1},
            {**valid, "count": True},
            {**valid, "duration_ms": float("nan")},
            {**valid, "outcome": "bad\nlog"},
            {**valid, "outcome": "secretcanary"},
            {**valid, "worker_id": " secret"},
            {**valid, "state_digest": "not-a-digest"},
        )

        for fields in invalid:
            with self.subTest(fields=tuple(fields)):
                self.assertFalse(self.safe_logger.emit("qe.test.operation_completed", fields))

        rendered = self.stream.getvalue()
        self.assertNotIn("payload-secret-canary", rendered)
        self.assertEqual(rendered.count("qe.logging.event_rejected"), len(invalid))

    def test_rejects_field_containers_before_an_unbounded_snapshot(self) -> None:
        fields = {f"untrusted_{index}": "oversized-fields-secret-canary" for index in range(33)}

        self.assertFalse(self.safe_logger.emit("qe.test.no_fields", fields))

        self.assertEqual(
            self.records(),
            [{"event": "qe.logging.event_rejected", "fields": {}}],
        )
        self.assertNotIn("oversized-fields-secret-canary", self.stream.getvalue())

    def test_rejects_exception_and_custom_mapping_without_stringifying(self) -> None:
        class ExplosiveValue:
            def __str__(self) -> str:
                raise AssertionError("must not stringify")

            def __repr__(self) -> str:
                raise AssertionError("must not render")

        fields = {
            "ready": True,
            "count": 1,
            "duration_ms": 1,
            "outcome": ExplosiveValue(),
            "worker_id": "worker",
        }

        self.assertFalse(self.safe_logger.emit("qe.test.operation_completed", fields))
        self.assertFalse(self.safe_logger.emit("qe.test.no_fields", {1: "value"}))
        self.assertEqual(len(self.records()), 2)

    def test_optional_field_may_be_omitted_and_no_field_event_is_exact(self) -> None:
        self.assertTrue(
            self.safe_logger.emit(
                "qe.test.operation_completed",
                {
                    "ready": False,
                    "count": 0,
                    "duration_ms": 0,
                    "outcome": "idle",
                    "worker_id": "worker",
                },
            )
        )
        self.assertTrue(self.safe_logger.emit("qe.test.no_fields"))

        self.assertNotIn("state_digest", self.records()[0]["fields"])
        self.assertEqual(self.records()[1]["fields"], {})

    def test_logging_backend_failure_never_escapes(self) -> None:
        safe_logger = SafeLogger(ExplodingLogger("exploding"), self.catalog)

        self.assertFalse(safe_logger.emit("qe.test.no_fields"))
        self.assertFalse(safe_logger.emit("unknown"))

    def test_logging_backend_cancellation_never_changes_business_flow(self) -> None:
        safe_logger = SafeLogger(CancellingLogger("cancelling"), self.catalog)

        self.assertFalse(safe_logger.emit("qe.test.no_fields"))
        self.assertFalse(safe_logger.emit("unknown"))

    def test_schema_and_catalog_validation_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            LogEventSchema("dynamic event", logging.INFO)
        with self.assertRaises(ValueError):
            LogEventSchema(
                "qe.test.event",
                logging.INFO,
                (
                    LogField(
                        "field",
                        LogFieldKind.CODE,
                        allowed_codes=("allowed",),
                    ),
                )
                * 2,
            )
        schema = LogEventSchema("qe.test.event", logging.INFO)
        with self.assertRaises(ValueError):
            SafeLogCatalog((schema, schema))
        with self.assertRaises(TypeError):
            LogField("value", "code")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            LogField("value", LogFieldKind.CODE)

    def test_catalog_snapshots_schemas_and_never_returns_internal_objects(self) -> None:
        field = LogField(
            "outcome",
            LogFieldKind.CODE,
            allowed_codes=("completed",),
        )
        schema = LogEventSchema("qe.test.snapshotted", logging.INFO, (field,))
        catalog = SafeLogCatalog((schema,))
        safe_logger = SafeLogger(self.logger, catalog)

        object.__setattr__(field, "allowed_codes", ("secret-canary",))
        object.__setattr__(schema, "event_code", "secret-canary\nforged")
        returned = catalog.get("qe.test.snapshotted")
        assert returned is not None
        object.__setattr__(returned.fields[0], "allowed_codes", ("secret-canary",))

        self.assertFalse(safe_logger.emit("qe.test.snapshotted", {"outcome": "secret-canary"}))
        self.assertTrue(safe_logger.emit("qe.test.snapshotted", {"outcome": "completed"}))
        rendered = self.stream.getvalue()
        self.assertNotIn("secret-canary", rendered)
        self.assertIn('"outcome":"completed"', rendered)


if __name__ == "__main__":
    unittest.main()
