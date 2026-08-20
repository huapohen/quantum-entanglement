#!/usr/bin/env python3
"""Verify offline source-bound vulnerability and license evidence for promotion."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

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

_SAFE_FAILURE_CODES = frozenset(
    {
        "evaluation_time_invalid",
        "license_expression_invalid",
        "license_expression_noncanonical",
        "license_expression_unknown",
        "risk_component_coverage_mismatch",
        "risk_database_drift",
        "risk_database_file_invalid",
        "risk_database_integrity_unverified",
        "risk_database_stale",
        "risk_database_unapproved",
        "risk_exception_database_mismatch",
        "risk_exception_expired",
        "risk_exception_inactive",
        "risk_exception_reused",
        "risk_exception_unused",
        "risk_fix_status_unknown",
        "risk_json_invalid",
        "risk_json_noncanonical",
        "risk_lock_drift",
        "risk_lock_evidence_invalid",
        "risk_manifest_drift",
        "risk_manifest_evidence_invalid",
        "risk_policy_database_invalid",
        "risk_policy_denied",
        "risk_policy_drift",
        "risk_policy_evidence_invalid",
        "risk_policy_exception_invalid",
        "risk_policy_exception_owner_invalid",
        "risk_policy_exception_rationale_invalid",
        "risk_policy_exception_scope_invalid",
        "risk_policy_exception_time_invalid",
        "risk_policy_file_invalid",
        "risk_policy_incomplete",
        "risk_policy_invalid",
        "risk_policy_license_invalid",
        "risk_policy_scanner_invalid",
        "risk_policy_vulnerability_invalid",
        "risk_promotion_disabled",
        "risk_result_artifact_invalid",
        "risk_result_component_duplicate",
        "risk_result_component_invalid",
        "risk_result_database_invalid",
        "risk_result_file_invalid",
        "risk_result_finding_duplicate",
        "risk_result_finding_invalid",
        "risk_result_from_future",
        "risk_result_invalid",
        "risk_result_license_invalid",
        "risk_result_lock_invalid",
        "risk_result_manifest_invalid",
        "risk_result_policy_invalid",
        "risk_result_project_invalid",
        "risk_result_sbom_invalid",
        "risk_result_scan_invalid",
        "risk_result_scanner_invalid",
        "risk_result_source_invalid",
        "risk_result_stale",
        "risk_sbom_component_invalid",
        "risk_sbom_drift",
        "risk_scan_incomplete",
        "risk_scanner_unapproved",
        "risk_severity_unknown",
        "risk_source_drift",
        "risk_source_evidence_changed",
        "risk_source_evidence_invalid",
    }
)


class _RedactedParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
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
        print(
            json.dumps(
                verification_summary_document(summary), separators=(",", ":"), sort_keys=True
            )
        )
    except DependencyRiskError as exc:
        code = exc.code if exc.code in _SAFE_FAILURE_CODES else "risk_internal_error"
        print(f"dependency risk verification failed: {code}", file=sys.stderr)
        return 1
    except Exception:
        print("dependency risk verification failed: risk_internal_error", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
