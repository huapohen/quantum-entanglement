import hashlib
import itertools
import sqlite3
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from quantum_entanglement.backup_topology import (
    BACKUP_TOPOLOGY_PROFILE,
    BACKUP_TOPOLOGY_REGISTRY,
    BACKUP_TOPOLOGY_REGISTRY_FORMAT,
    DOMAIN_MIGRATION_SIDECAR_PROFILE,
    EVENT_STORE_CORE_PROFILE,
    LEGACY_MIGRATION_LEDGER_PROFILE,
    PROJECTION_STORE_PROFILE,
    REVOCATION_GUARD_PROFILE,
    TrustedBackupSchemaObject,
    TrustedBackupTopologyProfile,
    TrustedBackupTopologyRegistry,
    backup_schema_ddl_sha256,
    canonicalize_backup_schema_sql,
)
from quantum_entanglement.domain_migrations import (
    DOMAIN_MIGRATION_REGISTRY,
    apply_bridge_migration_plan,
    inspect_schema_state,
    plan_bridge_migrations,
)
from quantum_entanglement.projections import SQLiteProjectionOffsetStore
from quantum_entanglement.store import SQLiteEventStore
from quantum_entanglement.tenancy import SQLiteRevocationRevisionGuard

SHA_A = "a" * 64
SHA_B = "b" * 64
T0 = "2026-08-20T00:00:00Z"


class TextSubclass(str):
    pass


def schema_object(
    *,
    profile: str = "qe.test/1",
    owner: str = "test",
    object_type: str = "table",
    name: str = "test_table",
    table_name: str = "test_table",
    ddl_sha256: str = SHA_A,
) -> TrustedBackupSchemaObject:
    return TrustedBackupSchemaObject(
        profile=profile,
        owner=owner,
        object_type=object_type,
        name=name,
        table_name=table_name,
        ddl_sha256=ddl_sha256,
    )


def topology_profile(
    name: str,
    *,
    migration_id=None,
    dependencies=(),
    objects=(),
) -> TrustedBackupTopologyProfile:
    return TrustedBackupTopologyProfile(
        name=name,
        migration_id=migration_id,
        dependencies=dependencies,
        objects=objects,
    )


class BackupSchemaSqlCanonicalizationTests(unittest.TestCase):
    def test_only_outer_and_unquoted_whitespace_is_normalized(self):
        source = """
          CREATE   TABLE IF NOT EXISTS demo (
              value TEXT DEFAULT 'two  spaces',
              quoted TEXT DEFAULT "three   spaces"
          );
        """

        self.assertEqual(
            canonicalize_backup_schema_sql(source),
            "CREATE TABLE demo ( value TEXT DEFAULT 'two  spaces', "
            'quoted TEXT DEFAULT "three   spaces" )',
        )

    def test_if_not_exists_is_removed_only_from_the_leading_ddl_clause(self):
        self.assertEqual(
            canonicalize_backup_schema_sql(
                "CREATE TABLE IF NOT EXISTS demo "
                "(value TEXT DEFAULT 'IF NOT EXISTS', "
                '"IF NOT EXISTS" TEXT)'
            ),
            "CREATE TABLE demo (value TEXT DEFAULT 'IF NOT EXISTS', \"IF NOT EXISTS\" TEXT)",
        )
        self.assertEqual(
            canonicalize_backup_schema_sql('CREATE TABLE "IF NOT EXISTS" (value TEXT)'),
            'CREATE TABLE "IF NOT EXISTS" (value TEXT)',
        )
        self.assertEqual(
            canonicalize_backup_schema_sql("CREATE VIEW demo AS SELECT 'IF NOT EXISTS'"),
            "CREATE VIEW demo AS SELECT 'IF NOT EXISTS'",
        )

    def test_quoted_escapes_and_bracketed_identifiers_are_preserved(self):
        source = """ CREATE TABLE [odd]]name] (
            "double""quote" TEXT DEFAULT 'single''quote',
            `tick``quote` TEXT
        ) ; """

        self.assertEqual(
            canonicalize_backup_schema_sql(source),
            """CREATE TABLE [odd]]name] ( "double""quote" TEXT DEFAULT """
            """'single''quote', `tick``quote` TEXT )""",
        )

    def test_unterminated_quoted_region_fails_closed(self):
        for source in (
            "CREATE TABLE 'unterminated",
            'CREATE TABLE "unterminated',
            "CREATE TABLE `unterminated",
            "CREATE TABLE [unterminated",
        ):
            with self.subTest(source=source):
                with self.assertRaisesRegex(ValueError, "unterminated"):
                    canonicalize_backup_schema_sql(source)

    def test_input_type_and_size_are_exact_and_bounded(self):
        with self.assertRaisesRegex(TypeError, "plain string"):
            canonicalize_backup_schema_sql(TextSubclass("CREATE TABLE t(x)"))
        with self.assertRaisesRegex(TypeError, "plain string"):
            canonicalize_backup_schema_sql(b"CREATE TABLE t(x)")
        with self.assertRaisesRegex(ValueError, "between 1 and"):
            canonicalize_backup_schema_sql("")
        with self.assertRaisesRegex(ValueError, "between 1 and"):
            canonicalize_backup_schema_sql("x" * ((64 * 1024) + 1))

    def test_hash_uses_the_exact_canonical_sql_bytes(self):
        canonical = "CREATE TABLE demo (value TEXT DEFAULT 'two  spaces')"
        source = " CREATE TABLE IF NOT EXISTS demo (value TEXT DEFAULT 'two  spaces'); "

        self.assertEqual(
            backup_schema_ddl_sha256(source),
            hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )
        self.assertNotEqual(
            backup_schema_ddl_sha256(source),
            backup_schema_ddl_sha256(source.replace("two  spaces", "two spaces")),
        )


