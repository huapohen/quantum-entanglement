import tempfile
import unittest
from pathlib import Path

from quantum_entanglement.artifacts import ArtifactLedger
from quantum_entanglement.events import DomainEvent
from quantum_entanglement.protocol import ArtifactOutput
from quantum_entanglement.store import ConcurrencyError, SQLiteEventStore


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


if __name__ == "__main__":
    unittest.main()

