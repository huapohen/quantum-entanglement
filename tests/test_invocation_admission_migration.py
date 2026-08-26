import hashlib
import re
import sqlite3
import unittest
from collections.abc import Sequence

from quantum_entanglement.migrations import (
    MIGRATIONS,
    MigrationDriftError,
    MigrationVersionError,
    apply_sqlite_migrations,
    current_schema_version,
    migration_text,
    validate_sqlite_schema,
)

NOW = "2026-08-26T00:00:00Z"
RECEIPT_FORMAT = "qe.invocation-admission-receipt/1"

ADMISSION_COLUMNS = (
    "invocation_id",
    "receipt_format",
    "session_id",
    "task_id",
    "stream_id",
    "job_idempotency_key",
    "original_version",
    "event_count",
    "event_ids_json",
    "first_sequence",
    "last_sequence",
    "first_global_position",
    "last_global_position",
    "event_manifest_sha256",
    "job_binding_sha256",
    "admitted_at",
)


def canonical_schema_sql(sql: str) -> str:
    without_idempotency = re.sub(
        r"\bIF\s+NOT\s+EXISTS\b",
        "",
        sql,
        flags=re.IGNORECASE,
    )
    return " ".join(without_idempotency.strip().rstrip(";").split())


def split_sql(script: str) -> tuple[str, ...]:
    statements: list[str] = []
    buffer = ""
    for character in script:
        buffer += character
        if character == ";" and sqlite3.complete_statement(buffer):
            statements.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        raise AssertionError("packaged migration ends with incomplete SQL")
    return tuple(statements)


class MigrationFourCommitFailingConnection:
    """Fail only the COMMIT that can see migration 4's uncommitted ledger row."""

    def __init__(self, delegate: sqlite3.Connection) -> None:
        self.delegate = delegate
        self.failed = False

    @property
    def in_transaction(self) -> bool:
        return self.delegate.in_transaction

    def create_function(self, *args: object, **kwargs: object) -> None:
        self.delegate.create_function(*args, **kwargs)

    def execute(
        self,
        statement: str,
        parameters: Sequence[object] = (),
    ) -> sqlite3.Cursor:
        if statement.strip().upper() == "COMMIT" and not self.failed:
            migration_four = self.delegate.execute(
                "SELECT 1 FROM main.qe_schema_migrations WHERE version = 4"
            ).fetchone()
            if migration_four is not None:
                self.failed = True
                raise sqlite3.OperationalError("injected migration 4 commit failure")
        return self.delegate.execute(statement, tuple(parameters))


class MigrationFourRollbackAckFailingConnection:
    """Rollback the v4 body, then simulate loss of its acknowledgement."""

    def __init__(self, delegate: sqlite3.Connection) -> None:
        self.delegate = delegate
        self.body_failed = False
        self.rollback_ack_failed = False

    @property
    def in_transaction(self) -> bool:
        return self.delegate.in_transaction

    def create_function(self, *args: object, **kwargs: object) -> None:
        self.delegate.create_function(*args, **kwargs)

    def execute(
        self,
        statement: str,
        parameters: Sequence[object] = (),
    ) -> sqlite3.Cursor:
        normalized = " ".join(statement.strip().rstrip(";").split()).upper()
        if (
            normalized.startswith("CREATE INDEX IF NOT EXISTS IDX_INVOCATION_ADMISSIONS_STREAM")
            and not self.body_failed
        ):
            self.body_failed = True
            raise sqlite3.OperationalError("injected migration 4 body failure")
        if normalized == "ROLLBACK" and self.body_failed and not self.rollback_ack_failed:
            self.delegate.execute("ROLLBACK")
            self.rollback_ack_failed = True
            raise sqlite3.OperationalError("injected rollback acknowledgement failure")
        return self.delegate.execute(statement, tuple(parameters))


class InvocationAdmissionMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        # Migrations 3 and 4 attach foreign keys to event-store-owned tables.
        # These minimal parent shapes preserve the same referenced candidate keys.
        self.connection.executescript(
            """
            CREATE TABLE events (
                global_position INTEGER PRIMARY KEY,
                stream_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                event_id TEXT NOT NULL UNIQUE,
                UNIQUE(stream_id, sequence)
            );
            CREATE TABLE outbox (
                message_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                lease_token TEXT
            );
            """
        )

    def tearDown(self) -> None:
        self.connection.close()

    def table_exists(self, name: str) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM main.sqlite_master WHERE type = 'table' AND name = ?",
                (name,),
            ).fetchone()
            is not None
        )

    def index_exists(self, name: str) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM main.sqlite_master WHERE type = 'index' AND name = ?",
                (name,),
            ).fetchone()
            is not None
        )

    def ledger_versions(self) -> list[int]:
        return [
            int(row["version"])
            for row in self.connection.execute(
                "SELECT version FROM main.qe_schema_migrations ORDER BY version"
            ).fetchall()
        ]

    def apply_v3(self) -> int:
        return apply_sqlite_migrations(
            self.connection,
            target_versions=tuple(item.version for item in MIGRATIONS[:3]),
            clock=lambda: NOW,
        )

    def apply_v4(self) -> int:
        return apply_sqlite_migrations(self.connection, clock=lambda: NOW)

    def insert_job(self) -> None:
        self.connection.execute(
            """
            INSERT INTO invocation_jobs (
                invocation_id, session_id, plan_id, task_id, agent_id,
                idempotency_key, payload_digest, status, max_attempts,
                available_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "invocation-1",
                "session-1",
                "plan-1",
                "task-1",
                "agent-1",
                "job-key-1",
                "payload-digest-1",
                "queued",
                3,
                NOW,
                NOW,
                NOW,
            ),
        )

    def valid_receipt(self) -> dict[str, object]:
        return {
            "invocation_id": "invocation-1",
            "receipt_format": RECEIPT_FORMAT,
            "session_id": "session-1",
            "task_id": "task-1",
            "stream_id": "session:session-1",
            "job_idempotency_key": "job-key-1",
            "original_version": 0,
            "event_count": 2,
            "event_ids_json": '["event-1","event-2"]',
            "first_sequence": 1,
            "last_sequence": 2,
            "first_global_position": 1,
            "last_global_position": 2,
            "event_manifest_sha256": "a" * 64,
            "job_binding_sha256": "b" * 64,
            "admitted_at": NOW,
        }

    def insert_receipt(self, values: dict[str, object]) -> None:
        placeholders = ", ".join("?" for _column in ADMISSION_COLUMNS)
        self.connection.execute(
            f"INSERT INTO invocation_admissions ({', '.join(ADMISSION_COLUMNS)}) "
            f"VALUES ({placeholders})",
            tuple(values[column] for column in ADMISSION_COLUMNS),
        )

    def test_populated_v3_database_upgrades_to_v4_without_data_loss(self) -> None:
        self.assertEqual(self.apply_v3(), 3)
        self.insert_job()
        blob = b"populated-v3"
        blob_digest = "sha256:" + hashlib.sha256(blob).hexdigest()
        ambiguity_digest = "c" * 64
        self.connection.execute(
            """
            INSERT INTO artifact_blobs(digest, content, byte_size, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (blob_digest, blob, len(blob), NOW),
        )
        self.connection.execute(
            "INSERT INTO outbox(message_id, status, lease_token) VALUES (?, ?, ?)",
            ("message-1", "pending", None),
        )
        self.connection.execute(
            """
            INSERT INTO outbox_ambiguities (
                message_id, lease_token_digest, reason_code, attempt_count, marked_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            ("message-1", ambiguity_digest, "callback_timeout", 1, NOW),
        )

        self.assertEqual(self.apply_v4(), 4)

        self.assertEqual(self.ledger_versions(), [1, 2, 3, 4])
        migration_four = MIGRATIONS[3]
        ledger_row = self.connection.execute(
            """
            SELECT filename, sha256, applied_at
            FROM qe_schema_migrations WHERE version = 4
            """
        ).fetchone()
        self.assertIsNotNone(ledger_row)
        self.assertEqual(ledger_row["filename"], migration_four.filename)
        self.assertEqual(
            ledger_row["sha256"],
            hashlib.sha256(migration_text(migration_four.filename).encode("utf-8")).hexdigest(),
        )
        self.assertEqual(ledger_row["applied_at"], NOW)
        self.assertEqual(
            tuple(
                self.connection.execute(
                    "SELECT status, max_attempts FROM invocation_jobs WHERE invocation_id = ?",
                    ("invocation-1",),
                ).fetchone()
            ),
            ("queued", 3),
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT content FROM artifact_blobs WHERE digest = ?", (blob_digest,)
            ).fetchone()["content"],
            blob,
        )
        self.assertEqual(
            tuple(
                self.connection.execute(
                    """
                    SELECT lease_token_digest, reason_code
                    FROM outbox_ambiguities WHERE message_id = ?
                    """,
                    ("message-1",),
                ).fetchone()
            ),
            (ambiguity_digest, "callback_timeout"),
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM invocation_admissions").fetchone()[0],
            0,
        )

    def test_v4_table_and_indexes_match_the_packaged_schema_exactly(self) -> None:
        self.assertEqual(self.apply_v4(), 4)
        statements = split_sql(migration_text(MIGRATIONS[3].filename))
        self.assertEqual(len(statements), 2)

        table_sql = self.connection.execute(
            "SELECT sql FROM main.sqlite_master WHERE type = 'table' AND name = ?",
            ("invocation_admissions",),
        ).fetchone()["sql"]
        index_sql = self.connection.execute(
            "SELECT sql FROM main.sqlite_master WHERE type = 'index' AND name = ?",
            ("idx_invocation_admissions_stream",),
        ).fetchone()["sql"]
        self.assertEqual(canonical_schema_sql(table_sql), canonical_schema_sql(statements[0]))
        self.assertEqual(canonical_schema_sql(index_sql), canonical_schema_sql(statements[1]))

        columns = [
            tuple(row)
            for row in self.connection.execute(
                "PRAGMA main.table_xinfo('invocation_admissions')"
            ).fetchall()
        ]
        self.assertEqual(
            columns,
            [
                (0, "invocation_id", "TEXT", 0, None, 1, 0),
                (1, "receipt_format", "TEXT", 1, None, 0, 0),
                (2, "session_id", "TEXT", 1, None, 0, 0),
                (3, "task_id", "TEXT", 1, None, 0, 0),
                (4, "stream_id", "TEXT", 1, None, 0, 0),
                (5, "job_idempotency_key", "TEXT", 1, None, 0, 0),
                (6, "original_version", "INTEGER", 1, None, 0, 0),
                (7, "event_count", "INTEGER", 1, None, 0, 0),
                (8, "event_ids_json", "TEXT", 1, None, 0, 0),
                (9, "first_sequence", "INTEGER", 1, None, 0, 0),
                (10, "last_sequence", "INTEGER", 1, None, 0, 0),
                (11, "first_global_position", "INTEGER", 1, None, 0, 0),
                (12, "last_global_position", "INTEGER", 1, None, 0, 0),
                (13, "event_manifest_sha256", "TEXT", 1, None, 0, 0),
                (14, "job_binding_sha256", "TEXT", 1, None, 0, 0),
                (15, "admitted_at", "TEXT", 1, None, 0, 0),
            ],
        )

        index_metadata = {
            row["name"]: (int(row["unique"]), row["origin"], int(row["partial"]))
            for row in self.connection.execute(
                "PRAGMA main.index_list('invocation_admissions')"
            ).fetchall()
        }
        self.assertEqual(
            index_metadata,
            {
                "idx_invocation_admissions_stream": (0, "c", 0),
                "sqlite_autoindex_invocation_admissions_1": (1, "pk", 0),
                "sqlite_autoindex_invocation_admissions_2": (1, "u", 0),
                "sqlite_autoindex_invocation_admissions_3": (1, "u", 0),
            },
        )
        index_columns = {
            name: tuple(
                row["name"]
                for row in self.connection.execute(f"PRAGMA main.index_info('{name}')").fetchall()
            )
            for name in index_metadata
        }
        self.assertEqual(
            index_columns,
            {
                "idx_invocation_admissions_stream": ("stream_id", "first_sequence"),
                "sqlite_autoindex_invocation_admissions_1": ("invocation_id",),
                "sqlite_autoindex_invocation_admissions_2": ("session_id", "task_id"),
                "sqlite_autoindex_invocation_admissions_3": (
                    "session_id",
                    "job_idempotency_key",
                ),
            },
        )
        self.assertEqual(validate_sqlite_schema(self.connection), 4)

    def test_prebuilt_weakened_table_never_receives_v4_ledger_row(self) -> None:
        self.assertEqual(self.apply_v3(), 3)
        self.connection.execute(
            """
            CREATE TABLE invocation_admissions (
                invocation_id TEXT PRIMARY KEY,
                stream_id TEXT NOT NULL,
                first_sequence INTEGER NOT NULL
            )
            """
        )

        with self.assertRaisesRegex(
            MigrationDriftError,
            "invocation_admissions.*differs",
        ):
            self.apply_v4()

        self.assertFalse(self.connection.in_transaction)
        self.assertEqual(self.ledger_versions(), [1, 2, 3])
        self.assertTrue(self.table_exists("invocation_admissions"))
        self.assertFalse(self.index_exists("idx_invocation_admissions_stream"))
        self.assertEqual(
            [
                row["name"]
                for row in self.connection.execute(
                    "PRAGMA main.table_info('invocation_admissions')"
                ).fetchall()
            ],
            ["invocation_id", "stream_id", "first_sequence"],
        )

    def test_down_migration_removes_v4_ledger_and_is_replayable(self) -> None:
        self.assertEqual(self.apply_v4(), 4)

        self.connection.executescript(
            migration_text(MIGRATIONS[3].filename.replace(".up.sql", ".down.sql"))
        )

        self.assertFalse(self.table_exists("invocation_admissions"))
        self.assertFalse(self.index_exists("idx_invocation_admissions_stream"))
        self.assertEqual(self.ledger_versions(), [1, 2, 3])
        self.assertEqual(current_schema_version(self.connection), 3)
        self.assertEqual(validate_sqlite_schema(self.connection), 3)
        self.assertEqual(self.apply_v4(), 4)
        self.assertTrue(self.table_exists("invocation_admissions"))

    def test_v3_registry_rejects_a_v4_database_without_mutation(self) -> None:
        self.assertEqual(self.apply_v4(), 4)
        v3_registry = tuple(MIGRATIONS[:3])

        with self.assertRaisesRegex(
            MigrationVersionError,
            "database schema version 4 is newer than this binary",
        ):
            apply_sqlite_migrations(
                self.connection,
                migrations=v3_registry,
                clock=lambda: NOW,
            )

        self.assertFalse(self.connection.in_transaction)
        self.assertEqual(self.ledger_versions(), [1, 2, 3, 4])
        self.assertTrue(self.table_exists("invocation_admissions"))
        self.assertEqual(validate_sqlite_schema(self.connection), 4)

    def test_foreign_keys_checks_and_digest_constraints_are_enforced(self) -> None:
        self.assertEqual(self.apply_v4(), 4)
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.assertEqual(self.connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.insert_job()
        self.connection.executemany(
            """
            INSERT INTO events(global_position, stream_id, sequence, event_id)
            VALUES (?, ?, ?, ?)
            """,
            (
                (1, "session:session-1", 1, "event-1"),
                (2, "session:session-1", 2, "event-2"),
            ),
        )

        foreign_keys = {
            (
                row["table"],
                row["from"],
                row["to"],
                row["on_update"],
                row["on_delete"],
                row["match"],
            )
            for row in self.connection.execute(
                "PRAGMA main.foreign_key_list('invocation_admissions')"
            ).fetchall()
        }
        self.assertEqual(
            foreign_keys,
            {
                (
                    "invocation_jobs",
                    "invocation_id",
                    "invocation_id",
                    "NO ACTION",
                    "RESTRICT",
                    "NONE",
                ),
                ("events", "stream_id", "stream_id", "NO ACTION", "RESTRICT", "NONE"),
                ("events", "first_sequence", "sequence", "NO ACTION", "RESTRICT", "NONE"),
                ("events", "last_sequence", "sequence", "NO ACTION", "RESTRICT", "NONE"),
                (
                    "events",
                    "first_global_position",
                    "global_position",
                    "NO ACTION",
                    "RESTRICT",
                    "NONE",
                ),
                (
                    "events",
                    "last_global_position",
                    "global_position",
                    "NO ACTION",
                    "RESTRICT",
                    "NONE",
                ),
            },
        )

        invalid_checks: tuple[tuple[str, dict[str, object]], ...] = (
            ("receipt format", {"receipt_format": "qe.invocation-admission-receipt/0"}),
            ("stream identity", {"stream_id": "session:other"}),
            ("negative original version", {"original_version": -1}),
            ("zero event count", {"event_count": 0}),
            ("first sequence", {"first_sequence": 2}),
            ("last sequence", {"last_sequence": 3}),
            ("global range", {"last_global_position": 3}),
            ("short event digest", {"event_manifest_sha256": "a" * 63}),
            ("uppercase event digest", {"event_manifest_sha256": "A" * 64}),
            ("non-hex event digest", {"event_manifest_sha256": "g" * 64}),
            ("short job digest", {"job_binding_sha256": "b" * 63}),
            ("uppercase job digest", {"job_binding_sha256": "B" * 64}),
            ("non-hex job digest", {"job_binding_sha256": "z" * 64}),
        )
        for label, changes in invalid_checks:
            with self.subTest(constraint=label):
                values = self.valid_receipt()
                values.update(changes)
                with self.assertRaises(sqlite3.IntegrityError):
                    self.insert_receipt(values)

        invalid_foreign_keys: tuple[tuple[str, dict[str, object]], ...] = (
            ("missing job", {"invocation_id": "missing-invocation"}),
            (
                "missing event sequence range",
                {
                    "original_version": 2,
                    "first_sequence": 3,
                    "last_sequence": 4,
                },
            ),
            (
                "missing event global range",
                {
                    "first_global_position": 99,
                    "last_global_position": 100,
                },
            ),
        )
        for label, changes in invalid_foreign_keys:
            with self.subTest(foreign_key=label):
                values = self.valid_receipt()
                values.update(changes)
                with self.assertRaises(sqlite3.IntegrityError):
                    self.insert_receipt(values)

        self.insert_receipt(self.valid_receipt())
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM invocation_admissions").fetchone()[0],
            1,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "DELETE FROM invocation_jobs WHERE invocation_id = ?", ("invocation-1",)
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("DELETE FROM events WHERE global_position = 1")
        self.assertEqual(
            self.connection.execute("PRAGMA main.foreign_key_check").fetchall(),
            [],
        )

    def test_v4_commit_failure_rolls_back_schema_and_ledger_then_retries(self) -> None:
        self.assertEqual(self.apply_v3(), 3)
        wrapped = MigrationFourCommitFailingConnection(self.connection)

        with self.assertRaisesRegex(
            sqlite3.OperationalError,
            "injected migration 4 commit failure",
        ):
            apply_sqlite_migrations(wrapped, clock=lambda: NOW)

        self.assertTrue(wrapped.failed)
        self.assertFalse(self.connection.in_transaction)
        self.assertEqual(self.ledger_versions(), [1, 2, 3])
        self.assertFalse(self.table_exists("invocation_admissions"))
        self.assertFalse(self.index_exists("idx_invocation_admissions_stream"))
        self.assertEqual(self.apply_v4(), 4)
        self.assertEqual(self.ledger_versions(), [1, 2, 3, 4])

    def test_v4_rollback_ack_failure_leaves_no_durable_half_state(self) -> None:
        self.assertEqual(self.apply_v3(), 3)
        wrapped = MigrationFourRollbackAckFailingConnection(self.connection)

        with self.assertRaisesRegex(
            sqlite3.OperationalError,
            "injected rollback acknowledgement failure",
        ):
            apply_sqlite_migrations(wrapped, clock=lambda: NOW)

        self.assertTrue(wrapped.body_failed)
        self.assertTrue(wrapped.rollback_ack_failed)
        self.assertFalse(self.connection.in_transaction)
        self.assertEqual(self.ledger_versions(), [1, 2, 3])
        self.assertFalse(self.table_exists("invocation_admissions"))
        self.assertFalse(self.index_exists("idx_invocation_admissions_stream"))
        self.assertEqual(self.apply_v4(), 4)
        self.assertEqual(self.ledger_versions(), [1, 2, 3, 4])


if __name__ == "__main__":
    unittest.main()
