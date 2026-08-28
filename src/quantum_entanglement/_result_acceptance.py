"""Private, side-effect-free preparation for future atomic result acceptance."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import NoReturn

from ._result_artifact_transaction import (
    _prepare_result_artifact_batch,
    _PreparedResultArtifactBatch,
)
from .invocation_execution import ScopedInvocationStartClaimedV3
from .invocation_results import (
    ScopedInvocationResultAcceptanceRequestV2,
    _acceptance_request_snapshot,
)


class _ResultAcceptanceSchemaUnavailableError(RuntimeError):
    """The inactive result schema is absent or not exact for private M5 work."""


class _ResultAcceptanceConflictError(RuntimeError):
    """A durable identity or fresh lease prerequisite differs from the request."""


class _ResultAcceptanceIntegrityError(RuntimeError):
    """A candidate result/start graph is partial, malformed, or contradictory."""


def _prepared_text(value: object, label: str) -> str:
    if type(value) is not str or not value or len(value.encode("utf-8")) > 4_096:
        raise ValueError(f"prepared result acceptance {label} is invalid")
    return value


def _prepared_digest(value: object, label: str) -> str:
    digest = _prepared_text(value, label)
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError(f"prepared result acceptance {label} is invalid")
    return digest


@dataclass(frozen=True)
class _ExistingResultAcceptanceGraphCandidateV2:
    """Structurally complete candidate that still requires exact full-graph readback."""

    invocation_id: str
    request_digest: str
    receipt_id: str
    receipt_digest: str
    artifact_count: int

    def __post_init__(self) -> None:
        if type(self) is not _ExistingResultAcceptanceGraphCandidateV2:
            raise TypeError("existing result graph candidate must use the exact private class")
        _prepared_text(self.invocation_id, "invocation identity")
        _prepared_digest(self.request_digest, "request digest")
        _prepared_text(self.receipt_id, "receipt identity")
        _prepared_digest(self.receipt_digest, "receipt digest")
        if type(self.artifact_count) is not int or not 0 <= self.artifact_count <= 256:
            raise ValueError("prepared result acceptance artifact count is invalid")


@dataclass(frozen=True)
class _FreshResultAcceptancePrerequisitesV2:
    """Sanitized durable bindings for a fresh writer; it carries no plaintext lease."""

    invocation_id: str
    request_digest: str
    start_receipt_digest: str
    attempt_id: str
    lease_epoch: int
    worker_id: str
    lease_token_digest: str = field(repr=False)
    heartbeat_at: str
    lease_expires_at: str
    expected_stream_version: int
    running_task_revision: int

    def __post_init__(self) -> None:
        if type(self) is not _FreshResultAcceptancePrerequisitesV2:
            raise TypeError("fresh result prerequisites must use the exact private class")
        _prepared_text(self.invocation_id, "invocation identity")
        _prepared_digest(self.request_digest, "request digest")
        _prepared_digest(self.start_receipt_digest, "start receipt digest")
        _prepared_text(self.attempt_id, "attempt identity")
        if type(self.lease_epoch) is not int or self.lease_epoch <= 0:
            raise ValueError("prepared result acceptance lease epoch is invalid")
        _prepared_text(self.worker_id, "worker identity")
        _prepared_digest(self.lease_token_digest, "lease token digest")
        _prepared_text(self.heartbeat_at, "heartbeat time")
        _prepared_text(self.lease_expires_at, "lease expiry")
        if type(self.expected_stream_version) is not int or self.expected_stream_version < 1:
            raise ValueError("prepared result acceptance stream version is invalid")
        if type(self.running_task_revision) is not int or self.running_task_revision < 1:
            raise ValueError("prepared result acceptance task revision is invalid")


def _scoped_invocation_start_claimed_snapshot(
    claimed: object,
) -> ScopedInvocationStartClaimedV3:
    if type(claimed) is not ScopedInvocationStartClaimedV3:
        raise TypeError("claimed start must be exact ScopedInvocationStartClaimedV3")
    return ScopedInvocationStartClaimedV3(
        receipt=object.__getattribute__(claimed, "receipt"),
        lease=object.__getattribute__(claimed, "lease"),
    )


@dataclass(frozen=True)
class _PreparedScopedInvocationResultAcceptanceV2:
    """Caller-detached inputs; the plaintext lease never enters repr or diagnostics."""

    request: ScopedInvocationResultAcceptanceRequestV2 = field(repr=False)
    claimed: ScopedInvocationStartClaimedV3 = field(repr=False)
    artifact_batch: _PreparedResultArtifactBatch = field(repr=False)

    def verify(self) -> None:
        if type(self) is not _PreparedScopedInvocationResultAcceptanceV2:
            raise TypeError("prepared result acceptance must use the exact private class")
        request = _acceptance_request_snapshot(object.__getattribute__(self, "request"))
        claimed = _scoped_invocation_start_claimed_snapshot(
            object.__getattribute__(self, "claimed")
        )
        if request.start_receipt != claimed.receipt:
            raise ValueError("result acceptance request does not match the claimed start")
        artifact_batch = object.__getattribute__(self, "artifact_batch")
        if type(artifact_batch) is not _PreparedResultArtifactBatch:
            raise TypeError("prepared result acceptance requires an exact Artifact batch")
        artifact_batch.verify()
        expected_batch = _prepare_result_artifact_batch(request.artifact_candidates)
        if artifact_batch != expected_batch:
            raise ValueError("prepared result acceptance Artifact batch changed")

    def __copy__(self) -> NoReturn:
        raise TypeError("prepared result acceptance cannot be copied")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("prepared result acceptance cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("prepared result acceptance cannot be serialized")


def _prepare_scoped_invocation_result_acceptance_v2(
    request: object,
    claimed: object,
) -> _PreparedScopedInvocationResultAcceptanceV2:
    """Freeze an exact request/claim pair without clock, ID, SQLite, or authority minting."""

    request_snapshot = _acceptance_request_snapshot(request)
    claimed_snapshot = _scoped_invocation_start_claimed_snapshot(claimed)
    if request_snapshot.start_receipt != claimed_snapshot.receipt:
        raise ValueError("result acceptance request does not match the claimed start")
    prepared = _PreparedScopedInvocationResultAcceptanceV2(
        request=request_snapshot,
        claimed=claimed_snapshot,
        artifact_batch=_prepare_result_artifact_batch(request_snapshot.artifact_candidates),
    )
    prepared.verify()
    return prepared
