import copy
import hashlib
import tempfile
import unittest
from collections import UserDict
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import quantum_entanglement
import quantum_entanglement.artifacts as artifacts_module
from quantum_entanglement.artifacts import (
    ArtifactLedger,
    ArtifactRecordError,
    ArtifactReplayError,
)
from quantum_entanglement.events import DomainEvent, StoredEvent
from quantum_entanglement.protocol import ArtifactOutput, ArtifactRef
from quantum_entanglement.store import ConcurrencyError, SQLiteEventStore

T0 = "2026-08-20T00:00:00Z"


def replay_event(position, *, name=None, version=1, content=""):
    if name is None:
        event_type = "test.non-artifact"
        payload = {"position": position}
    else:
        event_type = ArtifactLedger.EVENT_TYPE
        ref = ArtifactRef(
            artifact_id=f"artifact-{position}",
            name=name,
            version=version,
            media_type="text/markdown",
            uri=f"artifact://session-replay/{name}/v{version}",
            digest="sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest(),
            created_by="test",
            task_id=f"task-{position}",
            parent_version=version - 1 if version > 1 else None,
        )
        payload = {
            "sessionId": "session-replay",
            "taskId": ref.task_id,
            "ref": ref.to_dict(),
            "content": content,
            "metadata": {"position": position},
            "createdAt": T0,
            "trigger": "create" if version == 1 else "revise",
        }
    return StoredEvent(
        DomainEvent(
            stream_id="session:session-replay",
            event_type=event_type,
            actor_id="test",
            event_id=f"event-{position}",
            timestamp=T0,
            payload=payload,
        ),
        sequence=max(1, position),
        global_position=position,
    )


def replay_event_with_payload(position, payload):
    return StoredEvent(
        DomainEvent(
            stream_id="session:session-replay",
            event_type=ArtifactLedger.EVENT_TYPE,
            actor_id="test",
            event_id=f"event-payload-{position}",
            timestamp=T0,
            payload=payload,
        ),
        sequence=max(1, position),
        global_position=position,
    )


class FakeReplayStore(SQLiteEventStore):
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def read_all(self, after_position=0, limit=1000):
        self.calls.append((after_position, limit))
        response = self.pages.get(after_position, ())
        if isinstance(response, BaseException):
            raise response
        return response


class EventStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = SQLiteEventStore(str(Path(self.tempdir.name) / "events.sqlite3"))

    def tearDown(self):
        self.store.close()
        self.tempdir.cleanup()

    def test_append_is_ordered_and_idempotent(self):
        event = DomainEvent(
            stream_id="session:s1",
            event_type="message.received",
            actor_id="u1",
            idempotency_key="incoming-1",
            payload={"text": "hello"},
        )
        first = self.store.append(event, expected_version=0)
        retried = self.store.append(event, expected_version=0)

        self.assertEqual(first.global_position, retried.global_position)
        self.assertEqual(self.store.stream_version("session:s1"), 1)
        self.assertEqual(self.store.read_stream("session:s1")[0].event.payload["text"], "hello")

    def test_optimistic_concurrency_rejects_stale_writer(self):
        self.store.append(DomainEvent("s", "created", {}, "actor"), expected_version=0)
        with self.assertRaises(ConcurrencyError):
            self.store.append(DomainEvent("s", "changed", {}, "actor"), expected_version=0)

    def test_batch_append_is_atomic_and_sequential(self):
        events = [
            DomainEvent("s", "one", {"n": 1}, "actor"),
            DomainEvent("s", "two", {"n": 2}, "actor"),
        ]
        stored = self.store.append_many("s", events, expected_version=0)
        self.assertEqual([item.sequence for item in stored], [1, 2])


