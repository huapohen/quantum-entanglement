# ruff: noqa: UP006, UP035
"""Fail-closed admission models for the future heartbeat-supervised worker.

This module deliberately cannot dispatch work yet.  It snapshots and validates the exact
non-replayable invocation-start authority that a future pure/fake worker may consume, while the
gate's dispatch path always fails before inspecting caller work.  Atomic result acceptance and
receipt-bound recovery must land before the gate can be promoted.
"""

from __future__ import annotations

import asyncio
import inspect
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import NoReturn, cast

from .invocation_execution import (
    EffectClass,
    InvocationExecutionManifest,
    InvocationStartClaimed,
    RetryClass,
    ScopedInvocationExecutionManifestV2,
    ScopedInvocationStartClaimedV3,
)
from .invocation_results import (
    ScopedInvocationResultAcceptanceRequestV2,
    ScopedInvocationResultAcceptedV2,
    ScopedInvocationResultObservedV2,
)


class InvocationWorkerDisabledError(RuntimeError):
    """Raised while the durable result-acceptance prerequisites are unavailable."""

    code = "invocation_worker_disabled"

    def __init__(self) -> None:
        super().__init__(
            "heartbeat worker is disabled until atomic result acceptance and recovery are enabled"
        )


def _duration(value: object, label: str) -> float:
    if type(value) not in (int, float):
        raise TypeError(f"{label} must be an exact built-in number")
    try:
        normalized = float(cast("int | float", value))
    except OverflowError as error:
        raise ValueError(f"{label} is outside the supported finite range") from error
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError(f"{label} must be finite and greater than zero")
    return normalized


def _revision(value: object) -> str:
    if type(value) is not str:
        raise TypeError("handler_revision must be a plain string")
    if not value or value != value.strip():
        raise ValueError("handler_revision must be non-empty without surrounding whitespace")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        encoded = None
    if encoded is None:
        raise ValueError("handler_revision must be valid UTF-8") from None
    if len(encoded) > 4_096:
        raise ValueError("handler_revision exceeds its UTF-8 byte limit")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ValueError("handler_revision contains a C0 or DEL control character")
    return value


@dataclass(frozen=True)
class InvocationWorkerConfiguration:
    """Timing policy for one future pure/fake worker run."""

    lease_seconds: float
    heartbeat_interval_seconds: float
    handler_timeout_seconds: float
    drain_timeout_seconds: float

    def __post_init__(self) -> None:
        if type(self) is not InvocationWorkerConfiguration:
            raise TypeError("worker configuration must be exact InvocationWorkerConfiguration")
        lease_seconds = _duration(self.lease_seconds, "lease_seconds")
        heartbeat_interval = _duration(
            self.heartbeat_interval_seconds,
            "heartbeat_interval_seconds",
        )
        handler_timeout = _duration(
            self.handler_timeout_seconds,
            "handler_timeout_seconds",
        )
        drain_timeout = _duration(
            self.drain_timeout_seconds,
            "drain_timeout_seconds",
        )
        if heartbeat_interval > lease_seconds / 3:
            raise ValueError("heartbeat interval must not exceed one third of the lease")
        if handler_timeout >= lease_seconds:
            raise ValueError("handler timeout must be shorter than the lease")
        if drain_timeout > lease_seconds - handler_timeout:
            raise ValueError("drain timeout exceeds the lease time remaining after handler timeout")
        object.__setattr__(self, "lease_seconds", lease_seconds)
        object.__setattr__(self, "heartbeat_interval_seconds", heartbeat_interval)
        object.__setattr__(self, "handler_timeout_seconds", handler_timeout)
        object.__setattr__(self, "drain_timeout_seconds", drain_timeout)


def _manifest_snapshot(manifest: object) -> InvocationExecutionManifest:
    if type(manifest) is not InvocationExecutionManifest:
        raise TypeError("manifest must be an exact InvocationExecutionManifest")
    return InvocationExecutionManifest.from_dict(InvocationExecutionManifest.to_dict(manifest))


def _claim_snapshot(claim: object) -> InvocationStartClaimed:
    if type(claim) is not InvocationStartClaimed:
        raise TypeError("claim must be an exact InvocationStartClaimed")
    return InvocationStartClaimed(claim.receipt, claim.lease)


