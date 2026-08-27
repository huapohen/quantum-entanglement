import inspect
import sqlite3
import sys
import tempfile
import threading
import unittest
from asyncio import CancelledError
from pathlib import Path
from types import FrameType
from typing import Any, Callable, Optional
from unittest.mock import patch

import quantum_entanglement.backup as active_backup_module
import quantum_entanglement.backup_snapshot_v2 as snapshot_module
import quantum_entanglement.projections as projections_module
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
from quantum_entanglement.migrations import MIGRATIONS, apply_sqlite_migrations, migration_text
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


def initialize_event_store_schema_prefix(
    path: str,
    migration_count: int,
    shape: str,
) -> sqlite3.Connection:
    if migration_count < 3 or migration_count > len(MIGRATIONS):
        raise ValueError("event-store prefix helper supports migration counts 3 through current")
    event_store = SQLiteEventStore(path, clock=lambda: T0)
    event_store.close()
    connection = sqlite3.connect(path)
    for migration in reversed(MIGRATIONS[migration_count:]):
        connection.executescript(migration_text(migration.filename.replace(".up.sql", ".down.sql")))
        connection.execute(
            "DELETE FROM qe_schema_migrations WHERE version = ?",
            (migration.version,),
        )
    connection.commit()
    if shape != "sidecar_absent":
        install_domain_migration_sidecar(connection)
    if shape == "bridged_prefix":
        bootstrap_legacy_domain_migration_metadata(connection, clock=lambda: T0)
    return connection