class TrustedBackupTopologyModelTests(unittest.TestCase):
    def test_schema_object_is_exact_frozen_evidence(self):
        item = schema_object()

        self.assertEqual(item.name, "test_table")
        with self.assertRaises(FrozenInstanceError):
            item.name = "changed"  # type: ignore[misc]
        with self.assertRaisesRegex(TypeError, "plain string"):
            schema_object(profile=TextSubclass("qe.test/1"))
        with self.assertRaisesRegex(ValueError, "canonical profile"):
            schema_object(profile="INVALID")
        with self.assertRaisesRegex(ValueError, "owner"):
            schema_object(owner="not-valid")
        with self.assertRaisesRegex(ValueError, "unsupported"):
            schema_object(object_type="bogus")
        with self.assertRaisesRegex(ValueError, "must equal"):
            schema_object(name="one", table_name="two")
        with self.assertRaisesRegex(ValueError, "lowercase SHA-256"):
            schema_object(ddl_sha256="A" * 64)

    def test_only_sqlite_autoindexes_can_have_null_ddl(self):
        autoindex = TrustedBackupSchemaObject(
            profile="qe.test/1",
            owner="test",
            object_type="index",
            name="sqlite_autoindex_test_table_1",
            table_name="test_table",
            ddl_sha256=None,
        )
        self.assertIsNone(autoindex.ddl_sha256)

        with self.assertRaisesRegex(ValueError, "only SQLite autoindexes"):
            TrustedBackupSchemaObject(
                profile="qe.test/1",
                owner="test",
                object_type="index",
                name="ordinary_index",
                table_name="test_table",
                ddl_sha256=None,
            )

    def test_profile_takes_bounded_immutable_snapshots(self):
        items = [schema_object()]
        profile = topology_profile("qe.test/1", objects=items)
        items.clear()

        self.assertEqual(len(profile.objects), 1)
        self.assertRegex(profile.profile_sha256, r"^[0-9a-f]{64}$")
        with self.assertRaisesRegex(ValueError, "hard limit"):
            topology_profile(
                "qe.test/1",
                objects=itertools.repeat(schema_object()),
            )

    def test_profile_rejects_noncanonical_or_conflicting_evidence(self):
        first = schema_object(name="alpha", table_name="alpha")
        second = schema_object(name="zeta", table_name="zeta", ddl_sha256=SHA_B)
        with self.assertRaisesRegex(ValueError, "canonical order"):
            topology_profile("qe.test/1", objects=(second, first))
        with self.assertRaisesRegex(ValueError, "duplicates"):
            topology_profile("qe.test/1", objects=(first, first))
        with self.assertRaisesRegex(ValueError, "differs"):
            topology_profile("qe.other/1", objects=(first,))
        with self.assertRaisesRegex(ValueError, "itself"):
            topology_profile("qe.test/1", dependencies=("qe.test/1",))
        with self.assertRaisesRegex(ValueError, "positive exact integer"):
            topology_profile("qe.test/1", migration_id=True)

    def test_registry_rejects_unknown_cycles_and_duplicate_coordinates(self):
        alpha_object = schema_object(
            profile="qe.alpha/1",
            name="alpha",
            table_name="alpha",
        )
        beta_object = schema_object(
            profile="qe.beta/1",
            name="beta",
            table_name="beta",
            ddl_sha256=SHA_B,
        )
        alpha = topology_profile("qe.alpha/1", objects=(alpha_object,))
        beta_unknown = topology_profile(
            "qe.beta/1",
            dependencies=("qe.missing/1",),
            objects=(beta_object,),
        )
        with self.assertRaisesRegex(ValueError, "unknown dependency"):
            TrustedBackupTopologyRegistry((alpha, beta_unknown))

        alpha_cycle = topology_profile(
            "qe.alpha/1",
            dependencies=("qe.beta/1",),
            objects=(alpha_object,),
        )
        beta_cycle = topology_profile(
            "qe.beta/1",
            dependencies=("qe.alpha/1",),
            objects=(beta_object,),
        )
        with self.assertRaisesRegex(ValueError, "acyclic"):
            TrustedBackupTopologyRegistry((alpha_cycle, beta_cycle))

        duplicate_coordinate = schema_object(
            profile="qe.beta/1",
            name="alpha",
            table_name="alpha",
            ddl_sha256=SHA_B,
        )
        beta_duplicate = topology_profile(
            "qe.beta/1",
            objects=(duplicate_coordinate,),
        )
        with self.assertRaisesRegex(ValueError, "globally unique"):
            TrustedBackupTopologyRegistry((alpha, beta_duplicate))

    def test_registry_lookup_and_projection_are_exact_and_bounded(self):
        event_profile = BACKUP_TOPOLOGY_REGISTRY.profile(EVENT_STORE_CORE_PROFILE)
        self.assertEqual(event_profile.name, EVENT_STORE_CORE_PROFILE)
        with self.assertRaisesRegex(TypeError, "plain string"):
            BACKUP_TOPOLOGY_REGISTRY.profile(TextSubclass(EVENT_STORE_CORE_PROFILE))
        with self.assertRaisesRegex(ValueError, "not present"):
            BACKUP_TOPOLOGY_REGISTRY.profile("qe.unknown/1")
        with self.assertRaisesRegex(ValueError, "positive exact integer"):
            BACKUP_TOPOLOGY_REGISTRY.migration_profile(True)
        with self.assertRaisesRegex(ValueError, "duplicates"):
            BACKUP_TOPOLOGY_REGISTRY.objects_for_profiles(
                (EVENT_STORE_CORE_PROFILE, EVENT_STORE_CORE_PROFILE)
            )
        with self.assertRaisesRegex(TypeError, "plain string"):
            BACKUP_TOPOLOGY_REGISTRY.objects_for_profiles((TextSubclass(EVENT_STORE_CORE_PROFILE),))
        with self.assertRaisesRegex(ValueError, "hard limit"):
            BACKUP_TOPOLOGY_REGISTRY.objects_for_profiles(
                itertools.repeat(EVENT_STORE_CORE_PROFILE)
            )


