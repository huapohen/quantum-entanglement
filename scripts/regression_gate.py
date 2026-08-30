#!/usr/bin/env python3
"""Select and run the smallest sufficient local regression gate for changed paths.

The selector is intentionally conservative: an unmapped runtime change escalates to
the full Python gate instead of silently claiming that a narrow test is sufficient.
It never starts a real connector or sends an external message.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GateCommand:
    name: str
    argv: tuple[str, ...]


_DIRECT_TESTS = {
    "branch_catalog.py": ("tests/test_branch_catalog.py",),
    "report_sync_bundle.py": ("tests/test_report_sync_bundle.py",),
    "start_local_trial.sh": ("tests/test_start_local_trial_script.py",),
    "verify_web_first.sh": (),
}

_HIGH_RISK_TEST_GLOBS = {
    "artifact_store.py": ("tests/test_artifact_store.py", "tests/test_artifact_codecs.py"),
    "attempts.py": (
        "tests/test_invocation_recovery.py",
        "tests/test_invocation_worker_lifecycle.py",
    ),
    "native_im": ("tests/test_native_im_*.py",),
    "process_identity.py": ("tests/test_process_identity.py",),
    "projections.py": ("tests/test_projections.py", "tests/test_result_projection.py"),
    "publisher.py": ("tests/test_publisher.py",),
    "runtime.py": ("tests/test_runtime.py", "tests/test_agent_runtime.py"),
    "store.py": ("tests/test_store*.py", "tests/test_event_store_*.py"),
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _git_paths(root: Path, arguments: Sequence[str]) -> tuple[str, ...]:
    completed = subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True,
        capture_output=True,
        text=True,
    )
    return tuple(path for path in completed.stdout.splitlines() if path)


def changed_paths(root: Path, base: str | None) -> tuple[str, ...]:
    """Return changed paths, including staged, unstaged, and untracked changes."""

    if base is not None:
        paths = list(_git_paths(root, ("diff", "--name-only", f"{base}...HEAD")))
        paths.extend(_git_paths(root, ("ls-files", "--others", "--exclude-standard")))
        return tuple(dict.fromkeys(paths))
    paths = list(_git_paths(root, ("diff", "--name-only", "HEAD")))
    paths.extend(_git_paths(root, ("diff", "--cached", "--name-only")))
    paths.extend(_git_paths(root, ("ls-files", "--others", "--exclude-standard")))
    return tuple(dict.fromkeys(paths))


def _existing_glob_paths(root: Path, pattern: str) -> tuple[str, ...]:
    return tuple(
        sorted(path.relative_to(root).as_posix() for path in root.glob(pattern) if path.is_file())
    )


def _python_tests(root: Path, paths: Iterable[str]) -> tuple[str, ...]:
    selected: set[str] = set()
    unmapped_runtime = False
    for path in paths:
        if path.startswith("tests/") and path.endswith(".py"):
            selected.add(path)
            continue
        if path.startswith("src/quantum_entanglement/") and path.endswith(".py"):
            module = Path(path).name
            direct = root / "tests" / f"test_{module}"
            matched = False
            if direct.is_file():
                selected.add(direct.relative_to(root).as_posix())
                matched = True
            for key, globs in _HIGH_RISK_TEST_GLOBS.items():
                if key in module:
                    for pattern in globs:
                        found = _existing_glob_paths(root, pattern)
                        selected.update(found)
                        matched = matched or bool(found)
            if not matched:
                unmapped_runtime = True
            continue
        if path.startswith("scripts/"):
            name = Path(path).name
            for test in _DIRECT_TESTS.get(name, ()):
                if (root / test).is_file():
                    selected.add(test)
            if name not in _DIRECT_TESTS and name.endswith(".py"):
                candidate = f"tests/test_{Path(name).stem}.py"
                if (root / candidate).is_file():
                    selected.add(candidate)
                else:
                    unmapped_runtime = True

    if unmapped_runtime:
        return ("__FULL_PYTHON_GATE_REQUIRED__",)
    return tuple(sorted(selected))


def select_commands(
    root: Path,
    paths: Sequence[str],
    *,
    full: bool = False,
) -> tuple[GateCommand, ...]:
    """Build deterministic commands from a path inventory."""

    path_set = set(paths)
    commands: list[GateCommand] = [
        GateCommand("diff-check", ("git", "diff", "--check")),
        GateCommand("cached-diff-check", ("git", "diff", "--cached", "--check")),
    ]
    python_executable = sys.executable
    python_tests = _python_tests(root, paths)
    code_changes = any(
        path.startswith(("src/", "tests/", "scripts/")) for path in paths
    )
    if full or "__FULL_PYTHON_GATE_REQUIRED__" in python_tests:
        commands.extend(
            (
                GateCommand("pytest-full", (python_executable, "-m", "pytest", "-q")),
                GateCommand("ruff", ("ruff", "check", "src", "tests", "scripts")),
                GateCommand(
                    "mypy-strict",
                    (python_executable, "-m", "mypy", "--strict", "src"),
                ),
                GateCommand(
                    "compileall",
                    (python_executable, "-m", "compileall", "-q", "src"),
                ),
            )
        )
    elif python_tests:
        changed_python = tuple(
            path
            for path in paths
            if path.startswith(("src/", "tests/", "scripts/")) and path.endswith(".py")
        )
        ruff_paths = tuple(dict.fromkeys((*changed_python, *python_tests)))
        commands.extend(
            (
                GateCommand(
                    "pytest-focused",
                    (python_executable, "-m", "pytest", "-q", *python_tests),
                ),
                GateCommand("ruff-focused", ("ruff", "check", *ruff_paths)),
            )
        )
    elif code_changes:
        commands.append(
            GateCommand("pytest-full-unmapped", (python_executable, "-m", "pytest", "-q"))
        )

    go_changes = any(
        (
            path.startswith("apps/im-api/")
            and path.endswith(".go")
        )
        or path in {"go.work", "go.work.sum"}
        for path in paths
    )
    if full or go_changes:
        commands.extend(
            (
                GateCommand("go-test", ("go", "test", "./...")),
                GateCommand("go-vet", ("go", "vet", "./...")),
            )
        )

    web_changes = any(
        path.startswith("clients/im-web/") and not path.endswith(".md")
        for path in paths
    )
    if full or web_changes:
        commands.extend(
            (
                GateCommand("web-build", ("npm", "run", "build")),
                GateCommand("web-first-synthetic", ("./scripts/verify_web_first.sh",)),
            )
        )
    elif "scripts/verify_web_first.sh" in path_set:
        commands.append(GateCommand("web-first-synthetic", ("./scripts/verify_web_first.sh",)))

    return tuple(commands)


def _command_cwd(root: Path, command: GateCommand) -> Path:
    if command.name.startswith("go-"):
        return root / "apps/im-api"
    if command.name == "web-build":
        # The Web package owns its package.json; running npm from the repository root
        # makes the stage/full gate fail before it can test any product code.
        return root / "clients/im-web"
    return root


def _run(root: Path, command: GateCommand) -> int:
    environment = os.environ.copy()
    environment.setdefault("GIT_TERMINAL_PROMPT", "0")
    if (
        command.name.startswith("pytest")
        or command.name.startswith("mypy")
        or command.name == "compileall"
    ):
        environment["PYTHONPATH"] = str(root / "src")
    cwd = _command_cwd(root, command)
    print(f"[{command.name}] {shlex.join(command.argv)}")
    return subprocess.run(command.argv, cwd=cwd, env=environment, check=False).returncode


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base",
        help="git revision; compare base...HEAD instead of worktree changes",
    )
    parser.add_argument("--full", action="store_true", help="run all local language gates")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print selected gates without running them",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = _repo_root()
    try:
        paths = changed_paths(root, args.base)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"unable to determine changed paths: {exc}", file=sys.stderr)
        return 2
    commands = select_commands(root, paths, full=args.full)
    print(f"changed_paths={len(paths)}")
    if paths:
        print("selected_paths:")
        for path in paths:
            print(f"  - {path}")
    print("selected_gates:")
    for command in commands:
        print(f"  - {command.name}: {shlex.join(command.argv)}")
    if args.dry_run:
        return 0
    for command in commands:
        status = _run(root, command)
        if status != 0:
            print(f"gate_failed={command.name} exit={status}", file=sys.stderr)
            return status
    print("regression_gate=passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
