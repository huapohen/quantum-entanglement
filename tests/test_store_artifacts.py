import copy
import hashlib
import json
import tempfile
import unittest
from collections import UserDict
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

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


def replay_event(
    position,
    *,
    name=None,
    version=1,
    content="",
    session_id="session-replay",
    artifact_id=None,
    trigger=None,
):
    if name is None:
        event_type = "test.non-artifact"
        payload = {"position": position}
    else:
        event_type = ArtifactLedger.EVENT_TYPE
        ref = ArtifactRef(
            artifact_id=artifact_id or f"artifact-{position}",
            name=name,
            version=version,
            media_type="text/markdown",
            uri=(f"artifact://{quote(session_id, safe='')}/{quote(name)}/v{version}"),
            digest="sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest(),
            created_by="test",
            task_id=f"task-{position}",
            parent_version=version - 1 if version > 1 else None,
        )
        payload = {
            "sessionId": session_id,
            "taskId": ref.task_id,
            "ref": ref.to_dict(),
            "content": content,
            "metadata": {"position": position},
            "createdAt": T0,
            "trigger": trigger or ("create" if version == 1 else "revise"),
        }
    return StoredEvent(
        DomainEvent(
            stream_id=f"session:{session_id}",
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


def manual_replay_usage(payload):
    metadata = payload.get("metadata", {})

    def count_nodes(value):
        if type(value) is dict:
            return 1 + len(value) + sum(count_nodes(item) for item in value.values())
        if type(value) is list:
            return 1 + sum(count_nodes(item) for item in value)
        return 1

    metadata_bytes = len(
        json.dumps(
            metadata,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
    ref = payload["ref"]
    descriptor_bytes = sum(
        len(value.encode("utf-8"))
        for value in (
            payload["sessionId"],
            ref["artifactId"],
            ref["name"],
            ref["mediaType"],
            ref["uri"],
            ref["digest"],
            ref["createdBy"],
            ref["taskId"],
            payload["createdAt"],
            payload.get("trigger", "create"),
        )
    )
    content_bytes = len(payload["content"].encode("utf-8"))
    return {
        "content_bytes": content_bytes,
        "metadata_bytes": metadata_bytes,
        "metadata_nodes": count_nodes(metadata),
        "state_data_bytes": descriptor_bytes + content_bytes + metadata_bytes,
    }


class FakeReplayStore(SQLiteEventStore):
    def __init__(self, pages):
        self.pages = pages
        self.calls = []
        self.yielded_positions = []

    def read_all(self, after_position=0, limit=1000):
        self.calls.append((after_position, limit))
        response = self.pages.get(after_position, ())
        if isinstance(response, BaseException):
            raise response
        return response

    @contextmanager
    def stream_all_page(self, after_position=0, limit=1000):
        self.calls.append((after_position, limit))
        response = self.pages.get(after_position, ())
        if isinstance(response, BaseException):
            raise response

        def items():
            for item in response:
                self.yielded_positions.append(item.global_position)
                yield item

        yield items()


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

    def test_idempotent_retry_rejects_any_changed_request_field(self):
        base = {
            "agent_id": "agent-a",
            "media_type": "text/plain",
            "metadata": {"integer": 1, "zero": 0.0, "boolean": True},
            "correlation_id": "correlation-1",
            "causation_id": "causation-1",
            "trigger": "custom",
        }

        def record(values):
            return self.ledger.record(
                "s1",
                "task-exact",
                values["agent_id"],
                ArtifactOutput(
                    "exact.txt",
                    "same",
                    media_type=values["media_type"],
                    metadata=values["metadata"],
                ),
                correlation_id=values["correlation_id"],
                causation_id=values["causation_id"],
                trigger=values["trigger"],
            )

        original = record(base)
        exact_retry = record(copy.deepcopy(base))
        self.assertEqual(exact_retry.ref.artifact_id, original.ref.artifact_id)

        changes = {
            "agent": ("agent_id", "agent-b"),
            "media-type": ("media_type", "application/json"),
            "metadata": ("metadata", {"integer": 2, "zero": 0.0, "boolean": True}),
            "metadata-integer-float": (
                "metadata",
                {"integer": 1.0, "zero": 0.0, "boolean": True},
            ),
            "metadata-boolean-integer": (
                "metadata",
                {"integer": 1, "zero": 0.0, "boolean": 1},
            ),
            "metadata-negative-zero": (
                "metadata",
                {"integer": 1, "zero": -0.0, "boolean": True},
            ),
            "correlation": ("correlation_id", "correlation-2"),
            "causation": ("causation_id", "causation-2"),
            "trigger": ("trigger", "rollback"),
        }
        for case, (field, changed_value) in changes.items():
            with self.subTest(case=case):
                changed = copy.deepcopy(base)
                changed[field] = changed_value
                with self.assertRaisesRegex(
                    ArtifactRecordError,
                    "changed its request",
                ):
                    record(changed)

        self.assertEqual(len(self.store.read_all()), 1)

    def test_default_trigger_retry_uses_the_persisted_version_after_chain_advances(self):
        first_output = ArtifactOutput("chain.txt", "v1")
        first = self.ledger.record("s1", "task-1", "agent", first_output)
        self.ledger.record("s1", "task-2", "agent", ArtifactOutput("chain.txt", "v2"))

        retried = self.ledger.record("s1", "task-1", "agent", first_output)

        self.assertEqual(retried.ref.artifact_id, first.ref.artifact_id)
        self.assertEqual(retried.trigger, "create")
        self.assertEqual(len(self.ledger.history("s1", "chain.txt")), 2)

    def test_global_position_cas_prevents_two_ledgers_from_writing_through_quota(self):
        competing = ArtifactLedger(self.store)

        with patch.object(artifacts_module, "_MAX_REPLAY_ARTIFACT_VERSIONS", 1):
            accepted = self.ledger.record(
                "session-a",
                "task-a",
                "agent",
                ArtifactOutput("a.txt", "a"),
            )
            with self.assertRaisesRegex(ArtifactRecordError, "ledger safety limits"):
                competing.record(
                    "session-b",
                    "task-b",
                    "agent",
                    ArtifactOutput("b.txt", "b"),
                )
            rebuilt = ArtifactLedger(self.store)

        self.assertEqual(len(self.store.read_all()), 1)
        self.assertEqual(rebuilt.history("session-a", "a.txt"), (accepted,))
        self.assertEqual(rebuilt.history("session-b", "b.txt"), ())

    def test_global_position_cas_is_atomic_across_independent_connections(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "shared.sqlite3")
            first_store = SQLiteEventStore(path)
            second_store = SQLiteEventStore(path)
            try:
                first_ledger = ArtifactLedger(first_store)
                second_ledger = ArtifactLedger(second_store)

                with patch.object(
                    artifacts_module,
                    "_MAX_REPLAY_ARTIFACT_VERSIONS",
                    1,
                ):
                    first_ledger.record(
                        "session-a",
                        "task-a",
                        "agent",
                        ArtifactOutput("a.txt", "a"),
                    )
                    with self.assertRaisesRegex(
                        ArtifactRecordError,
                        "ledger safety limits",
                    ):
                        second_ledger.record(
                            "session-b",
                            "task-b",
                            "agent",
                            ArtifactOutput("b.txt", "b"),
                        )
                    rebuilt = ArtifactLedger(second_store)

                self.assertEqual(len(first_store.read_all()), 1)
                self.assertEqual(len(rebuilt.history("session-a", "a.txt")), 1)
                self.assertEqual(rebuilt.history("session-b", "b.txt"), ())
            finally:
                second_store.close()
                first_store.close()

    def test_record_rebuilds_and_retries_after_global_position_conflict(self):
        append = self.store.append
        injected = False

        def append_after_interleaving(
            event,
            expected_version=None,
            *,
            expected_global_position=None,
        ):
            nonlocal injected
            if not injected:
                injected = True
                append(
                    DomainEvent(
                        stream_id="session:other",
                        event_type="test.interleaved",
                        payload={"ok": True},
                        actor_id="test",
                        idempotency_key="interleaved:1",
                    )
                )
            return append(
                event,
                expected_version,
                expected_global_position=expected_global_position,
            )

        with patch.object(self.store, "append", side_effect=append_after_interleaving):
            recorded = self.ledger.record(
                "s1",
                "task-1",
                "agent",
                ArtifactOutput("result.txt", "result"),
            )

        self.assertEqual(recorded.ref.version, 1)
        self.assertEqual(self.ledger._replay_position, 2)
        self.assertEqual(len(self.store.read_all()), 2)

    def test_record_reconciles_committed_append_wrapper_failure(self):
        append = self.store.append

        def commit_then_raise(
            event,
            expected_version=None,
            *,
            expected_global_position=None,
        ):
            append(
                event,
                expected_version,
                expected_global_position=expected_global_position,
            )
            raise RuntimeError("injected artifact postcommit failure")

        with patch.object(self.store, "append", side_effect=commit_then_raise):
            recorded = self.ledger.record(
                "s1",
                "task-1",
                "agent",
                ArtifactOutput("result.txt", "stable"),
            )

        self.assertEqual(self.ledger.current("s1", "result.txt"), recorded)
        events = self.store.read_stream("session:s1")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event.event_type, ArtifactLedger.EVENT_TYPE)

    def test_record_bounds_repeated_global_position_conflicts(self):
        with patch.object(artifacts_module, "_MAX_RECORD_ADMISSION_ATTEMPTS", 2):
            with patch.object(
                self.store,
                "append",
                side_effect=ConcurrencyError("synthetic conflict"),
            ) as append:
                with self.assertRaisesRegex(
                    ArtifactRecordError,
                    "admission did not stabilize",
                ):
                    self.ledger.record(
                        "s1",
                        "task-1",
                        "agent",
                        ArtifactOutput("result.txt", "result"),
                    )

        self.assertEqual(append.call_count, 2)
        self.assertEqual(self.store.read_all(), ())
        self.assertEqual(self.ledger.history("s1", "result.txt"), ())

    def test_record_enforces_replay_limits_but_allows_exact_idempotent_retries(self):
        cases = (
            (
                "artifact-versions",
                "_MAX_REPLAY_ARTIFACT_VERSIONS",
                "artifact_versions",
            ),
            ("content-bytes", "_MAX_REPLAY_CONTENT_BYTES", "content_bytes"),
            ("metadata-bytes", "_MAX_REPLAY_METADATA_BYTES", "metadata_bytes"),
            ("metadata-nodes", "_MAX_REPLAY_METADATA_NODES", "metadata_nodes"),
            (
                "state-data-bytes",
                "_MAX_REPLAY_STATE_DATA_BYTES",
                "state_data_bytes",
            ),
        )
        for case, constant, usage_field in cases:
            with self.subTest(case=case):
                store = SQLiteEventStore()
                try:
                    ledger = ArtifactLedger(store)
                    output = ArtifactOutput(
                        "bounded.json",
                        "x",
                        metadata={"a": 1},
                    )
                    first = ledger.record("s1", "task-1", "agent", output)
                    previous_state = ledger._versions
                    previous_usage = ledger._usage
                    maximum = getattr(previous_usage, usage_field)

                    with patch.object(artifacts_module, constant, maximum):
                        retried = ledger.record("s1", "task-1", "agent", output)
                        with self.assertRaisesRegex(
                            ArtifactRecordError,
                            "ledger safety limits",
                        ):
                            ledger.record("s1", "task-2", "agent", output)

                    self.assertEqual(retried.ref.artifact_id, first.ref.artifact_id)
                    self.assertIs(ledger._versions, previous_state)
                    self.assertIs(ledger._usage, previous_usage)
                    self.assertEqual(len(store.read_all()), 1)
                finally:
                    store.close()

    def test_record_precommit_failure_publishes_no_artifact_memory(self):
        with patch.object(
            self.store,
            "append",
            side_effect=RuntimeError("injected artifact precommit failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "artifact precommit"):
                self.ledger.record(
                    "s1",
                    "task-1",
                    "agent",
                    ArtifactOutput("result.txt", "stable"),
                )

        self.assertIsNone(self.ledger.current("s1", "result.txt"))
        self.assertEqual(self.store.read_stream("session:s1"), ())

    def test_record_does_not_reconcile_a_different_committed_event(self):
        append = self.store.append

        def commit_other_then_raise(
            event,
            expected_version=None,
            *,
            expected_global_position=None,
        ):
            append(
                DomainEvent(
                    stream_id=event.stream_id,
                    event_type="test.concurrent",
                    actor_id="other",
                    payload={"other": True},
                ),
                expected_version,
                expected_global_position=expected_global_position,
            )
            raise RuntimeError("injected different postcommit failure")

        with patch.object(self.store, "append", side_effect=commit_other_then_raise):
            with self.assertRaisesRegex(RuntimeError, "different postcommit"):
                self.ledger.record(
                    "s1",
                    "task-1",
                    "agent",
                    ArtifactOutput("result.txt", "stable"),
                )

        self.assertIsNone(self.ledger.current("s1", "result.txt"))
        events = self.store.read_stream("session:s1")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event.event_type, "test.concurrent")

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
                    self.assertEqual(
                        self.ledger.history("s1", "stable.json"),
                        original_history,
                    )
                    self.assertEqual(original_history, (baseline,))
        finally:
            self.store._connection.set_trace_callback(None)

    def test_record_deeply_isolates_input_event_return_and_internal_metadata(self):
        metadata = {
            "nested": {"values": [1, {"label": "original"}]},
            "tags": [{"name": "stable"}],
        }
        captured_events = []
        append = self.store.append

        def capture_event(event, expected_version=None, *, expected_global_position=None):
            captured_events.append(event)
            return append(
                event,
                expected_version,
                expected_global_position=expected_global_position,
            )

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
        self.assertEqual(self.ledger.current("s1", "snapshot.json").metadata, expected)

        internal = self.ledger._versions[("s1", "snapshot.json")][0]
        self.assertEqual(self.ledger._snapshot_metadata(internal.metadata), expected)
        self.assertEqual(type(internal.metadata).__name__, "mappingproxy")
        self.assertEqual(type(internal.metadata["nested"]).__name__, "mappingproxy")
        self.assertIs(type(internal.metadata["nested"]["values"]), tuple)
        self.assertEqual(
            type(internal.metadata["nested"]["values"][1]).__name__,
            "mappingproxy",
        )

        persisted_metadata = self.store.read_all()[0].event.payload["metadata"]
        self.assertEqual(persisted_metadata, expected)

    def test_every_public_read_returns_an_independent_plain_json_metadata_snapshot(self):
        expected = {
            "nested": {"values": [{"label": "stable"}, 7]},
            "tags": ["one", "two"],
        }
        recorded = self.ledger.record(
            "s1",
            "task-snapshot",
            "agent",
            ArtifactOutput("snapshot.json", "body", metadata=copy.deepcopy(expected)),
        )

        readers = (
            ("record", lambda: recorded),
            ("current", lambda: self.ledger.current("s1", "snapshot.json")),
            ("history", lambda: self.ledger.history("s1", "snapshot.json")[0]),
            ("current-all", lambda: self.ledger.current_all("s1")[0]),
            ("by-task", lambda: self.ledger.by_task("s1", "task-snapshot")[0]),
        )
        for label, read in readers:
            with self.subTest(reader=label):
                item = read()
                self.assertIsNotNone(item)
                self.assertIs(type(item.metadata), dict)
                self.assertIs(type(item.metadata["nested"]), dict)
                self.assertIs(type(item.metadata["nested"]["values"]), list)
                item.metadata["nested"]["values"][0]["label"] = label
                item.metadata["nested"]["values"].append({"reader": label})
                item.metadata["tags"].clear()

                fresh = self.ledger.current("s1", "snapshot.json")
                self.assertEqual(fresh.metadata, expected)
                self.assertIsNot(fresh.metadata, item.metadata)
                self.assertIsNot(fresh.metadata["nested"], item.metadata["nested"])
                self.assertIsNot(
                    fresh.metadata["nested"]["values"],
                    item.metadata["nested"]["values"],
                )

    def test_restore_and_replay_results_cannot_mutate_ledger_metadata(self):
        source_metadata = {
            "nested": {"values": [{"label": "source"}]},
            "tags": ["stable"],
        }
        self.ledger.record(
            "s1",
            "task-source",
            "agent",
            ArtifactOutput("report.json", "v1", metadata=copy.deepcopy(source_metadata)),
        )
        restored = self.ledger.restore(
            "s1",
            "report.json",
            1,
            "task-restore",
            "owner",
        )

        restored.metadata["restoredFrom"] = 999
        restored.metadata["injected"] = {"values": ["mutated"]}
        fresh_history = self.ledger.history("s1", "report.json")
        self.assertEqual(fresh_history[0].metadata, source_metadata)
        self.assertEqual(fresh_history[1].metadata, {"restoredFrom": 1})

        rebuilt = ArtifactLedger(self.store)
        replayed_source = rebuilt.history("s1", "report.json")[0]
        replayed_source.metadata["nested"]["values"][0]["label"] = "replay-mutated"
        replayed_source.metadata["tags"].append("replay-mutated")
        replayed_head = rebuilt.current("s1", "report.json")
        replayed_head.metadata["restoredFrom"] = -1

        rebuilt_history = rebuilt.history("s1", "report.json")
        self.assertEqual(rebuilt_history[0].metadata, source_metadata)
        self.assertEqual(rebuilt_history[1].metadata, {"restoredFrom": 1})
        self.assertEqual(
            type(rebuilt._versions[("s1", "report.json")][0].metadata).__name__,
            "mappingproxy",
        )

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

    def test_replay_requires_each_version_chain_to_start_at_one_and_be_contiguous(self):
        cases = {
            "missing-v1": (replay_event(1, name="report.md", version=2),),
            "gap": (
                replay_event(1, name="report.md", version=1),
                replay_event(2, name="report.md", version=3),
            ),
            "duplicate-version": (
                replay_event(1, name="report.md", version=1),
                replay_event(2, name="report.md", version=1),
            ),
            "descending-version": (
                replay_event(1, name="report.md", version=1),
                replay_event(2, name="report.md", version=2),
                replay_event(3, name="report.md", version=1),
            ),
        }
        for case, events in cases.items():
            with self.subTest(case=case):
                source = FakeReplayStore({0: events})
                with self.assertRaisesRegex(ArtifactReplayError, "version chain"):
                    ArtifactLedger(source)

    def test_replay_rejects_duplicate_artifact_ids_across_the_ledger(self):
        cases = {
            "same-chain": (
                replay_event(1, name="report.md", version=1, artifact_id="artifact-shared"),
                replay_event(2, name="report.md", version=2, artifact_id="artifact-shared"),
            ),
            "different-artifact": (
                replay_event(1, name="report.md", artifact_id="artifact-shared"),
                replay_event(2, name="notes.md", artifact_id="artifact-shared"),
            ),
            "different-session": (
                replay_event(
                    1,
                    name="report.md",
                    session_id="session-a",
                    artifact_id="artifact-shared",
                ),
                replay_event(
                    2,
                    name="report.md",
                    session_id="session-b",
                    artifact_id="artifact-shared",
                ),
            ),
        }
        for case, events in cases.items():
            with self.subTest(case=case):
                source = FakeReplayStore({0: events})
                with self.assertRaisesRegex(ArtifactReplayError, "duplicate artifact id"):
                    ArtifactLedger(source)

    def test_replay_requires_the_exact_generated_artifact_uri(self):
        session_id = "team/a b"
        name = "folder/报告 v1.md"
        valid = replay_event(1, name=name, session_id=session_id)
        canonical_uri = "artifact://team%2Fa%20b/folder/%E6%8A%A5%E5%91%8A%20v1.md/v1"
        self.assertEqual(valid.event.payload["ref"]["uri"], canonical_uri)
        accepted = ArtifactLedger(FakeReplayStore({0: (valid,)}))
        self.assertEqual(accepted.current(session_id, name).ref.uri, canonical_uri)

        variants = {
            "unescaped-session-slash": canonical_uri.replace("team%2F", "team/", 1),
            "lowercase-percent-escape": canonical_uri.replace("%2F", "%2f", 1),
            "plus-for-space": canonical_uri.replace("%20", "+", 1),
            "encoded-name-slash": canonical_uri.replace("/folder/", "/folder%2F", 1),
            "raw-unicode": canonical_uri.replace(
                "%E6%8A%A5%E5%91%8A",
                "报告",
                1,
            ),
            "noncanonical-version": canonical_uri.removesuffix("/v1") + "/v01",
            "trailing-slash": canonical_uri + "/",
        }
        for case, uri in variants.items():
            with self.subTest(case=case):
                payload = copy.deepcopy(valid.event.payload)
                payload["ref"]["uri"] = uri
                self._assert_payload_rejected(payload)

    def test_replay_accepts_interleaved_independent_chains_and_custom_triggers(self):
        shared_name = "folder/报告.md"
        events = (
            replay_event(
                1,
                name=shared_name,
                session_id="session/a",
                content="a1",
                trigger="publish",
            ),
            replay_event(
                2,
                name=shared_name,
                session_id="session-b",
                content="b1",
                trigger="rollback",
            ),
            replay_event(
                3,
                name="notes.txt",
                session_id="session/a",
                content="notes",
                trigger="custom",
            ),
            replay_event(
                4,
                name=shared_name,
                session_id="session/a",
                version=2,
                content="a2",
                trigger="rollback",
            ),
        )

        ledger = ArtifactLedger(FakeReplayStore({0: events}))

        self.assertEqual(
            [(item.content, item.trigger) for item in ledger.history("session/a", shared_name)],
            [("a1", "publish"), ("a2", "rollback")],
        )
        self.assertEqual(ledger.current("session-b", shared_name).content, "b1")
        self.assertEqual(ledger.current("session/a", "notes.txt").trigger, "custom")

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

    def test_rebuild_accumulates_a_long_single_chain_and_freezes_it_once(self):
        version_count = 4_096
        page_size = 257
        events = tuple(
            replay_event(
                version,
                name="long-chain.md",
                version=version,
                content=f"v{version}",
            )
            for version in range(1, version_count + 1)
        )
        pages = {
            offset: events[offset : offset + page_size]
            for offset in range(0, version_count, page_size)
        }
        source = FakeReplayStore(pages)

        with patch.object(artifacts_module, "_REPLAY_PAGE_LIMIT", page_size):
            ledger = ArtifactLedger(source)

        history = ledger._versions[("session-replay", "long-chain.md")]
        self.assertIs(type(history), tuple)
        self.assertEqual(len(history), version_count)
        self.assertEqual(history[0].ref.version, 1)
        self.assertEqual(history[-1].ref.version, version_count)

    def test_replay_cumulative_safety_limits_fail_closed_without_state_leakage(self):
        first = replay_event(
            1,
            name="candidate.md",
            version=1,
            content="a",
        )
        second = replay_event(
            2,
            name="candidate.md",
            version=2,
            content="b",
        )
        first_usage = manual_replay_usage(first.event.payload)
        cases = (
            (
                "artifact-versions",
                "_MAX_REPLAY_ARTIFACT_VERSIONS",
                1,
                "artifact-version count",
            ),
            (
                "content-bytes",
                "_MAX_REPLAY_CONTENT_BYTES",
                first_usage["content_bytes"],
                "content-byte count",
            ),
            (
                "metadata-bytes",
                "_MAX_REPLAY_METADATA_BYTES",
                first_usage["metadata_bytes"],
                "metadata-byte count",
            ),
            (
                "metadata-nodes",
                "_MAX_REPLAY_METADATA_NODES",
                first_usage["metadata_nodes"],
                "metadata-node count",
            ),
            (
                "retained-state-bytes",
                "_MAX_REPLAY_STATE_DATA_BYTES",
                first_usage["state_data_bytes"],
                "state-data byte count",
            ),
        )
        for case, constant, maximum, message in cases:
            with self.subTest(case=case):
                source = FakeReplayStore(
                    {0: (replay_event(1, name="stable.md", content="stable"),)}
                )
                ledger = ArtifactLedger(source)
                previous_state = ledger._versions
                previous_usage = ledger._usage
                previous_position = ledger._replay_position
                previous_history = ledger.history("session-replay", "stable.md")
                source.pages = {0: (first, second)}

                with patch.object(artifacts_module, "_REPLAY_PAGE_LIMIT", 2):
                    with patch.object(artifacts_module, constant, maximum):
                        with self.assertRaisesRegex(ArtifactReplayError, message):
                            ledger._rebuild()

                self.assertIs(ledger._versions, previous_state)
                self.assertIs(ledger._usage, previous_usage)
                self.assertEqual(ledger._replay_position, previous_position)
                self.assertEqual(
                    ledger.history("session-replay", "stable.md"),
                    previous_history,
                )
                self.assertIsNone(ledger.current("session-replay", "candidate.md"))

    def test_stream_replay_stops_decoding_at_the_first_budget_failure(self):
        source = FakeReplayStore(
            {
                0: (
                    replay_event(1, name="candidate.md", content="a"),
                    replay_event(2, name="other.md", content="b"),
                )
            }
        )

        with patch.object(artifacts_module, "_MAX_REPLAY_CONTENT_BYTES", 0):
            with self.assertRaisesRegex(ArtifactReplayError, "content-byte count"):
                ArtifactLedger(source)

        self.assertEqual(source.yielded_positions, [1])

    def test_exact_cumulative_replay_safety_limits_are_accepted(self):
        events = (
            replay_event(1, name="exact.md", version=1, content="a"),
            replay_event(2, name="exact.md", version=2, content="b"),
        )
        usages = tuple(manual_replay_usage(item.event.payload) for item in events)
        limits = {
            "_MAX_REPLAY_ARTIFACT_VERSIONS": len(usages),
            "_MAX_REPLAY_CONTENT_BYTES": sum(item["content_bytes"] for item in usages),
            "_MAX_REPLAY_METADATA_BYTES": sum(item["metadata_bytes"] for item in usages),
            "_MAX_REPLAY_METADATA_NODES": sum(item["metadata_nodes"] for item in usages),
            "_MAX_REPLAY_STATE_DATA_BYTES": sum(item["state_data_bytes"] for item in usages),
        }

        with ExitStack() as patches:
            for constant, maximum in limits.items():
                patches.enter_context(patch.object(artifacts_module, constant, maximum))
            ledger = ArtifactLedger(FakeReplayStore({0: events}))

        self.assertEqual(len(ledger.history("session-replay", "exact.md")), 2)

    def test_replay_usage_counts_keys_and_multibyte_state_data_independently(self):
        stored = replay_event(
            1,
            session_id="团队/a",
            name="报告.md",
            content="协作",
            trigger="发布",
        )
        payload = copy.deepcopy(stored.event.payload)
        payload["metadata"] = {"a": 1, "nested": {"β": [True, None]}}
        expected = manual_replay_usage(payload)

        decoded = ArtifactLedger._decode_persisted_version_with_usage(payload)

        self.assertEqual(expected["metadata_nodes"], 9)
        self.assertEqual(decoded.content_bytes, expected["content_bytes"])
        self.assertEqual(decoded.metadata_bytes, expected["metadata_bytes"])
        self.assertEqual(decoded.metadata_nodes, expected["metadata_nodes"])
        self.assertEqual(decoded.state_data_bytes, expected["state_data_bytes"])

    def test_successful_rebuild_replaces_stale_state_in_one_step(self):
        source = FakeReplayStore(
            {0: (replay_event(1, name="stale.md", version=1, content="stale"),)}
        )
        with patch.object(artifacts_module, "_REPLAY_PAGE_LIMIT", 2):
            ledger = ArtifactLedger(source)
        stale_state = ledger._versions
        stale_usage = ledger._usage
        stale_position = ledger._replay_position

        source.pages = {
            0: (
                replay_event(1, name="report.md", version=1, content="fresh"),
                replay_event(2, name="notes.md", version=1, content="notes"),
            )
        }
        with patch.object(artifacts_module, "_REPLAY_PAGE_LIMIT", 2):
            ledger._rebuild()

        self.assertIsNot(ledger._versions, stale_state)
        self.assertIsNot(ledger._usage, stale_usage)
        self.assertNotEqual(ledger._replay_position, stale_position)
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
                previous_usage = ledger._usage
                previous_position = ledger._replay_position
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
                self.assertIs(ledger._usage, previous_usage)
                self.assertEqual(ledger._replay_position, previous_position)
                self.assertEqual(
                    ledger.history("session-replay", "stable.md"),
                    previous_history,
                )
                self.assertIsNone(ledger.current("session-replay", "candidate.md"))

    def test_late_chain_and_uri_failures_preserve_the_exact_previous_state(self):
        invalid_uri_payload = copy.deepcopy(
            replay_event(3, name="candidate.md", version=2).event.payload
        )
        invalid_uri_payload["ref"]["uri"] += "/"
        cases = {
            "gap": (
                (
                    replay_event(1, name="candidate.md", version=1),
                    replay_event(2),
                ),
                (replay_event(3, name="candidate.md", version=3),),
            ),
            "duplicate-version": (
                (
                    replay_event(1, name="candidate.md", version=1),
                    replay_event(2),
                ),
                (replay_event(3, name="candidate.md", version=1),),
            ),
            "descending-version": (
                (
                    replay_event(1, name="candidate.md", version=1),
                    replay_event(2, name="candidate.md", version=2),
                ),
                (replay_event(3, name="candidate.md", version=1),),
            ),
            "duplicate-artifact-id": (
                (
                    replay_event(
                        1,
                        name="candidate.md",
                        version=1,
                        artifact_id="artifact-shared",
                    ),
                    replay_event(2),
                ),
                (
                    replay_event(
                        3,
                        name="candidate.md",
                        version=2,
                        artifact_id="artifact-shared",
                    ),
                ),
            ),
            "uri": (
                (
                    replay_event(1, name="candidate.md", version=1),
                    replay_event(2),
                ),
                (replay_event_with_payload(3, invalid_uri_payload),),
            ),
        }
        for case, (first_page, late_page) in cases.items():
            with self.subTest(case=case):
                source = FakeReplayStore(
                    {0: (replay_event(1, name="stable.md", content="stable"),)}
                )
                with patch.object(artifacts_module, "_REPLAY_PAGE_LIMIT", 2):
                    ledger = ArtifactLedger(source)
                previous_state = ledger._versions
                previous_usage = ledger._usage
                previous_position = ledger._replay_position
                previous_history = ledger.history("session-replay", "stable.md")
                source.pages = {0: first_page, 2: late_page}

                with patch.object(artifacts_module, "_REPLAY_PAGE_LIMIT", 2):
                    with self.assertRaises(ArtifactReplayError):
                        ledger._rebuild()

                self.assertIs(ledger._versions, previous_state)
                self.assertIs(ledger._usage, previous_usage)
                self.assertEqual(ledger._replay_position, previous_position)
                self.assertEqual(
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
