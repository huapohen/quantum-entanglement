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
from typing import Callable, Sequence

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


def _quote_sql_literal(value: str) -> str:
    return value.replace("'", "''")


def apply_sqlite_migrations(
    connection: sqlite3.Connection,
    *,
    migrations: Sequence[Migration] = MIGRATIONS,
    clock: Callable[[], str] = utc_now,
) -> int:
    """Apply the registry and return the current schema version.

    The caller must provide an autocommit connection.  Concurrent initializers are
    safe: one applies the migration and the other verifies the winner's checksum.
    """

    if not callable(clock):
        raise TypeError("clock must be callable")
    _validate_registry(migrations)
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
        row = connection.execute(
            "SELECT filename, sha256 FROM qe_schema_migrations WHERE version = ?",
            (migration.version,),
        ).fetchone()
        sql = migration_text(migration.filename)
        digest = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        if row is not None:
            continue
        applied_at = clock()
        if not isinstance(applied_at, str) or not applied_at.strip():
            raise ValueError("migration clock must return a timestamp string")
        script = (
            "BEGIN IMMEDIATE;\n"
            f"{sql}\n"
            "INSERT INTO qe_schema_migrations "
            "(version, filename, sha256, applied_at) VALUES "
            f"({migration.version}, '{_quote_sql_literal(migration.filename)}', "
            f"'{_quote_sql_literal(digest)}', '{_quote_sql_literal(applied_at)}');\n"
            "COMMIT;"
        )
        try:
            connection.executescript(script)
        except sqlite3.IntegrityError:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            concurrent = connection.execute(
                "SELECT filename, sha256 FROM qe_schema_migrations WHERE version = ?",
                (migration.version,),
            ).fetchone()
            if (
                concurrent is None
                or concurrent["filename"] != migration.filename
                or concurrent["sha256"] != digest
            ):
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
