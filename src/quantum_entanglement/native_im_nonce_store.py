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
from dataclasses import dataclass
from typing import Any, Callable, Iterator, NoReturn, Optional, SupportsIndex, Tuple

from . import process_identity as _process_identity
from ._native_im_codec import NATIVE_IM_SCHEMA_VERSION, _digest, _id, _model_digest, _timestamp
from .migrations import apply_sqlite_migrations
from .native_im import (
    IMCapabilitySnapshotV1,
    IMInboundPageV1,
    IMInboundReadRequestV1,
    IMVerifiedInboundEnvelopeV1,
    InboundIMEventV1,
)
from .native_im_auth import NativeIMRawVerificationResultV1
from .native_im_inbox import (
    NativeIMInboundCheckpointConflictError,
    NativeIMInboundCheckpointV1,
    NativeIMInboundCommitAmbiguityError,
    NativeIMInboundConflictError,
    NativeIMInboundPageAdmissionResultV1,
    NativeIMInboundReadPreparationV1,
    NativeIMInboundTransactionError,
    NativeIMInboxEventReceiptV1,
    NativeIMScopeV1,
)
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


class NativeIMInboxStoreIntegrityError(NativeIMInboundConflictError):
    """Durable native-IM inbox rows are partial, malformed, or contradictory."""

    code = "native_im_inbox_store_integrity_failed"

    def __init__(self) -> None:
        super().__init__("native IM inbox state is not one canonical durable graph")


class _NativeIMStoreTransactionSignal(BaseException):
    """Private, data-free transaction outcome signal."""

    __slots__ = ("kind",)

    def __init__(self, kind: str) -> None:
        super().__init__("native IM store transaction signal")
        self.kind = kind


_NATIVE_IM_NONCE_STORE_QUARANTINE: list[object] = []

_INBOUND_READ_COLUMNS = (
    "tenant_id",
    "workspace_id",
    "provider",
    "channel_id",
    "read_request_id",
    "read_request_digest",
    "request_json",
    "base_checkpoint_revision",
    "after_cursor",
    "after_sequence",
    "request_snapshot_token",
    "status",
    "prepared_at",
    "page_digest",
    "response_snapshot_token",
    "next_cursor",
    "next_sequence",
    "continuation_snapshot_token",
    "has_more",
    "envelope_count",
    "event_manifest_sha256",
    "capability_revision",
    "capability_digest",
    "admitted_checkpoint_revision",
    "admitted_at",
)

_CHECKPOINT_COLUMNS = (
    "tenant_id",
    "workspace_id",
    "provider",
    "channel_id",
    "after_cursor",
    "after_sequence",
    "continuation_snapshot_token",
    "checkpoint_revision",
    "last_read_request_digest",
    "last_page_digest",
    "updated_at",
)

_INBOX_EVENT_COLUMNS = (
    "tenant_id",
    "workspace_id",
    "provider",
    "channel_id",
    "event_id",
    "event_digest",
    "event_json",
    "cursor",
    "sequence_number",
    "first_received_at",
    "admitted_at",
)

_INBOX_VERIFICATION_COLUMNS = (
    "tenant_id",
    "workspace_id",
    "provider",
    "channel_id",
    "verification_id",
    "event_id",
    "event_digest",
    "envelope_digest",
    "verifier_id",
    "authentication_evidence_digest",
    "tenant_mapping_revision",
    "verified_at",
    "traceparent",
    "admitted_at",
)

_INBOUND_READ_EVENT_COLUMNS = (
    "tenant_id",
    "workspace_id",
    "provider",
    "channel_id",
    "read_request_digest",
    "ordinal",
    "event_id",
    "verification_id",
    "envelope_digest",
)


@dataclass(frozen=True)
class _NativeIMInboundReadRecord:
    request: IMInboundReadRequestV1
    read_request_digest: str
    base_checkpoint_revision: int
    status: str
    prepared_at: str
    page_digest: Optional[str]
    response_snapshot_token: Optional[str]
    next_cursor: Optional[str]
    next_sequence: Optional[int]
    continuation_snapshot_token: Optional[str]
    has_more: Optional[bool]
    envelope_count: Optional[int]
    event_manifest_sha256: Optional[str]
    capability_revision: Optional[str]
    capability_digest: Optional[str]
    admitted_checkpoint_revision: Optional[int]
    admitted_at: Optional[str]


@dataclass(frozen=True)
class _NativeIMInboxEventRecord:
    event: InboundIMEventV1
    event_digest: str
    admitted_at: str


@dataclass(frozen=True)
class _NativeIMInboxVerificationRecord:
    verification_id: str
    event_id: str
    event_digest: str
    envelope_digest: str
    verifier_id: str
    authentication_evidence_digest: str
    tenant_mapping_revision: str
    verified_at: str
    traceparent: Optional[str]
    admitted_at: str


@dataclass(frozen=True)
class _NativeIMInboundReadEventRecord:
    ordinal: int
    event_id: str
    verification_id: str
    envelope_digest: str


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


