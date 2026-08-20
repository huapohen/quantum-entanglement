import copy
import json
import os
import pickle
import tempfile
import unittest
from pathlib import Path

from quantum_entanglement.service import (
    FileSecretProvider,
    SecretMaterial,
    SecretMaterialClosedError,
    SecretProviderError,
    SecretRef,
    SecretReferenceError,
)


class FileSecretProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name) / "secrets"
        self.root.mkdir(mode=0o700)
        self.root.chmod(0o700)
        self.provider = FileSecretProvider(self.root)

    def write_secret(self, name: str, value: bytes, *, mode: int = 0o600) -> Path:
        path = self.root / name
        path.write_bytes(value)
        path.chmod(mode)
        return path

    def test_resolves_owner_only_regular_file(self) -> None:
        self.write_secret("signing-key", b"local-secret-value")

        material = self.provider.resolve(SecretRef.parse("file://signing-key"))

        with material as view:
            self.assertEqual(view.tobytes(), b"local-secret-value")

    def test_rejects_nested_locator_and_unsupported_scheme(self) -> None:
        with self.assertRaisesRegex(SecretProviderError, "secret_locator_unsafe"):
            self.provider.resolve(SecretRef.parse("file://nested/signing-key"))
        with self.assertRaisesRegex(SecretProviderError, "secret_scheme_unsupported"):
            self.provider.resolve(SecretRef.parse("vault://signing-key"))

    def test_rejects_permissive_secret_and_root_modes(self) -> None:
        secret = self.write_secret("signing-key", b"value", mode=0o640)
        reference = SecretRef.parse("file://signing-key")
        with self.assertRaisesRegex(SecretProviderError, "secret_file_unsafe"):
            self.provider.resolve(reference)

        secret.chmod(0o600)
        self.root.chmod(0o750)
        with self.assertRaisesRegex(SecretProviderError, "secret_root_unsafe"):
            self.provider.resolve(reference)

    def test_rejects_symlink_and_hard_link(self) -> None:
        source = self.write_secret("source-key", b"value")
        symlink = self.root / "symlink-key"
        symlink.symlink_to(source)
        hard_link = self.root / "hard-link-key"
        os.link(source, hard_link)

        for name in ("symlink-key", "source-key", "hard-link-key"):
            with self.subTest(name=name), self.assertRaises(SecretProviderError):
                self.provider.resolve(SecretRef.parse(f"file://{name}"))

    def test_rejects_empty_and_oversized_file(self) -> None:
        self.write_secret("empty-key", b"")
        self.write_secret("large-key", b"x" * 9)
        provider = FileSecretProvider(self.root, maximum_bytes=8)

        with self.assertRaisesRegex(SecretProviderError, "secret_empty"):
            provider.resolve(SecretRef.parse("file://empty-key"))
        with self.assertRaisesRegex(SecretProviderError, "secret_too_large"):
            provider.resolve(SecretRef.parse("file://large-key"))

    def test_provider_errors_and_rendering_do_not_expose_paths_or_values(self) -> None:
        value = "secret-canary-do-not-log"
        self.write_secret("unsafe-key", value.encode("ascii"), mode=0o644)
        reference = SecretRef.parse("file://unsafe-key")

        with self.assertRaises(SecretProviderError) as caught:
            self.provider.resolve(reference)

        rendered = f"{caught.exception!r} {caught.exception} {self.provider!r}"
        self.assertNotIn(value, rendered)
        self.assertNotIn(str(self.root), rendered)
        self.assertNotIn(reference.locator, rendered)

    def test_constructor_rejects_relative_root_and_invalid_limit(self) -> None:
        with self.assertRaises(ValueError):
            FileSecretProvider(Path("relative"))
        with self.assertRaises((TypeError, ValueError)):
            FileSecretProvider(self.root, maximum_bytes=True)
        with self.assertRaises(ValueError):
            FileSecretProvider(self.root, maximum_bytes=0)


class SecretMaterialTests(unittest.TestCase):
    def test_context_manager_exposes_read_only_view_then_wipes_it(self) -> None:
        material = SecretMaterial(b"correct horse battery staple")

        with material as secret_view:
            self.assertTrue(secret_view.readonly)
            self.assertEqual(secret_view.tobytes(), b"correct horse battery staple")
            with self.assertRaises(TypeError):
                secret_view[0] = 0

        self.assertTrue(material.closed)
        self.assertEqual(secret_view.tobytes(), bytes(len(secret_view)))

    def test_closed_material_cannot_be_reopened(self) -> None:
        material = SecretMaterial(b"one-use-value")
        material.close()
        material.close()

        with self.assertRaises(SecretMaterialClosedError):
            material.view()
        with self.assertRaises(SecretMaterialClosedError):
            material.__enter__()

    def test_rendering_and_json_never_expose_value(self) -> None:
        value = b"secret-canary-never-log-this"
        material = SecretMaterial(value)

        self.assertNotIn(value.decode("ascii"), str(material))
        self.assertNotIn(value.decode("ascii"), repr(material))
        with self.assertRaises(TypeError):
            json.dumps({"secret": material})
        material.close()

    def test_copy_and_pickle_are_rejected(self) -> None:
        material = SecretMaterial(b"do-not-copy")
        self.addCleanup(material.close)

        with self.assertRaises(TypeError):
            copy.copy(material)
        with self.assertRaises(TypeError):
            copy.deepcopy(material)
        with self.assertRaises(TypeError):
            pickle.dumps(material)

    def test_rejects_text_empty_and_oversized_material(self) -> None:
        with self.assertRaises(TypeError):
            SecretMaterial("plaintext")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            SecretMaterial(b"")
        with self.assertRaises(ValueError):
            SecretMaterial(b"x" * (SecretMaterial.MAX_BYTES + 1))


class SecretReferenceTests(unittest.TestCase):
    def test_parses_canonical_reference_without_secret_material(self) -> None:
        reference = SecretRef.parse("file://service-signing-key")

        self.assertEqual(reference.scheme, "file")
        self.assertEqual(reference.locator, "service-signing-key")
        self.assertEqual(reference.canonical, "file://service-signing-key")

    def test_rendering_exposes_only_stable_fingerprint(self) -> None:
        reference = SecretRef.parse("vault://production/team/service-key")

        self.assertNotIn(reference.locator, str(reference))
        self.assertNotIn(reference.locator, repr(reference))
        self.assertEqual(str(reference), str(reference))
        self.assertEqual(len(reference.fingerprint.split(":", 1)[1]), 12)

    def test_json_does_not_implicitly_serialize_reference(self) -> None:
        with self.assertRaises(TypeError):
            json.dumps({"key": SecretRef.parse("file://service-key")})

    def test_rejects_non_canonical_or_ambiguous_references(self) -> None:
        invalid = (
            "FILE://key",
            "file:///absolute",
            "file://../key",
            "file://directory/../key",
            "file://directory//key",
            "file://key?version=1",
            "file://key#fragment",
            "file://user@key",
            " file://key",
            "file://key ",
            "file://",
        )

        for value in invalid:
            with self.subTest(value=value), self.assertRaises(SecretReferenceError):
                SecretRef.parse(value)

    def test_constructor_enforces_the_same_parser_contract(self) -> None:
        with self.assertRaises(SecretReferenceError):
            SecretRef(scheme="file", locator="directory/../key")

    def test_rejects_non_string_input_without_coercion(self) -> None:
        with self.assertRaises(TypeError):
            SecretRef.parse(123)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            SecretRef(scheme="file", locator=123)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
