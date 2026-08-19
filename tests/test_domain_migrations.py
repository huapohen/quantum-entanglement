import hashlib
import itertools
import sqlite3
import tempfile
import threading
import unittest
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, Optional, cast
from unittest import mock

from quantum_entanglement.domain_migrations import (
    DOMAIN_MIGRATION_DEPENDENCIES_TABLE_NAME,
    DOMAIN_MIGRATION_METADATA_TABLE_NAME,
    DOMAIN_MIGRATION_REGISTRY,
    DOMAIN_MIGRATION_SIDECAR_DDL,
    DOMAIN_MIGRATION_SIDECAR_SCHEMA_SHA256,
    LEGACY_DOMAIN_MIGRATIONS,
    MAX_DOMAIN_MIGRATION_SIDECAR_SCHEMA_OBJECTS,
    MAX_DOMAIN_MIGRATIONS,
    MAX_MIGRATION_DEPENDENCIES,
    MAX_MIGRATION_DOMAINS,
    MAX_OWNED_SCHEMA_OBJECTS,
    DomainMigrationBridgeIntegrityError,
    DomainMigrationBridgeShape,
    DomainMigrationBridgeState,
    DomainMigrationDependencyRow,
    DomainMigrationDescriptor,
    DomainMigrationLedgerRow,
    DomainMigrationLegacyBootstrapError,
    DomainMigrationMetadataRow,
    DomainMigrationRegistry,
    DomainMigrationSidecarInstallError,
    DomainMigrationSidecarSchemaError,
    DomainMigrationSidecarSchemaState,
    OwnedSchemaObject,
    bootstrap_legacy_domain_migration_metadata,
    install_domain_migration_sidecar,
    read_domain_migration_bridge_state,
    validate_domain_migration_registry,
    validate_domain_migration_sidecar_schema,
)
from quantum_entanglement.migrations import apply_sqlite_migrations, migration_text

SHA_A = "a" * 64
SHA_B = "b" * 64
BRIDGE_TIME = "2026-08-20T00:00:00Z"


class CountingIntegers:
    def __init__(self) -> None:
        self.yielded = 0

    def __iter__(self) -> "CountingIntegers":
        return self

    def __next__(self) -> int:
        self.yielded += 1
        return 1


class CountingRows:
    def __init__(self, row: object) -> None:
        self.row = row
        self.yielded = 0

    def __iter__(self) -> "CountingRows":
        return self

    def __next__(self) -> object:
        self.yielded += 1
        return self.row


class TextSubclass(str):
    pass


class FakeRowsCursor:
    def __init__(self, rows: Iterable[object]) -> None:
        self.rows: Iterator[object] = iter(rows)

    def fetchmany(self, size: int = 1) -> list[object]:
        return list(itertools.islice(self.rows, size))


class RecordingConnection:
    def __init__(self, delegate: sqlite3.Connection) -> None:
        self.delegate = delegate
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    @property
    def in_transaction(self) -> bool:
        return self.delegate.in_transaction

    def execute(
        self,
        statement: str,
        parameters: Sequence[object] = (),
    ) -> sqlite3.Cursor:
        snapshot = tuple(parameters)
        self.calls.append((statement, snapshot))
        return self.delegate.execute(statement, snapshot)


class FakeQueryConnection(RecordingConnection):
    def __init__(
        self,
        delegate: sqlite3.Connection,
        fake_queries: dict[str, Iterable[object]],
    ) -> None:
        super().__init__(delegate)
        self.fake_queries = fake_queries

    def execute(
        self,
        statement: str,
        parameters: Sequence[object] = (),
    ) -> Any:
        snapshot = tuple(parameters)
        self.calls.append((statement, snapshot))
        normalized = " ".join(statement.split())
        for marker, rows in self.fake_queries.items():
            if marker in normalized:
                return FakeRowsCursor(rows)
        return self.delegate.execute(statement, snapshot)


class InjectedFailureConnection(RecordingConnection):
    def __init__(
        self,
        delegate: sqlite3.Connection,
        *,
        target_statement: str,
        error: BaseException,
    ) -> None:
        super().__init__(delegate)
        self.target_statement = target_statement.strip().rstrip(";")
        self.error = error
        self.failed = False

    def execute(
        self,
        statement: str,
        parameters: Sequence[object] = (),
    ) -> sqlite3.Cursor:
        snapshot = tuple(parameters)
        self.calls.append((statement, snapshot))
        normalized = statement.strip().rstrip(";")
        if not self.failed and normalized == self.target_statement:
            self.failed = True
            raise self.error
        return self.delegate.execute(statement, snapshot)


class MarkerFailureConnection(RecordingConnection):
    def __init__(
        self,
        delegate: sqlite3.Connection,
        *,
        marker: str,
        occurrence: int,
        error: BaseException,
    ) -> None:
        super().__init__(delegate)
        self.marker = " ".join(marker.split())
        self.occurrence = occurrence
        self.error = error
        self.observed = 0
        self.failed = False

    def execute(
        self,
        statement: str,
        parameters: Sequence[object] = (),
    ) -> sqlite3.Cursor:
        snapshot = tuple(parameters)
        self.calls.append((statement, snapshot))
        normalized = " ".join(statement.strip().rstrip(";").split())
        if self.marker in normalized:
            self.observed += 1
            if self.observed == self.occurrence:
                self.failed = True
                raise self.error
        return self.delegate.execute(statement, snapshot)


class BeginBarrierConnection(RecordingConnection):
    def __init__(
        self,
        delegate: sqlite3.Connection,
        barrier: threading.Barrier,
    ) -> None:
        super().__init__(delegate)
        self.barrier = barrier

    def execute(
        self,
        statement: str,
        parameters: Sequence[object] = (),
    ) -> sqlite3.Cursor:
        if statement.strip().upper() == "BEGIN IMMEDIATE":
            self.barrier.wait(timeout=5)
        return super().execute(statement, parameters)


class PostBeginFailureConnection(RecordingConnection):
    def execute(
        self,
        statement: str,
        parameters: Sequence[object] = (),
    ) -> sqlite3.Cursor:
        cursor = super().execute(statement, parameters)
        if statement.strip().upper() == "BEGIN IMMEDIATE":
            raise KeyboardInterrupt("injected post-BEGIN interrupt")
        return cursor