def _validate_manifest_start_binding(
    manifest: InvocationExecutionManifest,
    claim: InvocationStartClaimed,
) -> None:
    evidence = claim.receipt.evidence
    bindings = (
        (manifest.invocation_id, evidence.invocation_id),
        (manifest.session_id, evidence.session_id),
        (manifest.plan_id, evidence.plan_id),
        (manifest.task_id, evidence.task_id),
        (manifest.agent_id, evidence.agent_id),
        (manifest.job_idempotency_key, evidence.job_idempotency_key),
        (manifest.envelope_digest, evidence.envelope_digest),
        (manifest.context_digest, evidence.context_digest),
        (manifest.authorization_digest, evidence.authorization_digest),
        (manifest.runtime_revision, evidence.runtime_revision),
        (manifest.correlation_id, evidence.correlation_id),
        (manifest.causation_id, evidence.causation_id),
    )
    if any(actual != expected for actual, expected in bindings):
        raise ValueError("manifest does not match invocation-start evidence")
    manifest_digest = manifest.canonical_digest()
    if manifest_digest != evidence.manifest_digest or manifest_digest != claim.lease.payload_digest:
        raise ValueError("manifest digest does not match invocation-start authority")
    if manifest.effect_class is not EffectClass.PURE:
        raise ValueError("heartbeat worker accepts only effectClass=pure")
    if manifest.retry_class is not RetryClass.NEVER:
        raise ValueError("heartbeat worker accepts only retryClass=never")
    if evidence.attempt_number != 1 or evidence.lease_epoch != 1 or claim.lease.max_attempts != 1:
        raise ValueError("heartbeat worker accepts only canonical first-attempt authority")


@dataclass(frozen=True)
class InvocationWorkerAdmission:
    """Capability-bearing, non-serializable snapshot prepared for future dispatch."""

    claim: InvocationStartClaimed = field(repr=False)
    manifest: InvocationExecutionManifest
    configuration: InvocationWorkerConfiguration
    handler_revision: str

    def __post_init__(self) -> None:
        if type(self) is not InvocationWorkerAdmission:
            raise TypeError("worker admission must be exact InvocationWorkerAdmission")
        claim = _claim_snapshot(self.claim)
        manifest = _manifest_snapshot(self.manifest)
        if type(self.configuration) is not InvocationWorkerConfiguration:
            raise TypeError("configuration must be an exact InvocationWorkerConfiguration")
        configuration = InvocationWorkerConfiguration(
            lease_seconds=self.configuration.lease_seconds,
            heartbeat_interval_seconds=self.configuration.heartbeat_interval_seconds,
            handler_timeout_seconds=self.configuration.handler_timeout_seconds,
            drain_timeout_seconds=self.configuration.drain_timeout_seconds,
        )
        handler_revision = _revision(self.handler_revision)
        _validate_manifest_start_binding(manifest, claim)
        if handler_revision != manifest.runtime_revision:
            raise ValueError("handler revision does not match the admitted runtime revision")
        object.__setattr__(self, "claim", claim)
        object.__setattr__(self, "manifest", manifest)
        object.__setattr__(self, "configuration", configuration)
        object.__setattr__(self, "handler_revision", handler_revision)

    @property
    def promotion_eligible(self) -> bool:
        """Legacy unscoped start evidence can never enable production dispatch."""

        return False


def _scoped_manifest_snapshot(manifest: object) -> ScopedInvocationExecutionManifestV2:
    if type(manifest) is not ScopedInvocationExecutionManifestV2:
        raise TypeError("manifest must be an exact ScopedInvocationExecutionManifestV2")
    return ScopedInvocationExecutionManifestV2.from_dict(manifest.to_dict())


def _scoped_claim_snapshot(claim: object) -> ScopedInvocationStartClaimedV3:
    if type(claim) is not ScopedInvocationStartClaimedV3:
        raise TypeError("claim must be an exact ScopedInvocationStartClaimedV3")
    return ScopedInvocationStartClaimedV3(claim.receipt, claim.lease)


