"""Process-local activation and continuously rechecked native-IM sandbox authority.

The concrete authority in this module is deliberately in-memory and offline. It is useful
for composition and adversarial tests, but it is not a durable production approval store.
It is never registered by default and grants only the exact health/read operations encoded
in one independently digested approval record.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, NoReturn, Protocol, SupportsIndex

from . import process_identity as _process_identity
from ._native_im_codec import _digest, _id, _timestamp
from .native_im_provider_profile import IMProviderProfileV1
from .native_im_sandbox_approval import (
    NativeIMSandboxApprovalBindingError,
    NativeIMSandboxApprovalV1,
    validate_native_im_sandbox_approval_binding_v1,
)
from .native_im_sandbox_approval_store import (
    NativeIMSandboxApprovalAuthorityStateV1,
    NativeIMSandboxApprovalStoreError,
    SQLiteNativeIMSandboxApprovalHighWaterV1,
)
from .service.native_im_config import NativeIMInboundOnlyConfigV1

_ACTIVATION_TOKEN = object()
_MAXIMUM_APPROVAL_TTL_SECONDS = 86_400
_OPERATIONS = {"health", "read"}


class NativeIMSandboxApprovalAuthorityError(RuntimeError):
    """A redacted, stable failure from the live approval authority boundary."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class NativeIMSandboxApprovalAuthorityPort(Protocol):
    """Continuously check a process-local permit before protected operations."""

    def activate(
        self,
        configuration: NativeIMInboundOnlyConfigV1,
        profile: IMProviderProfileV1,
    ) -> NativeIMSandboxApprovalPermitV1: ...

    def require_current(
        self,
        permit: NativeIMSandboxApprovalPermitV1,
        *,
        operation: str,
    ) -> NativeIMSandboxApprovalV1: ...


def _authority_error(code: str) -> NativeIMSandboxApprovalAuthorityError:
    return NativeIMSandboxApprovalAuthorityError(code)


def _parse_clock(value: object) -> tuple[str, datetime]:
    try:
        canonical = _timestamp(value, "now")
        parsed = datetime.fromisoformat(canonical[:-1] + "+00:00")
    except (TypeError, ValueError):
        raise NativeIMSandboxApprovalAuthorityError(
            "native_im_sandbox_approval_clock_invalid"
        ) from None
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise NativeIMSandboxApprovalAuthorityError(
            "native_im_sandbox_approval_clock_invalid"
        ) from None
    return canonical, parsed


