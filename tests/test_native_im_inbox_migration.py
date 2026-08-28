import hashlib
import sqlite3
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path

from quantum_entanglement.backup import (
    create_sqlite_backup,
    default_manifest_path,
    restore_sqlite_backup,
)
from quantum_entanglement.migrations import (
    MIGRATIONS,
    apply_sqlite_migrations,
    current_schema_version,
    migration_text,
    validate_sqlite_schema,
)
from quantum_entanglement.store import SQLiteEventStore

TS0 = "2026-08-20T00:00:00.000000Z"
TS1 = "2026-08-21T00:00:00.000000Z"
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64
DIGEST_D = "d" * 64
DIGEST_E = "e" * 64
DIGEST_F = "f" * 64

NATIVE_IM_TABLES = (
    "native_im_auth_nonces",
    "native_im_inbox_events",
    "native_im_inbox_verifications",
    "native_im_inbound_reads",
    "native_im_inbound_read_events",
    "native_im_inbound_checkpoints",
)


class NativeIMInboxMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.database = self.root / "state.sqlite3"
        with SQLiteEventStore(str(self.database), clock=lambda: TS0):
            pass
        self.connection = sqlite3.connect(self.database, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")

    def tearDown(self) -> None:
        self.connection.close()
        self.tempdir.cleanup()

    def _downgrade_to_v4(self) -> None:
        self.connection.executescript(
            migration_text("0006_native_im_sandbox_provenance.down.sql")
        )
        self.connection.execute("DELETE FROM main.qe_schema_migrations WHERE version = 6")
        self.connection.executescript(migration_text("0005_native_im_inbox.down.sql"))
        self.connection.execute("DELETE FROM main.qe_schema_migrations WHERE version = 5")

    def _insert_event(self) -> None:
        self.connection.execute(
            """
            INSERT INTO native_im_inbox_events (
                tenant_id, workspace_id, provider, channel_id, event_id,
                event_digest, event_json, cursor, sequence_number,
                first_received_at, admitted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "tenant-1",
                "workspace-1",
                "native-im",
                "channel-1",
                "event-1",
                DIGEST_C,
                '{"kind":"message"}',
                "cursor-1",
                1,
                TS0,
                TS0,
            ),
        )

    def _insert_verification(self) -> None:
        self.connection.execute(
            """
            INSERT INTO native_im_inbox_verifications (
                tenant_id, workspace_id, provider, channel_id, verification_id,
                event_id, event_digest, envelope_digest, verifier_id,
                authentication_evidence_digest, tenant_mapping_revision,
                verified_at, traceparent, admitted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "tenant-1",
                "workspace-1",
                "native-im",
                "channel-1",
                "verification-1",
                "event-1",
                DIGEST_C,
                DIGEST_D,
                "hmac-sha256-v1",
                DIGEST_B,
                "mapping-revision-1",
                TS0,
                None,
                TS0,
            ),
        )

    def _insert_admitted_read(self) -> None:
        self.connection.execute(
            """
            INSERT INTO native_im_inbound_reads (
                tenant_id, workspace_id, provider, channel_id,
                read_request_id, read_request_digest, request_json,
                base_checkpoint_revision, after_cursor, after_sequence,
                request_snapshot_token, status, prepared_at, page_digest,
                response_snapshot_token, next_cursor, next_sequence,
                continuation_snapshot_token, has_more, envelope_count,
                event_manifest_sha256, capability_revision, capability_digest,
                admitted_checkpoint_revision, admitted_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?
            )
            """,
            (
                "tenant-1",
                "workspace-1",
                "native-im",
                "channel-1",
                "read-request-1",
                DIGEST_E,
                '{"limit":100}',
                0,
                None,
                None,
                None,
                "admitted",
                TS0,
                DIGEST_F,
                "snapshot-1",
                "cursor-1",
                1,
                None,
                0,
                1,
                DIGEST_A,
                "capability-revision-1",
                DIGEST_B,
                1,
                TS0,
            ),
        )

    def _seed_all_tables(self) -> None:
        self.connection.execute(
            """
            INSERT INTO native_im_auth_nonces (
                tenant_id, workspace_id, provider, channel_id, key_id,
                nonce_digest, signed_at, expires_at,
                authentication_evidence_digest, profile_revision,
                profile_digest, claimed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "tenant-1",
                "workspace-1",
                "native-im",
                "channel-1",
                "key-1",
                DIGEST_A,
                TS0,
                TS1,
                DIGEST_B,
                "profile-revision-1",
                DIGEST_C,
                TS0,
            ),
        )
        self._insert_event()
        self._insert_verification()
        self._insert_admitted_read()
        self.connection.execute(
            """
            INSERT INTO native_im_inbound_read_events (
                tenant_id, workspace_id, provider, channel_id,
                read_request_digest, ordinal, event_id, verification_id,
                envelope_digest
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "tenant-1",
                "workspace-1",
                "native-im",
                "channel-1",
                DIGEST_E,
                0,
                "event-1",
                "verification-1",
                DIGEST_D,
            ),
        )
        self.connection.execute(
            """
            INSERT INTO native_im_inbound_checkpoints (
                tenant_id, workspace_id, provider, channel_id, after_cursor,
                after_sequence, continuation_snapshot_token,
                checkpoint_revision, last_read_request_digest,
                last_page_digest, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "tenant-1",
                "workspace-1",
                "native-im",
                "channel-1",
                "cursor-1",
                1,
                None,
                1,
                DIGEST_E,
                DIGEST_F,
                TS0,
            ),
        )

    def test_v4_upgrades_to_v5_with_exact_tables_columns_indexes_and_foreign_keys(
        self,
    ) -> None:
        self._downgrade_to_v4()
        self.assertEqual(current_schema_version(self.connection), 4)
        self.assertEqual(
            {
                row[0]
                for row in self.connection.execute(
                    "SELECT name FROM sqlite_schema WHERE type = 'table'"
                )
            }.intersection(NATIVE_IM_TABLES),
            set(),
        )

        self.assertEqual(
            apply_sqlite_migrations(
                self.connection,
                target_versions=tuple(range(1, 6)),
                clock=lambda: TS0,
            ),
            5,
        )
        self.assertEqual(validate_sqlite_schema(self.connection), 5)
        migration = MIGRATIONS[4]
        ledger = self.connection.execute(
            """
            SELECT filename, sha256 FROM qe_schema_migrations WHERE version = 5
            """
        ).fetchone()
        self.assertEqual(ledger["filename"], migration.filename)
        self.assertEqual(
            ledger["sha256"],
            hashlib.sha256(migration_text(migration.filename).encode()).hexdigest(),
        )

        expected_columns = {
            "native_im_auth_nonces": (
                ("tenant_id", "TEXT", 1, 1),
                ("workspace_id", "TEXT", 1, 2),
                ("provider", "TEXT", 1, 3),
                ("channel_id", "TEXT", 1, 4),
                ("key_id", "TEXT", 1, 5),
                ("nonce_digest", "TEXT", 1, 6),
                ("signed_at", "TEXT", 1, 0),
                ("expires_at", "TEXT", 1, 0),
                ("authentication_evidence_digest", "TEXT", 1, 0),
                ("profile_revision", "TEXT", 1, 0),
                ("profile_digest", "TEXT", 1, 0),
                ("claimed_at", "TEXT", 1, 0),
            ),
            "native_im_inbox_events": (
                ("tenant_id", "TEXT", 1, 1),
                ("workspace_id", "TEXT", 1, 2),
                ("provider", "TEXT", 1, 3),
                ("channel_id", "TEXT", 1, 4),
                ("event_id", "TEXT", 1, 5),
                ("event_digest", "TEXT", 1, 0),
                ("event_json", "TEXT", 1, 0),
                ("cursor", "TEXT", 1, 0),
                ("sequence_number", "INTEGER", 1, 0),
                ("first_received_at", "TEXT", 1, 0),
                ("admitted_at", "TEXT", 1, 0),
            ),
            "native_im_inbox_verifications": (
                ("tenant_id", "TEXT", 1, 1),
                ("workspace_id", "TEXT", 1, 2),
                ("provider", "TEXT", 1, 3),
                ("channel_id", "TEXT", 1, 4),
                ("verification_id", "TEXT", 1, 5),
                ("event_id", "TEXT", 1, 0),
                ("event_digest", "TEXT", 1, 0),
                ("envelope_digest", "TEXT", 1, 0),
                ("verifier_id", "TEXT", 1, 0),
                ("authentication_evidence_digest", "TEXT", 1, 0),
                ("tenant_mapping_revision", "TEXT", 1, 0),
                ("verified_at", "TEXT", 1, 0),
                ("traceparent", "TEXT", 0, 0),
                ("admitted_at", "TEXT", 1, 0),
            ),
            "native_im_inbound_reads": (
                ("tenant_id", "TEXT", 1, 1),
                ("workspace_id", "TEXT", 1, 2),
                ("provider", "TEXT", 1, 3),
                ("channel_id", "TEXT", 1, 4),
                ("read_request_id", "TEXT", 1, 0),
                ("read_request_digest", "TEXT", 1, 5),
                ("request_json", "TEXT", 1, 0),
                ("base_checkpoint_revision", "INTEGER", 1, 0),
                ("after_cursor", "TEXT", 0, 0),
                ("after_sequence", "INTEGER", 0, 0),
                ("request_snapshot_token", "TEXT", 0, 0),
                ("status", "TEXT", 1, 0),
                ("prepared_at", "TEXT", 1, 0),
                ("page_digest", "TEXT", 0, 0),
                ("response_snapshot_token", "TEXT", 0, 0),
                ("next_cursor", "TEXT", 0, 0),
                ("next_sequence", "INTEGER", 0, 0),
                ("continuation_snapshot_token", "TEXT", 0, 0),
                ("has_more", "INTEGER", 0, 0),
                ("envelope_count", "INTEGER", 0, 0),
                ("event_manifest_sha256", "TEXT", 0, 0),
                ("capability_revision", "TEXT", 0, 0),
                ("capability_digest", "TEXT", 0, 0),
                ("admitted_checkpoint_revision", "INTEGER", 0, 0),
                ("admitted_at", "TEXT", 0, 0),
            ),
            "native_im_inbound_read_events": (
                ("tenant_id", "TEXT", 1, 1),
                ("workspace_id", "TEXT", 1, 2),
                ("provider", "TEXT", 1, 3),
                ("channel_id", "TEXT", 1, 4),
                ("read_request_digest", "TEXT", 1, 5),
                ("ordinal", "INTEGER", 1, 6),
                ("event_id", "TEXT", 1, 0),
                ("verification_id", "TEXT", 1, 0),
                ("envelope_digest", "TEXT", 1, 0),
            ),
            "native_im_inbound_checkpoints": (
                ("tenant_id", "TEXT", 1, 1),
                ("workspace_id", "TEXT", 1, 2),
                ("provider", "TEXT", 1, 3),
                ("channel_id", "TEXT", 1, 4),
                ("after_cursor", "TEXT", 0, 0),
                ("after_sequence", "INTEGER", 0, 0),
                ("continuation_snapshot_token", "TEXT", 0, 0),
                ("checkpoint_revision", "INTEGER", 1, 0),
                ("last_read_request_digest", "TEXT", 1, 0),
                ("last_page_digest", "TEXT", 1, 0),
                ("updated_at", "TEXT", 1, 0),
            ),
        }
        for table, expected in expected_columns.items():
            with self.subTest(table=table):
                rows = self.connection.execute(f"PRAGMA table_info('{table}')").fetchall()
                self.assertEqual(
                    tuple((row["name"], row["type"], row["notnull"], row["pk"]) for row in rows),
                    expected,
                )
                self.assertTrue(all(row["dflt_value"] is None for row in rows))

        explicit_indexes = {
            "idx_native_im_auth_nonces_expiry": (
                "native_im_auth_nonces",
                0,
                ("tenant_id", "workspace_id", "provider", "channel_id", "expires_at"),
                None,
            ),
            "idx_native_im_inbound_reads_one_prepared": (
                "native_im_inbound_reads",
                1,
                ("tenant_id", "workspace_id", "provider", "channel_id"),
                "WHERE status = 'prepared'",
            ),
            "idx_native_im_inbound_reads_checkpoint_revision": (
                "native_im_inbound_reads",
                1,
                (
                    "tenant_id",
                    "workspace_id",
                    "provider",
                    "channel_id",
                    "admitted_checkpoint_revision",
                ),
                "WHERE status = 'admitted'",
            ),
        }
        actual_explicit = {}
        for row in self.connection.execute(
            """
            SELECT name, tbl_name, sql FROM sqlite_schema
            WHERE type = 'index' AND name LIKE 'idx_native_im_%'
            """
        ):
            normalized_sql = " ".join(row["sql"].split())
            index_columns = tuple(
                item["name"]
                for item in self.connection.execute(f"PRAGMA index_info('{row['name']}')")
            )
            is_unique = self.connection.execute(
                f"PRAGMA index_list('{row['tbl_name']}')"
            ).fetchall()
            uniqueness = next(item["unique"] for item in is_unique if item["name"] == row["name"])
            actual_explicit[row["name"]] = (
                row["tbl_name"],
                uniqueness,
                index_columns,
                "WHERE " + normalized_sql.split(" WHERE ", 1)[1]
                if " WHERE " in normalized_sql
                else None,
            )
        self.assertEqual(actual_explicit, explicit_indexes)

        expected_foreign_keys = {
            "native_im_inbox_verifications": {
                (
                    "native_im_inbox_events",
                    (
                        ("tenant_id", "tenant_id"),
                        ("workspace_id", "workspace_id"),
                        ("provider", "provider"),
                        ("channel_id", "channel_id"),
                        ("event_id", "event_id"),
                        ("event_digest", "event_digest"),
                    ),
                    "RESTRICT",
                    "RESTRICT",
                )
            },
            "native_im_inbound_read_events": {
                (
                    "native_im_inbound_reads",
                    (
                        ("tenant_id", "tenant_id"),
                        ("workspace_id", "workspace_id"),
                        ("provider", "provider"),
                        ("channel_id", "channel_id"),
                        ("read_request_digest", "read_request_digest"),
                    ),
                    "RESTRICT",
                    "RESTRICT",
                ),
                (
                    "native_im_inbox_verifications",
                    (
                        ("tenant_id", "tenant_id"),
                        ("workspace_id", "workspace_id"),
                        ("provider", "provider"),
                        ("channel_id", "channel_id"),
                        ("verification_id", "verification_id"),
                        ("envelope_digest", "envelope_digest"),
                    ),
                    "RESTRICT",
                    "RESTRICT",
                ),
            },
            "native_im_inbound_checkpoints": {
                (
                    "native_im_inbound_reads",
                    (
                        ("tenant_id", "tenant_id"),
                        ("workspace_id", "workspace_id"),
                        ("provider", "provider"),
                        ("channel_id", "channel_id"),
                        ("last_read_request_digest", "read_request_digest"),
                        ("last_page_digest", "page_digest"),
                    ),
                    "RESTRICT",
                    "RESTRICT",
                )
            },
        }
        for table in NATIVE_IM_TABLES:
            grouped = defaultdict(list)
            rows = self.connection.execute(f"PRAGMA foreign_key_list('{table}')").fetchall()
            for row in rows:
                grouped[row["id"]].append(row)
            actual = {
                (
                    group[0]["table"],
                    tuple(
                        (item["from"], item["to"])
                        for item in sorted(group, key=lambda item: item["seq"])
                    ),
                    group[0]["on_update"],
                    group[0]["on_delete"],
                )
                for group in grouped.values()
            }
            with self.subTest(foreign_keys=table):
                self.assertEqual(actual, expected_foreign_keys.get(table, set()))

    def test_digest_sequence_and_verification_bindings_fail_closed(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """
                INSERT INTO native_im_auth_nonces (
                    tenant_id, workspace_id, provider, channel_id, key_id,
                    nonce_digest, signed_at, expires_at,
                    authentication_evidence_digest, profile_revision,
                    profile_digest, claimed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "tenant-1",
                    "workspace-1",
                    "native-im",
                    "channel-1",
                    "key-1",
                    "A" * 64,
                    TS0,
                    TS1,
                    DIGEST_B,
                    "revision-1",
                    DIGEST_C,
                    TS0,
                ),
            )

        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """
                INSERT INTO native_im_inbox_events (
                    tenant_id, workspace_id, provider, channel_id, event_id,
                    event_digest, event_json, cursor, sequence_number,
                    first_received_at, admitted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "tenant-1",
                    "workspace-1",
                    "native-im",
                    "channel-1",
                    "bad-event",
                    DIGEST_C[:-1],
                    "{}",
                    "cursor-bad",
                    1,
                    TS0,
                    TS0,
                ),
            )
        self._insert_event()
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """
                INSERT INTO native_im_inbox_events (
                    tenant_id, workspace_id, provider, channel_id, event_id,
                    event_digest, event_json, cursor, sequence_number,
                    first_received_at, admitted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "tenant-1",
                    "workspace-1",
                    "native-im",
                    "channel-1",
                    "event-2",
                    DIGEST_D,
                    "{}",
                    "cursor-2",
                    1,
                    TS0,
                    TS0,
                ),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """
                INSERT INTO native_im_inbox_events (
                    tenant_id, workspace_id, provider, channel_id, event_id,
                    event_digest, event_json, cursor, sequence_number,
                    first_received_at, admitted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "tenant-1",
                    "workspace-1",
                    "native-im",
                    "channel-1",
                    "event-3",
                    DIGEST_E,
                    "{}",
                    "cursor-3",
                    -1,
                    TS0,
                    TS0,
                ),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """
                INSERT INTO native_im_inbox_verifications (
                    tenant_id, workspace_id, provider, channel_id, verification_id,
                    event_id, event_digest, envelope_digest, verifier_id,
                    authentication_evidence_digest, tenant_mapping_revision,
                    verified_at, traceparent, admitted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "tenant-1",
                    "workspace-1",
                    "native-im",
                    "channel-1",
                    "verification-bad",
                    "event-1",
                    DIGEST_D,
                    DIGEST_E,
                    "verifier-1",
                    DIGEST_B,
                    "mapping-1",
                    TS0,
                    None,
                    TS0,
                ),
            )

    def test_read_request_state_and_checkpoint_constraints_fail_closed(self) -> None:
        prepared = (
            "tenant-1",
            "workspace-1",
            "native-im",
            "channel-1",
            "request-prepared-1",
            DIGEST_A,
            "{}",
            0,
            None,
            None,
            None,
            "prepared",
            TS0,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        )
        insert_read = """
            INSERT INTO native_im_inbound_reads (
                tenant_id, workspace_id, provider, channel_id,
                read_request_id, read_request_digest, request_json,
                base_checkpoint_revision, after_cursor, after_sequence,
                request_snapshot_token, status, prepared_at, page_digest,
                response_snapshot_token, next_cursor, next_sequence,
                continuation_snapshot_token, has_more, envelope_count,
                event_manifest_sha256, capability_revision, capability_digest,
                admitted_checkpoint_revision, admitted_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?
            )
        """
        self.connection.execute(insert_read, prepared)
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                insert_read,
                prepared[:4] + ("request-prepared-2", DIGEST_B) + prepared[6:],
            )

        invalid_cursor_pair = prepared[:4] + (
            "request-cursor-pair",
            DIGEST_C,
            "{}",
            0,
            "cursor-0",
            None,
            None,
            "prepared",
            TS0,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(insert_read, invalid_cursor_pair)

        invalid_admitted_revision = prepared[:4] + (
            "request-admitted",
            DIGEST_D,
            "{}",
            4,
            None,
            None,
            None,
            "admitted",
            TS0,
            DIGEST_E,
            "snapshot-1",
            None,
            None,
            None,
            0,
            0,
            DIGEST_A,
            "capability-1",
            DIGEST_B,
            4,
            TS0,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(insert_read, invalid_admitted_revision)

        invalid_continuation = prepared[:4] + (
            "request-continuation",
            DIGEST_E,
            "{}",
            0,
            None,
            None,
            None,
            "admitted",
            TS0,
            DIGEST_F,
            "snapshot-1",
            None,
            None,
            "snapshot-1",
            1,
            1,
            DIGEST_A,
            "capability-1",
            DIGEST_B,
            1,
            TS0,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(insert_read, invalid_continuation)

        self._insert_admitted_read()
        checkpoint_sql = """
            INSERT INTO native_im_inbound_checkpoints (
                tenant_id, workspace_id, provider, channel_id, after_cursor,
                after_sequence, continuation_snapshot_token,
                checkpoint_revision, last_read_request_digest,
                last_page_digest, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                checkpoint_sql,
                (
                    "tenant-1",
                    "workspace-1",
                    "native-im",
                    "channel-1",
                    "cursor-1",
                    None,
                    None,
                    1,
                    DIGEST_E,
                    DIGEST_F,
                    TS0,
                ),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                checkpoint_sql,
                (
                    "tenant-1",
                    "workspace-1",
                    "native-im",
                    "channel-1",
                    "cursor-1",
                    1,
                    None,
                    0,
                    DIGEST_E,
                    DIGEST_F,
                    TS0,
                ),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                checkpoint_sql,
                (
                    "tenant-1",
                    "workspace-1",
                    "native-im",
                    "channel-1",
                    "cursor-1",
                    1,
                    None,
                    1,
                    DIGEST_E,
                    DIGEST_A,
                    TS0,
                ),
            )

    def test_down_migration_and_reapply_are_replayable(self) -> None:
        self._seed_all_tables()
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])

        self._downgrade_to_v4()
        self.assertEqual(current_schema_version(self.connection), 4)
        remaining = {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            )
        }
        self.assertTrue(set(NATIVE_IM_TABLES).isdisjoint(remaining))

        self.assertEqual(
            apply_sqlite_migrations(
                self.connection,
                target_versions=tuple(range(1, 6)),
                clock=lambda: TS1,
            ),
            5,
        )
        self.assertEqual(validate_sqlite_schema(self.connection), 5)
        for table in NATIVE_IM_TABLES:
            with self.subTest(table=table):
                self.assertEqual(
                    self.connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0],
                    0,
                )
        self._seed_all_tables()
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_active_backup_and_restore_preserve_all_six_table_row_counts(self) -> None:
        self.connection.execute("PRAGMA journal_mode=WAL")
        self._seed_all_tables()
        backup_path = self.root / "backup" / "snapshot.sqlite3"
        created = create_sqlite_backup(self.database, backup_path, clock=lambda: TS1)
        expected_counts = {table: 1 for table in NATIVE_IM_TABLES}
        self.assertEqual(
            {table: created.table_counts[table] for table in NATIVE_IM_TABLES},
            expected_counts,
        )
        self.assertEqual(
            [item["version"] for item in created.migrations],
            list(range(1, len(MIGRATIONS) + 1)),
        )

        destination = self.root / "restored" / "state.sqlite3"
        restored = restore_sqlite_backup(
            backup_path,
            destination,
            manifest_path=default_manifest_path(backup_path),
        )
        self.assertEqual(restored, created)
        self.assertEqual(
            {table: restored.table_counts[table] for table in NATIVE_IM_TABLES},
            expected_counts,
        )
        with sqlite3.connect(destination) as restored_connection:
            restored_connection.execute("PRAGMA foreign_keys=ON")
            self.assertEqual(restored_connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            for table in NATIVE_IM_TABLES:
                with self.subTest(restored_table=table):
                    self.assertEqual(
                        restored_connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[
                            0
                        ],
                        1,
                    )


if __name__ == "__main__":
    unittest.main()
