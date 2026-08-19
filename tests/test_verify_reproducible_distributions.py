import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import verify_reproducible_distributions as verifier


class ReproducibleDistributionVerifierTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.reference = self.root / "reference"
        self.candidate = self.root / "candidate"
        self.reference.mkdir()
        self.candidate.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def _write_set(self, directory, *, wheel=b"wheel", sdist=b"sdist"):
        (directory / "quantum_entanglement-0.1.0-py3-none-any.whl").write_bytes(wheel)
        (directory / "quantum_entanglement-0.1.0.tar.gz").write_bytes(sdist)

    def test_identical_sets_return_canonical_digest_summary(self):
        self._write_set(self.reference)
        self._write_set(self.candidate)

        result = verifier.verify_reproducible_distributions(self.reference, self.candidate)

        self.assertIs(result["byteIdentical"], True)
        self.assertEqual(
            [artifact["filename"] for artifact in result["artifacts"]],
            [
                "quantum_entanglement-0.1.0-py3-none-any.whl",
                "quantum_entanglement-0.1.0.tar.gz",
            ],
        )
        self.assertTrue(all(len(artifact["sha256"]) == 64 for artifact in result["artifacts"]))

    def test_content_and_filename_mismatches_fail_closed(self):
        self._write_set(self.reference)
        self._write_set(self.candidate, wheel=b"changed")
        with self.assertRaisesRegex(
            verifier.ReproducibilityVerificationError, "distribution_bytes_mismatch"
        ):
            verifier.verify_reproducible_distributions(self.reference, self.candidate)

        mismatched = self.candidate / "quantum_entanglement-0.1.0.tar.gz"
        mismatched.rename(self.candidate / "other-0.1.0.tar.gz")
        with self.assertRaisesRegex(
            verifier.ReproducibilityVerificationError, "distribution_set_mismatch"
        ):
            verifier.verify_reproducible_distributions(self.reference, self.candidate)

    def test_extra_missing_and_non_regular_entries_are_rejected(self):
        self._write_set(self.reference)
        self._write_set(self.candidate)
        (self.candidate / "unexpected.txt").write_text("unexpected", encoding="utf-8")
        with self.assertRaisesRegex(
            verifier.ReproducibilityVerificationError, "distribution_name_invalid"
        ):
            verifier.verify_reproducible_distributions(self.reference, self.candidate)

        (self.candidate / "unexpected.txt").unlink()
        wheel = self.candidate / "quantum_entanglement-0.1.0-py3-none-any.whl"
        wheel.unlink()
        wheel.symlink_to(self.reference / wheel.name)
        with self.assertRaisesRegex(
            verifier.ReproducibilityVerificationError, "distribution_not_regular"
        ):
            verifier.verify_reproducible_distributions(self.reference, self.candidate)

    def test_directories_must_be_distinct_and_inputs_are_bounded(self):
        self._write_set(self.reference)
        self._write_set(self.candidate)
        with self.assertRaisesRegex(
            verifier.ReproducibilityVerificationError,
            "distribution_directories_not_independent",
        ):
            verifier.verify_reproducible_distributions(self.reference, self.reference)

        with mock.patch.object(verifier, "_MAX_FILE_BYTES", 3):
            with self.assertRaisesRegex(
                verifier.ReproducibilityVerificationError, "distribution_too_large"
            ):
                verifier.verify_reproducible_distributions(self.reference, self.candidate)

    def test_cli_is_fail_closed_without_leaking_directory_paths(self):
        self._write_set(self.reference)
        self._write_set(self.candidate)
        script = Path(__file__).parents[1] / "scripts" / "verify_reproducible_distributions.py"
        command = [
            sys.executable,
            str(script),
            "--reference-directory",
            str(self.reference),
            "--candidate-directory",
            str(self.candidate),
        ]
        completed = subprocess.run(command, check=False, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIs(json.loads(completed.stdout)["byteIdentical"], True)

        candidate_wheel = self.candidate / "quantum_entanglement-0.1.0-py3-none-any.whl"
        candidate_wheel.write_bytes(b"changed")
        failed = subprocess.run(command, check=False, capture_output=True, text=True)
        self.assertEqual(failed.returncode, 1)
        self.assertEqual(
            failed.stderr,
            "verify_reproducible_distributions: distribution_bytes_mismatch\n",
        )
        self.assertNotIn(str(self.reference), failed.stderr)
        self.assertNotIn(str(self.candidate), failed.stderr)


if __name__ == "__main__":
    unittest.main()
