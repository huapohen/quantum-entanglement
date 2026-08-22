from __future__ import annotations

import json
from pathlib import Path

import pytest

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


def test_archive_source_name_recovers_original_branch() -> None:
    assert (
        archive_source_name("archive/2026-08-21/codex/service-boundary-v1")
        == "codex/service-boundary-v1"
    )
    assert (
        archive_source_name("archive/2026-08-21/gate-a-trusted-context-foundation")
        == "gate-a-trusted-context-foundation"
    )
    assert archive_source_name("main") is None


def test_branch_purpose_labels_archive_copies_and_recovery_refs() -> None:
    purposes = {"codex/service-boundary-v1": "服务边界候选。"}
    assert (
        branch_purpose("archive/2026-08-21/codex/service-boundary-v1", "checkpoint", purposes)
        == "只读取证副本：服务边界候选。"
    )
    assert "reflog" in branch_purpose(
        "archive/2026-08-21/reflog/recovered", "recovered subject", purposes
    )
    assert branch_category("archive/2026-08-21/dangling/lost") == "归档：孤立提交"


def test_load_purposes_validates_schema(tmp_path: Path) -> None:
    valid = tmp_path / "valid.json"
    valid.write_text(
        json.dumps({"schema_version": 1, "branches": {"main": "正式主线。"}}),
        encoding="utf-8",
    )
    assert load_purposes(valid) == {"main": "正式主线。"}

    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps({"schema_version": 2, "branches": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="schema_version 1"):
        load_purposes(invalid)


def test_render_catalog_leads_with_main_and_lists_every_kind(tmp_path: Path) -> None:
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
        worktree=str(tmp_path),
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
        path=str(tmp_path),
        head=main.oid,
        branch="main",
        prunable=False,
        exists=True,
        clean=None,
    )

    rendered = render_catalog(
        tmp_path,
        [main, archive],
        [worktree],
        [("v0.1.0", main.oid, "c" * 40, "2026-08-21T23:57:36+08:00")],
    )

    assert "日常开发、启动体验和后续集成都只使用 `main`" in rendered
    assert "archive/2026-08-21/dangling/recovered" in rendered
    assert "正式主线工作区" in rendered
    assert "`v0.1.0`" in rendered


def test_write_or_check_detects_stale_catalog(tmp_path: Path) -> None:
    output = tmp_path / "catalog.md"
    assert write_or_check(output, "current\n", check=False)
    assert write_or_check(output, "current\n", check=True)
    assert not write_or_check(output, "new\n", check=True)
