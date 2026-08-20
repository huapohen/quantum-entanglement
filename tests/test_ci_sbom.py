import unittest
from pathlib import Path


class PackageSbomWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow_path = Path(__file__).parents[1] / ".github" / "workflows" / "package.yml"
        cls.workflow = cls.workflow_path.read_text(encoding="utf-8")

    def test_release_lock_is_installed_after_manifest_verification_before_sbom_generation(self):
        manifest_verify = self.workflow.index("python scripts/distribution_manifest.py verify")
        release_install = self.workflow.index("-r requirements/release-py312.lock")
        sbom_generate = self.workflow.index("python scripts/sbom.py generate")

        self.assertLess(manifest_verify, release_install)
        self.assertLess(release_install, sbom_generate)
        install_step = self.workflow[manifest_verify:sbom_generate]
        self.assertIn("--require-hashes", install_step)
        self.assertIn("--only-binary :all:", install_step)

    def test_sboms_are_generated_outside_checkout_and_bound_to_source_commit(self):
        directory = "QE_SBOM_DIRECTORY: ${{ runner.temp }}/quantum-entanglement-sbom"
        self.assertGreaterEqual(self.workflow.count(directory), 3)
        self.assertIn('mkdir "$QE_SBOM_DIRECTORY"', self.workflow)
        self.assertIn("python scripts/sbom.py generate", self.workflow)
        self.assertIn("QE_EXPECTED_COMMIT: ${{ github.sha }}", self.workflow)
        self.assertIn('--distribution-manifest "$QE_DISTRIBUTION_MANIFEST_PATH"', self.workflow)
        self.assertIn('--sbom-directory "$QE_SBOM_DIRECTORY"', self.workflow)
        self.assertIn('--expected-commit "$QE_EXPECTED_COMMIT"', self.workflow)
        self.assertNotIn("github.workspace", self.workflow)

    def test_internal_verification_precedes_official_schema_validation(self):
        generate = self.workflow.index("python scripts/sbom.py generate")
        verify = self.workflow.index("python scripts/sbom.py verify")
        schema = self.workflow.index("JsonStrictValidator(SchemaVersion.V1_6)")

        self.assertLess(generate, verify)
        self.assertLess(verify, schema)
        self.assertIn("quantum-entanglement-runtime.cdx.json", self.workflow)
        self.assertIn("quantum-entanglement-build.cdx.json", self.workflow)
        self.assertIn("unexpected SBOM document set", self.workflow)

    def test_verified_sboms_are_retained_only_after_all_gates_pass(self):
        schema = self.workflow.index("JsonStrictValidator(SchemaVersion.V1_6)")
        smoke = self.workflow.index("Install and smoke-test wheel")
        upload = self.workflow.index(
            "uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
        )

        self.assertLess(schema, smoke)
        self.assertLess(smoke, upload)
        self.assertIn(
            "${{ runner.temp }}/quantum-entanglement-sbom/"
            "quantum-entanglement-runtime.cdx.json",
            self.workflow,
        )
        self.assertIn(
            "${{ runner.temp }}/quantum-entanglement-sbom/"
            "quantum-entanglement-build.cdx.json",
            self.workflow,
        )
        self.assertNotIn("if: ${{ always() }}", self.workflow)

    def test_sbom_failures_cannot_be_masked_or_uploaded_as_unverified_evidence(self):
        self.assertNotIn("continue-on-error: true", self.workflow)
        self.assertNotIn("|| true", self.workflow)
        self.assertNotIn("quantum-entanglement-sbom/*", self.workflow)
        self.assertEqual(self.workflow.count("python scripts/sbom.py generate"), 1)
        self.assertEqual(self.workflow.count("python scripts/sbom.py verify"), 1)


if __name__ == "__main__":
    unittest.main()
