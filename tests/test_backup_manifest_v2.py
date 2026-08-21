import copy
import hashlib
import inspect
import json
import sqlite3
import unittest
from collections.abc import Iterator
from dataclasses import FrozenInstanceError, replace
from typing import Any, Optional, cast
from unittest.mock import patch

import quantum_entanglement.backup as active_backup_module
import quantum_entanglement.backup_manifest_v2 as codec_module
from quantum_entanglement import (
    BACKUP_MANIFEST_V2_FORMAT as PUBLIC_V2_FORMAT,
)
from quantum_entanglement import (
    BackupManifest as PublicBackupManifest,
)
from quantum_entanglement import (
    BackupManifestV2 as PublicBackupManifestV2,
)
from quantum_entanglement import (
    decode_backup_manifest_v2 as public_decode_v2,
)
from quantum_entanglement import (
    encode_backup_manifest_v2 as public_encode_v2,
)
from quantum_entanglement import (
    parse_backup_manifest_v2 as public_parse_v2,
)
from quantum_entanglement.backup import BackupManifest
from quantum_entanglement.backup_manifest_v2 import (
    BACKUP_MANIFEST_V2_FORMAT,
    MAX_BACKUP_MANIFEST_V2_BYTES,
    BackupManifestV2,
    BackupManifestV2AppliedMigration,
    BackupManifestV2DependencyEdge,
    BackupManifestV2DomainHead,
    BackupManifestV2OwnedSchemaDigest,
    BackupManifestV2RegistryTopology,
    BackupManifestV2SchemaObject,
    BackupManifestV2SchemaState,
    BackupManifestV2TableCount,
    BackupManifestV2TopologyProfile,
    decode_backup_manifest_v2,
    encode_backup_manifest_v2,
    parse_backup_manifest_v2,
)
from quantum_entanglement.backup_topology import (
    BACKUP_TOPOLOGY_PROFILE,
    BACKUP_TOPOLOGY_REGISTRY,
    DOMAIN_MIGRATION_SIDECAR_PROFILE,
    EVENT_STORE_CORE_PROFILE,
    LEGACY_MIGRATION_LEDGER_PROFILE,
    PROJECTION_STORE_PROFILE,
    REVOCATION_GUARD_PROFILE,
)
from quantum_entanglement.domain_migrations import (
    DOMAIN_MIGRATION_REGISTRY,
    MAX_DOMAIN_MIGRATIONS,
)

SHA_A = "a" * 64
SHA_B = "b" * 64
BRIDGED_STATE_SHA256 = "d7741beeb65e9c74cd08be777136cdec403b9052c9fc79052d9dffd139a07a3f"
TOPOLOGY_FORMAT = "qe.sqlite-backup-registry-topology/1"
STATE_DIGESTS = {
    (0, "sidecar_absent"): "2785ed026a2ac2c5133535fe990d68e7fc34c9a2354ecc9047f5f8a031673304",
    (0, "empty"): "bd546f4b6fed8821344df7f7daae5585ffc47a371e9116ed6d55d5bbf66bc23f",
    (1, "sidecar_absent"): "9e7b2ef132bc39c06c16a9ece9fbea540e98e526946d17e11fe7233e8bce761f",
    (1, "legacy_prefix"): "f5741843450468e2f2d30ce4c2dd13c73f66904b46bfe3ced3cfa05c80e7a7c5",
    (1, "bridged_prefix"): "0b68caba8f1362bdf4a84b871cf760b61d3000f40b7608a7b309cdd4e59d3fc2",
    (2, "sidecar_absent"): "66a728dde8607ef393733a4d3aa697e70eaac4eddd7751aeae5bd12e5d8179c1",
    (2, "legacy_prefix"): "cfb74d395ee8ccd2b039dbfaa4895987e7834b5cc0ca1c23f86c8f5b952b3af8",
    (2, "bridged_prefix"): "56063f2ecf214885ee929da6325a2e9b88f7937a594786fb7c1360236fcfd21f",
    (3, "sidecar_absent"): "3a7ec821cd7265c98104245a245dc201b09abb04b86dda1146b4110c3b5ab10a",
    (3, "legacy_prefix"): "bb2cafe49a410b521097b647a3ac121843dc110d4db539605cbf011ebbe82e00",
    (3, "bridged_prefix"): BRIDGED_STATE_SHA256,
}


class TextSubclass(str):
    pass


