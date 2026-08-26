import importlib.resources
import sqlite3
import tempfile
import unittest
from asyncio import CancelledError
from pathlib import Path
from unittest import mock

from quantum_entanglement.attempts import SQLiteInvocationAttemptStore
from quantum_entanglement.migrations import (
    MIGRATIONS,
    Migration,
    MigrationDriftError,
    MigrationVersionError,
    apply_sqlite_migrations,
    current_schema_version,
    migration_text,
    validate_sqlite_schema,
)
from quantum_entanglement.store import SQLiteEventStore

NOW = "2026-08-20T00:00:00Z"


class SQLiteMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tempdir.name) / "state.sqlite3")

    def tearDown(self):
        self.tempdir.cleanup()

    def test_artifact_schema_is_applied_and_rollback_is_replayable(self):
        with SQLiteInvocationAttemptStore(self.path) as store:
            self.assertEqual(store.schema_version(), 2)

        connection = sqlite3.connect(self.path)
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            self.assertIn("artifact_blobs", tables)
            self.assertIn("artifact_versions", tables)
            down = (
                importlib.resources.files("quantum_entanglement.migrations")
                .joinpath("0002_artifacts.down.sql")
                .read_text(encoding="utf-8")
            )
            connection.executescript(down)
            remaining = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            self.assertNotIn("artifact_blobs", remaining)
            self.assertNotIn("artifact_versions", remaining)
        finally:
            connection.close()

        with SQLiteInvocationAttemptStore(self.path) as reopened:
            self.assertEqual(reopened.schema_version(), 2)

    def test_artifact_blob_constraints_reject_invalid_digest_and_size(self):
        with SQLiteInvocationAttemptStore(self.path):
            pass
        connection = sqlite3.connect(self.path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO artifact_blobs(digest, content, byte_size, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    ("sha256:" + ("g" * 64), b"data", 4, NOW),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO artifact_blobs(digest, content, byte_size, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    ("sha256:" + ("0" * 64), b"data", 3, NOW),
                )
        finally:
            connection.close()

    def test_database_from_future_binary_is_rejected(self):
        with SQLiteInvocationAttemptStore(self.path):
            pass
        connection = sqlite3.connect(self.path)
        connection.execute(
            """
            INSERT INTO qe_schema_migrations(version, filename, sha256, applied_at)
            VALUES (999, '0999_future.up.sql', ?, ?)
            """,
            ("0" * 64, NOW),
        )
        connection.commit()
        connection.close()

        with self.assertRaises(MigrationVersionError):
            SQLiteInvocationAttemptStore(self.path)


class CommitFailingConnection:
    def __init__(self, delegate):
        self.delegate = delegate
        self.failed = False

    @property
    def in_transaction(self):
        return self.delegate.in_transaction

    def create_function(self, *args, **kwargs):
        return self.delegate.create_function(*args, **kwargs)

    def execute(self, statement, parameters=()):
        if statement == "COMMIT" and not self.failed:
            self.failed = True
            raise sqlite3.OperationalError("injected commit failure")
        return self.delegate.execute(statement, parameters)


class MigrationProcessMismatchSignal(BaseException):
    pass


class ProcessGuardedConnection:
    def __init__(self, delegate, drifted):
        self.delegate = delegate
        self.drifted = drifted

    @property
    def in_transaction(self):
        if self.drifted():
            raise AssertionError("migration inspected inherited transaction state")
        return self.delegate.in_transaction

    def create_function(self, *args, **kwargs):
        if self.drifted():
            raise AssertionError("migration touched inherited connection")
        return self.delegate.create_function(*args, **kwargs)

    def execute(self, statement, parameters=()):
        if self.drifted():
            raise AssertionError("migration touched inherited connection")
        return self.delegate.execute(statement, parameters)


class MigrationRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tempdir.name) / "migrations.sqlite3")
        self.connection = sqlite3.connect(self.path, isolation_level=None)
        self.connection.row_factory = sqlite3.Row

    def tearDown(self):
        self.connection.close()
        self.tempdir.cleanup()

    def ledger_count(self):
        return self.connection.execute("SELECT COUNT(*) FROM qe_schema_migrations").fetchone()[0]

    def table_exists(self, name):
        return (
            self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (name,),
            ).fetchone()
            is not None
        )

    def prepare_event_store_migration_prefix(self, count: int) -> None:
        """Materialize the event-store prerequisites, then downgrade to v3 when needed."""

        if count not in (3, 4):
            raise ValueError("event-store migration prefix must be 3 or 4")
        self.connection.close()
        store = SQLiteEventStore(self.path, clock=lambda: NOW)
        store.close()
        self.connection = sqlite3.connect(self.path, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        for migration in reversed(MIGRATIONS[count:]):
            self.connection.executescript(
                migration_text(migration.filename.replace(".up.sql", ".down.sql"))
            )
            self.connection.execute(
                "DELETE FROM main.qe_schema_migrations WHERE version = ?",
                (migration.version,),
            )

    def test_invocation_admission_migration_upgrades_v3_with_exact_constraints(self):
        self.prepare_event_store_migration_prefix(3)

        self.assertEqual(
            apply_sqlite_migrations(
                self.connection,
                target_versions=(1, 2, 3),
                clock=lambda: NOW,
            ),
            3,
        )
        self.assertFalse(self.table_exists("invocation_admissions"))
        self.assertEqual(
            apply_sqlite_migrations(
                self.connection,
                target_versions=(1, 2, 3, 4),
                clock=lambda: NOW,
            ),
            4,
        )
        self.assertEqual(validate_sqlite_schema(self.connection), 4)
        self.assertTrue(self.table_exists("invocation_admissions"))
        self.assertIsNotNone(
            self.connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE type = 'index' "
                "AND name = 'idx_invocation_admissions_stream'"
            ).fetchone()
        )
        foreign_key_sources = {
            row[3]
            for row in self.connection.execute(
                "PRAGMA main.foreign_key_list('invocation_admissions')"
            ).fetchall()
        }
        self.assertEqual(
            foreign_key_sources,
            {
                "invocation_id",
                "stream_id",
                "first_sequence",
                "last_sequence",
                "first_global_position",
                "last_global_position",
            },
        )
        table_sql = self.connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = 'invocation_admissions'"
        ).fetchone()[0]
        for invariant in (
            "receipt_format = 'qe.invocation-admission-receipt/1'",
            "stream_id = 'session:' || session_id",
            "first_sequence = original_version + 1",
            "last_sequence = original_version + event_count",
            "last_global_position = first_global_position + event_count - 1",
        ):
            with self.subTest(invariant=invariant):
                self.assertIn(invariant, table_sql)

    def test_invocation_admission_migration_adopts_only_exact_preexisting_objects(self):
        self.prepare_event_store_migration_prefix(3)
        migration = MIGRATIONS[3]
        self.connection.executescript(migration_text(migration.filename))

        self.assertEqual(self.ledger_count(), 3)
        self.assertEqual(
            apply_sqlite_migrations(self.connection, clock=lambda: NOW),
            4,
        )
        self.assertEqual(self.ledger_count(), 4)

        self.connection.execute("DELETE FROM main.qe_schema_migrations WHERE version = 4")
        self.connection.execute("DROP INDEX idx_invocation_admissions_stream")
        self.connection.execute(
            "CREATE INDEX idx_invocation_admissions_stream ON invocation_admissions(stream_id)"
        )
        with self.assertRaisesRegex(
            MigrationDriftError,
            "idx_invocation_admissions_stream.*differs",
        ):
            apply_sqlite_migrations(self.connection, clock=lambda: NOW)

        self.assertFalse(self.connection.in_transaction)
        self.assertEqual(self.ledger_count(), 3)
        drifted_sql = self.connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type = 'index' "
            "AND name = 'idx_invocation_admissions_stream'"
        ).fetchone()[0]
        self.assertIn("invocation_admissions(stream_id)", drifted_sql)

    def test_invocation_admission_applied_ledger_filename_and_checksum_drift_fail_closed(self):
        self.prepare_event_store_migration_prefix(4)
        original = self.connection.execute(
            "SELECT filename, sha256 FROM main.qe_schema_migrations WHERE version = 4"
        ).fetchone()

        for column, replacement in (
            ("filename", "0004_tampered.up.sql"),
            ("sha256", "0" * 64),
        ):
            with self.subTest(column=column):
                self.connection.execute(
                    f"UPDATE main.qe_schema_migrations SET {column} = ? WHERE version = 4",
                    (replacement,),
                )
                with self.assertRaisesRegex(
                    MigrationDriftError,
                    "migration 4 checksum or filename differs",
                ):
                    apply_sqlite_migrations(self.connection, clock=lambda: NOW)
                self.connection.execute(
                    "UPDATE main.qe_schema_migrations "
                    "SET filename = ?, sha256 = ? WHERE version = 4",
                    (original["filename"], original["sha256"]),
                )

    def test_target_versions_must_be_a_continuous_registry_prefix(self):
        for invalid in ((2,), (1, 3), (1, 1), (3, 2, 1)):
            with self.subTest(target_versions=invalid):
                with self.assertRaisesRegex(ValueError, "continuous registry prefix"):
                    apply_sqlite_migrations(
                        self.connection,
                        target_versions=invalid,
                        clock=lambda: NOW,
                    )
        for invalid_type in ((True,), (1.0,)):
            with self.subTest(target_versions=invalid_type):
                with self.assertRaisesRegex(TypeError, "must be integers"):
                    apply_sqlite_migrations(
                        self.connection,
                        target_versions=invalid_type,
                        clock=lambda: NOW,
                    )
        self.assertFalse(self.table_exists("qe_schema_migrations"))
        self.assertEqual(
            apply_sqlite_migrations(
                self.connection,
                target_versions=(),
                clock=lambda: NOW,
            ),
            0,
        )
        self.assertEqual(self.ledger_count(), 0)
        self.assertEqual(tuple(item.version for item in MIGRATIONS[:2]), (1, 2))

    def test_registry_and_applied_ledger_must_not_contain_holes(self):
        with self.assertRaisesRegex(ValueError, "continuous prefix"):
            apply_sqlite_migrations(
                self.connection,
                migrations=(
                    Migration(1, "0001_first.up.sql"),
                    Migration(3, "0003_third.up.sql"),
                ),
                clock=lambda: NOW,
            )

        apply_sqlite_migrations(
            self.connection,
            target_versions=(1, 2),
            clock=lambda: NOW,
        )
        self.connection.execute("DELETE FROM qe_schema_migrations WHERE version = 1")

        with self.assertRaisesRegex(MigrationDriftError, "continuous registry prefix"):
            apply_sqlite_migrations(
                self.connection,
                target_versions=(1, 2),
                clock=lambda: NOW,
            )
        with self.assertRaisesRegex(MigrationDriftError, "continuous prefix"):
            current_schema_version(self.connection)
        versions = self.connection.execute(
            "SELECT version FROM qe_schema_migrations ORDER BY version"
        ).fetchall()
        self.assertEqual([row["version"] for row in versions], [2])

    def test_schema_validator_rejects_missing_and_weakened_migration_objects(self):
        apply_sqlite_migrations(
            self.connection,
            target_versions=(1, 2),
            clock=lambda: NOW,
        )
        self.assertEqual(validate_sqlite_schema(self.connection), 2)

        self.connection.execute("DROP TABLE artifact_versions")
        self.connection.execute("DROP TABLE artifact_blobs")
        self.connection.execute("CREATE TABLE artifact_blobs(digest TEXT PRIMARY KEY)")
        with self.assertRaisesRegex(MigrationDriftError, "artifact_blobs.*differs"):
            validate_sqlite_schema(self.connection)

        second = sqlite3.connect(":memory:", isolation_level=None)
        second.row_factory = sqlite3.Row
        try:
            apply_sqlite_migrations(
                second,
                target_versions=(1,),
                clock=lambda: NOW,
            )
            second.execute("DROP TABLE invocation_attempts")
            with self.assertRaisesRegex(MigrationDriftError, "invocation_attempts.*missing"):
                validate_sqlite_schema(second)
        finally:
            second.close()

    def test_schema_validator_ignores_temporary_ledger_shadowing(self):
        apply_sqlite_migrations(
            self.connection,
            target_versions=(1,),
            clock=lambda: NOW,
        )
        self.connection.execute(
            """
            CREATE TEMP TABLE qe_schema_migrations (
                version INTEGER,
                filename TEXT,
                sha256 TEXT
            )
            """
        )
        self.connection.execute(
            "INSERT INTO temp.qe_schema_migrations VALUES (999, 'forged.up.sql', 'forged')"
        )

        self.assertEqual(validate_sqlite_schema(self.connection), 1)
        self.assertEqual(
            apply_sqlite_migrations(
                self.connection,
                target_versions=(1, 2),
                clock=lambda: NOW,
            ),
            2,
        )
        self.assertEqual(
            self.connection.execute("SELECT version FROM temp.qe_schema_migrations").fetchone()[0],
            999,
        )

    def test_schema_validator_rejects_non_table_ledger_object(self):
        self.connection.execute("CREATE VIEW qe_schema_migrations AS SELECT 1 AS version")

        with self.assertRaisesRegex(MigrationDriftError, "is not a table"):
            validate_sqlite_schema(self.connection)

    def test_migration_runner_rejects_weakened_applied_schema(self):
        apply_sqlite_migrations(
            self.connection,
            target_versions=(1, 2),
            clock=lambda: NOW,
        )
        self.connection.execute("DROP TABLE artifact_versions")
        self.connection.execute("DROP TABLE artifact_blobs")
        self.connection.execute("CREATE TABLE artifact_blobs(digest TEXT PRIMARY KEY)")

        with self.assertRaisesRegex(MigrationDriftError, "artifact_blobs.*differs"):
            apply_sqlite_migrations(
                self.connection,
                target_versions=(1, 2),
                clock=lambda: NOW,
            )
        self.assertEqual(
            [
                row[0]
                for row in self.connection.execute(
                    "SELECT version FROM main.qe_schema_migrations ORDER BY version"
                ).fetchall()
            ],
            [1, 2],
        )

    def test_prebuilt_weakened_table_never_receives_migration_checksum(self):
        self.connection.execute("CREATE TABLE artifact_blobs(digest TEXT PRIMARY KEY)")

        with self.assertRaisesRegex(MigrationDriftError, "artifact_blobs.*differs"):
            apply_sqlite_migrations(
                self.connection,
                target_versions=(1, 2),
                clock=lambda: NOW,
            )

        self.assertEqual(
            [
                row[0]
                for row in self.connection.execute(
                    "SELECT version FROM main.qe_schema_migrations ORDER BY version"
                ).fetchall()
            ],
            [1],
        )
        self.assertFalse(self.table_exists("artifact_versions"))

    def test_weakened_ledger_schema_is_rejected_before_row_access(self):
        self.connection.execute("CREATE TABLE qe_schema_migrations(version INTEGER PRIMARY KEY)")

        with self.assertRaisesRegex(MigrationDriftError, "qe_schema_migrations.*differs"):
            apply_sqlite_migrations(
                self.connection,
                target_versions=(),
                clock=lambda: NOW,
            )

    def test_statement_splitter_handles_same_line_and_quoted_semicolon(self):
        migration = Migration(1, "0001_compound.up.sql")
        compound = (
            "CREATE TABLE first_table (value TEXT); "
            "CREATE TABLE second_table (value TEXT DEFAULT ';');"
        )

        with mock.patch(
            "quantum_entanglement.migrations.migration_text",
            return_value=compound,
        ):
            self.assertEqual(
                apply_sqlite_migrations(
                    self.connection,
                    migrations=(migration,),
                    clock=lambda: NOW,
                ),
                1,
            )

        self.assertTrue(self.table_exists("first_table"))
        self.assertTrue(self.table_exists("second_table"))

    def test_operational_error_rolls_back_partial_ddl_and_can_retry(self):
        migration = Migration(1, "0001_injected.up.sql")
        broken = """
        CREATE TABLE partial_table (value TEXT);
        INSERT INTO table_that_does_not_exist (value) VALUES ('failure');
        """
        fixed = "CREATE TABLE recovered_table (value TEXT);"

        with mock.patch(
            "quantum_entanglement.migrations.migration_text",
            return_value=broken,
        ):
            with self.assertRaises(sqlite3.OperationalError):
                apply_sqlite_migrations(
                    self.connection,
                    migrations=(migration,),
                    clock=lambda: NOW,
                )

        self.assertFalse(self.connection.in_transaction)
        self.assertFalse(self.table_exists("partial_table"))
        self.assertEqual(self.ledger_count(), 0)

        with mock.patch(
            "quantum_entanglement.migrations.migration_text",
            return_value=fixed,
        ):
            self.assertEqual(
                apply_sqlite_migrations(
                    self.connection,
                    migrations=(migration,),
                    clock=lambda: NOW,
                ),
                1,
            )
        self.assertTrue(self.table_exists("recovered_table"))

    def test_clock_base_exception_releases_lock_without_schema_or_ledger(self):
        migration = Migration(1, "0001_clock_failure.up.sql")

        def fail_clock():
            raise KeyboardInterrupt("injected clock interrupt")

        with mock.patch(
            "quantum_entanglement.migrations.migration_text",
            return_value="CREATE TABLE clock_partial (value TEXT);",
        ):
            with self.assertRaisesRegex(KeyboardInterrupt, "injected clock interrupt"):
                apply_sqlite_migrations(
                    self.connection,
                    migrations=(migration,),
                    clock=fail_clock,
                )

            self.assertFalse(self.connection.in_transaction)
            self.assertFalse(self.table_exists("clock_partial"))
            self.assertEqual(self.ledger_count(), 0)
            self.assertEqual(
                apply_sqlite_migrations(
                    self.connection,
                    migrations=(migration,),
                    clock=lambda: NOW,
                ),
                1,
            )

    def test_process_guard_rejects_after_clock_without_inspecting_inherited_connection(self):
        migration = Migration(1, "0001_process_drift.up.sql")
        drifted = False
        guarded = ProcessGuardedConnection(self.connection, lambda: drifted)

        def process_guard():
            if drifted:
                raise MigrationProcessMismatchSignal("process drifted")

        def fork_seam_clock():
            nonlocal drifted
            drifted = True
            return NOW

        with mock.patch(
            "quantum_entanglement.migrations.migration_text",
            return_value="CREATE TABLE process_drift_partial (value TEXT);",
        ):
            with self.assertRaisesRegex(MigrationProcessMismatchSignal, "process drifted"):
                apply_sqlite_migrations(
                    guarded,
                    migrations=(migration,),
                    clock=fork_seam_clock,
                    _process_guard=process_guard,
                )

        drifted = False
        self.assertTrue(self.connection.in_transaction)
        self.connection.execute("ROLLBACK")
        self.assertFalse(self.table_exists("process_drift_partial"))
        self.assertEqual(self.ledger_count(), 0)

    def test_originating_control_wins_when_process_guard_denies_migration_cleanup(self):
        migration = Migration(1, "0001_process_control.up.sql")
        drifted = False
        guarded = ProcessGuardedConnection(self.connection, lambda: drifted)

        def process_guard():
            if drifted:
                raise MigrationProcessMismatchSignal("process drifted")

        controls = (
            KeyboardInterrupt("originating migration keyboard interrupt"),
            SystemExit(61),
            GeneratorExit("originating migration generator exit"),
            CancelledError("originating migration cancellation"),
        )
        with mock.patch(
            "quantum_entanglement.migrations.migration_text",
            return_value="CREATE TABLE process_control_partial (value TEXT);",
        ):
            for originating in controls:
                with self.subTest(control=type(originating).__name__):

                    def control_clock(originating=originating):
                        nonlocal drifted
                        drifted = True
                        raise originating

                    with self.assertRaises(type(originating)) as caught:
                        apply_sqlite_migrations(
                            guarded,
                            migrations=(migration,),
                            clock=control_clock,
                            _process_guard=process_guard,
                        )
                    self.assertIs(caught.exception, originating)
                    drifted = False
                    self.assertTrue(self.connection.in_transaction)
                    self.connection.execute("ROLLBACK")
                    self.assertFalse(self.connection.in_transaction)

            class RollbackInterruptingConnection:
                @property
                def in_transaction(connection_self):
                    return self.connection.in_transaction

                def create_function(connection_self, *args, **kwargs):
                    return self.connection.create_function(*args, **kwargs)

                def execute(connection_self, statement, parameters=()):
                    if statement == "ROLLBACK":
                        raise SystemExit(62)
                    return self.connection.execute(statement, parameters)

            rollback_origin = KeyboardInterrupt("originating control before rollback")

            def rollback_control_clock():
                raise rollback_origin

            with self.assertRaises(KeyboardInterrupt) as caught_rollback:
                apply_sqlite_migrations(
                    RollbackInterruptingConnection(),
                    migrations=(migration,),
                    clock=rollback_control_clock,
                )
            self.assertIs(caught_rollback.exception, rollback_origin)
            self.assertTrue(self.connection.in_transaction)
            self.connection.execute("ROLLBACK")

        self.assertFalse(self.table_exists("process_control_partial"))
        self.assertEqual(self.ledger_count(), 0)

    def test_commit_failure_rolls_back_body_and_ledger_then_retries(self):
        migration = Migration(1, "0001_commit_failure.up.sql")
        wrapped = CommitFailingConnection(self.connection)

        with mock.patch(
            "quantum_entanglement.migrations.migration_text",
            return_value="CREATE TABLE commit_partial (value TEXT);",
        ):
            with self.assertRaisesRegex(sqlite3.OperationalError, "commit failure"):
                apply_sqlite_migrations(
                    wrapped,
                    migrations=(migration,),
                    clock=lambda: NOW,
                )

            self.assertFalse(self.connection.in_transaction)
            self.assertFalse(self.table_exists("commit_partial"))
            self.assertEqual(self.ledger_count(), 0)
            self.assertEqual(
                apply_sqlite_migrations(
                    wrapped,
                    migrations=(migration,),
                    clock=lambda: NOW,
                ),
                1,
            )
            self.assertTrue(self.table_exists("commit_partial"))
            self.assertEqual(current_schema_version(self.connection), 1)

    def test_authorizer_denied_commit_rolls_back_and_releases_write_lock(self):
        migration = Migration(1, "0001_denied_commit.up.sql")

        def deny_commit(action_code, operation, _table, _database, _trigger):
            if action_code == sqlite3.SQLITE_TRANSACTION and str(operation).upper() == "COMMIT":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        self.connection.set_authorizer(deny_commit)
        try:
            with mock.patch(
                "quantum_entanglement.migrations.migration_text",
                return_value="CREATE TABLE denied_commit (value TEXT);",
            ):
                with self.assertRaisesRegex(sqlite3.DatabaseError, "not authorized"):
                    apply_sqlite_migrations(
                        self.connection,
                        migrations=(migration,),
                        clock=lambda: NOW,
                    )
        finally:
            # Python 3.9 does not reliably treat None as "disable authorizer".
            self.connection.set_authorizer(lambda *_args: sqlite3.SQLITE_OK)

        self.assertFalse(self.connection.in_transaction)
        self.assertFalse(self.table_exists("denied_commit"))
        self.assertEqual(self.ledger_count(), 0)
        contender = sqlite3.connect(self.path, isolation_level=None, timeout=0.1)
        try:
            contender.execute("BEGIN IMMEDIATE")
            self.assertTrue(contender.in_transaction)
            contender.execute("ROLLBACK")
        finally:
            contender.close()


if __name__ == "__main__":
    unittest.main()
