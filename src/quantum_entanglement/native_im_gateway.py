"""Provider-neutral native-IM port and pure V1 result-admission gates."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .native_im import (
    IMAcceptanceQueryV1,
    IMActionReceiptV1,
    IMCapabilityRequestV1,
    IMCapabilitySnapshotV1,
    IMDispatchRequestV1,
    IMInboundPageV1,
    IMInboundReadRequestV1,
)


@runtime_checkable
class IMGatewayPort(Protocol):
    """The complete V1 boundary implemented by a native-IM provider adapter."""

    async def capability_snapshot(self, request: IMCapabilityRequestV1) -> IMCapabilitySnapshotV1:
        """Return the exact capability snapshot for the requested scope."""

    async def read_inbound(self, request: IMInboundReadRequestV1) -> IMInboundPageV1:
        """Read one deterministic, resume-bound page of verified inbound envelopes."""

    async def dispatch(self, request: IMDispatchRequestV1) -> IMActionReceiptV1:
        """Attempt one fenced action dispatch and return a receiver receipt."""

    async def query_acceptance(self, query: IMAcceptanceQueryV1) -> IMActionReceiptV1:
        """Reconcile one durable effect-unknown action without redispatching it."""


def _scope(value: object) -> tuple[object, object, object, object]:
    return (
        getattr(value, "tenant_id", None),
        getattr(value, "workspace_id", None),
        getattr(value, "provider", None),
        getattr(value, "channel_id", None),
    )


def _require_exact(value: object, expected: type[object], label: str) -> None:
    if type(value) is not expected:
        raise TypeError(f"{label} must be the exact {expected.__name__} model")


def validate_im_capability_result_v1(
    request: IMCapabilityRequestV1,
    result: IMCapabilitySnapshotV1,
) -> IMCapabilitySnapshotV1:
    """Purely admit a capability result bound to the exact requested scope."""

    _require_exact(request, IMCapabilityRequestV1, "capability request")
    _require_exact(result, IMCapabilitySnapshotV1, "capability result")
    if _scope(result) != _scope(request):
        raise ValueError("capability result scope does not match its request")
    return result


def validate_im_inbound_result_v1(
    request: IMInboundReadRequestV1,
    capability: IMCapabilitySnapshotV1,
    result: IMInboundPageV1,
) -> IMInboundPageV1:
    """Purely admit a page bound to its request and trusted capability snapshot."""

    _require_exact(request, IMInboundReadRequestV1, "inbound read request")
    _require_exact(capability, IMCapabilitySnapshotV1, "capability snapshot")
    _require_exact(result, IMInboundPageV1, "inbound result")
    if _scope(capability) != _scope(request):
        raise ValueError("inbound capability scope does not match its request")
    result.validate_request_binding(request)
    result.validate_capability_binding(capability)
    return result


def validate_im_dispatch_result_v1(
    request: IMDispatchRequestV1,
    result: IMActionReceiptV1,
) -> IMActionReceiptV1:
    """Purely admit one of the four dispatch-only receipt states."""

    _require_exact(request, IMDispatchRequestV1, "dispatch request")
    _require_exact(result, IMActionReceiptV1, "dispatch result")
    result.validate_dispatch_binding(request)
    return result


def validate_im_acceptance_result_v1(
    query: IMAcceptanceQueryV1,
    request: IMDispatchRequestV1,
    capability: IMCapabilitySnapshotV1,
    result: IMActionReceiptV1,
) -> IMActionReceiptV1:
    """Purely admit a query-only result, including static negative-finality gating."""

    _require_exact(query, IMAcceptanceQueryV1, "acceptance query")
    _require_exact(request, IMDispatchRequestV1, "dispatch request")
    _require_exact(capability, IMCapabilitySnapshotV1, "capability snapshot")
    _require_exact(result, IMActionReceiptV1, "acceptance result")
    result.validate_query_capability_binding(query, request, capability)
    return result


__all__ = [
    "IMGatewayPort",
    "validate_im_acceptance_result_v1",
    "validate_im_capability_result_v1",
    "validate_im_dispatch_result_v1",
    "validate_im_inbound_result_v1",
]
