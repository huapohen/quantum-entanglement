import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from scripts.generate_release_evidence import (
    Gate,
    canonical_json,
    default_gates,
    generate_evidence,
)

T0 = datetime(2026, 8, 20, 12, 34, 56, 789, tzinfo=timezone.utc)


class ReleaseEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self._git("init", "-q")
        self._git("config", "user.name", "Release Evidence Test")
        self._git("config", "user.email", "release-evidence@example.invalid")
        (self.root / "README.md").write_text("release candidate\n", encoding="utf-8")
        self._git("add", "README.md")
        self._git("commit", "-qm", "test fixture")

    def tearDown(self):
        self.tempdir.cleanup()

    def _git(self, *arguments):
        return subprocess.run(
            ("git", "-C", str(self.root), *arguments),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    @staticmethod
    def _python_gate(name, source):
        return Gate(
            name=name,
            argv=(sys.executable, "-c", source),
            record_executable_basename=True,
        )

    def _generate(self, *gates, timeout_seconds=10):
        return generate_evidence(
            self.root,
            gates,
            timeout_seconds=timeout_seconds,
            clock=lambda: T0,
        )

    def test_clean_source_and_passing_gates_are_releasable(self):
        evidence = self._generate(
            self._python_gate("pass-one", "raise SystemExit(0)"),
            self._python_gate("pass-two", "raise SystemExit(0)"),
        )

        self.assertEqual(evidence["format"], "quantum-entanglement.release-evidence")
        self.assertEqual(evidence["schemaVersion"], 1)
        self.assertEqual(evidence["generatedAt"], "2026-08-20T12:34:56.000789Z")
        self.assertEqual(evidence["source"]["commitSha"], self._git("rev-parse", "HEAD"))
        self.assertEqual(evidence["source"]["treeSha"], self._git("rev-parse", "HEAD^{tree}"))
        self.assertFalse(evidence["source"]["dirty"])
        self.assertTrue(evidence["source"]["identityStable"])
        self.assertEqual(evidence["runtime"]["pythonVersion"], platform_python_version())
        self.assertRegex(evidence["runtime"]["sqliteVersion"], r"^\d+\.\d+")
        self.assertEqual([item["status"] for item in evidence["gates"]], ["passed", "passed"])
        self.assertEqual(
            evidence["summary"],
            {
                "allGatesPassed": True,
                "errorCount": 0,
                "failedCount": 0,
                "gateCount": 2,
                "passedCount": 2,
                "reasonCodes": [],
                "releasable": True,
                "sourceClean": True,
                "timedOutCount": 0,
            },
        )

    def test_dirty_source_fails_closed_even_when_gate_passes(self):
        (self.root / "untracked.txt").write_text("dirty\n", encoding="utf-8")

        evidence = self._generate(self._python_gate("pass", "raise SystemExit(0)"))

        self.assertTrue(evidence["source"]["dirtyBeforeGates"])
        self.assertTrue(evidence["source"]["dirtyAfterGates"])
        self.assertFalse(evidence["summary"]["sourceClean"])
        self.assertFalse(evidence["summary"]["releasable"])
        self.assertEqual(
            evidence["summary"]["reasonCodes"],
            ["source_dirty_before_gates", "source_dirty_after_gates"],
        )

    def test_gate_created_dirtiness_fails_closed(self):
        gate = self._python_gate(
            "mutating-gate",
            "from pathlib import Path; Path('created.txt').write_text('changed')",
        )

        evidence = self._generate(gate)

        self.assertFalse(evidence["source"]["dirtyBeforeGates"])
        self.assertTrue(evidence["source"]["dirtyAfterGates"])
        self.assertTrue(evidence["source"]["dirty"])
        self.assertFalse(evidence["summary"]["releasable"])
        self.assertIn("source_dirty_after_gates", evidence["summary"]["reasonCodes"])

    def test_gate_created_commit_fails_closed_when_worktree_returns_clean(self):
        gate = self._python_gate(
            "committing-gate",
            "import subprocess; from pathlib import Path; "
            "Path('committed.txt').write_text('changed'); "
            "subprocess.run(('git', 'add', 'committed.txt'), check=True); "
            "subprocess.run(('git', 'commit', '-qm', 'gate mutation'), check=True)",
        )

        evidence = self._generate(gate)

        self.assertFalse(evidence["source"]["dirtyBeforeGates"])
        self.assertFalse(evidence["source"]["dirtyAfterGates"])
        self.assertFalse(evidence["source"]["dirty"])
        self.assertFalse(evidence["source"]["identityStable"])
        self.assertNotEqual(
            evidence["source"]["commitSha"],
            evidence["source"]["commitShaAfterGates"],
        )
        self.assertFalse(evidence["summary"]["releasable"])
        self.assertIn("source_identity_changed", evidence["summary"]["reasonCodes"])

    def test_nonzero_gate_is_recorded_without_short_circuiting_later_gates(self):
        evidence = self._generate(
            self._python_gate("fail", "raise SystemExit(17)"),
            self._python_gate("later-pass", "raise SystemExit(0)"),
        )

        self.assertEqual([item["status"] for item in evidence["gates"]], ["failed", "passed"])
        self.assertEqual(evidence["gates"][0]["exitCode"], 17)
        self.assertEqual(evidence["gates"][0]["failureKind"], "nonzero_exit")
        self.assertEqual(evidence["summary"]["failedCount"], 1)
        self.assertFalse(evidence["summary"]["allGatesPassed"])
        self.assertFalse(evidence["summary"]["releasable"])
        self.assertIn("gate_failed", evidence["summary"]["reasonCodes"])

    def test_timeout_and_missing_executable_are_redacted_and_fail_closed(self):
        missing = Gate(
            name="missing",
            argv=(str(self.root / "does-not-exist"),),
            record_executable_basename=True,
        )
        evidence = self._generate(
            self._python_gate("timeout", "import time; time.sleep(5)"),
            missing,
            timeout_seconds=1,
        )

        self.assertEqual([item["status"] for item in evidence["gates"]], ["timed_out", "error"])
        self.assertIsNone(evidence["gates"][0]["exitCode"])
        self.assertEqual(evidence["gates"][0]["failureKind"], "timeout")
        self.assertEqual(evidence["gates"][1]["failureKind"], "execution_error")
        self.assertEqual(evidence["summary"]["timedOutCount"], 1)
        self.assertEqual(evidence["summary"]["errorCount"], 1)
        self.assertFalse(evidence["summary"]["releasable"])
        serialized = canonical_json(evidence)
        self.assertNotIn(str(self.root), serialized)

    def test_no_gate_is_never_releasable(self):
        evidence = self._generate()

        self.assertFalse(evidence["summary"]["allGatesPassed"])
        self.assertFalse(evidence["summary"]["releasable"])
        self.assertIn("no_gates_executed", evidence["summary"]["reasonCodes"])

    def test_gate_output_and_environment_values_never_enter_evidence(self):
        secret_value = "super-secret-release-canary"
        leak_file = self.root / "leak.txt"
        leak_file.write_text(secret_value, encoding="utf-8")
        gate = self._python_gate(
            "no-output-retention",
            "from pathlib import Path; print(Path('leak.txt').read_text())",
        )
        with patch.dict(os.environ, {"QE_RELEASE_SECRET": secret_value}):
            evidence = self._generate(gate)

        serialized = canonical_json(evidence)
        self.assertNotIn(secret_value, serialized)
        self.assertNotIn("QE_RELEASE_SECRET", serialized)

    def test_canonical_json_is_compact_sorted_and_round_trips(self):
        evidence = self._generate(self._python_gate("pass", "raise SystemExit(0)"))

        serialized = canonical_json(evidence)

        self.assertTrue(serialized.endswith("\n"))
        self.assertNotIn(": ", serialized)
        self.assertNotIn(", ", serialized)
        self.assertEqual(json.loads(serialized), evidence)
        self.assertEqual(
            serialized,
            json.dumps(
                evidence,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n",
        )

    def test_default_gate_set_matches_the_documented_local_baseline(self):
        gates = default_gates("/private/runtime/python3")

        self.assertEqual(
            [gate.name for gate in gates],
            ["unit-tests", "deterministic-demo", "compileall", "ruff", "diff-check"],
        )
        self.assertEqual(gates[0].argv[0], "/private/runtime/python3")
        self.assertEqual(gates[0].argv[1:], ("-m", "pytest", "-q"))
        self.assertTrue(gates[0].record_executable_basename)
        self.assertEqual(gates[2].argv[-1], "scripts")
        self.assertEqual(gates[3].argv[-1], "scripts")

    def test_invalid_timeout_duplicate_names_and_naive_clock_are_rejected(self):
        gate = self._python_gate("same", "raise SystemExit(0)")
        with self.assertRaisesRegex(ValueError, "positive integer"):
            generate_evidence(self.root, (gate,), timeout_seconds=0)
        with self.assertRaisesRegex(ValueError, "unique"):
            generate_evidence(self.root, (gate, gate), clock=lambda: T0)
        with self.assertRaisesRegex(ValueError, "argv"):
            generate_evidence(self.root, (Gate(name="empty", argv=()),), clock=lambda: T0)
        with self.assertRaisesRegex(ValueError, "aware datetime"):
            generate_evidence(
                self.root,
                (gate,),
                clock=lambda: datetime(2026, 8, 20),
            )


def platform_python_version():
    return ".".join(str(item) for item in sys.version_info[:3])


if __name__ == "__main__":
    unittest.main()
