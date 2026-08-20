import copy
import json
import unittest
from pathlib import Path

from scripts.dependency_risk import (
    DependencyRiskError,
    canonical_json,
    canonical_license_expression,
    load_dependency_risk_policy_bytes,
    policy_summary,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = PROJECT_ROOT / "requirements" / "dependency-risk-policy.json"


class DependencyRiskPolicyTests(unittest.TestCase):
    @staticmethod
    def policy_document():
        return json.loads(POLICY_PATH.read_bytes())

    def parse(self, document):
        return load_dependency_risk_policy_bytes(canonical_json(document))

    def assert_code(self, code, callback):
        with self.assertRaisesRegex(DependencyRiskError, f"^{code}$"):
            callback()

    def test_repository_policy_is_canonical_versioned_and_blocks_promotion(self):
        policy = load_dependency_risk_policy_bytes(POLICY_PATH.read_bytes())

        self.assertFalse(policy.promotion_enabled)
        self.assertEqual(policy.allowed_scanners, ())
        self.assertEqual(policy.approved_databases, ())
        self.assertEqual(policy.allowed_license_expressions, ())
        self.assertEqual(policy.block_at_or_above, "high")
        self.assertEqual(policy.block_when_fix_available_at_or_above, "medium")
        self.assertEqual(len(policy.sha256), 64)

    def test_summary_is_deterministic_and_redacts_exception_content(self):
        document = self.policy_document()
        document["exceptions"]["records"] = [self.vulnerability_exception()]
        policy = self.parse(document)

        first = canonical_json(policy_summary(policy))
        second = canonical_json(policy_summary(policy))
        self.assertEqual(first, second)
        self.assertIn(b'"exceptionCount": 1', first)
        self.assertNotIn(b"security-team", first)
        self.assertNotIn(b"Temporary exact finding acceptance", first)
        self.assertNotIn(b"OSV-2026-1", first)

    def test_enabled_policy_requires_explicit_scanner_database_and_license_choices(self):
        document = self.policy_document()
        document["promotionEnabled"] = True
        self.assert_code("risk_policy_incomplete", lambda: self.parse(document))

        document["evidence"]["allowedScanners"] = [
            {"name": "reviewed-scanner", "sha256": "b" * 64, "version": "1.0.0"}
        ]
        document["evidence"]["approvedDatabases"] = [
            {
                "revision": "snapshot-2026-08-20",
                "sha256": "c" * 64,
                "source": "https://advisories.example.invalid/database",
            }
        ]
        document["licenses"]["allowedExpressions"] = ["Apache-2.0", "MIT"]
        policy = self.parse(document)
        self.assertTrue(policy.promotion_enabled)

    def test_unknown_fields_duplicates_and_noncanonical_bytes_are_rejected(self):
        document = self.policy_document()
        document["unknown"] = True
        self.assert_code("risk_policy_invalid", lambda: self.parse(document))
        self.assert_code(
            "risk_json_invalid",
            lambda: load_dependency_risk_policy_bytes(b'{"format":"a","format":"b"}\n'),
        )
        self.assert_code(
            "risk_json_noncanonical",
            lambda: load_dependency_risk_policy_bytes(
                canonical_json(self.policy_document()).rstrip() + b"  \n"
            ),
        )

    def test_thresholds_are_known_and_fix_threshold_cannot_be_weaker(self):
        for block_at, block_with_fix in (("urgent", "medium"), ("medium", "high")):
            with self.subTest(block_at=block_at, block_with_fix=block_with_fix):
                document = self.policy_document()
                document["vulnerabilities"] = {
                    "blockAtOrAbove": block_at,
                    "blockWhenFixAvailableAtOrAbove": block_with_fix,
                }
                self.assert_code(
                    "risk_policy_vulnerability_invalid", lambda value=document: self.parse(value)
                )

    def test_license_expressions_use_a_bounded_canonical_spdx_grammar(self):
        valid = (
            "MIT",
            "Apache-2.0 WITH LLVM-exception",
            "(MIT OR Apache-2.0) AND BSD-3-Clause",
            "LicenseRef-Reviewed",
        )
        for expression in valid:
            with self.subTest(expression=expression):
                self.assertEqual(canonical_license_expression(expression, "test"), expression)

        for expression, code in (
            ("NOASSERTION", "license_expression_unknown"),
            ("NONE", "license_expression_unknown"),
            ("MIT  OR Apache-2.0", "license_expression_noncanonical"),
            ("MIT OR", "license_expression_invalid"),
            ("MIT;GPL-3.0", "license_expression_invalid"),
            ("(MIT OR Apache-2.0", "license_expression_invalid"),
        ):
            with self.subTest(expression=expression):
                self.assert_code(
                    code,
                    lambda value=expression: canonical_license_expression(value, "test"),
                )

    @staticmethod
    def vulnerability_exception():
        return {
            "exceptionId": "RISK-2026-001",
            "databaseSha256": "b" * 64,
            "expiresAt": "2026-08-30T00:00:00Z",
            "findingSha256": "c" * 64,
            "findingId": "OSV-2026-1",
            "issuedAt": "2026-08-20T00:00:00Z",
            "kind": "vulnerability",
            "owner": "security-team",
            "purl": "pkg:pypi/example@1.0.0",
            "rationale": "Temporary exact finding acceptance with a tracked remediation owner.",
        }

    def test_exception_requires_exact_component_finding_result_and_bounded_approval(self):
        base = self.policy_document()
        base["exceptions"]["records"] = [self.vulnerability_exception()]
        policy = self.parse(base)
        self.assertEqual(
            policy.exceptions[0].scope,
            (
                "vulnerability",
                "pkg:pypi/example@1.0.0",
                "OSV-2026-1",
            ),
        )

        mutations = (
            ("risk_policy_exception_invalid", lambda item: item.update(purl="*")),
            ("risk_policy_exception_invalid", lambda item: item.pop("findingId")),
            ("risk_policy_exception_owner_invalid", lambda item: item.update(owner="*")),
            (
                "risk_policy_exception_rationale_invalid",
                lambda item: item.update(rationale="short"),
            ),
            (
                "risk_policy_exception_time_invalid",
                lambda item: item.update(expiresAt="2027-08-30T00:00:00Z"),
            ),
            ("risk_policy_exception_invalid", lambda item: item.update(findingSha256="A" * 64)),
        )
        for code, mutate in mutations:
            with self.subTest(code=code, mutate=mutate):
                document = copy.deepcopy(base)
                mutate(document["exceptions"]["records"][0])
                self.assert_code(code, lambda value=document: self.parse(value))

    def test_license_exception_can_target_only_one_exact_expression_or_unknown(self):
        base = self.vulnerability_exception()
        base.pop("findingId")
        base["kind"] = "license"
        base["licenseExpression"] = None
        document = self.policy_document()
        document["exceptions"]["records"] = [base]
        policy = self.parse(document)
        self.assertIsNone(policy.exceptions[0].subject)

        document["exceptions"]["records"][0]["licenseExpression"] = "GPL-3.0-only"
        policy = self.parse(document)
        self.assertEqual(policy.exceptions[0].subject, "GPL-3.0-only")

    def test_exception_ids_and_exact_scopes_must_be_sorted_and_unique(self):
        first = self.vulnerability_exception()
        second = copy.deepcopy(first)
        second["exceptionId"] = "RISK-2026-002"
        document = self.policy_document()
        document["exceptions"]["records"] = [second, first]
        self.assert_code("risk_policy_exception_invalid", lambda: self.parse(document))

        document["exceptions"]["records"] = [first, second]
        self.assert_code("risk_policy_exception_scope_invalid", lambda: self.parse(document))


if __name__ == "__main__":
    unittest.main()
