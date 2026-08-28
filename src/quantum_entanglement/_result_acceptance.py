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
