# ruff: noqa: UP006, UP035, UP045
"""Versioned, checksum-verified SQLite migration runner.

All stores share one ordered registry.  A process refuses to open a database whose
recorded migration differs from the packaged SQL or whose schema is newer than this
binary.  The migration body and its ledger row are committed in the same SQLite
transaction so a crash cannot expose an unrecorded schema change.
"""

from __future__ import annotations

import hashlib
import importlib.resources
import sqlite3
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from ..protocol import utc_now


class MigrationDriftError(RuntimeError):
    """Raised when the database migration history and packaged SQL disagree."""


class MigrationVersionError(RuntimeError):
    """Raised when a database was created by a newer, unsupported binary."""


@dataclass(frozen=True)
class Migration:
    version: int
    filename: str

    def __post_init__(self) -> None:
        if not isinstance(self.version, int) or isinstance(self.version, bool):
            raise TypeError("migration version must be an integer")
        if self.version <= 0:
            raise ValueError("migration version must be greater than zero")
        if not isinstance(self.filename, str) or not self.filename.endswith(".up.sql"):
            raise ValueError("migration filename must end with .up.sql")


MIGRATIONS: Sequence[Migration] = (
    Migration(1, "0001_invocation_attempts.up.sql"),
    Migration(2, "0002_artifacts.up.sql"),
)


def _validate_registry(migrations: Sequence[Migration]) -> None:
    versions = [item.version for item in migrations]
    filenames = [item.filename for item in migrations]
    if versions != sorted(versions) or len(set(versions)) != len(versions):
        raise ValueError("migration versions must be unique and strictly ordered")
    if len(set(filenames)) != len(filenames):
        raise ValueError("migration filenames must be unique")


def migration_text(filename: str) -> str:
    """Read migration SQL from installed package data."""

    return (
        importlib.resources.files("quantum_entanglement.migrations")
        .joinpath(filename)
        .read_text(encoding="utf-8")
    )


def _sql_statements(script: str) -> Sequence[str]:
    """Split packaged SQL with SQLite's own completeness parser."""

    statements = []
    buffer = ""
    for character in script:
        buffer += character
        if character == ";" and sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement:
                statements.append(statement)
            buffer = ""
    if buffer.strip():
        raise ValueError("migration SQL ends with an incomplete statement")
    return tuple(statements)


def apply_sqlite_migrations(
    connection: sqlite3.Connection,
    *,
    migrations: Sequence[Migration] = MIGRATIONS,
    target_versions: Optional[Sequence[int]] = None,
    clock: Callable[[], str] = utc_now,
) -> int:
    """Apply the registry and return the current schema version.

    The caller must provide an autocommit connection.  Concurrent initializers are
    safe: one applies the migration and the other verifies the winner's checksum.
    """

    if not callable(clock):
        raise TypeError("clock must be callable")
    _validate_registry(migrations)
    ordered_versions = tuple(item.version for item in migrations)
    if target_versions is None:
        selected_versions = set(ordered_versions)
    else:
        requested_versions = tuple(target_versions)
        if any(type(version) is not int for version in requested_versions):
            raise TypeError("target migration versions must be integers")
        expected_prefix = ordered_versions[: len(requested_versions)]
        if requested_versions != expected_prefix:
            raise ValueError("target migration versions must be a continuous registry prefix")
        selected_versions = set(requested_versions)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS qe_schema_migrations (
            version INTEGER PRIMARY KEY,
            filename TEXT NOT NULL UNIQUE,
            sha256 TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    known = {item.version: item for item in migrations}
    rows = connection.execute(
        "SELECT version, filename, sha256 FROM qe_schema_migrations ORDER BY version"
    ).fetchall()
    for row in rows:
        version = int(row["version"])
        migration = known.get(version)
        if migration is None:
            raise MigrationVersionError(
                f"database schema version {version} is newer than this binary"
            )
        sql = migration_text(migration.filename)
        digest = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        if row["filename"] != migration.filename or row["sha256"] != digest:
            raise MigrationDriftError(
                f"migration {version} checksum or filename differs from the applied schema"
            )

    for migration in migrations:
        if migration.version not in selected_versions:
            continue
        sql = migration_text(migration.filename)
        digest = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        try:
            connection.execute("BEGIN IMMEDIATE")
            # Recheck only after owning the write lock. Another initializer may
            # have committed this migration after the optimistic precheck.
            row = connection.execute(
                "SELECT filename, sha256 FROM qe_schema_migrations WHERE version = ?",
                (migration.version,),
            ).fetchone()
            if row is not None:
                if row["filename"] != migration.filename or row["sha256"] != digest:
                    raise MigrationDriftError(
                        f"migration {migration.version} checksum or filename differs "
                        "from the applied schema"
                    )
                connection.execute("COMMIT")
                continue
            applied_at = clock()
            if not isinstance(applied_at, str) or not applied_at.strip():
                raise ValueError("migration clock must return a timestamp string")
            for statement in _sql_statements(sql):
                connection.execute(statement)
            connection.execute(
                """
                INSERT INTO qe_schema_migrations (
                    version, filename, sha256, applied_at
                ) VALUES (?, ?, ?, ?)
                """,
                (migration.version, migration.filename, digest, applied_at),
            )
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise

    return current_schema_version(connection)


def current_schema_version(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT COALESCE(MAX(version), 0) AS version FROM qe_schema_migrations"
    ).fetchone()
    return int(row["version"])


__all__ = [
    "MIGRATIONS",
    "Migration",
    "MigrationDriftError",
    "MigrationVersionError",
    "apply_sqlite_migrations",
    "current_schema_version",
    "migration_text",
]
