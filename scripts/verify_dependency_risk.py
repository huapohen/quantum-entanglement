#!/usr/bin/env python3
"""Verify offline source-bound vulnerability and license evidence for promotion."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from scripts.dependency_risk import (  # noqa: E402
    DEFAULT_POLICY_PATH,
    DependencyRiskError,
    collect_risk_evidence_context,
    load_dependency_risk_policy,
    load_dependency_risk_result,
    parse_timestamp,
    read_database_snapshot,
    require_outside_repository_file,
    verification_summary_document,
    verify_dependency_risk,
)


class _RedactedParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        self.exit(2, "dependency risk verification failed: argument_invalid\n")


def _parser() -> argparse.ArgumentParser:
    parser = _RedactedParser(description=__doc__, add_help=True)
    parser.add_argument("--repository-root", type=Path, default=_REPOSITORY_ROOT)
    parser.add_argument("--distribution-directory", type=Path, default=Path("dist"))
    parser.add_argument("--distribution-manifest", type=Path, required=True)
    parser.add_argument("--sbom-directory", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--database-snapshot", type=Path, required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--evaluation-time", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        evaluation_time = parse_timestamp(args.evaluation_time, "evaluation_time_invalid")
        context = collect_risk_evidence_context(
            args.repository_root,
            args.distribution_directory,
            args.distribution_manifest,
            args.sbom_directory,
            expected_commit=args.expected_commit,
        )
        policy = load_dependency_risk_policy(args.repository_root / DEFAULT_POLICY_PATH)
        require_outside_repository_file(
            args.result, args.repository_root, "risk_result_file_invalid"
        )
        result = load_dependency_risk_result(args.result)
        database_snapshot = read_database_snapshot(
            args.database_snapshot,
            repository_root=args.repository_root,
            expected=result.database,
        )
        summary = verify_dependency_risk(
            policy,
            result,
            context,
            database_snapshot=database_snapshot,
            evaluation_time=evaluation_time,
        )
    except DependencyRiskError as exc:
        print(f"dependency risk verification failed: {exc.code}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            verification_summary_document(summary), separators=(",", ":"), sort_keys=True
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
