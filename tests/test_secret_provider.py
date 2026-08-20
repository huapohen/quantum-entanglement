import copy
import json
import pickle
import unittest

from quantum_entanglement.service import (
    SecretMaterial,
    SecretMaterialClosedError,
    SecretRef,
    SecretReferenceError,
)


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
