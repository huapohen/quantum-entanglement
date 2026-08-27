from __future__ import annotations

import unittest
from pathlib import Path

_DEPRECATED_PROTOTYPE_TERMS = (
    bytes.fromhex("e99d99e784b6"),
    bytes.fromhex("6a696e6772616e"),
)
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
        "report_sync_bundles",
    }
)
_EXCLUDED_FILES = frozenset({"analysis_report/notion_sync_manifest.json"})
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
            relative_bytes = relative.as_posix().encode("utf-8").lower()
            if relative.as_posix() in _EXCLUDED_FILES:
                continue
            if any(part in _EXCLUDED_DIRECTORIES for part in relative.parts):
                continue
            if any(term in relative_bytes for term in _DEPRECATED_PROTOTYPE_TERMS):
                violations.append(relative.as_posix())
                continue
            if not path.is_file():
                continue
            if path.name not in _TEXT_FILENAMES and path.suffix.lower() not in _TEXT_SUFFIXES:
                continue
            content = path.read_bytes().lower()
            if any(term in content for term in _DEPRECATED_PROTOTYPE_TERMS):
                violations.append(relative.as_posix())

        self.assertEqual(violations, [], "deprecated prototype terminology found")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
