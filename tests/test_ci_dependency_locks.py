import unittest
from pathlib import Path


class DependencyLockWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow_path = Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml"
        cls.workflow = cls.workflow_path.read_text(encoding="utf-8")
        cls.test_job = cls.workflow.split("  release-evidence:\n", 1)[0]

    def test_test_matrix_maps_each_supported_runtime_to_an_exact_lock(self):
        self.assertIn(
            'python-version: "3.9"\n            lockfile: requirements/dev-py39.lock',
            self.test_job,
        )
        self.assertIn(
            'python-version: "3.12"\n            lockfile: requirements/dev-py312.lock',
            self.test_job,
        )
        self.assertIn("cache-dependency-path: ${{ matrix.lockfile }}", self.test_job)

    def test_test_job_verifies_then_installs_hash_locked_tools(self):
        verify = self.test_job.index("python scripts/verify_dependency_locks.py")
        install = self.test_job.index('-r "${{ matrix.lockfile }}"')
        package = self.test_job.index("--no-build-isolation")
        tests = self.test_job.index("python -m pytest -q")

        self.assertLess(verify, install)
        self.assertLess(install, package)
        self.assertLess(package, tests)
        self.assertIn("--require-hashes", self.test_job)
        self.assertIn("--only-binary :all:", self.test_job)
        self.assertIn("--no-deps", self.test_job)
        self.assertNotIn("python -m pip install .\n", self.test_job)

    def test_lock_failures_are_not_masked(self):
        self.assertNotIn("continue-on-error: true", self.workflow)
        self.assertNotIn("|| true", self.workflow)


class PackageLockWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow_path = Path(__file__).parents[1] / ".github" / "workflows" / "package.yml"
        cls.workflow = cls.workflow_path.read_text(encoding="utf-8")

    def test_package_job_verifies_and_installs_the_build_lock(self):
        verify = self.workflow.index("python scripts/verify_dependency_locks.py")
        install = self.workflow.index("-r requirements/build-py312.lock")
        build = self.workflow.index("python -m build --no-isolation")

        self.assertLess(verify, install)
        self.assertLess(install, build)
        self.assertIn("cache-dependency-path: requirements/build-py312.lock", self.workflow)
        self.assertIn("--require-hashes", self.workflow)
        self.assertIn("--only-binary :all:", self.workflow)
        self.assertNotIn("python -m pip install build", self.workflow)

    def test_both_distribution_builds_disable_dependency_resolution(self):
        self.assertEqual(self.workflow.count("python -m build"), 2)
        self.assertEqual(self.workflow.count("--no-isolation"), 2)
        self.assertNotIn("continue-on-error: true", self.workflow)
        self.assertNotIn("|| true", self.workflow)


if __name__ == "__main__":
    unittest.main()
