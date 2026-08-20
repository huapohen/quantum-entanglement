import json
import unittest

from quantum_entanglement.service import SecretRef, SecretReferenceError


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
