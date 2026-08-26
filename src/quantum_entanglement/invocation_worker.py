# ruff: noqa: UP006, UP035
"""Fail-closed admission models for the future heartbeat-supervised worker.

This module deliberately cannot dispatch work yet.  It snapshots and validates the exact
non-replayable invocation-start authority that a future pure/fake worker may consume, while the
gate's dispatch path always fails before inspecting caller work.  Atomic result acceptance and
receipt-bound recovery must land before the gate can be promoted.
"""

from __future__ import annotations

import math
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import NoReturn, cast

from .invocation_execution import (
    EffectClass,
    InvocationExecutionManifest,
    InvocationStartClaimed,
    RetryClass,
    ScopedInvocationExecutionManifestV2,
    ScopedInvocationStartClaimedV3,
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
    "InvocationWorkerAdmission",
    "InvocationWorkerConfiguration",
    "InvocationWorkerDisabledError",
    "ScopedInvocationWorkerAdmissionV3",
]