def _timestamp_value(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00")


@dataclass(frozen=True, repr=False)
class NativeIMSandboxApprovalPermitV1:
    """Opaque live permission; it cannot be copied, persisted, or inherited."""

    approval_id: str
    authority_revision: int
    approval_digest: str = field(repr=False)
    generation: int
    _authority_token: object = field(repr=False)
    _process_owner: object = field(repr=False)
    _activation_token: object = field(repr=False)

    def __post_init__(self) -> None:
        if type(self) is not NativeIMSandboxApprovalPermitV1:
            raise TypeError("approval permit requires the exact V1 class")
        if self._activation_token is not _ACTIVATION_TOKEN:
            raise TypeError("approval permits are activated by a trusted authority")
        _id(self.approval_id, "approvalId")
        _digest(self.approval_digest, "approvalDigest")
        if type(self.authority_revision) is not int or self.authority_revision <= 0:
            raise TypeError("approval permit revision must be a positive exact integer")
        if type(self.generation) is not int or self.generation < 0:
            raise TypeError("approval permit generation must be a non-negative exact integer")

    def __copy__(self) -> NoReturn:
        raise TypeError("native IM sandbox approval permits cannot be copied")

    def __deepcopy__(self, memo: object) -> NoReturn:
        raise TypeError("native IM sandbox approval permits cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("native IM sandbox approval permits cannot be serialized")

    def __reduce_ex__(self, protocol: SupportsIndex) -> NoReturn:
        raise TypeError("native IM sandbox approval permits cannot be serialized")

    def __repr__(self) -> str:
        return (
            "NativeIMSandboxApprovalPermitV1("
            f"revision={self.authority_revision}, generation={self.generation})"
        )


class InMemoryNativeIMSandboxApprovalAuthorityV1:
    """Exact offline authority used only by explicit composition and tests.

    The caller must supply an independently trusted record digest. The record is copied
    through its canonical codec during construction. A durable authority and high-water
    store remain mandatory before a real provider transport can be enabled.
    """

    __slots__ = (
        "__approval",
        "__approval_digest",
        "__authority_token",
        "__clock",
        "__generation",
        "__high_water",
        "__lock",
        "__process_owner",
        "__revoked",
    )

    def __init__(
        self,
        approval: NativeIMSandboxApprovalV1,
        *,
        trusted_record_digest: str,
        high_water: SQLiteNativeIMSandboxApprovalHighWaterV1,
        clock: Callable[[], str],
    ) -> None:
        if type(approval) is not NativeIMSandboxApprovalV1:
            raise TypeError("authority requires the exact sandbox approval V1 record")
        try:
            _digest(trusted_record_digest, "trustedRecordDigest")
        except (TypeError, ValueError):
            raise NativeIMSandboxApprovalAuthorityError(
                "native_im_sandbox_approval_trust_anchor_invalid"
            ) from None
        if approval.canonical_digest() != trusted_record_digest:
            raise NativeIMSandboxApprovalAuthorityError(
                "native_im_sandbox_approval_trust_anchor_mismatch"
            ) from None
        if not callable(clock):
            raise TypeError("approval authority clock must be callable")
        if type(high_water) is not SQLiteNativeIMSandboxApprovalHighWaterV1:
            raise TypeError("approval authority requires the exact durable high-water store")
        self.__process_owner = _process_identity.capture_process_owner()
        self.__authority_token = object()
        self.__approval = NativeIMSandboxApprovalV1.from_json_bytes(
            approval.canonical_bytes()
        )
        self.__approval_digest = trusted_record_digest
        self.__clock = clock
        self.__high_water = high_water
        self.__generation = 0
        self.__revoked = False
        self.__lock = threading.RLock()

    def _require_process(self) -> None:
        _process_identity.require_current_process(
            self.__process_owner,
            lambda: _authority_error("native_im_sandbox_approval_process_mismatch"),
        )

    def _now(self) -> tuple[str, datetime]:
        self._require_process()
        failed = False
        value: object = None
        try:
            value = self.__clock()
        except Exception as error:
            failed = True
            error.__traceback__ = None
            error.__cause__ = None
            error.__context__ = None
        if failed:
            raise NativeIMSandboxApprovalAuthorityError(
                "native_im_sandbox_approval_clock_invalid"
            ) from None
        return _parse_clock(value)

    @staticmethod
    def _require_time_window(
        approval: NativeIMSandboxApprovalV1,
        now_text: str,
        now_value: datetime,
    ) -> None:
        issued = _timestamp_value(approval.issued_at)
        not_before = _timestamp_value(approval.not_before)
        expires = _timestamp_value(approval.expires_at)
        if (expires - not_before).total_seconds() > _MAXIMUM_APPROVAL_TTL_SECONDS:
            raise NativeIMSandboxApprovalAuthorityError(
                "native_im_sandbox_approval_ttl_exceeded"
            ) from None
        if now_value < issued or now_text < approval.not_before:
            raise NativeIMSandboxApprovalAuthorityError(
                "native_im_sandbox_approval_not_yet_valid"
            ) from None
        if now_text >= approval.expires_at:
            raise NativeIMSandboxApprovalAuthorityError(
                "native_im_sandbox_approval_expired"
            ) from None

    def _require_permit_locked(
        self,
        permit: NativeIMSandboxApprovalPermitV1,
        *,
        operation: str,
    ) -> NativeIMSandboxApprovalV1:
        if type(permit) is not NativeIMSandboxApprovalPermitV1:
            raise TypeError("approval authority requires the exact live permit V1 class")
        if type(operation) is not str or operation not in _OPERATIONS:
            raise NativeIMSandboxApprovalAuthorityError(
                "native_im_sandbox_approval_operation_forbidden"
            ) from None
        self._require_process()
        _process_identity.require_current_process(
            permit._process_owner,
            lambda: _authority_error("native_im_sandbox_approval_process_mismatch"),
        )
        if self.__revoked:
            raise NativeIMSandboxApprovalAuthorityError(
                "native_im_sandbox_approval_revoked"
            ) from None
        if (
            permit._activation_token is not _ACTIVATION_TOKEN
            or permit._authority_token is not self.__authority_token
            or permit.approval_id != self.__approval.approval_id
            or permit.authority_revision != self.__approval.authority_revision
            or permit.approval_digest != self.__approval_digest
            or permit.generation != self.__generation
        ):
            raise NativeIMSandboxApprovalAuthorityError(
                "native_im_sandbox_approval_permit_invalid"
            ) from None
        if operation not in self.__approval.allowed_operations:
            raise NativeIMSandboxApprovalAuthorityError(
                "native_im_sandbox_approval_operation_forbidden"
            ) from None
        now_text, now_value = self._now()
        self._require_time_window(self.__approval, now_text, now_value)
        self._observe_approved_locked(now_text)
        return NativeIMSandboxApprovalV1.from_json_bytes(
            self.__approval.canonical_bytes()
        )

    def _approved_state(self, observed_at: str) -> NativeIMSandboxApprovalAuthorityStateV1:
        return NativeIMSandboxApprovalAuthorityStateV1(
            schema_version=1,
            approval_id=self.__approval.approval_id,
            approval_digest=self.__approval_digest,
            authority_revision=self.__approval.authority_revision,
            state="approved",
            observed_at=observed_at,
        )

    def _raise_store_failure(self, error: NativeIMSandboxApprovalStoreError) -> NoReturn:
        code = error.code
        error.__traceback__ = None
        error.__cause__ = None
        error.__context__ = None
        if code == "native_im_sandbox_approval_store_terminal_revoked":
            self.__revoked = True
            self.__generation += 1
            raise NativeIMSandboxApprovalAuthorityError(
                "native_im_sandbox_approval_revoked"
            ) from None
        raise NativeIMSandboxApprovalAuthorityError(
            "native_im_sandbox_approval_durable_state_invalid"
        ) from None

    def _observe_approved_locked(self, observed_at: str) -> None:
        try:
            current = self.__high_water.current(self.__approval.approval_id)
        except NativeIMSandboxApprovalStoreError as error:
            self._raise_store_failure(error)
        if current is not None:
            if current.state == "revoked":
                self.__revoked = True
                self.__generation += 1
                raise NativeIMSandboxApprovalAuthorityError(
                    "native_im_sandbox_approval_revoked"
                ) from None
            if (
                current.approval_digest != self.__approval_digest
                or current.authority_revision != self.__approval.authority_revision
            ):
                raise NativeIMSandboxApprovalAuthorityError(
                    "native_im_sandbox_approval_durable_state_invalid"
                ) from None
        try:
            self.__high_water.observe(self._approved_state(observed_at))
        except NativeIMSandboxApprovalStoreError as error:
            self._raise_store_failure(error)

    def activate(
        self,
        configuration: NativeIMInboundOnlyConfigV1,
        profile: IMProviderProfileV1,
    ) -> NativeIMSandboxApprovalPermitV1:
        self._require_process()
        if type(configuration) is not NativeIMInboundOnlyConfigV1:
            raise TypeError("approval activation requires the exact inbound configuration")
        if type(profile) is not IMProviderProfileV1:
            raise TypeError("approval activation requires the exact provider profile")
        with self.__lock:
            self._require_process()
            if self.__revoked:
                raise NativeIMSandboxApprovalAuthorityError(
                    "native_im_sandbox_approval_revoked"
                ) from None
            try:
                validate_native_im_sandbox_approval_binding_v1(
                    self.__approval,
                    configuration,
                    profile,
                )
            except NativeIMSandboxApprovalBindingError:
                raise NativeIMSandboxApprovalAuthorityError(
                    "native_im_sandbox_approval_binding_mismatch"
                ) from None
            now_text, now_value = self._now()
            self._require_time_window(self.__approval, now_text, now_value)
            self._observe_approved_locked(now_text)
            return NativeIMSandboxApprovalPermitV1(
                approval_id=self.__approval.approval_id,
                authority_revision=self.__approval.authority_revision,
                approval_digest=self.__approval_digest,
                generation=self.__generation,
                _authority_token=self.__authority_token,
                _process_owner=_process_identity.capture_process_owner(),
                _activation_token=_ACTIVATION_TOKEN,
            )

    def require_current(
        self,
        permit: NativeIMSandboxApprovalPermitV1,
        *,
        operation: str,
    ) -> NativeIMSandboxApprovalV1:
        self._require_process()
        with self.__lock:
            return self._require_permit_locked(permit, operation=operation)

    @contextmanager
    def admission_guard(
        self,
        permit: NativeIMSandboxApprovalPermitV1,
        *,
        operation: str = "read",
    ) -> Iterator[NativeIMSandboxApprovalV1]:
        """Linearize final local admission against revocation and expiry checks."""

        self._require_process()
        self.__lock.acquire()
        try:
            approval = self._require_permit_locked(permit, operation=operation)
            now_text, now_value = self._now()
            self._require_time_window(self.__approval, now_text, now_value)
            state = self._approved_state(now_text)
            try:
                with self.__high_water.admission_guard(state):
                    yield approval
            except NativeIMSandboxApprovalStoreError as error:
                self._raise_store_failure(error)
        finally:
            self.__lock.release()

    def revoke(self) -> bool:
        self._require_process()
        with self.__lock:
            if self.__revoked:
                return False
            now_text, _ = self._now()
            try:
                current = self.__high_water.current(self.__approval.approval_id)
            except NativeIMSandboxApprovalStoreError as error:
                self._raise_store_failure(error)
            if current is not None and current.state == "revoked":
                self.__revoked = True
                self.__generation += 1
                return False
            revision = (
                self.__approval.authority_revision
                if current is None
                else current.authority_revision
            ) + 1
            revoked = NativeIMSandboxApprovalAuthorityStateV1(
                schema_version=1,
                approval_id=self.__approval.approval_id,
                approval_digest=self.__approval_digest,
                authority_revision=revision,
                state="revoked",
                observed_at=now_text,
            )
            self.__revoked = True
            self.__generation += 1
            try:
                self.__high_water.observe(revoked)
            except NativeIMSandboxApprovalStoreError as error:
                self._raise_store_failure(error)
            return True

    @property
    def revoked(self) -> bool:
        self._require_process()
        with self.__lock:
            return self.__revoked

    @property
    def generation(self) -> int:
        self._require_process()
        with self.__lock:
            return self.__generation

    def __copy__(self) -> NoReturn:
        raise TypeError("native IM sandbox approval authorities cannot be copied")

    def __deepcopy__(self, memo: object) -> NoReturn:
        raise TypeError("native IM sandbox approval authorities cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("native IM sandbox approval authorities cannot be serialized")

    def __reduce_ex__(self, protocol: SupportsIndex) -> NoReturn:
        raise TypeError("native IM sandbox approval authorities cannot be serialized")

    def __repr__(self) -> str:
        self._require_process()
        return (
            "InMemoryNativeIMSandboxApprovalAuthorityV1("
            f"generation={self.generation}, revoked={self.revoked!r})"
        )


__all__ = [
    "InMemoryNativeIMSandboxApprovalAuthorityV1",
    "NativeIMSandboxApprovalAuthorityError",
    "NativeIMSandboxApprovalAuthorityPort",
    "NativeIMSandboxApprovalPermitV1",
]
