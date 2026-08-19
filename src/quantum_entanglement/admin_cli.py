# ruff: noqa: UP006, UP035, UP045
"""Operational command line for backup and restore workflows."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from .backup import (
    BackupError,
    BackupExistsError,
    BackupIntegrityError,
    BackupManifest,
    create_sqlite_backup,
    default_manifest_path,
    restore_sqlite_backup,
    verify_sqlite_backup,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qe-admin",
        description="Quantum Entanglement local service administration",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="emit single-line JSON",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    backup = commands.add_parser("backup", help="create and verify a new SQLite backup")
    backup.add_argument("--source", required=True, help="live SQLite database path")
    backup.add_argument("--destination", required=True, help="new backup database path")
    backup.add_argument("--manifest", help="optional new manifest path")

    verify = commands.add_parser("verify-backup", help="verify a backup and its manifest")
    verify.add_argument("--backup", required=True, help="backup database path")
    verify.add_argument("--manifest", help="optional manifest path")

    restore = commands.add_parser("restore-backup", help="restore to a new SQLite path")
    restore.add_argument("--backup", required=True, help="backup database path")
    restore.add_argument("--destination", required=True, help="new restored database path")
    restore.add_argument("--manifest", help="optional manifest path")
    return parser


def _write_json(value: Dict[str, Any], *, compact: bool, error: bool = False) -> None:
    stream = sys.stderr if error else sys.stdout
    json.dump(
        value,
        stream,
        allow_nan=False,
        ensure_ascii=False,
        indent=None if compact else 2,
        separators=(",", ":") if compact else None,
        sort_keys=True,
    )
    stream.write("\n")


def _success(
    operation: str,
    manifest: BackupManifest,
    *,
    compact: bool,
    paths: Dict[str, str],
) -> int:
    _write_json(
        {
            "ok": True,
            "operation": operation,
            "paths": paths,
            "manifest": manifest.to_dict(),
        },
        compact=compact,
    )
    return 0


def _error_code(error: BaseException) -> str:
    if isinstance(error, BackupExistsError):
        return "TARGET_EXISTS"
    if isinstance(error, BackupIntegrityError):
        return "BACKUP_INTEGRITY_FAILED"
    if isinstance(error, FileNotFoundError):
        return "FILE_NOT_FOUND"
    if isinstance(error, BackupError):
        return "BACKUP_OPERATION_FAILED"
    if isinstance(error, (TypeError, ValueError)):
        return "INVALID_ARGUMENT"
    return "IO_ERROR"


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    compact = bool(args.compact)
    try:
        if args.command == "backup":
            destination = Path(args.destination)
            manifest_path = Path(args.manifest) if args.manifest else None
            manifest = create_sqlite_backup(
                args.source,
                destination,
                manifest_path=manifest_path,
            )
            return _success(
                "backup",
                manifest,
                compact=compact,
                paths={
                    "backup": str(destination),
                    "manifest": str(manifest_path or default_manifest_path(destination)),
                },
            )
        if args.command == "verify-backup":
            backup_path = Path(args.backup)
            manifest_path = Path(args.manifest) if args.manifest else None
            manifest = verify_sqlite_backup(
                backup_path,
                manifest_path=manifest_path,
            )
            return _success(
                "verify-backup",
                manifest,
                compact=compact,
                paths={
                    "backup": str(backup_path),
                    "manifest": str(manifest_path or default_manifest_path(backup_path)),
                },
            )
        if args.command == "restore-backup":
            backup_path = Path(args.backup)
            destination = Path(args.destination)
            manifest_path = Path(args.manifest) if args.manifest else None
            manifest = restore_sqlite_backup(
                backup_path,
                destination,
                manifest_path=manifest_path,
            )
            return _success(
                "restore-backup",
                manifest,
                compact=compact,
                paths={
                    "backup": str(backup_path),
                    "manifest": str(manifest_path or default_manifest_path(backup_path)),
                    "destination": str(destination),
                },
            )
        raise RuntimeError("argparse returned an unsupported command")
    except (BackupError, FileNotFoundError, OSError, TypeError, ValueError) as exc:
        _write_json(
            {
                "ok": False,
                "operation": args.command,
                "error": {
                    "code": _error_code(exc),
                    "message": str(exc),
                },
            },
            compact=compact,
            error=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
