#!/usr/bin/env python3
"""Reject obsolete workspace paths from tracked operational text files."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

TEXT_SUFFIXES = {
    ".html",
    ".json",
    ".md",
    ".py",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}

# Keep these split so the guard does not report its own source as stale.
OBSOLETE_PATHS = (
    "execute/" + "quantum_entanglement",
    "quantum_entanglement/" + "main",
)

# These occurrences describe migration history or exercise legacy-layout compatibility.
EXEMPTIONS = {
    "MIGRATION_MANIFEST.md": frozenset(OBSOLETE_PATHS),
    "tests/test_branch_catalog.py": frozenset(OBSOLETE_PATHS),
}


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    obsolete_path: str


def tracked_text_files(repo: Path) -> tuple[str, ...]:
    result = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "-z"],
        check=True,
        capture_output=True,
        text=True,
    )
    return tuple(
        path
        for path in result.stdout.split("\0")
        if path and Path(path).suffix.lower() in TEXT_SUFFIXES
    )


def find_obsolete_paths(repo: Path, relative_paths: Iterable[str]) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    for relative_path in relative_paths:
        exemptions = EXEMPTIONS.get(relative_path, frozenset())
        content = (repo / relative_path).read_text(encoding="utf-8")
        for line_number, line in enumerate(content.splitlines(), start=1):
            for obsolete_path in OBSOLETE_PATHS:
                if obsolete_path in line and obsolete_path not in exemptions:
                    findings.append(Finding(relative_path, line_number, obsolete_path))
    return tuple(findings)


def main(argv: Sequence[str] | None = None) -> int:
    args = tuple(sys.argv[1:] if argv is None else argv)
    if len(args) > 1:
        print("usage: check_workspace_paths.py [REPOSITORY]", file=sys.stderr)
        return 2
    repo = Path(args[0]).resolve() if args else Path(__file__).resolve().parent.parent
    try:
        findings = find_obsolete_paths(repo, tracked_text_files(repo))
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        print(f"workspace path audit failed: {exc}", file=sys.stderr)
        return 2
    if not findings:
        print(f"workspace paths current: {repo}")
        return 0
    for finding in findings:
        print(
            f"{finding.path}:{finding.line}: obsolete workspace path: "
            f"{finding.obsolete_path}",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
