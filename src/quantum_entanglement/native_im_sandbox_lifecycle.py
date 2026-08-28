"""Process-bound E2 lifecycle, kill switch, and atomic observation admission."""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import NoReturn, SupportsIndex

from .native_im import IMInboundReadRequestV1
from .native_im_inbox import (
    NativeIMInboundPageAdmissionResultV1,
    NativeIMInboundReadPreparationV1,
)
from .native_im_nonce_store import SQLiteNativeIMInboxStore
from .native_im_sandbox import (
    NativeIMHealthEvidenceV1,
    NativeIMInboundOnlySandboxAdapter,
    NativeIMVerifiedInboundReadV1,
)

_KILL_REASONS = {
    "canary_detected",
    "contract_failure",
    "health_failed",
    "manual",
    "shutdown",
}
_LIFECYCLE_STATES = {"stopped", "starting", "ready", "failed", "draining", "closed"}


class NativeIMKillSwitchTrippedError(RuntimeError):
    code = "native_im_kill_switch_tripped"

    def __init__(self) -> None:
        super().__init__(self.code)


class NativeIMSandboxLifecycleError(RuntimeError):
    code = "native_im_sandbox_lifecycle_invalid"

    def __init__(self) -> None:
        super().__init__(self.code)


class NativeIMSandboxProcessMismatchError(RuntimeError):
    code = "native_im_sandbox_process_mismatch"

    def __init__(self) -> None:
        super().__init__(self.code)


@dataclass(frozen=True, repr=False)
class NativeIMKillSwitchSnapshotV1:
    generation: int
    tripped: bool
    _owner_token: object = field(repr=False)
    _process_id: int = field(repr=False)

    def __post_init__(self) -> None:
        if type(self) is not NativeIMKillSwitchSnapshotV1:
            raise TypeError("kill-switch snapshot requires the exact V1 class")
        if type(self.generation) is not int or self.generation < 0:
            raise TypeError("kill-switch generation must be a non-negative exact integer")
        if type(self.tripped) is not bool:
            raise TypeError("kill-switch state must be an exact boolean")
        if type(self._process_id) is not int or self._process_id <= 0:
            raise TypeError("kill-switch snapshot process must be a positive exact integer")

    def __repr__(self) -> str:
        return (
            f"NativeIMKillSwitchSnapshotV1(generation={self.generation}, tripped={self.tripped!r})"
        )


class NativeIMSandboxKillSwitchV1:
    """One-way process-local gate serialized with the final admission call."""

    __slots__ = (
        "__generation",
        "__lock",
        "__owner_token",
        "__process_id",
        "__reason",
        "__tripped",
    )

    def __init__(self) -> None:
        self.__process_id = os.getpid()
        self.__owner_token = object()
        self.__lock = threading.Lock()
        self.__generation = 0
        self.__tripped = False
        self.__reason: str | None = None

    def _require_current_process(self) -> None:
        if os.getpid() != self.__process_id:
            raise NativeIMSandboxProcessMismatchError() from None

    def snapshot(self) -> NativeIMKillSwitchSnapshotV1:
        self._require_current_process()
        with self.__lock:
            return NativeIMKillSwitchSnapshotV1(
                generation=self.__generation,
                tripped=self.__tripped,
                _owner_token=self.__owner_token,
                _process_id=self.__process_id,
            )

    def require_permitted(self, snapshot: NativeIMKillSwitchSnapshotV1) -> None:
        self._require_current_process()
        if type(snapshot) is not NativeIMKillSwitchSnapshotV1:
            raise TypeError("kill-switch check requires the exact snapshot")
        with self.__lock:
            self._require_snapshot_permitted(snapshot)

    @contextmanager
    def admission_guard(
        self,
        snapshot: NativeIMKillSwitchSnapshotV1,
    ) -> Iterator[None]:
        """Prevent a completed trip from racing the atomic admission call."""

        self._require_current_process()
        if type(snapshot) is not NativeIMKillSwitchSnapshotV1:
            raise TypeError("kill-switch admission requires the exact snapshot")
        self.__lock.acquire()
        try:
            self._require_current_process()
            self._require_snapshot_permitted(snapshot)
            yield
        finally:
            self.__lock.release()

    def _require_snapshot_permitted(self, snapshot: NativeIMKillSwitchSnapshotV1) -> None:
        if (
            snapshot._owner_token is not self.__owner_token
            or snapshot._process_id != self.__process_id
            or snapshot.tripped
            or self.__tripped
            or snapshot.generation != self.__generation
        ):
            raise NativeIMKillSwitchTrippedError() from None

    def trip(self, reason: str) -> bool:
        self._require_current_process()
        if type(reason) is not str or reason not in _KILL_REASONS:
            raise ValueError("native IM kill-switch reason is invalid")
        with self.__lock:
            if self.__tripped:
                return False
            self.__tripped = True
            self.__reason = reason
            self.__generation += 1
            return True

    @property
    def tripped(self) -> bool:
        self._require_current_process()
        with self.__lock:
            return self.__tripped

    @property
    def generation(self) -> int:
        self._require_current_process()
        with self.__lock:
            return self.__generation

    def __repr__(self) -> str:
        snapshot = self.snapshot()
        return (
            "NativeIMSandboxKillSwitchV1("
            f"generation={snapshot.generation}, tripped={snapshot.tripped!r})"
        )

    def __reduce_ex__(self, protocol: SupportsIndex) -> NoReturn:
        raise TypeError("native IM kill switch cannot be serialized")