def _validate_scoped_manifest_start_binding(
    manifest: ScopedInvocationExecutionManifestV2,
    claim: ScopedInvocationStartClaimedV3,
) -> None:
    evidence = claim.receipt.evidence
    bindings = (
        (manifest.tenant_id, evidence.tenant_id),
        (manifest.workspace_id, evidence.workspace_id),
        (manifest.invocation_id, evidence.invocation_id),
        (manifest.session_id, evidence.session_id),
        (manifest.plan_id, evidence.plan_id),
        (manifest.task_id, evidence.task_id),
        (manifest.agent_id, evidence.agent_id),
        (manifest.job_idempotency_key, evidence.job_idempotency_key),
        (manifest.envelope_digest, evidence.envelope_digest),
        (manifest.context_digest, evidence.context_digest),
        (manifest.authorization_digest, evidence.authorization_digest),
        (manifest.runtime_revision, evidence.runtime_revision),
        (manifest.correlation_id, evidence.correlation_id),
        (manifest.causation_id, evidence.causation_id),
    )
    if any(actual != expected for actual, expected in bindings):
        raise ValueError("scoped manifest does not match schema-3 start evidence")
    manifest_digest = manifest.canonical_digest()
    if manifest_digest != evidence.manifest_digest or manifest_digest != claim.lease.payload_digest:
        raise ValueError("scoped manifest digest does not match schema-3 start authority")
    if manifest.effect_class is not EffectClass.PURE:
        raise ValueError("scoped heartbeat worker accepts only effectClass=pure")
    if manifest.retry_class is not RetryClass.NEVER:
        raise ValueError("scoped heartbeat worker accepts only retryClass=never")
    if evidence.attempt_number != 1 or evidence.lease_epoch != 1 or claim.lease.max_attempts != 1:
        raise ValueError("scoped heartbeat worker accepts only first-attempt authority")


@dataclass(frozen=True)
class ScopedInvocationWorkerAdmissionV3:
    """Scope-bearing capability snapshot prepared behind the disabled dispatch gate."""

    claim: ScopedInvocationStartClaimedV3 = field(repr=False)
    manifest: ScopedInvocationExecutionManifestV2
    configuration: InvocationWorkerConfiguration
    handler_revision: str

    def __post_init__(self) -> None:
        if type(self) is not ScopedInvocationWorkerAdmissionV3:
            raise TypeError(
                "scoped worker admission must be exact ScopedInvocationWorkerAdmissionV3"
            )
        claim = _scoped_claim_snapshot(self.claim)
        manifest = _scoped_manifest_snapshot(self.manifest)
        if type(self.configuration) is not InvocationWorkerConfiguration:
            raise TypeError("configuration must be an exact InvocationWorkerConfiguration")
        configuration = InvocationWorkerConfiguration(
            lease_seconds=self.configuration.lease_seconds,
            heartbeat_interval_seconds=self.configuration.heartbeat_interval_seconds,
            handler_timeout_seconds=self.configuration.handler_timeout_seconds,
            drain_timeout_seconds=self.configuration.drain_timeout_seconds,
        )
        handler_revision = _revision(self.handler_revision)
        _validate_scoped_manifest_start_binding(manifest, claim)
        if handler_revision != manifest.runtime_revision:
            raise ValueError("handler revision does not match the scoped runtime revision")
        object.__setattr__(self, "claim", claim)
        object.__setattr__(self, "manifest", manifest)
        object.__setattr__(self, "configuration", configuration)
        object.__setattr__(self, "handler_revision", handler_revision)

    @property
    def promotion_eligible(self) -> bool:
        """Scope is necessary, but the result/recovery prerequisites are still absent."""

        return False


class PureWorkerOutcome(str, Enum):
    """Terminal classification for the private, non-publishing supervision primitive."""

    RETURNED = "returned"
    FAILED = "failed"
    LEASE_LOST = "lease_lost"
    TIMED_OUT = "timed_out"
    CANCELED = "canceled"
    ACCEPTED = "accepted"
    OBSERVED = "observed"
    ACCEPTANCE_FAILED = "acceptance_failed"


@dataclass(frozen=True)
class PureWorkerContext:
    """Handler input that contains no store, lease, connector or authorization object."""

    manifest: ScopedInvocationExecutionManifestV2
    _cancel_event: asyncio.Event = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self) is not PureWorkerContext:
            raise TypeError("pure worker context must be exact")
        if type(self.manifest) is not ScopedInvocationExecutionManifestV2:
            raise TypeError("pure worker context manifest must be exact")
        if type(self._cancel_event) is not asyncio.Event:
            raise TypeError("pure worker context cancellation event must be exact")
        object.__setattr__(
            self,
            "manifest",
            ScopedInvocationExecutionManifestV2.from_dict(self.manifest.to_dict()),
        )

    @property
    def cancelled(self) -> bool:
        return self._cancel_event.is_set()

    async def wait_cancelled(self) -> None:
        await self._cancel_event.wait()


