import unittest
from unittest import mock

from quantum_entanglement.store import SQLiteEventStore


class SQLiteEventStoreInitializationTests(unittest.TestCase):
    def test_base_exception_during_initialization_closes_connection(self):
        connection = mock.Mock()

        with (
            mock.patch(
                "quantum_entanglement.store.sqlite3.connect",
                return_value=connection,
            ),
            mock.patch.object(
                SQLiteEventStore,
                "_initialize",
                side_effect=KeyboardInterrupt("injected initialization interrupt"),
            ),
        ):
            with self.assertRaisesRegex(KeyboardInterrupt, "initialization interrupt"):
                SQLiteEventStore(":memory:")

        connection.close.assert_called_once_with()

    def test_originating_initialization_control_wins_over_close_control(self):
        connection = mock.Mock()
        originating = KeyboardInterrupt("originating initialization interrupt")
        connection.close.side_effect = SystemExit(91)

        with (
            mock.patch(
                "quantum_entanglement.store.sqlite3.connect",
                return_value=connection,
            ),
            mock.patch.object(
                SQLiteEventStore,
                "_initialize",
                side_effect=originating,
            ),
        ):
            with self.assertRaises(KeyboardInterrupt) as caught:
                SQLiteEventStore(":memory:")

        self.assertIs(caught.exception, originating)
        connection.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
