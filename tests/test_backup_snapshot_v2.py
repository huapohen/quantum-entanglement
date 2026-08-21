import inspect
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch

import quantum_entanglement.backup as active_backup_module
import quantum_entanglement.backup_snapshot_v2 as snapshot_module
from quantum_entanglement.backup_manifest_v2 import (
    BACKUP_MANIFEST_V2_FORMAT,
    BackupManifestV2,
    decode_backup_manifest_v2,
    encode_backup_manifest_v2,
)
from quantum_entanglement.backup_snapshot_v2 import (
    BackupManifestV2Snapshot,
    BackupManifestV2SnapshotError,
    derive_backup_manifest_v2_snapshot,
)
from quantum_entanglement.backup_topology import (
    BACKUP_TOPOLOGY_REGISTRY,
    PROJECTION_STORE_PROFILE,
)
from quantum_entanglement.domain_migrations import (
    apply_bridge_migration_plan,
    bootstrap_legacy_domain_migration_metadata,
    inspect_schema_state,
    install_domain_migration_sidecar,
    plan_bridge_migrations,
)
from quantum_entanglement.migrations import apply_sqlite_migrations
from quantum_entanglement.projections import SQLiteProjectionOffsetStore
from quantum_entanglement.store import SQLiteEventStore
from quantum_entanglement.tenancy import SQLiteRevocationRevisionGuard

T0 = "2026-08-20T00:00:00Z"
CREATED_AT = "2026-08-20T00:00:00.000000Z"
SHA_A = "a" * 64


class HostileFault(Exception):
    pass


class HostileConnection(sqlite3.Connection):
    def __getattribute__(self, name: str) -> Any:
        if name in {"in_transaction", "row_factory", "text_factory"}:
            raise AssertionError("inherited connection state inspected")
        return super().__getattribute__(name)


def initialize_full_database(path: str) -> None:
    event_store = SQLiteEventStore(path, clock=lambda: T0)
    projection_store = SQLiteProjectionOffsetStore(path, clock=lambda: T0)
    revocation_guard = SQLiteRevocationRevisionGuard(path)
    event_store.close()
    projection_store.close()
    revocation_guard.close()
    connection = sqlite3.connect(path)
    try:
        source = inspect_schema_state(connection)
        apply_bridge_migration_plan(
            connection,
            plan_bridge_migrations(source),
            clock=lambda: T0,
        )
    finally:
        connection.close()


def initialize_schema_prefix(
    connection: sqlite3.Connection,
    migration_count: int,
    shape: str,
) -> None:
    if migration_count >= 3:
        raise ValueError("prefix helper supports only pre-event-store migration counts")
    if migration_count:
        apply_sqlite_migrations(
            connection,
            target_versions=tuple(range(1, migration_count + 1)),
        )
    else:
        connection.execute("VACUUM")
    if shape != "sidecar_absent":
        install_domain_migration_sidecar(connection)
    if shape == "bridged_prefix":
        bootstrap_legacy_domain_migration_metadata(connection, clock=lambda: T0)