@dataclass(frozen=True)
class NativeIMSandboxLifecycleStatusV1:
    state: str
    ready: bool
    kill_switch_tripped: bool
    kill_switch_generation: int

    def __post_init__(self) -> None:
        if type(self) is not NativeIMSandboxLifecycleStatusV1:
            raise TypeError("lifecycle status requires the exact V1 class")
        if type(self.state) is not str or self.state not in _LIFECYCLE_STATES:
            raise ValueError("lifecycle state is invalid")
        if type(self.ready) is not bool or type(self.kill_switch_tripped) is not bool:
            raise TypeError("lifecycle status flags must be exact booleans")
        if type(self.kill_switch_generation) is not int or self.kill_switch_generation < 0:
            raise TypeError("lifecycle generation must be a non-negative exact integer")
        if self.ready != (self.state == "ready" and not self.kill_switch_tripped):
            raise ValueError("lifecycle ready flag does not match state and kill switch")


class NativeIMSandboxLifecycleV1:
    """Serialize startup, inbound reads, kill-switch admission, and graceful close."""

    __slots__ = (
        "__adapter",
        "__kill_switch",
        "__lock",
        "__process_id",
        "__state",
        "__store",
    )

    def __init__(
        self,
        adapter: NativeIMInboundOnlySandboxAdapter,
        store: SQLiteNativeIMInboxStore,
        kill_switch: NativeIMSandboxKillSwitchV1,
    ) -> None:
        if type(adapter) is not NativeIMInboundOnlySandboxAdapter:
            raise TypeError("lifecycle requires the exact inbound-only adapter")
        if type(store) is not SQLiteNativeIMInboxStore:
            raise TypeError("lifecycle requires the exact native IM inbox store")
        if type(kill_switch) is not NativeIMSandboxKillSwitchV1:
            raise TypeError("lifecycle requires the exact kill switch")
        self.__process_id = os.getpid()
        self.__adapter = adapter
        self.__store = store
        self.__kill_switch = kill_switch
        self.__lock = asyncio.Lock()
        self.__state = "stopped"

    def _require_current_process(self) -> None:
        if os.getpid() != self.__process_id:
            raise NativeIMSandboxProcessMismatchError() from None

    async def start(self) -> NativeIMHealthEvidenceV1:
        self._require_current_process()
        async with self.__lock:
            self._require_current_process()
            if self.__state != "stopped":
                raise NativeIMSandboxLifecycleError() from None
            snapshot = self.__kill_switch.snapshot()
            self.__kill_switch.require_permitted(snapshot)
            self.__state = "starting"
            try:
                health = await self.__adapter.probe_health()
                self.__kill_switch.require_permitted(snapshot)
            except BaseException:
                self.__state = "failed"
                raise
            self.__state = "ready"
            return health

    async def admit_once(
        self,
        request: IMInboundReadRequestV1,
    ) -> NativeIMInboundPageAdmissionResultV1:
        self._require_current_process()
        async with self.__lock:
            self._require_current_process()
            if self.__state != "ready":
                raise NativeIMSandboxLifecycleError() from None
            snapshot = self.__kill_switch.snapshot()
            self.__kill_switch.require_permitted(snapshot)
            if type(request) is not IMInboundReadRequestV1:
                raise TypeError("lifecycle read requires the exact V1 request")
            preparation = self.__store.prepare_native_im_inbound_read(request)
            if type(preparation) is not NativeIMInboundReadPreparationV1:
                raise NativeIMSandboxLifecycleError() from None
            self.__kill_switch.require_permitted(snapshot)
            verified = await self.__adapter.read_verified_inbound(request)
            if type(verified) is not NativeIMVerifiedInboundReadV1:
                raise NativeIMSandboxLifecycleError() from None
            with self.__kill_switch.admission_guard(snapshot):
                result = self.__store.admit_native_im_inbound_page(
                    verified.request,
                    verified.capability,
                    verified.page,
                    verified.raw_verification,
                )
            if type(result) is not NativeIMInboundPageAdmissionResultV1:
                raise NativeIMSandboxLifecycleError() from None
            return result

    def trip(self, reason: str = "manual") -> bool:
        self._require_current_process()
        return self.__kill_switch.trip(reason)

    async def aclose(self) -> None:
        self._require_current_process()
        self.__kill_switch.trip("shutdown")
        async with self.__lock:
            self._require_current_process()
            if self.__state == "closed":
                return
            self.__state = "draining"
            try:
                await self.__adapter.aclose()
            except BaseException:
                self.__state = "failed"
                raise
            self.__state = "closed"

    def status(self) -> NativeIMSandboxLifecycleStatusV1:
        self._require_current_process()
        snapshot = self.__kill_switch.snapshot()
        return NativeIMSandboxLifecycleStatusV1(
            state=self.__state,
            ready=self.__state == "ready" and not snapshot.tripped,
            kill_switch_tripped=snapshot.tripped,
            kill_switch_generation=snapshot.generation,
        )

    def __repr__(self) -> str:
        status = self.status()
        return (
            "NativeIMSandboxLifecycleV1("
            f"state={status.state!r}, kill_switch_tripped={status.kill_switch_tripped!r})"
        )

    def __reduce_ex__(self, protocol: SupportsIndex) -> NoReturn:
        raise TypeError("native IM lifecycle cannot be serialized")


__all__ = [
    "NativeIMKillSwitchSnapshotV1",
    "NativeIMKillSwitchTrippedError",
    "NativeIMSandboxKillSwitchV1",
    "NativeIMSandboxLifecycleError",
    "NativeIMSandboxLifecycleStatusV1",
    "NativeIMSandboxLifecycleV1",
    "NativeIMSandboxProcessMismatchError",
]
