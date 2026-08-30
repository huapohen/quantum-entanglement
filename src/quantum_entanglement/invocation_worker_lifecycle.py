"""Private lifecycle composition for the scoped PURE worker rehearsal.

The lifecycle owns admission stop, per-run cancellation, store-owned heartbeats, and
bounded graceful drain.  It is intentionally separate from ``HeartbeatPureWorkerGate``:
the public product dispatch gate remains disabled until the production allowlist,
authentication, process-kill, compatibility, and operational gates are approved.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import cast

from .invocation_execution import (
    ScopedInvocationExecutionManifestV2,
    ScopedInvocationStartClaimedV3,
)
from .invocation_results import ScopedInvocationResultAcceptanceRequestV2
from .invocation_worker import (
    HeartbeatPureWorkerGate,
    HeartbeatPureWorkerSupervisor,
    InvocationWorkerConfiguration,
    PureWorkerContext,
    PureWorkerOutcome,
    PureWorkerRunResult,
)
from .store import SQLiteEventStore


class PureWorkerLifecycleState(str, Enum):
    """Monotonic lifecycle states for one process-local worker composition."""

    ACCEPTING = "accepting"
    DRAINING = "draining"
    CLOSED = "closed"


class PureWorkerLifecycleError(RuntimeError):
    """Base error for lifecycle admission and shutdown violations."""


class PureWorkerLifecycleDrainingError(PureWorkerLifecycleError):
    """Raised when a new run arrives after admission has stopped."""

    code = "pure_worker_lifecycle_draining"

    def __init__(self) -> None:
        super().__init__("pure worker lifecycle is draining and no longer accepts work")


class PureWorkerLifecycleClosedError(PureWorkerLifecycleError):
    """Raised when a closed lifecycle is asked to run work."""

    code = "pure_worker_lifecycle_closed"

    def __init__(self) -> None:
        super().__init__("pure worker lifecycle is closed")


@dataclass(frozen=True)
class PureWorkerLifecycleSnapshot:
    """Capability-free lifecycle observation returned by ``snapshot``/``close``."""

    state: PureWorkerLifecycleState
    active_runs: int

    def __post_init__(self) -> None:
        if type(self) is not PureWorkerLifecycleSnapshot:
            raise TypeError("lifecycle snapshot must be exact")
        if type(self.state) is not PureWorkerLifecycleState:
            raise TypeError("lifecycle snapshot state must be exact")
        if type(self.active_runs) is not int or self.active_runs < 0:
            raise ValueError("lifecycle snapshot active_runs must be non-negative")


def _timeout(value: object, field_name: str) -> float:
    if type(value) not in (int, float):
        raise TypeError(f"{field_name} must be an exact built-in number")
    normalized = float(cast(int | float, value))
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError(f"{field_name} must be finite and greater than zero")
    return normalized


async def _forward_shutdown(
    target: asyncio.Event,
    lifecycle_event: asyncio.Event,
    external_event: asyncio.Event | None,
) -> None:
    """Forward lifecycle or caller cancellation into one exact per-run event."""

    lifecycle_waiter = asyncio.create_task(lifecycle_event.wait())
    external_waiter = None if external_event is None else asyncio.create_task(external_event.wait())
    waiters: set[asyncio.Task[bool]] = {lifecycle_waiter}
    if external_waiter is not None:
        waiters.add(external_waiter)
    try:
        done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        if done:
            target.set()
    finally:
        for waiter in waiters:
            if not waiter.done():
                waiter.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)


class ScopedPureWorkerLifecycle:
    """Default-off store-backed lifecycle for scoped PURE/fake worker rehearsal.

    Every run gets a fresh supervisor and a per-run cancellation event.  Closing the
    lifecycle first changes state under the lock (stopping new admission), then signals
    all active runs.  The supervisor continues store heartbeats during its bounded
    cooperative drain; non-success outcomes relinquish the lease through the same
    store-owned CAS.  No handler receives a store, lease, connector, or credential.
    """

    def __init__(self, store: SQLiteEventStore) -> None:
        if type(store) is not SQLiteEventStore:
            raise TypeError("lifecycle requires an exact SQLiteEventStore")
        self._store = store
        self._state = PureWorkerLifecycleState.ACCEPTING
        self._state_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._drain_event = asyncio.Event()
        self._active: dict[asyncio.Task[PureWorkerRunResult], asyncio.Event] = {}

    @property
    def state(self) -> PureWorkerLifecycleState:
        return self._state

    async def snapshot(self) -> PureWorkerLifecycleSnapshot:
        async with self._state_lock:
            return PureWorkerLifecycleSnapshot(self._state, len(self._active))

    async def _register(self, cancellation: asyncio.Event) -> asyncio.Task[PureWorkerRunResult]:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("worker lifecycle requires an asyncio task")
        async with self._state_lock:
            if self._state is PureWorkerLifecycleState.DRAINING:
                raise PureWorkerLifecycleDrainingError()
            if self._state is PureWorkerLifecycleState.CLOSED:
                raise PureWorkerLifecycleClosedError()
            self._active[task] = cancellation
        return task

    async def run_and_accept(
        self,
        claimed: ScopedInvocationStartClaimedV3,
        manifest: ScopedInvocationExecutionManifestV2,
        configuration: InvocationWorkerConfiguration,
        handler: Callable[[PureWorkerContext], Awaitable[object]],
        *,
        acceptor: Callable[
            [ScopedInvocationResultAcceptanceRequestV2, ScopedInvocationStartClaimedV3],
            object,
        ],
        cancellation: asyncio.Event | None = None,
    ) -> PureWorkerRunResult:
        """Run one exact scoped PURE handler through store heartbeat and acceptance."""

        if type(claimed) is not ScopedInvocationStartClaimedV3:
            raise TypeError("lifecycle claimed value must be exact ScopedInvocationStartClaimedV3")
        if type(manifest) is not ScopedInvocationExecutionManifestV2:
            raise TypeError("lifecycle manifest must be exact ScopedInvocationExecutionManifestV2")
        if type(configuration) is not InvocationWorkerConfiguration:
            raise TypeError("lifecycle configuration must be exact")
        if not callable(handler) or not callable(acceptor):
            raise TypeError("lifecycle handler and acceptor must be callable")
        if cancellation is not None and type(cancellation) is not asyncio.Event:
            raise TypeError("lifecycle cancellation must be an exact asyncio.Event")

        claimed_snapshot = ScopedInvocationStartClaimedV3(claimed.receipt, claimed.lease)
        manifest_snapshot = ScopedInvocationExecutionManifestV2.from_dict(manifest.to_dict())
        configuration_snapshot = InvocationWorkerConfiguration(
            lease_seconds=configuration.lease_seconds,
            heartbeat_interval_seconds=configuration.heartbeat_interval_seconds,
            handler_timeout_seconds=configuration.handler_timeout_seconds,
            drain_timeout_seconds=configuration.drain_timeout_seconds,
        )
        cancellation_signal = asyncio.Event()
        task = await self._register(cancellation_signal)
        forwarder = asyncio.create_task(
            _forward_shutdown(cancellation_signal, self._drain_event, cancellation)
        )
        result: PureWorkerRunResult | None = None
        try:
            worker_admission = HeartbeatPureWorkerGate.prepare_scoped_v3(
                claimed_snapshot,
                manifest_snapshot,
                configuration_snapshot,
                handler_revision=manifest_snapshot.runtime_revision,
            )

            async def heartbeat(lease_seconds: float) -> bool:
                return self._store.heartbeat_scoped_invocation_start_v3(
                    claimed_snapshot,
                    lease_seconds=lease_seconds,
                )

            result = await HeartbeatPureWorkerSupervisor(
                worker_admission,
                heartbeat=heartbeat,
            ).run_and_accept(
                handler,
                acceptor=acceptor,
                cancellation=cancellation_signal,
            )
        except asyncio.CancelledError:
            cancellation_signal.set()
            result = PureWorkerRunResult(PureWorkerOutcome.CANCELED, drained=False)
        finally:
            forwarder.cancel()
            await asyncio.gather(forwarder, return_exceptions=True)
            try:
                if result is not None and result.outcome not in {
                    PureWorkerOutcome.ACCEPTED,
                    PureWorkerOutcome.OBSERVED,
                }:
                    self._store.relinquish_scoped_invocation_start_v3(claimed_snapshot)
            finally:
                # A poisoned/integrity-failing store must not strand this task in
                # process-local admission bookkeeping.  The original store error is
                # allowed to propagate, but close() must still observe active_runs=0.
                async with self._state_lock:
                    self._active.pop(task, None)
        if result is None:  # pragma: no cover - every branch sets a sanitized result.
            raise RuntimeError("worker lifecycle completed without a result")
        return result

    async def close(self, *, timeout_seconds: float = 5.0) -> PureWorkerLifecycleSnapshot:
        """Stop admission and drain active runs within one bounded process deadline."""

        timeout_snapshot = _timeout(timeout_seconds, "timeout_seconds")
        async with self._close_lock:
            current = asyncio.current_task()
            async with self._state_lock:
                if self._state is PureWorkerLifecycleState.CLOSED:
                    return PureWorkerLifecycleSnapshot(self._state, len(self._active))
                self._state = PureWorkerLifecycleState.DRAINING
                self._drain_event.set()
                active = tuple(
                    (task, event)
                    for task, event in self._active.items()
                    if task is not current
                )
                for _, event in active:
                    event.set()
            tasks = tuple(task for task, _ in active)
            if tasks:
                done, pending = await asyncio.wait(tasks, timeout=timeout_snapshot)
                del done
                if pending:
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
            async with self._state_lock:
                self._state = PureWorkerLifecycleState.CLOSED
                return PureWorkerLifecycleSnapshot(self._state, len(self._active))


__all__ = [
    "PureWorkerLifecycleClosedError",
    "PureWorkerLifecycleDrainingError",
    "PureWorkerLifecycleError",
    "PureWorkerLifecycleSnapshot",
    "PureWorkerLifecycleState",
    "ScopedPureWorkerLifecycle",
]
