from __future__ import annotations

import inspect
from dataclasses import replace

import pytest

from quantum_entanglement.native_im import IMCapabilityRequestV1
from quantum_entanglement.native_im_gateway import (
    IMGatewayPort,
    validate_im_acceptance_result_v1,
    validate_im_capability_result_v1,
    validate_im_dispatch_result_v1,
    validate_im_inbound_result_v1,
)
from tests.test_native_im_contract import (
    acceptance_query,
    action_command,
    action_receipt,
    capability,
    dispatch_request,
    inbound_page,
    inbound_read_request,
    lookup,
    operation,
)


def capability_request(**changes: object) -> IMCapabilityRequestV1:
    values: dict[str, object] = {
        "schema_version": 1,
        "tenant_id": "test-tenant",
        "workspace_id": "test-workspace",
        "provider": "qe.fake-im.v1",
        "channel_id": "test-channel",
        "request_id": "test-capability-request-1",
    }
    values.update(changes)
    return IMCapabilityRequestV1(**values)  # type: ignore[arg-type]


class StructurallyCompleteGateway:
    async def capability_snapshot(self, request):
        return capability()

    async def read_inbound(self, request):
        return inbound_page(request=request)

    async def dispatch(self, request):
        return action_receipt("effect_unknown", request=request)

    async def query_acceptance(self, query):
        return action_receipt("effect_unknown")


def test_gateway_port_freezes_only_four_async_methods() -> None:
    public_methods = {
        name: value
        for name, value in IMGatewayPort.__dict__.items()
        if callable(value) and not name.startswith("_")
    }
    assert set(public_methods) == {
        "capability_snapshot",
        "read_inbound",
        "dispatch",
        "query_acceptance",
    }
    assert all(inspect.iscoroutinefunction(value) for value in public_methods.values())
    assert isinstance(StructurallyCompleteGateway(), IMGatewayPort)


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("tenant_id", "test-other-tenant"),
        ("workspace_id", "test-other-workspace"),
        ("provider", "qe.other-im.v1"),
        ("channel_id", "test-other-channel"),
    ],
)
def test_capability_result_admission_binds_every_scope_axis(field: str, changed: str) -> None:
    request = capability_request()
    result = capability()
    assert validate_im_capability_result_v1(request, result) is result
    with pytest.raises(ValueError, match="scope"):
        validate_im_capability_result_v1(request, replace(result, **{field: changed}))


def test_capability_result_admission_requires_exact_models() -> None:
    with pytest.raises(TypeError, match="exact IMCapabilitySnapshotV1"):
        validate_im_capability_result_v1(capability_request(), object())  # type: ignore[arg-type]


def test_inbound_result_admission_binds_request_and_capability() -> None:
    request = inbound_read_request()
    snapshot = capability()
    result = inbound_page(request=request, capability=snapshot)
    assert validate_im_inbound_result_v1(request, snapshot, result) is result
    with pytest.raises(ValueError, match="readRequestId"):
        validate_im_inbound_result_v1(
            replace(request, read_request_id="test-read-request-other"), snapshot, result
        )
    with pytest.raises(ValueError, match="capabilityRevision"):
        validate_im_inbound_result_v1(
            request, replace(snapshot, revision="test-capability-other"), result
        )


@pytest.mark.parametrize(
    "state",
    ["succeeded", "rejected", "retryable_not_accepted", "effect_unknown"],
)
def test_dispatch_result_admission_accepts_only_dispatch_states(state: str) -> None:
    request = dispatch_request()
    result = action_receipt(state, request=request)
    assert validate_im_dispatch_result_v1(request, result) is result


@pytest.mark.parametrize("state", ["reconciled_succeeded", "reconciled_rejected"])
def test_dispatch_result_admission_rejects_query_states(state: str) -> None:
    request = dispatch_request()
    with pytest.raises(ValueError, match="reconciled"):
        validate_im_dispatch_result_v1(request, action_receipt(state, request=request))


@pytest.mark.parametrize("state", ["reconciled_succeeded", "reconciled_rejected", "effect_unknown"])
def test_acceptance_result_admission_accepts_only_query_states(state: str) -> None:
    snapshot = capability()
    request = dispatch_request(command=action_command(capability=snapshot))
    query = acceptance_query(request=request)
    result = action_receipt(state, request=request, causation_id=query.query_id)
    assert validate_im_acceptance_result_v1(query, request, snapshot, result) is result


@pytest.mark.parametrize("state", ["succeeded", "rejected", "retryable_not_accepted"])
def test_acceptance_result_admission_rejects_dispatch_only_states(state: str) -> None:
    snapshot = capability()
    request = dispatch_request(command=action_command(capability=snapshot))
    query = acceptance_query(request=request)
    result = action_receipt(state, request=request, causation_id=query.query_id)
    with pytest.raises(ValueError, match="dispatch-only"):
        validate_im_acceptance_result_v1(query, request, snapshot, result)


def test_acceptance_result_admission_rejects_impossible_final_negative() -> None:
    unavailable = lookup(negative_acceptance_mode="unavailable")
    profile = operation(acceptance_lookups=(unavailable,))
    snapshot = capability(operations=(profile,))
    request = dispatch_request(command=action_command(capability=snapshot))
    query = acceptance_query(request=request)
    result = action_receipt("reconciled_rejected", request=request, causation_id=query.query_id)
    with pytest.raises(ValueError, match="final negative"):
        validate_im_acceptance_result_v1(query, request, snapshot, result)
