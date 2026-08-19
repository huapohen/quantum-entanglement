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
import re
import sqlite3
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Sequence, Tuple

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
    Migration(3, "0003_outbox_ambiguities.up.sql"),
)

_LEDGER_SCHEMA_SQL = """
CREATE TABLE qe_schema_migrations (
    version INTEGER PRIMARY KEY,
    filename TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL,
    applied_at TEXT NOT NULL
)
"""
_CREATE_OBJECT_PATTERN = re.compile(
    r"\bCREATE\s+(?:UNIQUE\s+)?(?P<kind>TABLE|INDEX)\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)
_DROP_OBJECT_PATTERN = re.compile(
    r"\bDROP\s+(?P<kind>TABLE|INDEX)\s+"
    r"(?:IF\s+EXISTS\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)


def _validate_registry(migrations: Sequence[Migration]) -> None:
    versions = [item.version for item in migrations]
    filenames = [item.filename for item in migrations]
    if versions != sorted(versions) or len(set(versions)) != len(versions):
        raise ValueError("migration versions must be unique and strictly ordered")
    if versions != list(range(1, len(versions) + 1)):
        raise ValueError("migration registry must be a continuous prefix starting at one")
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


def _canonical_schema_sql(sql: str) -> str:
    without_idempotency_clause = re.sub(
        r"\bIF\s+NOT\s+EXISTS\b",
        "",
        sql,
        flags=re.IGNORECASE,
    )
    return " ".join(without_idempotency_clause.strip().rstrip(";").split())


def _expected_schema_objects(
    migrations: Sequence[Migration],
    applied_versions: Tuple[int, ...],
) -> Dict[Tuple[str, str], str]:
    expected: Dict[Tuple[str, str], str] = {
        ("table", "qe_schema_migrations"): _canonical_schema_sql(_LEDGER_SCHEMA_SQL)
    }
    applied = set(applied_versions)
    for migration in migrations:
        if migration.version not in applied:
            continue
        for statement in _sql_statements(migration_text(migration.filename)):
            created = _CREATE_OBJECT_PATTERN.search(statement)
            if created is not None:
                key = (created.group("kind").lower(), created.group("name"))
                expected[key] = _canonical_schema_sql(statement[created.start() :])
                continue
            dropped = _DROP_OBJECT_PATTERN.search(statement)
            if dropped is not None:
                key = (dropped.group("kind").lower(), dropped.group("name"))
                expected.pop(key, None)
    return expected


def validate_sqlite_schema(
    connection: sqlite3.Connection,
    *,
    migrations: Sequence[Migration] = MIGRATIONS,
) -> int:
    """Validate migration history and exact packaged schema objects without mutation."""

    _validate_registry(migrations)
    ledger = connection.execute(
        """
        SELECT type, sql FROM main.sqlite_master
        WHERE name = 'qe_schema_migrations'
        """
    ).fetchone()
    if ledger is None:
        return 0
    if ledger[0] != "table":
        raise MigrationDriftError("SQLite object 'qe_schema_migrations' is not a table")

    rows = connection.execute(
        """
        SELECT version, filename, sha256
        FROM main.qe_schema_migrations
        ORDER BY version
        """
    ).fetchall()
    known = {item.version: item for item in migrations}
    applied_versions = tuple(int(row[0]) for row in rows)
    for row in rows:
        version = int(row[0])
        migration = known.get(version)
        if migration is None:
            raise MigrationVersionError(
                f"database schema version {version} is newer than this binary"
            )
        sql = migration_text(migration.filename)
        digest = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        if row[1] != migration.filename or row[2] != digest:
            raise MigrationDriftError(
                f"migration {version} checksum or filename differs from the applied schema"
            )
    ordered_versions = tuple(item.version for item in migrations)
    if applied_versions != ordered_versions[: len(applied_versions)]:
        raise MigrationDriftError(
            "migration ledger must be a continuous registry prefix starting at one"
        )

    expected_objects = _expected_schema_objects(migrations, applied_versions)
    for (object_type, name), expected_sql in expected_objects.items():
        row = connection.execute(
            """
            SELECT type, sql FROM main.sqlite_master
            WHERE type = ? AND name = ?
            """,
            (object_type, name),
        ).fetchone()
        if row is None or row[1] is None:
            raise MigrationDriftError(f"migration-owned SQLite {object_type} {name!r} is missing")
        actual_sql = _canonical_schema_sql(str(row[1]))
        if actual_sql != expected_sql:
            raise MigrationDriftError(
                f"migration-owned SQLite {object_type} {name!r} differs from packaged schema"
            )
    return applied_versions[-1] if applied_versions else 0


def _sqlite_sha256(value: object) -> str:
    """Hash legacy fencing tokens without ever formatting or logging them."""

    if not isinstance(value, str) or not value:
        raise ValueError("qe_sha256 requires a non-empty text value")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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
    connection.create_function("qe_sha256", 1, _sqlite_sha256, deterministic=True)
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
    applied_versions = tuple(int(row["version"]) for row in rows)
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
    if applied_versions != ordered_versions[: len(applied_versions)]:
        raise MigrationDriftError(
            "migration ledger must be a continuous registry prefix starting at one"
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
    rows = connection.execute(
        "SELECT version FROM qe_schema_migrations ORDER BY version"
    ).fetchall()
    versions = tuple(int(row["version"]) for row in rows)
    if versions != tuple(range(1, len(versions) + 1)):
        raise MigrationDriftError("migration ledger must be a continuous prefix starting at one")
    return versions[-1] if versions else 0


__all__ = [
    "MIGRATIONS",
    "Migration",
    "MigrationDriftError",
    "MigrationVersionError",
    "apply_sqlite_migrations",
    "current_schema_version",
    "migration_text",
    "validate_sqlite_schema",
]