class ArtifactLedgerTests(unittest.TestCase):
    def setUp(self):
        self.store = SQLiteEventStore()
        self.ledger = ArtifactLedger(self.store)

    def tearDown(self):
        self.store.close()

    def test_versions_are_append_only_and_restore_creates_new_head(self):
        first = self.ledger.record("s1", "t1", "writer", ArtifactOutput("report.md", "v1"))
        second = self.ledger.record("s1", "t2", "writer", ArtifactOutput("report.md", "v2"))
        restored = self.ledger.restore("s1", "report.md", 1, "t3", "owner")

        self.assertEqual(first.ref.version, 1)
        self.assertEqual(second.ref.version, 2)
        self.assertEqual(second.trigger, "revise")
        self.assertEqual(restored.ref.version, 3)
        self.assertEqual(restored.trigger, "rollback")
        self.assertEqual(restored.content, "v1")
        self.assertEqual(len(self.ledger.history("s1", "report.md")), 3)

    def test_same_task_output_is_idempotent(self):
        output = ArtifactOutput("result.txt", "same")
        first = self.ledger.record("s1", "task-1", "agent", output)
        second = self.ledger.record("s1", "task-1", "agent", output)

        self.assertEqual(first.ref.artifact_id, second.ref.artifact_id)
        self.assertEqual(len(self.ledger.history("s1", "result.txt")), 1)

    def test_rebuild_can_repeat_without_duplicating_versions(self):
        self.ledger.record("s1", "t1", "writer", ArtifactOutput("report.md", "v1"))
        self.ledger.record("s1", "t2", "writer", ArtifactOutput("report.md", "v2"))
        expected = self.ledger.history("s1", "report.md")

        self.ledger._rebuild()
        self.ledger._rebuild()

        self.assertEqual(self.ledger.history("s1", "report.md"), expected)
        self.assertEqual(len(self.ledger.history("s1", "report.md")), 2)

    def test_idempotent_retry_rebuild_keeps_every_history_unique(self):
        concurrent = ArtifactLedger(self.store)
        unrelated = self.ledger.record(
            "s1",
            "task-other",
            "agent",
            ArtifactOutput("other.txt", "other"),
        )
        original = concurrent.record(
            "s1",
            "task-target",
            "agent",
            ArtifactOutput("result.txt", "same"),
        )

        retried = self.ledger.record(
            "s1",
            "task-target",
            "agent",
            ArtifactOutput("result.txt", "same"),
        )

        self.assertEqual(retried.ref.artifact_id, original.ref.artifact_id)
        self.assertEqual(self.ledger.history("s1", "other.txt"), (unrelated,))
        self.assertEqual(self.ledger.history("s1", "result.txt"), (original,))

    def test_valid_record_payload_remains_replay_compatible(self):
        recorded = self.ledger.record(
            "s1",
            "task-valid",
            "agent-valid",
            ArtifactOutput(
                "valid.json",
                '{"ok":true}',
                media_type="application/json",
                metadata={
                    "nested": {"values": [None, True, 7, 1.5, "text"]},
                    "unicode": "协作",
                },
            ),
        )

        rebuilt = ArtifactLedger(self.store)

        self.assertEqual(rebuilt.history("s1", "valid.json"), (recorded,))

    def test_record_error_is_part_of_the_supported_artifact_api(self):
        self.assertIn("ArtifactRecordError", artifacts_module.__all__)
        self.assertIs(quantum_entanglement.ArtifactRecordError, ArtifactRecordError)

    def test_invalid_record_input_is_rejected_before_sql_or_state_changes(self):
        baseline = self.ledger.record(
            "s1",
            "task-baseline",
            "agent",
            ArtifactOutput("stable.json", "stable", metadata={"kept": [1]}),
        )
        original_state = self.ledger._versions
        original_history = self.ledger.history("s1", "stable.json")

        class DictSubclass(dict):
            pass

        def tampered(field, value):
            output = ArtifactOutput("stable.json", "candidate")
            object.__setattr__(output, field, value)
            return output

        cases = (
            ("tuple", ArtifactOutput("stable.json", "candidate", metadata={"items": (1, 2)}), ()),
            (
                "user-dict",
                ArtifactOutput("stable.json", "candidate", metadata=UserDict({"ok": True})),
                (),
            ),
            (
                "dict-subclass",
                ArtifactOutput("stable.json", "candidate", metadata=DictSubclass(ok=True)),
                (),
            ),
            ("nan", ArtifactOutput("stable.json", "candidate", metadata={"n": float("nan")}), ()),
            (
                "positive-infinity",
                ArtifactOutput("stable.json", "candidate", metadata={"n": float("inf")}),
                (),
            ),
            (
                "negative-infinity",
                ArtifactOutput("stable.json", "candidate", metadata={"n": float("-inf")}),
                (),
            ),
            (
                "metadata-depth",
                ArtifactOutput("stable.json", "candidate", metadata={"nested": [[[]]]}),
                (("_MAX_METADATA_DEPTH", 3),),
            ),
            (
                "metadata-nodes",
                ArtifactOutput("stable.json", "candidate", metadata={"items": [1, 2]}),
                (("_MAX_METADATA_NODES", 3),),
            ),
            (
                "metadata-string",
                ArtifactOutput("stable.json", "candidate", metadata={"value": "abcd"}),
                (("_MAX_METADATA_STRING_LENGTH", 3),),
            ),
            (
                "metadata-bytes",
                ArtifactOutput("stable.json", "candidate", metadata={"value": "encoded"}),
                (("_MAX_METADATA_BYTES", 8),),
            ),
            (
                "content-bytes",
                ArtifactOutput("stable.json", "12345"),
                (("_MAX_CONTENT_BYTES", 4),),
            ),
            ("name-type", tampered("name", 7), ()),
            ("content-type", tampered("content", ["candidate"]), ()),
            ("media-type", tampered("media_type", object()), ()),
            ("output-subclass", type("DerivedOutput", (ArtifactOutput,), {})("x", "y"), ()),
        )

        traced = []
        self.store._connection.set_trace_callback(traced.append)
        try:
            for case, output, constant_patches in cases:
                with self.subTest(case=case):
                    traced.clear()
                    with ExitStack() as stack:
                        for constant, maximum in constant_patches:
                            stack.enter_context(patch.object(artifacts_module, constant, maximum))
                        with self.assertRaisesRegex(
                            ArtifactRecordError,
                            "artifact record input violates its contract",
                        ):
                            self.ledger.record("s1", "task-invalid", "agent", output)
                    self.assertEqual(traced, [])
                    self.assertIs(self.ledger._versions, original_state)
                    self.assertIs(
                        self.ledger.history("s1", "stable.json"),
                        original_history,
                    )
                    self.assertEqual(original_history, (baseline,))
        finally:
            self.store._connection.set_trace_callback(None)

    def test_record_snapshots_nested_metadata_for_state_and_event(self):
        metadata = {
            "nested": {"values": [1, {"label": "original"}]},
            "tags": [{"name": "stable"}],
        }
        captured_events = []
        append = self.store.append

        def capture_event(event, expected_version=None):
            captured_events.append(event)
            return append(event, expected_version)

        with patch.object(self.store, "append", side_effect=capture_event):
            recorded = self.ledger.record(
                "s1",
                "task-snapshot",
                "agent",
                ArtifactOutput("snapshot.json", "body", metadata=metadata),
            )

        expected = copy.deepcopy(metadata)
        event_metadata = captured_events[0].payload["metadata"]
        self.assertEqual(recorded.metadata, expected)
        self.assertEqual(event_metadata, expected)
        self.assertIsNot(recorded.metadata, event_metadata)
        self.assertIsNot(recorded.metadata["nested"], event_metadata["nested"])
        self.assertIsNot(
            recorded.metadata["nested"]["values"],
            event_metadata["nested"]["values"],
        )

        metadata["nested"]["values"][1]["label"] = "caller-mutated"
        metadata["tags"].append({"name": "caller-added"})
        self.assertEqual(recorded.metadata, expected)
        self.assertEqual(event_metadata, expected)

        recorded.metadata["nested"]["values"].append("item-mutated")
        recorded.metadata["tags"][0]["name"] = "item-mutated"
        self.assertEqual(event_metadata, expected)
        persisted_metadata = self.store.read_all()[0].event.payload["metadata"]
        self.assertEqual(persisted_metadata, expected)

    def test_record_rejects_invalid_envelope_fields_and_empty_trigger_before_sql(self):
        output = ArtifactOutput("envelope.json", "body")
        invalid_calls = (
            lambda: self.ledger.record(True, "task", "agent", output),
            lambda: self.ledger.record("s1", False, "agent", output),
            lambda: self.ledger.record("s1", "task", 7, output),
            lambda: self.ledger.record("s1", "task", "agent", output, correlation_id=True),
            lambda: self.ledger.record("s1", "task", "agent", output, causation_id=[]),
            lambda: self.ledger.record("s1", "task", "agent", output, trigger=""),
            lambda: self.ledger.record("s1", "task", "agent", output, trigger=False),
        )
        statements = []
        self.store._connection.set_trace_callback(statements.append)
        try:
            for invalid_call in invalid_calls:
                with self.subTest(call=invalid_call):
                    statements.clear()
                    with self.assertRaises(ArtifactRecordError):
                        invalid_call()
                    self.assertEqual(statements, [])
                    self.assertEqual(self.ledger.history("s1", "envelope.json"), ())
        finally:
            self.store._connection.set_trace_callback(None)

        recorded = self.ledger.record(
            "s1",
            "task",
            "agent",
            output,
            correlation_id="correlation",
            causation_id="causation",
            trigger="publish",
        )
        stored = self.store.read_all(limit=1)[0]
        self.assertEqual(stored.event.correlation_id, "correlation")
        self.assertEqual(stored.event.causation_id, "causation")
        self.assertEqual(recorded.trigger, "publish")

    def test_valid_record_state_exactly_matches_a_fresh_replay(self):
        recorded = self.ledger.record(
            "s1",
            "task-roundtrip",
            "agent",
            ArtifactOutput(
                "roundtrip.json",
                '{"status":"ready"}',
                media_type="application/json",
                metadata={
                    "nested": {"values": [None, True, 7, -1.5, "协作"]},
                    "objects": [{"key": "value"}],
                },
            ),
            trigger="publish",
        )

        replayed = ArtifactLedger(self.store).current("s1", "roundtrip.json")

        self.assertEqual(replayed, recorded)


