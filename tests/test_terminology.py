from __future__ import annotations

import unittest
from pathlib import Path

_DEPRECATED_PROTOTYPE_TERM = bytes.fromhex("e99d99e784b6")
_EXCLUDED_DIRECTORIES = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "node_modules",
        "references",
    }
)
_TEXT_FILENAMES = frozenset({".gitignore", "PKG-INFO"})
_TEXT_SUFFIXES = frozenset(
    {
        ".css",
        ".html",
        ".js",
        ".json",
        ".md",
        ".py",
        ".rst",
        ".sh",
        ".sql",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".yaml",
        ".yml",
    }
)


class TerminologyTests(unittest.TestCase):
    def test_active_repository_content_uses_the_canonical_v0_name(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        violations: list[str] = []

        for path in repository.rglob("*"):
            relative = path.relative_to(repository)
            if any(part in _EXCLUDED_DIRECTORIES for part in relative.parts):
                continue
            if _DEPRECATED_PROTOTYPE_TERM in relative.as_posix().encode("utf-8"):
                violations.append(relative.as_posix())
                continue
            if not path.is_file():
                continue
            if path.name not in _TEXT_FILENAMES and path.suffix.lower() not in _TEXT_SUFFIXES:
                continue
            if _DEPRECATED_PROTOTYPE_TERM in path.read_bytes():
                violations.append(relative.as_posix())

        self.assertEqual(violations, [], "deprecated prototype terminology found")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
