import unittest
from pathlib import Path


class ReleaseEvidenceWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow_path = Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml"
        cls.workflow = cls.workflow_path.read_text(encoding="utf-8")
        marker = "  release-evidence:\n"
        if cls.workflow.count(marker) != 1:
            raise AssertionError("ci workflow must contain exactly one release-evidence job")
        cls.job = marker + cls.workflow.split(marker, 1)[1]

    def test_evidence_job_uses_clean_checkout_without_persisted_credentials(self):
        self.assertIn("name: Canonical release evidence", self.job)
        self.assertIn(
            "uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
            self.job,
        )
        self.assertIn("persist-credentials: false", self.job)
        self.assertNotIn("permissions: write", self.job)

    def test_generator_output_lives_outside_checkout_and_is_not_masked(self):
        output = (
            "QE_RELEASE_EVIDENCE_PATH: "
            "${{ runner.temp }}/quantum-entanglement-release-evidence.json"
        )
        self.assertGreaterEqual(self.job.count(output), 2)
        self.assertIn("python scripts/generate_release_evidence.py", self.job)
        self.assertIn('> "$QE_RELEASE_EVIDENCE_PATH"', self.job)
        self.assertNotIn("continue-on-error: true", self.job)
        self.assertNotIn("|| true", self.job)
        self.assertNotIn("github.workspace", self.job)

    def test_verifier_binds_canonical_evidence_to_github_source_sha(self):
        self.assertIn("python scripts/verify_release_evidence.py", self.job)
        self.assertIn("QE_EXPECTED_COMMIT: ${{ github.sha }}", self.job)
        self.assertIn('--expected-commit "$QE_EXPECTED_COMMIT"', self.job)
        self.assertIn("--repository-root .", self.job)
        self.assertLess(
            self.job.index("python scripts/generate_release_evidence.py"),
            self.job.index("python scripts/verify_release_evidence.py"),
        )

    def test_pass_or_fail_evidence_is_retained_after_verification(self):
        upload = self.job.index(
            "uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
        )
        self.assertGreater(upload, self.job.index("python scripts/verify_release_evidence.py"))
        self.assertIn("if: ${{ always() }}", self.job)
        self.assertIn("if-no-files-found: error", self.job)
        self.assertIn("retention-days: 14", self.job)
        self.assertIn("github.run_id", self.job)
        self.assertIn("github.run_attempt", self.job)

    def test_job_installs_the_declared_dev_toolchain_before_generation(self):
        install = self.job.index("python -m pip install '.[dev]'")
        generate = self.job.index("python scripts/generate_release_evidence.py")
        self.assertLess(install, generate)
        self.assertIn('python-version: "3.12"', self.job)


if __name__ == "__main__":
    unittest.main()
