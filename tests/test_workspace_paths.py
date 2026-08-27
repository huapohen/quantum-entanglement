from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.check_workspace_paths import OBSOLETE_PATHS, find_obsolete_paths


class WorkspacePathAuditTests(unittest.TestCase):
    def test_accepts_flattened_workspace_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = root / "docs" / "current.md"
            document.parent.mkdir()
            document.write_text(
                "/Users/example/agent/execute/infinite/quantum_entanglement\n",
                encoding="utf-8",
            )

            self.assertEqual(find_obsolete_paths(root, ["docs/current.md"]), ())

    def test_rejects_obsolete_path_in_operational_script(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "scripts" / "launch.sh"
            script.parent.mkdir()
            script.write_text(f"cd /workspace/{OBSOLETE_PATHS[0]}\n", encoding="utf-8")

            findings = find_obsolete_paths(root, ["scripts/launch.sh"])

            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].path, "scripts/launch.sh")
            self.assertEqual(findings[0].line, 1)

    def test_accepts_mainline_linked_worktree_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = root / "docs" / "current.md"
            document.parent.mkdir()
            document.write_text(
                "/workspace/execute/infinite/worktrees/quantum_entanglement/"
                "mainline_continue_quantum_entanglement\n",
                encoding="utf-8",
            )

            self.assertEqual(find_obsolete_paths(root, ["docs/current.md"]), ())

    def test_rejects_obsolete_main_subdirectory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = root / "docs" / "obsolete.md"
            document.parent.mkdir()
            document.write_text(
                f"/workspace/execute/infinite/{OBSOLETE_PATHS[1]}/scripts/start.sh\n",
                encoding="utf-8",
            )

            findings = find_obsolete_paths(root, ["docs/obsolete.md"])
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].obsolete_path, OBSOLETE_PATHS[1])

    def test_allows_documented_migration_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "MIGRATION_MANIFEST.md"
            manifest.write_text(" -> ".join(OBSOLETE_PATHS), encoding="utf-8")

            self.assertEqual(find_obsolete_paths(root, ["MIGRATION_MANIFEST.md"]), ())


if __name__ == "__main__":
    unittest.main()