class BackupManifestV2SnapshotTests(unittest.TestCase):
    def test_full_materialized_catalog_derives_manifest_ready_exact_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = str(Path(tempdir) / "full.sqlite3")
            initialize_full_database(path)
            connection = sqlite3.connect(path)
            connection.row_factory = sqlite3.Row
            try:
                self.assertFalse(connection.in_transaction)
                snapshot = derive_backup_manifest_v2_snapshot(connection)
                self.assertFalse(connection.in_transaction)
                connection.execute("ANALYZE")
                connection.commit()
                snapshot_with_statistics = derive_backup_manifest_v2_snapshot(connection)
                self.assertFalse(connection.in_transaction)
            finally:
                connection.close()

        self.assertIs(type(snapshot), BackupManifestV2Snapshot)
        self.assertEqual(snapshot.schema_state.shape, "bridged_prefix")
        self.assertEqual(len(snapshot.schema_state.applied_migrations), 3)
        self.assertEqual(len(snapshot.registry_topology.present_profiles), 8)
        self.assertEqual(len(snapshot.registry_topology.schema_objects), 58)
        self.assertEqual(
            tuple(item.profile for item in snapshot.registry_topology.present_profiles),
            tuple(profile.name for profile in BACKUP_TOPOLOGY_REGISTRY.profiles),
        )
        self.assertEqual(snapshot_with_statistics.schema_state, snapshot.schema_state)
        self.assertEqual(
            snapshot_with_statistics.registry_topology.present_profiles,
            snapshot.registry_topology.present_profiles,
        )
        manifest = BackupManifestV2(
            format_version=BACKUP_MANIFEST_V2_FORMAT,
            backup_id="backup_" + ("a" * 32),
            created_at=CREATED_AT,
            database_sha256=SHA_A,
            byte_size=snapshot.page_count * snapshot.page_size,
            page_count=snapshot.page_count,
            page_size=snapshot.page_size,
            schema_state=snapshot.schema_state,
            registry_topology=snapshot.registry_topology,
        )
        self.assertEqual(
            decode_backup_manifest_v2(encode_backup_manifest_v2(manifest)),
            manifest,
        )

        with tempfile.TemporaryDirectory() as tempdir:
            empty_path = str(Path(tempdir) / "empty.sqlite3")
            empty_connection = sqlite3.connect(empty_path)
            empty_connection.execute("VACUUM")
            try:
                empty = derive_backup_manifest_v2_snapshot(empty_connection)
            finally:
                empty_connection.close()
        with self.assertRaisesRegex(ValueError, "differs from schemaState"):
            BackupManifestV2Snapshot(
                page_count=snapshot.page_count,
                page_size=snapshot.page_size,
                schema_state=snapshot.schema_state,
                registry_topology=empty.registry_topology,
            )

    def test_every_current_schema_shape_uses_one_owned_transaction(self) -> None:
        cases = (
            (0, "sidecar_absent"),
            (0, "empty"),
            (1, "sidecar_absent"),
            (1, "legacy_prefix"),
            (1, "bridged_prefix"),
            (2, "sidecar_absent"),
            (2, "legacy_prefix"),
            (2, "bridged_prefix"),
            (3, "sidecar_absent"),
            (3, "legacy_prefix"),
            (3, "bridged_prefix"),
        )
        for migration_count, shape in cases:
            with self.subTest(migration_count=migration_count, shape=shape):
                with tempfile.TemporaryDirectory() as tempdir:
                    path = str(Path(tempdir) / "prefix.sqlite3")
                    if migration_count == 3:
                        event_store = SQLiteEventStore(path, clock=lambda: T0)
                        event_store.close()
                        connection = sqlite3.connect(path)
                        if shape != "sidecar_absent":
                            install_domain_migration_sidecar(connection)
                        if shape == "bridged_prefix":
                            bootstrap_legacy_domain_migration_metadata(
                                connection,
                                clock=lambda: T0,
                            )
                    else:
                        connection = sqlite3.connect(path)
                        initialize_schema_prefix(connection, migration_count, shape)
                    try:
                        snapshot = derive_backup_manifest_v2_snapshot(connection)
                        self.assertFalse(connection.in_transaction)
                    finally:
                        connection.close()
                self.assertEqual(snapshot.schema_state.shape, shape)
                self.assertEqual(
                    len(snapshot.schema_state.applied_migrations),
                    migration_count,
                )

    def test_unknown_drifted_partial_and_malformed_schema_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            unknown_path = str(Path(tempdir) / "unknown.sqlite3")
            unknown = sqlite3.connect(unknown_path)
            unknown.execute("CREATE TABLE unknown_table (value INTEGER NOT NULL)")
            unknown.commit()
            with self.assertRaisesRegex(
                BackupManifestV2SnapshotError,
                "schema_catalog_contains_unknown_object",
            ):
                derive_backup_manifest_v2_snapshot(unknown)
            self.assertFalse(unknown.in_transaction)
            unknown.close()

            partial_path = str(Path(tempdir) / "partial.sqlite3")
            initialize_full_database(partial_path)
            partial = sqlite3.connect(partial_path)
            projection_profile = BACKUP_TOPOLOGY_REGISTRY.profile(PROJECTION_STORE_PROFILE)
            removable = next(
                item
                for item in projection_profile.objects
                if item.object_type == "index" and item.ddl_sha256 is not None
            )
            partial.execute(f'DROP INDEX "{removable.name}"')
            partial.commit()
            with self.assertRaisesRegex(
                BackupManifestV2SnapshotError,
                "schema_catalog_contains_partial_profile",
            ):
                derive_backup_manifest_v2_snapshot(partial)
            self.assertFalse(partial.in_transaction)
            partial.close()

            timestamp_path = str(Path(tempdir) / "timestamp.sqlite3")
            initialize_full_database(timestamp_path)
            malformed = sqlite3.connect(timestamp_path)
            malformed.execute(
                "UPDATE qe_schema_migrations SET applied_at = 'not-a-time' WHERE version = 1"
            )
            malformed.commit()
            with self.assertRaisesRegex(
                BackupManifestV2SnapshotError,
                "schema_state_invalid",
            ):
                derive_backup_manifest_v2_snapshot(malformed)
            self.assertFalse(malformed.in_transaction)
            malformed.close()

    def test_connection_boundary_rejects_subclasses_configuration_and_caller_transaction(
        self,
    ) -> None:
        hostile = sqlite3.connect(":memory:", factory=HostileConnection)
        try:
            with self.assertRaisesRegex(
                BackupManifestV2SnapshotError,
                "connection_type_invalid",
            ):
                derive_backup_manifest_v2_snapshot(hostile)
        finally:
            hostile.close()

        connection = sqlite3.connect(":memory:")
        connection.row_factory = lambda _cursor, row: row
        with self.assertRaisesRegex(
            BackupManifestV2SnapshotError,
            "connection_row_factory_unsupported",
        ):
            derive_backup_manifest_v2_snapshot(connection)
        connection.row_factory = None
        connection.text_factory = bytes
        with self.assertRaisesRegex(
            BackupManifestV2SnapshotError,
            "connection_text_factory_unsupported",
        ):
            derive_backup_manifest_v2_snapshot(connection)
        connection.text_factory = str
        connection.execute("BEGIN")
        with self.assertRaisesRegex(
            BackupManifestV2SnapshotError,
            "connection_transaction_already_active",
        ):
            derive_backup_manifest_v2_snapshot(connection)
        self.assertTrue(connection.in_transaction)
        connection.rollback()
        connection.close()

        closed = sqlite3.connect(":memory:")
        closed.close()
        with self.assertRaisesRegex(
            BackupManifestV2SnapshotError,
            "connection_state_unavailable",
        ):
            derive_backup_manifest_v2_snapshot(closed)

    def test_public_errors_detach_an_active_exception_context(self) -> None:
        caught: Optional[BackupManifestV2SnapshotError] = None
        try:
            raise HostileFault("outer-secret")
        except HostileFault:
            try:
                derive_backup_manifest_v2_snapshot(object())
            except BackupManifestV2SnapshotError as error:
                caught = error
        self.assertIsNotNone(caught)
        assert caught is not None
        self.assertIsNone(caught.__context__)
        self.assertIsNone(caught.__cause__)

    def test_originating_control_precedes_cleanup_control(self) -> None:
        connection = sqlite3.connect(":memory:")
        try:
            with (
                patch.object(
                    snapshot_module,
                    "_derive_inside_transaction",
                    side_effect=KeyboardInterrupt("origin"),
                ),
                patch.object(
                    snapshot_module,
                    "_rollback_owned_snapshot",
                    side_effect=SystemExit("cleanup"),
                ),
                self.assertRaises(KeyboardInterrupt) as caught,
            ):
                derive_backup_manifest_v2_snapshot(connection)
            self.assertEqual(caught.exception.args, ("origin",))
            self.assertTrue(connection.in_transaction)
            connection.rollback()

            with (
                patch.object(
                    snapshot_module,
                    "_execute",
                    side_effect=GeneratorExit("begin"),
                ),
                self.assertRaises(GeneratorExit) as begin_caught,
            ):
                derive_backup_manifest_v2_snapshot(connection)
            self.assertEqual(begin_caught.exception.args, ("begin",))
            self.assertFalse(connection.in_transaction)
        finally:
            if connection.in_transaction:
                connection.rollback()
            connection.close()

    def test_derivation_performs_no_schema_or_row_write(self) -> None:
        write_actions = {
            sqlite3.SQLITE_CREATE_INDEX,
            sqlite3.SQLITE_CREATE_TABLE,
            sqlite3.SQLITE_CREATE_TRIGGER,
            sqlite3.SQLITE_CREATE_VIEW,
            sqlite3.SQLITE_DELETE,
            sqlite3.SQLITE_DROP_INDEX,
            sqlite3.SQLITE_DROP_TABLE,
            sqlite3.SQLITE_DROP_TRIGGER,
            sqlite3.SQLITE_DROP_VIEW,
            sqlite3.SQLITE_INSERT,
            sqlite3.SQLITE_UPDATE,
        }
        observed: list[int] = []
        with tempfile.TemporaryDirectory() as tempdir:
            path = str(Path(tempdir) / "readonly.sqlite3")
            initialize_full_database(path)
            connection = sqlite3.connect(path)

            def authorize(
                action: int,
                _first: Optional[str],
                _second: Optional[str],
                _database: Optional[str],
                _source: Optional[str],
            ) -> int:
                observed.append(action)
                return sqlite3.SQLITE_OK

            connection.set_authorizer(authorize)
            try:
                derive_backup_manifest_v2_snapshot(connection)
            finally:
                connection.set_authorizer(None)
                connection.close()
        self.assertTrue(observed)
        self.assertFalse(write_actions & set(observed))

    def test_concurrent_wal_commit_cannot_mix_snapshot_table_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = str(Path(tempdir) / "wal.sqlite3")
            initialize_full_database(path)
            reader = sqlite3.connect(path)
            reached_count = threading.Event()
            writer_done = threading.Event()
            writer_errors: list[BaseException] = []
            count_query_started = False

            def trace(statement: str) -> None:
                nonlocal count_query_started
                if 'SELECT COUNT(*) FROM main."projection_offsets"' in statement:
                    count_query_started = True

            def progress() -> int:
                if count_query_started and not reached_count.is_set():
                    reached_count.set()
                    if not writer_done.wait(5):
                        return 1
                return 0

            def write_after_snapshot() -> None:
                try:
                    if not reached_count.wait(5):
                        raise AssertionError("reader did not reach table count")
                    writer = sqlite3.connect(path)
                    try:
                        writer.execute(
                            """
                            INSERT INTO projection_offsets (
                                projection_name, last_global_position, owner_id,
                                owner_epoch, lease_expires_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            ("concurrent", 0, "writer", 1, T0, T0),
                        )
                        writer.commit()
                    finally:
                        writer.close()
                except BaseException as error:
                    writer_errors.append(error)
                finally:
                    writer_done.set()

            reader.set_trace_callback(trace)
            reader.set_progress_handler(progress, 1)
            thread = threading.Thread(target=write_after_snapshot)
            thread.start()
            try:
                snapshot = derive_backup_manifest_v2_snapshot(reader)
            finally:
                reader.set_progress_handler(None, 0)
                reader.set_trace_callback(None)
                reader.close()
                thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(writer_errors, [])
            counts = {item.name: item.row_count for item in snapshot.registry_topology.table_counts}
            self.assertEqual(counts["projection_offsets"], 0)
            verification = sqlite3.connect(path)
            try:
                current = verification.execute(
                    "SELECT COUNT(*) FROM projection_offsets"
                ).fetchone()[0]
            finally:
                verification.close()
            self.assertEqual(current, 1)

    def test_active_v1_module_and_cli_remain_unaware_of_snapshot_v2(self) -> None:
        source = inspect.getsource(active_backup_module)
        self.assertNotIn("backup_snapshot_v2", source)
        self.assertNotIn("derive_backup_manifest_v2_snapshot", source)
        self.assertNotIn("BackupManifestV2Snapshot", source)


if __name__ == "__main__":
    unittest.main()
