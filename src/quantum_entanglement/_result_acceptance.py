"""Private, side-effect-free preparation for future atomic result acceptance."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import NoReturn

from ._result_artifact_transaction import (
    _prepare_result_artifact_batch,
    _PreparedResultArtifactBatch,
    _ResultArtifactMaterializationPlan,
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


def _prepared_timestamp(value: object, label: str) -> str:
    timestamp = _prepared_text(value, label)
    if len(timestamp) != 27 or not timestamp.endswith("Z"):
        raise ValueError(f"prepared result acceptance {label} is invalid")
    try:
        parsed = datetime.fromisoformat(timestamp[:-1] + "+00:00")
    except ValueError:
        raise ValueError(f"prepared result acceptance {label} is invalid") from None
    canonical = (
        parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )
    if timestamp != canonical:
        raise ValueError(f"prepared result acceptance {label} is invalid")
    return timestamp


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
        heartbeat_at = _prepared_timestamp(self.heartbeat_at, "heartbeat time")
        lease_expires_at = _prepared_timestamp(self.lease_expires_at, "lease expiry")
        if lease_expires_at <= heartbeat_at:
            raise ValueError("prepared result acceptance lease is not active")
        if type(self.expected_stream_version) is not int or self.expected_stream_version < 1:
            raise ValueError("prepared result acceptance stream version is invalid")
        if type(self.running_task_revision) is not int or self.running_task_revision < 1:
            raise ValueError("prepared result acceptance task revision is invalid")


_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN = object()


class _FreshResultAcceptanceWritePlanV2:
    """One owner-transaction-bound plan; it is neither Accepted nor durable evidence."""

    __slots__ = (
        "__active",
        "__artifact_plan",
        "__materialization_started",
        "__prepared",
        "__prerequisites",
    )

    def __init__(
        self,
        *,
        prepared: _PreparedScopedInvocationResultAcceptanceV2,
        prerequisites: _FreshResultAcceptancePrerequisitesV2,
        artifact_plan: _ResultArtifactMaterializationPlan,
        token: object,
    ) -> None:
        if type(self) is not _FreshResultAcceptanceWritePlanV2:
            raise TypeError("fresh result acceptance write plan must be exact")
        if token is not _RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN:
            raise TypeError("fresh result acceptance write plan constructor is private")
        if type(prepared) is not _PreparedScopedInvocationResultAcceptanceV2:
            raise TypeError("fresh result acceptance write plan inputs are not exact")
        prepared.verify()
        if type(prerequisites) is not _FreshResultAcceptancePrerequisitesV2:
            raise TypeError("fresh result acceptance prerequisites are not exact")
        _FreshResultAcceptancePrerequisitesV2.__post_init__(prerequisites)
        if type(artifact_plan) is not _ResultArtifactMaterializationPlan:
            raise TypeError("fresh result acceptance Artifact plan is not exact")
        self.__prepared = prepared
        self.__prerequisites = prerequisites
        self.__artifact_plan = artifact_plan
        self.__materialization_started = False
        self.__active = True

    def _validated(
        self,
        *,
        token: object,
    ) -> tuple[
        _PreparedScopedInvocationResultAcceptanceV2,
        _FreshResultAcceptancePrerequisitesV2,
        _ResultArtifactMaterializationPlan,
    ]:
        if type(self) is not _FreshResultAcceptanceWritePlanV2:
            raise TypeError("fresh result acceptance write plan must be exact")
        if token is not _RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN:
            raise TypeError("fresh result acceptance write plan validation is private")
        if type(self.__active) is not bool or not self.__active:
            raise RuntimeError("fresh result acceptance write plan is no longer active")
        self.__prepared.verify()
        _FreshResultAcceptancePrerequisitesV2.__post_init__(self.__prerequisites)
        return self.__prepared, self.__prerequisites, self.__artifact_plan

    def _begin_artifact_materialization(
        self,
        *,
        token: object,
    ) -> tuple[
        _PreparedScopedInvocationResultAcceptanceV2,
        _FreshResultAcceptancePrerequisitesV2,
        _ResultArtifactMaterializationPlan,
    ]:
        if token is not _RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN:
            raise TypeError("fresh result acceptance materialization is private")
        if type(self) is not _FreshResultAcceptanceWritePlanV2:
            raise TypeError("fresh result acceptance write plan must be exact")
        if type(self.__active) is not bool or not self.__active:
            raise RuntimeError("fresh result acceptance write plan is no longer active")
        if type(self.__materialization_started) is not bool:
            raise RuntimeError("fresh result acceptance write plan state is invalid")
        if self.__materialization_started:
            raise RuntimeError("fresh result acceptance materialization already started")
        self.__materialization_started = True
        self.__prepared.verify()
        _FreshResultAcceptancePrerequisitesV2.__post_init__(self.__prerequisites)
        if type(self.__artifact_plan) is not _ResultArtifactMaterializationPlan:
            raise RuntimeError("fresh result acceptance Artifact plan is invalid")
        return self.__prepared, self.__prerequisites, self.__artifact_plan

    def _invalidate(self, *, token: object) -> None:
        if token is not _RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN:
            raise TypeError("fresh result acceptance write plan invalidation is private")
        self.__active = False
        self.__materialization_started = True
        object.__setattr__(self, "_FreshResultAcceptanceWritePlanV2__artifact_plan", None)
        object.__setattr__(self, "_FreshResultAcceptanceWritePlanV2__prepared", None)
        object.__setattr__(self, "_FreshResultAcceptanceWritePlanV2__prerequisites", None)

    def __copy__(self) -> NoReturn:
        raise TypeError("fresh result acceptance write plans cannot be copied")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("fresh result acceptance write plans cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("fresh result acceptance write plans cannot be serialized")


class _MaterializedFreshResultAcceptancePlanV2:
    """One post-clock, post-Artifact plan; it remains private and grants no authority."""

    __slots__ = (
        "__accepted_at",
        "__active",
        "__artifacts",
        "__identity_allocation_started",
        "__prepared",
        "__prerequisites",
    )

    def __init__(
        self,
        *,
        prepared: _PreparedScopedInvocationResultAcceptanceV2,
        prerequisites: _FreshResultAcceptancePrerequisitesV2,
        accepted_at: str,
        artifacts: tuple[object, ...],
        token: object,
    ) -> None:
        if type(self) is not _MaterializedFreshResultAcceptancePlanV2:
            raise TypeError("materialized result acceptance plan must be exact")
        if token is not _RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN:
            raise TypeError("materialized result acceptance plan constructor is private")
        self.__prepared = prepared
        self.__prerequisites = prerequisites
        self.__accepted_at = accepted_at
        self.__artifacts = artifacts
        self.__identity_allocation_started = False
        self.__active = True
        self._validated(token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN)

    def _validated(
        self,
        *,
        token: object,
    ) -> tuple[
        _PreparedScopedInvocationResultAcceptanceV2,
        _FreshResultAcceptancePrerequisitesV2,
        str,
        tuple[object, ...],
    ]:
        if type(self) is not _MaterializedFreshResultAcceptancePlanV2:
            raise TypeError("materialized result acceptance plan must be exact")
        if token is not _RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN:
            raise TypeError("materialized result acceptance plan validation is private")
        if type(self.__active) is not bool or not self.__active:
            raise RuntimeError("materialized result acceptance plan is no longer active")
        if type(self.__prepared) is not _PreparedScopedInvocationResultAcceptanceV2:
            raise TypeError("materialized result acceptance inputs are not exact")
        self.__prepared.verify()
        if type(self.__prerequisites) is not _FreshResultAcceptancePrerequisitesV2:
            raise TypeError("materialized result acceptance prerequisites are not exact")
        _FreshResultAcceptancePrerequisitesV2.__post_init__(self.__prerequisites)
        accepted_at = _prepared_timestamp(self.__accepted_at, "accepted time")
        if accepted_at < self.__prerequisites.heartbeat_at:
            raise ValueError("materialized result acceptance time precedes its heartbeat")
        if accepted_at >= self.__prerequisites.lease_expires_at:
            raise ValueError("materialized result acceptance time is outside its lease")
        if type(self.__artifacts) is not tuple:
            raise TypeError("materialized result acceptance Artifacts are not exact")
        expected_artifacts = tuple(item.descriptor for item in self.__prepared.artifact_batch.items)
        if self.__artifacts != expected_artifacts:
            raise ValueError("materialized result acceptance Artifact order changed")
        return (
            self.__prepared,
            self.__prerequisites,
            accepted_at,
            self.__artifacts,
        )

    def _begin_identity_allocation(self, *, token: object) -> None:
        if token is not _RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN:
            raise TypeError("result acceptance identity allocation is private")
        if type(self) is not _MaterializedFreshResultAcceptancePlanV2:
            raise TypeError("materialized result acceptance plan must be exact")
        if type(self.__active) is not bool or not self.__active:
            raise RuntimeError("materialized result acceptance plan is no longer active")
        if type(self.__identity_allocation_started) is not bool:
            raise RuntimeError("materialized result acceptance identity state is invalid")
        if self.__identity_allocation_started:
            raise RuntimeError("result acceptance identity allocation already started")
        self.__identity_allocation_started = True
        self._validated(token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN)

    def _invalidate(self, *, token: object) -> None:
        if token is not _RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN:
            raise TypeError("materialized result acceptance plan invalidation is private")
        self.__active = False
        self.__identity_allocation_started = True
        object.__setattr__(
            self,
            "_MaterializedFreshResultAcceptancePlanV2__prepared",
            None,
        )
        object.__setattr__(
            self,
            "_MaterializedFreshResultAcceptancePlanV2__prerequisites",
            None,
        )
        object.__setattr__(
            self,
            "_MaterializedFreshResultAcceptancePlanV2__artifacts",
            (),
        )

    def __copy__(self) -> NoReturn:
        raise TypeError("materialized result acceptance plans cannot be copied")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("materialized result acceptance plans cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("materialized result acceptance plans cannot be serialized")


class _IdentifiedFreshResultAcceptancePlanV2:
    """One store-identified fresh plan; IDs are candidates until the full graph commits."""

    __slots__ = (
        "__active",
        "__materialized",
        "__receipt_id",
        "__result_event_id",
        "__terminal_event_id",
    )

    def __init__(
        self,
        *,
        materialized: _MaterializedFreshResultAcceptancePlanV2,
        receipt_id: str,
        result_event_id: str,
        terminal_event_id: str,
        token: object,
    ) -> None:
        if type(self) is not _IdentifiedFreshResultAcceptancePlanV2:
            raise TypeError("identified result acceptance plan must be exact")
        if token is not _RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN:
            raise TypeError("identified result acceptance plan constructor is private")
        self.__materialized = materialized
        self.__receipt_id = receipt_id
        self.__result_event_id = result_event_id
        self.__terminal_event_id = terminal_event_id
        self.__active = True
        self._validated(token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN)

    def _validated(
        self,
        *,
        token: object,
    ) -> tuple[_MaterializedFreshResultAcceptancePlanV2, str, str, str]:
        if type(self) is not _IdentifiedFreshResultAcceptancePlanV2:
            raise TypeError("identified result acceptance plan must be exact")
        if token is not _RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN:
            raise TypeError("identified result acceptance plan validation is private")
        if type(self.__active) is not bool or not self.__active:
            raise RuntimeError("identified result acceptance plan is no longer active")
        if type(self.__materialized) is not _MaterializedFreshResultAcceptancePlanV2:
            raise TypeError("identified result acceptance materialization is not exact")
        prepared, _, _, _ = self.__materialized._validated(
            token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN
        )
        receipt_id = _prepared_text(self.__receipt_id, "receipt identity")
        result_event_id = _prepared_text(self.__result_event_id, "result event identity")
        terminal_event_id = _prepared_text(
            self.__terminal_event_id,
            "terminal event identity",
        )
        if len({receipt_id, result_event_id, terminal_event_id}) != 3:
            raise ValueError("result acceptance store identities are not distinct")
        start_event_id = prepared.request.start_receipt.event_id
        if result_event_id == start_event_id or terminal_event_id == start_event_id:
            raise ValueError("result acceptance event identity reuses the start event")
        return self.__materialized, receipt_id, result_event_id, terminal_event_id

    def _invalidate(self, *, token: object) -> None:
        if token is not _RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN:
            raise TypeError("identified result acceptance plan invalidation is private")
        self.__active = False
        object.__setattr__(
            self,
            "_IdentifiedFreshResultAcceptancePlanV2__materialized",
            None,
        )

    def __copy__(self) -> NoReturn:
        raise TypeError("identified result acceptance plans cannot be copied")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("identified result acceptance plans cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("identified result acceptance plans cannot be serialized")


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
