# ruff: noqa: UP006, UP035, UP045
"""Tenant-scoped, transactional artifact metadata and blob persistence.

The event-sourced ``ArtifactLedger`` remains useful for the 0.1 kernel, but it keeps
content in event JSON and assigns versions in process memory.  This store is the
durable single-node write boundary: content-addressed bytes and version metadata are
committed in one SQLite transaction, version allocation is serialized across
processes, and every read verifies the stored SHA-256 digest and request fingerprint.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import os
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Tuple, cast
from urllib.parse import quote

from .migrations import apply_sqlite_migrations, current_schema_version
from .protocol import new_id, utc_now

_RFC3339_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
)
_MAX_IDENTIFIER_LENGTH = 512
_MAX_MEDIA_TYPE_LENGTH = 255
_MAX_METADATA_CONTAINER_DEPTH = 64
_MAX_METADATA_NODES = 10_000
_MAX_METADATA_KEY_LENGTH = 512
_MAX_METADATA_STRING_LENGTH = 65_536
_MAX_METADATA_INTEGER_BITS = 4_096
_MAX_SQLITE_INTEGER = 9_223_372_036_854_775_807
_SHA256_HEX_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ArtifactConflictError(RuntimeError):
    """Raised when an artifact identity or idempotency key changes meaning."""


class ArtifactConcurrencyError(RuntimeError):
    """Raised when a caller writes from a stale artifact head."""


class ArtifactIntegrityError(RuntimeError):
    """Raised when persisted metadata or content fails integrity verification."""


class ArtifactTooLargeError(ValueError):
    """Raised before persistence when content or metadata exceeds configured limits."""


def _required_text(value: str, name: str, *, max_length: int = _MAX_IDENTIFIER_LENGTH) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value.strip():
        raise ValueError(f"{name} is required")
    if len(value) > max_length:
        raise ValueError(f"{name} exceeds {max_length} characters")
    if "\x00" in value or "\r" in value or "\n" in value:
        raise ValueError(f"{name} contains a forbidden control character")
    return value


def _normalize_timestamp(value: str, name: str = "timestamp") -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be an RFC 3339 string")
    if not _RFC3339_PATTERN.fullmatch(value) or value.endswith("-00:00"):
        raise ValueError(f"{name} must be a strict RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an RFC 3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _validate_json_value(value: Any, path: str = "metadata") -> None:
    """Validate bounded JSON without recursing through caller-controlled input."""

    # Each entry is ``(value, path, parent-container depth, is-exit-marker)``.
    # Exit markers make cycle detection distinguish an active ancestor from a
    # harmless repeated reference that JSON will encode twice.
    stack: list[tuple[Any, str, int, bool]] = [(value, path, 0, False)]
    active_container_ids: set[int] = set()
    node_count = 0

    while stack:
        current, current_path, parent_depth, is_exit_marker = stack.pop()
        if is_exit_marker:
            active_container_ids.remove(id(current))
            continue

        node_count += 1
        if node_count > _MAX_METADATA_NODES:
            raise ArtifactTooLargeError(f"metadata exceeds {_MAX_METADATA_NODES} JSON value nodes")

        if current is None or isinstance(current, bool):
            continue
        if isinstance(current, str):
            if len(current) > _MAX_METADATA_STRING_LENGTH:
                raise ArtifactTooLargeError(
                    f"{current_path} exceeds {_MAX_METADATA_STRING_LENGTH} characters"
                )
            continue
        if isinstance(current, int):
            if current.bit_length() > _MAX_METADATA_INTEGER_BITS:
                raise ArtifactTooLargeError(
                    f"{current_path} exceeds {_MAX_METADATA_INTEGER_BITS} integer bits"
                )
            continue
        if isinstance(current, float):
            if not math.isfinite(current):
                raise ValueError(f"{current_path} contains a non-finite number")
            continue
        if type(current) not in (list, dict):
            raise TypeError(f"{current_path} contains unsupported type {type(current).__name__}")

        container_depth = parent_depth + 1
        if container_depth > _MAX_METADATA_CONTAINER_DEPTH:
            raise ArtifactTooLargeError(
                f"metadata exceeds {_MAX_METADATA_CONTAINER_DEPTH} nested JSON containers"
            )
        container_id = id(current)
        if container_id in active_container_ids:
            raise ValueError(f"{current_path} contains a reference cycle")
        active_container_ids.add(container_id)
        stack.append((current, current_path, parent_depth, True))

        if len(current) > _MAX_METADATA_NODES - node_count:
            raise ArtifactTooLargeError(f"metadata exceeds {_MAX_METADATA_NODES} JSON value nodes")
        if type(current) is list:
            try:
                for index in range(len(current) - 1, -1, -1):
                    stack.append(
                        (current[index], f"{current_path}[{index}]", container_depth, False)
                    )
            except IndexError as exc:
                raise ValueError("metadata changed while it was being validated") from exc
            continue

        try:
            items = tuple(current.items())
        except RuntimeError as exc:
            raise ValueError("metadata changed while it was being validated") from exc
        for key, item in reversed(items):
            if not isinstance(key, str):
                raise TypeError(f"{current_path} keys must be strings")
            if len(key) > _MAX_METADATA_KEY_LENGTH:
                raise ArtifactTooLargeError(
                    f"{current_path} key exceeds {_MAX_METADATA_KEY_LENGTH} characters"
                )
            stack.append((item, f"{current_path}.{key}", container_depth, False))


def _canonical_metadata(metadata: Mapping[str, Any]) -> Tuple[str, Mapping[str, Any]]:
    if type(metadata) is not dict:
        raise TypeError("metadata must be a plain dictionary")
    _validate_json_value(metadata)
    try:
        encoded = json.dumps(
            metadata,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        decoded = json.loads(encoded)
    except RecursionError as exc:  # Defensive against concurrent caller mutation.
        raise ArtifactTooLargeError("metadata nesting exceeds the structural limit") from exc
    if not isinstance(decoded, dict):  # pragma: no cover - guaranteed by the input guard.
        raise TypeError("metadata must encode a JSON object")
    return encoded, cast(Mapping[str, Any], decoded)


def _content_digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _persisted_integer(
    value: object,
    name: str,
    *,
    minimum: int = 0,
) -> int:
    if type(value) is not int:
        raise TypeError(f"persisted {name} must be an integer")
    if not minimum <= value <= _MAX_SQLITE_INTEGER:
        raise ValueError(f"persisted {name} is outside the supported range")
    return value


def _persisted_text(
    value: object,
    name: str,
    *,
    max_length: int = _MAX_IDENTIFIER_LENGTH,
) -> str:
    if type(value) is not str:
        raise TypeError(f"persisted {name} must be text")
    return _required_text(value, name, max_length=max_length)


def _request_digest(
    *,
    tenant_id: str,
    workspace_id: str,
    session_id: str,
    task_id: str,
    name: str,
    media_type: str,
    blob_digest: str,
    byte_size: int,
    metadata: Mapping[str, Any],
    created_by: str,
) -> str:
    payload = {
        "tenantId": tenant_id,
        "workspaceId": workspace_id,
        "sessionId": session_id,
        "taskId": task_id,
        "name": name,
        "mediaType": media_type,
        "blobDigest": blob_digest,
        "byteSize": byte_size,
        "metadata": dict(metadata),
        "createdBy": created_by,
    }
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ArtifactWrite:
    tenant_id: str
    workspace_id: str
    session_id: str
    task_id: str
    name: str
    content: bytes
    created_by: str
    idempotency_key: str
    media_type: str = "application/octet-stream"
    metadata: Mapping[str, Any] = field(default_factory=dict)
    artifact_id: str = field(default_factory=lambda: new_id("art"))

    def __post_init__(self) -> None:
        for name in (
            "tenant_id",
            "workspace_id",
            "session_id",
            "task_id",
            "name",
            "created_by",
            "idempotency_key",
            "artifact_id",
        ):
            _required_text(getattr(self, name), name)
        _required_text(self.media_type, "media_type", max_length=_MAX_MEDIA_TYPE_LENGTH)
        if not isinstance(self.content, bytes):
            raise TypeError("content must be immutable bytes")
        _canonical_metadata(self.metadata)


@dataclass(frozen=True)
class StoredArtifact:
    artifact_id: str
    tenant_id: str
    workspace_id: str
    session_id: str
    task_id: str
    name: str
    version: int
    parent_version: Optional[int]
    media_type: str
    digest: str
    byte_size: int
    metadata: Mapping[str, Any]
    created_by: str
    created_at: str
    idempotency_key: str
    request_digest: str
    content: bytes

    @property
    def uri(self) -> str:
        tenant = quote(self.tenant_id, safe="")
        workspace = quote(self.workspace_id, safe="")
        session = quote(self.session_id, safe="")
        name = quote(self.name, safe="")
        return f"artifact://{tenant}/{workspace}/{session}/{name}/v{self.version}"


class SQLiteArtifactStore:
    """Atomic content-addressed artifact store for a shared SQLite database."""

    def __init__(
        self,
        path: str = ":memory:",
        *,
        max_content_bytes: int = 16 * 1024 * 1024,
        max_metadata_bytes: int = 64 * 1024,
        busy_timeout_ms: int = 5_000,
        clock: Callable[[], str] = utc_now,
    ) -> None:
        for value, name in (
            (max_content_bytes, "max_content_bytes"),
            (max_metadata_bytes, "max_metadata_bytes"),
        ):
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"{name} must be an integer")
            if value <= 0:
                raise ValueError(f"{name} must be greater than zero")
        if not isinstance(busy_timeout_ms, int) or isinstance(busy_timeout_ms, bool):
            raise TypeError("busy_timeout_ms must be an integer")
        if busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms cannot be negative")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self.path = path
        self._max_content_bytes = max_content_bytes
        self._max_metadata_bytes = max_metadata_bytes
        self._clock = clock
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._connection = sqlite3.connect(
            path,
            check_same_thread=False,
            isolation_level=None,
            timeout=busy_timeout_ms / 1_000,
        )
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        try:
            with self._lock:
                self._connection.execute("PRAGMA foreign_keys=ON")
                self._connection.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
                if path != ":memory:":
                    self._enable_wal(busy_timeout_ms)
                    self._connection.execute("PRAGMA synchronous=FULL")
                # ``target_versions`` was added while domain migrations were split.
                # Keep a rolling-upgrade bridge so this component remains runnable
                # with the immediately preceding migration runner commit.
                migration_parameters = inspect.signature(apply_sqlite_migrations).parameters
                if "target_versions" in migration_parameters:
                    apply_sqlite_migrations(
                        self._connection,
                        target_versions=(1, 2),
                        clock=self._now,
                    )
                else:  # pragma: no cover - exercised with a compatibility stub.
                    apply_sqlite_migrations(self._connection, clock=self._now)
        except BaseException:
            self._connection.close()
            raise

    def __enter__(self) -> SQLiteArtifactStore:
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.close()

    def _now(self) -> str:
        return _normalize_timestamp(self._clock(), "clock")

    def _enable_wal(self, busy_timeout_ms: int) -> None:
        deadline = time.monotonic() + (busy_timeout_ms / 1_000)
        delay = 0.001
        while True:
            try:
                row = self._connection.execute("PRAGMA journal_mode=WAL").fetchone()
                if row is None or str(row[0]).lower() != "wal":
                    raise RuntimeError("SQLite refused WAL journal mode")
                return
            except sqlite3.OperationalError as exc:
                locked = "locked" in str(exc).lower() or "busy" in str(exc).lower()
                if not locked or time.monotonic() >= deadline:
                    raise
                time.sleep(delay)
                delay = min(0.05, delay * 2)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                try:
                    self._connection.execute("COMMIT")
                except BaseException:
                    if self._connection.in_transaction:
                        self._connection.execute("ROLLBACK")
                    raise

    def _prepare(self, spec: ArtifactWrite) -> Tuple[str, Mapping[str, Any], str, str]:
        if len(spec.content) > self._max_content_bytes:
            raise ArtifactTooLargeError(f"artifact content exceeds {self._max_content_bytes} bytes")
        metadata_json, metadata = _canonical_metadata(spec.metadata)
        if len(metadata_json.encode("utf-8")) > self._max_metadata_bytes:
            raise ArtifactTooLargeError(
                f"artifact metadata exceeds {self._max_metadata_bytes} bytes"
            )
        digest = _content_digest(spec.content)
        request_digest = _request_digest(
            tenant_id=spec.tenant_id,
            workspace_id=spec.workspace_id,
            session_id=spec.session_id,
            task_id=spec.task_id,
            name=spec.name,
            media_type=spec.media_type,
            blob_digest=digest,
            byte_size=len(spec.content),
            metadata=metadata,
            created_by=spec.created_by,
        )
        return metadata_json, metadata, digest, request_digest

    @staticmethod
    def _select_record(
        connection: sqlite3.Connection,
        tenant_id: str,
        workspace_id: str,
        artifact_id: str,
    ) -> Optional[sqlite3.Row]:
        return cast(
            Optional[sqlite3.Row],
            connection.execute(
                """
                SELECT version.*, blob.content, blob.byte_size AS blob_byte_size
                FROM artifact_versions AS version
                JOIN artifact_blobs AS blob ON blob.digest = version.blob_digest
                WHERE version.tenant_id = ? AND version.workspace_id = ?
                  AND version.artifact_id = ?
                """,
                (tenant_id, workspace_id, artifact_id),
            ).fetchone(),
        )

    @staticmethod
    def _row_to_artifact(row: sqlite3.Row) -> StoredArtifact:
        try:
            content_value = row["content"]
            if type(content_value) is not bytes:
                raise TypeError("persisted artifact content must be a SQLite BLOB")
            content = content_value
            metadata_json = row["metadata_json"]
            if type(metadata_json) is not str:
                raise TypeError("persisted artifact metadata_json must be text")
            metadata_value = json.loads(metadata_json)
        except (
            IndexError,
            KeyError,
            TypeError,
            ValueError,
            RecursionError,
            json.JSONDecodeError,
        ) as exc:
            raise ArtifactIntegrityError("artifact row contains malformed data") from exc
        if not isinstance(metadata_value, dict):
            raise ArtifactIntegrityError("artifact metadata is not a JSON object")
        try:
            _validate_json_value(metadata_value)
            artifact_id = _persisted_text(row["artifact_id"], "artifact_id")
            tenant_id = _persisted_text(row["tenant_id"], "tenant_id")
            workspace_id = _persisted_text(row["workspace_id"], "workspace_id")
            session_id = _persisted_text(row["session_id"], "session_id")
            task_id = _persisted_text(row["task_id"], "task_id")
            name = _persisted_text(row["name"], "name")
            media_type = _persisted_text(
                row["media_type"],
                "media_type",
                max_length=_MAX_MEDIA_TYPE_LENGTH,
            )
            created_by = _persisted_text(row["created_by"], "created_by")
            idempotency_key = _persisted_text(row["idempotency_key"], "idempotency_key")
            blob_digest = _persisted_text(
                row["blob_digest"],
                "blob_digest",
                max_length=71,
            )
            if not blob_digest.startswith("sha256:") or not _SHA256_HEX_PATTERN.fullmatch(
                blob_digest[7:]
            ):
                raise ValueError("persisted blob_digest is not canonical SHA-256")
            request_digest = _persisted_text(
                row["request_digest"],
                "request_digest",
                max_length=64,
            )
            if not _SHA256_HEX_PATTERN.fullmatch(request_digest):
                raise ValueError("persisted request_digest is not canonical SHA-256")
            expected_content_digest = _content_digest(content)
            byte_size = _persisted_integer(row["byte_size"], "byte_size")
            blob_byte_size = _persisted_integer(row["blob_byte_size"], "blob_byte_size")
            version = _persisted_integer(row["version"], "version", minimum=1)
            parent_version = (
                _persisted_integer(row["parent_version"], "parent_version", minimum=1)
                if row["parent_version"] is not None
                else None
            )
            raw_created_at = _persisted_text(
                row["created_at"],
                "created_at",
                max_length=32,
            )
            created_at = _normalize_timestamp(raw_created_at, "created_at")
            if raw_created_at != created_at:
                raise ValueError("persisted created_at is not canonical UTC")
            expected_request_digest = _request_digest(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                session_id=session_id,
                task_id=task_id,
                name=name,
                media_type=media_type,
                blob_digest=blob_digest,
                byte_size=byte_size,
                metadata=cast(Mapping[str, Any], metadata_value),
                created_by=created_by,
            )
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise ArtifactIntegrityError("artifact row violates its data contract") from exc
        if expected_content_digest != blob_digest:
            raise ArtifactIntegrityError("artifact blob digest verification failed")
        if len(content) != byte_size or len(content) != blob_byte_size:
            raise ArtifactIntegrityError("artifact byte size verification failed")
        if expected_request_digest != request_digest:
            raise ArtifactIntegrityError("artifact request fingerprint verification failed")
        if version <= 0 or parent_version != (version - 1 if version > 1 else None):
            raise ArtifactIntegrityError("artifact version lineage is invalid")
        return StoredArtifact(
            artifact_id=artifact_id,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            session_id=session_id,
            task_id=task_id,
            name=name,
            version=version,
            parent_version=parent_version,
            media_type=media_type,
            digest=blob_digest,
            byte_size=byte_size,
            metadata=cast(Mapping[str, Any], metadata_value),
            created_by=created_by,
            created_at=created_at,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
            content=content,
        )

    def write(
        self,
        spec: ArtifactWrite,
        *,
        expected_head_version: Optional[int] = None,
    ) -> StoredArtifact:
        """Commit content and one immutable version, or return an identical retry."""

        if not isinstance(spec, ArtifactWrite):
            raise TypeError("spec must be an ArtifactWrite")
        if expected_head_version is not None:
            if not isinstance(expected_head_version, int) or isinstance(
                expected_head_version, bool
            ):
                raise TypeError("expected_head_version must be an integer")
            if expected_head_version < 0:
                raise ValueError("expected_head_version cannot be negative")
        metadata_json, _metadata, digest, request_digest = self._prepare(spec)
        with self._transaction() as connection:
            existing_rows = connection.execute(
                """
                SELECT * FROM artifact_versions
                WHERE (
                    tenant_id = ? AND workspace_id = ? AND artifact_id = ?
                ) OR (
                    tenant_id = ? AND workspace_id = ? AND idempotency_key = ?
                )
                """,
                (
                    spec.tenant_id,
                    spec.workspace_id,
                    spec.artifact_id,
                    spec.tenant_id,
                    spec.workspace_id,
                    spec.idempotency_key,
                ),
            ).fetchall()
            if existing_rows:
                row = existing_rows[0]
                if (
                    len(existing_rows) != 1
                    or row["idempotency_key"] != spec.idempotency_key
                    or row["request_digest"] != request_digest
                ):
                    raise ArtifactConflictError(
                        "artifact identity or idempotency key is bound to different content"
                    )
                existing = self._select_record(
                    connection,
                    spec.tenant_id,
                    spec.workspace_id,
                    row["artifact_id"],
                )
                if existing is None:  # pragma: no cover - protected by the transaction.
                    raise ArtifactIntegrityError("idempotent artifact record has no blob")
                return self._row_to_artifact(existing)

            head_row = connection.execute(
                """
                SELECT COALESCE(MAX(version), 0) AS version
                FROM artifact_versions
                WHERE tenant_id = ? AND workspace_id = ?
                  AND session_id = ? AND name = ?
                """,
                (spec.tenant_id, spec.workspace_id, spec.session_id, spec.name),
            ).fetchone()
            current_version = int(head_row["version"])
            if expected_head_version is not None and current_version != expected_head_version:
                raise ArtifactConcurrencyError(
                    f"artifact head changed: expected {expected_head_version}, "
                    f"found {current_version}"
                )
            version = current_version + 1
            parent_version = current_version if current_version > 0 else None
            now = self._now()
            connection.execute(
                """
                INSERT INTO artifact_blobs(digest, content, byte_size, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(digest) DO NOTHING
                """,
                (digest, spec.content, len(spec.content), now),
            )
            blob_row = connection.execute(
                "SELECT content, byte_size FROM artifact_blobs WHERE digest = ?",
                (digest,),
            ).fetchone()
            if (
                blob_row is None
                or bytes(blob_row["content"]) != spec.content
                or int(blob_row["byte_size"]) != len(spec.content)
            ):
                raise ArtifactIntegrityError("existing content-addressed blob is corrupted")
            connection.execute(
                """
                INSERT INTO artifact_versions(
                    artifact_id, tenant_id, workspace_id, session_id, task_id,
                    name, version, parent_version, media_type, blob_digest,
                    byte_size, metadata_json, created_by, created_at,
                    idempotency_key, request_digest
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    spec.artifact_id,
                    spec.tenant_id,
                    spec.workspace_id,
                    spec.session_id,
                    spec.task_id,
                    spec.name,
                    version,
                    parent_version,
                    spec.media_type,
                    digest,
                    len(spec.content),
                    metadata_json,
                    spec.created_by,
                    now,
                    spec.idempotency_key,
                    request_digest,
                ),
            )
            stored = self._select_record(
                connection, spec.tenant_id, spec.workspace_id, spec.artifact_id
            )
            if stored is None:  # pragma: no cover - protected by the transaction.
                raise RuntimeError("stored artifact disappeared")
            return self._row_to_artifact(stored)

    def get(
        self,
        tenant_id: str,
        workspace_id: str,
        artifact_id: str,
    ) -> Optional[StoredArtifact]:
        for value, name in (
            (tenant_id, "tenant_id"),
            (workspace_id, "workspace_id"),
            (artifact_id, "artifact_id"),
        ):
            _required_text(value, name)
        with self._lock:
            row = self._select_record(self._connection, tenant_id, workspace_id, artifact_id)
            return self._row_to_artifact(row) if row is not None else None

    def head(
        self,
        tenant_id: str,
        workspace_id: str,
        session_id: str,
        name: str,
    ) -> Optional[StoredArtifact]:
        for value, field_name in (
            (tenant_id, "tenant_id"),
            (workspace_id, "workspace_id"),
            (session_id, "session_id"),
            (name, "name"),
        ):
            _required_text(value, field_name)
        with self._lock:
            row = cast(
                Optional[sqlite3.Row],
                self._connection.execute(
                    """
                    SELECT version.*, blob.content, blob.byte_size AS blob_byte_size
                    FROM artifact_versions AS version
                    JOIN artifact_blobs AS blob ON blob.digest = version.blob_digest
                    WHERE version.tenant_id = ? AND version.workspace_id = ?
                      AND version.session_id = ? AND version.name = ?
                    ORDER BY version.version DESC LIMIT 1
                    """,
                    (tenant_id, workspace_id, session_id, name),
                ).fetchone(),
            )
            return self._row_to_artifact(row) if row is not None else None

    def history(
        self,
        tenant_id: str,
        workspace_id: str,
        session_id: str,
        name: str,
        *,
        after_version: int = 0,
        limit: int = 100,
    ) -> Tuple[StoredArtifact, ...]:
        for value, field_name in (
            (tenant_id, "tenant_id"),
            (workspace_id, "workspace_id"),
            (session_id, "session_id"),
            (name, "name"),
        ):
            _required_text(value, field_name)
        if not isinstance(after_version, int) or isinstance(after_version, bool):
            raise TypeError("after_version must be an integer")
        if after_version < 0:
            raise ValueError("after_version cannot be negative")
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise TypeError("limit must be an integer")
        if not 1 <= limit <= 1_000:
            raise ValueError("limit must be between 1 and 1000")
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT version.*, blob.content, blob.byte_size AS blob_byte_size
                FROM artifact_versions AS version
                JOIN artifact_blobs AS blob ON blob.digest = version.blob_digest
                WHERE version.tenant_id = ? AND version.workspace_id = ?
                  AND version.session_id = ? AND version.name = ?
                  AND version.version > ?
                ORDER BY version.version ASC LIMIT ?
                """,
                (tenant_id, workspace_id, session_id, name, after_version, limit),
            ).fetchall()
            return tuple(self._row_to_artifact(row) for row in rows)

    def verify_scope(self, tenant_id: str, workspace_id: str) -> int:
        """Verify every artifact visible to one scope and return the checked count."""

        _required_text(tenant_id, "tenant_id")
        _required_text(workspace_id, "workspace_id")
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT version.*, blob.content, blob.byte_size AS blob_byte_size
                FROM artifact_versions AS version
                JOIN artifact_blobs AS blob ON blob.digest = version.blob_digest
                WHERE version.tenant_id = ? AND version.workspace_id = ?
                ORDER BY version.session_id, version.name, version.version
                """,
                (tenant_id, workspace_id),
            ).fetchall()
            for row in rows:
                self._row_to_artifact(row)
            return len(rows)

    def schema_version(self) -> int:
        with self._lock:
            return int(current_schema_version(self._connection))

    def close(self) -> None:
        with self._lock:
            self._connection.close()


__all__ = [
    "ArtifactConcurrencyError",
    "ArtifactConflictError",
    "ArtifactIntegrityError",
    "ArtifactTooLargeError",
    "ArtifactWrite",
    "SQLiteArtifactStore",
    "StoredArtifact",
]
