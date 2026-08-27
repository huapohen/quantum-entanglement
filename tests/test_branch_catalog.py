from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.branch_catalog import (
    BranchRecord,
    WorktreeRecord,
    archive_source_name,
    branch_category,
    branch_hub_root,
    branch_purpose,
    branch_worktree_root,
    catalog_main_baseline,
    catalog_tip_baseline,
    catalog_worktree_baselines,
    git,
    load_purposes,
    render_catalog,
    write_or_check,
)


class BranchCatalogTests(unittest.TestCase):
    def test_branch_hub_root_supports_all_local_layouts(self) -> None:
        original = Path("/workspace/execute/quantum_entanglement")
        nested = Path("/workspace/execute/infinite/quantum_entanglement/main")
        flattened = Path("/workspace/execute/infinite/quantum_entanglement")
        linked = Path("/workspace/execute/infinite/worktrees/quantum_entanglement/native-im-review")
        self.assertEqual(
            branch_hub_root(original),
            Path("/workspace/execute/infinite/quantum_entanglement"),
        )
        self.assertEqual(
            branch_hub_root(nested),
            Path("/workspace/execute/infinite/quantum_entanglement"),
        )
        self.assertEqual(
            branch_hub_root(flattened),
            Path("/workspace/execute/infinite/quantum_entanglement"),
        )
        self.assertEqual(
            branch_hub_root(linked),
            Path("/workspace/execute/infinite/quantum_entanglement"),
        )
        self.assertEqual(
            branch_worktree_root(linked),
            Path("/workspace/execute/infinite/worktrees/quantum_entanglement"),
        )

    def test_catalog_baseline_skips_a_catalog_only_tip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git(root, "init", "-b", "main")
            git(root, "config", "user.name", "catalog-test")
            git(root, "config", "user.email", "catalog-test@example.invalid")
            payload = root / "payload.txt"
            payload.write_text("one\n", encoding="utf-8")
            git(root, "add", "payload.txt")
            git(root, "commit", "-m", "initial payload")
            baseline = git(root, "rev-parse", "HEAD").stdout.strip()

            catalog = root / "BRANCH_CATALOG.md"
            catalog.write_text("snapshot\n", encoding="utf-8")
            git(root, "add", "BRANCH_CATALOG.md")
            git(root, "commit", "-m", "refresh catalog")
            self.assertEqual(catalog_main_baseline(root, catalog, "HEAD"), baseline)

            payload.write_text("two\n", encoding="utf-8")
            git(root, "add", "payload.txt")
            git(root, "commit", "-m", "update payload")
            current = git(root, "rev-parse", "HEAD").stdout.strip()
            self.assertEqual(catalog_main_baseline(root, catalog, "HEAD"), current)

    def test_catalog_tip_baseline_supports_linked_worktree_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "repo"
            root.mkdir()
            git(root, "init", "-b", "main")
            git(root, "config", "user.name", "catalog-test")
            git(root, "config", "user.email", "catalog-test@example.invalid")
            payload = root / "payload.txt"
            payload.write_text("one\n", encoding="utf-8")
            git(root, "add", "payload.txt")
            git(root, "commit", "-m", "initial payload")
            baseline = git(root, "rev-parse", "HEAD").stdout.strip()

            catalog = root / "BRANCH_CATALOG.md"
            catalog.write_text("snapshot\n", encoding="utf-8")
            git(root, "add", "BRANCH_CATALOG.md")
            git(root, "commit", "-m", "refresh catalog")
            tip = git(root, "rev-parse", "HEAD").stdout.strip()

            linked = base / "linked"
            git(root, "worktree", "add", "-b", "review", str(linked), "HEAD")
            self.assertEqual(
                catalog_tip_baseline(root, linked / "BRANCH_CATALOG.md", tip),
                baseline,
            )
            normalized = catalog_worktree_baselines(
                root,
                linked / "BRANCH_CATALOG.md",
                [
                    WorktreeRecord(
                        path=str(linked),
                        head=tip,
                        branch="review",
                        prunable=False,
                        exists=True,
                        clean=True,
                    )
                ],
            )
            self.assertEqual(normalized[0].head, baseline)

    def test_archive_source_name_recovers_original_branch(self) -> None:
        self.assertEqual(
            archive_source_name("archive/2026-08-21/codex/service-boundary-v1"),
            "codex/service-boundary-v1",
        )
        self.assertEqual(
            archive_source_name("archive/2026-08-21/gate-a-trusted-context-foundation"),
            "gate-a-trusted-context-foundation",
        )
        self.assertIsNone(archive_source_name("main"))

    def test_branch_purpose_labels_archive_copies_and_recovery_refs(self) -> None:
        purposes = {"codex/service-boundary-v1": "服务边界候选。"}
        self.assertEqual(
            branch_purpose("archive/2026-08-21/codex/service-boundary-v1", "checkpoint", purposes),
            "只读取证副本：服务边界候选。",
        )
        self.assertIn(
            "reflog",
            branch_purpose("archive/2026-08-21/reflog/recovered", "recovered subject", purposes),
        )
        self.assertEqual(branch_category("archive/2026-08-21/dangling/lost"), "归档：孤立提交")

    def test_load_purposes_validates_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid = root / "valid.json"
            valid.write_text(
                json.dumps({"schema_version": 1, "branches": {"main": "正式主线。"}}),
                encoding="utf-8",
            )
            self.assertEqual(load_purposes(valid), {"main": "正式主线。"})

            invalid = root / "invalid.json"
            invalid.write_text(json.dumps({"schema_version": 2, "branches": {}}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "schema_version 1"):
                load_purposes(invalid)

    def test_render_catalog_leads_with_main_and_lists_every_kind(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            main = BranchRecord(
                name="main",
                oid="a" * 40,
                tip_time="2026-08-21T23:46:18+08:00",
                subject="main checkpoint",
                purpose="唯一正式主线。",
                category="正式主线",
                relation="主线当前节点",
                ahead=0,
                behind=0,
                worktree=str(root),
            )
            archive = BranchRecord(
                name="archive/2026-08-21/dangling/recovered",
                oid="b" * 40,
                tip_time="2026-08-20T08:00:00+08:00",
                subject="recovered checkpoint",
                purpose="保存孤立节点。",
                category="归档：孤立提交",
                relation="未直接并入 main",
                ahead=1,
                behind=2,
                worktree=None,
            )
            worktree = WorktreeRecord(
                path=str(root),
                head=main.oid,
                branch="main",
                prunable=False,
                exists=True,
                clean=None,
            )

            rendered = render_catalog(
                root,
                [main, archive],
                [worktree],
                [("v0.1.0", main.oid, "c" * 40, "2026-08-21T23:57:36+08:00")],
            )

        self.assertIn("日常开发、启动体验和后续集成都只使用 `main`", rendered)
        self.assertIn("archive/2026-08-21/dangling/recovered", rendered)
        self.assertIn("正式主线工作区", rendered)
        self.assertIn("当前没有辅助 linked worktree", rendered)
        self.assertIn("完成后必须合并、推送并移除", rendered)
        self.assertIn("`v0.1.0`", rendered)

    def test_render_catalog_counts_only_auxiliary_linked_worktrees(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            main = BranchRecord(
                name="main",
                oid="a" * 40,
                tip_time="2026-08-27T10:00:00+08:00",
                subject="main checkpoint",
                purpose="唯一正式主线。",
                category="正式主线",
                relation="主线目录基线",
                ahead=0,
                behind=0,
                worktree=str(root),
            )
            worktrees = [
                WorktreeRecord(
                    path=str(root),
                    head=main.oid,
                    branch="main",
                    prunable=False,
                    exists=True,
                    clean=None,
                ),
                WorktreeRecord(
                    path=str(root / "worktrees" / "receipt-review"),
                    head="b" * 40,
                    branch="codex/receipt-review",
                    prunable=False,
                    exists=True,
                    clean=True,
                ),
            ]

            rendered = render_catalog(root, [main], worktrees, [])

        self.assertIn("当前另有 1 个辅助 linked worktree", rendered)
        self.assertIn("完成后必须合并、推送并移除", rendered)

    def test_render_catalog_uses_catalog_baseline_for_main_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = "a" * 40
            catalog_commit = "b" * 40
            main = BranchRecord(
                name="main",
                oid=baseline,
                tip_time="2026-08-22T10:00:00+08:00",
                subject="payload baseline",
                purpose="唯一正式主线。",
                category="正式主线",
                relation="主线目录基线",
                ahead=0,
                behind=0,
                worktree=str(root),
            )
            worktree = WorktreeRecord(
                path=str(root),
                head=catalog_commit,
                branch="main",
                prunable=False,
                exists=True,
                clean=None,
            )

            rendered = render_catalog(root, [main], [worktree], [])

        self.assertIn(f"| 正式主线工作区 | `main` | `{baseline[:12]}`", rendered)
        self.assertNotIn(catalog_commit[:12], rendered)

    def test_write_or_check_detects_stale_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "catalog.md"
            self.assertTrue(write_or_check(output, "current\\n", check=False))
            self.assertTrue(write_or_check(output, "current\\n", check=True))
            self.assertFalse(write_or_check(output, "new\\n", check=True))


if __name__ == "__main__":
    unittest.main()
