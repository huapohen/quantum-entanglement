from __future__ import annotations

import sqlite3
import tempfile
import unittest
from collections import Counter
from pathlib import Path

import quantum_entanglement
from quantum_entanglement._inactive_invocation_results_migration import (
    _INACTIVE_INVOCATION_RESULTS_CANDIDATE,
    _INACTIVE_INVOCATION_RESULTS_DESCRIPTOR,
    _INACTIVE_INVOCATION_RESULTS_MIGRATIONS,
    _KNOWN_INVOCATION_RESULTS_DOMAIN_REGISTRY,
    _KNOWN_INVOCATION_RESULTS_MIGRATIONS,
)
from quantum_entanglement.domain_migrations import (
    DOMAIN_MIGRATION_REGISTRY,
    LEGACY_DOMAIN_MIGRATIONS,
)
from quantum_entanglement.migrations import (
    MIGRATIONS,
    MigrationVersionError,
    _sql_statements,
    apply_sqlite_migrations,
    current_schema_version,
    migration_text,
    validate_sqlite_schema,
)
from quantum_entanglement.store import SQLiteEventStore

_RESULT_TABLES = (
    "invocation_result_artifacts",
    "invocation_result_manifests",
    "invocation_result_publications",
    "invocation_result_receipts",
    "invocation_result_requests",
)

_CANDIDATE_INDEXES = (
    "idx_invocation_result_artifacts_reverse",
    "idx_invocation_result_publications_trigger",
    "idx_invocation_result_receipts_attempt",
    "idx_invocation_result_receipts_manifest",
    "idx_invocation_result_receipts_request",
    "idx_invocation_result_receipts_scope",
    "idx_invocation_result_requests_manifest",
    "idx_invocation_result_requests_scope",
    "uq_artifact_versions_result_binding",
    "uq_events_result_receipt_coordinates",
    "uq_invocation_admissions_result_binding",
    "uq_invocation_attempts_result_binding",
    "uq_invocation_jobs_result_binding",
    "uq_outbox_result_publication_binding",
)

_RESULT_COLUMNS = {
    "invocation_result_manifests": (
        "tenant_id",
        "workspace_id",
        "manifest_digest",
        "schema_version",
        "canonical_bytes",
        "byte_size",
        "created_at",
    ),
    "invocation_result_requests": (
        "tenant_id",
        "workspace_id",
        "request_digest",
        "schema_version",
        "acceptance_idempotency_key",
        "request_identity_bytes",
        "request_identity_byte_size",
        "invocation_id",
        "session_id",
        "plan_id",
        "task_id",
        "agent_id",
        "job_idempotency_key",
        "start_receipt_digest",
        "execution_manifest_digest",
        "result_manifest_digest",
        "expected_stream_version",
        "running_task_revision",
        "terminal_task_revision",
        "correlation_id",
        "causation_id",
        "runtime_revision",
        "effect_class",
        "action_receipt_set_digest",
        "result_ref",
        "primary_artifact_id",
        "artifact_count",
        "created_at",
    ),
    "invocation_result_receipts": (
        "tenant_id",
        "workspace_id",
        "receipt_id",
        "schema_version",
        "request_digest",
        "invocation_id",
        "session_id",
        "plan_id",
        "task_id",
        "agent_id",
        "job_idempotency_key",
        "acceptance_idempotency_key",
        "attempt_id",
        "attempt_number",
        "lease_epoch",
        "worker_id",
        "lease_token_digest",
        "start_receipt_digest",
        "execution_manifest_digest",
        "result_manifest_schema_version",
        "result_manifest_digest",
        "result_ref",
        "effect_class",
        "action_receipt_set_digest",
        "expected_stream_version",
        "running_task_revision",
        "terminal_task_revision",
        "accepted_at",
        "artifact_count",
        "result_evidence_digest",
        "terminal_transition_digest",
        "receipt_digest",
        "result_event_id",
        "result_event_stream_id",
        "result_event_type",
        "result_event_timestamp",
        "result_event_sequence",
        "result_event_global_position",
        "result_event_envelope_digest",
        "terminal_event_id",
        "terminal_event_stream_id",
        "terminal_event_type",
        "terminal_event_timestamp",
        "terminal_event_sequence",
        "terminal_event_global_position",
        "terminal_event_envelope_digest",
    ),
    "invocation_result_artifacts": (
        "tenant_id",
        "workspace_id",
        "receipt_id",
        "ordinal",
        "session_id",
        "task_id",
        "artifact_id",
        "name",
        "version",
        "parent_version",
        "media_type",
        "blob_digest",
        "byte_size",
        "metadata_digest",
        "created_by",
        "idempotency_key",
        "artifact_request_digest",
        "candidate_digest",
    ),
    "invocation_result_publications": (
        "tenant_id",
        "workspace_id",
        "receipt_id",
        "publication_kind",
        "message_id",
        "destination",
        "idempotency_key",
        "payload_digest",
        "headers_digest",
        "triggering_event_id",
        "triggering_global_position",
        "created_at",
    ),
}


class InactiveInvocationResultsMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tempdir.name) / "event-store.sqlite3")
        self.store = SQLiteEventStore(self.path)
        self.connection = self.store._connection

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def apply_candidate(self) -> None:
        version = apply_sqlite_migrations(
            self.connection,
            migrations=_KNOWN_INVOCATION_RESULTS_MIGRATIONS,
        )
        self.assertEqual(version, 7)

    def run_down(self) -> None:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            for statement in _sql_statements(migration_text("0007_invocation_results.down.sql")):
                self.connection.execute(statement)
            self.connection.execute("COMMIT")
        except BaseException:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise

    def explicit_object_names(self) -> tuple[str, ...]:
        rows = self.connection.execute(
            """
            SELECT name
            FROM main.sqlite_master
            WHERE type IN ('table', 'index')
              AND name NOT LIKE 'sqlite_autoindex_%'
              AND (
                    name LIKE 'invocation_result_%'
                    OR name LIKE 'idx_invocation_result_%'
                    OR name LIKE 'uq_%_result_%'
              )
            ORDER BY name
            """
        ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def test_candidate_identity_is_exact_disabled_and_separate(self) -> None:
        self.assertEqual(tuple(item.version for item in MIGRATIONS), (1, 2, 3, 4, 5, 6))
        self.assertEqual(
            tuple(item.migration_id for item in LEGACY_DOMAIN_MIGRATIONS),
            (1, 2, 3, 4, 5, 6),
        )
        self.assertEqual(
            tuple(item.migration_id for item in DOMAIN_MIGRATION_REGISTRY.descriptors),
            (1, 2, 3, 4, 5, 6),
        )
        self.assertEqual(
            tuple(
                item.migration_id for item in _KNOWN_INVOCATION_RESULTS_DOMAIN_REGISTRY.descriptors
            ),
            (1, 2, 3, 4, 5, 6, 7),
        )
        self.assertEqual(len(_INACTIVE_INVOCATION_RESULTS_MIGRATIONS), 1)
        self.assertIs(
            _INACTIVE_INVOCATION_RESULTS_MIGRATIONS[0],
            _INACTIVE_INVOCATION_RESULTS_CANDIDATE.migration,
        )
        self.assertFalse(_INACTIVE_INVOCATION_RESULTS_CANDIDATE.enabled)
        self.assertEqual(
            _INACTIVE_INVOCATION_RESULTS_CANDIDATE.component_preconditions,
            ("qe.event-store-core/1",),
        )
        self.assertFalse(hasattr(_INACTIVE_INVOCATION_RESULTS_CANDIDATE, "apply"))
        self.assertFalse(hasattr(_INACTIVE_INVOCATION_RESULTS_CANDIDATE, "executor"))
        self.assertEqual(_INACTIVE_INVOCATION_RESULTS_DESCRIPTOR.migration_id, 7)
        self.assertEqual(
            _INACTIVE_INVOCATION_RESULTS_DESCRIPTOR.filename,
            "0007_invocation_results.up.sql",
        )
        self.assertEqual(_INACTIVE_INVOCATION_RESULTS_DESCRIPTOR.domain, "invocation_results")
        self.assertEqual(_INACTIVE_INVOCATION_RESULTS_DESCRIPTOR.domain_version, 1)
        self.assertEqual(_INACTIVE_INVOCATION_RESULTS_DESCRIPTOR.kind, "native")
        self.assertEqual(_INACTIVE_INVOCATION_RESULTS_DESCRIPTOR.dependencies, (1, 2, 4))
        self.assertNotIn("_INACTIVE_INVOCATION_RESULTS_CANDIDATE", quantum_entanglement.__all__)

    def test_default_store_stays_at_six_without_candidate_objects(self) -> None:
        self.assertEqual(current_schema_version(self.connection), 6)
        self.assertEqual(validate_sqlite_schema(self.connection), 6)
        self.assertEqual(
            tuple(
                int(row[0])
                for row in self.connection.execute(
                    "SELECT version FROM qe_schema_migrations ORDER BY version"
                ).fetchall()
            ),
            (1, 2, 3, 4, 5, 6),
        )
        self.assertEqual(self.explicit_object_names(), ())

    def test_isolated_rehearsal_installs_exact_catalog_and_foreign_keys(self) -> None:
        self.apply_candidate()
        self.assertEqual(
            self.explicit_object_names(),
            tuple(sorted((*_RESULT_TABLES, *_CANDIDATE_INDEXES))),
        )
        self.assertEqual(
            validate_sqlite_schema(
                self.connection, migrations=_KNOWN_INVOCATION_RESULTS_MIGRATIONS
            ),
            7,
        )
        with self.assertRaises(MigrationVersionError):
            validate_sqlite_schema(self.connection)
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

        for table_name, expected_columns in _RESULT_COLUMNS.items():
            with self.subTest(table=table_name):
                rows = self.connection.execute(f"PRAGMA table_info({table_name})").fetchall()
                self.assertEqual(tuple(str(row[1]) for row in rows), expected_columns)

        expected_foreign_tables = {
            "invocation_result_manifests": Counter(),
            "invocation_result_requests": Counter(
                {
                    "invocation_jobs": 6,
                    "invocation_admissions": 4,
                    "invocation_result_manifests": 3,
                }
            ),
            "invocation_result_receipts": Counter(
                {
                    "invocation_result_requests": 3,
                    "invocation_attempts": 6,
                    "events": 12,
                }
            ),
            "invocation_result_artifacts": Counter(
                {"invocation_result_receipts": 3, "artifact_versions": 4}
            ),
            "invocation_result_publications": Counter(
                {"invocation_result_receipts": 5, "outbox": 6}
            ),
        }
        for table_name, expected in expected_foreign_tables.items():
            with self.subTest(foreign_keys=table_name):
                rows = self.connection.execute(f"PRAGMA foreign_key_list({table_name})").fetchall()
                self.assertEqual(Counter(str(row[2]) for row in rows), expected)
                self.assertTrue(all(row[5] == "RESTRICT" and row[6] == "RESTRICT" for row in rows))

    def test_descriptor_owned_objects_are_exact_candidate_creates(self) -> None:
        self.assertEqual(
            tuple(
                (item.object_type, item.name)
                for item in _INACTIVE_INVOCATION_RESULTS_DESCRIPTOR.owned_objects
            ),
            tuple(
                sorted(
                    (
                        *(("table", name) for name in _RESULT_TABLES),
                        *(("index", name) for name in _CANDIDATE_INDEXES),
                    )
                )
            ),
        )
        self.assertEqual(len(_INACTIVE_INVOCATION_RESULTS_DESCRIPTOR.owned_objects), 19)
        self.assertTrue(
            all(
                len(item.ddl_sha256) == 64
                for item in _INACTIVE_INVOCATION_RESULTS_DESCRIPTOR.owned_objects
            )
        )

    def test_empty_down_rehearsal_returns_to_exact_schema_six(self) -> None:
        self.apply_candidate()
        self.run_down()
        self.assertEqual(current_schema_version(self.connection), 6)
        self.assertEqual(validate_sqlite_schema(self.connection), 6)
        self.assertEqual(
            validate_sqlite_schema(
                self.connection,
                migrations=_KNOWN_INVOCATION_RESULTS_MIGRATIONS,
            ),
            6,
        )
        self.assertEqual(self.explicit_object_names(), ())
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_nonempty_down_guard_rolls_back_before_any_drop(self) -> None:
        self.apply_candidate()
        self.connection.execute(
            """
            INSERT INTO invocation_result_manifests (
                tenant_id,
                workspace_id,
                manifest_digest,
                schema_version,
                canonical_bytes,
                byte_size,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "tenant-1",
                "workspace-1",
                "a" * 64,
                2,
                sqlite3.Binary(b"{}"),
                2,
                "2026-08-29T00:00:00.000000Z",
            ),
        )
        before = self.explicit_object_names()
        with self.assertRaises(sqlite3.IntegrityError):
            self.run_down()
        self.assertEqual(self.explicit_object_names(), before)
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM invocation_result_manifests").fetchone()[
                0
            ],
            1,
        )
        self.assertEqual(current_schema_version(self.connection), 7)

    def test_old_default_store_reopen_rejects_rehearsed_seven(self) -> None:
        self.apply_candidate()
        self.store.close()
        with self.assertRaises(MigrationVersionError):
            SQLiteEventStore(self.path)


if __name__ == "__main__":
    unittest.main()