class ArtifactLedgerReplayTests(unittest.TestCase):
    @staticmethod
    def _valid_payload():
        return copy.deepcopy(
            replay_event(1, name="report.md", version=1, content="body").event.payload
        )

    def _assert_payload_rejected(self, payload):
        source = FakeReplayStore({0: (replay_event_with_payload(1, payload),)})
        with self.assertRaisesRegex(
            ArtifactReplayError,
            "persisted artifact.versioned payload violates its contract",
        ):
            ArtifactLedger(source)

    def test_replay_error_is_part_of_the_supported_artifact_api(self):
        self.assertIn("ArtifactReplayError", artifacts_module.__all__)
        self.assertIs(quantum_entanglement.ArtifactReplayError, ArtifactReplayError)

        source = FakeReplayStore({})
        ArtifactLedger(source)
        self.assertEqual(source.calls, [(0, 1_000)])

    def test_replay_accepts_only_the_documented_optional_payload_keys(self):
        payload = self._valid_payload()
        payload.pop("metadata")
        payload.pop("trigger")
        payload["ref"].pop("parentVersion")
        source = FakeReplayStore({0: (replay_event_with_payload(1, payload),)})

        ledger = ArtifactLedger(source)

        item = ledger.current("session-replay", "report.md")
        self.assertEqual(item.metadata, {})
        self.assertEqual(item.trigger, "create")
        self.assertIsNone(item.ref.parent_version)

        shape_cases = {}
        missing_payload = self._valid_payload()
        missing_payload.pop("content")
        shape_cases["missing-payload-key"] = missing_payload
        extra_payload = self._valid_payload()
        extra_payload["unexpected"] = True
        shape_cases["extra-payload-key"] = extra_payload
        missing_ref = self._valid_payload()
        missing_ref["ref"].pop("artifactId")
        shape_cases["missing-ref-key"] = missing_ref
        extra_ref = self._valid_payload()
        extra_ref["ref"]["unexpected"] = True
        shape_cases["extra-ref-key"] = extra_ref
        shape_cases["non-object-payload"] = []
        for case, malformed in shape_cases.items():
            with self.subTest(case=case):
                self._assert_payload_rejected(malformed)

    def test_replay_rejects_scalar_collection_and_object_coercions(self):
        coercions = (
            ("payload", "sessionId", True),
            ("payload", "taskId", 1.5),
            ("payload", "content", ["body"]),
            ("payload", "createdAt", object()),
            ("payload", "trigger", False),
            ("payload", "metadata", [("source", "test")]),
            ("payload", "ref", []),
            ("ref", "artifactId", True),
            ("ref", "name", ["report.md"]),
            ("ref", "version", True),
            ("ref", "version", 1.0),
            ("ref", "mediaType", object()),
            ("ref", "uri", ["artifact://invalid"]),
            ("ref", "digest", False),
            ("ref", "createdBy", 7.0),
            ("ref", "taskId", ["task-1"]),
            ("ref", "parentVersion", False),
        )
        for scope, field, value in coercions:
            with self.subTest(scope=scope, field=field, value_type=type(value).__name__):
                payload = self._valid_payload()
                target = payload if scope == "payload" else payload["ref"]
                target[field] = value
                self._assert_payload_rejected(payload)

    def test_replay_rejects_blank_oversized_and_noncanonical_text_fields(self):
        blank_fields = (
            ("payload", "sessionId"),
            ("payload", "taskId"),
            ("payload", "trigger"),
            ("ref", "artifactId"),
            ("ref", "name"),
            ("ref", "mediaType"),
            ("ref", "uri"),
            ("ref", "digest"),
            ("ref", "createdBy"),
            ("ref", "taskId"),
        )
        for scope, field in blank_fields:
            with self.subTest(scope=scope, field=field):
                payload = self._valid_payload()
                target = payload if scope == "payload" else payload["ref"]
                target[field] = "  "
                self._assert_payload_rejected(payload)

        oversized_fields = (
            ("payload", "sessionId", "s" * 513),
            ("ref", "mediaType", "m" * 256),
            ("ref", "uri", "u" * 4_097),
        )
        for scope, field, value in oversized_fields:
            with self.subTest(scope=scope, field=field):
                payload = self._valid_payload()
                target = payload if scope == "payload" else payload["ref"]
                target[field] = value
                self._assert_payload_rejected(payload)

        for timestamp in (
            "2026-08-20T00:00:00+00:00",
            "2026-08-20T08:00:00+08:00",
            "2026-08-20T00:00:00.1Z",
            "2026-02-30T00:00:00Z",
        ):
            with self.subTest(timestamp=timestamp):
                payload = self._valid_payload()
                payload["createdAt"] = timestamp
                self._assert_payload_rejected(payload)

    def test_replay_enforces_content_metadata_and_json_structure_limits(self):
        payload = self._valid_payload()
        payload["content"] = "12345"
        payload["ref"]["digest"] = "sha256:" + hashlib.sha256(b"12345").hexdigest()
        with patch.object(artifacts_module, "_MAX_CONTENT_BYTES", 4):
            self._assert_payload_rejected(payload)

        metadata_cases = {}
        oversized_string = self._valid_payload()
        oversized_string["metadata"] = {"value": "abcd"}
        metadata_cases["string"] = (oversized_string, "_MAX_METADATA_STRING_LENGTH", 3)
        oversized_key = self._valid_payload()
        oversized_key["metadata"] = {"abcd": 1}
        metadata_cases["key"] = (oversized_key, "_MAX_METADATA_KEY_LENGTH", 3)
        oversized_nodes = self._valid_payload()
        oversized_nodes["metadata"] = {"items": [1, 2]}
        metadata_cases["nodes"] = (oversized_nodes, "_MAX_METADATA_NODES", 3)
        oversized_integer = self._valid_payload()
        oversized_integer["metadata"] = {"value": 8}
        metadata_cases["integer"] = (oversized_integer, "_MAX_METADATA_INTEGER_BITS", 3)
        oversized_encoding = self._valid_payload()
        oversized_encoding["metadata"] = {"value": "encoded"}
        metadata_cases["encoded-bytes"] = (oversized_encoding, "_MAX_METADATA_BYTES", 8)
        for case, (malformed, constant, maximum) in metadata_cases.items():
            with self.subTest(case=case):
                with patch.object(artifacts_module, constant, maximum):
                    self._assert_payload_rejected(malformed)

        deep = self._valid_payload()
        nested = []
        deep["metadata"] = {"nested": nested}
        for _ in range(64):
            child = []
            nested.append(child)
            nested = child
        self._assert_payload_rejected(deep)

        for nonfinite in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(nonfinite=repr(nonfinite)):
                payload = self._valid_payload()
                payload["metadata"] = {"value": nonfinite}
                self._assert_payload_rejected(payload)

        cyclic = self._valid_payload()
        cyclic_metadata = {}
        cyclic_metadata["self"] = cyclic_metadata
        cyclic["metadata"] = cyclic_metadata
        self._assert_payload_rejected(cyclic)

        unsupported = self._valid_payload()
        unsupported["metadata"] = {"tuple": (1, 2)}
        self._assert_payload_rejected(unsupported)

        non_text_key = self._valid_payload()
        non_text_key["metadata"] = {1: "value"}
        self._assert_payload_rejected(non_text_key)

    def test_replay_verifies_ref_lineage_digest_and_task_consistency(self):
        cases = {}
        bad_digest = self._valid_payload()
        bad_digest["ref"]["digest"] = "sha256:" + ("0" * 64)
        cases["digest-content"] = bad_digest
        malformed_digest = self._valid_payload()
        malformed_digest["ref"]["digest"] = "SHA256:" + ("0" * 64)
        cases["digest-shape"] = malformed_digest
        task_mismatch = self._valid_payload()
        task_mismatch["taskId"] = "different-task"
        cases["task"] = task_mismatch
        zero_version = self._valid_payload()
        zero_version["ref"]["version"] = 0
        cases["version"] = zero_version
        parent_mismatch = self._valid_payload()
        parent_mismatch["ref"]["version"] = 2
        parent_mismatch["ref"]["parentVersion"] = None
        cases["parent"] = parent_mismatch
        for case, payload in cases.items():
            with self.subTest(case=case):
                self._assert_payload_rejected(payload)

    def test_rebuild_uses_bounded_keyset_pages_without_losing_artifacts(self):
        events = (
            replay_event(1),
            replay_event(2, name="report.md", version=1, content="v1"),
            replay_event(3),
            replay_event(4, name="report.md", version=2, content="v2"),
            replay_event(5, name="notes.md", version=1, content="notes"),
        )
        source = FakeReplayStore(
            {
                0: events[:2],
                2: events[2:4],
                4: events[4:],
            }
        )
        with patch.object(artifacts_module, "_REPLAY_PAGE_LIMIT", 2):
            ledger = ArtifactLedger(source)

        self.assertEqual(source.calls, [(0, 2), (2, 2), (4, 2)])
        self.assertTrue(all(limit <= 1_000 for _cursor, limit in source.calls))
        self.assertEqual(
            [item.content for item in ledger.history("session-replay", "report.md")],
            ["v1", "v2"],
        )
        self.assertEqual(ledger.current("session-replay", "notes.md").content, "notes")

    def test_successful_rebuild_replaces_stale_state_in_one_step(self):
        source = FakeReplayStore(
            {0: (replay_event(1, name="stale.md", version=1, content="stale"),)}
        )
        with patch.object(artifacts_module, "_REPLAY_PAGE_LIMIT", 2):
            ledger = ArtifactLedger(source)
        stale_state = ledger._versions

        source.pages = {
            0: (
                replay_event(1, name="report.md", version=1, content="fresh"),
                replay_event(2, name="notes.md", version=1, content="notes"),
            )
        }
        with patch.object(artifacts_module, "_REPLAY_PAGE_LIMIT", 2):
            ledger._rebuild()

        self.assertIsNot(ledger._versions, stale_state)
        self.assertIsNone(ledger.current("session-replay", "stale.md"))
        self.assertEqual(ledger.current("session-replay", "report.md").content, "fresh")
        self.assertEqual(ledger.current("session-replay", "notes.md").content, "notes")

    def test_late_replay_failures_preserve_the_exact_previous_state(self):
        malformed_payload = StoredEvent(
            DomainEvent(
                stream_id="session:session-replay",
                event_type=ArtifactLedger.EVENT_TYPE,
                actor_id="test",
                event_id="event-malformed",
                timestamp=T0,
                payload={"sessionId": "session-replay", "content": "broken"},
            ),
            sequence=3,
            global_position=3,
        )
        late_failures = {
            "page": (
                (replay_event(2),),
                ArtifactReplayError,
            ),
            "source": (
                RuntimeError("source failed"),
                RuntimeError,
            ),
            "payload-ref": (
                (malformed_payload,),
                ArtifactReplayError,
            ),
        }
        for case, (late_response, error_type) in late_failures.items():
            with self.subTest(case=case):
                source = FakeReplayStore(
                    {0: (replay_event(1, name="stable.md", content="stable"),)}
                )
                with patch.object(artifacts_module, "_REPLAY_PAGE_LIMIT", 2):
                    ledger = ArtifactLedger(source)
                previous_state = ledger._versions
                previous_history = ledger.history("session-replay", "stable.md")
                source.pages = {
                    0: (
                        replay_event(1, name="candidate.md", content="candidate"),
                        replay_event(2),
                    ),
                    2: late_response,
                }

                with patch.object(artifacts_module, "_REPLAY_PAGE_LIMIT", 2):
                    with self.assertRaises(error_type):
                        ledger._rebuild()

                self.assertIs(ledger._versions, previous_state)
                self.assertIs(
                    ledger.history("session-replay", "stable.md"),
                    previous_history,
                )
                self.assertIsNone(ledger.current("session-replay", "candidate.md"))

    def test_replay_rejects_oversized_unordered_and_nonadvancing_pages(self):
        cases = {
            "oversized": (
                {0: (replay_event(1), replay_event(2))},
                1,
                "page limit",
            ),
            "unordered": (
                {0: (replay_event(2), replay_event(1))},
                2,
                "strictly increasing",
            ),
            "duplicate": (
                {0: (replay_event(1), replay_event(1))},
                2,
                "strictly increasing",
            ),
            "nonadvancing": (
                {0: (replay_event(0),)},
                1,
                "strictly increasing",
            ),
        }
        for case, (pages, page_limit, message) in cases.items():
            with self.subTest(case=case):
                source = FakeReplayStore(pages)
                with patch.object(artifacts_module, "_REPLAY_PAGE_LIMIT", page_limit):
                    with self.assertRaisesRegex(ArtifactReplayError, message):
                        ArtifactLedger(source)
                self.assertTrue(all(limit <= 1_000 for _cursor, limit in source.calls))

    def test_replay_limit_uses_one_record_probe_and_rejects_the_next_event(self):
        source = FakeReplayStore(
            {
                0: (replay_event(1), replay_event(2)),
                2: (replay_event(3),),
                3: (replay_event(4),),
            }
        )
        with patch.object(artifacts_module, "_REPLAY_PAGE_LIMIT", 2):
            with patch.object(artifacts_module, "_MAX_REPLAY_EVENTS", 3):
                with self.assertRaisesRegex(ArtifactReplayError, "3-event safety limit"):
                    ArtifactLedger(source)

        self.assertEqual(source.calls, [(0, 2), (2, 1), (3, 1)])

    def test_exact_replay_limit_is_accepted_only_after_an_empty_probe(self):
        source = FakeReplayStore(
            {
                0: (replay_event(1), replay_event(2)),
                2: (replay_event(3),),
            }
        )
        with patch.object(artifacts_module, "_REPLAY_PAGE_LIMIT", 2):
            with patch.object(artifacts_module, "_MAX_REPLAY_EVENTS", 3):
                ArtifactLedger(source)

        self.assertEqual(source.calls, [(0, 2), (2, 1), (3, 1)])


if __name__ == "__main__":
    unittest.main()
