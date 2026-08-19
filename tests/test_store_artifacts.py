import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import quantum_entanglement
import quantum_entanglement.artifacts as artifacts_module
from quantum_entanglement.artifacts import ArtifactLedger, ArtifactReplayError
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
            digest=f"digest-{position}",
            created_by="test",
            task_id=f"task-{position}",
            parent_version=version - 1 if version > 1 else None,
        )
        payload = {
            "sessionId": "session-replay",
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


class ArtifactLedgerReplayTests(unittest.TestCase):
    def test_replay_error_is_part_of_the_supported_artifact_api(self):
        self.assertIn("ArtifactReplayError", artifacts_module.__all__)
        self.assertIs(quantum_entanglement.ArtifactReplayError, ArtifactReplayError)

        source = FakeReplayStore({})
        ArtifactLedger(source)
        self.assertEqual(source.calls, [(0, 1_000)])

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
                KeyError,
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