@dataclass(frozen=True)
class PureWorkerRunResult:
    """Sanitized outcome; a returned value is retained only for a future result acceptor."""

    outcome: PureWorkerOutcome
    value: object = field(default=None, repr=False)
    drained: bool = True

    def __post_init__(self) -> None:
        if type(self) is not PureWorkerRunResult:
            raise TypeError("pure worker result must be exact")
        if type(self.outcome) is not PureWorkerOutcome:
            raise TypeError("pure worker outcome must be exact")
        if self.outcome not in {
            PureWorkerOutcome.RETURNED,
            PureWorkerOutcome.FAILED,
            PureWorkerOutcome.LEASE_LOST,
            PureWorkerOutcome.TIMED_OUT,
            PureWorkerOutcome.CANCELED,
            PureWorkerOutcome.ACCEPTED,
            PureWorkerOutcome.OBSERVED,
            PureWorkerOutcome.ACCEPTANCE_FAILED,
        }:
            raise ValueError("pure worker outcome is unsupported")
        if type(self.drained) is not bool:
            raise TypeError("pure worker drained flag must be a boolean")


async def _maybe_await(value: object) -> object:
    if inspect.isawaitable(value):
        return await cast(Awaitable[object], value)
    return value


async def _cancel_and_drain(task: asyncio.Task[object], timeout: float) -> bool:
    """Give a pure handler its bounded cooperative drain window without waiting forever."""

    if task.done():
        try:
            task.result()
        except BaseException:
            pass
        return True
    done, _ = await asyncio.wait((task,), timeout=timeout)
    if task not in done:
        task.cancel()
        task.add_done_callback(_consume_task_result)
        return False
    try:
        task.result()
    except BaseException:
        pass
    return True


def _consume_task_result(task: asyncio.Task[object]) -> None:
    try:
        task.result()
    except BaseException:
        pass


