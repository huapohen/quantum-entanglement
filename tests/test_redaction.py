import json
import unittest

from quantum_entanglement.service import RedactionPolicy, Redactor


class ExplosiveObject:
    def __str__(self) -> str:
        raise AssertionError("redactor must not stringify unknown objects")

    def __repr__(self) -> str:
        raise AssertionError("redactor must not render unknown objects")


class RedactorTests(unittest.TestCase):
    def test_redacts_sensitive_keys_at_every_nested_level(self) -> None:
        canaries = {
            "Authorization": "Bearer authorization-canary",
            "nested": {
                "api_key": "sk-api-key-canary-value",
                "client_secret": "client-secret-canary",
                "X-API-Key": "x-api-key-canary",
                "idToken": "id-token-canary",
                "session_token_value": "session-token-canary",
                "payload": {"message": "payload-canary"},
                "Cookie": "session=cookie-canary",
            },
            "items": [{"lease-token": "lease-canary"}, {"prompt": "prompt-canary"}],
        }

        rendered = json.dumps(Redactor().sanitize(canaries), sort_keys=True)

        for canary in (
            "authorization-canary",
            "api-key-canary",
            "client-secret-canary",
            "x-api-key-canary",
            "id-token-canary",
            "session-token-canary",
            "payload-canary",
            "cookie-canary",
            "lease-canary",
            "prompt-canary",
        ):
            self.assertNotIn(canary, rendered)
        self.assertIn("<redacted>", rendered)

    def test_redacts_known_inline_credential_shapes(self) -> None:
        jwt = "a" * 20 + "." + "b" * 20 + "." + "c" * 20
        value = (
            "Bearer abcdefghijklmnop "
            "https://user:password@example.invalid/path "
            "sk-abcdefghijklmnop "
            f"{jwt}"
        )

        rendered = Redactor().sanitize(value)

        self.assertNotIn("abcdefghijklmnop", rendered)
        self.assertNotIn("user:password", rendered)
        self.assertNotIn(jwt, rendered)

    def test_exception_message_and_unknown_object_are_never_rendered(self) -> None:
        canary = "exception-secret-canary"
        sanitized = Redactor().sanitize(
            {
                "error": RuntimeError(canary),
                "unknown": ExplosiveObject(),
            }
        )
        rendered = json.dumps(sanitized, sort_keys=True)

        self.assertNotIn(canary, rendered)
        self.assertIn("RuntimeError", rendered)
        self.assertIn("<redacted:object>", rendered)

    def test_cycles_and_depth_are_bounded_and_json_safe(self) -> None:
        cyclic: list[object] = []
        cyclic.append(cyclic)
        deep: object = "leaf"
        for _ in range(20):
            deep = [deep]

        sanitized = Redactor(RedactionPolicy(maximum_depth=3)).sanitize(
            {"cycle": cyclic, "deep": deep}
        )
        rendered = json.dumps(sanitized, sort_keys=True)

        self.assertIn("<redacted:cycle>", rendered)
        self.assertIn("<redacted:depth-limit>", rendered)

    def test_item_string_and_number_limits_fail_closed(self) -> None:
        redactor = Redactor(RedactionPolicy(maximum_depth=2, maximum_items=2, maximum_string=8))

        sanitized = redactor.sanitize(
            {
                "one": "1234567890",
                "two": float("nan"),
                "three": "not emitted",
            }
        )
        rendered = json.dumps(sanitized, sort_keys=True)

        self.assertIn("<truncated>", rendered)
        self.assertIn("<redacted:non-finite-number>", rendered)
        self.assertEqual(sanitized["truncatedFields"], 1)

    def test_bytes_and_invalid_keys_never_render_content(self) -> None:
        canary = b"binary-secret-canary"

        rendered = json.dumps(
            Redactor().sanitize({"valid": canary, "bad\nkey": "log-forge"}),
            sort_keys=True,
        )

        self.assertNotIn(canary.decode("ascii"), rendered)
        self.assertNotIn("bad\\nkey", rendered)
        self.assertIn("invalidField", rendered)

    def test_policy_rejects_unbounded_or_coerced_limits(self) -> None:
        with self.assertRaises(TypeError):
            RedactionPolicy(maximum_depth=True)
        with self.assertRaises(ValueError):
            RedactionPolicy(maximum_items=0)
        with self.assertRaises(ValueError):
            RedactionPolicy(maximum_string=4_097)


if __name__ == "__main__":
    unittest.main()