def _persisted_integer(value: object, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum or value > (1 << 63) - 1:
        raise ValueError(f"persisted {label} is not a supported SQLite integer")
    return value


def _persisted_optional_id(value: object, label: str) -> Optional[str]:
    if value is None:
        return None
    return _id(value, label)


def _persisted_optional_digest(value: object, label: str) -> Optional[str]:
    if value is None:
        return None
    return _digest(value, label)


def _persisted_optional_integer(value: object, label: str) -> Optional[int]:
    if value is None:
        return None
    return _persisted_integer(value, label)


def _raw_verification_snapshot(
    value: object,
) -> NativeIMRawVerificationResultV1:
    if type(value) is not NativeIMRawVerificationResultV1:
        raise TypeError("raw verification must be an exact NativeIMRawVerificationResultV1")
    return NativeIMRawVerificationResultV1(
        schema_version=value.schema_version,
        verifier_id=value.verifier_id,
        key_id=value.key_id,
        signed_at=value.signed_at,
        expires_at=value.expires_at,
        verified_at=value.verified_at,
        body_digest=value.body_digest,
        nonce_digest=value.nonce_digest,
        authentication_evidence_digest=value.authentication_evidence_digest,
    )


def _validate_page_authentication_binding(
    page: IMInboundPageV1,
    verification: NativeIMRawVerificationResultV1,
) -> None:
    for envelope in page.envelopes:
        if (
            envelope.verifier_id != verification.verifier_id
            or envelope.authentication_evidence_digest
            != verification.authentication_evidence_digest
            or envelope.verified_at != verification.verified_at
        ):
            raise NativeIMInboundConflictError(
                "native IM page envelopes do not match raw verification evidence"
            )


def _event_manifest_digest(
    page: IMInboundPageV1,
    verification: NativeIMRawVerificationResultV1,
) -> str:
    return _model_digest(
        "NativeIMInboundEventManifestV1",
        {
            "authenticationEvidenceDigest": verification.authentication_evidence_digest,
            "bodyDigest": verification.body_digest,
            "envelopes": [
                {
                    "envelopeDigest": envelope.canonical_digest(),
                    "eventDigest": envelope.event_digest,
                    "eventId": envelope.event.event_id,
                    "ordinal": ordinal,
                    "verificationId": envelope.verification_id,
                }
                for ordinal, envelope in enumerate(page.envelopes)
            ],
            "nonceDigest": verification.nonce_digest,
            "pageDigest": page.canonical_digest(),
            "readRequestDigest": page.read_request_digest,
            "schemaVersion": NATIVE_IM_SCHEMA_VERSION,
            "verifiedAt": verification.verified_at,
            "verifierId": verification.verifier_id,
        },
    )


def _raise_clean_inbound_error(kind: str) -> NoReturn:
    error_type: type[BaseException]
    if kind == "integrity":
        error_type = NativeIMInboxStoreIntegrityError
    elif kind == "transaction":
        error_type = NativeIMInboundTransactionError
    elif kind == "ambiguous":
        error_type = NativeIMInboundCommitAmbiguityError
    else:  # pragma: no cover - private callers use a closed enum.
        raise RuntimeError("unsupported native IM inbound error kind") from None
    try:
        raise error_type() from None
    except BaseException as public_error:
        if type(public_error) is error_type:
            public_error.__context__ = None
        raise


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
                    raise _NativeIMStoreTransactionSignal("ambiguous") from None
                raise _NativeIMStoreTransactionSignal("transaction") from None
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
                    raise _NativeIMStoreTransactionSignal("ambiguous") from None
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
                        raise _NativeIMStoreTransactionSignal("transaction") from None
                    self._poisoned = True
                    raise _NativeIMStoreTransactionSignal("ambiguous") from None
        finally:
            if self._process_is_current():
                lock.release()

    @staticmethod
    def _validated_nonce_row(row: sqlite3.Row) -> Tuple[str, ...]:
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

    def _claim_nonce_in_transaction(
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
            persisted = self._validated_nonce_row(row)
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
        if self._validated_nonce_row(readback) != (*expected_binding, claimed_at):
            raise NativeIMNonceIntegrityError() from None
        return True

    @staticmethod
    def _validated_inbound_read_row(row: sqlite3.Row) -> _NativeIMInboundReadRecord:
        try:
            if tuple(row.keys()) != _INBOUND_READ_COLUMNS:
                raise ValueError("persisted inbound read columns differ")
            tenant_id = _id(row["tenant_id"], "persisted tenantId")
            workspace_id = _id(row["workspace_id"], "persisted workspaceId")
            provider = _id(row["provider"], "persisted provider")
            channel_id = _id(row["channel_id"], "persisted channelId")
            read_request_id = _id(row["read_request_id"], "persisted readRequestId")
            read_request_digest = _digest(
                row["read_request_digest"],
                "persisted readRequestDigest",
            )
            request_json = row["request_json"]
            if type(request_json) is not str:
                raise TypeError("persisted requestJson must use SQLite TEXT storage")
            request_bytes = request_json.encode("utf-8")
            request = IMInboundReadRequestV1.from_json_bytes(request_bytes)
            if request.canonical_bytes() != request_bytes:
                raise ValueError("persisted requestJson is not canonical")
            if request.canonical_digest() != read_request_digest:
                raise ValueError("persisted request digest differs")
            if (
                request.tenant_id,
                request.workspace_id,
                request.provider,
                request.channel_id,
                request.read_request_id,
            ) != (
                tenant_id,
                workspace_id,
                provider,
                channel_id,
                read_request_id,
            ):
                raise ValueError("persisted request identity differs")
            after_cursor = _persisted_optional_id(
                row["after_cursor"],
                "persisted afterCursor",
            )
            after_sequence = _persisted_optional_integer(
                row["after_sequence"],
                "persisted afterSequence",
            )
            request_snapshot_token = _persisted_optional_id(
                row["request_snapshot_token"],
                "persisted requestSnapshotToken",
            )
            if (
                request.after_cursor,
                request.after_sequence,
                request.snapshot_token,
            ) != (after_cursor, after_sequence, request_snapshot_token):
                raise ValueError("persisted request resume fields differ")
            base_checkpoint_revision = _persisted_integer(
                row["base_checkpoint_revision"],
                "baseCheckpointRevision",
            )
            status = row["status"]
            if type(status) is not str or status not in {"prepared", "admitted"}:
                raise ValueError("persisted inbound read status is invalid")
            prepared_at = _timestamp(row["prepared_at"], "persisted preparedAt")
            page_digest = _persisted_optional_digest(
                row["page_digest"],
                "persisted pageDigest",
            )
            response_snapshot_token = _persisted_optional_id(
                row["response_snapshot_token"],
                "persisted responseSnapshotToken",
            )
            next_cursor = _persisted_optional_id(
                row["next_cursor"],
                "persisted nextCursor",
            )
            next_sequence = _persisted_optional_integer(
                row["next_sequence"],
                "persisted nextSequence",
            )
            continuation_snapshot_token = _persisted_optional_id(
                row["continuation_snapshot_token"],
                "persisted continuationSnapshotToken",
            )
            has_more_value = row["has_more"]
            if has_more_value is None:
                has_more = None
            elif type(has_more_value) is int and has_more_value in {0, 1}:
                has_more = bool(has_more_value)
            else:
                raise ValueError("persisted hasMore is invalid")
            envelope_count = _persisted_optional_integer(
                row["envelope_count"],
                "persisted envelopeCount",
            )
            if envelope_count is not None and envelope_count > 1_000:
                raise ValueError("persisted envelopeCount exceeds its bound")
            event_manifest_sha256 = _persisted_optional_digest(
                row["event_manifest_sha256"],
                "persisted eventManifestSha256",
            )
            capability_revision = _persisted_optional_id(
                row["capability_revision"],
                "persisted capabilityRevision",
            )
            capability_digest = _persisted_optional_digest(
                row["capability_digest"],
                "persisted capabilityDigest",
            )
            admitted_checkpoint_revision = _persisted_optional_integer(
                row["admitted_checkpoint_revision"],
                "persisted admittedCheckpointRevision",
            )
            admitted_at_value = row["admitted_at"]
            admitted_at = (
                None
                if admitted_at_value is None
                else _timestamp(admitted_at_value, "persisted admittedAt")
            )

            admitted_values = (
                page_digest,
                response_snapshot_token,
                has_more,
                envelope_count,
                event_manifest_sha256,
                capability_revision,
                capability_digest,
                admitted_checkpoint_revision,
                admitted_at,
            )
            if status == "prepared":
                if any(value is not None for value in admitted_values) or any(
                    value is not None
                    for value in (
                        next_cursor,
                        next_sequence,
                        continuation_snapshot_token,
                    )
                ):
                    raise ValueError("prepared inbound read carries admitted fields")
            else:
                if any(value is None for value in admitted_values):
                    raise ValueError("admitted inbound read lacks required fields")
                if (next_cursor is None) != (next_sequence is None):
                    raise ValueError("admitted next cursor pair is partial")
                if admitted_checkpoint_revision != base_checkpoint_revision + 1:
                    raise ValueError("admitted checkpoint revision is not consecutive")
                if (
                    request.snapshot_token is not None
                    and response_snapshot_token != request.snapshot_token
                ):
                    raise ValueError("admitted page changed its requested snapshot")
                if envelope_count is not None and envelope_count > request.limit:
                    raise ValueError("admitted page exceeds the requested limit")
                if envelope_count and next_cursor is None:
                    raise ValueError("non-empty admitted page lacks its next cursor pair")
                if (
                    envelope_count
                    and request.after_sequence is not None
                    and (next_sequence is None or next_sequence <= request.after_sequence)
                ):
                    raise ValueError("admitted page does not advance its sequence")
                if has_more:
                    if (
                        continuation_snapshot_token != response_snapshot_token
                        or next_cursor is None
                        or envelope_count == 0
                    ):
                        raise ValueError("continuing page state is contradictory")
                elif continuation_snapshot_token is not None:
                    raise ValueError("terminal page retained a continuation snapshot")
                if envelope_count == 0 and (
                    next_cursor,
                    next_sequence,
                ) != (request.after_cursor, request.after_sequence):
                    raise ValueError("empty page changed its resume cursor")
        except (
            IndexError,
            KeyError,
            TypeError,
            UnicodeError,
            ValueError,
        ):
            raise NativeIMInboxStoreIntegrityError() from None
        return _NativeIMInboundReadRecord(
            request=request,
            read_request_digest=read_request_digest,
            base_checkpoint_revision=base_checkpoint_revision,
            status=status,
            prepared_at=prepared_at,
            page_digest=page_digest,
            response_snapshot_token=response_snapshot_token,
            next_cursor=next_cursor,
            next_sequence=next_sequence,
            continuation_snapshot_token=continuation_snapshot_token,
            has_more=has_more,
            envelope_count=envelope_count,
            event_manifest_sha256=event_manifest_sha256,
            capability_revision=capability_revision,
            capability_digest=capability_digest,
            admitted_checkpoint_revision=admitted_checkpoint_revision,
            admitted_at=admitted_at,
        )

    @staticmethod
    def _validated_inbox_event_row(row: sqlite3.Row) -> _NativeIMInboxEventRecord:
        try:
            if tuple(row.keys()) != _INBOX_EVENT_COLUMNS:
                raise ValueError("persisted native IM event columns differ")
            scope = (
                _id(row["tenant_id"], "persisted event tenantId"),
                _id(row["workspace_id"], "persisted event workspaceId"),
                _id(row["provider"], "persisted event provider"),
                _id(row["channel_id"], "persisted event channelId"),
            )
            event_id = _id(row["event_id"], "persisted eventId")
            event_digest = _digest(row["event_digest"], "persisted eventDigest")
            event_json = row["event_json"]
            if type(event_json) is not str:
                raise TypeError("persisted eventJson must use SQLite TEXT storage")
            event_bytes = event_json.encode("utf-8")
            event = InboundIMEventV1.from_json_bytes(event_bytes)
            if event.canonical_bytes() != event_bytes:
                raise ValueError("persisted eventJson is not canonical")
            if event.canonical_digest() != event_digest or event.event_id != event_id:
                raise ValueError("persisted event identity or digest differs")
            if (
                event.conversation.tenant_id,
                event.conversation.workspace_id,
                event.conversation.provider,
                event.conversation.channel_id,
            ) != scope:
                raise ValueError("persisted event scope differs")
            if _id(row["cursor"], "persisted event cursor") != event.cursor:
                raise ValueError("persisted event cursor differs")
            if (
                _persisted_integer(row["sequence_number"], "event sequenceNumber")
                != event.sequence_number
            ):
                raise ValueError("persisted event sequence differs")
            if (
                _timestamp(row["first_received_at"], "persisted event firstReceivedAt")
                != event.first_received_at
            ):
                raise ValueError("persisted event firstReceivedAt differs")
            admitted_at = _timestamp(row["admitted_at"], "persisted event admittedAt")
        except (IndexError, KeyError, TypeError, UnicodeError, ValueError):
            raise NativeIMInboxStoreIntegrityError() from None
        return _NativeIMInboxEventRecord(
            event=event,
            event_digest=event_digest,
            admitted_at=admitted_at,
        )

    @staticmethod
    def _validated_inbox_verification_row(
        row: sqlite3.Row,
    ) -> _NativeIMInboxVerificationRecord:
        try:
            if tuple(row.keys()) != _INBOX_VERIFICATION_COLUMNS:
                raise ValueError("persisted native IM verification columns differ")
            for column, label in (
                ("tenant_id", "persisted verification tenantId"),
                ("workspace_id", "persisted verification workspaceId"),
                ("provider", "persisted verification provider"),
                ("channel_id", "persisted verification channelId"),
            ):
                _id(row[column], label)
            verification_id = _id(
                row["verification_id"],
                "persisted verificationId",
            )
            event_id = _id(row["event_id"], "persisted verification eventId")
            event_digest = _digest(
                row["event_digest"],
                "persisted verification eventDigest",
            )
            envelope_digest = _digest(
                row["envelope_digest"],
                "persisted envelopeDigest",
            )
            verifier_id = _id(row["verifier_id"], "persisted verifierId")
            evidence_digest = _digest(
                row["authentication_evidence_digest"],
                "persisted authenticationEvidenceDigest",
            )
            mapping_revision = _id(
                row["tenant_mapping_revision"],
                "persisted tenantMappingRevision",
            )
            verified_at = _timestamp(row["verified_at"], "persisted verifiedAt")
            traceparent = row["traceparent"]
            if traceparent is not None and type(traceparent) is not str:
                raise TypeError("persisted traceparent must use SQLite TEXT storage")
            admitted_at = _timestamp(
                row["admitted_at"],
                "persisted verification admittedAt",
            )
        except (IndexError, KeyError, TypeError, ValueError):
            raise NativeIMInboxStoreIntegrityError() from None
        return _NativeIMInboxVerificationRecord(
            verification_id=verification_id,
            event_id=event_id,
            event_digest=event_digest,
            envelope_digest=envelope_digest,
            verifier_id=verifier_id,
            authentication_evidence_digest=evidence_digest,
            tenant_mapping_revision=mapping_revision,
            verified_at=verified_at,
            traceparent=traceparent,
            admitted_at=admitted_at,
        )

    @staticmethod
    def _validated_inbound_read_event_row(
        row: sqlite3.Row,
    ) -> _NativeIMInboundReadEventRecord:
        try:
            if tuple(row.keys()) != _INBOUND_READ_EVENT_COLUMNS:
                raise ValueError("persisted native IM read-event columns differ")
            for column, label in (
                ("tenant_id", "persisted read-event tenantId"),
                ("workspace_id", "persisted read-event workspaceId"),
                ("provider", "persisted read-event provider"),
                ("channel_id", "persisted read-event channelId"),
            ):
                _id(row[column], label)
            _digest(row["read_request_digest"], "persisted readRequestDigest")
            ordinal = _persisted_integer(row["ordinal"], "read-event ordinal")
            if ordinal >= 1_000:
                raise ValueError("persisted read-event ordinal exceeds its bound")
            event_id = _id(row["event_id"], "persisted read-event eventId")
            verification_id = _id(
                row["verification_id"],
                "persisted read-event verificationId",
            )
            envelope_digest = _digest(
                row["envelope_digest"],
                "persisted read-event envelopeDigest",
            )
        except (IndexError, KeyError, TypeError, ValueError):
            raise NativeIMInboxStoreIntegrityError() from None
        return _NativeIMInboundReadEventRecord(
            ordinal=ordinal,
            event_id=event_id,
            verification_id=verification_id,
            envelope_digest=envelope_digest,
        )

    def _load_inbound_checkpoint(
        self,
        connection: sqlite3.Connection,
        scope: NativeIMScopeV1,
    ) -> Optional[NativeIMInboundCheckpointV1]:
        admitted_summary = connection.execute(
            """
            SELECT COUNT(*) AS row_count,
                   MAX(admitted_checkpoint_revision) AS maximum_revision
            FROM native_im_inbound_reads
            WHERE tenant_id = ? AND workspace_id = ?
              AND provider = ? AND channel_id = ? AND status = 'admitted'
            """,
            (
                scope.tenant_id,
                scope.workspace_id,
                scope.provider,
                scope.channel_id,
            ),
        ).fetchone()
        if admitted_summary is None:
            raise NativeIMInboxStoreIntegrityError() from None
        try:
            admitted_count = _persisted_integer(
                admitted_summary["row_count"],
                "admitted read count",
            )
            maximum_revision = _persisted_optional_integer(
                admitted_summary["maximum_revision"],
                "maximum admitted checkpoint revision",
            )
            if (admitted_count == 0) != (maximum_revision is None):
                raise ValueError("admitted read summary is contradictory")
            if maximum_revision is not None and admitted_count != maximum_revision:
                raise ValueError("admitted checkpoint revisions are not contiguous")
        except (IndexError, KeyError, TypeError, ValueError):
            raise NativeIMInboxStoreIntegrityError() from None

        row = connection.execute(
            """
            SELECT * FROM native_im_inbound_checkpoints
            WHERE tenant_id = ? AND workspace_id = ?
              AND provider = ? AND channel_id = ?
            """,
            (
                scope.tenant_id,
                scope.workspace_id,
                scope.provider,
                scope.channel_id,
            ),
        ).fetchone()
        if row is None:
            if admitted_count:
                raise NativeIMInboxStoreIntegrityError() from None
            return None
        try:
            if tuple(row.keys()) != _CHECKPOINT_COLUMNS:
                raise ValueError("persisted checkpoint columns differ")
            persisted_scope = NativeIMScopeV1(
                schema_version=NATIVE_IM_SCHEMA_VERSION,
                tenant_id=_id(row["tenant_id"], "persisted checkpoint tenantId"),
                workspace_id=_id(
                    row["workspace_id"],
                    "persisted checkpoint workspaceId",
                ),
                provider=_id(row["provider"], "persisted checkpoint provider"),
                channel_id=_id(
                    row["channel_id"],
                    "persisted checkpoint channelId",
                ),
            )
            if persisted_scope != scope:
                raise ValueError("persisted checkpoint scope differs")
            checkpoint = NativeIMInboundCheckpointV1(
                schema_version=NATIVE_IM_SCHEMA_VERSION,
                scope=persisted_scope,
                after_cursor=_persisted_optional_id(
                    row["after_cursor"],
                    "persisted checkpoint afterCursor",
                ),
                after_sequence=_persisted_optional_integer(
                    row["after_sequence"],
                    "persisted checkpoint afterSequence",
                ),
                continuation_snapshot_token=_persisted_optional_id(
                    row["continuation_snapshot_token"],
                    "persisted checkpoint continuationSnapshotToken",
                ),
                checkpoint_revision=_persisted_integer(
                    row["checkpoint_revision"],
                    "checkpointRevision",
                    minimum=1,
                ),
                last_read_request_digest=_digest(
                    row["last_read_request_digest"],
                    "persisted checkpoint lastReadRequestDigest",
                ),
                last_page_digest=_digest(
                    row["last_page_digest"],
                    "persisted checkpoint lastPageDigest",
                ),
                updated_at=_timestamp(
                    row["updated_at"],
                    "persisted checkpoint updatedAt",
                ),
            )
        except (IndexError, KeyError, TypeError, ValueError):
            raise NativeIMInboxStoreIntegrityError() from None

        if maximum_revision != checkpoint.checkpoint_revision:
            raise NativeIMInboxStoreIntegrityError() from None

        read_row = connection.execute(
            """
            SELECT * FROM native_im_inbound_reads
            WHERE tenant_id = ? AND workspace_id = ? AND provider = ?
              AND channel_id = ? AND read_request_digest = ?
            """,
            (
                scope.tenant_id,
                scope.workspace_id,
                scope.provider,
                scope.channel_id,
                checkpoint.last_read_request_digest,
            ),
        ).fetchone()
        if read_row is None:
            raise NativeIMInboxStoreIntegrityError() from None
        record = self._validated_inbound_read_row(read_row)
        if (
            record.status != "admitted"
            or record.page_digest != checkpoint.last_page_digest
            or record.admitted_checkpoint_revision != checkpoint.checkpoint_revision
            or (record.next_cursor, record.next_sequence)
            != (checkpoint.after_cursor, checkpoint.after_sequence)
            or record.continuation_snapshot_token != checkpoint.continuation_snapshot_token
            or record.admitted_at != checkpoint.updated_at
        ):
            raise NativeIMInboxStoreIntegrityError() from None
        return checkpoint

    @staticmethod
    def _checkpoint_matches_request(
        checkpoint: Optional[NativeIMInboundCheckpointV1],
        record: _NativeIMInboundReadRecord,
    ) -> bool:
        if checkpoint is None:
            return record.base_checkpoint_revision == 0 and (
                record.request.after_cursor,
                record.request.after_sequence,
                record.request.snapshot_token,
            ) == (None, None, None)
        return record.base_checkpoint_revision == checkpoint.checkpoint_revision and (
            record.request.after_cursor,
            record.request.after_sequence,
            record.request.snapshot_token,
        ) == (
            checkpoint.after_cursor,
            checkpoint.after_sequence,
            checkpoint.continuation_snapshot_token,
        )

    @staticmethod
    def _checkpoint_from_admitted_record(
        scope: NativeIMScopeV1,
        record: _NativeIMInboundReadRecord,
    ) -> NativeIMInboundCheckpointV1:
        if (
            record.status != "admitted"
            or record.page_digest is None
            or record.admitted_checkpoint_revision is None
            or record.admitted_at is None
        ):
            raise NativeIMInboxStoreIntegrityError() from None
        return NativeIMInboundCheckpointV1(
            schema_version=NATIVE_IM_SCHEMA_VERSION,
            scope=scope,
            after_cursor=record.next_cursor,
            after_sequence=record.next_sequence,
            continuation_snapshot_token=record.continuation_snapshot_token,
            checkpoint_revision=record.admitted_checkpoint_revision,
            last_read_request_digest=record.read_request_digest,
            last_page_digest=record.page_digest,
            updated_at=record.admitted_at,
        )

    def _readback_inbound_page_admission(
        self,
        connection: sqlite3.Connection,
        *,
        request: IMInboundReadRequestV1,
        page: IMInboundPageV1,
        event_manifest_sha256: str,
        disposition: str,
    ) -> NativeIMInboundPageAdmissionResultV1:
        scope = NativeIMScopeV1(
            schema_version=NATIVE_IM_SCHEMA_VERSION,
            tenant_id=request.tenant_id,
            workspace_id=request.workspace_id,
            provider=request.provider,
            channel_id=request.channel_id,
        )
        scope_values = (
            scope.tenant_id,
            scope.workspace_id,
            scope.provider,
            scope.channel_id,
        )
        read_row = connection.execute(
            """
            SELECT * FROM native_im_inbound_reads
            WHERE tenant_id = ? AND workspace_id = ? AND provider = ?
              AND channel_id = ? AND read_request_digest = ?
            """,
            (*scope_values, page.read_request_digest),
        ).fetchone()
        if read_row is None:
            raise NativeIMInboxStoreIntegrityError() from None
        record = self._validated_inbound_read_row(read_row)
        if (
            record.request != request
            or record.status != "admitted"
            or record.page_digest is None
            or record.response_snapshot_token is None
            or record.has_more is None
            or record.envelope_count is None
            or record.event_manifest_sha256 is None
            or record.capability_revision is None
            or record.capability_digest is None
            or record.admitted_checkpoint_revision is None
            or record.admitted_at is None
        ):
            raise NativeIMInboxStoreIntegrityError() from None

        if self._load_inbound_checkpoint(connection, scope) is None:
            raise NativeIMInboxStoreIntegrityError() from None
        checkpoint = self._checkpoint_from_admitted_record(scope, record)
        link_rows = connection.execute(
            """
            SELECT * FROM native_im_inbound_read_events
            WHERE tenant_id = ? AND workspace_id = ? AND provider = ?
              AND channel_id = ? AND read_request_digest = ?
            ORDER BY ordinal
            """,
            (*scope_values, record.read_request_digest),
        ).fetchall()
        if len(link_rows) != record.envelope_count:
            raise NativeIMInboxStoreIntegrityError() from None

        receipts: list[NativeIMInboxEventReceiptV1] = []
        reconstructed_envelopes: list[IMVerifiedInboundEnvelopeV1] = []
        for ordinal, link_row in enumerate(link_rows):
            link = self._validated_inbound_read_event_row(link_row)
            if (
                tuple(link_row[column] for column in _INBOUND_READ_EVENT_COLUMNS[:4])
                != scope_values
                or link_row["read_request_digest"] != record.read_request_digest
                or link.ordinal != ordinal
            ):
                raise NativeIMInboxStoreIntegrityError() from None

            event_row = connection.execute(
                """
                SELECT * FROM native_im_inbox_events
                WHERE tenant_id = ? AND workspace_id = ? AND provider = ?
                  AND channel_id = ? AND event_id = ?
                """,
                (*scope_values, link.event_id),
            ).fetchone()
            verification_row = connection.execute(
                """
                SELECT * FROM native_im_inbox_verifications
                WHERE tenant_id = ? AND workspace_id = ? AND provider = ?
                  AND channel_id = ? AND verification_id = ?
                """,
                (*scope_values, link.verification_id),
            ).fetchone()
            if event_row is None or verification_row is None:
                raise NativeIMInboxStoreIntegrityError() from None
            event_record = self._validated_inbox_event_row(event_row)
            verification_record = self._validated_inbox_verification_row(verification_row)
            if (
                tuple(event_row[column] for column in _INBOX_EVENT_COLUMNS[:4]) != scope_values
                or tuple(verification_row[column] for column in _INBOX_VERIFICATION_COLUMNS[:4])
                != scope_values
                or event_record.event.event_id != link.event_id
                or verification_record.verification_id != link.verification_id
                or verification_record.event_id != event_record.event.event_id
                or verification_record.event_digest != event_record.event_digest
                or verification_record.envelope_digest != link.envelope_digest
                or event_record.admitted_at != record.admitted_at
                or verification_record.admitted_at != record.admitted_at
            ):
                raise NativeIMInboxStoreIntegrityError() from None
            try:
                reconstructed_envelope = IMVerifiedInboundEnvelopeV1(
                    schema_version=NATIVE_IM_SCHEMA_VERSION,
                    event=event_record.event,
                    event_digest=verification_record.event_digest,
                    verification_id=verification_record.verification_id,
                    verifier_id=verification_record.verifier_id,
                    authentication_evidence_digest=(
                        verification_record.authentication_evidence_digest
                    ),
                    tenant_mapping_revision=verification_record.tenant_mapping_revision,
                    verified_at=verification_record.verified_at,
                    traceparent=verification_record.traceparent,
                )
            except (TypeError, UnicodeError, ValueError):
                raise NativeIMInboxStoreIntegrityError() from None
            if reconstructed_envelope.canonical_digest() != link.envelope_digest:
                raise NativeIMInboxStoreIntegrityError() from None
            reconstructed_envelopes.append(reconstructed_envelope)
            receipts.append(
                NativeIMInboxEventReceiptV1(
                    schema_version=NATIVE_IM_SCHEMA_VERSION,
                    scope=scope,
                    event_id=event_record.event.event_id,
                    event_digest=event_record.event_digest,
                    cursor=event_record.event.cursor,
                    sequence_number=event_record.event.sequence_number,
                    first_received_at=event_record.event.first_received_at,
                    admitted_at=event_record.admitted_at,
                )
            )

        try:
            persisted_page = IMInboundPageV1(
                schema_version=NATIVE_IM_SCHEMA_VERSION,
                tenant_id=scope.tenant_id,
                workspace_id=scope.workspace_id,
                provider=scope.provider,
                channel_id=scope.channel_id,
                read_request_id=record.request.read_request_id,
                read_request_digest=record.read_request_digest,
                snapshot_token=record.response_snapshot_token,
                envelopes=tuple(reconstructed_envelopes),
                next_cursor=record.next_cursor,
                next_sequence=record.next_sequence,
                has_more=record.has_more,
                capability_revision=record.capability_revision,
                capability_digest=record.capability_digest,
            )
            persisted_page.validate_request_binding(record.request)
        except (TypeError, UnicodeError, ValueError):
            raise NativeIMInboxStoreIntegrityError() from None
        if persisted_page.canonical_digest() != record.page_digest:
            raise NativeIMInboxStoreIntegrityError() from None
        if persisted_page != page or record.event_manifest_sha256 != event_manifest_sha256:
            raise NativeIMInboundConflictError(
                "native IM read is already bound to a different admitted page"
            )

        return NativeIMInboundPageAdmissionResultV1(
            schema_version=NATIVE_IM_SCHEMA_VERSION,
            scope=scope,
            read_request_id=request.read_request_id,
            read_request_digest=record.read_request_digest,
            page_digest=record.page_digest,
            disposition=disposition,
            checkpoint=checkpoint,
            event_receipts=tuple(receipts),
            admitted_at=record.admitted_at,
        )

    def _insert_inbound_page_envelopes(
        self,
        connection: sqlite3.Connection,
        *,
        scope: NativeIMScopeV1,
        read_request_digest: str,
        page: IMInboundPageV1,
        admitted_at: str,
    ) -> None:
        scope_values = (
            scope.tenant_id,
            scope.workspace_id,
            scope.provider,
            scope.channel_id,
        )
        for ordinal, envelope in enumerate(page.envelopes):
            event = envelope.event
            event_rows = connection.execute(
                """
                SELECT * FROM native_im_inbox_events
                WHERE tenant_id = ? AND workspace_id = ? AND provider = ?
                  AND channel_id = ?
                  AND (event_id = ? OR sequence_number = ?)
                ORDER BY event_id
                """,
                (*scope_values, event.event_id, event.sequence_number),
            ).fetchall()
            if event_rows:
                records = tuple(self._validated_inbox_event_row(row) for row in event_rows)
                if len(records) != 1 or (
                    records[0].event != event or records[0].event_digest != envelope.event_digest
                ):
                    raise NativeIMInboundConflictError(
                        "native IM event identity is already bound differently"
                    )
                raise NativeIMInboxStoreIntegrityError() from None

            verification_row = connection.execute(
                """
                SELECT * FROM native_im_inbox_verifications
                WHERE tenant_id = ? AND workspace_id = ? AND provider = ?
                  AND channel_id = ? AND verification_id = ?
                """,
                (*scope_values, envelope.verification_id),
            ).fetchone()
            if verification_row is not None:
                record = self._validated_inbox_verification_row(verification_row)
                if (
                    record.event_id != event.event_id
                    or record.event_digest != envelope.event_digest
                    or record.envelope_digest != envelope.canonical_digest()
                ):
                    raise NativeIMInboundConflictError(
                        "native IM verification identity is already bound differently"
                    )
                raise NativeIMInboxStoreIntegrityError() from None

            event_cursor = connection.execute(
                """
                INSERT INTO native_im_inbox_events (
                    tenant_id, workspace_id, provider, channel_id,
                    event_id, event_digest, event_json, cursor,
                    sequence_number, first_received_at, admitted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    *scope_values,
                    event.event_id,
                    envelope.event_digest,
                    event.canonical_bytes().decode("utf-8"),
                    event.cursor,
                    event.sequence_number,
                    event.first_received_at,
                    admitted_at,
                ),
            )
            verification_cursor = connection.execute(
                """
                INSERT INTO native_im_inbox_verifications (
                    tenant_id, workspace_id, provider, channel_id,
                    verification_id, event_id, event_digest, envelope_digest,
                    verifier_id, authentication_evidence_digest,
                    tenant_mapping_revision, verified_at, traceparent, admitted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    *scope_values,
                    envelope.verification_id,
                    event.event_id,
                    envelope.event_digest,
                    envelope.canonical_digest(),
                    envelope.verifier_id,
                    envelope.authentication_evidence_digest,
                    envelope.tenant_mapping_revision,
                    envelope.verified_at,
                    envelope.traceparent,
                    admitted_at,
                ),
            )
            link_cursor = connection.execute(
                """
                INSERT INTO native_im_inbound_read_events (
                    tenant_id, workspace_id, provider, channel_id,
                    read_request_digest, ordinal, event_id,
                    verification_id, envelope_digest
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    *scope_values,
                    read_request_digest,
                    ordinal,
                    event.event_id,
                    envelope.verification_id,
                    envelope.canonical_digest(),
                ),
            )
            if (
                event_cursor.rowcount != 1
                or verification_cursor.rowcount != 1
                or link_cursor.rowcount != 1
            ):
                raise NativeIMInboxStoreIntegrityError() from None

    @staticmethod
    def _advance_inbound_checkpoint(
        connection: sqlite3.Connection,
        *,
        scope: NativeIMScopeV1,
        previous: Optional[NativeIMInboundCheckpointV1],
        read_request_digest: str,
        page: IMInboundPageV1,
        page_digest: str,
        admitted_at: str,
    ) -> int:
        scope_values = (
            scope.tenant_id,
            scope.workspace_id,
            scope.provider,
            scope.channel_id,
        )
        continuation = page.snapshot_token if page.has_more else None
        if previous is None:
            revision = 1
            cursor = connection.execute(
                """
                INSERT INTO native_im_inbound_checkpoints (
                    tenant_id, workspace_id, provider, channel_id,
                    after_cursor, after_sequence, continuation_snapshot_token,
                    checkpoint_revision, last_read_request_digest,
                    last_page_digest, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    *scope_values,
                    page.next_cursor,
                    page.next_sequence,
                    continuation,
                    revision,
                    read_request_digest,
                    page_digest,
                    admitted_at,
                ),
            )
        else:
            revision = previous.checkpoint_revision + 1
            cursor = connection.execute(
                """
                UPDATE native_im_inbound_checkpoints
                SET after_cursor = ?, after_sequence = ?,
                    continuation_snapshot_token = ?, checkpoint_revision = ?,
                    last_read_request_digest = ?, last_page_digest = ?, updated_at = ?
                WHERE tenant_id = ? AND workspace_id = ? AND provider = ?
                  AND channel_id = ? AND checkpoint_revision = ?
                  AND after_cursor IS ? AND after_sequence IS ?
                  AND continuation_snapshot_token IS ?
                  AND last_read_request_digest = ? AND last_page_digest = ?
                  AND updated_at = ?
                """,
                (
                    page.next_cursor,
                    page.next_sequence,
                    continuation,
                    revision,
                    read_request_digest,
                    page_digest,
                    admitted_at,
                    *scope_values,
                    previous.checkpoint_revision,
                    previous.after_cursor,
                    previous.after_sequence,
                    previous.continuation_snapshot_token,
                    previous.last_read_request_digest,
                    previous.last_page_digest,
                    previous.updated_at,
                ),
            )
        if cursor.rowcount != 1:
            raise NativeIMInboundCheckpointConflictError(
                "native IM checkpoint changed before page admission"
            )
        return revision

    def _admit_native_im_inbound_page_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        request: IMInboundReadRequestV1,
        page: IMInboundPageV1,
        event_manifest_sha256: str,
        nonce_claimed: bool,
    ) -> NativeIMInboundPageAdmissionResultV1:
        scope = NativeIMScopeV1(
            schema_version=NATIVE_IM_SCHEMA_VERSION,
            tenant_id=request.tenant_id,
            workspace_id=request.workspace_id,
            provider=request.provider,
            channel_id=request.channel_id,
        )
        scope_values = (
            scope.tenant_id,
            scope.workspace_id,
            scope.provider,
            scope.channel_id,
        )
        candidates = connection.execute(
            """
            SELECT * FROM native_im_inbound_reads
            WHERE tenant_id = ? AND workspace_id = ? AND provider = ?
              AND channel_id = ?
              AND (read_request_id = ? OR read_request_digest = ?)
            ORDER BY read_request_digest
            """,
            (*scope_values, request.read_request_id, request.canonical_digest()),
        ).fetchall()
        if len(candidates) > 1:
            raise NativeIMInboundConflictError(
                "native IM read identities are bound to different rows"
            )
        if not candidates:
            raise NativeIMInboundConflictError("native IM page requires an exact prepared read")
        record = self._validated_inbound_read_row(candidates[0])
        if record.request != request or record.read_request_digest != request.canonical_digest():
            raise NativeIMInboundConflictError(
                "native IM read identity is already bound differently"
            )
        if record.status == "admitted":
            return self._readback_inbound_page_admission(
                connection,
                request=request,
                page=page,
                event_manifest_sha256=event_manifest_sha256,
                disposition="observed_replay",
            )
        if nonce_claimed is not True:
            raise NativeIMInboundConflictError(
                "native IM nonce was consumed before its page admission"
            )
        checkpoint = self._load_inbound_checkpoint(connection, scope)
        if not self._checkpoint_matches_request(checkpoint, record):
            raise NativeIMInboundCheckpointConflictError(
                "native IM prepared read no longer matches the durable checkpoint"
            )

        admitted_at_raw = self._clock()
        self._require_current_process()
        admitted_at = _timestamp(admitted_at_raw, "clock")
        page_digest = page.canonical_digest()
        self._insert_inbound_page_envelopes(
            connection,
            scope=scope,
            read_request_digest=record.read_request_digest,
            page=page,
            admitted_at=admitted_at,
        )
        revision = record.base_checkpoint_revision + 1
        continuation = page.snapshot_token if page.has_more else None
        cursor = connection.execute(
            """
            UPDATE native_im_inbound_reads
            SET status = 'admitted', page_digest = ?, response_snapshot_token = ?,
                next_cursor = ?, next_sequence = ?, continuation_snapshot_token = ?,
                has_more = ?, envelope_count = ?, event_manifest_sha256 = ?,
                capability_revision = ?, capability_digest = ?,
                admitted_checkpoint_revision = ?, admitted_at = ?
            WHERE tenant_id = ? AND workspace_id = ? AND provider = ?
              AND channel_id = ? AND read_request_digest = ?
              AND read_request_id = ? AND status = 'prepared'
              AND base_checkpoint_revision = ?
            """,
            (
                page_digest,
                page.snapshot_token,
                page.next_cursor,
                page.next_sequence,
                continuation,
                int(page.has_more),
                len(page.envelopes),
                event_manifest_sha256,
                page.capability_revision,
                page.capability_digest,
                revision,
                admitted_at,
                *scope_values,
                record.read_request_digest,
                request.read_request_id,
                record.base_checkpoint_revision,
            ),
        )
        if cursor.rowcount != 1:
            raise NativeIMInboundCheckpointConflictError(
                "native IM prepared read changed before page admission"
            )
        checkpoint_revision = self._advance_inbound_checkpoint(
            connection,
            scope=scope,
            previous=checkpoint,
            read_request_digest=record.read_request_digest,
            page=page,
            page_digest=page_digest,
            admitted_at=admitted_at,
        )
        if checkpoint_revision != revision:
            raise NativeIMInboxStoreIntegrityError() from None
        return self._readback_inbound_page_admission(
            connection,
            request=request,
            page=page,
            event_manifest_sha256=event_manifest_sha256,
            disposition="fresh_observation",
        )

    def prepare_native_im_inbound_read(
        self,
        request: IMInboundReadRequestV1,
    ) -> NativeIMInboundReadPreparationV1:
        """Persist one capability-free read request without invoking a provider."""

        self._require_operational()
        if type(request) is not IMInboundReadRequestV1:
            raise TypeError("request must be an exact IMInboundReadRequestV1")
        request_bytes = request.canonical_bytes()
        request_snapshot = IMInboundReadRequestV1.from_json_bytes(request_bytes)
        request_digest = request_snapshot.canonical_digest()
        request_json = request_bytes.decode("utf-8")
        scope = NativeIMScopeV1(
            schema_version=NATIVE_IM_SCHEMA_VERSION,
            tenant_id=request_snapshot.tenant_id,
            workspace_id=request_snapshot.workspace_id,
            provider=request_snapshot.provider,
            channel_id=request_snapshot.channel_id,
        )
        scope_values = (
            scope.tenant_id,
            scope.workspace_id,
            scope.provider,
            scope.channel_id,
        )
        result: Optional[NativeIMInboundReadPreparationV1] = None
        fixed_error_kind: Optional[str] = None
        try:
            with self._write_transaction() as connection:
                candidates = connection.execute(
                    """
                    SELECT * FROM native_im_inbound_reads
                    WHERE tenant_id = ? AND workspace_id = ?
                      AND provider = ? AND channel_id = ?
                      AND (read_request_id = ? OR read_request_digest = ?)
                    ORDER BY read_request_digest
                    """,
                    (*scope_values, request_snapshot.read_request_id, request_digest),
                ).fetchall()
                if len(candidates) > 1:
                    raise NativeIMInboundConflictError(
                        "native IM read identities are bound to different rows"
                    )
                if candidates:
                    record = self._validated_inbound_read_row(candidates[0])
                    if (
                        record.read_request_digest != request_digest
                        or record.request != request_snapshot
                    ):
                        raise NativeIMInboundConflictError(
                            "native IM read identity is already bound differently"
                        )
                    checkpoint = self._load_inbound_checkpoint(connection, scope)
                    if record.status == "prepared":
                        if not self._checkpoint_matches_request(checkpoint, record):
                            raise NativeIMInboxStoreIntegrityError() from None
                    elif checkpoint is None:
                        raise NativeIMInboxStoreIntegrityError() from None
                    result = NativeIMInboundReadPreparationV1(
                        schema_version=NATIVE_IM_SCHEMA_VERSION,
                        scope=scope,
                        read_request_id=request_snapshot.read_request_id,
                        read_request_digest=request_digest,
                        base_checkpoint_revision=record.base_checkpoint_revision,
                        read_status=record.status,
                        disposition="observed_replay",
                        prepared_at=record.prepared_at,
                    )
                else:
                    prepared_rows = connection.execute(
                        """
                        SELECT * FROM native_im_inbound_reads
                        WHERE tenant_id = ? AND workspace_id = ?
                          AND provider = ? AND channel_id = ?
                          AND status = 'prepared'
                        """,
                        scope_values,
                    ).fetchall()
                    if len(prepared_rows) > 1:
                        raise NativeIMInboxStoreIntegrityError() from None
                    if prepared_rows:
                        existing_prepared = self._validated_inbound_read_row(prepared_rows[0])
                        checkpoint = self._load_inbound_checkpoint(connection, scope)
                        if not self._checkpoint_matches_request(checkpoint, existing_prepared):
                            raise NativeIMInboxStoreIntegrityError() from None
                        raise NativeIMInboundConflictError(
                            "native IM scope already has a different prepared read"
                        )
                    checkpoint = self._load_inbound_checkpoint(connection, scope)
                    expected_resume: Tuple[Optional[str], Optional[int], Optional[str]]
                    if checkpoint is None:
                        base_checkpoint_revision = 0
                        expected_resume = (None, None, None)
                    else:
                        base_checkpoint_revision = checkpoint.checkpoint_revision
                        expected_resume = (
                            checkpoint.after_cursor,
                            checkpoint.after_sequence,
                            checkpoint.continuation_snapshot_token,
                        )
                    requested_resume = (
                        request_snapshot.after_cursor,
                        request_snapshot.after_sequence,
                        request_snapshot.snapshot_token,
                    )
                    if requested_resume != expected_resume:
                        raise NativeIMInboundCheckpointConflictError(
                            "native IM read does not match the durable checkpoint"
                        )
                    prepared_at_raw = self._clock()
                    self._require_current_process()
                    prepared_at = _timestamp(prepared_at_raw, "clock")
                    inserted = connection.execute(
                        """
                        INSERT INTO native_im_inbound_reads (
                            tenant_id, workspace_id, provider, channel_id,
                            read_request_id, read_request_digest, request_json,
                            base_checkpoint_revision, after_cursor, after_sequence,
                            request_snapshot_token, status, prepared_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?)
                        """,
                        (
                            *scope_values,
                            request_snapshot.read_request_id,
                            request_digest,
                            request_json,
                            base_checkpoint_revision,
                            request_snapshot.after_cursor,
                            request_snapshot.after_sequence,
                            request_snapshot.snapshot_token,
                            prepared_at,
                        ),
                    )
                    if inserted.rowcount != 1:
                        raise NativeIMInboxStoreIntegrityError() from None
                    readback = connection.execute(
                        """
                        SELECT * FROM native_im_inbound_reads
                        WHERE tenant_id = ? AND workspace_id = ?
                          AND provider = ? AND channel_id = ?
                          AND read_request_digest = ?
                        """,
                        (*scope_values, request_digest),
                    ).fetchone()
                    if readback is None:
                        raise NativeIMInboxStoreIntegrityError() from None
                    record = self._validated_inbound_read_row(readback)
                    if (
                        record.request != request_snapshot
                        or record.base_checkpoint_revision != base_checkpoint_revision
                        or record.status != "prepared"
                        or record.prepared_at != prepared_at
                    ):
                        raise NativeIMInboxStoreIntegrityError() from None
                    result = NativeIMInboundReadPreparationV1(
                        schema_version=NATIVE_IM_SCHEMA_VERSION,
                        scope=scope,
                        read_request_id=request_snapshot.read_request_id,
                        read_request_digest=request_digest,
                        base_checkpoint_revision=base_checkpoint_revision,
                        read_status="prepared",
                        disposition="fresh_observation",
                        prepared_at=prepared_at,
                    )
        except _NativeIMStoreTransactionSignal as error:
            fixed_error_kind = error.kind
            _detach_exception(error)
        except sqlite3.IntegrityError as error:
            fixed_error_kind = "integrity"
            _detach_exception(error)
        except sqlite3.Error as error:
            fixed_error_kind = "transaction"
            _detach_exception(error)
        if fixed_error_kind is not None:
            _raise_clean_inbound_error(fixed_error_kind)
        if type(result) is not NativeIMInboundReadPreparationV1:
            raise RuntimeError("native IM read preparation completed without a result")
        return result

    def admit_native_im_inbound_page(
        self,
        request: IMInboundReadRequestV1,
        capability: IMCapabilitySnapshotV1,
        page: IMInboundPageV1,
        raw_verification: NativeIMRawVerificationResultV1,
    ) -> NativeIMInboundPageAdmissionResultV1:
        """Atomically claim verified nonce evidence and admit one prepared page."""

        self._require_operational()
        if type(request) is not IMInboundReadRequestV1:
            raise TypeError("request must be an exact IMInboundReadRequestV1")
        if type(capability) is not IMCapabilitySnapshotV1:
            raise TypeError("capability must be an exact IMCapabilitySnapshotV1")
        if type(page) is not IMInboundPageV1:
            raise TypeError("page must be an exact IMInboundPageV1")
        request_snapshot = IMInboundReadRequestV1.from_json_bytes(request.canonical_bytes())
        capability_snapshot = IMCapabilitySnapshotV1.from_json_bytes(capability.canonical_bytes())
        page_snapshot = IMInboundPageV1.from_json_bytes(page.canonical_bytes())
        verification_snapshot = _raw_verification_snapshot(raw_verification)
        page_snapshot.validate_request_binding(request_snapshot)
        page_snapshot.validate_capability_binding(capability_snapshot)
        _validate_page_authentication_binding(page_snapshot, verification_snapshot)
        event_manifest_sha256 = _event_manifest_digest(
            page_snapshot,
            verification_snapshot,
        )
        scope = (
            request_snapshot.tenant_id,
            request_snapshot.workspace_id,
            request_snapshot.provider,
            request_snapshot.channel_id,
        )
        result: Optional[NativeIMInboundPageAdmissionResultV1] = None
        fixed_error_kind: Optional[str] = None
        try:
            with self._write_transaction() as connection:
                nonce_claimed = self._claim_nonce_in_transaction(
                    connection,
                    scope=scope,
                    key_id=verification_snapshot.key_id,
                    nonce_digest=verification_snapshot.nonce_digest,
                    signed_at=verification_snapshot.signed_at,
                    expires_at=verification_snapshot.expires_at,
                    authentication_evidence_digest=(
                        verification_snapshot.authentication_evidence_digest
                    ),
                )
                result = self._admit_native_im_inbound_page_in_transaction(
                    connection,
                    request=request_snapshot,
                    page=page_snapshot,
                    event_manifest_sha256=event_manifest_sha256,
                    nonce_claimed=nonce_claimed,
                )
        except _NativeIMStoreTransactionSignal as error:
            fixed_error_kind = error.kind
            _detach_exception(error)
        except NativeIMNonceIntegrityError as error:
            fixed_error_kind = "integrity"
            _detach_exception(error)
        except sqlite3.IntegrityError as error:
            fixed_error_kind = "integrity"
            _detach_exception(error)
        except sqlite3.Error as error:
            fixed_error_kind = "transaction"
            _detach_exception(error)
        if fixed_error_kind is not None:
            _raise_clean_inbound_error(fixed_error_kind)
        if type(result) is not NativeIMInboundPageAdmissionResultV1:
            raise RuntimeError("native IM page admission completed without a result")
        return result

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
                result = self._claim_nonce_in_transaction(
                    connection,
                    scope=scope_value,
                    key_id=key_id_value,
                    nonce_digest=nonce_digest_value,
                    signed_at=signed_at_value,
                    expires_at=expires_at_value,
                    authentication_evidence_digest=evidence_digest_value,
                )
        except _NativeIMStoreTransactionSignal as error:
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
    "NativeIMInboxStoreIntegrityError",
    "NativeIMNonceCommitAmbiguityError",
    "NativeIMNonceIntegrityError",
    "NativeIMNonceStoreClosedError",
    "NativeIMNonceStorePoisonedError",
    "NativeIMNonceStoreProcessMismatchError",
    "NativeIMNonceTransactionError",
    "SQLiteNativeIMInboxStore",
    "SQLiteNativeIMNonceReplayGuard",
]