class CurrentBackupTopologyRegistryTests(unittest.TestCase):
    def test_registry_identity_and_profile_digests_are_frozen(self):
        self.assertEqual(BACKUP_TOPOLOGY_PROFILE, "qe.sqlite-topology/bridge-v1")
        self.assertEqual(
            BACKUP_TOPOLOGY_REGISTRY_FORMAT,
            "qe.sqlite-topology-registry/1",
        )
        self.assertEqual(
            BACKUP_TOPOLOGY_REGISTRY.registry_sha256,
            "97350bc7e6cf94f021ab7468e66b2dc66cc5bc07c239fbdae1a32328ed4925f6",
        )
        self.assertEqual(
            {profile.name: profile.profile_sha256 for profile in BACKUP_TOPOLOGY_REGISTRY.profiles},
            {
                "qe.domain-migration-0001/1": (
                    "ab209dc8dd55c7e26216ec12ea253628a0b07d90ffa357b0bb946129240d903e"
                ),
                "qe.domain-migration-0002/1": (
                    "319fc12130c89af1e787b15910deded5b2bb13eff815574d64073398e7b963e6"
                ),
                "qe.domain-migration-0003/1": (
                    "33af51710909aa4466961c517824778f13a99ca0f61c465349176bb9c5a72987"
                ),
                DOMAIN_MIGRATION_SIDECAR_PROFILE: (
                    "8ab5a58f463735962076f97395ead0d56b8cba42e9bb586426afd9bb102b4249"
                ),
                EVENT_STORE_CORE_PROFILE: (
                    "883bad37cfb6a088894b073495b4c6c588e461bb6faeeef9dde8442576d63316"
                ),
                LEGACY_MIGRATION_LEDGER_PROFILE: (
                    "23fe66dc01d9173d95b44c3278709a7042bc86aa8a0a51453672f169293f4e48"
                ),
                PROJECTION_STORE_PROFILE: (
                    "f83952022e79e9ee1fd32fb2a51b3ebbef38a11d015d595cc6066621d7fb7da5"
                ),
                REVOCATION_GUARD_PROFILE: (
                    "99d612e38a3336508d8c16d273c877fc39035e1d20335434b9eee86d6e705a66"
                ),
            },
        )

    def test_migration_profiles_are_exactly_bound_to_domain_registry(self):
        self.assertEqual(
            {
                profile.migration_id
                for profile in BACKUP_TOPOLOGY_REGISTRY.profiles
                if profile.migration_id is not None
            },
            {descriptor.migration_id for descriptor in DOMAIN_MIGRATION_REGISTRY.descriptors},
        )
        for descriptor in DOMAIN_MIGRATION_REGISTRY.descriptors:
            with self.subTest(migration_id=descriptor.migration_id):
                profile = BACKUP_TOPOLOGY_REGISTRY.migration_profile(descriptor.migration_id)
                self.assertEqual(
                    {
                        (item.object_type, item.name, item.ddl_sha256)
                        for item in profile.objects
                        if item.ddl_sha256 is not None
                    },
                    {
                        (item.object_type, item.name, item.ddl_sha256)
                        for item in descriptor.owned_objects
                    },
                )

    def test_full_current_database_catalog_matches_all_trusted_objects(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = str(Path(tempdir) / "complete.sqlite3")
            event_store = SQLiteEventStore(path, clock=lambda: T0)
            projection_store = SQLiteProjectionOffsetStore(path, clock=lambda: T0)
            revocation_guard = SQLiteRevocationRevisionGuard(path)
            event_store.close()
            projection_store.close()
            revocation_guard.close()

            connection = sqlite3.connect(path)
            try:
                source = inspect_schema_state(connection)
                target = apply_bridge_migration_plan(
                    connection,
                    plan_bridge_migrations(source),
                    clock=lambda: T0,
                )
                self.assertEqual(target.shape.value, "bridged_prefix")
                catalog_rows = connection.execute(
                    """
                    SELECT type, name, tbl_name, sql
                    FROM sqlite_schema
                    WHERE name NOT LIKE 'sqlite_stat%'
                    ORDER BY type, name
                    """
                ).fetchall()
            finally:
                connection.close()

        present_profiles = tuple(profile.name for profile in BACKUP_TOPOLOGY_REGISTRY.profiles)
        expected_objects = BACKUP_TOPOLOGY_REGISTRY.objects_for_profiles(present_profiles)
        expected = {
            (item.object_type, item.name): (item.table_name, item.ddl_sha256)
            for item in expected_objects
        }
        actual = {
            (object_type, name): (
                table_name,
                None if schema_sql is None else backup_schema_ddl_sha256(schema_sql),
            )
            for object_type, name, table_name, schema_sql in catalog_rows
        }

        self.assertEqual(len(expected), 58)
        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