class DomainMigrationRegistryTests(unittest.TestCase):
    def descriptor(self, migration_id: int) -> DomainMigrationDescriptor:
        return next(item for item in LEGACY_DOMAIN_MIGRATIONS if item.migration_id == migration_id)

    def replacing(
        self,
        target_id: int,
        **changes: Any,
    ) -> tuple[DomainMigrationDescriptor, ...]:
        return tuple(
            replace(item, **changes) if item.migration_id == target_id else item
            for item in LEGACY_DOMAIN_MIGRATIONS
        )

    def test_current_legacy_mapping_and_owned_objects_are_golden(self) -> None:
        expected = (
            (
                1,
                "0001_invocation_attempts.up.sql",
                "26e825171c52574ba3862f9efe6810cffae88964dd3fefc16cc7d1153d5a150c",
                "attempts",
                (
                    ("index", "idx_invocation_attempts_job"),
                    ("index", "idx_invocation_attempts_status"),
                    ("index", "idx_invocation_jobs_claim"),
                    ("index", "idx_invocation_jobs_lease_expiry"),
                    ("index", "idx_invocation_jobs_session"),
                    ("table", "invocation_attempts"),
                    ("table", "invocation_jobs"),
                ),
            ),
            (
                2,
                "0002_artifacts.up.sql",
                "6bd602c2fe18a2b0674123e19fde7cb0219c73a0901143fcd4faa196daea53f5",
                "artifacts",
                (
                    ("index", "idx_artifact_versions_digest"),
                    ("index", "idx_artifact_versions_head"),
                    ("index", "idx_artifact_versions_task"),
                    ("table", "artifact_blobs"),
                    ("table", "artifact_versions"),
                ),
            ),
            (
                3,
                "0003_outbox_ambiguities.up.sql",
                "218c89563df8f404e50618f0ac171472a2ecc4b1d17fcd06bcb34bddc191cbd0",
                "delivery",
                (
                    ("index", "idx_outbox_ambiguities_one_open"),
                    ("index", "idx_outbox_ambiguities_opened"),
                    ("table", "outbox_ambiguities"),
                ),
            ),
        )
        actual = tuple(
            (
                descriptor.migration_id,
                descriptor.filename,
                descriptor.sql_sha256,
                descriptor.domain,
                tuple((owned.object_type, owned.name) for owned in descriptor.owned_objects),
            )
            for descriptor in LEGACY_DOMAIN_MIGRATIONS
        )
        self.assertEqual(actual, expected)
        for descriptor in LEGACY_DOMAIN_MIGRATIONS:
            self.assertEqual(descriptor.domain_version, 1)
            self.assertEqual(descriptor.kind, "legacy_bootstrap")
            self.assertEqual(descriptor.dependencies, ())
            self.assertNotIn(
                ("table", "qe_schema_migrations"),
                tuple((owned.object_type, owned.name) for owned in descriptor.owned_objects),
            )

    def test_current_ddl_descriptor_and_registry_digests_are_golden(self) -> None:
        expected_ddl = {
            (1, "index", "idx_invocation_attempts_job"): (
                "5fac47cc7f038759ebecf0b47d9d2eb749f76459fe34f2c09d6a53504fa04d74"
            ),
            (1, "index", "idx_invocation_attempts_status"): (
                "b2a7fce664294eca50deafef6eb6973fccf290bd6fe0aadc57c301e8a3edb161"
            ),
            (1, "index", "idx_invocation_jobs_claim"): (
                "645ff6d8f76113ec8d8339a5c1793e72297a98fd87adc3d32aa774394b22f4ad"
            ),
            (1, "index", "idx_invocation_jobs_lease_expiry"): (
                "482b8bca8237e03d842e20ca03f2bc0cbcfdf70e1452888b7960741b5bb2a5f7"
            ),
            (1, "index", "idx_invocation_jobs_session"): (
                "fe381b176f9f46410a6f7975592eed2fe78dae7ce1dd3ae6758f8bb1591a9176"
            ),
            (1, "table", "invocation_attempts"): (
                "5606618188d69f34cc092909dab26e9166ebc95df2d2467dac00bf26944b0ba4"
            ),
            (1, "table", "invocation_jobs"): (
                "36273db6dbec193faf37ca8f23b68efc1471a6fe51c74679a646e8253fe21ae8"
            ),
            (2, "index", "idx_artifact_versions_digest"): (
                "6f96c49420ce234a4f3f93a757647613040e9432430e1e0aa0152f47ef34a6a7"
            ),
            (2, "index", "idx_artifact_versions_head"): (
                "cb903e3efc219003501022cf9e03bc7527c09d0040878758f3a9de5caff78995"
            ),
            (2, "index", "idx_artifact_versions_task"): (
                "56b79bb782d84f96b26087766b823a007dd04b6b2c0c521330fdc3e4aef82efb"
            ),
            (2, "table", "artifact_blobs"): (
                "2c32324870b0be6b8f5ea524575912ff0eb08be9be10cc3cae28e96069cc35e9"
            ),
            (2, "table", "artifact_versions"): (
                "5fdaf59eed765b0f5b9ebddf1140e78d79a87803cf4d2a847a9dd596e447f9d6"
            ),
            (3, "index", "idx_outbox_ambiguities_one_open"): (
                "252c5aa2e457c9da3372fb61a852e090f41772354b1de7d2d9af5516b6c6a907"
            ),
            (3, "index", "idx_outbox_ambiguities_opened"): (
                "122b2f52d304a314989b57231fd0fffb07c25bd11a192aec26e02595c27131ea"
            ),
            (3, "table", "outbox_ambiguities"): (
                "59f6e65b66ba31d65ffc6bc61a3c773b51695e84916e1fa55a9178065ce5dc5e"
            ),
        }
        actual_ddl = {
            (
                descriptor.migration_id,
                owned.object_type,
                owned.name,
            ): owned.ddl_sha256
            for descriptor in LEGACY_DOMAIN_MIGRATIONS
            for owned in descriptor.owned_objects
        }
        self.assertEqual(actual_ddl, expected_ddl)
        self.assertEqual(
            tuple(item.descriptor_sha256 for item in LEGACY_DOMAIN_MIGRATIONS),
            (
                "11ad7f30230403c1084e41a991d7b4c46ef73968d058e57f69c1e22354c7a106",
                "119576a3a5c974af6fa55ab67b7f3af9666af0de153925311308d1736496d1b2",
                "7df074851ad36dcf32da85cb68c847b8cc0b4db3fd20ad858a43cfd927bf814f",
            ),
        )
        self.assertEqual(
            DOMAIN_MIGRATION_REGISTRY.registry_sha256,
            "3671c849a30e82acf9d0e6e96d36fdbfa7839936fa4314822c11cbca6bc2db73",
        )

    def test_registry_is_input_order_independent_and_normalized(self) -> None:
        forward = validate_domain_migration_registry(LEGACY_DOMAIN_MIGRATIONS)
        reverse = validate_domain_migration_registry(reversed(LEGACY_DOMAIN_MIGRATIONS))
        generated = validate_domain_migration_registry(
            item
            for item in (
                LEGACY_DOMAIN_MIGRATIONS[1],
                LEGACY_DOMAIN_MIGRATIONS[2],
                LEGACY_DOMAIN_MIGRATIONS[0],
            )
        )
        self.assertEqual(forward, reverse)
        self.assertEqual(forward, generated)
        self.assertEqual(
            tuple(item.migration_id for item in forward.descriptors),
            (1, 2, 3),
        )

    def test_dependency_and_owned_manifest_order_do_not_change_digests(self) -> None:
        objects = (
            OwnedSchemaObject("table", "zeta", SHA_A),
            OwnedSchemaObject("index", "alpha", SHA_B),
        )
        unordered = DomainMigrationDescriptor(
            migration_id=4,
            filename="0004_native.up.sql",
            sql_sha256=SHA_A,
            domain="native",
            domain_version=1,
            kind="native",
            dependencies=(3, 1, 2),
            owned_objects=objects,
        )
        ordered = replace(
            unordered,
            dependencies=(1, 2, 3),
            owned_objects=tuple(reversed(objects)),
        )
        self.assertEqual(
            unordered.owned_object_manifest_sha256,
            ordered.owned_object_manifest_sha256,
        )
        self.assertEqual(unordered.descriptor_sha256, ordered.descriptor_sha256)

    def test_frozen_descriptor_snapshots_nested_iterables(self) -> None:
        dependencies = [3, 1, 2]
        owned = [OwnedSchemaObject("table", "stable", SHA_A)]
        descriptor = DomainMigrationDescriptor(
            migration_id=4,
            filename="0004_native.up.sql",
            sql_sha256=SHA_A,
            domain="native",
            domain_version=1,
            kind="native",
            # Deliberately bypass the static tuple contract to prove runtime snapshotting.
            dependencies=dependencies,  # type: ignore[arg-type]
            owned_objects=owned,  # type: ignore[arg-type]
        )
        dependencies.append(4)
        owned.clear()
        self.assertEqual(descriptor.dependencies, (3, 1, 2))
        self.assertEqual(len(descriptor.owned_objects), 1)
        with self.assertRaises((AttributeError, TypeError)):
            # Deliberately violate the frozen contract to prove runtime enforcement.
            descriptor.domain = "changed"  # type: ignore[misc]

    def test_constructor_bounds_tuples_and_infinite_iterables_before_digest(self) -> None:
        with self.assertRaisesRegex(ValueError, "migration dependencies exceeds"):
            DomainMigrationDescriptor(
                migration_id=4,
                filename="0004_native.up.sql",
                sql_sha256=SHA_A,
                domain="native",
                domain_version=1,
                kind="native",
                dependencies=tuple(range(MAX_MIGRATION_DEPENDENCIES + 1)),
                owned_objects=(),
            )

        with self.assertRaisesRegex(ValueError, "owned schema objects exceeds"):
            DomainMigrationDescriptor(
                migration_id=4,
                filename="0004_native.up.sql",
                sql_sha256=SHA_A,
                domain="native",
                domain_version=1,
                kind="native",
                dependencies=(),
                owned_objects=tuple(
                    OwnedSchemaObject("table", "bounded", SHA_A)
                    for _ in range(MAX_OWNED_SCHEMA_OBJECTS + 1)
                ),
            )

        infinite = CountingIntegers()
        with self.assertRaisesRegex(ValueError, "migration dependencies exceeds"):
            DomainMigrationDescriptor(
                migration_id=4,
                filename="0004_native.up.sql",
                sql_sha256=SHA_A,
                domain="native",
                domain_version=1,
                kind="native",
                # Deliberately bypass the tuple contract to exercise bounded consumption.
                dependencies=infinite,  # type: ignore[arg-type]
                owned_objects=(),
            )
        self.assertEqual(infinite.yielded, MAX_MIGRATION_DEPENDENCIES + 1)

    def test_digest_properties_validate_scalars_before_canonical_serialization(self) -> None:
        oversized_domain = DomainMigrationDescriptor(
            migration_id=4,
            filename="0004_native.up.sql",
            sql_sha256=SHA_A,
            domain="a" * 65,
            domain_version=1,
            kind="native",
            dependencies=(),
            owned_objects=(),
        )
        with mock.patch("quantum_entanglement.domain_migrations._canonical_sha256") as digest:
            with self.assertRaisesRegex(ValueError, "migration domain"):
                _ = oversized_domain.descriptor_sha256
            digest.assert_not_called()

        oversized_object = replace(
            self.descriptor(1),
            owned_objects=(OwnedSchemaObject("table", "a" * 129, SHA_A),),
        )
        with mock.patch("quantum_entanglement.domain_migrations._canonical_sha256") as digest:
            with self.assertRaisesRegex(ValueError, "schema object name"):
                _ = oversized_object.owned_object_manifest_sha256
            digest.assert_not_called()

    def test_sql_digest_and_packaged_bytes_are_tamper_evident(self) -> None:
        with self.assertRaisesRegex(ValueError, "SQL digest differs"):
            validate_domain_migration_registry(self.replacing(1, sql_sha256="0" * 64))
        original = self.descriptor(1)
        with mock.patch(
            "quantum_entanglement.domain_migrations.migration_text",
            return_value="-- altered package bytes\n",
        ):
            with self.assertRaisesRegex(ValueError, "SQL digest differs"):
                validate_domain_migration_registry(LEGACY_DOMAIN_MIGRATIONS)
        self.assertEqual(
            original.sql_sha256,
            hashlib.sha256(migration_text(original.filename).encode("utf-8")).hexdigest(),
        )

    def test_filename_and_legacy_manifest_tampering_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "filename differs"):
            validate_domain_migration_registry(self.replacing(1, filename="0099_tampered.up.sql"))
        owned = self.descriptor(1).owned_objects
        changed_owned = (replace(owned[0], ddl_sha256="0" * 64),) + owned[1:]
        with self.assertRaisesRegex(ValueError, "immutable bootstrap mapping"):
            validate_domain_migration_registry(self.replacing(1, owned_objects=changed_owned))

    def test_duplicate_migration_id_filename_and_domain_coordinate_are_rejected(self) -> None:
        first = self.descriptor(1)
        second = self.descriptor(2)
        cases = (
            (
                LEGACY_DOMAIN_MIGRATIONS + (replace(second, migration_id=1),),
                "duplicate migration ID",
            ),
            (
                self.replacing(2, filename=first.filename),
                "duplicate migration filename",
            ),
            (
                self.replacing(
                    2,
                    domain=first.domain,
                    domain_version=first.domain_version,
                ),
                "duplicate domain migration coordinate",
            ),
        )
        for descriptors, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    validate_domain_migration_registry(descriptors)

    def test_domain_versions_must_be_contiguous_from_one(self) -> None:
        with self.assertRaisesRegex(ValueError, "continuous prefix starting at one"):
            validate_domain_migration_registry(self.replacing(1, domain_version=2))

    def test_unknown_self_and_cyclic_dependencies_are_rejected(self) -> None:
        cases = (
            (self.replacing(1, dependencies=(999,)), "unknown dependencies"),
            (self.replacing(1, dependencies=(1,)), "cannot depend on itself"),
            (
                tuple(
                    replace(item, dependencies=(2,))
                    if item.migration_id == 1
                    else replace(item, dependencies=(1,))
                    if item.migration_id == 2
                    else item
                    for item in LEGACY_DOMAIN_MIGRATIONS
                ),
                "must be acyclic",
            ),
        )
        for descriptors, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    validate_domain_migration_registry(descriptors)

    def test_duplicate_dependencies_and_owned_objects_are_rejected(self) -> None:
        first = self.descriptor(1)
        with self.assertRaisesRegex(ValueError, "duplicate dependencies"):
            validate_domain_migration_registry(self.replacing(1, dependencies=(2, 2)))
        with self.assertRaisesRegex(ValueError, "duplicate owned object"):
            validate_domain_migration_registry(
                self.replacing(1, owned_objects=first.owned_objects + (first.owned_objects[0],))
            )

    def test_bool_is_not_accepted_as_an_integer(self) -> None:
        with self.assertRaisesRegex(TypeError, "migration ID must be an integer"):
            validate_domain_migration_registry(self.replacing(1, migration_id=True))
        with self.assertRaisesRegex(TypeError, "domain version must be an integer"):
            validate_domain_migration_registry(self.replacing(1, domain_version=True))
        with self.assertRaisesRegex(TypeError, "dependency must be an integer"):
            validate_domain_migration_registry(self.replacing(1, dependencies=(True,)))

    def test_names_and_hashes_must_be_bounded_and_canonical(self) -> None:
        first = self.descriptor(1)
        cases = (
            (self.replacing(1, domain="a" * 65), "migration domain"),
            (self.replacing(1, domain="Not_Canonical"), "migration domain"),
            (
                self.replacing(1, filename=("a" * 249) + ".up.sql"),
                "migration filename",
            ),
            (self.replacing(1, filename="../escape.up.sql"), "migration filename"),
            (self.replacing(1, sql_sha256="A" * 64), "lowercase hexadecimal"),
            (
                self.replacing(
                    1,
                    owned_objects=(replace(first.owned_objects[0], name="a" * 129),),
                ),
                "schema object name",
            ),
        )
        for descriptors, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    validate_domain_migration_registry(descriptors)

    def test_collection_limits_are_enforced_before_later_validation(self) -> None:
        first = self.descriptor(1)
        with self.assertRaisesRegex(ValueError, "domain migration descriptors exceeds"):
            validate_domain_migration_registry(itertools.repeat(first, MAX_DOMAIN_MIGRATIONS + 1))
        with self.assertRaisesRegex(ValueError, "dependencies exceeds"):
            validate_domain_migration_registry(
                self.replacing(
                    1,
                    dependencies=tuple(range(1, MAX_MIGRATION_DEPENDENCIES + 2)),
                )
            )
        with self.assertRaisesRegex(ValueError, "owned schema objects exceeds"):
            validate_domain_migration_registry(
                self.replacing(
                    1,
                    owned_objects=tuple(
                        OwnedSchemaObject("table", f"object_{index}", SHA_A)
                        for index in range(MAX_OWNED_SCHEMA_OBJECTS + 1)
                    ),
                )
            )

    def test_domain_count_has_a_hard_limit(self) -> None:
        descriptors = tuple(
            DomainMigrationDescriptor(
                migration_id=index + 1,
                filename=f"{index + 1:04d}_bounded.up.sql",
                sql_sha256=SHA_A,
                domain=f"domain_{index}",
                domain_version=1,
                kind="native",
                dependencies=(),
                owned_objects=(),
            )
            for index in range(MAX_MIGRATION_DOMAINS + 1)
        )
        with self.assertRaisesRegex(ValueError, "hard limit.*domains"):
            validate_domain_migration_registry(descriptors)


class DomainMigrationSidecarSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)

    def tearDown(self) -> None:
        self.connection.close()

    def create_exact_sidecar(self) -> None:
        for statement in DOMAIN_MIGRATION_SIDECAR_DDL:
            self.connection.execute(statement)

    def test_exact_sidecar_is_accepted_without_writes(self) -> None:
        self.create_exact_sidecar()
        self.connection.row_factory = sqlite3.Row
        before_changes = self.connection.total_changes

        state = validate_domain_migration_sidecar_schema(self.connection)

        self.assertIs(state, DomainMigrationSidecarSchemaState.EXACT)
        self.assertEqual(self.connection.total_changes, before_changes)
        rows = self.connection.execute(
            """
            SELECT type, name, tbl_name, sql
            FROM main.sqlite_master
            WHERE name IN (?, ?)
               OR tbl_name IN (?, ?)
            ORDER BY type, name, tbl_name
            """,
            (
                "qe_schema_migration_metadata",
                "qe_schema_migration_dependencies",
                "qe_schema_migration_metadata",
                "qe_schema_migration_dependencies",
            ),
        ).fetchall()
        self.assertEqual(len(rows), 4)
        self.assertEqual(
            tuple((row[0], row[1], row[2]) for row in rows),
            (
                (
                    "index",
                    "sqlite_autoindex_qe_schema_migration_dependencies_1",
                    "qe_schema_migration_dependencies",
                ),
                (
                    "index",
                    "sqlite_autoindex_qe_schema_migration_metadata_1",
                    "qe_schema_migration_metadata",
                ),
                (
                    "table",
                    "qe_schema_migration_dependencies",
                    "qe_schema_migration_dependencies",
                ),
                (
                    "table",
                    "qe_schema_migration_metadata",
                    "qe_schema_migration_metadata",
                ),
            ),
        )

    def test_sidecar_schema_definition_digest_is_golden(self) -> None:
        self.assertEqual(
            DOMAIN_MIGRATION_SIDECAR_SCHEMA_SHA256,
            "6d8c9e2672f3f14f6e1721f3faa29ee6f7656dcd8f028524d19c54ad7e24aabb",
        )

    def test_legacy_database_without_sidecar_is_a_supported_absent_state(self) -> None:
        self.assertEqual(
            apply_sqlite_migrations(self.connection, target_versions=(1,)),
            1,
        )
        before_changes = self.connection.total_changes

        state = validate_domain_migration_sidecar_schema(self.connection)

        self.assertIs(state, DomainMigrationSidecarSchemaState.ABSENT)
        self.assertEqual(self.connection.total_changes, before_changes)

    def test_sidecar_pair_must_be_both_absent_or_both_exact(self) -> None:
        self.connection.execute(DOMAIN_MIGRATION_SIDECAR_DDL[0])

        with self.assertRaisesRegex(
            DomainMigrationSidecarSchemaError,
            "both absent or both exact",
        ):
            validate_domain_migration_sidecar_schema(self.connection)

    def test_weak_sidecar_tables_are_rejected(self) -> None:
        self.connection.execute(
            """
            CREATE TABLE qe_schema_migration_metadata (
                migration_version INTEGER PRIMARY KEY
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE qe_schema_migration_dependencies (
                migration_version INTEGER,
                depends_on_version INTEGER
            )
            """
        )

        with self.assertRaisesRegex(
            DomainMigrationSidecarSchemaError,
            "differs from the exact packaged definition",
        ):
            validate_domain_migration_sidecar_schema(self.connection)

    def test_views_cannot_shadow_sidecar_tables(self) -> None:
        self.connection.execute(
            "CREATE VIEW qe_schema_migration_metadata AS SELECT 1 AS migration_version"
        )
        self.connection.execute(
            "CREATE VIEW qe_schema_migration_dependencies AS SELECT 1 AS migration_version"
        )

        with self.assertRaisesRegex(
            DomainMigrationSidecarSchemaError,
            "both absent or both exact",
        ):
            validate_domain_migration_sidecar_schema(self.connection)

    def test_unexpected_sidecar_index_is_rejected(self) -> None:
        self.create_exact_sidecar()
        self.connection.execute(
            """
            CREATE INDEX idx_qe_metadata_unexpected
            ON qe_schema_migration_metadata(domain)
            """
        )

        with self.assertRaisesRegex(
            DomainMigrationSidecarSchemaError,
            "differs from the exact packaged definition",
        ):
            validate_domain_migration_sidecar_schema(self.connection)

    def test_malformed_catalog_sql_is_rejected_without_echoing_it(self) -> None:
        self.create_exact_sidecar()
        malformed = "CREATE TABLE qe_schema_migration_metadata (this is not valid SQL"

        def corrupt_catalog_sql(
            _cursor: sqlite3.Cursor,
            row: tuple[object, ...],
        ) -> tuple[object, ...]:
            if row[1] == "qe_schema_migration_metadata":
                return (row[0], row[1], row[2], malformed)
            return row

        self.connection.row_factory = corrupt_catalog_sql
        try:
            with self.assertRaisesRegex(
                DomainMigrationSidecarSchemaError,
                "differs from the exact packaged definition",
            ) as raised:
                validate_domain_migration_sidecar_schema(self.connection)
        finally:
            self.connection.row_factory = None
        self.assertNotIn(malformed, str(raised.exception))

    def test_sidecar_catalog_has_a_hard_object_limit(self) -> None:
        self.create_exact_sidecar()
        expected_object_count = 4
        extra_count = MAX_DOMAIN_MIGRATION_SIDECAR_SCHEMA_OBJECTS - expected_object_count + 1
        for index in range(extra_count):
            self.connection.execute(
                f"""
                CREATE INDEX idx_qe_metadata_extra_{index}
                ON qe_schema_migration_metadata(domain)
                """
            )

        with self.assertRaisesRegex(
            DomainMigrationSidecarSchemaError,
            "exceeds the inspection limit",
        ):
            validate_domain_migration_sidecar_schema(self.connection)

    def test_validator_uses_one_parameterized_catalog_query(self) -> None:
        self.create_exact_sidecar()
        recording = RecordingConnection(self.connection)

        state = validate_domain_migration_sidecar_schema(cast(sqlite3.Connection, recording))

        self.assertIs(state, DomainMigrationSidecarSchemaState.EXACT)
        self.assertEqual(len(recording.calls), 1)
        statement, parameters = recording.calls[0]
        self.assertEqual(statement.count("?"), 5)
        self.assertEqual(len(parameters), 5)
        for parameter in parameters[:4]:
            self.assertNotIn(str(parameter), statement)

    def test_validator_remains_read_only_under_a_write_deny_authorizer(self) -> None:
        self.create_exact_sidecar()
        denied_actions: list[int] = []
        write_actions = {
            sqlite3.SQLITE_ALTER_TABLE,
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

        def authorize(
            action: int,
            _argument_one: Optional[str],
            _argument_two: Optional[str],
            _database_name: Optional[str],
            _trigger_name: Optional[str],
        ) -> int:
            if action in write_actions:
                denied_actions.append(action)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        self.connection.set_authorizer(authorize)
        try:
            state = validate_domain_migration_sidecar_schema(self.connection)
        finally:
            self.connection.set_authorizer(None)

        self.assertIs(state, DomainMigrationSidecarSchemaState.EXACT)
        self.assertEqual(denied_actions, [])

    def test_temp_sidecar_names_do_not_shadow_main_catalog(self) -> None:
        for statement in DOMAIN_MIGRATION_SIDECAR_DDL:
            self.connection.execute(statement.replace("CREATE TABLE", "CREATE TEMP TABLE", 1))

        self.assertIs(
            validate_domain_migration_sidecar_schema(self.connection),
            DomainMigrationSidecarSchemaState.ABSENT,
        )

    def test_catalog_inspection_failures_use_the_stable_schema_error(self) -> None:
        self.connection.close()

        with self.assertRaisesRegex(
            DomainMigrationSidecarSchemaError,
            "catalog could not be inspected",
        ):
            validate_domain_migration_sidecar_schema(self.connection)


class DomainMigrationSidecarInstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tempdir.name) / "sidecar.sqlite3")
        self.connection = sqlite3.connect(
            self.path,
            isolation_level=None,
            timeout=1,
        )

    def tearDown(self) -> None:
        self.connection.close()
        self.tempdir.cleanup()

    @staticmethod
    def normalize_statement(statement: str) -> str:
        return " ".join(statement.strip().rstrip(";").split())

    def create_exact_sidecar(self, connection: sqlite3.Connection) -> None:
        for statement in DOMAIN_MIGRATION_SIDECAR_DDL:
            connection.execute(statement)

    def assert_write_lock_available(self) -> None:
        contender = sqlite3.connect(
            self.path,
            isolation_level=None,
            timeout=0.1,
        )
        try:
            contender.execute("BEGIN IMMEDIATE")
            self.assertTrue(contender.in_transaction)
            contender.execute("ROLLBACK")
        finally:
            contender.close()

    def test_absent_install_is_atomic_exact_and_then_idempotently_read_only(self) -> None:
        traced: list[str] = []
        self.connection.set_trace_callback(traced.append)
        try:
            first = install_domain_migration_sidecar(self.connection)
            first_trace = tuple(traced)
            traced.clear()
            second = install_domain_migration_sidecar(self.connection)
            second_trace = tuple(traced)
        finally:
            self.connection.set_trace_callback(None)

        self.assertIs(first, DomainMigrationSidecarSchemaState.EXACT)
        self.assertIs(second, DomainMigrationSidecarSchemaState.EXACT)
        writes_and_transactions = tuple(
            normalized
            for statement in first_trace
            if (normalized := self.normalize_statement(statement)).upper()
            in {"BEGIN IMMEDIATE", "COMMIT", "ROLLBACK"}
            or normalized.upper().startswith("CREATE TABLE ")
        )
        self.assertEqual(
            writes_and_transactions,
            (
                "BEGIN IMMEDIATE",
                self.normalize_statement(DOMAIN_MIGRATION_SIDECAR_DDL[0]),
                self.normalize_statement(DOMAIN_MIGRATION_SIDECAR_DDL[1]),
                "COMMIT",
            ),
        )
        self.assertFalse(
            any(
                self.normalize_statement(statement).upper()
                in {"BEGIN IMMEDIATE", "COMMIT", "ROLLBACK"}
                or self.normalize_statement(statement).upper().startswith("CREATE TABLE ")
                for statement in second_trace
            )
        )
        counts = self.connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM main.qe_schema_migration_metadata),
                (SELECT COUNT(*) FROM main.qe_schema_migration_dependencies)
            """
        ).fetchone()
        self.assertIsNotNone(counts)
        self.assertEqual(tuple(counts), (0, 0))

    def test_installer_authorizer_observes_only_expected_schema_and_catalog_writes(self) -> None:
        actions: list[tuple[int, Optional[str], Optional[str]]] = []

        def authorize(
            action: int,
            argument_one: Optional[str],
            argument_two: Optional[str],
            _database_name: Optional[str],
            _trigger_name: Optional[str],
        ) -> int:
            actions.append((action, argument_one, argument_two))
            return sqlite3.SQLITE_OK

        self.connection.set_authorizer(authorize)
        try:
            state = install_domain_migration_sidecar(self.connection)
        finally:
            self.connection.set_authorizer(None)

        self.assertIs(state, DomainMigrationSidecarSchemaState.EXACT)
        self.assertEqual(
            {
                argument_one
                for action, argument_one, _argument_two in actions
                if action == sqlite3.SQLITE_CREATE_TABLE
            },
            {
                DOMAIN_MIGRATION_METADATA_TABLE_NAME,
                DOMAIN_MIGRATION_DEPENDENCIES_TABLE_NAME,
            },
        )
        self.assertEqual(
            [
                argument_one
                for action, argument_one, _argument_two in actions
                if action == sqlite3.SQLITE_TRANSACTION
            ],
            ["BEGIN", "COMMIT"],
        )
        application_dml = [
            (action, argument_one)
            for action, argument_one, _argument_two in actions
            if action in {sqlite3.SQLITE_DELETE, sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE}
            and argument_one != "sqlite_master"
        ]
        self.assertEqual(application_dml, [])

    def test_active_caller_transaction_is_rejected_and_left_untouched(self) -> None:
        self.connection.execute("BEGIN")
        self.connection.execute("CREATE TABLE caller_pending (value TEXT)")
        traced: list[str] = []
        self.connection.set_trace_callback(traced.append)
        try:
            with self.assertRaisesRegex(
                DomainMigrationSidecarInstallError,
                "requires no active caller transaction",
            ):
                install_domain_migration_sidecar(self.connection)
        finally:
            self.connection.set_trace_callback(None)

        self.assertTrue(self.connection.in_transaction)
        self.assertEqual(traced, [])
        self.assertIsNotNone(
            self.connection.execute(
                "SELECT 1 FROM main.sqlite_master WHERE type = ? AND name = ?",
                ("table", "caller_pending"),
            ).fetchone()
        )
        self.connection.execute("ROLLBACK")
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM main.sqlite_master WHERE type = ? AND name = ?",
                ("table", "caller_pending"),
            ).fetchone()
        )

    def test_partial_schema_fails_before_begin_immediate(self) -> None:
        self.connection.execute(DOMAIN_MIGRATION_SIDECAR_DDL[0])
        traced: list[str] = []
        self.connection.set_trace_callback(traced.append)
        try:
            with self.assertRaisesRegex(
                DomainMigrationSidecarSchemaError,
                "both absent or both exact",
            ):
                install_domain_migration_sidecar(self.connection)
        finally:
            self.connection.set_trace_callback(None)

        self.assertFalse(self.connection.in_transaction)
        self.assertFalse(
            any(self.normalize_statement(item).upper() == "BEGIN IMMEDIATE" for item in traced)
        )

    def test_weak_schema_fails_before_begin_immediate(self) -> None:
        self.connection.execute(
            "CREATE TABLE qe_schema_migration_metadata (migration_version INTEGER)"
        )
        self.connection.execute(
            "CREATE TABLE qe_schema_migration_dependencies (migration_version INTEGER)"
        )
        traced: list[str] = []
        self.connection.set_trace_callback(traced.append)
        try:
            with self.assertRaisesRegex(
                DomainMigrationSidecarSchemaError,
                "differs from the exact packaged definition",
            ):
                install_domain_migration_sidecar(self.connection)
        finally:
            self.connection.set_trace_callback(None)

        self.assertFalse(self.connection.in_transaction)
        self.assertFalse(
            any(self.normalize_statement(item).upper() == "BEGIN IMMEDIATE" for item in traced)
        )

    def test_future_extra_object_fails_before_begin_immediate(self) -> None:
        self.create_exact_sidecar(self.connection)
        self.connection.execute(
            """
            CREATE INDEX idx_qe_future_sidecar
            ON qe_schema_migration_metadata(domain)
            """
        )
        traced: list[str] = []
        self.connection.set_trace_callback(traced.append)
        try:
            with self.assertRaisesRegex(
                DomainMigrationSidecarSchemaError,
                "differs from the exact packaged definition",
            ):
                install_domain_migration_sidecar(self.connection)
        finally:
            self.connection.set_trace_callback(None)

        self.assertFalse(self.connection.in_transaction)
        self.assertFalse(
            any(self.normalize_statement(item).upper() == "BEGIN IMMEDIATE" for item in traced)
        )

    def test_second_ddl_failure_rolls_back_first_table_and_releases_lock(self) -> None:
        wrapped = InjectedFailureConnection(
            self.connection,
            target_statement=DOMAIN_MIGRATION_SIDECAR_DDL[1],
            error=sqlite3.OperationalError("injected dependency DDL failure"),
        )

        with self.assertRaisesRegex(sqlite3.OperationalError, "dependency DDL failure"):
            install_domain_migration_sidecar(cast(sqlite3.Connection, wrapped))

        self.assertTrue(wrapped.failed)
        self.assertFalse(self.connection.in_transaction)
        self.assertIs(
            validate_domain_migration_sidecar_schema(self.connection),
            DomainMigrationSidecarSchemaState.ABSENT,
        )
        self.assert_write_lock_available()
        self.assertIs(
            install_domain_migration_sidecar(self.connection),
            DomainMigrationSidecarSchemaState.EXACT,
        )

    def test_base_exception_rolls_back_partial_ddl_and_releases_lock(self) -> None:
        wrapped = InjectedFailureConnection(
            self.connection,
            target_statement=DOMAIN_MIGRATION_SIDECAR_DDL[1],
            error=KeyboardInterrupt("injected sidecar interrupt"),
        )

        with self.assertRaisesRegex(KeyboardInterrupt, "sidecar interrupt"):
            install_domain_migration_sidecar(cast(sqlite3.Connection, wrapped))

        self.assertFalse(self.connection.in_transaction)
        self.assertIs(
            validate_domain_migration_sidecar_schema(self.connection),
            DomainMigrationSidecarSchemaState.ABSENT,
        )
        self.assert_write_lock_available()

    def test_post_begin_exception_still_rolls_back_the_acquired_transaction(self) -> None:
        wrapped = PostBeginFailureConnection(self.connection)

        with self.assertRaisesRegex(KeyboardInterrupt, "post-BEGIN interrupt"):
            install_domain_migration_sidecar(cast(sqlite3.Connection, wrapped))

        self.assertFalse(self.connection.in_transaction)
        self.assertIs(
            validate_domain_migration_sidecar_schema(self.connection),
            DomainMigrationSidecarSchemaState.ABSENT,
        )
        self.assertIn(
            "ROLLBACK",
            tuple(statement.strip().upper() for statement, _parameters in wrapped.calls),
        )
        self.assert_write_lock_available()

    def test_commit_failure_rolls_back_both_tables_and_releases_lock(self) -> None:
        wrapped = InjectedFailureConnection(
            self.connection,
            target_statement="COMMIT",
            error=sqlite3.OperationalError("injected sidecar commit failure"),
        )

        with self.assertRaisesRegex(sqlite3.OperationalError, "commit failure"):
            install_domain_migration_sidecar(cast(sqlite3.Connection, wrapped))

        self.assertTrue(wrapped.failed)
        self.assertFalse(self.connection.in_transaction)
        self.assertIs(
            validate_domain_migration_sidecar_schema(self.connection),
            DomainMigrationSidecarSchemaState.ABSENT,
        )
        self.assertIn(
            "ROLLBACK",
            tuple(statement.strip().upper() for statement, _parameters in wrapped.calls),
        )
        self.assert_write_lock_available()

    def test_two_absent_preflights_serialize_and_only_one_creates(self) -> None:
        barrier = threading.Barrier(2)
        guard = threading.Lock()
        results: list[DomainMigrationSidecarSchemaState] = []
        failures: list[BaseException] = []
        call_logs: list[tuple[str, ...]] = []

        def worker() -> None:
            connection = sqlite3.connect(
                self.path,
                isolation_level=None,
                timeout=5,
            )
            wrapped = BeginBarrierConnection(connection, barrier)
            try:
                state = install_domain_migration_sidecar(cast(sqlite3.Connection, wrapped))
                with guard:
                    results.append(state)
            except BaseException as error:  # pragma: no cover - assertion reports details.
                with guard:
                    failures.append(error)
            finally:
                with guard:
                    call_logs.append(tuple(statement for statement, _parameters in wrapped.calls))
                connection.close()

        threads = [
            threading.Thread(target=worker, daemon=True, name=f"sidecar-installer-{index}")
            for index in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(failures, [])
        self.assertEqual(
            results,
            [
                DomainMigrationSidecarSchemaState.EXACT,
                DomainMigrationSidecarSchemaState.EXACT,
            ],
        )
        flattened = tuple(statement for call_log in call_logs for statement in call_log)
        for statement in DOMAIN_MIGRATION_SIDECAR_DDL:
            self.assertEqual(flattened.count(statement), 1)
        self.assertEqual(
            sum(item.strip().upper() == "BEGIN IMMEDIATE" for item in flattened),
            2,
        )
        self.assertEqual(sum(item.strip().upper() == "COMMIT" for item in flattened), 2)
        self.assertNotIn("ROLLBACK", tuple(item.strip().upper() for item in flattened))
        self.assertIs(
            validate_domain_migration_sidecar_schema(self.connection),
            DomainMigrationSidecarSchemaState.EXACT,
        )
        self.assert_write_lock_available()


class DomainMigrationBridgeStateTests(unittest.TestCase):
    LEDGER_QUERY_MARKER = (
        "SELECT version, filename, sha256, applied_at FROM main.qe_schema_migrations"
    )
    METADATA_QUERY_MARKER = (
        "SELECT migration_version, domain, domain_version, metadata_kind, "
        "descriptor_sha256, owned_schema_sha256, recorded_at "
        "FROM main.qe_schema_migration_metadata"
    )
    DEPENDENCY_QUERY_MARKER = (
        "SELECT migration_version, depends_on_version FROM main.qe_schema_migration_dependencies"
    )

    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)

    def tearDown(self) -> None:
        self.connection.close()

    def create_legacy_prefix(self, count: int) -> None:
        versions = tuple(item.migration_id for item in LEGACY_DOMAIN_MIGRATIONS[:count])
        self.assertEqual(
            apply_sqlite_migrations(
                self.connection,
                target_versions=versions,
                clock=lambda: BRIDGE_TIME,
            ),
            versions[-1] if versions else 0,
        )

    def create_exact_sidecar(self) -> None:
        self.assertIs(
            install_domain_migration_sidecar(self.connection),
            DomainMigrationSidecarSchemaState.EXACT,
        )

    def insert_exact_metadata(self, count: int) -> None:
        for descriptor in DOMAIN_MIGRATION_REGISTRY.descriptors[:count]:
            self.connection.execute(
                """
                INSERT INTO main.qe_schema_migration_metadata (
                    migration_version,
                    domain,
                    domain_version,
                    metadata_kind,
                    descriptor_sha256,
                    owned_schema_sha256,
                    recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    descriptor.migration_id,
                    descriptor.domain,
                    descriptor.domain_version,
                    "legacy_bootstrap",
                    descriptor.descriptor_sha256,
                    descriptor.owned_object_manifest_sha256,
                    BRIDGE_TIME,
                ),
            )

    @staticmethod
    def fake_connection(
        connection: sqlite3.Connection,
        marker: str,
        rows: Iterable[object],
    ) -> sqlite3.Connection:
        return cast(
            sqlite3.Connection,
            FakeQueryConnection(connection, {marker: rows}),
        )

    def test_all_bridge_shapes_are_immutable_registry_bound_snapshots(self) -> None:
        empty = read_domain_migration_bridge_state(self.connection)
        self.assertEqual(
            empty,
            DomainMigrationBridgeState(
                shape=DomainMigrationBridgeShape.LEGACY_PREFIX,
                legacy_schema_version=0,
                ledger_rows=(),
                metadata_rows=(),
                dependency_rows=(),
                registry_sha256=DOMAIN_MIGRATION_REGISTRY.registry_sha256,
            ),
        )

        self.create_legacy_prefix(2)
        legacy = read_domain_migration_bridge_state(self.connection)
        self.assertIs(legacy.shape, DomainMigrationBridgeShape.LEGACY_PREFIX)
        self.assertEqual(legacy.legacy_schema_version, 2)
        self.assertEqual(
            legacy.ledger_rows,
            tuple(
                DomainMigrationLedgerRow(
                    descriptor.migration_id,
                    descriptor.filename,
                    descriptor.sql_sha256,
                    BRIDGE_TIME,
                )
                for descriptor in DOMAIN_MIGRATION_REGISTRY.descriptors[:2]
            ),
        )

        self.create_exact_sidecar()
        sidecar_empty = read_domain_migration_bridge_state(self.connection)
        self.assertIs(
            sidecar_empty.shape,
            DomainMigrationBridgeShape.SIDECAR_EMPTY_PREFIX,
        )
        self.assertEqual(sidecar_empty.metadata_rows, ())
        self.assertEqual(sidecar_empty.dependency_rows, ())

        self.insert_exact_metadata(2)
        bridged = read_domain_migration_bridge_state(self.connection)
        self.assertIs(bridged.shape, DomainMigrationBridgeShape.BRIDGED_PREFIX)
        self.assertEqual(
            bridged.metadata_rows,
            tuple(
                DomainMigrationMetadataRow(
                    descriptor.migration_id,
                    descriptor.domain,
                    descriptor.domain_version,
                    "legacy_bootstrap",
                    descriptor.descriptor_sha256,
                    descriptor.owned_object_manifest_sha256,
                    BRIDGE_TIME,
                )
                for descriptor in DOMAIN_MIGRATION_REGISTRY.descriptors[:2]
            ),
        )
        self.assertEqual(bridged.dependency_rows, ())
        self.assertEqual(
            bridged.registry_sha256,
            DOMAIN_MIGRATION_REGISTRY.registry_sha256,
        )
        with self.assertRaises((AttributeError, TypeError)):
            bridged.legacy_schema_version = 3  # type: ignore[misc]

    def test_exact_empty_sidecar_without_a_legacy_ledger_is_supported(self) -> None:
        self.create_exact_sidecar()

        state = read_domain_migration_bridge_state(self.connection)

        self.assertIs(state.shape, DomainMigrationBridgeShape.SIDECAR_EMPTY_PREFIX)
        self.assertEqual(state.legacy_schema_version, 0)
        self.assertEqual(state.ledger_rows, ())

    def test_partial_extra_and_unknown_metadata_are_rejected(self) -> None:
        self.create_legacy_prefix(2)
        self.create_exact_sidecar()
        self.insert_exact_metadata(1)
        with self.assertRaisesRegex(
            DomainMigrationBridgeIntegrityError,
            "does not exactly cover",
        ):
            read_domain_migration_bridge_state(self.connection)

        self.connection.execute(
            """
            INSERT INTO main.qe_schema_migration_metadata (
                migration_version, domain, domain_version, metadata_kind,
                descriptor_sha256, owned_schema_sha256, recorded_at
            ) VALUES (999, 'future', 1, 'legacy_bootstrap', ?, ?, ?)
            """,
            (
                "0" * 64,
                "1" * 64,
                BRIDGE_TIME,
            ),
        )
        with self.assertRaisesRegex(
            DomainMigrationBridgeIntegrityError,
            "does not exactly cover",
        ):
            read_domain_migration_bridge_state(self.connection)

    def test_every_registry_controlled_metadata_field_must_match_exactly(self) -> None:
        self.create_legacy_prefix(1)
        self.create_exact_sidecar()
        self.insert_exact_metadata(1)
        descriptor = DOMAIN_MIGRATION_REGISTRY.descriptors[0]
        cases = (
            ("domain", "changed", descriptor.domain),
            ("domain_version", 2, descriptor.domain_version),
            ("metadata_kind", "native", "legacy_bootstrap"),
            ("descriptor_sha256", "0" * 64, descriptor.descriptor_sha256),
            (
                "owned_schema_sha256",
                "0" * 64,
                descriptor.owned_object_manifest_sha256,
            ),
        )
        for column, drifted, exact in cases:
            with self.subTest(column=column):
                self.connection.execute(
                    f"""
                    UPDATE main.qe_schema_migration_metadata
                    SET {column} = ?
                    WHERE migration_version = 1
                    """,
                    (drifted,),
                )
                with self.assertRaisesRegex(
                    DomainMigrationBridgeIntegrityError,
                    "exact registry descriptor",
                ):
                    read_domain_migration_bridge_state(self.connection)
                self.connection.execute(
                    f"""
                    UPDATE main.qe_schema_migration_metadata
                    SET {column} = ?
                    WHERE migration_version = 1
                    """,
                    (exact,),
                )

    def test_dependency_drift_and_unapplied_endpoints_are_rejected(self) -> None:
        self.create_legacy_prefix(2)
        self.create_exact_sidecar()
        self.insert_exact_metadata(2)
        self.connection.execute(
            """
            INSERT INTO main.qe_schema_migration_dependencies (
                migration_version, depends_on_version
            ) VALUES (2, 1)
            """
        )
        with self.assertRaisesRegex(
            DomainMigrationBridgeIntegrityError,
            "exact registry edges",
        ):
            read_domain_migration_bridge_state(self.connection)

        self.connection.execute("DELETE FROM main.qe_schema_migration_dependencies")
        self.connection.execute(
            """
            INSERT INTO main.qe_schema_migration_dependencies (
                migration_version, depends_on_version
            ) VALUES (1, 3)
            """
        )
        with self.assertRaisesRegex(
            DomainMigrationBridgeIntegrityError,
            "unapplied ledger row",
        ):
            read_domain_migration_bridge_state(self.connection)

    def test_empty_metadata_cannot_hide_dependency_rows(self) -> None:
        self.create_legacy_prefix(1)
        self.create_exact_sidecar()
        self.connection.execute(
            """
            INSERT INTO main.qe_schema_migration_dependencies (
                migration_version, depends_on_version
            ) VALUES (1, 2)
            """
        )

        with self.assertRaisesRegex(
            DomainMigrationBridgeIntegrityError,
            "empty.*metadata.*dependency",
        ):
            read_domain_migration_bridge_state(self.connection)

    def test_ledger_holes_future_rows_and_registry_drift_are_rejected(self) -> None:
        self.create_legacy_prefix(2)
        self.connection.execute("DELETE FROM main.qe_schema_migrations WHERE version = 1")
        with self.assertRaisesRegex(
            DomainMigrationBridgeIntegrityError,
            "continuous supported registry prefix",
        ):
            read_domain_migration_bridge_state(self.connection)

        self.connection.execute("DELETE FROM main.qe_schema_migrations")
        first = DOMAIN_MIGRATION_REGISTRY.descriptors[0]
        self.connection.execute(
            """
            INSERT INTO main.qe_schema_migrations (
                version, filename, sha256, applied_at
            ) VALUES (?, ?, ?, ?)
            """,
            (first.migration_id, first.filename, first.sql_sha256, BRIDGE_TIME),
        )
        self.connection.execute(
            """
            INSERT INTO main.qe_schema_migrations (
                version, filename, sha256, applied_at
            ) VALUES (999, '0999_future.up.sql', ?, ?)
            """,
            ("0" * 64, BRIDGE_TIME),
        )
        with self.assertRaisesRegex(
            DomainMigrationBridgeIntegrityError,
            "continuous supported registry prefix",
        ):
            read_domain_migration_bridge_state(self.connection)

        self.connection.execute("DELETE FROM main.qe_schema_migrations WHERE version = 999")
        for column, value in (
            ("filename", "0001_changed.up.sql"),
            ("sha256", "0" * 64),
        ):
            with self.subTest(column=column):
                self.connection.execute(
                    f"UPDATE main.qe_schema_migrations SET {column} = ? WHERE version = 1",
                    (value,),
                )
                with self.assertRaisesRegex(
                    DomainMigrationBridgeIntegrityError,
                    "filename or SQL digest",
                ):
                    read_domain_migration_bridge_state(self.connection)
                exact = first.filename if column == "filename" else first.sql_sha256
                self.connection.execute(
                    f"UPDATE main.qe_schema_migrations SET {column} = ? WHERE version = 1",
                    (exact,),
                )

    def test_weak_ledger_and_owned_schema_drift_use_the_stable_integrity_error(self) -> None:
        self.connection.execute(
            """
            CREATE TABLE qe_schema_migrations (
                version INTEGER,
                filename TEXT,
                sha256 TEXT,
                applied_at TEXT
            )
            """
        )
        first = DOMAIN_MIGRATION_REGISTRY.descriptors[0]
        self.connection.execute(
            "INSERT INTO qe_schema_migrations VALUES (?, ?, ?, ?)",
            (first.migration_id, first.filename, first.sql_sha256, BRIDGE_TIME),
        )
        with self.assertRaisesRegex(
            DomainMigrationBridgeIntegrityError,
            "ledger or owned schema is not exact",
        ):
            read_domain_migration_bridge_state(self.connection)

        self.connection.execute("DROP TABLE qe_schema_migrations")
        self.create_legacy_prefix(1)
        self.connection.execute("DROP INDEX idx_invocation_attempts_job")
        with self.assertRaisesRegex(
            DomainMigrationBridgeIntegrityError,
            "ledger or owned schema is not exact",
        ):
            read_domain_migration_bridge_state(self.connection)

    def test_known_owned_objects_cannot_exist_without_their_ledger_rows(self) -> None:
        self.connection.execute("CREATE TABLE invocation_jobs (value TEXT)")
        with self.assertRaisesRegex(
            DomainMigrationBridgeIntegrityError,
            "unapplied legacy registry migration",
        ):
            read_domain_migration_bridge_state(self.connection)

        self.connection.execute("DROP TABLE invocation_jobs")
        self.create_legacy_prefix(2)
        self.connection.execute("DELETE FROM main.qe_schema_migrations WHERE version = 2")
        with self.assertRaisesRegex(
            DomainMigrationBridgeIntegrityError,
            "unapplied legacy registry migration",
        ):
            read_domain_migration_bridge_state(self.connection)

    def test_partial_sidecar_is_classified_as_bridge_integrity_failure(self) -> None:
        self.create_legacy_prefix(1)
        self.connection.execute(DOMAIN_MIGRATION_SIDECAR_DDL[0])

        with self.assertRaisesRegex(
            DomainMigrationBridgeIntegrityError,
            "sidecar schema is not exact",
        ):
            read_domain_migration_bridge_state(self.connection)
        self.assertFalse(self.connection.in_transaction)

    def test_ledger_and_metadata_timestamps_must_be_canonical_utc(self) -> None:
        self.create_legacy_prefix(1)
        self.create_exact_sidecar()
        self.insert_exact_metadata(1)

        self.connection.execute(
            "UPDATE main.qe_schema_migrations SET applied_at = ? WHERE version = 1",
            ("2026-08-20T00:00:00.123456Z",),
        )
        self.connection.execute(
            """
            UPDATE main.qe_schema_migration_metadata
            SET recorded_at = ?
            WHERE migration_version = 1
            """,
            ("2026-08-20T00:00:00.654321Z",),
        )
        self.assertIs(
            read_domain_migration_bridge_state(self.connection).shape,
            DomainMigrationBridgeShape.BRIDGED_PREFIX,
        )

        invalid_timestamps = (
            "2026-08-20T00:00:00.000000Z",
            "2026-08-20T00:00:00.1Z",
            "2026-08-20T00:00:00+00:00",
            "2026-02-30T00:00:00Z",
            " 2026-08-20T00:00:00Z",
        )
        for table, column, key_column in (
            ("qe_schema_migrations", "applied_at", "version"),
            (
                "qe_schema_migration_metadata",
                "recorded_at",
                "migration_version",
            ),
        ):
            for timestamp in invalid_timestamps:
                with self.subTest(table=table, timestamp=timestamp):
                    self.connection.execute(
                        f"UPDATE main.{table} SET {column} = ? WHERE {key_column} = 1",
                        (timestamp,),
                    )
                    with self.assertRaisesRegex(
                        DomainMigrationBridgeIntegrityError,
                        "canonical RFC3339 UTC",
                    ):
                        read_domain_migration_bridge_state(self.connection)
                    self.connection.execute(
                        f"UPDATE main.{table} SET {column} = ? WHERE {key_column} = 1",
                        (BRIDGE_TIME,),
                    )

    def test_fake_ledger_values_are_never_dynamically_coerced(self) -> None:
        self.create_legacy_prefix(1)
        descriptor = DOMAIN_MIGRATION_REGISTRY.descriptors[0]
        valid = (
            descriptor.migration_id,
            descriptor.filename,
            descriptor.sql_sha256,
            BRIDGE_TIME,
        )
        cases = (
            ("1", valid[1], valid[2], valid[3]),
            (True, valid[1], valid[2], valid[3]),
            (1.0, valid[1], valid[2], valid[3]),
            (0, valid[1], valid[2], valid[3]),
            (2**63, valid[1], valid[2], valid[3]),
            (valid[0], TextSubclass(valid[1]), valid[2], valid[3]),
            (valid[0], "a" * 256, valid[2], valid[3]),
            (valid[0], valid[1], bytes(valid[2], "ascii"), valid[3]),
            (valid[0], valid[1], "A" * 64, valid[3]),
            (valid[0], valid[1], valid[2], TextSubclass(valid[3])),
            (valid[0], valid[1], valid[2], "2" * 33),
        )
        for row in cases:
            with self.subTest(value_types=tuple(type(item).__name__ for item in row)):
                fake = self.fake_connection(
                    self.connection,
                    self.LEDGER_QUERY_MARKER,
                    (row,),
                )
                with self.assertRaises(DomainMigrationBridgeIntegrityError):
                    read_domain_migration_bridge_state(fake)
                self.assertFalse(self.connection.in_transaction)

    def test_fake_metadata_and_dependency_values_require_exact_sqlite_types(self) -> None:
        self.create_legacy_prefix(1)
        self.create_exact_sidecar()
        descriptor = DOMAIN_MIGRATION_REGISTRY.descriptors[0]
        valid_metadata = (
            descriptor.migration_id,
            descriptor.domain,
            descriptor.domain_version,
            "legacy_bootstrap",
            descriptor.descriptor_sha256,
            descriptor.owned_object_manifest_sha256,
            BRIDGE_TIME,
        )
        metadata_cases = (
            (True,) + valid_metadata[1:],
            (valid_metadata[0], bytes(valid_metadata[1], "ascii")) + valid_metadata[2:],
            (valid_metadata[0], "A_domain") + valid_metadata[2:],
            (valid_metadata[0], "a" * 65) + valid_metadata[2:],
            valid_metadata[:2] + ("1",) + valid_metadata[3:],
            valid_metadata[:2] + (0,) + valid_metadata[3:],
            valid_metadata[:2] + (2**63,) + valid_metadata[3:],
            valid_metadata[:3] + (TextSubclass("legacy_bootstrap"),) + valid_metadata[4:],
            valid_metadata[:3] + ("future",) + valid_metadata[4:],
            valid_metadata[:4] + (bytes(valid_metadata[4], "ascii"),) + valid_metadata[5:],
            valid_metadata[:4] + ("A" * 64,) + valid_metadata[5:],
            valid_metadata[:5] + ("f" * 63,) + valid_metadata[6:],
            valid_metadata[:-1] + (TextSubclass(BRIDGE_TIME),),
            valid_metadata[:-1] + ("2" * 33,),
        )
        for row in metadata_cases:
            with self.subTest(value_types=tuple(type(item).__name__ for item in row)):
                fake = self.fake_connection(
                    self.connection,
                    self.METADATA_QUERY_MARKER,
                    (row,),
                )
                with self.assertRaises(DomainMigrationBridgeIntegrityError):
                    read_domain_migration_bridge_state(fake)

        self.insert_exact_metadata(1)
        for dependency_row in (
            (1.0, 2),
            (1, "2"),
            (True, 2),
            (0, 2),
            (1, 2**63),
        ):
            with self.subTest(dependency=dependency_row):
                fake = self.fake_connection(
                    self.connection,
                    self.DEPENDENCY_QUERY_MARKER,
                    (dependency_row,),
                )
                with self.assertRaises(DomainMigrationBridgeIntegrityError):
                    read_domain_migration_bridge_state(fake)

    def test_fake_row_column_shapes_are_rejected_before_indexing(self) -> None:
        self.create_legacy_prefix(1)
        descriptor = DOMAIN_MIGRATION_REGISTRY.descriptors[0]
        malformed_rows: tuple[object, ...] = (
            1,
            (descriptor.migration_id,),
            (
                descriptor.migration_id,
                descriptor.filename,
                descriptor.sql_sha256,
                BRIDGE_TIME,
                "unexpected",
            ),
        )
        for row in malformed_rows:
            with self.subTest(row_type=type(row).__name__):
                fake = self.fake_connection(
                    self.connection,
                    self.LEDGER_QUERY_MARKER,
                    (row,),
                )
                with self.assertRaisesRegex(
                    DomainMigrationBridgeIntegrityError,
                    "malformed column shape",
                ):
                    read_domain_migration_bridge_state(fake)

    def test_every_durable_query_is_parameter_bounded_and_fake_rows_stop_at_limit(self) -> None:
        self.create_legacy_prefix(1)
        self.create_exact_sidecar()
        self.insert_exact_metadata(1)
        recording = RecordingConnection(self.connection)

        state = read_domain_migration_bridge_state(cast(sqlite3.Connection, recording))

        self.assertIs(state.shape, DomainMigrationBridgeShape.BRIDGED_PREFIX)
        durable_markers = (
            self.LEDGER_QUERY_MARKER,
            self.METADATA_QUERY_MARKER,
            self.DEPENDENCY_QUERY_MARKER,
        )
        durable_calls = []
        for statement, parameters in recording.calls:
            normalized = " ".join(statement.split())
            if any(marker in normalized for marker in durable_markers):
                durable_calls.append((normalized, parameters))
        self.assertEqual(len(durable_calls), 3)
        for statement, parameters in durable_calls:
            self.assertIn("LIMIT ?", statement)
            self.assertEqual(parameters, (MAX_DOMAIN_MIGRATIONS + 1,))

        descriptor = DOMAIN_MIGRATION_REGISTRY.descriptors[0]
        rows_and_markers = (
            (
                CountingRows(
                    (
                        descriptor.migration_id,
                        descriptor.filename,
                        descriptor.sql_sha256,
                        BRIDGE_TIME,
                    )
                ),
                self.LEDGER_QUERY_MARKER,
            ),
            (
                CountingRows(
                    (
                        descriptor.migration_id,
                        descriptor.domain,
                        descriptor.domain_version,
                        "legacy_bootstrap",
                        descriptor.descriptor_sha256,
                        descriptor.owned_object_manifest_sha256,
                        BRIDGE_TIME,
                    )
                ),
                self.METADATA_QUERY_MARKER,
            ),
            (CountingRows((1, 2)), self.DEPENDENCY_QUERY_MARKER),
        )
        for rows, marker in rows_and_markers:
            with self.subTest(marker=marker):
                fake = self.fake_connection(self.connection, marker, rows)
                with self.assertRaisesRegex(
                    DomainMigrationBridgeIntegrityError,
                    "hard limit",
                ):
                    read_domain_migration_bridge_state(fake)
                self.assertEqual(rows.yielded, MAX_DOMAIN_MIGRATIONS + 1)

    def test_reader_is_read_only_under_a_write_deny_authorizer(self) -> None:
        self.create_legacy_prefix(1)
        self.create_exact_sidecar()
        self.insert_exact_metadata(1)
        denied_actions: list[int] = []
        write_actions = {
            sqlite3.SQLITE_ALTER_TABLE,
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

        def authorize(
            action: int,
            _argument_one: Optional[str],
            _argument_two: Optional[str],
            _database_name: Optional[str],
            _trigger_name: Optional[str],
        ) -> int:
            if action in write_actions:
                denied_actions.append(action)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        before_changes = self.connection.total_changes
        self.connection.set_authorizer(authorize)
        try:
            state = read_domain_migration_bridge_state(self.connection)
        finally:
            self.connection.set_authorizer(None)

        self.assertIs(state.shape, DomainMigrationBridgeShape.BRIDGED_PREFIX)
        self.assertEqual(denied_actions, [])
        self.assertEqual(self.connection.total_changes, before_changes)
        self.assertFalse(self.connection.in_transaction)

    def test_owned_snapshot_rolls_back_and_caller_transaction_is_untouched(self) -> None:
        self.create_legacy_prefix(1)
        traced: list[str] = []
        self.connection.set_trace_callback(traced.append)
        try:
            read_domain_migration_bridge_state(self.connection)
        finally:
            self.connection.set_trace_callback(None)
        transaction_statements = tuple(
            " ".join(statement.strip().split()).upper()
            for statement in traced
            if statement.strip().upper().startswith(("BEGIN", "COMMIT", "ROLLBACK"))
        )
        self.assertEqual(transaction_statements, ("BEGIN", "ROLLBACK"))
        self.assertFalse(self.connection.in_transaction)

        self.connection.execute("CREATE TABLE caller_state (value TEXT)")
        self.connection.execute("BEGIN")
        self.connection.execute("INSERT INTO caller_state VALUES ('pending')")
        recording = RecordingConnection(self.connection)
        state = read_domain_migration_bridge_state(cast(sqlite3.Connection, recording))
        self.assertIs(state.shape, DomainMigrationBridgeShape.LEGACY_PREFIX)
        self.assertTrue(self.connection.in_transaction)
        self.assertFalse(
            any(
                statement.strip().upper() in {"BEGIN", "COMMIT", "ROLLBACK"}
                for statement, _parameters in recording.calls
            )
        )
        self.assertEqual(
            self.connection.execute("SELECT value FROM caller_state").fetchone(),
            ("pending",),
        )
        self.connection.execute("ROLLBACK")
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM caller_state").fetchone(),
            (0,),
        )

    def test_closed_connection_uses_the_stable_snapshot_error(self) -> None:
        self.connection.close()

        with self.assertRaisesRegex(
            DomainMigrationBridgeIntegrityError,
            "snapshot could not be opened",
        ):
            read_domain_migration_bridge_state(self.connection)


