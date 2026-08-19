import unittest
from pathlib import Path


class PackageManifestWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow_path = Path(__file__).parents[1] / ".github" / "workflows" / "package.yml"
        cls.workflow = cls.workflow_path.read_text(encoding="utf-8")

    def test_package_checkout_does_not_persist_git_credentials(self):
        self.assertIn("uses: actions/checkout@v7", self.workflow)
        self.assertIn("persist-credentials: false", self.workflow)
        self.assertNotIn("permissions: write", self.workflow)

    def test_build_epoch_is_bound_to_commit_before_build(self):
        epoch = 'echo "SOURCE_DATE_EPOCH=$(git show -s --format=%ct HEAD)"'
        build = "python -m build"
        self.assertIn(epoch, self.workflow)
        self.assertLess(self.workflow.index(epoch), self.workflow.index(build))

    def test_manifest_is_generated_outside_checkout_then_strictly_verified(self):
        path = (
            "QE_DISTRIBUTION_MANIFEST_PATH: "
            "${{ runner.temp }}/quantum-entanglement-distribution-manifest.json"
        )
        generate = "python scripts/distribution_manifest.py generate"
        verify = "python scripts/distribution_manifest.py verify"
        self.assertGreaterEqual(self.workflow.count(path), 2)
        self.assertIn(generate, self.workflow)
        self.assertIn(verify, self.workflow)
        self.assertLess(self.workflow.index("python -m build"), self.workflow.index(generate))
        self.assertLess(self.workflow.index(generate), self.workflow.index(verify))
        self.assertIn("--distribution-directory dist", self.workflow)
        self.assertNotIn("github.workspace", self.workflow)

    def test_verifier_binds_archives_to_github_sha(self):
        self.assertIn("QE_EXPECTED_COMMIT: ${{ github.sha }}", self.workflow)
        self.assertIn('--expected-commit "$QE_EXPECTED_COMMIT"', self.workflow)
        self.assertNotIn("continue-on-error: true", self.workflow)
        self.assertNotIn("|| true", self.workflow)

    def test_only_verified_distributions_and_manifest_are_uploaded(self):
        verify = self.workflow.index("python scripts/distribution_manifest.py verify")
        smoke = self.workflow.index("Install and smoke-test wheel")
        upload = self.workflow.index("uses: actions/upload-artifact@v7")
        self.assertLess(verify, smoke)
        self.assertLess(smoke, upload)
        self.assertIn("dist/*", self.workflow)
        self.assertIn(
            "${{ runner.temp }}/quantum-entanglement-distribution-manifest.json",
            self.workflow,
        )
        self.assertIn("if-no-files-found: error", self.workflow)
        self.assertNotIn("if: ${{ always() }}", self.workflow)


if __name__ == "__main__":
    unittest.main()
