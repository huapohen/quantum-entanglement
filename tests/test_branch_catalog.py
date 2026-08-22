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
    branch_purpose,
    load_purposes,
    render_catalog,
    write_or_check,
)


class BranchCatalogTests(unittest.TestCase):
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
        self.assertIn("`v0.1.0`", rendered)

    def test_write_or_check_detects_stale_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "catalog.md"
            self.assertTrue(write_or_check(output, "current\\n", check=False))
            self.assertTrue(write_or_check(output, "current\\n", check=True))
            self.assertFalse(write_or_check(output, "new\\n", check=True))


if __name__ == "__main__":
    unittest.main()
