import unittest
from pathlib import Path


class PackageManifestWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repository_root = Path(__file__).parents[1]
        cls.workflow_path = cls.repository_root / ".github" / "workflows" / "package.yml"
        cls.workflow = cls.workflow_path.read_text(encoding="utf-8")

    def test_sdist_explicitly_includes_test_package_marker(self):
        manifest = (self.repository_root / "MANIFEST.in").read_text(encoding="utf-8")

        self.assertEqual(manifest, "include tests/__init__.py\n")

    def test_project_metadata_matches_the_verified_python_compatibility_window(self):
        pyproject = (self.repository_root / "pyproject.toml").read_text(encoding="utf-8")

        self.assertIn('requires-python = ">=3.9,<3.14"', pyproject)
        self.assertNotIn('requires-python = ">=3.9"', pyproject)

    def test_package_checkout_does_not_persist_git_credentials(self):
        self.assertIn(
            "uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
            self.workflow,
        )
        self.assertIn("persist-credentials: false", self.workflow)
        self.assertNotIn("permissions: write", self.workflow)

    def test_build_epoch_is_bound_to_commit_before_build(self):
        epoch = 'echo "SOURCE_DATE_EPOCH=$(git show -s --format=%ct HEAD)"'
        build = "python -m build"
        self.assertIn(epoch, self.workflow)
        self.assertLess(self.workflow.index(epoch), self.workflow.index(build))

    def test_source_distribution_is_canonicalized_before_manifest(self):
        build = "python -m build"
        normalize = "python scripts/normalize_sdist.py"
        manifest = "python scripts/distribution_manifest.py generate"
        self.assertIn(normalize, self.workflow)
        self.assertLess(self.workflow.index(build), self.workflow.index(normalize))
        self.assertLess(self.workflow.index(normalize), self.workflow.index(manifest))
        self.assertIn("--distribution-directory dist", self.workflow)

    def test_reproducibility_is_verified_from_an_independent_checkout(self):
        worktree = 'git worktree add --detach "$QE_REBUILD_SOURCE" "$QE_EXPECTED_COMMIT"'
        rebuild = (
            "python -m build \\\n"
            "            --no-isolation \\\n"
            '            --outdir "$QE_REBUILD_DIRECTORY" \\\n'
            '            "$QE_REBUILD_SOURCE"'
        )
        compare = "python scripts/verify_reproducible_distributions.py"
        manifest = "python scripts/distribution_manifest.py generate"
        self.assertIn("QE_EXPECTED_COMMIT: ${{ github.sha }}", self.workflow)
        self.assertIn("QE_REBUILD_SOURCE: ${{ runner.temp }}", self.workflow)
        self.assertIn("QE_REBUILD_DIRECTORY: ${{ runner.temp }}", self.workflow)
        self.assertIn(worktree, self.workflow)
        self.assertIn(rebuild, self.workflow)
        self.assertIn(compare, self.workflow)
        self.assertIn("--reference-directory dist", self.workflow)
        self.assertIn('--candidate-directory "$QE_REBUILD_DIRECTORY"', self.workflow)
        self.assertLess(self.workflow.index(worktree), self.workflow.index(rebuild))
        self.assertLess(self.workflow.index(rebuild), self.workflow.rindex(compare))
        self.assertLess(self.workflow.rindex(compare), self.workflow.index(manifest))
        self.assertNotIn("continue-on-error: true", self.workflow)

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
        upload = self.workflow.index(
            "uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
        )
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
