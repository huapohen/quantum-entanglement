import hashlib
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from scripts.verify_dependency_locks import (
    DependencyLockError,
    main,
    verify_dependency_locks,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class DependencyLockTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.repository = Path(self.tempdir.name) / "repository"
        self.repository.mkdir()
        shutil.copy2(PROJECT_ROOT / "pyproject.toml", self.repository / "pyproject.toml")
        shutil.copytree(PROJECT_ROOT / "requirements", self.repository / "requirements")

    def tearDown(self):
        self.tempdir.cleanup()

    @property
    def policy_path(self):
        return self.repository / "requirements" / "lock-policy.json"

    def _read_policy(self):
        return json.loads(self.policy_path.read_text(encoding="ascii"))

    def _write_policy(self, policy):
        self.policy_path.write_text(
            json.dumps(
                policy,
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
                separators=(",", ": "),
            )
            + "\n",
            encoding="ascii",
        )

    def _refresh_digest(self, index, field, relative_path):
        policy = self._read_policy()
        value = (self.repository / relative_path).read_bytes()
        policy["locks"][index][field] = hashlib.sha256(value).hexdigest()
        self._write_policy(policy)

    def _assert_code(self, code):
        with self.assertRaisesRegex(DependencyLockError, f"^{code}$"):
            verify_dependency_locks(self.repository)

    def test_repository_inventory_is_valid_and_source_aligned(self):
        targets = verify_dependency_locks(self.repository)

        self.assertEqual(
            [(target.scope, target.python_version) for target in targets],
            [("build", "3.12"), ("dev", "3.9"), ("dev", "3.12"), ("release", "3.12")],
        )
        self.assertTrue(all(target.packages for target in targets))
        self.assertEqual(
            {package.name for package in targets[-1].roots},
            {"build", "cyclonedx-bom", "pip", "ruff", "setuptools"},
        )
        self.assertTrue(
            all("pip" in {package.name for package in target.roots} for target in targets)
        )

    def test_input_and_lock_digest_drift_are_rejected(self):
        for field, relative_path in (
            ("inputSha256", "requirements/build.in"),
            ("lockSha256", "requirements/build-py312.lock"),
        ):
            with self.subTest(field=field):
                path = self.repository / relative_path
                original = path.read_bytes()
                path.write_bytes(original + b"\n")
                self._assert_code("lock_digest_mismatch")
                path.write_bytes(original)

    def test_unpinned_input_and_unsorted_roots_are_rejected_after_rebinding(self):
        path = self.repository / "requirements" / "build.in"
        for value in (b"build>=1.4.4\nsetuptools==82.0.1\n", b"setuptools==82.0.1\nbuild==1.4.4\n"):
            with self.subTest(value=value):
                path.write_bytes(value)
                self._refresh_digest(0, "inputSha256", "requirements/build.in")
                self._assert_code("lock_input_invalid")

    def test_unhashed_url_and_non_binary_lock_syntax_is_rejected_after_rebinding(self):
        path = self.repository / "requirements" / "build-py312.lock"
        original = path.read_text(encoding="ascii")
        mutations = (
            original.replace("--hash=sha256:", "--hash=sha512:", 1),
            original.replace(
                "--only-binary :all:", "--index-url https://example.invalid/simple", 1
            ),
            original.replace("--only-binary :all:", "--only-binary setuptools", 1),
        )
        for value in mutations:
            changed_line = next(
                line
                for line in value.splitlines()
                if "sha512" in line or "--index" in line or "--only-binary setuptools" in line
            )
            with self.subTest(first_changed_line=changed_line):
                path.write_text(value, encoding="ascii")
                self._refresh_digest(0, "lockSha256", "requirements/build-py312.lock")
                self._assert_code("lock_file_invalid")

    def test_duplicate_and_unsorted_hashes_are_rejected_after_rebinding(self):
        path = self.repository / "requirements" / "build-py312.lock"
        original = path.read_text(encoding="ascii")
        lines = original.splitlines()
        first_hash = next(index for index, line in enumerate(lines) if "--hash=sha256:" in line)
        duplicate = list(lines)
        duplicate[first_hash + 1] = duplicate[first_hash].removesuffix(" \\")
        unsorted = list(lines)
        unsorted[first_hash], unsorted[first_hash + 1] = (
            unsorted[first_hash + 1] + " \\",
            unsorted[first_hash].removesuffix(" \\"),
        )
        duplicate_value = "\n".join(duplicate) + "\n"
        unsorted_value = "\n".join(unsorted) + "\n"
        for value in (duplicate_value, unsorted_value):
            with self.subTest(kind="duplicate" if value == duplicate_value else "unsorted"):
                path.write_text(value, encoding="ascii")
                self._refresh_digest(0, "lockSha256", "requirements/build-py312.lock")
                self._assert_code("lock_file_invalid")

    def test_root_version_must_match_the_resolved_lock(self):
        path = self.repository / "requirements" / "build-py312.lock"
        value = path.read_text(encoding="ascii").replace("build==1.4.4 \\", "build==1.4.3 \\", 1)
        path.write_text(value, encoding="ascii")
        self._refresh_digest(0, "lockSha256", "requirements/build-py312.lock")

        self._assert_code("lock_root_mismatch")

    def test_noncanonical_duplicate_and_expanded_policies_are_rejected(self):
        original = self.policy_path.read_bytes()
        self.policy_path.write_bytes(original + b" ")
        self._assert_code("lock_policy_noncanonical")

        self.policy_path.write_text('{"format":"a","format":"b"}\n', encoding="ascii")
        self._assert_code("lock_policy_invalid")

        self.policy_path.write_bytes(original)
        policy = self._read_policy()
        policy["locks"].append(dict(policy["locks"][-1]))
        self._write_policy(policy)
        self._assert_code("lock_inventory_invalid")

    def test_target_path_and_matrix_cannot_escape_the_declared_inventory(self):
        policy = self._read_policy()
        policy["locks"][0]["lock"] = "requirements/../pyproject.toml"
        policy["locks"][0]["pythonVersion"] = "3.13"
        self._write_policy(policy)

        self._assert_code("lock_inventory_invalid")

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are not supported")
    def test_symlinked_lock_inputs_are_rejected_before_digesting(self):
        path = self.repository / "requirements" / "build.in"
        path.unlink()
        path.symlink_to("dev.in")

        self._assert_code("lock_input_invalid")

    def test_oversized_lock_is_rejected_before_digesting(self):
        path = self.repository / "requirements" / "build-py312.lock"
        path.write_bytes(b"x" * (2 * 1024 * 1024 + 1))

        self._assert_code("lock_file_invalid")

    def test_pyproject_build_and_dev_roots_cannot_drift(self):
        path = self.repository / "pyproject.toml"
        original = path.read_text(encoding="utf-8")
        for old, new in (
            ("setuptools==82.0.1", "setuptools==82.0.0"),
            ("ruff==0.16.3", "ruff==0.16.2"),
        ):
            with self.subTest(root=old):
                path.write_text(original.replace(old, new, 1), encoding="utf-8")
                self._assert_code("pyproject_lock_mismatch")

    def test_cli_emits_canonical_summary_and_fixed_failure_code(self):
        stdout = StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(main(["--repository-root", str(self.repository)]), 0)
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {"lockTargets": 4, "packageRecords": 74, "verified": True},
        )

        path = self.repository / "requirements" / "build.in"
        path.write_bytes(path.read_bytes() + b"\n")
        stderr = StringIO()
        with redirect_stderr(stderr):
            self.assertEqual(main(["--repository-root", str(self.repository)]), 1)
        self.assertEqual(
            stderr.getvalue(),
            "dependency lock verification failed: lock_digest_mismatch\n",
        )
        self.assertNotIn(str(self.repository), stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
