# ruff: noqa: UP006, UP035
"""Durable anti-rollback high-water for native-IM sandbox approval state.

This store does not load approvals or create permits. It records the highest independently
observed authority state so process restart cannot revive a lower revision, an equivocated
state, a wall-clock rollback, or an approval ID that has reached terminal revocation.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, ClassVar, Dict, NoReturn, SupportsIndex

from . import process_identity as _process_identity
from ._native_im_codec import (
    NativeIMCodecTooLargeError,
    _canonical_json_bytes,
    _decode_json_bytes,
    _digest,
    _enum,
    _id,
    _model_digest,
    _plain_dict,
    _positive_integer,
    _schema_version,
    _timestamp,
)

_MAX_STATE_BYTES = 16 * 1_024
_STATES = {"approved", "revoked"}
_STATE_FIELDS = {
    "approvalDigest",
    "approvalId",
    "authorityRevision",
    "observedAt",
    "schemaVersion",
    "state",
}
_TABLE_NAME = "qe_native_im_sandbox_approval_high_water"
_SCHEMA = f"""
CREATE TABLE {_TABLE_NAME} (
    approval_id TEXT PRIMARY KEY,
    approval_digest TEXT NOT NULL CHECK(
        length(approval_digest) = 64
        AND approval_digest NOT GLOB '*[^0-9a-f]*'
    ),
    authority_revision INTEGER NOT NULL CHECK(authority_revision > 0),
    state TEXT NOT NULL CHECK(state IN ('approved', 'revoked')),
    state_digest TEXT NOT NULL CHECK(
        length(state_digest) = 64
        AND state_digest NOT GLOB '*[^0-9a-f]*'
    ),
    terminal_revoked INTEGER NOT NULL CHECK(terminal_revoked IN (0, 1)),
    max_observed_at TEXT NOT NULL CHECK(length(max_observed_at) = 27),
    CHECK(
        (state = 'revoked' AND terminal_revoked = 1)
        OR (state = 'approved' AND terminal_revoked = 0)
    )
)
""".strip()


class NativeIMSandboxApprovalStoreError(RuntimeError):
    """Stable redacted durable approval-state failure."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class NativeIMSandboxApprovalStoreIntegrityError(NativeIMSandboxApprovalStoreError):
    """Owned schema or persisted state failed exact integrity validation."""


@dataclass(frozen=True, repr=False)
class NativeIMSandboxApprovalAuthorityStateV1:
    """One canonical observation from the independent approval authority."""

    schema_version: int
    approval_id: str
    approval_digest: str = field(repr=False)
    authority_revision: int
    state: str
    observed_at: str

    _MODEL_NAME: ClassVar[str] = "NativeIMSandboxApprovalAuthorityStateV1"

    def __post_init__(self) -> None:
        if type(self) is not NativeIMSandboxApprovalAuthorityStateV1:
            raise TypeError("approval authority state requires the exact V1 class")
        _schema_version(self.schema_version)
        _id(self.approval_id, "approvalId")
        _digest(self.approval_digest, "approvalDigest")
        _positive_integer(self.authority_revision, "authorityRevision")
        _enum(self.state, _STATES, "state")
        _timestamp(self.observed_at, "observedAt")
        self.canonical_bytes()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "approvalDigest": self.approval_digest,
            "approvalId": self.approval_id,
            "authorityRevision": self.authority_revision,
            "observedAt": self.observed_at,
            "schemaVersion": self.schema_version,
            "state": self.state,
        }

    @classmethod
    def from_dict(cls, value: object) -> NativeIMSandboxApprovalAuthorityStateV1:
        if cls is not NativeIMSandboxApprovalAuthorityStateV1:
            raise TypeError("approval authority state decoder requires the exact V1 class")
        body = _plain_dict(value, _STATE_FIELDS, "native IM sandbox approval authority state")
        return cls(
            schema_version=body["schemaVersion"],
            approval_id=body["approvalId"],
            approval_digest=body["approvalDigest"],
            authority_revision=body["authorityRevision"],
            state=body["state"],
            observed_at=body["observedAt"],
        )

    @classmethod
    def from_json_bytes(
        cls,
        encoded: object,
    ) -> NativeIMSandboxApprovalAuthorityStateV1:
        if cls is not NativeIMSandboxApprovalAuthorityStateV1:
            raise TypeError("approval authority state decoder requires the exact V1 class")
        return cls.from_dict(
            _decode_json_bytes(
                encoded,
                "native IM sandbox approval authority state",
                maximum_bytes=_MAX_STATE_BYTES,
            )
        )

    def canonical_bytes(self) -> bytes:
        encoded = _canonical_json_bytes(self.to_dict())
        if len(encoded) > _MAX_STATE_BYTES:
            raise NativeIMCodecTooLargeError("approval authority state exceeds its byte limit")
        return encoded

    def canonical_digest(self) -> str:
        return _model_digest(self._MODEL_NAME, self.to_dict())

    def state_binding_digest(self) -> str:
        """Bind the immutable state while excluding monotonic observation time."""

        return _model_digest(
            "NativeIMSandboxApprovalAuthorityStateBindingV1",
            {
                "approvalDigest": self.approval_digest,
                "approvalId": self.approval_id,
                "authorityRevision": self.authority_revision,
                "schemaVersion": self.schema_version,
                "state": self.state,
            },
        )

    def __repr__(self) -> str:
        return (
            "NativeIMSandboxApprovalAuthorityStateV1("
            f"revision={self.authority_revision}, state={self.state!r})"
        )


