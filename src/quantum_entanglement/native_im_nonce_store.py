# ruff: noqa: UP006, UP031, UP035, UP045
"""Process-bound SQLite replay ledger for native-IM inbound authentication.

This module is the first persistence slice of the dedicated native-IM inbox store.  It
does not decode provider payloads, create domain events, dispatch work, or grant outbound
authority.  Later inbox/page admission uses the same store boundary so nonce evidence and
observations can be committed atomically before any real connector is enabled.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import traceback as traceback_module
from contextlib import contextmanager
from typing import Any, Callable, Iterator, NoReturn, Optional, SupportsIndex, Tuple

from . import process_identity as _process_identity
from ._native_im_codec import _digest, _id, _timestamp
from .migrations import apply_sqlite_migrations
from .protocol import utc_now
from .store import SQLiteEventStore


class NativeIMNonceIntegrityError(RuntimeError):
    """The durable nonce graph is malformed or an identity was rebound."""

    code = "native_im_nonce_integrity_failed"

    def __init__(self) -> None:
        super().__init__("native IM nonce state is not bound to one canonical claim")


class NativeIMNonceTransactionError(RuntimeError):
    """A nonce claim transaction was confirmed not to have committed."""

    code = "native_im_nonce_transaction_failed"

    def __init__(self) -> None:
        super().__init__("native IM nonce claim transaction was rolled back")


class NativeIMNonceCommitAmbiguityError(NativeIMNonceTransactionError):
    """A nonce claim may be durable but its COMMIT was not acknowledged."""

    code = "native_im_nonce_commit_ambiguous"

    def __init__(self) -> None:
        RuntimeError.__init__(
            self,
            "native IM nonce claim outcome is unknown; reopen and reconcile the exact claim",
        )


class NativeIMNonceStoreClosedError(RuntimeError):
    """The native-IM inbox store has already been closed."""

    code = "native_im_nonce_store_closed"

    def __init__(self) -> None:
        super().__init__("native IM nonce store is closed")


class NativeIMNonceStorePoisonedError(NativeIMNonceIntegrityError):
    """An ambiguous transaction quarantined this store instance."""

    code = "native_im_nonce_store_poisoned"

    def __init__(self) -> None:
        RuntimeError.__init__(self, "native IM nonce store is poisoned and must be reopened")


class NativeIMNonceStoreProcessMismatchError(RuntimeError):
    """A fork-inherited store was used outside its owning process epoch."""

    code = "native_im_nonce_store_process_mismatch"

    def __init__(self) -> None:
        super().__init__(self.code)


class _NonceTransactionSignal(BaseException):
    """Private, data-free transaction outcome signal."""

    __slots__ = ("kind",)

    def __init__(self, kind: str) -> None:
        super().__init__("native IM nonce transaction signal")
        self.kind = kind


_NATIVE_IM_NONCE_STORE_QUARANTINE: list[object] = []


def _detach_exception(error: BaseException) -> None:
    error_traceback = error.__traceback__
    error.__cause__ = None
    error.__context__ = None
    error.__traceback__ = None
    if error_traceback is not None:
        traceback_module.clear_frames(error_traceback)


def _raise_clean_error(kind: str) -> NoReturn:
    error_type: type[BaseException]
    if kind == "integrity":
        error_type = NativeIMNonceIntegrityError
    elif kind == "transaction":
        error_type = NativeIMNonceTransactionError
    elif kind == "ambiguous":
        error_type = NativeIMNonceCommitAmbiguityError
    else:  # pragma: no cover - private callers use a closed enum.
        raise RuntimeError("unsupported native IM nonce error kind") from None
    try:
        raise error_type() from None
    except BaseException as public_error:
        if type(public_error) is error_type:
            public_error.__context__ = None
        raise


def _scope_snapshot(value: object) -> Tuple[str, str, str, str]:
    if type(value) is not tuple:
        raise TypeError("scope must be an exact tuple")
    if len(value) != 4:
        raise ValueError("scope must contain tenant, workspace, provider, and channel")
    return (
        _id(value[0], "tenantId"),
        _id(value[1], "workspaceId"),
        _id(value[2], "provider"),
        _id(value[3], "channelId"),
    )


class SQLiteNativeIMInboxStore:
    """Dedicated SQLite boundary for capability-free native-IM observations.

    Only durable nonce claims are exposed in this slice.  The class name intentionally
    reflects the final ownership boundary: verified envelopes, pages, and checkpoints
    will be admitted here rather than through the generic domain event inbox.
    """

    def __init__(
        self,
        path: str,
        *,
        profile_revision: str,
        profile_digest: str,
        clock: Callable[[], str] = utc_now,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        self._process_owner = _process_identity.capture_process_owner()
        self._require_current_process()
        if type(path) is not str or not path:
            raise ValueError("path must be a non-empty string")
        profile_revision_snapshot = _id(profile_revision, "profileRevision")
        profile_digest_snapshot = _digest(profile_digest, "profileDigest")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if type(busy_timeout_ms) is not int:
            raise TypeError("busy_timeout_ms must be an integer")
        if not 1 <= busy_timeout_ms <= 300_000:
            raise ValueError("busy_timeout_ms must be between 1 and 300000")

        self._path = path
        self._profile_revision = profile_revision_snapshot
        self._profile_digest = profile_digest_snapshot
        self._clock = clock
        self._lock = threading.RLock()
        self._closed = False
        self._poisoned = False
        connection: Optional[sqlite3.Connection] = None
        try:
            if path == ":memory:":
                raise ValueError("native IM nonce storage requires a durable file path")
            absolute_path = os.path.abspath(path)
            os.makedirs(os.path.dirname(absolute_path), exist_ok=True)
            try:
                descriptor = os.open(
                    absolute_path,
                    os.O_CREAT | os.O_EXCL | os.O_RDWR,
                    0o600,
                )
            except FileExistsError:
                pass
            else:
                os.close(descriptor)
            self._require_current_process()
            # The continuous legacy registry still has event-store-owned parents in
            # migrations 1 and 4.  Bootstrap that shared schema through its sole owner,
            # then use an independent connection for all native-IM transactions.
            with SQLiteEventStore(path, clock=clock):
                pass
            self._require_current_process()
            connection = sqlite3.connect(
                path,
                check_same_thread=False,
                isolation_level=None,
                timeout=busy_timeout_ms / 1_000,
            )
            self._connection = connection
            connection.row_factory = sqlite3.Row
            self._initialize(busy_timeout_ms)
        except BaseException:
            if self._process_is_current() and connection is not None:
                connection.close()
            raise

    def _require_current_process(self) -> None:
        _process_identity.require_current_process(
            self._process_owner,
            NativeIMNonceStoreProcessMismatchError,
        )

    def _process_is_current(self) -> bool:
        try:
            self._require_current_process()
        except NativeIMNonceStoreProcessMismatchError:
            return False
        return True

    def _quarantine_if_inherited(self) -> None:
        if not self._process_is_current():
            _NATIVE_IM_NONCE_STORE_QUARANTINE.append(self)

    def __del__(self) -> None:
        try:
            self._quarantine_if_inherited()
        except BaseException:
            pass

    def _require_operational(self) -> None:
        self._require_current_process()
        if self._closed:
            raise NativeIMNonceStoreClosedError() from None
        if self._poisoned:
            raise NativeIMNonceStorePoisonedError() from None

    def _initialize(self, busy_timeout_ms: int) -> None:
        self._require_current_process()
        connection = self._connection
        connection.execute("PRAGMA busy_timeout=%d" % busy_timeout_ms)
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA foreign_keys=ON")
        journal = connection.execute("PRAGMA journal_mode=WAL").fetchone()
        if journal is None or str(journal[0]).lower() != "wal":
            raise RuntimeError("SQLite refused WAL journal mode")
        connection.execute("PRAGMA synchronous=FULL")
        self._require_current_process()
        apply_sqlite_migrations(
            connection,
            clock=self._clock,
            _process_guard=self._require_current_process,
        )
        self._require_current_process()

    def _begin_write_transaction(self, connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")

    def _commit_write_transaction(self, connection: sqlite3.Connection) -> None:
        connection.execute("COMMIT")

    def _rollback_write_transaction(self, connection: sqlite3.Connection) -> None:
        connection.execute("ROLLBACK")

    def _transaction_open(self, connection: sqlite3.Connection) -> bool:
        state = connection.in_transaction
        if type(state) is not bool:
            raise RuntimeError("SQLite returned a non-boolean transaction state")
        return state

    def _confirmed_rollback(self, connection: sqlite3.Connection) -> bool:
        if self._transaction_open(connection):
            self._rollback_write_transaction(connection)
        return not self._transaction_open(connection)

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        self._require_operational()
        lock = self._lock
        lock.acquire()
        try:
            self._require_operational()
            connection = self._connection
            try:
                self._begin_write_transaction(connection)
                self._require_current_process()
            except BaseException as begin_error:
                if not self._process_is_current():
                    raise
                rolled_back = False
                try:
                    rolled_back = self._confirmed_rollback(connection)
                except BaseException as rollback_error:
                    _detach_exception(rollback_error)
                _detach_exception(begin_error)
                if not rolled_back:
                    self._poisoned = True
                    raise _NonceTransactionSignal("ambiguous") from None
                raise _NonceTransactionSignal("transaction") from None
            try:
                yield connection
            except BaseException as body_error:
                if not self._process_is_current():
                    raise
                rolled_back = False
                try:
                    rolled_back = self._confirmed_rollback(connection)
                except BaseException as rollback_error:
                    _detach_exception(rollback_error)
                if not rolled_back:
                    self._poisoned = True
                    _detach_exception(body_error)
                    raise _NonceTransactionSignal("ambiguous") from None
                raise
            else:
                self._require_current_process()
                try:
                    self._commit_write_transaction(connection)
                    self._require_current_process()
                except BaseException as commit_error:
                    if not self._process_is_current():
                        raise
                    rolled_back = False
                    transaction_was_open = False
                    try:
                        transaction_was_open = self._transaction_open(connection)
                        if transaction_was_open:
                            rolled_back = self._confirmed_rollback(connection)
                    except BaseException as rollback_error:
                        _detach_exception(rollback_error)
                    _detach_exception(commit_error)
                    if transaction_was_open and rolled_back:
                        raise _NonceTransactionSignal("transaction") from None
                    self._poisoned = True
                    raise _NonceTransactionSignal("ambiguous") from None
        finally:
            if self._process_is_current():
                lock.release()

    @staticmethod
    def _validated_row(row: sqlite3.Row) -> Tuple[str, ...]:
        try:
            scope = (
                _id(row["tenant_id"], "persisted tenantId"),
                _id(row["workspace_id"], "persisted workspaceId"),
                _id(row["provider"], "persisted provider"),
                _id(row["channel_id"], "persisted channelId"),
            )
            key_id = _id(row["key_id"], "persisted keyId")
            nonce_digest = _digest(row["nonce_digest"], "persisted nonceDigest")
            signed_at = _timestamp(row["signed_at"], "persisted signedAt")
            expires_at = _timestamp(row["expires_at"], "persisted expiresAt")
            if expires_at <= signed_at:
                raise ValueError("persisted expiry does not follow signed time")
            evidence_digest = _digest(
                row["authentication_evidence_digest"],
                "persisted authenticationEvidenceDigest",
            )
            profile_revision = _id(row["profile_revision"], "persisted profileRevision")
            profile_digest = _digest(row["profile_digest"], "persisted profileDigest")
            claimed_at = _timestamp(row["claimed_at"], "persisted claimedAt")
        except (IndexError, KeyError, TypeError, ValueError):
            raise NativeIMNonceIntegrityError() from None
        return (
            *scope,
            key_id,
            nonce_digest,
            signed_at,
            expires_at,
            evidence_digest,
            profile_revision,
            profile_digest,
            claimed_at,
        )

    def _claim_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        scope: Tuple[str, str, str, str],
        key_id: str,
        nonce_digest: str,
        signed_at: str,
        expires_at: str,
        authentication_evidence_digest: str,
    ) -> bool:
        identity = (*scope, key_id, nonce_digest)
        row = connection.execute(
            """
            SELECT * FROM native_im_auth_nonces
            WHERE tenant_id = ? AND workspace_id = ? AND provider = ?
              AND channel_id = ? AND key_id = ? AND nonce_digest = ?
            """,
            identity,
        ).fetchone()
        expected_binding = (
            *identity,
            signed_at,
            expires_at,
            authentication_evidence_digest,
            self._profile_revision,
            self._profile_digest,
        )
        if row is not None:
            persisted = self._validated_row(row)
            if persisted[:-1] != expected_binding:
                raise NativeIMNonceIntegrityError() from None
            return False

        claimed_at_raw = self._clock()
        self._require_current_process()
        claimed_at = _timestamp(claimed_at_raw, "clock")
        cursor = connection.execute(
            """
            INSERT INTO native_im_auth_nonces (
                tenant_id, workspace_id, provider, channel_id, key_id,
                nonce_digest, signed_at, expires_at,
                authentication_evidence_digest, profile_revision,
                profile_digest, claimed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(
                tenant_id, workspace_id, provider, channel_id, key_id, nonce_digest
            ) DO NOTHING
            """,
            (*expected_binding, claimed_at),
        )
        if cursor.rowcount != 1:
            raise NativeIMNonceIntegrityError() from None
        readback = connection.execute(
            """
            SELECT * FROM native_im_auth_nonces
            WHERE tenant_id = ? AND workspace_id = ? AND provider = ?
              AND channel_id = ? AND key_id = ? AND nonce_digest = ?
            """,
            identity,
        ).fetchone()
        if readback is None:
            raise NativeIMNonceIntegrityError() from None
        if self._validated_row(readback) != (*expected_binding, claimed_at):
            raise NativeIMNonceIntegrityError() from None
        return True

    def claim(
        self,
        *,
        scope: tuple[str, str, str, str],
        key_id: str,
        nonce_digest: str,
        signed_at: str,
        expires_at: str,
        authentication_evidence_digest: str,
    ) -> bool:
        """Claim one profile-bound nonce, returning ``True`` only after COMMIT ACK."""

        self._require_operational()
        scope_value = _scope_snapshot(scope)
        key_id_value = _id(key_id, "keyId")
        nonce_digest_value = _digest(nonce_digest, "nonceDigest")
        signed_at_value = _timestamp(signed_at, "signedAt")
        expires_at_value = _timestamp(expires_at, "expiresAt")
        if expires_at_value <= signed_at_value:
            raise ValueError("expiresAt must follow signedAt")
        evidence_digest_value = _digest(
            authentication_evidence_digest,
            "authenticationEvidenceDigest",
        )
        result: Optional[bool] = None
        fixed_error_kind: Optional[str] = None
        try:
            with self._write_transaction() as connection:
                result = self._claim_in_transaction(
                    connection,
                    scope=scope_value,
                    key_id=key_id_value,
                    nonce_digest=nonce_digest_value,
                    signed_at=signed_at_value,
                    expires_at=expires_at_value,
                    authentication_evidence_digest=evidence_digest_value,
                )
        except _NonceTransactionSignal as error:
            fixed_error_kind = error.kind
            _detach_exception(error)
        except sqlite3.Error as error:
            fixed_error_kind = "integrity"
            _detach_exception(error)
        if fixed_error_kind is not None:
            _raise_clean_error(fixed_error_kind)
        if type(result) is not bool:  # pragma: no cover - completed bodies assign bool.
            raise RuntimeError("native IM nonce claim completed without a result")
        return result

    def close(self) -> None:
        """Close this process-owned store; repeated current-process close is harmless."""

        self._require_current_process()
        if self._closed:
            return
        with self._lock:
            self._require_current_process()
            if self._closed:
                return
            self._connection.close()
            self._closed = True

    def __enter__(self) -> SQLiteNativeIMInboxStore:
        self._require_operational()
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        fingerprint = hashlib.sha256(
            (self._profile_revision + "\n" + self._profile_digest).encode("utf-8")
        ).hexdigest()[:16]
        return f"{type(self).__name__}(fingerprint={fingerprint!r})"

    def __copy__(self) -> NoReturn:
        raise TypeError("native IM inbox stores cannot be copied")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("native IM inbox stores cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("native IM inbox stores cannot be serialized")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("native IM inbox stores cannot be serialized")


class SQLiteNativeIMNonceReplayGuard(SQLiteNativeIMInboxStore):
    """Compatibility name implementing :class:`NativeIMNonceReplayGuardPort`."""


__all__ = [
    "NativeIMNonceCommitAmbiguityError",
    "NativeIMNonceIntegrityError",
    "NativeIMNonceStoreClosedError",
    "NativeIMNonceStorePoisonedError",
    "NativeIMNonceStoreProcessMismatchError",
    "NativeIMNonceTransactionError",
    "SQLiteNativeIMInboxStore",
    "SQLiteNativeIMNonceReplayGuard",
]
