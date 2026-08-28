"""Private immutable inputs for future owner-transaction result Artifact writes."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import islice
from typing import NoReturn

from ._artifact_codec import (
    MAX_ARTIFACT_METADATA_BYTES,
    artifact_request_digest_v1,
    decode_canonical_artifact_metadata_v1,
)
from ._sqlite_schema_codec import backup_schema_ddl_sha256
from .invocation_results import (
    ScopedInvocationResultArtifactCandidateV2,
    ScopedInvocationResultArtifactV2,
)

_MAX_RESULT_ARTIFACTS = 256
_MAX_RESULT_ARTIFACT_CONTENT_BYTES = 64 * 1024 * 1024
_MAX_RESULT_ARTIFACT_METADATA_BYTES = 1_048_576
_RESULT_ARTIFACT_HISTORY_FETCH_BATCH_SIZE = 64
_RESULT_ARTIFACT_TRANSACTION_TOKEN = object()
_MISSING_RESULT_ARTIFACT_SCHEMA_DIGEST = object()

_HEAD_AGGREGATE_COLUMNS = (
    "row_count",
    "minimum_version",
    "maximum_version",
    "invalid_lineage_count",
)
_SCHEMA_VERSION_COLUMNS = ("schema_version",)
_SCHEMA_OBJECT_COLUMNS = (
    "object_type",
    "object_name",
    "table_name",
    "root_page",
    "ddl_sql",
)
_TEMP_SCHEMA_OBJECT_COLUMNS = ("object_type", "object_name", "table_name")
_RESULT_ARTIFACT_TABLE_NAMES = frozenset({"artifact_blobs", "artifact_versions"})
_RESULT_ARTIFACT_SCHEMA_DDL_SHA256 = {
    ("index", "idx_artifact_versions_digest", "artifact_versions"): (
        "6f96c49420ce234a4f3f93a757647613040e9432430e1e0aa0152f47ef34a6a7"
    ),
    ("index", "idx_artifact_versions_head", "artifact_versions"): (
        "cb903e3efc219003501022cf9e03bc7527c09d0040878758f3a9de5caff78995"
    ),
    ("index", "idx_artifact_versions_task", "artifact_versions"): (
        "56b79bb782d84f96b26087766b823a007dd04b6b2c0c521330fdc3e4aef82efb"
    ),
    ("index", "sqlite_autoindex_artifact_blobs_1", "artifact_blobs"): None,
    ("index", "sqlite_autoindex_artifact_versions_1", "artifact_versions"): None,
    ("index", "sqlite_autoindex_artifact_versions_2", "artifact_versions"): None,
    ("index", "sqlite_autoindex_artifact_versions_3", "artifact_versions"): None,
    ("table", "artifact_blobs", "artifact_blobs"): (
        "2c32324870b0be6b8f5ea524575912ff0eb08be9be10cc3cae28e96069cc35e9"
    ),
    ("table", "artifact_versions", "artifact_versions"): (
        "5fdaf59eed765b0f5b9ebddf1140e78d79a87803cf4d2a847a9dd596e447f9d6"
    ),
}
_BLOB_COLUMNS = (
    "digest",
    "content",
    "byte_size",
    "created_at",
    "digest_storage",
    "content_storage",
    "byte_size_storage",
    "created_at_storage",
    "content_length",
)
_VERSION_COLUMNS = (
    "artifact_id",
    "tenant_id",
    "workspace_id",
    "session_id",
    "task_id",
    "name",
    "version",
    "parent_version",
    "media_type",
    "blob_digest",
    "byte_size",
    "metadata_json",
    "created_by",
    "created_at",
    "idempotency_key",
    "request_digest",
)
_HISTORY_PREFLIGHT_COLUMNS = (
    "row_id",
    "version",
    "parent_version",
    "byte_size",
    "version_storage",
    "parent_version_storage",
    "byte_size_storage",
    "artifact_id_storage",
    "artifact_id_bytes",
    "tenant_id_storage",
    "tenant_id_bytes",
    "workspace_id_storage",
    "workspace_id_bytes",
    "session_id_storage",
    "session_id_bytes",
    "task_id_storage",
    "task_id_bytes",
    "name_storage",
    "name_bytes",
    "media_type_storage",
    "media_type_bytes",
    "blob_digest_storage",
    "blob_digest_bytes",
    "metadata_json_storage",
    "metadata_json_bytes",
    "created_by_storage",
    "created_by_bytes",
    "created_at_storage",
    "created_at_bytes",
    "idempotency_key_storage",
    "idempotency_key_bytes",
    "request_digest_storage",
    "request_digest_bytes",
)
_HISTORY_TEXT_BYTE_BOUNDS = (
    ("artifact_id", 1, 4_096),
    ("tenant_id", 1, 4_096),
    ("workspace_id", 1, 4_096),
    ("session_id", 1, 4_096),
    ("task_id", 1, 4_096),
    ("name", 1, 4_096),
    ("media_type", 1, 255),
    ("blob_digest", 71, 71),
    ("metadata_json", 1, MAX_ARTIFACT_METADATA_BYTES),
    ("created_by", 1, 4_096),
    ("created_at", 27, 27),
    ("idempotency_key", 1, 4_096),
    ("request_digest", 64, 64),
)
_CHANGE_COUNT_COLUMNS = ("changed",)


class _ResultArtifactConflictError(RuntimeError):
    """An Artifact identity or idempotency key is already reserved."""


class _ResultArtifactConcurrencyError(RuntimeError):
    """An exact candidate head no longer matches durable history."""


class _ResultArtifactIntegrityError(RuntimeError):
    """Artifact storage or DML effects violate the owner transaction contract."""


class _ResultArtifactTransactionError(RuntimeError):
    """The owner transaction was confirmed rolled back."""


class _ResultArtifactCommitAmbiguityError(RuntimeError):
    """The owner transaction may have committed and requires reopen/reconcile."""


class _ResultArtifactTransactionHandle:
    """One non-transferable, exit-invalidated EventStore transaction capability."""

    __slots__ = (
        "__active",
        "__connection",
        "__generation",
        "__process_owner",
        "__store",
    )

    def __init__(
        self,
        *,
        store: object,
        connection: sqlite3.Connection,
        process_owner: object,
        generation: int,
        token: object,
    ) -> None:
        if type(self) is not _ResultArtifactTransactionHandle:
            raise TypeError("result Artifact transaction handle must use the exact private class")
        if token is not _RESULT_ARTIFACT_TRANSACTION_TOKEN:
            raise TypeError("result Artifact transaction handle constructor is private")
        if type(connection) is not sqlite3.Connection:
            raise TypeError(
                "result Artifact transaction handle requires an exact SQLite connection"
            )
        if type(generation) is not int or generation <= 0:
            raise ValueError("result Artifact transaction generation is invalid")
        self.__store = store
        self.__connection: sqlite3.Connection | None = connection
        self.__process_owner = process_owner
        self.__generation = generation
        self.__active = True

    def _validated_connection(
        self,
        *,
        store: object,
        process_owner: object,
        generation: int,
        token: object,
    ) -> sqlite3.Connection:
        if type(self) is not _ResultArtifactTransactionHandle:
            raise TypeError("result Artifact transaction handle must be exact")
        if token is not _RESULT_ARTIFACT_TRANSACTION_TOKEN:
            raise TypeError("result Artifact transaction validation is private")
        if type(self.__active) is not bool or not self.__active:
            raise RuntimeError("result Artifact transaction handle is no longer active")
        if self.__store is not store or self.__process_owner is not process_owner:
            raise RuntimeError("result Artifact transaction handle has a foreign owner")
        if type(generation) is not int or self.__generation != generation:
            raise RuntimeError("result Artifact transaction handle generation is not active")
        connection = self.__connection
        if type(connection) is not sqlite3.Connection:
            raise RuntimeError("result Artifact transaction handle connection changed")
        transaction_open = connection.in_transaction
        if type(transaction_open) is not bool or not transaction_open:
            raise RuntimeError("result Artifact transaction is not open")
        return connection

    def _invalidate(self, *, token: object) -> None:
        if token is not _RESULT_ARTIFACT_TRANSACTION_TOKEN:
            raise TypeError("result Artifact transaction invalidation is private")
        self.__active = False
        self.__store = None
        self.__connection = None
        self.__process_owner = None

    def __copy__(self) -> NoReturn:
        raise TypeError("result Artifact transaction handles cannot be copied")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("result Artifact transaction handles cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("result Artifact transaction handles cannot be serialized")


@dataclass(frozen=True)
class _PreparedResultArtifact:
    """One caller-detached candidate snapshot; content never enters its repr."""

    ordinal: int
    tenant_id: str
    workspace_id: str
    session_id: str
    task_id: str
    artifact_id: str
    name: str
    media_type: str
    content: bytes = field(repr=False)
    metadata_canonical_bytes: bytes = field(repr=False)
    metadata_json: str = field(repr=False)
    created_by: str
    idempotency_key: str
    expected_head_version: int
    descriptor: ScopedInvocationResultArtifactV2
    candidate_sha256: str

    def verify(self) -> None:
        if type(self) is not _PreparedResultArtifact:
            raise TypeError("prepared result Artifact must use the exact private class")
        if type(self.ordinal) is not int or not 0 <= self.ordinal < _MAX_RESULT_ARTIFACTS:
            raise ValueError("prepared result Artifact ordinal is invalid")
        if type(self.content) is not bytes or type(self.metadata_canonical_bytes) is not bytes:
            raise TypeError("prepared result Artifact bytes are not immutable")
        if type(self.metadata_json) is not str:
            raise TypeError("prepared result Artifact metadata JSON is not text")
        try:
            if self.metadata_canonical_bytes.decode("utf-8") != self.metadata_json:
                raise ValueError("prepared result Artifact metadata bytes changed")
        except UnicodeError as error:
            raise ValueError("prepared result Artifact metadata is not UTF-8") from error
        candidate = ScopedInvocationResultArtifactCandidateV2(
            tenant_id=self.tenant_id,
            workspace_id=self.workspace_id,
            session_id=self.session_id,
            task_id=self.task_id,
            artifact_id=self.artifact_id,
            name=self.name,
            media_type=self.media_type,
            content=self.content,
            metadata_canonical_bytes=self.metadata_canonical_bytes,
            created_by=self.created_by,
            idempotency_key=self.idempotency_key,
            expected_head_version=self.expected_head_version,
        )
        if type(self.descriptor) is not ScopedInvocationResultArtifactV2:
            raise TypeError("prepared result Artifact descriptor is not exact")
        if candidate.to_descriptor() != self.descriptor:
            raise ValueError("prepared result Artifact descriptor changed")
        if candidate.canonical_digest() != self.candidate_sha256:
            raise ValueError("prepared result Artifact candidate digest changed")


@dataclass(frozen=True)
class _PreparedResultArtifactBatch:
    """One exact ordered batch detached from caller-owned candidate objects."""

    items: tuple[_PreparedResultArtifact, ...]
    total_content_bytes: int
    total_metadata_bytes: int

    def verify(self) -> None:
        if type(self) is not _PreparedResultArtifactBatch:
            raise TypeError("prepared result Artifact batch must use the exact private class")
        if type(self.items) is not tuple:
            raise TypeError("prepared result Artifact batch items must be an exact tuple")
        if len(self.items) > _MAX_RESULT_ARTIFACTS:
            raise ValueError("prepared result Artifact batch exceeds its item limit")
        for ordinal, item in enumerate(self.items):
            if type(item) is not _PreparedResultArtifact:
                raise TypeError("prepared result Artifact batch contains a non-exact item")
            item.verify()
            if item.ordinal != ordinal:
                raise ValueError("prepared result Artifact batch order changed")
        content_bytes = sum(len(item.content) for item in self.items)
        metadata_bytes = sum(len(item.metadata_canonical_bytes) for item in self.items)
        if type(self.total_content_bytes) is not int or self.total_content_bytes != content_bytes:
            raise ValueError("prepared result Artifact content total changed")
        if (
            type(self.total_metadata_bytes) is not int
            or self.total_metadata_bytes != metadata_bytes
        ):
            raise ValueError("prepared result Artifact metadata total changed")
        if content_bytes > _MAX_RESULT_ARTIFACT_CONTENT_BYTES:
            raise ValueError("prepared result Artifact content exceeds its batch limit")
        if metadata_bytes > _MAX_RESULT_ARTIFACT_METADATA_BYTES:
            raise ValueError("prepared result Artifact metadata exceeds its batch limit")
        _validate_batch_identities(self.items)


def _validate_batch_identities(items: tuple[_PreparedResultArtifact, ...]) -> None:
    if not items:
        return
    expected_scope = (
        items[0].tenant_id,
        items[0].workspace_id,
        items[0].session_id,
        items[0].task_id,
    )
    artifact_ids: set[str] = set()
    idempotency_coordinates: set[tuple[str, str, str]] = set()
    head_coordinates: set[tuple[str, str, str, str]] = set()
    blob_contents: dict[str, bytes] = {}
    for item in items:
        scope = (item.tenant_id, item.workspace_id, item.session_id, item.task_id)
        if scope != expected_scope:
            raise ValueError("prepared result Artifact batch scope is not exact")
        if item.artifact_id in artifact_ids:
            raise ValueError("prepared result Artifact IDs must be unique")
        artifact_ids.add(item.artifact_id)
        idempotency_coordinate = (item.tenant_id, item.workspace_id, item.idempotency_key)
        if idempotency_coordinate in idempotency_coordinates:
            raise ValueError("prepared result Artifact idempotency keys must be unique")
        idempotency_coordinates.add(idempotency_coordinate)
        head_coordinate = (item.tenant_id, item.workspace_id, item.session_id, item.name)
        if head_coordinate in head_coordinates:
            raise ValueError("prepared result Artifact head coordinates must be unique")
        head_coordinates.add(head_coordinate)
        blob_digest = item.descriptor.blob_digest
        previous_content = blob_contents.get(blob_digest)
        if previous_content is not None and previous_content != item.content:
            raise ValueError("prepared result Artifact blob digest has conflicting content")
        blob_contents[blob_digest] = item.content


def _prepare_result_artifact_batch(
    candidates: Iterable[ScopedInvocationResultArtifactCandidateV2],
) -> _PreparedResultArtifactBatch:
    try:
        iterator = iter(candidates)
    except TypeError as error:
        raise TypeError("result Artifact candidates must be iterable") from error
    snapshot = tuple(islice(iterator, _MAX_RESULT_ARTIFACTS + 1))
    if len(snapshot) > _MAX_RESULT_ARTIFACTS:
        raise ValueError("result Artifact candidates exceed the batch item limit")
    prepared: list[_PreparedResultArtifact] = []
    for ordinal, candidate in enumerate(snapshot):
        if type(candidate) is not ScopedInvocationResultArtifactCandidateV2:
            raise TypeError("result Artifact candidates must use the exact schema-2 class")
        ScopedInvocationResultArtifactCandidateV2.__post_init__(candidate)
        metadata_bytes = candidate.metadata_canonical_bytes
        item = _PreparedResultArtifact(
            ordinal=ordinal,
            tenant_id=candidate.tenant_id,
            workspace_id=candidate.workspace_id,
            session_id=candidate.session_id,
            task_id=candidate.task_id,
            artifact_id=candidate.artifact_id,
            name=candidate.name,
            media_type=candidate.media_type,
            content=candidate.content,
            metadata_canonical_bytes=metadata_bytes,
            metadata_json=metadata_bytes.decode("utf-8"),
            created_by=candidate.created_by,
            idempotency_key=candidate.idempotency_key,
            expected_head_version=candidate.expected_head_version,
            descriptor=candidate.to_descriptor(),
            candidate_sha256=candidate.canonical_digest(),
        )
        item.verify()
        prepared.append(item)
    batch = _PreparedResultArtifactBatch(
        items=tuple(prepared),
        total_content_bytes=sum(len(item.content) for item in prepared),
        total_metadata_bytes=sum(len(item.metadata_canonical_bytes) for item in prepared),
    )
    batch.verify()
    return batch


def _require_exact_connection_codec(connection: sqlite3.Connection) -> None:
    if type(connection) is not sqlite3.Connection:
        raise _ResultArtifactIntegrityError("result Artifact connection is not exact")
    if connection.row_factory is not sqlite3.Row or connection.text_factory is not str:
        raise _ResultArtifactIntegrityError("result Artifact SQLite codecs are not exact")


def _require_exact_row(row: object, columns: Sequence[str]) -> sqlite3.Row:
    if type(row) is not sqlite3.Row:
        raise _ResultArtifactIntegrityError("result Artifact row must be an exact sqlite3.Row")
    try:
        keys = row.keys()
    except (AttributeError, TypeError, ValueError) as error:
        raise _ResultArtifactIntegrityError("result Artifact row shape is invalid") from error
    if tuple(keys) != tuple(columns):
        raise _ResultArtifactIntegrityError("result Artifact row columns are not exact")
    return row


def _exact_row_value(row: sqlite3.Row, name: str) -> object:
    try:
        return row[name]
    except (IndexError, KeyError) as error:
        raise _ResultArtifactIntegrityError("result Artifact row shape is invalid") from error


def _canonical_result_artifact_timestamp(value: object, *, persisted: bool) -> str:
    if type(value) is not str or not value:
        raise ValueError("result Artifact transaction clock returned an invalid timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(
            "result Artifact transaction clock returned an invalid timestamp"
        ) from error
    if parsed.tzinfo is None:
        raise ValueError("result Artifact transaction clock timestamp has no timezone")
    canonical = (
        parsed.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    if len(canonical.encode("ascii")) != 27:
        raise ValueError("result Artifact transaction timestamp is not canonical")
    if persisted and value != canonical:
        raise _ResultArtifactIntegrityError(
            "persisted result Artifact timestamp is not canonical UTC microseconds"
        )
    return canonical


def _run_process_guard(process_guard: Callable[[], None]) -> None:
    process_guard()


def _guarded_fetchone(
    connection: sqlite3.Connection,
    process_guard: Callable[[], None],
    sql: str,
    parameters: tuple[object, ...] = (),
) -> object:
    _run_process_guard(process_guard)
    _require_exact_connection_codec(connection)
    cursor = connection.execute(sql, parameters)
    _run_process_guard(process_guard)
    _require_exact_connection_codec(connection)
    row = cursor.fetchone()
    _run_process_guard(process_guard)
    _require_exact_connection_codec(connection)
    return row


def _guarded_fetchall(
    connection: sqlite3.Connection,
    process_guard: Callable[[], None],
    sql: str,
    parameters: tuple[object, ...] = (),
) -> list[object]:
    _run_process_guard(process_guard)
    _require_exact_connection_codec(connection)
    cursor = connection.execute(sql, parameters)
    _run_process_guard(process_guard)
    _require_exact_connection_codec(connection)
    rows = cursor.fetchall()
    _run_process_guard(process_guard)
    _require_exact_connection_codec(connection)
    if type(rows) is not list:
        raise _ResultArtifactIntegrityError("result Artifact row collection is not exact")
    return rows


def _guarded_fetchmany(
    connection: sqlite3.Connection,
    cursor: sqlite3.Cursor,
    process_guard: Callable[[], None],
) -> list[object]:
    _run_process_guard(process_guard)
    _require_exact_connection_codec(connection)
    if type(cursor) is not sqlite3.Cursor:
        raise _ResultArtifactIntegrityError("result Artifact cursor must be exact")
    batch_size = _RESULT_ARTIFACT_HISTORY_FETCH_BATCH_SIZE
    if type(batch_size) is not int or not 1 <= batch_size <= 64:
        raise _ResultArtifactIntegrityError(
            "result Artifact history batch size is invalid"
        )
    rows = cursor.fetchmany(batch_size)
    _run_process_guard(process_guard)
    _require_exact_connection_codec(connection)
    if type(rows) is not list or len(rows) > batch_size:
        raise _ResultArtifactIntegrityError(
            "result Artifact history batch collection is invalid"
        )
    return rows


def _guarded_execute(
    connection: sqlite3.Connection,
    process_guard: Callable[[], None],
    sql: str,
    parameters: tuple[object, ...] = (),
) -> None:
    _run_process_guard(process_guard)
    _require_exact_connection_codec(connection)
    connection.execute(sql, parameters)
    _run_process_guard(process_guard)
    _require_exact_connection_codec(connection)


@contextmanager
def _result_artifact_process_progress_fence(
    connection: sqlite3.Connection,
    process_guard: Callable[[], None],
) -> Iterator[Callable[[], None]]:
    """Own SQLite callbacks after clock sampling and fence every later VM instruction."""

    callbacks_claimed = False

    def progress() -> int:
        try:
            process_guard()
        except BaseException:
            return 1
        return 0

    def refresh() -> None:
        _run_process_guard(process_guard)
        _require_exact_connection_codec(connection)
        connection.set_progress_handler(progress, 1)
        _run_process_guard(process_guard)

    def authorize(
        action: int,
        first: object,
        second: object,
        database: object,
        source: object,
    ) -> int:
        try:
            process_guard()
        except BaseException:
            return sqlite3.SQLITE_DENY
        if source is not None:
            return sqlite3.SQLITE_DENY
        if action in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION}:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            if database == "main" and first in {
                "sqlite_master",
                *_RESULT_ARTIFACT_TABLE_NAMES,
            }:
                return sqlite3.SQLITE_OK
            if database == "temp" and first == "sqlite_temp_master":
                return sqlite3.SQLITE_OK
        if (
            action == sqlite3.SQLITE_INSERT
            and database == "main"
            and first in _RESULT_ARTIFACT_TABLE_NAMES
        ):
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_PRAGMA and first == "schema_version" and second is None:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    def claim_callbacks() -> None:
        nonlocal callbacks_claimed
        _run_process_guard(process_guard)
        _require_exact_connection_codec(connection)
        # A constructor clock may retain the store through a closure. It cannot be
        # allowed to leave a trace/authorizer callback that changes the process fence
        # or introduces trigger work between topology verification and Artifact DML.
        connection.set_trace_callback(None)
        connection.set_authorizer(authorize)
        callbacks_claimed = True
        refresh()

    refresh()
    try:
        yield claim_callbacks
    finally:
        # A mismatch child must not mutate the inherited SQLite wrapper even for cleanup.
        _run_process_guard(process_guard)
        if callbacks_claimed:
            connection.set_authorizer(None)
            connection.set_trace_callback(None)
        connection.set_progress_handler(None, 0)
        _run_process_guard(process_guard)


def _result_artifact_schema_snapshot(
    connection: sqlite3.Connection,
    process_guard: Callable[[], None],
) -> tuple[int, tuple[tuple[str, str, str, int, str | None], ...]]:
    raw_temp_object = _guarded_fetchone(
        connection,
        process_guard,
        """
        SELECT
            type AS object_type,
            name AS object_name,
            tbl_name AS table_name
        FROM temp.sqlite_temp_schema
        WHERE
            name IN ('artifact_blobs', 'artifact_versions')
            OR tbl_name IN ('artifact_blobs', 'artifact_versions')
        LIMIT 1
        """,
    )
    if raw_temp_object is not None:
        _require_exact_row(raw_temp_object, _TEMP_SCHEMA_OBJECT_COLUMNS)
        raise _ResultArtifactIntegrityError(
            "result Artifact tables are shadowed by an unexpected TEMP schema object"
        )

    raw_schema_version = _guarded_fetchone(
        connection,
        process_guard,
        "PRAGMA main.schema_version",
    )
    if raw_schema_version is None:
        raise _ResultArtifactIntegrityError("result Artifact schema version is missing")
    schema_version_row = _require_exact_row(
        raw_schema_version,
        _SCHEMA_VERSION_COLUMNS,
    )
    schema_version = _exact_row_integer(schema_version_row, "schema_version")

    raw_objects = _guarded_fetchall(
        connection,
        process_guard,
        """
        SELECT
            type AS object_type,
            name AS object_name,
            tbl_name AS table_name,
            rootpage AS root_page,
            sql AS ddl_sql
        FROM main.sqlite_schema
        WHERE
            name IN (
                'artifact_blobs',
                'artifact_versions',
                'idx_artifact_versions_digest',
                'idx_artifact_versions_head',
                'idx_artifact_versions_task',
                'sqlite_autoindex_artifact_blobs_1',
                'sqlite_autoindex_artifact_versions_1',
                'sqlite_autoindex_artifact_versions_2',
                'sqlite_autoindex_artifact_versions_3'
            )
            OR tbl_name IN ('artifact_blobs', 'artifact_versions')
        ORDER BY type, name
        """,
    )
    if len(raw_objects) != len(_RESULT_ARTIFACT_SCHEMA_DDL_SHA256):
        raise _ResultArtifactIntegrityError("result Artifact main schema topology changed")
    observed: list[tuple[str, str, str, int, str | None]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw_object in raw_objects:
        row = _require_exact_row(raw_object, _SCHEMA_OBJECT_COLUMNS)
        coordinate = (
            _exact_row_text(row, "object_type"),
            _exact_row_text(row, "object_name"),
            _exact_row_text(row, "table_name"),
        )
        if coordinate in seen:
            raise _ResultArtifactIntegrityError(
                "result Artifact main schema coordinates are duplicated"
            )
        seen.add(coordinate)
        expected_digest = _RESULT_ARTIFACT_SCHEMA_DDL_SHA256.get(
            coordinate,
            _MISSING_RESULT_ARTIFACT_SCHEMA_DIGEST,
        )
        if expected_digest is _MISSING_RESULT_ARTIFACT_SCHEMA_DIGEST:
            raise _ResultArtifactIntegrityError(
                "result Artifact main schema contains an unexpected object"
            )
        root_page = _exact_row_integer(row, "root_page", minimum=1)
        ddl_sql = _exact_row_value(row, "ddl_sql")
        if expected_digest is None:
            if ddl_sql is not None:
                raise _ResultArtifactIntegrityError(
                    "result Artifact autoindex unexpectedly has catalog SQL"
                )
        else:
            if type(ddl_sql) is not str:
                raise _ResultArtifactIntegrityError(
                    "result Artifact explicit schema SQL is missing"
                )
            try:
                actual_digest = backup_schema_ddl_sha256(ddl_sql)
            except (TypeError, ValueError) as error:
                raise _ResultArtifactIntegrityError(
                    "result Artifact explicit schema SQL is invalid"
                ) from error
            if actual_digest != expected_digest:
                raise _ResultArtifactIntegrityError(
                    "result Artifact explicit schema DDL changed"
                )
        observed.append((*coordinate, root_page, ddl_sql))
    if seen != set(_RESULT_ARTIFACT_SCHEMA_DDL_SHA256):
        raise _ResultArtifactIntegrityError("result Artifact main schema topology changed")
    return schema_version, tuple(observed)


def _exact_row_text(row: sqlite3.Row, name: str) -> str:
    value = _exact_row_value(row, name)
    if type(value) is not str:
        raise _ResultArtifactIntegrityError("result Artifact row text storage is invalid")
    return value


def _exact_row_integer(row: sqlite3.Row, name: str, *, minimum: int = 0) -> int:
    value = _exact_row_value(row, name)
    if type(value) is not int or value < minimum:
        raise _ResultArtifactIntegrityError("result Artifact row integer storage is invalid")
    return value


def _preflight_result_artifact_head(
    connection: sqlite3.Connection,
    item: _PreparedResultArtifact,
    process_guard: Callable[[], None],
) -> None:
    raw_row = _guarded_fetchone(
        connection,
        process_guard,
        """
        SELECT
            count(*) AS row_count,
            COALESCE(min(version), 0) AS minimum_version,
            COALESCE(max(version), 0) AS maximum_version,
            COALESCE(sum(
                CASE
                    WHEN typeof(version) != 'integer' OR version < 1 THEN 1
                    WHEN version = 1 AND parent_version IS NULL THEN 0
                    WHEN version > 1
                         AND typeof(parent_version) = 'integer'
                         AND parent_version = version - 1 THEN 0
                    ELSE 1
                END
            ), 0) AS invalid_lineage_count
        FROM main.artifact_versions
        WHERE tenant_id = ? AND workspace_id = ?
          AND session_id = ? AND name = ?
        """,
        (item.tenant_id, item.workspace_id, item.session_id, item.name),
    )
    if raw_row is None:
        raise _ResultArtifactIntegrityError("result Artifact head aggregate is missing")
    row = _require_exact_row(raw_row, _HEAD_AGGREGATE_COLUMNS)
    row_count = _exact_row_integer(row, "row_count")
    minimum_version = _exact_row_integer(row, "minimum_version")
    maximum_version = _exact_row_integer(row, "maximum_version")
    invalid_lineage_count = _exact_row_integer(row, "invalid_lineage_count")
    expected_minimum = 1 if row_count else 0
    if (
        minimum_version != expected_minimum
        or maximum_version != row_count
        or invalid_lineage_count != 0
    ):
        raise _ResultArtifactIntegrityError(
            "result Artifact version history is not a contiguous exact lineage"
        )
    _verify_existing_result_artifact_history(
        connection,
        item,
        row_count,
        process_guard,
    )
    if maximum_version != item.expected_head_version:
        raise _ResultArtifactConcurrencyError("result Artifact head changed")


def _verify_existing_result_artifact_history(
    connection: sqlite3.Connection,
    item: _PreparedResultArtifact,
    row_count: int,
    process_guard: Callable[[], None],
) -> None:
    _run_process_guard(process_guard)
    _require_exact_connection_codec(connection)
    cursor = connection.execute(
        """
        SELECT
            rowid AS row_id,
            version,
            parent_version,
            byte_size,
            typeof(version) AS version_storage,
            typeof(parent_version) AS parent_version_storage,
            typeof(byte_size) AS byte_size_storage,
            typeof(artifact_id) AS artifact_id_storage,
            length(CAST(artifact_id AS BLOB)) AS artifact_id_bytes,
            typeof(tenant_id) AS tenant_id_storage,
            length(CAST(tenant_id AS BLOB)) AS tenant_id_bytes,
            typeof(workspace_id) AS workspace_id_storage,
            length(CAST(workspace_id AS BLOB)) AS workspace_id_bytes,
            typeof(session_id) AS session_id_storage,
            length(CAST(session_id AS BLOB)) AS session_id_bytes,
            typeof(task_id) AS task_id_storage,
            length(CAST(task_id AS BLOB)) AS task_id_bytes,
            typeof(name) AS name_storage,
            length(CAST(name AS BLOB)) AS name_bytes,
            typeof(media_type) AS media_type_storage,
            length(CAST(media_type AS BLOB)) AS media_type_bytes,
            typeof(blob_digest) AS blob_digest_storage,
            length(CAST(blob_digest AS BLOB)) AS blob_digest_bytes,
            typeof(metadata_json) AS metadata_json_storage,
            length(CAST(metadata_json AS BLOB)) AS metadata_json_bytes,
            typeof(created_by) AS created_by_storage,
            length(CAST(created_by AS BLOB)) AS created_by_bytes,
            typeof(created_at) AS created_at_storage,
            length(CAST(created_at AS BLOB)) AS created_at_bytes,
            typeof(idempotency_key) AS idempotency_key_storage,
            length(CAST(idempotency_key AS BLOB)) AS idempotency_key_bytes,
            typeof(request_digest) AS request_digest_storage,
            length(CAST(request_digest AS BLOB)) AS request_digest_bytes
        FROM main.artifact_versions
        WHERE tenant_id = ? AND workspace_id = ?
          AND session_id = ? AND name = ?
        ORDER BY version ASC
        """,
        (item.tenant_id, item.workspace_id, item.session_id, item.name),
    )
    _run_process_guard(process_guard)
    _require_exact_connection_codec(connection)
    if type(cursor) is not sqlite3.Cursor:
        raise _ResultArtifactIntegrityError("result Artifact history cursor must be exact")
    observed_rows = 0
    try:
        while True:
            raw_rows = _guarded_fetchmany(connection, cursor, process_guard)
            if not raw_rows:
                break
            for raw_preflight_row in raw_rows:
                expected_version = observed_rows + 1
                preflight = _require_exact_row(
                    raw_preflight_row,
                    _HISTORY_PREFLIGHT_COLUMNS,
                )
                row_id = _exact_row_integer(preflight, "row_id", minimum=1)
                version = _exact_row_integer(preflight, "version", minimum=1)
                byte_size = _exact_row_integer(preflight, "byte_size")
                parent = _exact_row_value(preflight, "parent_version")
                expected_parent = expected_version - 1 if expected_version > 1 else None
                expected_parent_storage = "integer" if expected_parent is not None else "null"
                if (
                    _exact_row_text(preflight, "version_storage") != "integer"
                    or _exact_row_text(preflight, "byte_size_storage") != "integer"
                    or _exact_row_text(preflight, "parent_version_storage")
                    != expected_parent_storage
                    or version != expected_version
                    or parent != expected_parent
                    or (parent is not None and type(parent) is not int)
                ):
                    raise _ResultArtifactIntegrityError(
                        "result Artifact history lineage or integer storage changed"
                    )
                for name, minimum_bytes, maximum_bytes in _HISTORY_TEXT_BYTE_BOUNDS:
                    if _exact_row_text(preflight, f"{name}_storage") != "text":
                        raise _ResultArtifactIntegrityError(
                            "result Artifact history text storage changed"
                        )
                    encoded_length = _exact_row_integer(preflight, f"{name}_bytes")
                    if not minimum_bytes <= encoded_length <= maximum_bytes:
                        raise _ResultArtifactIntegrityError(
                            "result Artifact history text exceeds its persisted byte contract"
                        )

                raw_row = _guarded_fetchone(
                    connection,
                    process_guard,
                    """
                    SELECT
                        artifact_id,
                        tenant_id,
                        workspace_id,
                        session_id,
                        task_id,
                        name,
                        version,
                        parent_version,
                        media_type,
                        blob_digest,
                        byte_size,
                        metadata_json,
                        created_by,
                        created_at,
                        idempotency_key,
                        request_digest
                    FROM main.artifact_versions
                    WHERE rowid = ?
                    """,
                    (row_id,),
                )
                if raw_row is None:
                    raise _ResultArtifactIntegrityError(
                        "result Artifact history row changed after preflight"
                    )
                row = _require_exact_row(raw_row, _VERSION_COLUMNS)
                text = {
                    name: _exact_row_text(row, name)
                    for name, _minimum, _maximum in _HISTORY_TEXT_BYTE_BOUNDS
                }
                if (
                    text["tenant_id"] != item.tenant_id
                    or text["workspace_id"] != item.workspace_id
                    or text["session_id"] != item.session_id
                    or text["name"] != item.name
                ):
                    raise _ResultArtifactIntegrityError(
                        "result Artifact history scope changed"
                    )
                if (
                    _exact_row_integer(row, "version", minimum=1) != version
                    or _exact_row_value(row, "parent_version") != parent
                    or _exact_row_integer(row, "byte_size") != byte_size
                ):
                    raise _ResultArtifactIntegrityError(
                        "result Artifact history row changed after preflight"
                    )
                try:
                    metadata = decode_canonical_artifact_metadata_v1(
                        text["metadata_json"].encode("utf-8")
                    )
                    request_digest = artifact_request_digest_v1(
                        tenant_id=text["tenant_id"],
                        workspace_id=text["workspace_id"],
                        session_id=text["session_id"],
                        task_id=text["task_id"],
                        name=text["name"],
                        media_type=text["media_type"],
                        blob_digest=text["blob_digest"],
                        byte_size=byte_size,
                        metadata=metadata,
                        created_by=text["created_by"],
                    )
                    _canonical_result_artifact_timestamp(
                        text["created_at"],
                        persisted=True,
                    )
                except (TypeError, ValueError, UnicodeError) as error:
                    raise _ResultArtifactIntegrityError(
                        "result Artifact history violates its canonical row contract"
                    ) from error
                if request_digest != text["request_digest"]:
                    raise _ResultArtifactIntegrityError(
                        "result Artifact history request digest changed"
                    )
                observed_rows += 1
                _run_process_guard(process_guard)
    finally:
        _run_process_guard(process_guard)
        cursor.close()
        _run_process_guard(process_guard)
        _require_exact_connection_codec(connection)
    if observed_rows != row_count:
        raise _ResultArtifactIntegrityError("result Artifact history row count changed")


def _preflight_result_artifact_identity(
    connection: sqlite3.Connection,
    item: _PreparedResultArtifact,
    process_guard: Callable[[], None],
) -> None:
    rows = _guarded_fetchall(
        connection,
        process_guard,
        """
        SELECT artifact_id
        FROM main.artifact_versions
        WHERE artifact_id = ?
           OR (tenant_id = ? AND workspace_id = ? AND idempotency_key = ?)
        LIMIT 2
        """,
        (
            item.artifact_id,
            item.tenant_id,
            item.workspace_id,
            item.idempotency_key,
        ),
    )
    if rows:
        raise _ResultArtifactConflictError("result Artifact identity is already bound")


def _select_result_artifact_blob(
    connection: sqlite3.Connection,
    digest: str,
    process_guard: Callable[[], None],
) -> object:
    return _guarded_fetchone(
        connection,
        process_guard,
        """
        SELECT
            digest,
            content,
            byte_size,
            created_at,
            typeof(digest) AS digest_storage,
            typeof(content) AS content_storage,
            typeof(byte_size) AS byte_size_storage,
            typeof(created_at) AS created_at_storage,
            length(content) AS content_length
        FROM main.artifact_blobs
        WHERE digest = ?
        """,
        (digest,),
    )


def _verify_result_artifact_blob(
    raw_row: object,
    item: _PreparedResultArtifact,
    *,
    fresh: bool,
    created_at: str | None,
) -> None:
    if raw_row is None:
        raise _ResultArtifactIntegrityError("result Artifact blob is missing")
    row = _require_exact_row(raw_row, _BLOB_COLUMNS)
    if (
        _exact_row_text(row, "digest_storage") != "text"
        or _exact_row_text(row, "content_storage") != "blob"
        or _exact_row_text(row, "byte_size_storage") != "integer"
        or _exact_row_text(row, "created_at_storage") != "text"
    ):
        raise _ResultArtifactIntegrityError("result Artifact blob storage classes changed")
    content = _exact_row_value(row, "content")
    if type(content) is not bytes:
        raise _ResultArtifactIntegrityError("result Artifact blob content is not exact bytes")
    if (
        _exact_row_text(row, "digest") != item.descriptor.blob_digest
        or content != item.content
        or _exact_row_integer(row, "byte_size") != len(item.content)
        or _exact_row_integer(row, "content_length") != len(item.content)
    ):
        raise _ResultArtifactIntegrityError("result Artifact blob readback differs")
    stored_created_at = _exact_row_text(row, "created_at")
    try:
        _canonical_result_artifact_timestamp(stored_created_at, persisted=True)
    except ValueError as error:
        raise _ResultArtifactIntegrityError(
            "persisted result Artifact timestamp is not canonical UTC microseconds"
        ) from error
    if fresh and (type(created_at) is not str or stored_created_at != created_at):
        raise _ResultArtifactIntegrityError("fresh result Artifact blob timestamp differs")


def _verify_result_artifact_version(
    connection: sqlite3.Connection,
    item: _PreparedResultArtifact,
    created_at: str,
    process_guard: Callable[[], None],
) -> None:
    raw_row = _guarded_fetchone(
        connection,
        process_guard,
        """
        SELECT
            artifact_id,
            tenant_id,
            workspace_id,
            session_id,
            task_id,
            name,
            version,
            parent_version,
            media_type,
            blob_digest,
            byte_size,
            metadata_json,
            created_by,
            created_at,
            idempotency_key,
            request_digest
        FROM main.artifact_versions
        WHERE artifact_id = ?
        """,
        (item.artifact_id,),
    )
    if raw_row is None:
        raise _ResultArtifactIntegrityError("result Artifact version readback is missing")
    row = _require_exact_row(raw_row, _VERSION_COLUMNS)
    expected = item.descriptor
    text_values = {
        "artifact_id": item.artifact_id,
        "tenant_id": item.tenant_id,
        "workspace_id": item.workspace_id,
        "session_id": item.session_id,
        "task_id": item.task_id,
        "name": item.name,
        "media_type": item.media_type,
        "blob_digest": expected.blob_digest,
        "metadata_json": item.metadata_json,
        "created_by": item.created_by,
        "created_at": created_at,
        "idempotency_key": item.idempotency_key,
        "request_digest": expected.request_digest,
    }
    for name, value in text_values.items():
        if _exact_row_text(row, name) != value:
            raise _ResultArtifactIntegrityError("result Artifact version text readback differs")
    if _exact_row_integer(row, "version", minimum=1) != expected.version:
        raise _ResultArtifactIntegrityError("result Artifact version readback differs")
    parent = _exact_row_value(row, "parent_version")
    if parent != expected.parent_version or (parent is not None and type(parent) is not int):
        raise _ResultArtifactIntegrityError("result Artifact parent readback differs")
    if _exact_row_integer(row, "byte_size") != expected.byte_size:
        raise _ResultArtifactIntegrityError("result Artifact byte-size readback differs")
    try:
        metadata_bytes = _exact_row_text(row, "metadata_json").encode("utf-8")
    except UnicodeError as error:
        raise _ResultArtifactIntegrityError(
            "result Artifact metadata readback is not UTF-8"
        ) from error
    if metadata_bytes != item.metadata_canonical_bytes:
        raise _ResultArtifactIntegrityError("result Artifact metadata bytes differ")


def _write_prepared_result_artifacts_in_transaction_body(
    connection: sqlite3.Connection,
    batch: _PreparedResultArtifactBatch,
    *,
    clock: Callable[[], str],
    process_guard: Callable[[], None],
    claim_sqlite_callbacks: Callable[[], None],
) -> tuple[ScopedInvocationResultArtifactV2, ...]:
    _run_process_guard(process_guard)
    if type(connection) is not sqlite3.Connection or not connection.in_transaction:
        raise RuntimeError("result Artifact write requires an exact open SQLite transaction")
    if type(batch) is not _PreparedResultArtifactBatch:
        raise TypeError("result Artifact write requires an exact prepared batch")
    batch.verify()
    if not callable(clock):
        raise TypeError("result Artifact transaction clock must be callable")
    if not callable(process_guard):
        raise TypeError("result Artifact process guard must be callable")
    if not callable(claim_sqlite_callbacks):
        raise TypeError("result Artifact SQLite callback claim must be callable")
    _require_exact_connection_codec(connection)
    schema_snapshot = _result_artifact_schema_snapshot(connection, process_guard)
    if not batch.items:
        return ()
    for item in batch.items:
        _run_process_guard(process_guard)
        _preflight_result_artifact_identity(connection, item, process_guard)
        _preflight_result_artifact_head(connection, item, process_guard)
        existing_blob = _select_result_artifact_blob(
            connection,
            item.descriptor.blob_digest,
            process_guard,
        )
        if existing_blob is not None:
            _verify_result_artifact_blob(existing_blob, item, fresh=False, created_at=None)
        _run_process_guard(process_guard)
    _run_process_guard(process_guard)
    before_total_changes = connection.total_changes
    _run_process_guard(process_guard)
    if type(before_total_changes) is not int:
        raise _ResultArtifactIntegrityError("SQLite total-change counter is invalid")
    _run_process_guard(process_guard)
    created_at = clock()
    _run_process_guard(process_guard)
    created_at = _canonical_result_artifact_timestamp(created_at, persisted=False)
    _run_process_guard(process_guard)
    claim_sqlite_callbacks()
    transaction_open = connection.in_transaction
    if type(transaction_open) is not bool or not transaction_open:
        raise _ResultArtifactIntegrityError(
            "result Artifact owner transaction changed during clock sampling"
        )
    if _result_artifact_schema_snapshot(connection, process_guard) != schema_snapshot:
        raise _ResultArtifactIntegrityError(
            "result Artifact schema changed during clock sampling"
        )
    fresh_blobs = 0
    for item in batch.items:
        _guarded_execute(
            connection,
            process_guard,
            """
            INSERT INTO main.artifact_blobs(digest, content, byte_size, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(digest) DO NOTHING
            """,
            (
                item.descriptor.blob_digest,
                sqlite3.Binary(item.content),
                item.descriptor.byte_size,
                created_at,
            ),
        )
        raw_blob_changes_row = _guarded_fetchone(
            connection,
            process_guard,
            "SELECT changes() AS changed",
        )
        if raw_blob_changes_row is None:
            raise _ResultArtifactIntegrityError("result Artifact blob change count is missing")
        blob_changes_row = _require_exact_row(raw_blob_changes_row, _CHANGE_COUNT_COLUMNS)
        blob_changes = _exact_row_integer(blob_changes_row, "changed")
        if blob_changes not in (0, 1):
            raise _ResultArtifactIntegrityError("result Artifact blob change count is invalid")
        fresh_blobs += blob_changes
        _verify_result_artifact_blob(
            _select_result_artifact_blob(
                connection,
                item.descriptor.blob_digest,
                process_guard,
            ),
            item,
            fresh=bool(blob_changes),
            created_at=created_at,
        )
        try:
            _guarded_execute(
                connection,
                process_guard,
                """
                INSERT INTO main.artifact_versions(
                    artifact_id,
                    tenant_id,
                    workspace_id,
                    session_id,
                    task_id,
                    name,
                    version,
                    parent_version,
                    media_type,
                    blob_digest,
                    byte_size,
                    metadata_json,
                    created_by,
                    created_at,
                    idempotency_key,
                    request_digest
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.artifact_id,
                    item.tenant_id,
                    item.workspace_id,
                    item.session_id,
                    item.task_id,
                    item.name,
                    item.descriptor.version,
                    item.descriptor.parent_version,
                    item.media_type,
                    item.descriptor.blob_digest,
                    item.descriptor.byte_size,
                    item.metadata_json,
                    item.created_by,
                    created_at,
                    item.idempotency_key,
                    item.descriptor.request_digest,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise _ResultArtifactIntegrityError(
                "result Artifact version insert violated the preflight contract"
            ) from error
        raw_version_changes_row = _guarded_fetchone(
            connection,
            process_guard,
            "SELECT changes() AS changed",
        )
        if raw_version_changes_row is None:
            raise _ResultArtifactIntegrityError("result Artifact version change count is missing")
        version_changes_row = _require_exact_row(raw_version_changes_row, _CHANGE_COUNT_COLUMNS)
        if _exact_row_integer(version_changes_row, "changed") != 1:
            raise _ResultArtifactIntegrityError("result Artifact version change count is invalid")
        _verify_result_artifact_version(connection, item, created_at, process_guard)
    _run_process_guard(process_guard)
    after_total_changes = connection.total_changes
    _run_process_guard(process_guard)
    if type(after_total_changes) is not int:
        raise _ResultArtifactIntegrityError("SQLite total-change counter is invalid")
    if after_total_changes - before_total_changes != fresh_blobs + len(batch.items):
        raise _ResultArtifactIntegrityError(
            "result Artifact transaction had unexpected DML effects"
        )
    transaction_open = connection.in_transaction
    if type(transaction_open) is not bool or not transaction_open:
        raise _ResultArtifactIntegrityError(
            "result Artifact owner transaction closed before final readback"
        )
    if _result_artifact_schema_snapshot(connection, process_guard) != schema_snapshot:
        raise _ResultArtifactIntegrityError(
            "result Artifact schema changed during owner DML"
        )
    _run_process_guard(process_guard)
    return tuple(item.descriptor for item in batch.items)


def _write_prepared_result_artifacts_in_transaction(
    connection: sqlite3.Connection,
    batch: _PreparedResultArtifactBatch,
    *,
    clock: Callable[[], str],
    process_guard: Callable[[], None],
) -> tuple[ScopedInvocationResultArtifactV2, ...]:
    if not callable(process_guard):
        raise TypeError("result Artifact process guard must be callable")
    with _result_artifact_process_progress_fence(
        connection,
        process_guard,
    ) as claim_sqlite_callbacks:
        return _write_prepared_result_artifacts_in_transaction_body(
            connection,
            batch,
            clock=clock,
            process_guard=process_guard,
            claim_sqlite_callbacks=claim_sqlite_callbacks,
        )