def canonical_sha256(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def refresh_topology_digest(value: dict[str, Any]) -> None:
    topology = value["registryTopology"]
    evidence = {key: item for key, item in topology.items() if key != "topologySha256"}
    topology["topologySha256"] = canonical_sha256(evidence)


def topology_profile_dict(name: str) -> dict[str, Any]:
    profile = BACKUP_TOPOLOGY_REGISTRY.profile(name)
    return {
        "presence": "present",
        "profile": profile.name,
        "profileSha256": profile.profile_sha256,
    }


def topology_object_dict(item: Any) -> dict[str, Any]:
    return {
        "profile": item.profile,
        "owner": item.owner,
        "objectType": item.object_type,
        "name": item.name,
        "tableName": item.table_name,
        "ddlSha256": item.ddl_sha256,
    }


def present_profile_names(
    migration_count: int,
    *,
    sidecar_format: int,
    include_optional: bool,
) -> tuple[str, ...]:
    names = {
        BACKUP_TOPOLOGY_REGISTRY.migration_profile(descriptor.migration_id).name
        for descriptor in DOMAIN_MIGRATION_REGISTRY.descriptors[:migration_count]
    }
    if migration_count:
        names.add(LEGACY_MIGRATION_LEDGER_PROFILE)
    if sidecar_format:
        names.add(DOMAIN_MIGRATION_SIDECAR_PROFILE)
    if migration_count >= 3:
        names.add(EVENT_STORE_CORE_PROFILE)
    if include_optional:
        names.update(
            {
                EVENT_STORE_CORE_PROFILE,
                PROJECTION_STORE_PROFILE,
                REVOCATION_GUARD_PROFILE,
            }
        )
    return tuple(sorted(names, key=lambda item: item.encode("utf-8")))


def topology_dict(
    *,
    migration_count: int,
    sidecar_format: int,
    shape: str,
    state_sha256: str,
    include_optional: bool,
) -> dict[str, Any]:
    profile_names = present_profile_names(
        migration_count,
        sidecar_format=sidecar_format,
        include_optional=include_optional,
    )
    objects = BACKUP_TOPOLOGY_REGISTRY.objects_for_profiles(profile_names)
    table_names = tuple(
        sorted(
            (item.name for item in objects if item.object_type == "table"),
            key=lambda item: item.encode("utf-8"),
        )
    )
    row_counts = {name: index + 1 for index, name in enumerate(table_names)}
    if LEGACY_MIGRATION_LEDGER_PROFILE in profile_names:
        row_counts["qe_schema_migrations"] = migration_count
    if DOMAIN_MIGRATION_SIDECAR_PROFILE in profile_names:
        row_counts["qe_schema_migration_metadata"] = (
            migration_count if shape == "bridged_prefix" else 0
        )
        row_counts["qe_schema_migration_dependencies"] = 0
    evidence = {
        "format": TOPOLOGY_FORMAT,
        "topologyProfile": BACKUP_TOPOLOGY_PROFILE,
        "topologyRegistrySha256": BACKUP_TOPOLOGY_REGISTRY.registry_sha256,
        "registrySha256": DOMAIN_MIGRATION_REGISTRY.registry_sha256,
        "stateSha256": state_sha256,
        "presentProfiles": [topology_profile_dict(name) for name in profile_names],
        "schemaObjects": [topology_object_dict(item) for item in objects],
        "tableCounts": [{"name": name, "rowCount": row_counts[name]} for name in table_names],
    }
    return {
        **evidence,
        "topologySha256": canonical_sha256(evidence),
    }


def valid_manifest_dict() -> dict[str, Any]:
    registry = DOMAIN_MIGRATION_REGISTRY
    timestamps = (
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:01.000001Z",
        "2026-08-20T00:00:02Z",
    )
    migrations = [
        {
            "migrationId": descriptor.migration_id,
            "filename": descriptor.filename,
            "sqlSha256": descriptor.sql_sha256,
            "domain": descriptor.domain,
            "domainVersion": descriptor.domain_version,
            "kind": descriptor.kind,
            "descriptorSha256": descriptor.descriptor_sha256,
            "ownedSchemaSha256": descriptor.owned_object_manifest_sha256,
            "metadataRecorded": True,
            "appliedAt": timestamps[index],
        }
        for index, descriptor in enumerate(registry.descriptors)
    ]
    heads = sorted(
        (
            {
                "domain": descriptor.domain,
                "domainVersion": descriptor.domain_version,
                "migrationId": descriptor.migration_id,
                "ownedSchemaSha256": descriptor.owned_object_manifest_sha256,
                "metadataRecorded": True,
            }
            for descriptor in registry.descriptors
        ),
        key=lambda item: (
            item["domain"].encode("utf-8"),
            item["domainVersion"],
            item["migrationId"],
        ),
    )
    owned_digests = [
        {
            "domain": item["domain"],
            "ownedSchemaSha256": item["ownedSchemaSha256"],
        }
        for item in heads
    ]
    return {
        "formatVersion": BACKUP_MANIFEST_V2_FORMAT,
        "backupId": "backup_" + ("a" * 32),
        "createdAt": "2026-08-20T00:00:00.000000Z",
        "databaseSha256": SHA_B,
        "byteSize": 8192,
        "pageCount": 2,
        "pageSize": 4096,
        "schemaState": {
            "sidecarFormat": 1,
            "shape": "bridged_prefix",
            "legacySchemaVersion": 3,
            "appliedMigrations": migrations,
            "domainHeads": heads,
            "dependencyEdges": [],
            "ownedSchemaDigests": owned_digests,
            "registrySha256": registry.registry_sha256,
            "stateSha256": BRIDGED_STATE_SHA256,
        },
        "registryTopology": topology_dict(
            migration_count=3,
            sidecar_format=1,
            shape="bridged_prefix",
            state_sha256=BRIDGED_STATE_SHA256,
            include_optional=True,
        ),
    }


def valid_prefix_manifest_dict(migration_count: int, shape: str) -> dict[str, Any]:
    value = valid_manifest_dict()
    state = value["schemaState"]
    state_sha256 = STATE_DIGESTS[(migration_count, shape)]
    metadata_recorded = shape == "bridged_prefix"
    migrations = state["appliedMigrations"][:migration_count]
    for migration in migrations:
        migration["metadataRecorded"] = metadata_recorded
    heads = sorted(
        (
            {
                "domain": migration["domain"],
                "domainVersion": migration["domainVersion"],
                "migrationId": migration["migrationId"],
                "ownedSchemaSha256": migration["ownedSchemaSha256"],
                "metadataRecorded": metadata_recorded,
            }
            for migration in migrations
        ),
        key=lambda item: (
            item["domain"].encode("utf-8"),
            item["domainVersion"],
            item["migrationId"],
        ),
    )
    sidecar_format = 0 if shape == "sidecar_absent" else 1
    state.update(
        {
            "sidecarFormat": sidecar_format,
            "shape": shape,
            "legacySchemaVersion": migration_count,
            "appliedMigrations": migrations,
            "domainHeads": heads,
            "dependencyEdges": [],
            "ownedSchemaDigests": [
                {
                    "domain": head["domain"],
                    "ownedSchemaSha256": head["ownedSchemaSha256"],
                }
                for head in heads
            ],
            "stateSha256": state_sha256,
        }
    )
    value["registryTopology"] = topology_dict(
        migration_count=migration_count,
        sidecar_format=sidecar_format,
        shape=shape,
        state_sha256=state_sha256,
        include_optional=False,
    )
    return value


class ExactBackupManifestV2CodecTests(unittest.TestCase):
    def assert_rejected(self, mutate: Any, pattern: Optional[str] = None) -> None:
        value = valid_manifest_dict()
        mutate(value)
        context = self.assertRaisesRegex((TypeError, ValueError), pattern) if pattern else None
        if context is not None:
            with context:
                parse_backup_manifest_v2(value)
        else:
            with self.assertRaises((TypeError, ValueError)):
                parse_backup_manifest_v2(value)

    def test_public_api_is_explicit_and_v1_type_remains_unchanged(self) -> None:
        self.assertIs(PublicBackupManifest, BackupManifest)
        self.assertIs(PublicBackupManifestV2, BackupManifestV2)
        self.assertIs(public_parse_v2, parse_backup_manifest_v2)
        self.assertIs(public_encode_v2, encode_backup_manifest_v2)
        self.assertIs(public_decode_v2, decode_backup_manifest_v2)
        self.assertEqual(PUBLIC_V2_FORMAT, "qe.sqlite-backup/2")
        self.assertIsNot(PublicBackupManifest, PublicBackupManifestV2)

        v1_value = {
            "formatVersion": "qe.sqlite-backup/1",
            "backupId": "backup_" + ("f" * 32),
            "createdAt": "2026-08-20T00:00:00.000000Z",
            "databaseSha256": SHA_A,
            "byteSize": 4096,
            "pageCount": 1,
            "pageSize": 4096,
            "tableCounts": {},
            "migrations": [],
        }
        parsed_v1 = BackupManifest.from_dict(copy.deepcopy(v1_value))
        self.assertEqual(parsed_v1.to_dict(), v1_value)
        with self.assertRaisesRegex(ValueError, "format version 1"):
            BackupManifest.from_dict(valid_manifest_dict())

    def test_v1_active_module_has_no_v2_reachability(self) -> None:
        source = inspect.getsource(active_backup_module)
        self.assertNotIn("qe.sqlite-backup/2", source)
        self.assertNotIn("BackupManifestV2", source)
        self.assertNotIn("backup_manifest_v2", source)
        for operation in (
            active_backup_module.create_sqlite_backup,
            active_backup_module.verify_sqlite_backup,
            active_backup_module.restore_sqlite_backup,
        ):
            self.assertIs(operation.__globals__["BackupManifest"], BackupManifest)
            self.assertNotIn("BackupManifestV2", operation.__globals__)

    def test_valid_model_has_exact_immutable_tuple_collections(self) -> None:
        parsed = parse_backup_manifest_v2(valid_manifest_dict())
        self.assertIs(type(parsed), BackupManifestV2)
        self.assertIs(type(parsed.schema_state.applied_migrations), tuple)
        self.assertIs(type(parsed.schema_state.domain_heads), tuple)
        self.assertIs(type(parsed.schema_state.dependency_edges), tuple)
        self.assertIs(type(parsed.schema_state.owned_schema_digests), tuple)
        self.assertIs(type(parsed.registry_topology.present_profiles), tuple)
        self.assertIs(type(parsed.registry_topology.schema_objects), tuple)
        self.assertIs(type(parsed.registry_topology.table_counts), tuple)
        self.assertEqual(len(parsed.schema_state.applied_migrations), 3)
        self.assertEqual(len(parsed.registry_topology.present_profiles), 8)
        self.assertEqual(len(parsed.registry_topology.schema_objects), 58)
        self.assertEqual(
            tuple(item.name for item in parsed.registry_topology.table_counts),
            tuple(sorted(item.name for item in parsed.registry_topology.table_counts)),
        )
        with self.assertRaises(FrozenInstanceError):
            parsed.page_size = 8192  # type: ignore[misc]

    def test_dict_and_canonical_byte_round_trips_are_exact(self) -> None:
        value = valid_manifest_dict()
        parsed = BackupManifestV2.from_dict(copy.deepcopy(value))
        self.assertEqual(parsed.to_dict(), value)

        expected = (
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        encoded = parsed.to_json_bytes()
        self.assertEqual(encoded, expected)
        self.assertEqual(encode_backup_manifest_v2(parsed), expected)
        self.assertEqual(BackupManifestV2.from_json_bytes(encoded), parsed)
        self.assertEqual(decode_backup_manifest_v2(encoded), parsed)
        self.assertEqual(decode_backup_manifest_v2(encoded).to_json_bytes(), encoded)

    def test_every_current_bridge_only_shape_and_registry_prefix_round_trips(self) -> None:
        for (migration_count, shape), state_sha256 in STATE_DIGESTS.items():
            with self.subTest(migration_count=migration_count, shape=shape):
                value = valid_prefix_manifest_dict(migration_count, shape)
                parsed = parse_backup_manifest_v2(value)
                self.assertEqual(parsed.schema_state.shape, shape)
                self.assertEqual(parsed.schema_state.state_sha256, state_sha256)
                self.assertEqual(
                    len(parsed.schema_state.applied_migrations),
                    migration_count,
                )
                self.assertEqual(
                    decode_backup_manifest_v2(encode_backup_manifest_v2(parsed)),
                    parsed,
                )

    def test_input_and_output_containers_are_detached(self) -> None:
        value = valid_manifest_dict()
        parsed = parse_backup_manifest_v2(value)
        expected = parsed.to_dict()

        value["schemaState"]["appliedMigrations"].clear()
        value["schemaState"]["domainHeads"][0]["domain"] = "mutated"
        value["registryTopology"]["presentProfiles"].clear()
        value["registryTopology"]["schemaObjects"].clear()
        value["registryTopology"]["tableCounts"].clear()
        self.assertEqual(parsed.to_dict(), expected)

        emitted = parsed.to_dict()
        emitted["schemaState"]["appliedMigrations"].clear()
        emitted["registryTopology"]["presentProfiles"][0]["profile"] = "mutated"
        emitted["registryTopology"]["schemaObjects"][0]["name"] = "mutated"
        self.assertEqual(parsed.to_dict(), expected)

        migration_list = list(parsed.schema_state.applied_migrations)
        snapshot = BackupManifestV2SchemaState(
            sidecar_format=parsed.schema_state.sidecar_format,
            shape=parsed.schema_state.shape,
            legacy_schema_version=parsed.schema_state.legacy_schema_version,
            applied_migrations=cast(Any, migration_list),
            domain_heads=parsed.schema_state.domain_heads,
            dependency_edges=parsed.schema_state.dependency_edges,
            owned_schema_digests=parsed.schema_state.owned_schema_digests,
            registry_sha256=parsed.schema_state.registry_sha256,
            state_sha256=parsed.schema_state.state_sha256,
        )
        migration_list.clear()
        self.assertEqual(snapshot.applied_migrations, parsed.schema_state.applied_migrations)

    def test_top_level_extra_missing_future_and_wrong_container_are_rejected(self) -> None:
        fields = tuple(valid_manifest_dict())
        for field in fields:
            with self.subTest(missing=field):
                value = valid_manifest_dict()
                del value[field]
                with self.assertRaisesRegex(ValueError, "fields do not match"):
                    parse_backup_manifest_v2(value)
        value = valid_manifest_dict()
        value["optionalFuture"] = None
        with self.assertRaisesRegex(ValueError, "fields do not match"):
            parse_backup_manifest_v2(value)
        value = valid_manifest_dict()
        value["formatVersion"] = "qe.sqlite-backup/3"
        with self.assertRaisesRegex(ValueError, "exactly qe.sqlite-backup/2"):
            parse_backup_manifest_v2(value)
        for wrong in ([], (), None, "manifest"):
            with self.subTest(wrong=type(wrong).__name__):
                with self.assertRaisesRegex(TypeError, "plain dictionary"):
                    parse_backup_manifest_v2(wrong)

        class DictSubclass(dict[str, Any]):
            pass

        with self.assertRaisesRegex(TypeError, "plain dictionary"):
            parse_backup_manifest_v2(DictSubclass(valid_manifest_dict()))

    def test_direct_mapping_keys_must_be_exact_strings(self) -> None:
        value = valid_manifest_dict()
        top_level = {
            TextSubclass(key) if key == "formatVersion" else key: item
            for key, item in value.items()
        }
        with self.assertRaisesRegex(TypeError, "keys must be plain strings"):
            parse_backup_manifest_v2(top_level)

        value = valid_manifest_dict()
        nested = value["registryTopology"]["schemaObjects"][0]
        value["registryTopology"]["schemaObjects"][0] = {
            TextSubclass(key) if key == "owner" else key: item for key, item in nested.items()
        }
        with self.assertRaisesRegex(TypeError, "keys must be plain strings"):
            parse_backup_manifest_v2(value)

    def test_every_nested_object_requires_exact_fields(self) -> None:
        locations = (
            ("schemaState",),
            ("schemaState", "appliedMigrations", 0),
            ("schemaState", "domainHeads", 0),
            ("schemaState", "ownedSchemaDigests", 0),
            ("registryTopology",),
            ("registryTopology", "presentProfiles", 0),
            ("registryTopology", "schemaObjects", 0),
            ("registryTopology", "tableCounts", 0),
        )
        for location in locations:
            with self.subTest(location=location, mutation="extra"):
                value = valid_manifest_dict()
                target: Any = value
                for part in location:
                    target = target[part]
                target["future"] = 1
                with self.assertRaisesRegex(ValueError, "fields do not match"):
                    parse_backup_manifest_v2(value)
            with self.subTest(location=location, mutation="missing"):
                value = valid_manifest_dict()
                target = value
                for part in location:
                    target = target[part]
                del target[next(iter(target))]
                with self.assertRaisesRegex(ValueError, "fields do not match"):
                    parse_backup_manifest_v2(value)

        value = valid_manifest_dict()
        value["schemaState"]["dependencyEdges"] = [
            {"migrationId": 2, "dependsOnMigrationId": 1, "future": False}
        ]
        with self.assertRaisesRegex(ValueError, "fields do not match"):
            parse_backup_manifest_v2(value)

    def test_nested_collections_require_json_lists(self) -> None:
        collection_paths = (
            ("schemaState", "appliedMigrations"),
            ("schemaState", "domainHeads"),
            ("schemaState", "dependencyEdges"),
            ("schemaState", "ownedSchemaDigests"),
            ("registryTopology", "presentProfiles"),
            ("registryTopology", "schemaObjects"),
            ("registryTopology", "tableCounts"),
        )
        for path in collection_paths:
            with self.subTest(path=path):
                value = valid_manifest_dict()
                parent = value[path[0]]
                parent[path[1]] = tuple(parent[path[1]])
                with self.assertRaisesRegex(TypeError, "must be a list"):
                    parse_backup_manifest_v2(value)

    def test_scalar_hash_geometry_id_and_timestamp_bounds_are_exact(self) -> None:
        mutations = (
            lambda value: value.__setitem__("backupId", "backup_" + ("A" * 32)),
            lambda value: value.__setitem__("createdAt", "2026-08-20T00:00:00Z"),
            lambda value: value.__setitem__("createdAt", "2026-02-30T00:00:00.000000Z"),
            lambda value: value.__setitem__("databaseSha256", "A" * 64),
            lambda value: value.__setitem__("databaseSha256", "a" * 63),
            lambda value: value.__setitem__("byteSize", True),
            lambda value: value.__setitem__("byteSize", 0),
            lambda value: value.__setitem__("byteSize", 4096),
            lambda value: value.__setitem__("pageCount", True),
            lambda value: value.__setitem__("pageCount", 0),
            lambda value: value.__setitem__("pageCount", 2**32),
            lambda value: value.__setitem__("pageSize", True),
            lambda value: value.__setitem__("pageSize", 513),
            lambda value: value.__setitem__("pageSize", 131072),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(case=index):
                self.assert_rejected(mutate)

    def test_schema_state_rejects_future_native_sparse_and_digest_drift(self) -> None:
        mutations = (
            lambda value: value["schemaState"].__setitem__("sidecarFormat", True),
            lambda value: value["schemaState"].__setitem__("sidecarFormat", 2),
            lambda value: value["schemaState"].__setitem__("shape", "domain_sparse"),
            lambda value: value["schemaState"].__setitem__("shape", "future"),
            lambda value: value["schemaState"].__setitem__("legacySchemaVersion", True),
            lambda value: value["schemaState"].__setitem__("legacySchemaVersion", 4),
            lambda value: value["schemaState"].__setitem__("registrySha256", SHA_A),
            lambda value: value["schemaState"].__setitem__("stateSha256", SHA_A),
            lambda value: value["schemaState"]["appliedMigrations"][0].__setitem__(
                "kind", "native"
            ),
            lambda value: value["schemaState"]["appliedMigrations"][0].__setitem__(
                "descriptorSha256", SHA_A
            ),
            lambda value: value["schemaState"]["appliedMigrations"][0].__setitem__(
                "sqlSha256", SHA_A
            ),
            lambda value: value["schemaState"]["appliedMigrations"][0].__setitem__(
                "ownedSchemaSha256", SHA_A
            ),
            lambda value: value["schemaState"]["appliedMigrations"][0].__setitem__(
                "migrationId", True
            ),
            lambda value: value["schemaState"]["appliedMigrations"][0].__setitem__(
                "domainVersion", True
            ),
            lambda value: value["schemaState"]["appliedMigrations"][0].__setitem__(
                "metadataRecorded", 1
            ),
            lambda value: value["schemaState"]["appliedMigrations"][0].__setitem__(
                "appliedAt", "2026-08-20T00:00:00.000000Z"
            ),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(case=index):
                self.assert_rejected(mutate)

    def test_all_schema_state_collections_reject_duplicate_and_order_drift(self) -> None:
        cases = []

        def duplicate_migration(value: dict[str, Any]) -> None:
            rows = value["schemaState"]["appliedMigrations"]
            rows.insert(1, copy.deepcopy(rows[0]))

        cases.append(duplicate_migration)

        def reorder_migrations(value: dict[str, Any]) -> None:
            rows = value["schemaState"]["appliedMigrations"]
            rows[0], rows[1] = rows[1], rows[0]

        cases.append(reorder_migrations)

        def duplicate_head(value: dict[str, Any]) -> None:
            rows = value["schemaState"]["domainHeads"]
            rows.insert(1, copy.deepcopy(rows[0]))

        cases.append(duplicate_head)

        def reorder_heads(value: dict[str, Any]) -> None:
            rows = value["schemaState"]["domainHeads"]
            rows[0], rows[1] = rows[1], rows[0]

        cases.append(reorder_heads)

        def duplicate_digest(value: dict[str, Any]) -> None:
            rows = value["schemaState"]["ownedSchemaDigests"]
            rows.insert(1, copy.deepcopy(rows[0]))

        cases.append(duplicate_digest)

        def reorder_digests(value: dict[str, Any]) -> None:
            rows = value["schemaState"]["ownedSchemaDigests"]
            rows[0], rows[1] = rows[1], rows[0]

        cases.append(reorder_digests)

        def duplicate_edge(value: dict[str, Any]) -> None:
            edge = {"migrationId": 2, "dependsOnMigrationId": 1}
            value["schemaState"]["dependencyEdges"] = [edge, copy.deepcopy(edge)]

        cases.append(duplicate_edge)

        def reorder_edges(value: dict[str, Any]) -> None:
            value["schemaState"]["dependencyEdges"] = [
                {"migrationId": 3, "dependsOnMigrationId": 2},
                {"migrationId": 2, "dependsOnMigrationId": 1},
            ]

        cases.append(reorder_edges)

        for index, mutate in enumerate(cases):
            with self.subTest(case=index):
                self.assert_rejected(mutate)

    def test_dependency_edges_are_bound_to_the_current_registry(self) -> None:
        value = valid_manifest_dict()
        value["schemaState"]["dependencyEdges"] = [{"migrationId": 2, "dependsOnMigrationId": 1}]
        with self.assertRaisesRegex(ValueError, "canonical bridge-only registry state"):
            parse_backup_manifest_v2(value)

        value = valid_manifest_dict()
        value["schemaState"]["dependencyEdges"] = [{"migrationId": 999, "dependsOnMigrationId": 1}]
        with self.assertRaises((TypeError, ValueError)):
            parse_backup_manifest_v2(value)

    def test_registry_topology_is_exact_registry_and_state_bound_evidence(self) -> None:
        parsed = parse_backup_manifest_v2(valid_manifest_dict())
        expected_coordinates = tuple(
            (
                item.profile,
                item.owner,
                item.object_type,
                item.name,
                item.table_name,
                item.ddl_sha256,
            )
            for item in BACKUP_TOPOLOGY_REGISTRY.objects_for_profiles(
                profile.name for profile in BACKUP_TOPOLOGY_REGISTRY.profiles
            )
        )
        actual_coordinates = tuple(
            (
                item.profile,
                item.owner,
                item.object_type,
                item.name,
                item.table_name,
                item.ddl_sha256,
            )
            for item in parsed.registry_topology.schema_objects
        )
        self.assertEqual(actual_coordinates, expected_coordinates)
        self.assertEqual(
            parsed.registry_topology.topology_profile,
            BACKUP_TOPOLOGY_PROFILE,
        )
        self.assertEqual(
            parsed.registry_topology.topology_registry_sha256,
            BACKUP_TOPOLOGY_REGISTRY.registry_sha256,
        )
        table_names = {item.name for item in parsed.registry_topology.table_counts}
        self.assertTrue(
            {
                "qe_schema_migrations",
                "qe_schema_migration_metadata",
                "qe_schema_migration_dependencies",
                "invocation_jobs",
                "invocation_attempts",
                "artifact_blobs",
                "artifact_versions",
                "outbox_ambiguities",
            }.issubset(table_names)
        )

    def test_registry_topology_rejects_tamper_duplicates_order_and_unknown_tables(self) -> None:
        def topology_profile(value: dict[str, Any]) -> None:
            value["registryTopology"]["topologyProfile"] = "qe.sqlite-topology/future"

        def topology_registry_digest(value: dict[str, Any]) -> None:
            value["registryTopology"]["topologyRegistrySha256"] = SHA_A

        def registry_digest(value: dict[str, Any]) -> None:
            value["registryTopology"]["registrySha256"] = SHA_A

        def state_digest(value: dict[str, Any]) -> None:
            value["registryTopology"]["stateSha256"] = SHA_A

        def profile_digest(value: dict[str, Any]) -> None:
            value["registryTopology"]["presentProfiles"][0]["profileSha256"] = SHA_A

        def missing_profile(value: dict[str, Any]) -> None:
            value["registryTopology"]["presentProfiles"].pop()

        def duplicate_profile(value: dict[str, Any]) -> None:
            rows = value["registryTopology"]["presentProfiles"]
            rows.insert(1, copy.deepcopy(rows[0]))

        def reorder_profile(value: dict[str, Any]) -> None:
            rows = value["registryTopology"]["presentProfiles"]
            rows[0], rows[1] = rows[1], rows[0]

        def schema_ddl(value: dict[str, Any]) -> None:
            row = next(
                item
                for item in value["registryTopology"]["schemaObjects"]
                if item["ddlSha256"] is not None
            )
            row["ddlSha256"] = SHA_A

        def missing_schema_object(value: dict[str, Any]) -> None:
            value["registryTopology"]["schemaObjects"].pop()

        def duplicate_schema_object(value: dict[str, Any]) -> None:
            rows = value["registryTopology"]["schemaObjects"]
            rows.insert(1, copy.deepcopy(rows[0]))

        def reorder_schema_object(value: dict[str, Any]) -> None:
            rows = value["registryTopology"]["schemaObjects"]
            rows[0], rows[1] = rows[1], rows[0]

        def duplicate_count(value: dict[str, Any]) -> None:
            rows = value["registryTopology"]["tableCounts"]
            rows.insert(1, copy.deepcopy(rows[0]))

        def reorder_count(value: dict[str, Any]) -> None:
            rows = value["registryTopology"]["tableCounts"]
            rows[0], rows[1] = rows[1], rows[0]

        def unknown_table(value: dict[str, Any]) -> None:
            value["registryTopology"]["tableCounts"].append(
                {"name": "future_receipts", "rowCount": 0}
            )
            value["registryTopology"]["tableCounts"].sort(key=lambda item: item["name"])
            refresh_topology_digest(value)

        def missing_sidecar(value: dict[str, Any]) -> None:
            value["registryTopology"]["tableCounts"] = [
                item
                for item in value["registryTopology"]["tableCounts"]
                if item["name"] != "qe_schema_migration_dependencies"
            ]
            refresh_topology_digest(value)

        def missing_registry_table(value: dict[str, Any]) -> None:
            value["registryTopology"]["tableCounts"] = [
                item
                for item in value["registryTopology"]["tableCounts"]
                if item["name"] != "artifact_blobs"
            ]
            refresh_topology_digest(value)

        def bad_topology_digest(value: dict[str, Any]) -> None:
            value["registryTopology"]["topologySha256"] = SHA_A

        cases = (
            topology_profile,
            topology_registry_digest,
            registry_digest,
            state_digest,
            profile_digest,
            missing_profile,
            duplicate_profile,
            reorder_profile,
            schema_ddl,
            missing_schema_object,
            duplicate_schema_object,
            reorder_schema_object,
            duplicate_count,
            reorder_count,
            unknown_table,
            missing_sidecar,
            missing_registry_table,
            bad_topology_digest,
        )
        for mutate in cases:
            with self.subTest(case=mutate.__name__):
                self.assert_rejected(mutate)

    def test_topology_dependency_and_state_row_count_relations_fail_closed(self) -> None:
        value = valid_prefix_manifest_dict(3, "bridged_prefix")
        value["registryTopology"]["presentProfiles"] = [
            item
            for item in value["registryTopology"]["presentProfiles"]
            if item["profile"] != EVENT_STORE_CORE_PROFILE
        ]
        refresh_topology_digest(value)
        with self.assertRaisesRegex(ValueError, "required profile dependency"):
            parse_backup_manifest_v2(value)

        value = valid_manifest_dict()
        ledger = next(
            item
            for item in value["registryTopology"]["tableCounts"]
            if item["name"] == "qe_schema_migrations"
        )
        ledger["rowCount"] = 2
        refresh_topology_digest(value)
        with self.assertRaisesRegex(ValueError, "ledger row count"):
            parse_backup_manifest_v2(value)

        value = valid_manifest_dict()
        metadata = next(
            item
            for item in value["registryTopology"]["tableCounts"]
            if item["name"] == "qe_schema_migration_metadata"
        )
        metadata["rowCount"] = 2
        refresh_topology_digest(value)
        with self.assertRaisesRegex(ValueError, "metadata row count"):
            parse_backup_manifest_v2(value)

    def test_schema_object_scalar_models_reject_untrusted_coordinates(self) -> None:
        parsed = parse_backup_manifest_v2(valid_manifest_dict())
        schema_object = parsed.registry_topology.schema_objects[0]
        with self.assertRaisesRegex(ValueError, "not trusted"):
            replace(schema_object, profile="qe.future/1")
        with self.assertRaisesRegex(ValueError, "canonical identifier"):
            replace(schema_object, owner="Not-Canonical")
        table = next(
            item for item in parsed.registry_topology.schema_objects if item.object_type == "table"
        )
        with self.assertRaisesRegex(ValueError, "must equal"):
            replace(table, table_name="different_table")

    def test_table_counts_are_dynamic_but_canonically_digest_bound(self) -> None:
        value = valid_manifest_dict()
        value["registryTopology"]["tableCounts"][0]["rowCount"] += 1
        with self.assertRaisesRegex(ValueError, "digest differs"):
            parse_backup_manifest_v2(value)

        refresh_topology_digest(value)
        parsed = parse_backup_manifest_v2(value)
        self.assertEqual(parsed.registry_topology.table_counts[0].row_count, 2)

        value = valid_manifest_dict()
        value["registryTopology"]["tableCounts"] = [
            item for item in value["registryTopology"]["tableCounts"] if item["name"] != "events"
        ]
        refresh_topology_digest(value)
        with self.assertRaisesRegex(ValueError, "tableCounts differ"):
            parse_backup_manifest_v2(value)

    def test_exact_boolean_integer_string_and_sha_types_are_rejected_nested(self) -> None:
        mutations = (
            lambda value: value["schemaState"]["appliedMigrations"][0].__setitem__(
                "migrationId", True
            ),
            lambda value: value["schemaState"]["domainHeads"][0].__setitem__("metadataRecorded", 1),
            lambda value: value["registryTopology"]["presentProfiles"][0].__setitem__(
                "presence", True
            ),
            lambda value: value["registryTopology"]["schemaObjects"][0].__setitem__(
                "objectType", "sequence"
            ),
            lambda value: value["registryTopology"]["schemaObjects"][0].__setitem__(
                "name", "bad/name"
            ),
            lambda value: value["registryTopology"]["tableCounts"][0].__setitem__("rowCount", True),
            lambda value: value["registryTopology"]["tableCounts"][0].__setitem__("rowCount", -1),
            lambda value: value["registryTopology"]["tableCounts"][0].__setitem__(
                "rowCount", 2**63
            ),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(case=index):
                self.assert_rejected(mutate)

    def test_collection_limits_reject_oversize_before_item_parsing(self) -> None:
        value = valid_manifest_dict()
        valid_item = value["schemaState"]["appliedMigrations"][0]
        value["schemaState"]["appliedMigrations"] = [valid_item] * (MAX_DOMAIN_MIGRATIONS + 1)
        with self.assertRaisesRegex(ValueError, "exceeds the hard limit"):
            parse_backup_manifest_v2(value)

    def test_direct_topology_model_snapshots_only_bounded_iterable_prefixes(self) -> None:
        parsed = parse_backup_manifest_v2(valid_manifest_dict())
        yielded = 0

        def infinite() -> Iterator[BackupManifestV2TopologyProfile]:
            nonlocal yielded
            while True:
                yielded += 1
                yield parsed.registry_topology.present_profiles[0]

        with self.assertRaisesRegex(ValueError, "exceeds the hard limit"):
            replace(
                parsed.registry_topology,
                present_profiles=cast(Any, infinite()),
            )
        self.assertEqual(yielded, 65)

        value = valid_manifest_dict()
        count = value["registryTopology"]["tableCounts"][0]
        value["registryTopology"]["tableCounts"] = [count] * 4097
        with self.assertRaisesRegex(ValueError, "exceeds the hard limit"):
            parse_backup_manifest_v2(value)

    def test_direct_model_snapshots_only_a_bounded_prefix_of_infinite_iterable(self) -> None:
        parsed = parse_backup_manifest_v2(valid_manifest_dict())
        yielded = 0
        first_migration = parsed.schema_state.applied_migrations[0]

        def infinite() -> Iterator[BackupManifestV2AppliedMigration]:
            nonlocal yielded
            while True:
                yielded += 1
                yield first_migration

        with self.assertRaisesRegex(ValueError, "exceeds the hard limit"):
            replace(
                parsed.schema_state,
                applied_migrations=cast(Any, infinite()),
            )
        self.assertEqual(yielded, MAX_DOMAIN_MIGRATIONS + 1)

    def test_json_decoder_rejects_duplicates_noncanonical_bytes_and_nonfinite_values(self) -> None:
        encoded = encode_backup_manifest_v2(parse_backup_manifest_v2(valid_manifest_dict()))
        backup_id = b'"backupId":"backup_' + (b"a" * 32) + b'"'
        duplicate = encoded.replace(backup_id, backup_id + b"," + backup_id, 1)
        with self.assertRaisesRegex(ValueError, "duplicate keys"):
            decode_backup_manifest_v2(duplicate)

        noncanonical_values = (
            encoded[:-1],
            encoded + b"\n",
            b" " + encoded,
            json.dumps(valid_manifest_dict(), ensure_ascii=False).encode("utf-8"),
        )
        for value in noncanonical_values:
            with self.subTest(value=value[:20]):
                with self.assertRaisesRegex(ValueError, "not canonical"):
                    decode_backup_manifest_v2(value)

        nan_value = encoded.replace(b'"pageCount":2', b'"pageCount":NaN', 1)
        with self.assertRaisesRegex(ValueError, "unsupported constant"):
            decode_backup_manifest_v2(nan_value)
        decimal_value = encoded.replace(b'"pageCount":2', b'"pageCount":2.0', 1)
        with self.assertRaisesRegex(ValueError, "unsupported decimal"):
            decode_backup_manifest_v2(decimal_value)

    def test_json_integer_tokens_are_bounded_before_python_conversion(self) -> None:
        encoded = encode_backup_manifest_v2(parse_backup_manifest_v2(valid_manifest_dict()))
        oversized_token = "9" * 20
        oversized = encoded.replace(
            b'"pageCount":2',
            b'"pageCount":' + oversized_token.encode("ascii"),
            1,
        )
        converted: list[str] = []

        def record_conversion(value: str) -> int:
            converted.append(value)
            return int(value)

        with (
            patch.object(codec_module, "_bounded_python_int", side_effect=record_conversion),
            self.assertRaisesRegex(ValueError, "lexical limit"),
        ):
            decode_backup_manifest_v2(oversized)
        self.assertNotIn(oversized_token, converted)

        value = valid_manifest_dict()
        dynamic_count = next(
            item
            for item in value["registryTopology"]["tableCounts"]
            if item["name"]
            not in {
                "qe_schema_migrations",
                "qe_schema_migration_metadata",
                "qe_schema_migration_dependencies",
            }
        )
        dynamic_count["rowCount"] = (2**63) - 1
        refresh_topology_digest(value)
        canonical = (
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        self.assertEqual(
            decode_backup_manifest_v2(canonical).to_json_bytes(),
            canonical,
        )

    def test_json_decoder_rejects_wrong_type_invalid_utf8_malformed_deep_and_oversize(self) -> None:
        for wrong in ("{}", bytearray(b"{}"), memoryview(b"{}"), None):
            with self.subTest(wrong=type(wrong).__name__):
                with self.assertRaisesRegex(TypeError, "exact bytes"):
                    decode_backup_manifest_v2(wrong)

        class BytesSubclass(bytes):
            pass

        with self.assertRaisesRegex(TypeError, "exact bytes"):
            decode_backup_manifest_v2(BytesSubclass(b"{}"))
        with self.assertRaisesRegex(ValueError, "malformed"):
            decode_backup_manifest_v2(b"\xff")
        with self.assertRaisesRegex(ValueError, "malformed"):
            decode_backup_manifest_v2(b"{")
        with self.assertRaises((TypeError, ValueError)):
            decode_backup_manifest_v2((b"[" * 2000) + b"0" + (b"]" * 2000))
        with self.assertRaisesRegex(ValueError, "unsupported byte size"):
            decode_backup_manifest_v2(b"x" * (MAX_BACKUP_MANIFEST_V2_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "unsupported byte size"):
            decode_backup_manifest_v2(b"")

    def test_serializer_requires_exact_valid_model_and_revalidates_frozen_state(self) -> None:
        parsed = parse_backup_manifest_v2(valid_manifest_dict())
        for wrong in (parsed.to_dict(), object(), None):
            with self.subTest(wrong=type(wrong).__name__):
                with self.assertRaisesRegex(TypeError, "exact BackupManifestV2"):
                    encode_backup_manifest_v2(wrong)

        object.__setattr__(parsed, "database_sha256", "forged")
        with self.assertRaises((TypeError, ValueError)):
            encode_backup_manifest_v2(parsed)

        parsed = parse_backup_manifest_v2(valid_manifest_dict())
        object.__setattr__(parsed.registry_topology, "topology_sha256", SHA_A)
        with self.assertRaises((TypeError, ValueError)):
            encode_backup_manifest_v2(parsed)

        parsed = parse_backup_manifest_v2(valid_manifest_dict())
        yielded = 0
        first_migration = parsed.schema_state.applied_migrations[0]

        def infinite() -> Iterator[BackupManifestV2AppliedMigration]:
            nonlocal yielded
            while True:
                yielded += 1
                yield first_migration

        object.__setattr__(parsed.schema_state, "applied_migrations", infinite())
        with self.assertRaisesRegex(ValueError, "exceeds the hard limit"):
            encode_backup_manifest_v2(parsed)
        self.assertEqual(yielded, MAX_DOMAIN_MIGRATIONS + 1)

    def test_codec_has_no_sqlite_filesystem_or_migration_side_effects(self) -> None:
        value = valid_manifest_dict()
        self.assertNotIn("sqlite3", codec_module.__dict__)
        self.assertNotIn("Path", codec_module.__dict__)
        self.assertNotIn("os", codec_module.__dict__)
        with (
            patch("builtins.open", side_effect=AssertionError("file access")),
            patch.object(
                sqlite3,
                "connect",
                side_effect=AssertionError("SQLite access"),
            ),
        ):
            parsed = parse_backup_manifest_v2(value)
            encoded = encode_backup_manifest_v2(parsed)
            self.assertEqual(decode_backup_manifest_v2(encoded), parsed)

    def test_nested_models_and_exports_have_exact_names(self) -> None:
        parsed = parse_backup_manifest_v2(valid_manifest_dict())
        self.assertIs(type(parsed.schema_state), BackupManifestV2SchemaState)
        self.assertIs(
            type(parsed.schema_state.applied_migrations[0]),
            BackupManifestV2AppliedMigration,
        )
        self.assertIs(type(parsed.schema_state.domain_heads[0]), BackupManifestV2DomainHead)
        self.assertIs(
            type(parsed.schema_state.owned_schema_digests[0]),
            BackupManifestV2OwnedSchemaDigest,
        )
        self.assertIs(type(parsed.registry_topology), BackupManifestV2RegistryTopology)
        self.assertIs(
            type(parsed.registry_topology.present_profiles[0]),
            BackupManifestV2TopologyProfile,
        )
        self.assertIs(
            type(parsed.registry_topology.schema_objects[0]),
            BackupManifestV2SchemaObject,
        )
        self.assertIs(
            type(parsed.registry_topology.table_counts[0]),
            BackupManifestV2TableCount,
        )
        edge = BackupManifestV2DependencyEdge(2, 1)
        self.assertEqual(edge.to_dict(), {"migrationId": 2, "dependsOnMigrationId": 1})
        self.assertEqual(BACKUP_MANIFEST_V2_FORMAT, "qe.sqlite-backup/2")
        self.assertEqual(
            set(codec_module.__all__),
            {
                "BACKUP_MANIFEST_V2_FORMAT",
                "MAX_BACKUP_MANIFEST_V2_BYTES",
                "BackupManifestV2",
                "BackupManifestV2AppliedMigration",
                "BackupManifestV2DependencyEdge",
                "BackupManifestV2DomainHead",
                "BackupManifestV2OwnedSchemaDigest",
                "BackupManifestV2RegistryTopology",
                "BackupManifestV2SchemaObject",
                "BackupManifestV2SchemaState",
                "BackupManifestV2TableCount",
                "BackupManifestV2TopologyProfile",
                "decode_backup_manifest_v2",
                "encode_backup_manifest_v2",
                "parse_backup_manifest_v2",
            },
        )


if __name__ == "__main__":
    unittest.main()
