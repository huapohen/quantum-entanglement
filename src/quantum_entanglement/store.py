"""SQLite append-only event store with optimistic concurrency and idempotency."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

from .events import DomainEvent, StoredEvent


class ConcurrencyError(RuntimeError):
    """Raised when a stream changed after the caller read it."""


class SQLiteEventStore:
    """Small durable event log suitable for the kernel and local-first clients."""

    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        if path != ":memory:":
            parent = os.path.dirname(os.path.abspath(path))
            os.makedirs(parent, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._initialize()

    def _initialize(self) -> None:
        with self._lock:
            self._connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS events (
                    global_position INTEGER PRIMARY KEY AUTOINCREMENT,
                    stream_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    correlation_id TEXT,
                    causation_id TEXT,
                    idempotency_key TEXT,
                    UNIQUE(stream_id, sequence),
                    UNIQUE(stream_id, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS idx_events_stream
                    ON events(stream_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_events_correlation
                    ON events(correlation_id, global_position);
                CREATE TABLE IF NOT EXISTS snapshots (
                    stream_id TEXT PRIMARY KEY,
                    sequence INTEGER NOT NULL,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> StoredEvent:
        event = DomainEvent(
            stream_id=row["stream_id"],
            event_type=row["event_type"],
            payload=json.loads(row["payload_json"]),
            actor_id=row["actor_id"],
            event_id=row["event_id"],
            timestamp=row["timestamp"],
            correlation_id=row["correlation_id"],
            causation_id=row["causation_id"],
            idempotency_key=row["idempotency_key"],
        )
        return StoredEvent(
            event=event,
            sequence=int(row["sequence"]),
            global_position=int(row["global_position"]),
        )

    def stream_version(self, stream_id: str) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS version FROM events WHERE stream_id = ?",
                (stream_id,),
            ).fetchone()
            return int(row["version"])

    def append(self, event: DomainEvent, expected_version: Optional[int] = None) -> StoredEvent:
        """Append one event, returning the existing record for an idempotent retry."""

        with self._transaction() as connection:
            if event.idempotency_key is not None:
                existing = connection.execute(
                    "SELECT * FROM events WHERE stream_id = ? AND idempotency_key = ?",
                    (event.stream_id, event.idempotency_key),
                ).fetchone()
                if existing is not None:
                    return self._row_to_event(existing)

            row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS version FROM events WHERE stream_id = ?",
                (event.stream_id,),
            ).fetchone()
            current_version = int(row["version"])
            if expected_version is not None and expected_version != current_version:
                raise ConcurrencyError(
                    "stream %s expected version %d but is %d"
                    % (event.stream_id, expected_version, current_version)
                )
            sequence = current_version + 1
            cursor = connection.execute(
                """
                INSERT INTO events (
                    stream_id, sequence, event_id, event_type, actor_id, timestamp,
                    payload_json, correlation_id, causation_id, idempotency_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.stream_id,
                    sequence,
                    event.event_id,
                    event.event_type,
                    event.actor_id,
                    event.timestamp,
                    json.dumps(dict(event.payload), ensure_ascii=False, sort_keys=True),
                    event.correlation_id,
                    event.causation_id,
                    event.idempotency_key,
                ),
            )
            return StoredEvent(event, sequence, int(cursor.lastrowid))

    def append_many(
        self,
        stream_id: str,
        events: Iterable[DomainEvent],
        expected_version: Optional[int] = None,
    ) -> Tuple[StoredEvent, ...]:
        """Atomically append a batch to one stream."""

        batch = tuple(events)
        if any(item.stream_id != stream_id for item in batch):
            raise ValueError("all batch events must use the declared stream_id")
        if not batch:
            return ()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS version FROM events WHERE stream_id = ?",
                (stream_id,),
            ).fetchone()
            current_version = int(row["version"])
            if expected_version is not None and expected_version != current_version:
                raise ConcurrencyError(
                    "stream %s expected version %d but is %d"
                    % (stream_id, expected_version, current_version)
                )
            stored: List[StoredEvent] = []
            for offset, event in enumerate(batch, start=1):
                sequence = current_version + offset
                cursor = connection.execute(
                    """
                    INSERT INTO events (
                        stream_id, sequence, event_id, event_type, actor_id, timestamp,
                        payload_json, correlation_id, causation_id, idempotency_key
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        stream_id,
                        sequence,
                        event.event_id,
                        event.event_type,
                        event.actor_id,
                        event.timestamp,
                        json.dumps(dict(event.payload), ensure_ascii=False, sort_keys=True),
                        event.correlation_id,
                        event.causation_id,
                        event.idempotency_key,
                    ),
                )
                stored.append(StoredEvent(event, sequence, int(cursor.lastrowid)))
            return tuple(stored)

    def read_stream(self, stream_id: str, after_sequence: int = 0) -> Tuple[StoredEvent, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM events WHERE stream_id = ? AND sequence > ? ORDER BY sequence",
                (stream_id, after_sequence),
            ).fetchall()
            return tuple(self._row_to_event(row) for row in rows)

    def read_all(self, after_position: int = 0, limit: int = 1000) -> Tuple[StoredEvent, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM events WHERE global_position > ? ORDER BY global_position LIMIT ?",
                (after_position, limit),
            ).fetchall()
            return tuple(self._row_to_event(row) for row in rows)

    def save_snapshot(self, stream_id: str, sequence: int, state: Dict[str, object], at: str) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO snapshots(stream_id, sequence, state_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(stream_id) DO UPDATE SET
                    sequence = excluded.sequence,
                    state_json = excluded.state_json,
                    updated_at = excluded.updated_at
                WHERE excluded.sequence >= snapshots.sequence
                """,
                (stream_id, sequence, json.dumps(state, ensure_ascii=False, sort_keys=True), at),
            )

    def load_snapshot(self, stream_id: str) -> Optional[Tuple[int, Dict[str, object]]]:
        with self._lock:
            row = self._connection.execute(
                "SELECT sequence, state_json FROM snapshots WHERE stream_id = ?", (stream_id,)
            ).fetchone()
            if row is None:
                return None
            return int(row["sequence"]), json.loads(row["state_json"])

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "SQLiteEventStore":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

