#!/usr/bin/env python3
"""Generate the local and remote branch governance catalog."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class BranchRecord:
    name: str
    oid: str
    tip_time: str
    subject: str
    purpose: str
    category: str
    relation: str
    ahead: int
    behind: int
    worktree: str | None


@dataclass(frozen=True)
class WorktreeRecord:
    path: str
    head: str
    branch: str | None
    prunable: bool
    exists: bool
    clean: bool | None


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", os.fspath(repo), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def load_purposes(path: Path) -> dict[str, str]:
    payload: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("branch metadata must use schema_version 1")
    branches = payload.get("branches")
    if not isinstance(branches, dict):
        raise ValueError("branch metadata must contain a branches object")
    result: dict[str, str] = {}
    for name, purpose in branches.items():
        if not isinstance(name, str) or not isinstance(purpose, str) or not purpose.strip():
            raise ValueError("branch names and purposes must be non-empty strings")
        result[name] = purpose.strip()
    return result


def parse_worktrees(repo: Path) -> list[WorktreeRecord]:
    raw = git(repo, "worktree", "list", "--porcelain").stdout.strip()
    if not raw:
        return []
    records: list[WorktreeRecord] = []
    for block in raw.split("\n\n"):
        fields: dict[str, str] = {}
        flags: set[str] = set()
        for line in block.splitlines():
            key, separator, value = line.partition(" ")
            if separator:
                fields[key] = value
            else:
                flags.add(key)
        path = fields["worktree"]
        branch_ref = fields.get("branch")
        branch = branch_ref.removeprefix("refs/heads/") if branch_ref else None
        exists = Path(path).exists()
        clean: bool | None = None
        if exists and Path(path).resolve() != repo.resolve():
            status = git(Path(path), "status", "--porcelain=v1", check=False)
            clean = status.returncode == 0 and not status.stdout.strip()
        records.append(
            WorktreeRecord(
                path=path,
                head=fields["HEAD"],
                branch=branch,
                prunable="prunable" in fields or "prunable" in flags,
                exists=exists,
                clean=clean,
            )
        )
    return records


def remote_refs(repo: Path) -> list[tuple[str, str, str, str]]:
    fmt = "%00".join(
        [
            "%(refname)",
            "%(objectname)",
            "%(committerdate:iso8601-strict)",
            "%(subject)",
            "%(symref)",
        ]
    )
    output = git(repo, "for-each-ref", f"--format={fmt}", "refs/remotes/origin").stdout
    records: list[tuple[str, str, str, str]] = []
    for line in output.splitlines():
        refname, oid, tip_time, subject, symref = line.split("\0", 4)
        if symref or refname == "refs/remotes/origin/HEAD":
            continue
        name = refname.removeprefix("refs/remotes/origin/")
        records.append((name, oid, tip_time, subject))
    return records


def tag_rows(repo: Path) -> list[tuple[str, str, str, str]]:
    fmt = "%00".join(
        ["%(refname:strip=2)", "%(*objectname)", "%(objectname)", "%(creatordate:iso8601-strict)"]
    )
    output = git(repo, "for-each-ref", f"--format={fmt}", "refs/tags").stdout
    tags: list[tuple[str, str, str, str]] = []
    for line in output.splitlines():
        name, peeled, object_oid, created = line.split("\0", 3)
        tags.append((name, peeled or object_oid, object_oid, created))
    return sorted(tags, key=lambda row: row[3], reverse=True)


def branch_category(name: str) -> str:
    if name == "main":
        return "正式主线"
    if name.startswith("archive/"):
        parts = name.split("/")
        archive_kind = parts[2] if len(parts) > 2 else "snapshot"
        labels = {
            "dangling": "归档：孤立提交",
            "reflog": "归档：reflog 救援",
            "worktree": "归档：临时 worktree",
            "codex": "归档：开发分支副本",
            "agent": "归档：Agent 分支副本",
            "gate-a-trusted-context-foundation": "归档：Gate A 副本",
        }
        return labels.get(archive_kind, "归档：历史快照")
    if name.startswith("agent/"):
        return "历史：证据分支"
    if name.startswith("gate-"):
        return "历史：门禁候选"
    if name.startswith("codex/"):
        return "历史：开发候选"
    return "历史：其他"


def archive_source_name(name: str) -> str | None:
    parts = name.split("/", 2)
    if len(parts) != 3 or parts[0] != "archive":
        return None
    return parts[2]


def branch_purpose(name: str, subject: str, purposes: dict[str, str]) -> str:
    if name in purposes:
        return purposes[name]
    if name.startswith("archive/"):
        source = archive_source_name(name)
        if source and source in purposes:
            return f"只读取证副本：{purposes[source]}"
        parts = name.split("/")
        kind = parts[2] if len(parts) > 2 else "snapshot"
        kind_purpose = {
            "dangling": "保存当时无分支引用的提交，防止垃圾回收后丢失。",
            "reflog": "从本地 reflog 恢复的历史节点，仅供追溯。",
            "worktree": "从临时或 detached worktree 保存的历史节点，仅供追溯。",
        }.get(kind, "保存历史节点，仅供审计和恢复。")
        return f"{kind_purpose} 节点主题：{subject}"
    return f"用途待补充；当前节点主题：{subject}"


def relation_to_main(repo: Path, oid: str, main_oid: str) -> tuple[str, int, int]:
    if oid == main_oid:
        return "主线目录基线", 0, 0
    counts = git(repo, "rev-list", "--left-right", "--count", f"{main_oid}...{oid}").stdout.split()
    behind, ahead = (int(counts[0]), int(counts[1]))
    merged = git(repo, "merge-base", "--is-ancestor", oid, main_oid, check=False).returncode == 0
    relation = "已作为祖先进入 main" if merged else "未直接并入 main"
    return relation, ahead, behind


def commit_details(repo: Path, oid: str) -> tuple[str, str]:
    value = git(repo, "show", "-s", "--format=%cI%x00%s", oid).stdout.rstrip("\n")
    tip_time, subject = value.split("\0", 1)
    return tip_time, subject


def catalog_main_baseline(
    repo: Path, output: Path, main_ref: str = "refs/remotes/origin/main"
) -> str:
    tip = git(repo, "rev-parse", main_ref).stdout.strip()
    try:
        relative_output = output.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        return tip
    candidate = tip
    while True:
        changed_paths = git(
            repo,
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            candidate,
            "--",
        ).stdout.splitlines()
        if changed_paths != [relative_output]:
            return candidate
        parent = git(repo, "rev-parse", f"{candidate}^", check=False)
        if parent.returncode != 0 or not parent.stdout.strip():
            return candidate
        candidate = parent.stdout.strip()


def collect_branches(
    repo: Path,
    purposes: dict[str, str],
    worktrees: Sequence[WorktreeRecord],
    *,
    main_oid: str | None = None,
) -> list[BranchRecord]:
    if main_oid is None:
        main_oid = git(repo, "rev-parse", "refs/remotes/origin/main").stdout.strip()
    worktree_by_branch = {item.branch: item.path for item in worktrees if item.branch}
    records: list[BranchRecord] = []
    for name, oid, tip_time, subject in remote_refs(repo):
        if name == "main" and oid != main_oid:
            oid = main_oid
            tip_time, subject = commit_details(repo, main_oid)
        relation, ahead, behind = relation_to_main(repo, oid, main_oid)
        records.append(
            BranchRecord(
                name=name,
                oid=oid,
                tip_time=tip_time,
                subject=subject,
                purpose=branch_purpose(name, subject, purposes),
                category=branch_category(name),
                relation=relation,
                ahead=ahead,
                behind=behind,
                worktree=worktree_by_branch.get(name),
            )
        )
    return sorted(records, key=lambda item: (item.tip_time, item.name), reverse=True)


def md(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def branch_hub_root(repo: Path) -> Path:
    if repo.name == "quantum_entanglement" and repo.parent.name == "infinite":
        return repo
    if (
        repo.name == "main"
        and repo.parent.name == "quantum_entanglement"
        and repo.parent.parent.name == "infinite"
    ):
        return repo.parent
    return repo.parent / "infinite" / repo.name


def render_catalog(
    repo: Path,
    branches: Sequence[BranchRecord],
    worktrees: Sequence[WorktreeRecord],
    tags: Sequence[tuple[str, str, str, str]],
) -> str:
    main = next(branch for branch in branches if branch.name == "main")
    hub_root = branch_hub_root(repo)
    archive_count = sum(branch.name.startswith("archive/") for branch in branches)
    historical_count = len(branches) - archive_count - 1
    auxiliary_worktree_count = sum(
        Path(item.path).resolve() != repo.resolve() for item in worktrees
    )
    if auxiliary_worktree_count:
        worktree_summary = (
            f"`main` 固定在 `{repo}`。当前另有 {auxiliary_worktree_count} 个辅助 linked "
            f"worktree；它们统一位于 `{hub_root / 'worktrees'}`，完成后必须合并、推送并移除。"
        )
    else:
        worktree_summary = (
            f"`main` 固定在 `{repo}`。当前没有辅助 linked worktree；后续临时 worktree "
            f"统一创建在 `{hub_root / 'worktrees'}`，完成后必须合并、推送并移除。"
        )
    lines = [
        "# Quantum Entanglement 分支与 Worktree 导航",
        "",
        "> 结论先行：**正式开发、发布和后续集成使用 `main`；当前未合并的 Web IM/Agent Store "
        "阶段验收使用 `dev_wanwork_quantum_entanglement`。** 除非是在做历史审计或定点恢复，"
        "不要直接在 `codex/*`、`agent/*`、`gate-*` 或 `archive/*` 上继续开发，也不要把这些分支整条合并回 `main`。",
        "",
        "## 你现在应该用哪个",
        "",
        "| 场景 | 应使用的引用 | 说明 |",
        "| --- | --- | --- |",
        f"| 正式开发、发布、后续集成 | `main`（目录基线 `{main.oid[:12]}`） | "
        f"唯一正式主分支；目录 `{repo}`。 |",
        "| 当前 Web IM/Agent Store 阶段验收 | `dev_wanwork_quantum_entanglement` | "
        "未合并到 main 的独立验收分支；worktree 位于统一 `worktrees/quantum_entanglement` 目录。 |",
        "| 复现当前本地试用版本 | `v0.1.0-local-trial.2` | "
        "固定版本标签，不会随 `main` 后续提交移动。 |",
        "| 查看上一试用检查点 | `v0.1.0-local-trial.1` | 已被 `.2` 取代，仅用于对比。 |",
        "| 恢复某项历史实现 | 先从 `main` 新建分支，再挑选提交 | "
        "优先 `git cherry-pick` 单个已审阅提交，不直接合并历史分支。 |",
        "| 事故取证或找回孤立提交 | `archive/*` | 只读保险引用，禁止作为新开发起点。 |",
        "",
        "## 分支数量为什么看起来很多",
        "",
        f"远端当前共有 **{len(branches)}** 个分支引用：1 个正式主线、"
        f"{historical_count} 个历史开发/证据候选、{archive_count} 个只读取证归档。"
        "`archive/*` 中有不少只是同一历史节点的保险副本，并不代表同时维护的产品版本。",
        "",
        "Git 本身不保存可靠的“分支创建时间”。下表的“节点时间”是该分支尖端提交的提交时间，"
        "这是能够审计的时间节点；不能把它冒充为分支创建时间。"
        "`领先/落后` 以目录基线为准；若 `origin/main` 最新提交只更新本目录，"
        "生成器会使用其父提交，避免目录提交导致自身立即过期。",
        "",
        "## 命名和生命周期",
        "",
        "- `main`：唯一正式主线，受 CI 和发布检查约束。",
        "- `codex/*`：阶段性实现、修复或集成候选；当前统一按历史只读处理。",
        "- `agent/*`：工程证据账本或 Agent 专项产物分支；当前统一按历史只读处理。",
        "- `gate-*`：安全门禁候选；不等于门禁已经批准。",
        "- `archive/<日期>/*`：为避免历史节点丢失而建立的冻结保险引用。",
        "- `v*` 标签：不可移动的验收/发布检查点；复现版本时优先用标签而不是猜分支。",
        "",
        "## 所有非归档开发分支",
        "",
        "| 节点时间 | 分支 | 用途 | 相对 main | 差异 | Worktree |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for branch in branches:
        if branch.name.startswith("archive/"):
            continue
        worktree = f"`{md(branch.worktree)}`" if branch.worktree else "—"
        lines.append(
            f"| {branch.tip_time} | `{md(branch.name)}`<br>`{branch.oid[:12]}` | "
            f"{md(branch.purpose)} | {branch.relation} | "
            f"领先 {branch.ahead} / 落后 {branch.behind} | {worktree} |"
        )
    lines.extend(
        [
            "",
            "### 如何理解“未直接并入 main”",
            "",
            "这不等于该分支的价值没有进入主线。部分修复曾通过重写、cherry-pick "
            "或在更新基线上重新实现，因此旧分支尖端不会成为 `main` 的祖先。"
            "不要仅凭这个字段执行 merge；应先比较具体提交和测试证据。",
            "",
            "## 所有 archive 取证分支",
            "",
            "| 节点时间 | 归档引用 | 类型 | 保存内容 | 相对 main |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for branch in branches:
        if not branch.name.startswith("archive/"):
            continue
        lines.append(
            f"| {branch.tip_time} | `{md(branch.name)}`<br>`{branch.oid[:12]}` | "
            f"{branch.category} | {md(branch.purpose)} | {branch.relation}；"
            f"领先 {branch.ahead} / 落后 {branch.behind} |"
        )
    lines.extend(
        [
            "",
            "## 本机 Worktree 目录",
            "",
            worktree_summary,
            "",
            "| 状态 | 分支/模式 | HEAD | 路径 |",
            "| --- | --- | --- | --- |",
        ]
    )
    for item in sorted(worktrees, key=lambda record: record.path):
        is_main_worktree = Path(item.path).resolve() == repo.resolve()
        if item.prunable:
            state = "失效登记（可 prune）"
        elif not item.exists:
            state = "路径缺失"
        elif item.clean:
            state = "存在、干净"
        elif item.clean is False:
            state = "存在、有未提交修改"
        elif is_main_worktree:
            state = "正式主线工作区"
        else:
            state = "存在、状态未知"
        mode = item.branch or "detached"
        displayed_head = main.oid if is_main_worktree and item.branch == "main" else item.head
        lines.append(f"| {state} | `{md(mode)}` | `{displayed_head[:12]}` | `{md(item.path)}` |")
    lines.extend(
        [
            "",
            "## 固定版本标签",
            "",
            "| 标签 | 指向提交 | 标签对象 | 时间 |",
            "| --- | --- | --- | --- |",
        ]
    )
    for name, target, tag_object, created in tags:
        lines.append(f"| `{md(name)}` | `{target[:12]}` | `{tag_object[:12]}` | {created} |")
    lines.extend(
        [
            "",
            "## 以后如何新增分支而不弄乱 execute",
            "",
            "从主线创建分支时，直接把 worktree 放进统一目录：",
            "",
            "```bash",
            f"cd {repo}",
            "git fetch origin",
            f"git worktree add {hub_root / 'worktrees'}/<目录名> \\",
            "  -b codex/<任务名> origin/main",
            "```",
            "",
            "完成后刷新本文档：",
            "",
            "```bash",
            "./scripts/update_branch_catalog.sh --fetch",
            "```",
            "",
            "新分支会自动出现在表格里。如果希望用途说明不是“待补充”，在 "
            "`docs/branch_catalog_metadata.json` 的 `branches` 中增加一条说明后再次运行更新脚本。",
            "",
            "检查目录是否过期而不写文件：",
            "",
            "```bash",
            "./scripts/update_branch_catalog.sh --check",
            "```",
            "",
            "## 管理规则",
            "",
            "1. `main` 永远是唯一正式主线；阶段分支不能自封为发布分支。",
            "2. Web IM/Agent Store 阶段在未合并前只在对应 `dev_*` worktree 验收；是否合并由用户审阅后决定。",
            "3. 每个小改动继续独立提交；阶段完成且获准后才合并回 `main` 并推送远端。",
            f"4. 新 worktree 一律建在 `{hub_root / 'worktrees'}`。",
            "5. 推送成功后删除已完成的本地 worktree 和本地阶段分支，不长期堆积。",
            "6. 删除远端 active 分支前，必须确认提交已进入 `main` 或已有同 SHA 的 "
            "`archive/*` 冻结引用。",
            "7. `archive/*` 只用于保全证据，不在其中开发、不移动其尖端。",
            "8. 删除 worktree 前先确认状态干净、提交已推送；"
            "使用 `git worktree remove`，不要直接删目录。",
            "9. 每次新增、移动或删除分支/worktree 后运行目录更新脚本并提交生成结果。",
            "",
        ]
    )
    return "\n".join(lines)


def write_or_check(path: Path, content: str, check: bool) -> bool:
    existing = path.read_text(encoding="utf-8") if path.exists() else None
    if check:
        if existing != content:
            print(f"stale: {path}", file=sys.stderr)
            return False
        print(f"current: {path}")
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)
    print(f"updated: {path}")
    return True


def build_parser() -> argparse.ArgumentParser:
    script_repo = Path(__file__).resolve().parent.parent
    default_output = branch_hub_root(script_repo) / "BRANCH_CATALOG.md"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=script_repo)
    parser.add_argument(
        "--metadata", type=Path, default=script_repo / "docs" / "branch_catalog_metadata.json"
    )
    parser.add_argument("--output", type=Path, default=default_output)
    parser.add_argument(
        "--fetch", action="store_true", help="refresh origin refs before generation"
    )
    parser.add_argument("--check", action="store_true", help="fail if generated files are stale")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo = args.repo.resolve()
    if args.fetch:
        result = git(repo, "fetch", "--prune", "origin", check=False)
        if result.returncode != 0:
            print(result.stderr.strip(), file=sys.stderr)
            return result.returncode
    try:
        purposes = load_purposes(args.metadata.resolve())
        worktrees = parse_worktrees(repo)
        output = args.output.resolve()
        main_oid = catalog_main_baseline(repo, output)
        branches = collect_branches(repo, purposes, worktrees, main_oid=main_oid)
        content = render_catalog(repo, branches, worktrees, tag_rows(repo))
    except (KeyError, ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"branch catalog generation failed: {exc}", file=sys.stderr)
        return 2
    return 0 if write_or_check(output, content, args.check) else 1


if __name__ == "__main__":
    raise SystemExit(main())
