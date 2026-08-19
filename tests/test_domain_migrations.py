import hashlib
import itertools
import unittest
from dataclasses import replace
from typing import Any
from unittest import mock

from quantum_entanglement.domain_migrations import (
    DOMAIN_MIGRATION_REGISTRY,
    LEGACY_DOMAIN_MIGRATIONS,
    MAX_DOMAIN_MIGRATIONS,
    MAX_MIGRATION_DEPENDENCIES,
    MAX_MIGRATION_DOMAINS,
    MAX_OWNED_SCHEMA_OBJECTS,
    DomainMigrationDescriptor,
    OwnedSchemaObject,
    validate_domain_migration_registry,
)
from quantum_entanglement.migrations import migration_text

SHA_A = "a" * 64
SHA_B = "b" * 64


class CountingIntegers:
    def __init__(self) -> None:
        self.yielded = 0

    def __iter__(self) -> "CountingIntegers":
        return self

    def __next__(self) -> int:
        self.yielded += 1
        return 1


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


if __name__ == "__main__":
    unittest.main()
