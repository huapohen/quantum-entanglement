import os
import tempfile
import unittest
from collections.abc import Iterator, Mapping
from pathlib import Path

from quantum_entanglement.service import ConfigurationError, RuntimeMode, ServiceConfig


class ChangingEnvironment(Mapping[str, str]):
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values
        self.reads: dict[str, int] = {}

    def __getitem__(self, key: str) -> str:
        self.reads[key] = self.reads.get(key, 0) + 1
        if key == "QE_CONNECTOR" and self.reads[key] > 1:
            return "feishu"
        return self.values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.values)

    def __len__(self) -> int:
        return len(self.values)


class ServiceConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        root = Path(self.temporary_directory.name).resolve()
        self.data_directory = root / "data"
        self.secret_root = root / "secrets"
        self.data_directory.mkdir(mode=0o700)
        self.secret_root.mkdir(mode=0o700)
        self.data_directory.chmod(0o700)
        self.secret_root.chmod(0o700)
        self.database_path = self.data_directory / "service.sqlite3"

    def environment(self) -> dict[str, str]:
        return {
            "PATH": "/host/path/is-not-configuration",
            "QE_BIND_HOST": "127.0.0.1",
            "QE_BIND_PORT": "8443",
            "QE_CONFIG_VERSION": "1",
            "QE_CONNECTOR": "fake",
            "QE_DATABASE_PATH": str(self.database_path),
            "QE_DATA_DIR": str(self.data_directory),
            "QE_DEBUG": "false",
            "QE_MAX_CONCURRENCY": "32",
            "QE_MAX_REQUEST_BYTES": "1048576",
            "QE_RUNTIME_MODE": "production",
            "QE_SECRET_ROOT": str(self.secret_root),
            "QE_SHUTDOWN_GRACE_SECONDS": "30",
        }

    def test_parses_complete_production_configuration(self) -> None:
        configuration = ServiceConfig.from_environment(self.environment())

        self.assertIs(configuration.runtime_mode, RuntimeMode.PRODUCTION)
        self.assertEqual(configuration.database_path, self.database_path)
        self.assertEqual(configuration.connector, "fake")
        self.assertFalse(configuration.debug)
        self.assertEqual(len(configuration.fingerprint), 16)
        self.assertNotIn(str(self.data_directory), repr(configuration))

    def test_ignores_non_qe_host_environment_but_rejects_unknown_qe_name(self) -> None:
        values = self.environment()
        values["AWS_SECRET_ACCESS_KEY"] = "host-value-is-not-read"
        ServiceConfig.from_environment(values)

        canary = "plaintext-key-must-never-render"
        values["QE_API_KEY"] = canary
        with self.assertRaises(ConfigurationError) as caught:
            ServiceConfig.from_environment(values)
        rendered = f"{caught.exception!r} {caught.exception}"
        self.assertEqual(caught.exception.code, "configuration_unknown_field")
        self.assertNotIn(canary, rendered)
        self.assertNotIn("QE_API_KEY", rendered)

        values = self.environment()
        values["QE_LOG\nFORGE"] = "not-rendered"
        with self.assertRaises(ConfigurationError) as forged:
            ServiceConfig.from_environment(values)
        self.assertNotIn("LOG", str(forged.exception))

    def test_requires_every_allowlisted_field_without_defaults(self) -> None:
        for field in sorted(key for key in self.environment() if key.startswith("QE_")):
            values = self.environment()
            del values[field]
            with self.subTest(field=field), self.assertRaises(ConfigurationError) as caught:
                ServiceConfig.from_environment(values)
            self.assertEqual(caught.exception.code, "configuration_missing_field")

    def test_rejects_debug_non_fake_connector_and_non_loopback_bind(self) -> None:
        invalid = (
            ("QE_DEBUG", "true", "production_debug_forbidden"),
            ("QE_CONNECTOR", "feishu", "connector_not_permitted"),
            ("QE_CONNECTOR", "wecom", "connector_not_permitted"),
            ("QE_BIND_HOST", "0.0.0.0", "bind_host_not_literal_loopback"),
            ("QE_BIND_HOST", "localhost", "bind_host_not_literal_loopback"),
        )
        for field, value, code in invalid:
            values = self.environment()
            values[field] = value
            with (
                self.subTest(field=field, value=value),
                self.assertRaises(ConfigurationError) as caught,
            ):
                ServiceConfig.from_environment(values)
            self.assertEqual(caught.exception.code, code)

    def test_rejects_relative_memory_and_out_of_directory_database_paths(self) -> None:
        for value in (":memory:", "relative.sqlite3", str(self.secret_root / "database.sqlite3")):
            values = self.environment()
            values["QE_DATABASE_PATH"] = value
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                ServiceConfig.from_environment(values)

    def test_rejects_overlapping_secret_and_data_roots(self) -> None:
        nested = self.data_directory / "secrets"
        nested.mkdir(mode=0o700)
        nested.chmod(0o700)
        values = self.environment()
        values["QE_SECRET_ROOT"] = str(nested)

        with self.assertRaisesRegex(ConfigurationError, "secret_root_overlaps_data"):
            ServiceConfig.from_environment(values)

    def test_rejects_symlinked_and_permissive_paths(self) -> None:
        linked_data = self.data_directory.parent / "linked-data"
        linked_data.symlink_to(self.data_directory, target_is_directory=True)
        values = self.environment()
        values["QE_DATA_DIR"] = str(linked_data)
        values["QE_DATABASE_PATH"] = str(linked_data / self.database_path.name)
        with self.assertRaisesRegex(ConfigurationError, "configuration_path_symlink"):
            ServiceConfig.from_environment(values)

        self.data_directory.chmod(0o750)
        with self.assertRaisesRegex(ConfigurationError, "configuration_path_permissions"):
            ServiceConfig.from_environment(self.environment())

    def test_rejects_group_or_world_writable_path_ancestor(self) -> None:
        unsafe_parent = self.data_directory.parent / "unsafe-parent"
        unsafe_parent.mkdir(mode=0o700)
        unsafe_data = unsafe_parent / "data"
        unsafe_data.mkdir(mode=0o700)
        unsafe_parent.chmod(0o777)
        self.addCleanup(unsafe_parent.chmod, 0o700)
        values = self.environment()
        values["QE_DATA_DIR"] = str(unsafe_data)
        values["QE_DATABASE_PATH"] = str(unsafe_data / "service.sqlite3")

        with self.assertRaisesRegex(
            ConfigurationError,
            "configuration_path_ancestor_permissions",
        ):
            ServiceConfig.from_environment(values)

    def test_rejects_unsafe_existing_database(self) -> None:
        self.database_path.write_bytes(b"not-a-real-database")
        self.database_path.chmod(0o640)
        with self.assertRaisesRegex(ConfigurationError, "configuration_path_permissions"):
            ServiceConfig.from_environment(self.environment())

        self.database_path.chmod(0o600)
        hard_link = self.data_directory / "linked.sqlite3"
        os.link(self.database_path, hard_link)
        with self.assertRaisesRegex(ConfigurationError, "database_link_count_unsafe"):
            ServiceConfig.from_environment(self.environment())

    def test_rejects_noncanonical_and_control_character_values(self) -> None:
        invalid = (
            ("QE_DATA_DIR", f"{self.data_directory}/../data"),
            ("QE_CONNECTOR", " fake"),
            ("QE_CONNECTOR", "fake\nproduction"),
            ("QE_CONNECTOR", "x" * 4_097),
        )
        for field, value in invalid:
            values = self.environment()
            values[field] = value
            with self.subTest(field=field), self.assertRaises(ConfigurationError):
                ServiceConfig.from_environment(values)

    def test_rejects_boolean_and_integer_coercion(self) -> None:
        invalid = (
            ("QE_DEBUG", "False"),
            ("QE_BIND_PORT", "08443"),
            ("QE_BIND_PORT", "0"),
            ("QE_BIND_PORT", "65536"),
            ("QE_MAX_REQUEST_BYTES", "1023"),
            ("QE_MAX_CONCURRENCY", "0"),
            ("QE_SHUTDOWN_GRACE_SECONDS", "301"),
        )
        for field, value in invalid:
            values = self.environment()
            values[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ConfigurationError):
                ServiceConfig.from_environment(values)

    def test_rejects_non_string_mapping_values_without_rendering_them(self) -> None:
        values = self.environment()
        values["QE_BIND_PORT"] = 8443  # type: ignore[assignment]
        with self.assertRaisesRegex(ConfigurationError, "configuration_type_invalid"):
            ServiceConfig.from_environment(values)

    def test_reads_a_mutable_mapping_only_once_into_a_bounded_snapshot(self) -> None:
        values = self.environment()
        values["AWS_SECRET_ACCESS_KEY"] = "ambient-secret-must-not-be-read"
        changing = ChangingEnvironment(values)

        configuration = ServiceConfig.from_environment(changing)

        self.assertEqual(configuration.connector, "fake")
        self.assertEqual(changing.reads["QE_CONNECTOR"], 1)
        self.assertNotIn("PATH", changing.reads)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", changing.reads)

    def test_rejects_oversized_environment_snapshot(self) -> None:
        values = self.environment()
        values.update({f"HOST_FIELD_{index}": "value" for index in range(4_097)})

        with self.assertRaisesRegex(ConfigurationError, "configuration_snapshot_too_large"):
            ServiceConfig.from_environment(values)


if __name__ == "__main__":
    unittest.main()
