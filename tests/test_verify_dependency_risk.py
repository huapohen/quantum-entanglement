import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from io import StringIO
from unittest.mock import patch

import scripts.verify_dependency_risk as risk_cli
from scripts.dependency_risk import (
    DependencyRiskError,
    ExpectedComponent,
    RiskEvidenceContext,
    canonical_json,
    load_dependency_risk_policy_bytes,
    load_dependency_risk_result_bytes,
    parse_timestamp,
    verification_summary_document,
    verify_dependency_risk,
    vulnerability_finding_sha256,
)
from tests import test_dependency_risk_policy as policy_fixtures
from tests import test_dependency_risk_result as result_fixtures


class DependencyRiskVerifierTests(unittest.TestCase):
    DATABASE_BYTES = b"reviewed deterministic risk snapshot\n"
    EVALUATION_TIME = "2026-08-20T13:00:00Z"

    def assert_code(self, code, callback):
        with self.assertRaisesRegex(DependencyRiskError, f"^{code}$"):
            callback()

    @classmethod
    def enabled_policy_document(cls, database_sha256):
        document = policy_fixtures.DependencyRiskPolicyTests.policy_document()
        document["promotionEnabled"] = True
        document["evidence"]["allowedScanners"] = [
            {"name": "reviewed-scanner", "sha256": "0" * 64, "version": "1.0.0"}
        ]
        document["evidence"]["approvedDatabases"] = [
            {
                "revision": "snapshot-2026-08-20",
                "sha256": database_sha256,
                "source": "https://advisories.example.invalid/database",
            }
        ]
        document["licenses"]["allowedExpressions"] = ["MIT"]
        return document

    @classmethod
    def result_document(cls):
        document = result_fixtures.DependencyRiskResultTests.result_document()
        database_digest = cls.database_digest()
        document["database"]["byteSize"] = len(cls.DATABASE_BYTES)
        document["database"]["sha256"] = database_digest
        finding = document["scan"]["components"][1]["vulnerabilities"][0]
        finding["severity"] = "low"
        finding["fixStatus"] = "none"
        finding["fixedVersions"] = []
        return document

    @classmethod
    def database_digest(cls):
        import hashlib

        return hashlib.sha256(cls.DATABASE_BYTES).hexdigest()

    @staticmethod
    def context_from_result(result):
        return RiskEvidenceContext(
            project_name=result.project_name,
            project_version=result.project_version,
            commit_sha=result.commit_sha,
            tree_sha=result.tree_sha,
            artifacts=result.artifacts,
            distribution_manifest=result.distribution_manifest,
            lock_inventory=result.lock_inventory,
            sboms=result.sboms,
            components=tuple(
                ExpectedComponent(
                    purl=component.purl,
                    artifact_sha256=component.artifact_sha256,
                )
                for component in result.components
            ),
        )

    @classmethod
    def case(cls, *, result_document=None, policy_document=None):
        raw_result = result_document if result_document is not None else cls.result_document()
        raw_policy = (
            policy_document
            if policy_document is not None
            else cls.enabled_policy_document(cls.database_digest())
        )
        policy = load_dependency_risk_policy_bytes(canonical_json(raw_policy))
        raw_result["promotionPolicySha256"] = policy.sha256
        result = load_dependency_risk_result_bytes(canonical_json(raw_result))
        return policy, result, cls.context_from_result(result)

    @classmethod
    def verify(cls, policy, result, context, **overrides):
        arguments = {
            "database_snapshot": cls.DATABASE_BYTES,
            "evaluation_time": parse_timestamp(cls.EVALUATION_TIME, "test"),
        }
        arguments.update(overrides)
        return verify_dependency_risk(policy, result, context, **arguments)

    def test_exact_complete_allowed_evidence_produces_redacted_promote_summary(self):
        policy, result, context = self.case()
        summary = self.verify(policy, result, context)
        document = verification_summary_document(summary)

        self.assertEqual(document["decision"], "promote")
        self.assertEqual(document["componentCount"], 2)
        self.assertEqual(document["findingCount"], 1)
        self.assertEqual(document["appliedExceptionCount"], 0)
        encoded = canonical_json(document)
        self.assertNotIn(b"reviewed-scanner", encoded)
        self.assertNotIn(b"pkg:pypi", encoded)
        self.assertNotIn(b"OSV-2026-1", encoded)

    def test_source_manifest_lock_and_sbom_drift_fail_independently(self):
        policy, result, context = self.case()
        cases = (
            (
                "risk_source_drift",
                replace(context, commit_sha="c" * 40),
            ),
            (
                "risk_manifest_drift",
                replace(
                    context,
                    distribution_manifest=replace(context.distribution_manifest, sha256="c" * 64),
                ),
            ),
            (
                "risk_lock_drift",
                replace(
                    context,
                    lock_inventory=replace(context.lock_inventory, inventory_sha256="c" * 64),
                ),
            ),
            (
                "risk_sbom_drift",
                replace(
                    context,
                    sboms=(replace(context.sboms[0], sha256="c" * 64), context.sboms[1]),
                ),
            ),
        )
        for code, changed_context in cases:
            with self.subTest(code=code):
                self.assert_code(
                    code,
                    lambda value=changed_context: self.verify(policy, result, value),
                )

    def test_missing_component_and_same_purl_different_version_fail_coverage(self):
        policy, original, context = self.case()

        document = self.result_document()
        document["scan"]["components"].pop()
        policy, missing, _ = self.case(result_document=document)
        self.assert_code(
            "risk_component_coverage_mismatch",
            lambda: self.verify(policy, missing, context),
        )

        document = self.result_document()
        document["scan"]["components"][1]["purl"] = "pkg:pypi/setuptools@81.0.0"
        policy, wrong_version, _ = self.case(result_document=document)
        self.assert_code(
            "risk_component_coverage_mismatch",
            lambda: self.verify(policy, wrong_version, context),
        )
        self.assertEqual(original.components[1].purl, "pkg:pypi/setuptools@82.0.1")

    def test_partial_top_level_or_component_scan_fails_closed(self):
        _, original, context = self.case()
        for mutate in (
            lambda document: document["scan"].update(status="partial"),
            lambda document: document["scan"]["components"][0].update(scanStatus="error"),
        ):
            with self.subTest(mutate=mutate):
                document = self.result_document()
                mutate(document)
                policy, result, _ = self.case(result_document=document)
                self.assert_code(
                    "risk_scan_incomplete",
                    lambda checked_policy=policy, checked_result=result: self.verify(
                        checked_policy, checked_result, context
                    ),
                )
        self.assertEqual(original.scan_status, "complete")

    def test_unknown_severity_fix_and_license_cannot_silently_pass(self):
        _, _, context = self.case()

        document = self.result_document()
        finding = document["scan"]["components"][1]["vulnerabilities"][0]
        finding["severity"] = "unknown"
        policy, result, _ = self.case(result_document=document)
        self.assert_code("risk_severity_unknown", lambda: self.verify(policy, result, context))

        document = self.result_document()
        finding = document["scan"]["components"][1]["vulnerabilities"][0]
        finding["fixStatus"] = "unknown"
        policy, result, _ = self.case(result_document=document)
        self.assert_code("risk_fix_status_unknown", lambda: self.verify(policy, result, context))

        document = self.result_document()
        document["scan"]["components"][0]["license"] = {
            "expression": None,
            "status": "unknown",
        }
        policy, result, _ = self.case(result_document=document)
        self.assert_code("risk_policy_denied", lambda: self.verify(policy, result, context))

    def test_stale_database_overlong_validity_and_stale_result_fail_closed(self):
        policy, result, context = self.case()
        self.assert_code(
            "risk_database_stale",
            lambda: self.verify(
                policy,
                result,
                context,
                evaluation_time=parse_timestamp("2026-08-27T00:00:00Z", "test"),
            ),
        )

        document = self.enabled_policy_document(self.database_digest())
        document["evidence"]["maximumDatabaseValiditySeconds"] = 86400
        policy, result, context = self.case(policy_document=document)
        self.assert_code("risk_database_stale", lambda: self.verify(policy, result, context))

        document = self.enabled_policy_document(self.database_digest())
        document["evidence"]["maximumResultAgeSeconds"] = 1800
        policy, result, context = self.case(policy_document=document)
        self.assert_code("risk_result_stale", lambda: self.verify(policy, result, context))

    def test_scanner_database_integrity_policy_and_snapshot_are_exact(self):
        policy, result, context = self.case()
        self.assert_code(
            "risk_database_drift",
            lambda: self.verify(policy, result, context, database_snapshot=b"different snapshot\n"),
        )

        document = self.result_document()
        document["scanner"]["sha256"] = "a" * 64
        policy, result, context = self.case(result_document=document)
        self.assert_code("risk_scanner_unapproved", lambda: self.verify(policy, result, context))

        document = self.result_document()
        document["database"]["integrityStatus"] = "unverified"
        policy, result, context = self.case(result_document=document)
        self.assert_code(
            "risk_database_integrity_unverified",
            lambda: self.verify(policy, result, context),
        )

        policy, result, context = self.case()
        result = replace(result, promotion_policy_sha256="a" * 64)
        self.assert_code("risk_policy_drift", lambda: self.verify(policy, result, context))

    def exception_case(self, *, expires_at="2026-08-21T00:00:00Z", wrong_digest=False):
        result_document = self.result_document()
        finding = result_document["scan"]["components"][1]["vulnerabilities"][0]
        finding["severity"] = "high"
        finding["fixStatus"] = "available"
        finding["fixedVersions"] = ["82.0.2"]
        preliminary = load_dependency_risk_result_bytes(canonical_json(result_document))
        fingerprint = vulnerability_finding_sha256(
            preliminary.components[1], preliminary.components[1].vulnerabilities[0]
        )
        if wrong_digest:
            fingerprint = "f" * 64
        policy_document = self.enabled_policy_document(self.database_digest())
        policy_document["exceptions"]["records"] = [
            {
                "databaseSha256": self.database_digest(),
                "exceptionId": "RISK-2026-001",
                "expiresAt": expires_at,
                "findingId": "OSV-2026-1",
                "findingSha256": fingerprint,
                "issuedAt": "2026-08-20T00:00:00Z",
                "kind": "vulnerability",
                "owner": "security-team",
                "purl": "pkg:pypi/setuptools@82.0.1",
                "rationale": (
                    "Temporary exact finding acceptance with a tracked remediation owner."
                ),
            }
        ]
        return self.case(
            result_document=result_document,
            policy_document=policy_document,
        )

    def test_exact_waiver_is_consumed_once_and_redacted(self):
        policy, result, context = self.exception_case()
        summary = self.verify(policy, result, context)
        self.assertEqual(summary.applied_exception_count, 1)
        output = canonical_json(verification_summary_document(summary))
        self.assertNotIn(b"security-team", output)
        self.assertNotIn(b"RISK-2026-001", output)

    def test_expired_mismatched_and_unused_waivers_fail_closed(self):
        policy, result, context = self.exception_case(expires_at="2026-08-20T12:30:00Z")
        self.assert_code("risk_exception_expired", lambda: self.verify(policy, result, context))

        policy, result, context = self.exception_case(wrong_digest=True)
        self.assert_code("risk_policy_denied", lambda: self.verify(policy, result, context))

        policy, result, context = self.exception_case()
        result = replace(
            result,
            components=(
                result.components[0],
                replace(
                    result.components[1],
                    vulnerabilities=(
                        replace(
                            result.components[1].vulnerabilities[0],
                            severity="low",
                            fix_status="none",
                            fixed_versions=(),
                        ),
                    ),
                ),
            ),
        )
        context = replace(
            context,
            components=tuple(
                ExpectedComponent(item.purl, item.artifact_sha256) for item in result.components
            ),
        )
        self.assert_code("risk_exception_unused", lambda: self.verify(policy, result, context))

    def test_repository_default_policy_cannot_produce_promotion(self):
        policy_document = policy_fixtures.DependencyRiskPolicyTests.policy_document()
        policy, result, context = self.case(policy_document=policy_document)
        self.assert_code("risk_promotion_disabled", lambda: self.verify(policy, result, context))