def _normalized_ddl(value: str) -> str:
    return " ".join(value.strip().rstrip(";").split()).casefold()


class SQLiteNativeIMSandboxApprovalHighWaterV1:
    """Exact-schema, cross-process durable approval revision/state high-water."""

    __slots__ = (
        "__closed",
        "__connection",
        "__lock",
        "__path",
        "__path_fingerprint",
        "__process_owner",
    )

    def __init__(self, path: str, *, busy_timeout_ms: int = 5_000) -> None:
        if type(path) is not str or not path:
            raise ValueError("approval high-water path must be non-empty text")
        if path != ":memory:" and not os.path.isabs(path):
            raise ValueError("approval high-water path must be absolute")
        if type(busy_timeout_ms) is not int:
            raise TypeError("approval high-water busy timeout must be an exact integer")
        if not 1 <= busy_timeout_ms <= 300_000:
            raise ValueError("approval high-water busy timeout is outside the supported range")
        self.__process_owner = _process_identity.capture_process_owner()
        self.__path = path
        self.__path_fingerprint = _model_digest(
            "NativeIMSandboxApprovalHighWaterPathV1",
            {"path": path},
        )[:12]
        self.__lock = threading.RLock()
        self.__closed = False
        if path != ":memory:":
            parent = os.path.dirname(path)
            os.makedirs(parent, mode=0o700, exist_ok=True)
            try:
                descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            except FileExistsError:
                if os.path.islink(path):
                    raise NativeIMSandboxApprovalStoreIntegrityError(
                        "native_im_sandbox_approval_store_symlink_forbidden"
                    ) from None
            else:
                os.close(descriptor)
            try:
                mode = os.stat(path, follow_symlinks=False).st_mode & 0o777
            except OSError:
                raise NativeIMSandboxApprovalStoreIntegrityError(
                    "native_im_sandbox_approval_store_path_invalid"
                ) from None
            if mode & 0o077:
                raise NativeIMSandboxApprovalStoreIntegrityError(
                    "native_im_sandbox_approval_store_permissions_too_broad"
                ) from None
        try:
            connection = sqlite3.connect(
                path,
                check_same_thread=False,
                isolation_level=None,
                timeout=busy_timeout_ms / 1_000,
            )
        except sqlite3.Error:
            raise NativeIMSandboxApprovalStoreError(
                "native_im_sandbox_approval_store_open_failed"
            ) from None
        connection.row_factory = sqlite3.Row
        self.__connection = connection
        try:
            self._initialize(busy_timeout_ms)
        except BaseException:
            connection.close()
            self.__closed = True
            raise

    def _require_process(self) -> None:
        _process_identity.require_current_process(
            self.__process_owner,
            lambda: NativeIMSandboxApprovalStoreError(
                "native_im_sandbox_approval_store_process_mismatch"
            ),
        )

    def _require_open(self) -> None:
        self._require_process()
        if self.__closed:
            raise NativeIMSandboxApprovalStoreError(
                "native_im_sandbox_approval_store_closed"
            ) from None

    def _initialize(self, busy_timeout_ms: int) -> None:
        self._require_open()
        with self.__lock:
            try:
                self.__connection.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
                self.__connection.execute("PRAGMA trusted_schema=OFF")
                self.__connection.execute("PRAGMA foreign_keys=ON")
                if self.__path != ":memory:":
                    self.__connection.execute("PRAGMA journal_mode=WAL")
                    self.__connection.execute("PRAGMA synchronous=FULL")
            except sqlite3.Error:
                raise NativeIMSandboxApprovalStoreError(
                    "native_im_sandbox_approval_store_initialize_failed"
                ) from None
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT type, sql FROM sqlite_master WHERE name = ?",
                (_TABLE_NAME,),
            ).fetchone()
            if existing is None:
                connection.execute(_SCHEMA)
            self._validate_schema(connection)
            rows = connection.execute(
                f"SELECT * FROM {_TABLE_NAME} ORDER BY approval_id"
            ).fetchall()
            for row in rows:
                self._validate_row(row)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        self._require_open()
        with self.__lock:
            try:
                self.__connection.execute("BEGIN IMMEDIATE")
                yield self.__connection
            except BaseException:
                try:
                    self.__connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            else:
                try:
                    self.__connection.execute("COMMIT")
                except sqlite3.Error:
                    try:
                        self.__connection.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    raise NativeIMSandboxApprovalStoreError(
                        "native_im_sandbox_approval_store_commit_failed"
                    ) from None

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
        owned = connection.execute(
            "SELECT type, sql FROM sqlite_master WHERE name = ?",
            (_TABLE_NAME,),
        ).fetchone()
        if (
            owned is None
            or owned["type"] != "table"
            or type(owned["sql"]) is not str
            or _normalized_ddl(owned["sql"]) != _normalized_ddl(_SCHEMA)
        ):
            raise NativeIMSandboxApprovalStoreIntegrityError(
                "native_im_sandbox_approval_store_schema_mismatch"
            ) from None
        unexpected = connection.execute(
            """
            SELECT type, name FROM sqlite_master
            WHERE tbl_name = ? AND type IN ('index', 'trigger') AND sql IS NOT NULL
            ORDER BY type, name
            """,
            (_TABLE_NAME,),
        ).fetchall()
        if unexpected:
            raise NativeIMSandboxApprovalStoreIntegrityError(
                "native_im_sandbox_approval_store_schema_extension_forbidden"
            ) from None

    @staticmethod
    def _validate_row(row: sqlite3.Row) -> NativeIMSandboxApprovalAuthorityStateV1:
        try:
            approval_id = _id(row["approval_id"], "approvalId")
            approval_digest = _digest(row["approval_digest"], "approvalDigest")
            revision = _positive_integer(row["authority_revision"], "authorityRevision")
            state = _enum(row["state"], _STATES, "state")
            state_digest = _digest(row["state_digest"], "stateDigest")
            terminal = row["terminal_revoked"]
            observed_at = _timestamp(row["max_observed_at"], "maxObservedAt")
        except (IndexError, KeyError, TypeError, ValueError):
            raise NativeIMSandboxApprovalStoreIntegrityError(
                "native_im_sandbox_approval_store_row_invalid"
            ) from None
        if type(terminal) is not int or terminal not in {0, 1}:
            raise NativeIMSandboxApprovalStoreIntegrityError(
                "native_im_sandbox_approval_store_row_invalid"
            ) from None
        value = NativeIMSandboxApprovalAuthorityStateV1(
            schema_version=1,
            approval_id=approval_id,
            approval_digest=approval_digest,
            authority_revision=revision,
            state=state,
            observed_at=observed_at,
        )
        if state_digest != value.state_binding_digest() or bool(terminal) != (state == "revoked"):
            raise NativeIMSandboxApprovalStoreIntegrityError(
                "native_im_sandbox_approval_store_row_integrity_failed"
            ) from None
        return value

    def _observe_in_transaction(
        self,
        connection: sqlite3.Connection,
        snapshot: NativeIMSandboxApprovalAuthorityStateV1,
    ) -> NativeIMSandboxApprovalAuthorityStateV1:
        self._validate_schema(connection)
        row = connection.execute(
            f"SELECT * FROM {_TABLE_NAME} WHERE approval_id = ?",
            (snapshot.approval_id,),
        ).fetchone()
        if row is None:
            connection.execute(
                f"""
                INSERT INTO {_TABLE_NAME} (
                    approval_id, approval_digest, authority_revision, state,
                    state_digest, terminal_revoked, max_observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.approval_id,
                    snapshot.approval_digest,
                    snapshot.authority_revision,
                    snapshot.state,
                    snapshot.state_binding_digest(),
                    int(snapshot.state == "revoked"),
                    snapshot.observed_at,
                ),
            )
            return snapshot
        current = self._validate_row(row)
        if snapshot.observed_at < current.observed_at:
            raise NativeIMSandboxApprovalStoreError(
                "native_im_sandbox_approval_store_clock_rollback"
            ) from None
        if current.state == "revoked":
            if (
                snapshot.authority_revision != current.authority_revision
                or snapshot.state != "revoked"
                or snapshot.approval_digest != current.approval_digest
                or snapshot.state_binding_digest() != current.state_binding_digest()
            ):
                raise NativeIMSandboxApprovalStoreError(
                    "native_im_sandbox_approval_store_terminal_revoked"
                ) from None
        elif snapshot.authority_revision < current.authority_revision:
            raise NativeIMSandboxApprovalStoreError(
                "native_im_sandbox_approval_store_revision_rollback"
            ) from None
        elif snapshot.authority_revision == current.authority_revision:
            if (
                snapshot.approval_digest != current.approval_digest
                or snapshot.state_binding_digest() != current.state_binding_digest()
            ):
                raise NativeIMSandboxApprovalStoreError(
                    "native_im_sandbox_approval_store_equivocation"
                ) from None
        else:
            if snapshot.approval_digest != current.approval_digest:
                raise NativeIMSandboxApprovalStoreError(
                    "native_im_sandbox_approval_store_record_changed"
                ) from None
            if snapshot.state != "revoked":
                raise NativeIMSandboxApprovalStoreError(
                    "native_im_sandbox_approval_store_renewal_requires_new_id"
                ) from None
        connection.execute(
            f"""
            UPDATE {_TABLE_NAME}
            SET approval_digest = ?, authority_revision = ?, state = ?,
                state_digest = ?, terminal_revoked = ?, max_observed_at = ?
            WHERE approval_id = ?
            """,
            (
                snapshot.approval_digest,
                snapshot.authority_revision,
                snapshot.state,
                snapshot.state_binding_digest(),
                int(snapshot.state == "revoked"),
                snapshot.observed_at,
                snapshot.approval_id,
            ),
        )
        return snapshot

    @staticmethod
    def _snapshot_state(
        value: NativeIMSandboxApprovalAuthorityStateV1,
    ) -> NativeIMSandboxApprovalAuthorityStateV1:
        if type(value) is not NativeIMSandboxApprovalAuthorityStateV1:
            raise TypeError("approval high-water requires the exact authority state V1")
        return NativeIMSandboxApprovalAuthorityStateV1.from_json_bytes(value.canonical_bytes())

    def observe(
        self,
        value: NativeIMSandboxApprovalAuthorityStateV1,
    ) -> NativeIMSandboxApprovalAuthorityStateV1:
        """Atomically validate and advance one authority state observation."""

        self._require_open()
        snapshot = self._snapshot_state(value)
        try:
            with self._transaction() as connection:
                return self._observe_in_transaction(connection, snapshot)
        except sqlite3.Error:
            raise NativeIMSandboxApprovalStoreError(
                "native_im_sandbox_approval_store_write_failed"
            ) from None

    @contextmanager
    def admission_guard(
        self,
        value: NativeIMSandboxApprovalAuthorityStateV1,
    ) -> Iterator[NativeIMSandboxApprovalAuthorityStateV1]:
        """Hold the durable write lock across one final local admission section."""

        self._require_open()
        snapshot = self._snapshot_state(value)
        if snapshot.state != "approved":
            raise NativeIMSandboxApprovalStoreError(
                "native_im_sandbox_approval_store_admission_state_forbidden"
            ) from None
        try:
            with self._transaction() as connection:
                yield self._observe_in_transaction(connection, snapshot)
        except sqlite3.Error:
            raise NativeIMSandboxApprovalStoreError(
                "native_im_sandbox_approval_store_admission_failed"
            ) from None

    def current(
        self,
        approval_id: str,
    ) -> NativeIMSandboxApprovalAuthorityStateV1 | None:
        self._require_open()
        try:
            canonical_id = _id(approval_id, "approvalId")
        except (TypeError, ValueError):
            raise NativeIMSandboxApprovalStoreError(
                "native_im_sandbox_approval_store_approval_id_invalid"
            ) from None
        try:
            with self._transaction() as connection:
                self._validate_schema(connection)
                row = connection.execute(
                    f"SELECT * FROM {_TABLE_NAME} WHERE approval_id = ?",
                    (canonical_id,),
                ).fetchone()
                return None if row is None else self._validate_row(row)
        except sqlite3.Error:
            raise NativeIMSandboxApprovalStoreError(
                "native_im_sandbox_approval_store_read_failed"
            ) from None

    def close(self) -> None:
        self._require_process()
        with self.__lock:
            if self.__closed:
                return
            try:
                self.__connection.close()
            except sqlite3.Error:
                raise NativeIMSandboxApprovalStoreError(
                    "native_im_sandbox_approval_store_close_failed"
                ) from None
            self.__closed = True

    @property
    def closed(self) -> bool:
        self._require_process()
        return self.__closed

    @property
    def durable(self) -> bool:
        """Whether the high-water survives process restart on a filesystem path."""

        self._require_process()
        return self.__path != ":memory:"

    def __enter__(self) -> SQLiteNativeIMSandboxApprovalHighWaterV1:
        self._require_open()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def __copy__(self) -> NoReturn:
        raise TypeError("native IM sandbox approval high-water stores cannot be copied")

    def __deepcopy__(self, memo: object) -> NoReturn:
        raise TypeError("native IM sandbox approval high-water stores cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("native IM sandbox approval high-water stores cannot be serialized")

    def __reduce_ex__(self, protocol: SupportsIndex) -> NoReturn:
        raise TypeError("native IM sandbox approval high-water stores cannot be serialized")

    def __repr__(self) -> str:
        self._require_process()
        return (
            "SQLiteNativeIMSandboxApprovalHighWaterV1("
            f"path={self.__path_fingerprint!r}, closed={self.__closed!r})"
        )


__all__ = [
    "NativeIMSandboxApprovalAuthorityStateV1",
    "NativeIMSandboxApprovalStoreError",
    "NativeIMSandboxApprovalStoreIntegrityError",
    "SQLiteNativeIMSandboxApprovalHighWaterV1",
]
