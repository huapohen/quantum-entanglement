import sqlite3
import tempfile
import unittest
from pathlib import Path

from quantum_entanglement.events import DomainEvent
from quantum_entanglement.store import SQLiteEventStore


class SQLiteEventTransactionTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tempdir.name) / "events.sqlite3")
        self.store = SQLiteEventStore(self.path)

    def tearDown(self):
        self.store.close()
        self.tempdir.cleanup()

    def assert_second_connection_can_acquire_write_lock(self):
        contender = sqlite3.connect(self.path, isolation_level=None, timeout=0.1)
        try:
            contender.execute("BEGIN IMMEDIATE")
            self.assertTrue(contender.in_transaction)
            contender.execute("ROLLBACK")
        finally:
            contender.close()

    def test_base_exception_rolls_back_and_releases_write_lock(self):
        with self.assertRaisesRegex(KeyboardInterrupt, "injected interrupt"):
            with self.store._transaction():
                raise KeyboardInterrupt("injected interrupt")

        self.assertFalse(self.store._connection.in_transaction)
        self.assert_second_connection_can_acquire_write_lock()

    def test_commit_denial_rolls_back_and_releases_write_lock(self):
        def deny_commit(action_code, operation, _table, _database, _trigger):
            if action_code == sqlite3.SQLITE_TRANSACTION and str(operation).upper() == "COMMIT":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        event = DomainEvent(
            "session:commit-denial",
            "session.created",
            {},
            "test",
            idempotency_key="commit-denial",
        )
        self.store._connection.set_authorizer(deny_commit)
        try:
            with self.assertRaisesRegex(sqlite3.DatabaseError, "not authorized"):
                self.store.append(event)
        finally:
            # Python 3.9 does not reliably treat None as "disable authorizer".
            self.store._connection.set_authorizer(lambda *_args: sqlite3.SQLITE_OK)

        self.assertFalse(self.store._connection.in_transaction)
        self.assertEqual(self.store.stream_version("session:commit-denial"), 0)
        self.assert_second_connection_can_acquire_write_lock()


if __name__ == "__main__":
    unittest.main()