class DependencyRiskCliTests(unittest.TestCase):
    def setUp(self):
        self.verifier = DependencyRiskVerifierTests()
        self.policy, self.result, self.context = self.verifier.case()

    @staticmethod
    def arguments():
        return [
            "--distribution-manifest",
            "/outside/manifest.json",
            "--sbom-directory",
            "/outside/sbom",
            "--result",
            "/outside/result-sensitive.json",
            "--database-snapshot",
            "/outside/database-sensitive.json",
            "--expected-commit",
            "a" * 40,
            "--evaluation-time",
            DependencyRiskVerifierTests.EVALUATION_TIME,
        ]

    def test_success_output_is_compact_canonical_and_redacted(self):
        stdout = StringIO()
        with (
            patch.object(risk_cli, "collect_risk_evidence_context", return_value=self.context),
            patch.object(risk_cli, "load_dependency_risk_policy", return_value=self.policy),
            patch.object(risk_cli, "require_outside_repository_file"),
            patch.object(risk_cli, "load_dependency_risk_result", return_value=self.result),
            patch.object(
                risk_cli,
                "read_database_snapshot",
                return_value=DependencyRiskVerifierTests.DATABASE_BYTES,
            ),
            redirect_stdout(stdout),
        ):
            exit_code = risk_cli.main(self.arguments())

        self.assertEqual(exit_code, 0)
        output = stdout.getvalue()
        self.assertEqual(json.loads(output)["decision"], "promote")
        self.assertEqual(output, output.strip() + "\n")
        self.assertNotIn("pkg:pypi", output)
        self.assertNotIn("reviewed-scanner", output)
        self.assertNotIn("sensitive", output)

    def test_verification_failure_prints_only_fixed_code(self):
        stderr = StringIO()
        with (
            patch.object(
                risk_cli,
                "collect_risk_evidence_context",
                side_effect=DependencyRiskError("risk_source_evidence_invalid"),
            ),
            redirect_stderr(stderr),
        ):
            exit_code = risk_cli.main(self.arguments())
        self.assertEqual(exit_code, 1)
        self.assertEqual(
            stderr.getvalue(),
            "dependency risk verification failed: risk_source_evidence_invalid\n",
        )
        self.assertNotIn("sensitive", stderr.getvalue())

    def test_unexpected_failure_is_redacted_and_exits_one(self):
        stderr = StringIO()
        with (
            patch.object(
                risk_cli,
                "collect_risk_evidence_context",
                side_effect=RuntimeError("sensitive implementation detail"),
            ),
            redirect_stderr(stderr),
        ):
            exit_code = risk_cli.main(self.arguments())
        self.assertEqual(exit_code, 1)
        self.assertEqual(
            stderr.getvalue(),
            "dependency risk verification failed: risk_internal_error\n",
        )
        self.assertNotIn("sensitive", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_argument_failure_is_redacted_and_exits_two(self):
        stderr = StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            risk_cli.main(["--result", "/outside/result-sensitive.json"])
        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(
            stderr.getvalue(), "dependency risk verification failed: argument_invalid\n"
        )
        self.assertNotIn("sensitive", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
