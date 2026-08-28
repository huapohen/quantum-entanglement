from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from quantum_entanglement.migrations import (
    MIGRATIONS,
    apply_sqlite_migrations,
    current_schema_version,
    migration_text,
    validate_sqlite_schema,
)
from quantum_entanglement.store import SQLiteEventStore

NOW = "2026-08-28T01:02:03.000000Z"


def _downgrade_to_v5(connection: sqlite3.Connection) -> None:
    connection.executescript(migration_text("0006_native_im_sandbox_provenance.down.sql"))
    connection.execute("DELETE FROM qe_schema_migrations WHERE version = 6")


def test_v5_upgrades_to_exact_provenance_schema_and_can_downgrade(tmp_path: Path) -> None:
    database = tmp_path / "provenance-migration.sqlite3"
    with SQLiteEventStore(str(database), clock=lambda: NOW):
        pass
    connection = sqlite3.connect(database, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        _downgrade_to_v5(connection)
        assert current_schema_version(connection) == 5
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE name = 'native_im_inbound_provenance'"
            ).fetchone()
            is None
        )

        assert apply_sqlite_migrations(connection, clock=lambda: NOW) == 6
        assert validate_sqlite_schema(connection) == 6
        migration = MIGRATIONS[5]
        ledger = connection.execute(
            "SELECT filename, sha256 FROM qe_schema_migrations WHERE version = 6"
        ).fetchone()
        assert tuple(ledger) == (
            migration.filename,
            hashlib.sha256(migration_text(migration.filename).encode()).hexdigest(),
        )

        columns = connection.execute("PRAGMA table_info('native_im_inbound_provenance')").fetchall()
        assert tuple((row["name"], row["type"], row["notnull"], row["pk"]) for row in columns) == (
            ("tenant_id", "TEXT", 1, 1),
            ("workspace_id", "TEXT", 1, 2),
            ("provider", "TEXT", 1, 3),
            ("channel_id", "TEXT", 1, 4),
            ("read_request_digest", "TEXT", 1, 5),
            ("page_digest", "TEXT", 1, 0),
            ("approval_id", "TEXT", 1, 0),
            ("authority_revision", "INTEGER", 1, 0),
            ("approval_digest", "TEXT", 1, 0),
            ("configuration_binding_digest", "TEXT", 1, 0),
            ("profile_id", "TEXT", 1, 0),
            ("profile_revision", "TEXT", 1, 0),
            ("profile_digest", "TEXT", 1, 0),
            ("provider_manifest_digest", "TEXT", 1, 0),
            ("transport_contract_id", "TEXT", 1, 0),
            ("transport_contract_digest", "TEXT", 1, 0),
            ("mapper_contract_id", "TEXT", 1, 0),
            ("mapper_contract_digest", "TEXT", 1, 0),
            ("transport_evidence_digest", "TEXT", 1, 0),
            ("mapping_evidence_digest", "TEXT", 1, 0),
            ("provenance_json", "TEXT", 1, 0),
            ("provenance_digest", "TEXT", 1, 0),
            ("admitted_at", "TEXT", 1, 0),
        )
        foreign_keys = connection.execute(
            "PRAGMA foreign_key_list('native_im_inbound_provenance')"
        ).fetchall()
        assert len(foreign_keys) == 6
        assert {
            (row["table"], row["from"], row["to"], row["on_update"], row["on_delete"])
            for row in foreign_keys
        } == {
            ("native_im_inbound_reads", column, column, "RESTRICT", "RESTRICT")
            for column in (
                "tenant_id",
                "workspace_id",
                "provider",
                "channel_id",
                "read_request_digest",
                "page_digest",
            )
        }
        assert (
            connection.execute("SELECT name FROM sqlite_schema WHERE type = 'trigger'").fetchall()
            == []
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

        _downgrade_to_v5(connection)
        assert current_schema_version(connection) == 5
        assert validate_sqlite_schema(connection) == 5
    finally:
        connection.close()


def test_provenance_schema_rejects_orphan_and_noncanonical_digest(tmp_path: Path) -> None:
    database = tmp_path / "provenance-constraints.sqlite3"
    with SQLiteEventStore(str(database), clock=lambda: NOW):
        pass
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        columns = (
            "tenant_id, workspace_id, provider, channel_id, read_request_digest, "
            "page_digest, approval_id, authority_revision, approval_digest, "
            "configuration_binding_digest, profile_id, profile_revision, profile_digest, "
            "provider_manifest_digest, transport_contract_id, transport_contract_digest, "
            "mapper_contract_id, mapper_contract_digest, transport_evidence_digest, "
            "mapping_evidence_digest, provenance_json, provenance_digest, admitted_at"
        )
        values: tuple[object, ...] = (
            "tenant",
            "workspace",
            "provider",
            "channel",
            "a" * 64,
            "b" * 64,
            "approval",
            1,
            "c" * 64,
            "d" * 64,
            "profile",
            "revision",
            "e" * 64,
            "f" * 64,
            "transport",
            "0" * 64,
            "mapper",
            "1" * 64,
            "2" * 64,
            "3" * 64,
            "{}",
            "4" * 64,
            NOW,
        )
        statement = (
            f"INSERT INTO native_im_inbound_provenance ({columns}) "
            f"VALUES ({','.join('?' for _ in values)})"
        )
        try:
            connection.execute(statement, values)
        except sqlite3.IntegrityError:
            pass
        else:  # pragma: no cover - the foreign key is a release-blocking invariant.
            raise AssertionError("orphan provenance row was accepted")

        invalid = list(values)
        invalid[8] = "C" * 64
        try:
            connection.execute(statement, tuple(invalid))
        except sqlite3.IntegrityError:
            pass
        else:  # pragma: no cover - the digest CHECK is a release-blocking invariant.
            raise AssertionError("noncanonical provenance digest was accepted")
    finally:
        connection.close()
