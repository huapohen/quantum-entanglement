from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.regression_gate import (
    GateCommand,
    _command_cwd,
    _python_tests,
    changed_paths,
    select_commands,
)


class RegressionGateSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repository = Path(__file__).resolve().parents[1]

    def test_document_only_change_selects_diff_checks(self) -> None:
        commands = select_commands(self.repository, ["docs/production/README.md"])
        self.assertEqual(
            tuple(command.name for command in commands),
            ("diff-check", "cached-diff-check"),
        )

    def test_direct_python_module_selects_its_test_only(self) -> None:
        tests = _python_tests(self.repository, ["src/quantum_entanglement/runtime.py"])
        self.assertEqual(tests, ("tests/test_agent_runtime.py", "tests/test_runtime.py"))

    def test_focused_ruff_includes_changed_source_and_selected_tests(self) -> None:
        commands = select_commands(
            self.repository,
            ["src/quantum_entanglement/runtime.py"],
        )
        ruff = next(command for command in commands if command.name == "ruff-focused")
        self.assertEqual(
            ruff.argv[0:2],
            ("ruff", "check"),
        )
        self.assertIn("src/quantum_entanglement/runtime.py", ruff.argv)
        self.assertIn("tests/test_runtime.py", ruff.argv)

    def test_unmapped_runtime_change_escalates_to_full_python_gate(self) -> None:
        tests = _python_tests(self.repository, ["src/quantum_entanglement/new_runtime_piece.py"])
        self.assertEqual(tests, ("__FULL_PYTHON_GATE_REQUIRED__",))

    def test_go_and_web_changes_select_both_product_gates(self) -> None:
        commands = select_commands(
            self.repository,
            ["apps/im-api/internal/app/app.go", "clients/im-web/src/App.tsx"],
        )
        names = tuple(command.name for command in commands)
        self.assertIn("go-test", names)
        self.assertIn("go-vet", names)
        self.assertIn("web-build", names)
        self.assertIn("web-first-synthetic", names)

    def test_module_documentation_does_not_select_language_gates(self) -> None:
        commands = select_commands(
            self.repository,
            ["apps/im-api/README.md", "clients/im-web/README.md"],
        )
        self.assertEqual(
            tuple(command.name for command in commands),
            ("diff-check", "cached-diff-check"),
        )

    def test_report_sync_script_maps_to_focused_test(self) -> None:
        tests = _python_tests(self.repository, ["scripts/report_sync_bundle.py"])
        self.assertEqual(tests, ("tests/test_report_sync_bundle.py",))

    def test_web_build_runs_from_web_package_root(self) -> None:
        command = GateCommand("web-build", ("npm", "run", "build"))
        self.assertEqual(
            _command_cwd(self.repository, command), self.repository / "clients/im-web"
        )

    def test_changed_paths_includes_untracked_files(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(("git", "init", "-q", str(root)), check=True)
            (root / "seed.txt").write_text("seed\n", encoding="utf-8")
            subprocess.run(("git", "-C", str(root), "add", "seed.txt"), check=True)
            subprocess.run(
                (
                    "git",
                    "-C",
                    str(root),
                    "-c",
                    "user.name=Regression Test",
                    "-c",
                    "user.email=regression@example.invalid",
                    "commit",
                    "-qm",
                    "seed",
                ),
                check=True,
            )
            (root / "new_file.py").write_text("print('new')\n", encoding="utf-8")
            self.assertEqual(changed_paths(root, None), ("new_file.py",))


if __name__ == "__main__":
    unittest.main()