class HeartbeatPureWorkerSupervisor:
    """Private PURE/fake heartbeat loop with an optional result-acceptance seam."""

    def __init__(
        self,
        admission: ScopedInvocationWorkerAdmissionV3,
        *,
        heartbeat: Callable[[float], object],
    ) -> None:
        if type(admission) is not ScopedInvocationWorkerAdmissionV3:
            raise TypeError("supervisor requires exact scoped worker admission")
        if not callable(heartbeat):
            raise TypeError("heartbeat callback must be callable")
        self.admission = admission
        self._heartbeat = heartbeat

    async def _heartbeat_once(self) -> bool:
        try:
            result = await _maybe_await(self._heartbeat(self.admission.configuration.lease_seconds))
        except asyncio.CancelledError:
            raise
        except BaseException:
            return False
        return type(result) is bool and result

    async def _heartbeat_loop(self, lost: asyncio.Event, stopped: asyncio.Event) -> None:
        interval = self.admission.configuration.heartbeat_interval_seconds
        while not stopped.is_set():
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            if stopped.is_set():
                return
            if not await self._heartbeat_once():
                lost.set()
                return

    async def run(
        self,
        handler: Callable[[PureWorkerContext], Awaitable[object]],
        *,
        cancellation: asyncio.Event | None = None,
        acceptance: Callable[[object], object] | None = None,
    ) -> PureWorkerRunResult:
        """Run after a first heartbeat and keep fencing active through acceptance.

        ``acceptance`` is an internal composition callback.  It is called only for a
        returned handler value while the heartbeat task is still running.  Its result
        must be an exact ``AcceptedV2`` or ``ObservedV2``; all other values and all
        callback failures become a sanitized non-success outcome.  The public gate
        remains disabled, so this seam cannot dispatch product work by itself.
        """

        if not callable(handler):
            raise TypeError("pure worker handler must be callable")
        if cancellation is not None and type(cancellation) is not asyncio.Event:
            raise TypeError("worker cancellation must be an exact asyncio.Event")
        if acceptance is not None and not callable(acceptance):
            raise TypeError("result acceptance callback must be callable")
        if cancellation is not None and cancellation.is_set():
            return PureWorkerRunResult(PureWorkerOutcome.CANCELED)
        if not await self._heartbeat_once():
            return PureWorkerRunResult(PureWorkerOutcome.LEASE_LOST)

        local_cancel = asyncio.Event()
        context = PureWorkerContext(self.admission.manifest, local_cancel)
        try:
            awaitable = handler(context)
        except BaseException:
            return PureWorkerRunResult(PureWorkerOutcome.FAILED)
        if not inspect.isawaitable(awaitable):
            return PureWorkerRunResult(PureWorkerOutcome.FAILED)
        handler_task: asyncio.Task[object] = asyncio.create_task(_maybe_await(awaitable))
        lost = asyncio.Event()
        stopped = asyncio.Event()
        heartbeat_task = asyncio.create_task(self._heartbeat_loop(lost, stopped))
        lost_waiter = asyncio.create_task(lost.wait())
        timeout_waiter = asyncio.create_task(
            asyncio.sleep(self.admission.configuration.handler_timeout_seconds)
        )
        cancel_waiter = None if cancellation is None else asyncio.create_task(cancellation.wait())
        watchers: set[asyncio.Task[object]] = {handler_task, lost_waiter, timeout_waiter}
        if cancel_waiter is not None:
            watchers.add(cancel_waiter)
        drained = True
        try:
            done, _ = await asyncio.wait(watchers, return_when=asyncio.FIRST_COMPLETED)
            # Any non-handler signal winning the same event-loop turn is treated as a
            # fence/timeout, never as a successful handler completion.
            if lost_waiter in done:
                outcome = PureWorkerOutcome.LEASE_LOST
            elif cancel_waiter is not None and cancel_waiter in done:
                outcome = PureWorkerOutcome.CANCELED
            elif timeout_waiter in done:
                outcome = PureWorkerOutcome.TIMED_OUT
            elif handler_task in done:
                try:
                    value = handler_task.result()
                except BaseException:
                    return PureWorkerRunResult(PureWorkerOutcome.FAILED)
                if acceptance is None:
                    return PureWorkerRunResult(PureWorkerOutcome.RETURNED, value=value)

                async def call_acceptance() -> object:
                    if acceptance is None:  # pragma: no cover - narrowed above.
                        raise RuntimeError("result acceptance callback disappeared")
                    return await _maybe_await(acceptance(value))

                acceptance_task: asyncio.Task[object] = asyncio.create_task(call_acceptance())
                acceptance_timeout = asyncio.create_task(
                    asyncio.sleep(
                        max(
                            0.001,
                            self.admission.configuration.lease_seconds
                            - self.admission.configuration.handler_timeout_seconds,
                        )
                    )
                )
                acceptance_watchers: set[asyncio.Task[object]] = {
                    acceptance_task,
                    lost_waiter,
                    acceptance_timeout,
                }
                if cancel_waiter is not None:
                    acceptance_watchers.add(cancel_waiter)
                acceptance_done, _ = await asyncio.wait(
                    acceptance_watchers,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                try:
                    if lost_waiter in acceptance_done:
                        local_cancel.set()
                        await _cancel_and_drain(
                            acceptance_task,
                            self.admission.configuration.drain_timeout_seconds,
                        )
                        return PureWorkerRunResult(PureWorkerOutcome.LEASE_LOST)
                    if cancel_waiter is not None and cancel_waiter in acceptance_done:
                        local_cancel.set()
                        await _cancel_and_drain(
                            acceptance_task,
                            self.admission.configuration.drain_timeout_seconds,
                        )
                        return PureWorkerRunResult(PureWorkerOutcome.CANCELED)
                    if acceptance_timeout in acceptance_done:
                        local_cancel.set()
                        drained = await _cancel_and_drain(
                            acceptance_task,
                            self.admission.configuration.drain_timeout_seconds,
                        )
                        return PureWorkerRunResult(
                            PureWorkerOutcome.ACCEPTANCE_FAILED,
                            drained=drained,
                        )
                    try:
                        accepted_value = acceptance_task.result()
                    except BaseException:
                        return PureWorkerRunResult(PureWorkerOutcome.ACCEPTANCE_FAILED)
                    if type(accepted_value) is ScopedInvocationResultAcceptedV2:
                        return PureWorkerRunResult(
                            PureWorkerOutcome.ACCEPTED,
                            value=accepted_value,
                        )
                    if type(accepted_value) is ScopedInvocationResultObservedV2:
                        return PureWorkerRunResult(
                            PureWorkerOutcome.OBSERVED,
                            value=accepted_value,
                        )
                    return PureWorkerRunResult(PureWorkerOutcome.ACCEPTANCE_FAILED)
                finally:
                    acceptance_timeout.cancel()
                    if acceptance_task not in acceptance_done:
                        acceptance_task.cancel()
                    await asyncio.gather(
                        acceptance_timeout,
                        acceptance_task,
                        return_exceptions=True,
                    )
            else:  # pragma: no cover - asyncio.wait always returns one watcher.
                outcome = PureWorkerOutcome.FAILED
            local_cancel.set()
            drained = await _cancel_and_drain(
                handler_task,
                self.admission.configuration.drain_timeout_seconds,
            )
            return PureWorkerRunResult(outcome, drained=drained)
        except asyncio.CancelledError:
            # A caller may cancel the supervisor task itself while a handler is still
            # running (for example, a hard shutdown deadline).  Do not leave that
            # handler orphaned: signal cooperative cancellation and use the same
            # bounded drain contract as timeout/lease-loss paths.  The public result
            # remains a sanitized non-success classification.
            local_cancel.set()
            drained = await _cancel_and_drain(
                handler_task,
                self.admission.configuration.drain_timeout_seconds,
            )
            return PureWorkerRunResult(PureWorkerOutcome.CANCELED, drained=drained)
        finally:
            # Defensive cleanup for unexpected exceptions raised by the supervision
            # machinery itself.  Normal paths have already drained this task; this
            # branch prevents an internal error from leaking a live pure handler.
            if not handler_task.done():
                local_cancel.set()
                await _cancel_and_drain(
                    handler_task,
                    self.admission.configuration.drain_timeout_seconds,
                )
            stopped.set()
            heartbeat_task.cancel()
            for watcher in watchers:
                if watcher is not handler_task:
                    watcher.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
            await asyncio.gather(
                *(watcher for watcher in watchers if watcher is not handler_task),
                return_exceptions=True,
            )

    async def run_and_accept(
        self,
        handler: Callable[[PureWorkerContext], Awaitable[object]],
        *,
        acceptor: Callable[
            [ScopedInvocationResultAcceptanceRequestV2, ScopedInvocationStartClaimedV3],
            object,
        ],
        cancellation: asyncio.Event | None = None,
    ) -> PureWorkerRunResult:
        """Run a structured result through a store-owned acceptance callback.

        The handler may return only an exact capability-free acceptance request.  The
        supervisor supplies its snapped start claim to the acceptor, so a handler cannot
        substitute another invocation or lease.  This is a candidate composition seam;
        the product dispatch gate is intentionally not connected to it.
        """

        if not callable(acceptor):
            raise TypeError("result acceptor must be callable")

        async def accept(value: object) -> object:
            if type(value) is not ScopedInvocationResultAcceptanceRequestV2:
                raise TypeError("pure worker result must be an exact result acceptance request")
            return await _maybe_await(acceptor(value, self.admission.claim))

        return await self.run(
            handler,
            cancellation=cancellation,
            acceptance=accept,
        )


async def _disabled_dispatch() -> NoReturn:
    """Raise from an argument-free frame so caller work cannot enter the exception graph."""

    raise InvocationWorkerDisabledError() from None


class HeartbeatPureWorkerGate:
    """Default-off composition seam for the future supervised pure/fake worker."""

    @property
    def dispatch_enabled(self) -> bool:
        return False

    @staticmethod
    def prepare(
        claim: InvocationStartClaimed,
        manifest: InvocationExecutionManifest,
        configuration: InvocationWorkerConfiguration,
        *,
        handler_revision: str,
    ) -> InvocationWorkerAdmission:
        return InvocationWorkerAdmission(
            claim=claim,
            manifest=manifest,
            configuration=configuration,
            handler_revision=handler_revision,
        )

    @staticmethod
    def prepare_scoped_v3(
        claim: ScopedInvocationStartClaimedV3,
        manifest: ScopedInvocationExecutionManifestV2,
        configuration: InvocationWorkerConfiguration,
        *,
        handler_revision: str,
    ) -> ScopedInvocationWorkerAdmissionV3:
        """Validate scoped authority without making dispatch reachable."""

        return ScopedInvocationWorkerAdmissionV3(
            claim=claim,
            manifest=manifest,
            configuration=configuration,
            handler_revision=handler_revision,
        )

    def dispatch(self, _admission: object, _handler: object) -> Awaitable[NoReturn]:
        """Return a coroutine that always fails before inspecting caller-owned work."""

        return _disabled_dispatch()


__all__ = [
    "HeartbeatPureWorkerGate",
    "HeartbeatPureWorkerSupervisor",
    "InvocationWorkerAdmission",
    "InvocationWorkerConfiguration",
    "InvocationWorkerDisabledError",
    "PureWorkerContext",
    "PureWorkerOutcome",
    "PureWorkerRunResult",
    "ScopedInvocationWorkerAdmissionV3",
]
