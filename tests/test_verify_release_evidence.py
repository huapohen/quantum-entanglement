import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import scripts.verify_release_evidence as verifier_module
from scripts.generate_release_evidence import Gate, GitSnapshot, canonical_json, generate_evidence
from scripts.verify_release_evidence import (
    EvidenceVerificationError,
    load_canonical_evidence,
    main,
    verify_releasable_evidence,
)

T0 = datetime(2026, 8, 20, 12, 34, 56, 789, tzinfo=timezone.utc)


class VerifyReleaseEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tempdir.name)
        self.repository = self.base / "repository"
        self.repository.mkdir()
        self._git("init", "-q")
        self._git("config", "user.name", "Release Evidence Test")
        self._git("config", "user.email", "release-evidence@example.invalid")
        (self.repository / "README.md").write_text("release candidate\n", encoding="utf-8")
        self._git("add", "README.md")
        self._git("commit", "-qm", "test fixture")
        self.gate = Gate(
            name="pass",
            argv=(sys.executable, "-c", "raise SystemExit(0)"),
            record_executable_basename=True,
        )
        self.evidence = generate_evidence(
            self.repository,
            (self.gate,),
            timeout_seconds=10,
            clock=lambda: T0,
        )
        self.evidence_path = self.base / "evidence.json"

    def tearDown(self):
        self.tempdir.cleanup()

    def _git(self, *arguments):
        return subprocess.run(
            ("git", "-C", str(self.repository), *arguments),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def _write_canonical(self, evidence=None):
        value = self.evidence if evidence is None else evidence
        self.evidence_path.write_text(canonical_json(value), encoding="utf-8")

    def _verify(self, evidence=None):
        value = self.evidence if evidence is None else evidence
        verify_releasable_evidence(
            value,
            expected_commit_sha=self._git("rev-parse", "HEAD"),
            expected_tree_sha=self._git("rev-parse", "HEAD^{tree}"),
            expected_gates=(self.gate,),
        )

    def test_valid_canonical_evidence_is_loaded_and_verified(self):
        self._write_canonical()

        loaded = load_canonical_evidence(self.evidence_path)
        self._verify(loaded)

        self.assertEqual(loaded, self.evidence)

    def test_noncanonical_duplicate_nonfinite_and_non_object_json_are_rejected(self):
        invalid_documents = (
            (json.dumps(self.evidence, indent=2), "evidence_not_canonical"),
            (
                '{"format":"first",' + canonical_json(self.evidence)[1:],
                "duplicate_json_key",
            ),
            ('{"value":NaN}\n', "non_finite_json_value"),
            ("[]\n", "evidence_not_object"),
        )
        for document, code in invalid_documents:
            with self.subTest(code=code):
                self.evidence_path.write_text(document, encoding="utf-8")
                with self.assertRaisesRegex(EvidenceVerificationError, code):
                    load_canonical_evidence(self.evidence_path)

    def test_symlink_and_oversized_evidence_are_rejected_before_json_decode(self):
        regular = self.base / "regular.json"
        regular.write_text("{}\n", encoding="utf-8")
        self.evidence_path.symlink_to(regular)
        with self.assertRaisesRegex(EvidenceVerificationError, "evidence_symlink"):
            load_canonical_evidence(self.evidence_path)

        self.evidence_path.unlink()
        self.evidence_path.write_bytes(b"x" * (1024 * 1024 + 1))
        with self.assertRaisesRegex(EvidenceVerificationError, "evidence_too_large"):
            load_canonical_evidence(self.evidence_path)

    def test_source_dirty_unstable_or_mismatched_identity_is_rejected(self):
        cases = (
            ("dirty", True, "source_not_clean"),
            ("identityStable", False, "source_identity_unstable"),
            ("commitShaAfterGates", "0" * 40, "source_commit_mismatch"),
            ("treeSha", "1" * 40, "source_tree_mismatch"),
        )
        for field, value, code in cases:
            with self.subTest(field=field):
                candidate = json.loads(canonical_json(self.evidence))
                candidate["source"][field] = value
                with self.assertRaisesRegex(EvidenceVerificationError, code):
                    self._verify(candidate)

    def test_failed_or_reordered_gate_and_false_summary_are_rejected(self):
        candidates = []
        failed_gate = json.loads(canonical_json(self.evidence))
        failed_gate["gates"][0]["status"] = "failed"
        candidates.append((failed_gate, "gate_result_invalid"))
        wrong_argv = json.loads(canonical_json(self.evidence))
        wrong_argv["gates"][0]["argv"].append("--not-run")
        candidates.append((wrong_argv, "gate_argv_invalid"))
        false_summary = json.loads(canonical_json(self.evidence))
        false_summary["summary"]["releasable"] = False
        candidates.append((false_summary, "summary_not_releasable"))
        wrong_count = json.loads(canonical_json(self.evidence))
        wrong_count["summary"]["passedCount"] = 999
        candidates.append((wrong_count, "summary_counts_invalid"))

        for candidate, code in candidates:
            with self.subTest(code=code):
                with self.assertRaisesRegex(EvidenceVerificationError, code):
                    self._verify(candidate)

    def test_unknown_fields_and_malformed_runtime_or_timestamp_are_rejected(self):
        candidates = []
        extra = json.loads(canonical_json(self.evidence))
        extra["unexpected"] = True
        candidates.append((extra, "top_level_shape_invalid"))
        runtime = json.loads(canonical_json(self.evidence))
        runtime["runtime"]["pythonVersion"] = ""
        candidates.append((runtime, "runtime_value_invalid"))
        timestamp = json.loads(canonical_json(self.evidence))
        timestamp["generatedAt"] = "2026-08-20T12:34:56Z"
        candidates.append((timestamp, "generated_at_invalid"))

        for candidate, code in candidates:
            with self.subTest(code=code):
                with self.assertRaisesRegex(EvidenceVerificationError, code):
                    self._verify(candidate)

    def test_cli_verifies_against_clean_repository_and_expected_commit(self):
        self._write_canonical()
        stdout = StringIO()
        stderr = StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(
                (
                    str(self.evidence_path),
                    "--repository-root",
                    str(self.repository),
                    "--expected-commit",
                    self._git("rev-parse", "HEAD"),
                ),
                expected_gates=(self.gate,),
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(stdout.getvalue(), "release evidence verified\n")
        self.assertEqual(stderr.getvalue(), "")

    def test_cli_failure_emits_only_fixed_code_not_evidence_or_environment_values(self):
        secret = "release-verifier-secret-canary"
        self.evidence_path.write_text(secret, encoding="utf-8")
        stdout = StringIO()
        stderr = StringIO()

        with (
            redirect_stdout(stdout),
            redirect_stderr(stderr),
            patch.dict(os.environ, {"QE_VERIFIER_SECRET": secret}),
        ):
            exit_code = main(
                (
                    str(self.evidence_path),
                    "--repository-root",
                    str(self.repository),
                ),
                expected_gates=(self.gate,),
            )

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertRegex(stderr.getvalue(), r"^release evidence verification failed: [a-z_]+\n$")
        self.assertNotIn(secret, stderr.getvalue())
        self.assertNotIn(str(self.evidence_path), stderr.getvalue())

    def test_cli_rejects_dirty_repository_even_for_valid_evidence(self):
        self._write_canonical()
        (self.repository / "untracked.txt").write_text("dirty\n", encoding="utf-8")
        stderr = StringIO()

        with redirect_stdout(StringIO()), redirect_stderr(stderr):
            exit_code = main(
                (
                    str(self.evidence_path),
                    "--repository-root",
                    str(self.repository),
                ),
                expected_gates=(self.gate,),
            )

        self.assertEqual(exit_code, 1)
        self.assertEqual(
            stderr.getvalue(),
            "release evidence verification failed: repository_not_clean\n",
        )

    def test_cli_rejects_even_ignored_evidence_inside_repository(self):
        inside = self.repository / "ignored-evidence.json"
        exclude = self.repository / ".git" / "info" / "exclude"
        exclude.write_text("ignored-evidence.json\n", encoding="utf-8")
        inside.write_text(canonical_json(self.evidence), encoding="utf-8")
        self.assertEqual(self._git("status", "--porcelain=v1"), "")
        stderr = StringIO()

        with redirect_stdout(StringIO()), redirect_stderr(stderr):
            exit_code = main(
                (
                    str(inside),
                    "--repository-root",
                    str(self.repository),
                ),
                expected_gates=(self.gate,),
            )

        self.assertEqual(exit_code, 1)
        self.assertEqual(
            stderr.getvalue(),
            "release evidence verification failed: evidence_inside_repository\n",
        )

    def test_cli_rejects_repository_change_during_verification(self):
        self._write_canonical()
        initial = verifier_module.capture_git_snapshot(self.repository)
        changed = GitSnapshot(
            commit_sha=initial.commit_sha,
            tree_sha=initial.tree_sha,
            dirty=True,
        )
        stderr = StringIO()

        with (
            patch.object(
                verifier_module,
                "capture_git_snapshot",
                side_effect=(initial, changed),
            ),
            redirect_stdout(StringIO()),
            redirect_stderr(stderr),
        ):
            exit_code = main(
                (
                    str(self.evidence_path),
                    "--repository-root",
                    str(self.repository),
                ),
                expected_gates=(self.gate,),
            )

        self.assertEqual(exit_code, 1)
        self.assertEqual(
            stderr.getvalue(),
            "release evidence verification failed: repository_changed_during_verification\n",
        )


if __name__ == "__main__":
    unittest.main()