class DomainMigrationLegacyBootstrapTests(unittest.TestCase):
    METADATA_INSERT_MARKER = "INSERT INTO main.qe_schema_migration_metadata"
    DEPENDENCY_INSERT_MARKER = "INSERT INTO main.qe_schema_migration_dependencies"
    METADATA_QUERY_MARKER = (
        "SELECT migration_version, domain, domain_version, metadata_kind, "
        "descriptor_sha256, owned_schema_sha256, recorded_at "
        "FROM main.qe_schema_migration_metadata"
    )

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tempdir.name) / "legacy-bootstrap.sqlite3")
        self.connection = sqlite3.connect(
            self.path,
            isolation_level=None,
            timeout=1,
        )

    def tearDown(self) -> None:
        self.connection.close()
        self.tempdir.cleanup()

    def prepare_prefix(self, connection: sqlite3.Connection, count: int) -> None:
        if count >= 3:
            connection.execute(
                """
                CREATE TABLE outbox (
                    message_id TEXT PRIMARY KEY,
                    lease_token TEXT,
                    status TEXT NOT NULL
                )
                """
            )
        versions = tuple(item.migration_id for item in LEGACY_DOMAIN_MIGRATIONS[:count])
        self.assertEqual(
            apply_sqlite_migrations(
                connection,
                target_versions=versions,
                clock=lambda: BRIDGE_TIME,
            ),
            versions[-1] if versions else 0,
        )
        self.assertIs(
            install_domain_migration_sidecar(connection),
            DomainMigrationSidecarSchemaState.EXACT,
        )

    def assert_sidecar_empty(self, connection: sqlite3.Connection) -> None:
        state = read_domain_migration_bridge_state(connection)
        self.assertIs(state.shape, DomainMigrationBridgeShape.SIDECAR_EMPTY_PREFIX)
        self.assertEqual(state.metadata_rows, ())
        self.assertEqual(state.dependency_rows, ())
        self.assertFalse(connection.in_transaction)

    def assert_write_lock_available(self) -> None:
        contender = sqlite3.connect(
            self.path,
            isolation_level=None,
            timeout=0.1,
        )
        try:
            contender.execute("BEGIN IMMEDIATE")
            contender.execute("ROLLBACK")
        finally:
            contender.close()

    @staticmethod
    def dependency_registry() -> DomainMigrationRegistry:
        first, second = DOMAIN_MIGRATION_REGISTRY.descriptors[:2]
        dependent = replace(second, dependencies=(first.migration_id,))
        return DomainMigrationRegistry(
            descriptors=(first, dependent),
            registry_sha256="f" * 64,
        )

    def test_every_current_nonempty_legacy_prefix_bootstraps_exactly(self) -> None:
        for count in range(1, len(LEGACY_DOMAIN_MIGRATIONS) + 1):
            with self.subTest(count=count):
                connection = sqlite3.connect(":memory:", isolation_level=None)
                try:
                    self.prepare_prefix(connection, count)
                    source = read_domain_migration_bridge_state(connection)
                    clock_calls = 0

                    def clock() -> str:
                        nonlocal clock_calls
                        clock_calls += 1
                        return BRIDGE_TIME

                    state = bootstrap_legacy_domain_migration_metadata(
                        connection,
                        clock=clock,
                    )

                    self.assertIs(state.shape, DomainMigrationBridgeShape.BRIDGED_PREFIX)
                    self.assertEqual(clock_calls, 1)
                    self.assertEqual(state.ledger_rows, source.ledger_rows)
                    self.assertEqual(state.legacy_schema_version, count)
                    self.assertEqual(len(state.metadata_rows), count)
                    self.assertEqual(
                        {row.recorded_at for row in state.metadata_rows},
                        {BRIDGE_TIME},
                    )
                    self.assertEqual(state.dependency_rows, ())
                    self.assertEqual(
                        connection.execute(
                            "SELECT COUNT(*) FROM main.qe_schema_migrations"
                        ).fetchone(),
                        (count,),
                    )
                finally:
                    connection.close()

    def test_empty_ledger_is_a_locked_zero_dml_noop_without_clock_sampling(self) -> None:
        self.prepare_prefix(self.connection, 0)
        recording = RecordingConnection(self.connection)
        clock = mock.Mock(side_effect=AssertionError("empty prefix sampled the clock"))
        before_changes = self.connection.total_changes

        state = bootstrap_legacy_domain_migration_metadata(
            cast(sqlite3.Connection, recording),
            clock=clock,
        )

        self.assertIs(state.shape, DomainMigrationBridgeShape.SIDECAR_EMPTY_PREFIX)
        clock.assert_not_called()
        self.assertEqual(self.connection.total_changes, before_changes)
        normalized = tuple(" ".join(statement.split()) for statement, _ in recording.calls)
        self.assertIn("BEGIN IMMEDIATE", normalized)
        self.assertNotIn("COMMIT", normalized)
        self.assertFalse(any("INSERT INTO main." in item for item in normalized))
        self.assertFalse(self.connection.in_transaction)

    def test_bridged_prefix_is_idempotent_with_zero_dml_and_no_clock(self) -> None:
        self.prepare_prefix(self.connection, 2)
        first = bootstrap_legacy_domain_migration_metadata(
            self.connection,
            clock=lambda: BRIDGE_TIME,
        )
        before_changes = self.connection.total_changes
        recording = RecordingConnection(self.connection)
        clock = mock.Mock(side_effect=AssertionError("idempotent path sampled the clock"))

        second = bootstrap_legacy_domain_migration_metadata(
            cast(sqlite3.Connection, recording),
            clock=clock,
        )

        self.assertEqual(second, first)
        clock.assert_not_called()
        self.assertEqual(self.connection.total_changes, before_changes)
        normalized = tuple(" ".join(statement.split()) for statement, _ in recording.calls)
        self.assertIn("BEGIN IMMEDIATE", normalized)
        self.assertFalse(any("INSERT INTO main." in item for item in normalized))
        self.assertNotIn("COMMIT", normalized)
        self.assertFalse(self.connection.in_transaction)

    def test_absent_sidecar_is_rejected_before_the_write_lock(self) -> None:
        apply_sqlite_migrations(
            self.connection,
            target_versions=(1,),
            clock=lambda: BRIDGE_TIME,
        )
        recording = RecordingConnection(self.connection)

        with self.assertRaisesRegex(
            DomainMigrationLegacyBootstrapError,
            "requires an existing exact sidecar",
        ):
            bootstrap_legacy_domain_migration_metadata(
                cast(sqlite3.Connection, recording),
                clock=lambda: BRIDGE_TIME,
            )

        normalized = tuple(" ".join(statement.split()) for statement, _ in recording.calls)
        self.assertNotIn("BEGIN IMMEDIATE", normalized)
        self.assertFalse(any("INSERT INTO main." in item for item in normalized))
        self.assertFalse(self.connection.in_transaction)

    def test_active_caller_transaction_is_rejected_untouched(self) -> None:
        self.prepare_prefix(self.connection, 1)
        self.connection.execute("CREATE TABLE caller_pending (value TEXT)")
        self.connection.execute("BEGIN")
        self.connection.execute("INSERT INTO caller_pending VALUES ('pending')")
        recording = RecordingConnection(self.connection)
        clock = mock.Mock(side_effect=AssertionError("caller transaction sampled clock"))

        with self.assertRaisesRegex(
            DomainMigrationLegacyBootstrapError,
            "requires no active caller transaction",
        ):
            bootstrap_legacy_domain_migration_metadata(
                cast(sqlite3.Connection, recording),
                clock=clock,
            )

        self.assertEqual(recording.calls, [])
        clock.assert_not_called()
        self.assertTrue(self.connection.in_transaction)
        self.assertEqual(
            self.connection.execute("SELECT value FROM caller_pending").fetchone(),
            ("pending",),
        )
        self.connection.execute("ROLLBACK")
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM caller_pending").fetchone(),
            (0,),
        )

    def test_trace_authorizer_and_recorder_prove_the_only_writes_are_parameterized_rows(
        self,
    ) -> None:
        self.prepare_prefix(self.connection, 2)
        traced: list[str] = []
        actions: list[tuple[int, Optional[str]]] = []
        forbidden_actions = {
            sqlite3.SQLITE_ALTER_TABLE,
            sqlite3.SQLITE_CREATE_INDEX,
            sqlite3.SQLITE_CREATE_TABLE,
            sqlite3.SQLITE_CREATE_TRIGGER,
            sqlite3.SQLITE_CREATE_VIEW,
            sqlite3.SQLITE_DELETE,
            sqlite3.SQLITE_DROP_INDEX,
            sqlite3.SQLITE_DROP_TABLE,
            sqlite3.SQLITE_DROP_TRIGGER,
            sqlite3.SQLITE_DROP_VIEW,
            sqlite3.SQLITE_UPDATE,
        }
        allowed_insert_tables = {DOMAIN_MIGRATION_METADATA_TABLE_NAME}

        def authorize(
            action: int,
            argument_one: Optional[str],
            _argument_two: Optional[str],
            _database_name: Optional[str],
            _trigger_name: Optional[str],
        ) -> int:
            actions.append((action, argument_one))
            if action in forbidden_actions:
                return sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_INSERT and argument_one not in allowed_insert_tables:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        clock_calls = 0

        def clock() -> str:
            nonlocal clock_calls
            clock_calls += 1
            self.assertTrue(self.connection.in_transaction)
            return BRIDGE_TIME

        recording = RecordingConnection(self.connection)
        self.connection.set_trace_callback(traced.append)
        self.connection.set_authorizer(authorize)
        try:
            state = bootstrap_legacy_domain_migration_metadata(
                cast(sqlite3.Connection, recording),
                clock=clock,
            )
        finally:
            self.connection.set_authorizer(None)
            self.connection.set_trace_callback(None)

        self.assertIs(state.shape, DomainMigrationBridgeShape.BRIDGED_PREFIX)
        self.assertEqual(clock_calls, 1)
        application_dml = [
            (action, table)
            for action, table in actions
            if action
            in {
                sqlite3.SQLITE_DELETE,
                sqlite3.SQLITE_INSERT,
                sqlite3.SQLITE_UPDATE,
            }
        ]
        self.assertTrue(application_dml)
        self.assertEqual(
            set(application_dml),
            {(sqlite3.SQLITE_INSERT, DOMAIN_MIGRATION_METADATA_TABLE_NAME)},
        )
        insert_calls = [
            (" ".join(statement.split()), parameters)
            for statement, parameters in recording.calls
            if self.METADATA_INSERT_MARKER in " ".join(statement.split())
        ]
        self.assertEqual(len(insert_calls), 2)
        for statement, parameters in insert_calls:
            self.assertEqual(statement.count("?"), 7)
            self.assertEqual(len(parameters), 7)
            for parameter in parameters:
                if type(parameter) is str:
                    self.assertNotIn(parameter, statement)
        transaction_trace = tuple(
            " ".join(statement.strip().split()).upper()
            for statement in traced
            if statement.strip().upper().startswith(("BEGIN", "COMMIT", "ROLLBACK"))
        )
        self.assertEqual(
            transaction_trace,
            (
                "BEGIN",
                "ROLLBACK",
                "BEGIN IMMEDIATE",
                "COMMIT",
                "BEGIN",
                "ROLLBACK",
            ),
        )

    def test_partial_metadata_and_nonlegacy_registry_fail_with_zero_dml(self) -> None:
        self.prepare_prefix(self.connection, 2)
        first = DOMAIN_MIGRATION_REGISTRY.descriptors[0]
        self.connection.execute(
            """
            INSERT INTO main.qe_schema_migration_metadata (
                migration_version, domain, domain_version, metadata_kind,
                descriptor_sha256, owned_schema_sha256, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                first.migration_id,
                first.domain,
                first.domain_version,
                "legacy_bootstrap",
                first.descriptor_sha256,
                first.owned_object_manifest_sha256,
                BRIDGE_TIME,
            ),
        )
        recording = RecordingConnection(self.connection)
        with self.assertRaisesRegex(
            DomainMigrationLegacyBootstrapError,
            "preflight state is not exact",
        ):
            bootstrap_legacy_domain_migration_metadata(
                cast(sqlite3.Connection, recording),
                clock=lambda: BRIDGE_TIME,
            )
        normalized = tuple(" ".join(statement.split()) for statement, _ in recording.calls)
        self.assertNotIn("BEGIN IMMEDIATE", normalized)
        self.assertFalse(any(self.METADATA_INSERT_MARKER in item for item in normalized))

        self.connection.execute("DELETE FROM main.qe_schema_migration_metadata")
        original = DOMAIN_MIGRATION_REGISTRY.descriptors[0]
        native_registry = DomainMigrationRegistry(
            descriptors=(replace(original, kind="native"),),
            registry_sha256="e" * 64,
        )
        self.connection.execute("DELETE FROM main.qe_schema_migrations WHERE version = 2")
        for table in ("artifact_versions", "artifact_blobs"):
            self.connection.execute(f"DROP TABLE {table}")
        recording = RecordingConnection(self.connection)
        with mock.patch(
            "quantum_entanglement.domain_migrations.DOMAIN_MIGRATION_REGISTRY",
            native_registry,
        ):
            with self.assertRaisesRegex(
                DomainMigrationLegacyBootstrapError,
                "refuses non-legacy descriptors",
            ):
                bootstrap_legacy_domain_migration_metadata(
                    cast(sqlite3.Connection, recording),
                    clock=lambda: BRIDGE_TIME,
                )
        normalized = tuple(" ".join(statement.split()) for statement, _ in recording.calls)
        self.assertFalse(any(self.METADATA_INSERT_MARKER in item for item in normalized))
        self.assertFalse(self.connection.in_transaction)

    def test_invalid_and_raising_clocks_rollback_without_rows_or_locks(self) -> None:
        self.prepare_prefix(self.connection, 1)
        invalid_values: tuple[object, ...] = (
            object(),
            "2026-08-20T00:00:00+00:00",
            "2026-08-20T00:00:00.000000Z",
            "durable-secret-value",
        )
        for value in invalid_values:
            with self.subTest(value_type=type(value).__name__):
                clock = mock.Mock(return_value=value)
                with self.assertRaisesRegex(
                    DomainMigrationLegacyBootstrapError,
                    "clock returned a non-canonical timestamp",
                ) as raised:
                    bootstrap_legacy_domain_migration_metadata(
                        self.connection,
                        clock=cast(Any, clock),
                    )
                self.assertNotIn("durable-secret-value", str(raised.exception))
                clock.assert_called_once_with()
                self.assert_sidecar_empty(self.connection)
                self.assert_write_lock_available()

        for error in (
            ValueError("injected clock failure"),
            KeyboardInterrupt("injected clock interrupt"),
        ):
            with self.subTest(error_type=type(error).__name__):
                clock = mock.Mock(side_effect=error)
                with self.assertRaisesRegex(type(error), "injected clock"):
                    bootstrap_legacy_domain_migration_metadata(
                        self.connection,
                        clock=cast(Any, clock),
                    )
                clock.assert_called_once_with()
                self.assert_sidecar_empty(self.connection)
                self.assert_write_lock_available()

    def test_metadata_begin_commit_and_postcondition_failures_all_rollback(self) -> None:
        self.prepare_prefix(self.connection, 1)
        cases = (
            (
                self.METADATA_INSERT_MARKER,
                1,
                sqlite3.OperationalError("injected metadata insert failure"),
            ),
            (
                self.METADATA_INSERT_MARKER,
                1,
                KeyboardInterrupt("injected metadata insert interrupt"),
            ),
            ("COMMIT", 1, sqlite3.OperationalError("injected commit failure")),
        )
        for marker, occurrence, error in cases:
            with self.subTest(marker=marker, error_type=type(error).__name__):
                wrapped = MarkerFailureConnection(
                    self.connection,
                    marker=marker,
                    occurrence=occurrence,
                    error=error,
                )
                with self.assertRaisesRegex(type(error), "injected"):
                    bootstrap_legacy_domain_migration_metadata(
                        cast(sqlite3.Connection, wrapped),
                        clock=lambda: BRIDGE_TIME,
                    )
                self.assertTrue(wrapped.failed)
                self.assert_sidecar_empty(self.connection)
                self.assert_write_lock_available()

        postcondition = MarkerFailureConnection(
            self.connection,
            marker=self.METADATA_QUERY_MARKER,
            occurrence=3,
            error=sqlite3.OperationalError("injected postcondition failure"),
        )
        with self.assertRaisesRegex(
            DomainMigrationLegacyBootstrapError,
            "post-write state is not exact",
        ):
            bootstrap_legacy_domain_migration_metadata(
                cast(sqlite3.Connection, postcondition),
                clock=lambda: BRIDGE_TIME,
            )
        self.assertTrue(postcondition.failed)
        self.assert_sidecar_empty(self.connection)
        self.assert_write_lock_available()

        post_begin = PostBeginFailureConnection(self.connection)
        with self.assertRaisesRegex(KeyboardInterrupt, "post-BEGIN"):
            bootstrap_legacy_domain_migration_metadata(
                cast(sqlite3.Connection, post_begin),
                clock=lambda: BRIDGE_TIME,
            )
        self.assert_sidecar_empty(self.connection)
        self.assert_write_lock_available()

    def test_dependency_rows_are_exact_and_second_table_failure_rolls_back(self) -> None:
        self.prepare_prefix(self.connection, 2)
        registry = self.dependency_registry()
        with mock.patch(
            "quantum_entanglement.domain_migrations.DOMAIN_MIGRATION_REGISTRY",
            registry,
        ):
            wrapped = MarkerFailureConnection(
                self.connection,
                marker=self.DEPENDENCY_INSERT_MARKER,
                occurrence=1,
                error=sqlite3.OperationalError("injected dependency insert failure"),
            )
            with self.assertRaisesRegex(sqlite3.OperationalError, "dependency insert"):
                bootstrap_legacy_domain_migration_metadata(
                    cast(sqlite3.Connection, wrapped),
                    clock=lambda: BRIDGE_TIME,
                )
            self.assert_sidecar_empty(self.connection)
            self.assert_write_lock_available()

            recording = RecordingConnection(self.connection)
            state = bootstrap_legacy_domain_migration_metadata(
                cast(sqlite3.Connection, recording),
                clock=lambda: BRIDGE_TIME,
            )
            self.assertEqual(
                state.dependency_rows,
                (DomainMigrationDependencyRow(2, 1),),
            )
            dependency_calls = [
                (" ".join(statement.split()), parameters)
                for statement, parameters in recording.calls
                if self.DEPENDENCY_INSERT_MARKER in " ".join(statement.split())
            ]
            self.assertEqual(dependency_calls, [(dependency_calls[0][0], (2, 1))])
            self.assertEqual(dependency_calls[0][0].count("?"), 2)

    def test_cleanup_failure_is_dedicated_and_preserves_the_primary_context(self) -> None:
        self.prepare_prefix(self.connection, 1)
        primary = MarkerFailureConnection(
            self.connection,
            marker=self.METADATA_INSERT_MARKER,
            occurrence=1,
            error=ValueError("injected primary failure"),
        )
        cleanup = MarkerFailureConnection(
            cast(sqlite3.Connection, primary),
            marker="ROLLBACK",
            occurrence=2,
            error=RuntimeError("injected cleanup failure"),
        )

        with self.assertRaisesRegex(
            DomainMigrationLegacyBootstrapError,
            "rollback failed",
        ) as raised:
            bootstrap_legacy_domain_migration_metadata(
                cast(sqlite3.Connection, cleanup),
                clock=lambda: BRIDGE_TIME,
            )

        cleanup_cause = raised.exception.__cause__
        self.assertIsInstance(cleanup_cause, RuntimeError)
        self.assertIsInstance(cast(BaseException, cleanup_cause).__context__, ValueError)
        self.assertTrue(self.connection.in_transaction)
        self.connection.execute("ROLLBACK")
        self.assert_sidecar_empty(self.connection)
        self.assert_write_lock_available()

    def test_two_empty_preflights_have_one_winner_and_one_clock_sample(self) -> None:
        self.prepare_prefix(self.connection, 2)
        barrier = threading.Barrier(2)
        guard = threading.Lock()
        results: list[DomainMigrationBridgeState] = []
        failures: list[BaseException] = []
        call_logs: list[tuple[str, ...]] = []
        clock_calls = 0

        def clock() -> str:
            nonlocal clock_calls
            with guard:
                clock_calls += 1
            return BRIDGE_TIME

        def worker() -> None:
            connection = sqlite3.connect(
                self.path,
                isolation_level=None,
                timeout=5,
            )
            wrapped = BeginBarrierConnection(connection, barrier)
            try:
                state = bootstrap_legacy_domain_migration_metadata(
                    cast(sqlite3.Connection, wrapped),
                    clock=clock,
                )
                with guard:
                    results.append(state)
            except BaseException as error:  # pragma: no cover - assertion reports details.
                with guard:
                    failures.append(error)
            finally:
                with guard:
                    call_logs.append(
                        tuple(" ".join(statement.split()) for statement, _ in wrapped.calls)
                    )
                connection.close()

        threads = [
            threading.Thread(target=worker, daemon=True, name=f"legacy-bootstrap-{index}")
            for index in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(failures, [])
        self.assertEqual(len(results), 2)
        self.assertTrue(
            all(state.shape is DomainMigrationBridgeShape.BRIDGED_PREFIX for state in results)
        )
        self.assertEqual(results[0], results[1])
        self.assertEqual(clock_calls, 1)
        flattened = tuple(statement for call_log in call_logs for statement in call_log)
        self.assertEqual(flattened.count("BEGIN IMMEDIATE"), 2)
        self.assertEqual(
            sum(self.METADATA_INSERT_MARKER in statement for statement in flattened),
            2,
        )
        self.assertEqual(flattened.count("COMMIT"), 1)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM main.qe_schema_migration_metadata"
            ).fetchone(),
            (2,),
        )
        self.assertIs(
            read_domain_migration_bridge_state(self.connection).shape,
            DomainMigrationBridgeShape.BRIDGED_PREFIX,
        )
        self.assert_write_lock_available()


if __name__ == "__main__":
    unittest.main()