class BackupManifestV2SnapshotTests(unittest.TestCase):
    def capture_semantic_boundary_control(
        self,
        connection: sqlite3.Connection,
        control: BaseException,
        *,
        boundary_ready: Callable[[], bool],
        transaction_active: bool,
    ) -> tuple[BaseException, bool]:
        """Inject once at a runtime transaction boundary, independent of source lines."""

        injected = False

        def trace(frame: FrameType, event: str, _argument: object) -> Any:
            nonlocal injected
            if (
                not injected
                and event == "line"
                and frame.f_globals is snapshot_module.__dict__
                and boundary_ready()
                and connection.in_transaction is transaction_active
            ):
                injected = True
                raise control
            return trace

        caught: Optional[BaseException] = None
        previous_trace = sys.gettrace()
        sys.settrace(trace)
        try:
            derive_backup_manifest_v2_snapshot(connection)
        except BaseException as error:
            caught = error
        finally:
            sys.settrace(previous_trace)
        self.assertIsNotNone(caught)
        assert caught is not None
        return caught, injected

    def assert_control_traceback_is_not_synthetically_reraised(
        self,
        control: BaseException,
    ) -> None:
        traceback_cursor = control.__traceback__
        self.assertIsNotNone(traceback_cursor)
        public_frames = 0
        trace_frames = 0
        while traceback_cursor is not None:
            if traceback_cursor.tb_frame.f_code is derive_backup_manifest_v2_snapshot.__code__:
                public_frames += 1
            if traceback_cursor.tb_frame.f_code is self.capture_semantic_boundary_control.__code__:
                trace_frames += 1
            traceback_cursor = traceback_cursor.tb_next
        self.assertEqual(public_frames, 1)
        self.assertEqual(trace_frames, 1)

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
        self.assertEqual(len(snapshot.schema_state.applied_migrations), len(MIGRATIONS))
        self.assertEqual(len(snapshot.registry_topology.present_profiles), 10)
        self.assertEqual(len(snapshot.registry_topology.schema_objects), 85)
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
            (4, "sidecar_absent"),
            (4, "legacy_prefix"),
            (4, "bridged_prefix"),
            (5, "sidecar_absent"),
            (5, "legacy_prefix"),
            (5, "bridged_prefix"),
        )
        for migration_count, shape in cases:
            with self.subTest(migration_count=migration_count, shape=shape):
                with tempfile.TemporaryDirectory() as tempdir:
                    path = str(Path(tempdir) / "prefix.sqlite3")
                    if migration_count >= 3:
                        connection = initialize_event_store_schema_prefix(
                            path,
                            migration_count,
                            shape,
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

            drift_path = str(Path(tempdir) / "drift.sqlite3")
            drift = sqlite3.connect(drift_path)
            drift.execute(projections_module._PROJECTION_OFFSETS_TABLE_SQL)
            drift.execute(projections_module._PROJECTION_RECEIPTS_TABLE_SQL)
            drift.execute(
                projections_module._PROJECTION_RECEIPTS_POSITION_INDEX_SQL.replace(
                    "projection_name, global_position",
                    "event_id",
                )
            )
            drift.commit()
            with self.assertRaisesRegex(
                BackupManifestV2SnapshotError,
                "schema_catalog_object_drift",
            ):
                derive_backup_manifest_v2_snapshot(drift)
            self.assertFalse(drift.in_transaction)
            drift.close()

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

    def test_begin_denial_or_nonopening_fails_closed_without_claiming_a_transaction(
        self,
    ) -> None:
        connection = sqlite3.connect(":memory:")
        connection.execute("VACUUM")

        def deny_begin(
            action: int,
            first: Optional[str],
            _second: Optional[str],
            _database: Optional[str],
            _source: Optional[str],
        ) -> int:
            if action == sqlite3.SQLITE_TRANSACTION and first == "BEGIN":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        def allow_all(
            _action: int,
            _first: Optional[str],
            _second: Optional[str],
            _database: Optional[str],
            _source: Optional[str],
        ) -> int:
            return sqlite3.SQLITE_OK

        try:
            connection.set_authorizer(deny_begin)
            with self.assertRaisesRegex(
                BackupManifestV2SnapshotError,
                "read_snapshot_begin_read_failed",
            ):
                derive_backup_manifest_v2_snapshot(connection)
            self.assertFalse(connection.in_transaction)
            connection.set_authorizer(allow_all)

            with (
                patch.object(snapshot_module, "_execute", return_value=None),
                self.assertRaisesRegex(
                    BackupManifestV2SnapshotError,
                    "read_snapshot_not_opened",
                ),
            ):
                derive_backup_manifest_v2_snapshot(connection)
            self.assertFalse(connection.in_transaction)

            snapshot = derive_backup_manifest_v2_snapshot(connection)
            self.assertIs(type(snapshot), BackupManifestV2Snapshot)
            self.assertFalse(connection.in_transaction)
        finally:
            connection.set_authorizer(None)
            if connection.in_transaction:
                connection.rollback()
            connection.close()

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

    def test_single_control_after_begin_effect_is_rolled_back_and_connection_reused(
        self,
    ) -> None:
        def begin_observer(state: dict[str, bool]) -> Callable[[str], None]:
            def observe(statement: str) -> None:
                if " ".join(statement.strip().split()).upper() == "BEGIN":
                    state["begin_seen"] = True

            return observe

        control_types = (KeyboardInterrupt, SystemExit, GeneratorExit, CancelledError)
        for control_type in control_types:
            with self.subTest(control_type=control_type.__name__):
                connection = sqlite3.connect(":memory:")
                connection.execute("VACUUM")
                boundary_state = {"begin_seen": False}
                connection.set_trace_callback(begin_observer(boundary_state))
                control = control_type(f"begin-boundary-{control_type.__name__}")
                try:
                    caught, injected = self.capture_semantic_boundary_control(
                        connection,
                        control,
                        boundary_ready=lambda state=boundary_state: state["begin_seen"],
                        transaction_active=True,
                    )
                    self.assertTrue(injected)
                    self.assertIs(caught, control)
                    self.assertFalse(connection.in_transaction)
                    self.assert_control_traceback_is_not_synthetically_reraised(control)
                    snapshot = derive_backup_manifest_v2_snapshot(connection)
                    self.assertIs(type(snapshot), BackupManifestV2Snapshot)
                    self.assertFalse(connection.in_transaction)
                finally:
                    connection.set_trace_callback(None)
                    if connection.in_transaction:
                        connection.rollback()
                    connection.close()

    def test_single_control_after_body_success_or_failure_always_cleans_snapshot(
        self,
    ) -> None:
        control_types = (KeyboardInterrupt, SystemExit, GeneratorExit, CancelledError)
        real_derive = snapshot_module._derive_inside_transaction

        def body_for(
            expected_outcome: str,
            state: dict[str, bool],
        ) -> Callable[[sqlite3.Connection], BackupManifestV2Snapshot]:
            def finish_body(target: sqlite3.Connection) -> BackupManifestV2Snapshot:
                if expected_outcome == "failure":
                    state["body_finished"] = True
                    raise HostileFault("body failed before asynchronous control")
                result = real_derive(target)
                state["body_finished"] = True
                return result

            return finish_body

        for body_outcome in ("success", "failure"):
            for control_type in control_types:
                with self.subTest(body_outcome=body_outcome, control_type=control_type.__name__):
                    connection = sqlite3.connect(":memory:")
                    connection.execute("VACUUM")
                    boundary_state = {"body_finished": False}

                    control = control_type(f"body-{body_outcome}-boundary-{control_type.__name__}")
                    try:
                        with patch.object(
                            snapshot_module,
                            "_derive_inside_transaction",
                            side_effect=body_for(body_outcome, boundary_state),
                        ):
                            caught, injected = self.capture_semantic_boundary_control(
                                connection,
                                control,
                                boundary_ready=lambda state=boundary_state: state["body_finished"],
                                transaction_active=True,
                            )
                        self.assertTrue(injected)
                        self.assertIs(caught, control)
                        self.assertFalse(connection.in_transaction)
                        self.assert_control_traceback_is_not_synthetically_reraised(control)
                        snapshot = derive_backup_manifest_v2_snapshot(connection)
                        self.assertIs(type(snapshot), BackupManifestV2Snapshot)
                        self.assertFalse(connection.in_transaction)
                    finally:
                        if connection.in_transaction:
                            connection.rollback()
                        connection.close()

    def test_transient_cleanup_error_or_control_is_retried_without_hiding_origin(
        self,
    ) -> None:
        real_rollback = snapshot_module._rollback_owned_snapshot
        control_types = (KeyboardInterrupt, SystemExit, GeneratorExit, CancelledError)

        def rollback_after_one_failure(
            failure: BaseException,
            state: dict[str, int],
        ) -> Callable[[sqlite3.Connection], None]:
            def rollback_once(target: sqlite3.Connection) -> None:
                state["calls"] += 1
                if state["calls"] == 1:
                    raise failure
                real_rollback(target)

            return rollback_once

        cases = (
            (HostileFault("ordinary cleanup failure"), None),
            (SystemExit("cleanup control"), None),
            (SystemExit("cleanup must not replace origin"), KeyboardInterrupt("origin")),
            (KeyboardInterrupt("cleanup must not replace origin"), SystemExit("origin")),
            (GeneratorExit("cleanup must not replace origin"), CancelledError("origin")),
            (CancelledError("cleanup must not replace origin"), GeneratorExit("origin")),
        )
        for cleanup_failure, originating_control in cases:
            with self.subTest(
                cleanup_type=type(cleanup_failure).__name__,
                origin_type=(
                    None if originating_control is None else type(originating_control).__name__
                ),
            ):
                connection = sqlite3.connect(":memory:")
                connection.execute("VACUUM")
                cleanup_state = {"calls": 0}

                body_patch = (
                    patch.object(
                        snapshot_module,
                        "_derive_inside_transaction",
                        side_effect=originating_control,
                    )
                    if originating_control is not None
                    else patch.object(
                        snapshot_module,
                        "_derive_inside_transaction",
                        wraps=snapshot_module._derive_inside_transaction,
                    )
                )
                caught: Optional[BaseException] = None
                try:
                    with (
                        body_patch,
                        patch.object(
                            snapshot_module,
                            "_rollback_owned_snapshot",
                            side_effect=rollback_after_one_failure(
                                cleanup_failure,
                                cleanup_state,
                            ),
                        ),
                    ):
                        try:
                            derive_backup_manifest_v2_snapshot(connection)
                        except BaseException as error:
                            caught = error
                    self.assertIsNotNone(caught)
                    assert caught is not None
                    if originating_control is not None:
                        self.assertIs(caught, originating_control)
                    elif type(cleanup_failure) in control_types:
                        self.assertIs(caught, cleanup_failure)
                    else:
                        self.assertIs(type(caught), BackupManifestV2SnapshotError)
                        self.assertEqual(str(caught), "read_snapshot_cleanup_failed")
                    self.assertEqual(cleanup_state["calls"], 2)
                    self.assertFalse(connection.in_transaction)
                    snapshot = derive_backup_manifest_v2_snapshot(connection)
                    self.assertIs(type(snapshot), BackupManifestV2Snapshot)
                    self.assertFalse(connection.in_transaction)
                finally:
                    if connection.in_transaction:
                        connection.rollback()
                    connection.close()

    def test_ambient_handled_control_cannot_authenticate_cleanup_priority(self) -> None:
        real_rollback = snapshot_module._rollback_owned_snapshot
        control_types = (KeyboardInterrupt, SystemExit, GeneratorExit, CancelledError)

        def rollback_after_failure(
            state: dict[str, int],
        ) -> Callable[[sqlite3.Connection], None]:
            def rollback_once(target: sqlite3.Connection) -> None:
                state["calls"] += 1
                if state["calls"] == 1:
                    raise HostileFault("cleanup must not trust ambient control")
                real_rollback(target)

            return rollback_once

        for control_type in control_types:
            with self.subTest(control_type=control_type.__name__):
                connection = sqlite3.connect(":memory:")
                connection.execute("VACUUM")
                cleanup_state = {"calls": 0}

                caught: Optional[BackupManifestV2SnapshotError] = None
                try:
                    try:
                        raise control_type(f"ambient-{control_type.__name__}")
                    except BaseException:
                        with patch.object(
                            snapshot_module,
                            "_rollback_owned_snapshot",
                            side_effect=rollback_after_failure(cleanup_state),
                        ):
                            try:
                                derive_backup_manifest_v2_snapshot(connection)
                            except BackupManifestV2SnapshotError as error:
                                caught = error
                    self.assertIsNotNone(caught)
                    assert caught is not None
                    self.assertEqual(str(caught), "read_snapshot_cleanup_failed")
                    self.assertIsNone(caught.__context__)
                    self.assertIsNone(caught.__cause__)
                    self.assertEqual(cleanup_state["calls"], 2)
                    self.assertFalse(connection.in_transaction)
                    snapshot = derive_backup_manifest_v2_snapshot(connection)
                    self.assertIs(type(snapshot), BackupManifestV2Snapshot)
                finally:
                    if connection.in_transaction:
                        connection.rollback()
                    connection.close()

    def test_control_after_real_rollback_and_wal_writer_leave_reader_reusable(self) -> None:
        def rollback_observer(state: dict[str, bool]) -> Callable[[str], None]:
            def observe(statement: str) -> None:
                if " ".join(statement.strip().split()).upper() == "ROLLBACK":
                    state["seen"] = True

            return observe

        def rollback_ready(state: dict[str, bool]) -> Callable[[], bool]:
            def ready() -> bool:
                return state["seen"]

            return ready

        control_types = (KeyboardInterrupt, SystemExit, GeneratorExit, CancelledError)
        for control_type in control_types:
            with self.subTest(control_type=control_type.__name__):
                with tempfile.TemporaryDirectory() as tempdir:
                    path = str(Path(tempdir) / "wal-boundary.sqlite3")
                    initialize_full_database(path)
                    journal = sqlite3.connect(path)
                    try:
                        self.assertEqual(
                            journal.execute("PRAGMA journal_mode=WAL").fetchone(),
                            ("wal",),
                        )
                    finally:
                        journal.close()

                    marker = f"post-rollback-boundary-{control_type.__name__}"
                    if control_type is SystemExit:
                        system_exit_code: Optional[object] = (marker, 73)
                        control = control_type(system_exit_code)
                    else:
                        system_exit_code = None
                        control = control_type(marker, 73)
                    try:
                        raise control
                    except BaseException as seeded:
                        self.assertIs(seeded, control)
                    preexisting_traceback = control.__traceback__
                    self.assertIsNotNone(preexisting_traceback)
                    original_args = control.args
                    cause = HostileFault(f"cause-{control_type.__name__}")
                    context = HostileFault(f"context-{control_type.__name__}")
                    control.__cause__ = cause
                    control.__context__ = context
                    original_suppress_context = control.__suppress_context__

                    reader = sqlite3.connect(path)
                    rollback_state = {"seen": False}
                    reader.set_trace_callback(rollback_observer(rollback_state))
                    try:
                        caught, injected = self.capture_semantic_boundary_control(
                            reader,
                            control,
                            boundary_ready=rollback_ready(rollback_state),
                            transaction_active=False,
                        )
                        self.assertTrue(injected)
                        self.assertIs(caught, control)
                        self.assertFalse(reader.in_transaction)
                        self.assertIs(control.args, original_args)
                        self.assertIs(control.__cause__, cause)
                        self.assertIs(control.__context__, context)
                        self.assertIs(
                            control.__suppress_context__,
                            original_suppress_context,
                        )
                        if control_type is SystemExit:
                            self.assertIs(control.code, system_exit_code)
                        traceback_cursor = control.__traceback__
                        retained_preexisting_traceback = False
                        while traceback_cursor is not None:
                            if traceback_cursor is preexisting_traceback:
                                retained_preexisting_traceback = True
                            traceback_cursor = traceback_cursor.tb_next
                        self.assertTrue(retained_preexisting_traceback)
                        self.assert_control_traceback_is_not_synthetically_reraised(control)

                        writer = sqlite3.connect(path)
                        try:
                            for row_count in (1, 2):
                                writer.execute(
                                    """
                                    INSERT INTO projection_offsets (
                                        projection_name, last_global_position, owner_id,
                                        owner_epoch, lease_expires_at, updated_at
                                    ) VALUES (?, ?, ?, ?, ?, ?)
                                    """,
                                    (
                                        f"post-control-{control_type.__name__}-{row_count}",
                                        0,
                                        "writer",
                                        1,
                                        T0,
                                        T0,
                                    ),
                                )
                                writer.commit()
                                self.assertFalse(writer.in_transaction)
                                self.assertEqual(
                                    writer.execute(
                                        "SELECT COUNT(*) FROM projection_offsets"
                                    ).fetchone(),
                                    (row_count,),
                                )
                                snapshot = derive_backup_manifest_v2_snapshot(reader)
                                counts = {
                                    item.name: item.row_count
                                    for item in snapshot.registry_topology.table_counts
                                }
                                self.assertEqual(counts["projection_offsets"], row_count)
                                self.assertFalse(reader.in_transaction)
                        finally:
                            if writer.in_transaction:
                                writer.rollback()
                            writer.close()
                    finally:
                        reader.set_trace_callback(None)
                        if reader.in_transaction:
                            reader.rollback()
                        reader.close()
                        control.__traceback__ = None
                        control.__cause__ = None
                        control.__context__ = None

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
