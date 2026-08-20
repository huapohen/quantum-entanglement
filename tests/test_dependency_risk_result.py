import copy
import unittest

from scripts.dependency_risk import (
    DependencyRiskError,
    canonical_json,
    license_finding_sha256,
    load_dependency_risk_result_bytes,
    vulnerability_finding_sha256,
)


class DependencyRiskResultTests(unittest.TestCase):
    @staticmethod
    def result_document():
        return {
            "artifacts": [
                {
                    "byteSize": 120,
                    "filename": "quantum_entanglement-0.1.0.tar.gz",
                    "kind": "sdist",
                    "sha256": "1" * 64,
                },
                {
                    "byteSize": 100,
                    "filename": "quantum_entanglement-0.1.0-py3-none-any.whl",
                    "kind": "wheel",
                    "sha256": "2" * 64,
                },
            ],
            "database": {
                "byteSize": 4096,
                "expiresAt": "2026-08-27T00:00:00Z",
                "fetchedAt": "2026-08-20T00:00:00Z",
                "filename": "reviewed-risk-snapshot.json",
                "integrityStatus": "verified",
                "revision": "snapshot-2026-08-20",
                "sha256": "3" * 64,
                "source": "https://advisories.example.invalid/database",
            },
            "distributionManifest": {"byteSize": 1024, "sha256": "4" * 64},
            "format": "quantum-entanglement.dependency-risk-result",
            "lockInventory": {
                "inventorySha256": "5" * 64,
                "lockPolicySha256": "6" * 64,
                "packageRecordCount": 74,
                "targetCount": 4,
                "targets": [
                    {
                        "inputSha256": "7" * 64,
                        "lockSha256": "8" * 64,
                        "platform": "x86_64-unknown-linux-gnu",
                        "pythonVersion": "3.12",
                        "scope": "build",
                    },
                    {
                        "inputSha256": "9" * 64,
                        "lockSha256": "a" * 64,
                        "platform": "x86_64-unknown-linux-gnu",
                        "pythonVersion": "3.9",
                        "scope": "dev",
                    },
                    {
                        "inputSha256": "9" * 64,
                        "lockSha256": "b" * 64,
                        "platform": "x86_64-unknown-linux-gnu",
                        "pythonVersion": "3.12",
                        "scope": "dev",
                    },
                    {
                        "inputSha256": "c" * 64,
                        "lockSha256": "d" * 64,
                        "platform": "x86_64-unknown-linux-gnu",
                        "pythonVersion": "3.12",
                        "scope": "release",
                    },
                ],
            },
            "project": {"name": "quantum-entanglement", "version": "0.1.0"},
            "promotionPolicySha256": "6" * 64,
            "sboms": [
                {
                    "byteSize": 2200,
                    "filename": "quantum-entanglement-runtime.cdx.json",
                    "kind": "runtime",
                    "sha256": "e" * 64,
                },
                {
                    "byteSize": 79000,
                    "filename": "quantum-entanglement-build.cdx.json",
                    "kind": "build",
                    "sha256": "f" * 64,
                },
            ],
            "scan": {
                "completedAt": "2026-08-20T12:00:00Z",
                "components": [
                    {
                        "artifactSha256": ["1" * 64, "2" * 64],
                        "license": {"expression": "MIT", "status": "known"},
                        "purl": "pkg:pypi/quantum-entanglement@0.1.0",
                        "scanStatus": "complete",
                        "vulnerabilities": [],
                    },
                    {
                        "artifactSha256": ["8" * 64],
                        "license": {"expression": "MIT", "status": "known"},
                        "purl": "pkg:pypi/setuptools@82.0.1",
                        "scanStatus": "complete",
                        "vulnerabilities": [
                            {
                                "aliases": ["CVE-2026-0001"],
                                "fixedVersions": ["82.0.2"],
                                "fixStatus": "available",
                                "id": "OSV-2026-1",
                                "severity": "high",
                            }
                        ],
                    },
                ],
                "status": "complete",
            },
            "scanner": {
                "name": "reviewed-scanner",
                "sha256": "0" * 64,
                "version": "1.0.0",
            },
            "schemaVersion": 1,
            "source": {"commitSha": "a" * 40, "treeSha": "b" * 40},
        }

    def parse(self, document):
        return load_dependency_risk_result_bytes(canonical_json(document))

    def assert_code(self, code, callback):
        with self.assertRaisesRegex(DependencyRiskError, f"^{code}$"):
            callback()

    def test_result_is_canonical_versioned_and_binds_every_evidence_layer(self):
        result = self.parse(self.result_document())

        self.assertEqual(result.project_name, "quantum-entanglement")
        self.assertEqual(result.commit_sha, "a" * 40)
        self.assertEqual(result.artifacts[1].kind, "wheel")
        self.assertEqual(result.distribution_manifest.sha256, "4" * 64)
        self.assertEqual(result.lock_inventory.target_count, 4)
        self.assertEqual(result.lock_inventory.package_record_count, 74)
        self.assertEqual(result.sboms[0].kind, "runtime")
        self.assertEqual(result.scanner.sha256, "0" * 64)
        self.assertEqual(result.promotion_policy_sha256, "6" * 64)
        self.assertEqual(result.database.integrity_status, "verified")
        self.assertEqual(len(result.components), 2)
        self.assertEqual(len(result.sha256), 64)

    def test_unknown_fields_duplicate_keys_and_noncanonical_bytes_are_rejected(self):
        document = self.result_document()
        document["unknown"] = True
        self.assert_code("risk_result_invalid", lambda: self.parse(document))
        self.assert_code(
            "risk_json_invalid",
            lambda: load_dependency_risk_result_bytes(b'{"format":"a","format":"b"}\n'),
        )
        self.assert_code(
            "risk_json_noncanonical",
            lambda: load_dependency_risk_result_bytes(
                canonical_json(self.result_document()).rstrip() + b" \n"
            ),
        )

    def test_partial_status_is_representable_but_not_collapsed_into_complete(self):
        document = self.result_document()
        document["scan"]["status"] = "partial"
        document["scan"]["components"][0]["scanStatus"] = "error"
        result = self.parse(document)
        self.assertEqual(result.scan_status, "partial")
        self.assertEqual(result.components[0].scan_status, "error")

    def test_component_purls_are_versioned_sorted_and_unique(self):
        document = self.result_document()
        components = document["scan"]["components"]
        components.reverse()
        self.assert_code("risk_result_component_invalid", lambda: self.parse(document))

        document = self.result_document()
        document["scan"]["components"].append(copy.deepcopy(document["scan"]["components"][1]))
        self.assert_code("risk_result_component_duplicate", lambda: self.parse(document))

        document = self.result_document()
        document["scan"]["components"][1]["purl"] = "pkg:pypi/setuptools@81.0.0"
        result = self.parse(document)
        self.assertEqual(result.components[1].purl, "pkg:pypi/setuptools@81.0.0")

    def test_license_known_unknown_and_invalid_states_are_distinct(self):
        document = self.result_document()
        document["scan"]["components"][0]["license"] = {
            "expression": None,
            "status": "unknown",
        }
        result = self.parse(document)
        self.assertIsNone(result.components[0].license.expression)

        for license_value in (
            {"expression": None, "status": "known"},
            {"expression": "MIT", "status": "unknown"},
            {"expression": "NOASSERTION", "status": "known"},
        ):
            with self.subTest(license_value=license_value):
                document = self.result_document()
                document["scan"]["components"][0]["license"] = license_value
                expected = (
                    "license_expression_unknown"
                    if license_value["expression"] == "NOASSERTION"
                    else "risk_result_license_invalid"
                )
                self.assert_code(expected, lambda value=document: self.parse(value))

    def test_severity_and_fix_status_preserve_unknown_and_consistency(self):
        document = self.result_document()
        finding = document["scan"]["components"][1]["vulnerabilities"][0]
        finding["severity"] = "unknown"
        finding["fixStatus"] = "unknown"
        finding["fixedVersions"] = []
        result = self.parse(document)
        self.assertEqual(result.components[1].vulnerabilities[0].severity, "unknown")
        self.assertEqual(result.components[1].vulnerabilities[0].fix_status, "unknown")

        mutations = (
            lambda item: item.update(severity="urgent"),
            lambda item: item.update(fixStatus="maybe"),
            lambda item: item.update(fixStatus="none"),
            lambda item: item.update(fixedVersions=[]),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                document = self.result_document()
                mutate(document["scan"]["components"][1]["vulnerabilities"][0])
                self.assert_code(
                    "risk_result_finding_invalid", lambda value=document: self.parse(value)
                )

    def test_finding_ids_aliases_and_records_cannot_be_ambiguous_or_duplicate(self):
        document = self.result_document()
        finding = document["scan"]["components"][1]["vulnerabilities"][0]
        duplicate = copy.deepcopy(finding)
        duplicate["id"] = "CVE-2026-0001"
        duplicate["aliases"] = []
        document["scan"]["components"][1]["vulnerabilities"].append(duplicate)
        self.assert_code("risk_result_finding_duplicate", lambda: self.parse(document))

        document = self.result_document()
        duplicate = copy.deepcopy(document["scan"]["components"][1]["vulnerabilities"][0])
        duplicate["id"] = "OSV-2026-2"
        duplicate["aliases"] = []
        document["scan"]["components"][1]["vulnerabilities"].append(duplicate)
        result = self.parse(document)
        self.assertEqual(len(result.components[1].vulnerabilities), 2)

    def test_database_integrity_state_and_time_window_are_explicit(self):
        for status in ("unverified", "failed"):
            document = self.result_document()
            document["database"]["integrityStatus"] = status
            self.assertEqual(self.parse(document).database.integrity_status, status)

        document = self.result_document()
        document["database"]["integrityStatus"] = "unknown"
        self.assert_code("risk_result_database_invalid", lambda: self.parse(document))

        document = self.result_document()
        document["database"]["expiresAt"] = document["database"]["fetchedAt"]
        self.assert_code("risk_result_database_invalid", lambda: self.parse(document))

    def test_fingerprints_bind_purl_artifacts_license_severity_and_fix_data(self):
        result = self.parse(self.result_document())
        project = result.components[0]
        dependency = result.components[1]
        finding = dependency.vulnerabilities[0]
        license_digest = license_finding_sha256(project)
        vulnerability_digest = vulnerability_finding_sha256(dependency, finding)

        document = self.result_document()
        document["scan"]["components"][0]["license"]["expression"] = "Apache-2.0"
        changed = self.parse(document)
        self.assertNotEqual(license_finding_sha256(changed.components[0]), license_digest)

        document = self.result_document()
        document["scan"]["components"][1]["vulnerabilities"][0]["severity"] = "critical"
        changed = self.parse(document)
        self.assertNotEqual(
            vulnerability_finding_sha256(
                changed.components[1], changed.components[1].vulnerabilities[0]
            ),
            vulnerability_digest,
        )


if __name__ == "__main__":
    unittest.main()
