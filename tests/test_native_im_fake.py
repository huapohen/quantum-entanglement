from __future__ import annotations

import ast
import copy
import inspect
import os
import pickle
import socket
from dataclasses import replace
from pathlib import Path

import pytest

from quantum_entanglement.native_im import IMInboundReadRequestV1
from quantum_entanglement.native_im_fake import (
    FAKE_IM_PROVIDER,
    FakeIMAdapter,
    FakeIMFaultScript,
    FakeIMOutboundDisabledError,
    FakeIMReceiverCollisionError,
    FakeIMTestOutboundPermit,
)
from quantum_entanglement.native_im_gateway import (
    IMGatewayPort,
    validate_im_acceptance_result_v1,
    validate_im_dispatch_result_v1,
)
from tests.test_native_im_contract import (
    acceptance_query,
    action_command,
    action_intent,
    action_receipt,
    attachment,
    capability,
    content,
    dispatch_request,
    inbound_event,
    text_segment,
    verified_envelope,
)
from tests.test_native_im_gateway import capability_request

SOURCE_PATH = (
    Path(__file__).resolve().parents[1] / "src" / "quantum_entanglement" / "native_im_fake.py"
)


def envelopes(count: int = 3):
    return tuple(
        verified_envelope(
            event=inbound_event(
                event_id=f"test-event-{index}",
                cursor=f"test-cursor-{index}",
                sequence_number=index,
            ),
            verification_id=f"test-verification-{index}",
        )
        for index in range(1, count + 1)
    )


def adapter(*, event_values=None, snapshot=None, **scope):
    return FakeIMAdapter(
        tenant_id=scope.get("tenant_id", "test-tenant"),
        workspace_id=scope.get("workspace_id", "test-workspace"),
        channel_id=scope.get("channel_id", "test-channel"),
        capability=snapshot or capability(),
        envelopes=event_values if event_values is not None else envelopes(),
    )


def outbound_adapter(*, snapshot=None, outbound_permit=None, fault_script=None):
    return FakeIMAdapter.for_test(
        tenant_id="test-tenant",
        workspace_id="test-workspace",
        channel_id="test-channel",
        capability=snapshot or capability(),
        envelopes=envelopes(),
        outbound_permit=outbound_permit or FakeIMTestOutboundPermit(),
        fault_script=fault_script,
    )


def read_request(**changes: object) -> IMInboundReadRequestV1:
    values: dict[str, object] = {
        "schema_version": 1,
        "tenant_id": "test-tenant",
        "workspace_id": "test-workspace",
        "provider": FAKE_IM_PROVIDER,
        "channel_id": "test-channel",
        "after_cursor": None,
        "after_sequence": None,
        "snapshot_token": None,
        "limit": 2,
        "read_request_id": "test-read-request-1",
    }
    values.update(changes)
    return IMInboundReadRequestV1(**values)  # type: ignore[arg-type]


def test_fake_constructor_exposes_no_endpoint_or_credential_input() -> None:
    assert tuple(inspect.signature(FakeIMAdapter).parameters) == (
        "tenant_id",
        "workspace_id",
        "channel_id",
        "capability",
        "envelopes",
    )
    fake = adapter()
    assert isinstance(fake, IMGatewayPort)
    with pytest.raises(AttributeError):
        fake._envelopes = ()
    with pytest.raises(AttributeError):
        fake.endpoint = "forbidden"  # type: ignore[attr-defined]


@pytest.mark.parametrize("field", ["tenant_id", "workspace_id", "channel_id"])
def test_fake_requires_reserved_test_scope(field: str) -> None:
    with pytest.raises(ValueError, match="reserved test- prefix"):
        adapter(**{field: "production-scope"})


def test_fake_fixes_provider_and_exact_scope() -> None:
    with pytest.raises(ValueError, match="capability scope"):
        adapter(snapshot=replace(capability(), provider="qe.other-im.v1"))
    with pytest.raises(ValueError, match="envelope scope"):
        adapter(
            tenant_id="test-other-tenant",
            snapshot=replace(capability(), tenant_id="test-other-tenant"),
        )


def test_fake_requires_immutable_ordered_unique_envelopes() -> None:
    with pytest.raises(TypeError, match="immutable tuple"):
        adapter(event_values=list(envelopes()))
    first = envelopes(1)[0]
    duplicate_sequence = verified_envelope(
        event=inbound_event(event_id="test-event-other", cursor="test-cursor-other"),
        verification_id="test-verification-other",
    )
    with pytest.raises(ValueError, match="strictly increase"):
        adapter(event_values=(first, duplicate_sequence))
    duplicate_id = verified_envelope(
        event=inbound_event(event_id="test-event-1", cursor="test-cursor-2", sequence_number=2),
        verification_id="test-verification-2",
    )
    with pytest.raises(ValueError, match="event IDs"):
        adapter(event_values=(first, duplicate_id))


@pytest.mark.asyncio
async def test_fake_capability_is_exactly_scope_bound() -> None:
    fake = adapter()
    snapshot = await fake.capability_snapshot(capability_request())
    assert snapshot == capability()
    with pytest.raises(ValueError, match="scope"):
        await fake.capability_snapshot(capability_request(channel_id="test-other-channel"))


@pytest.mark.asyncio
async def test_fake_read_uses_cursor_pairs_without_process_offset() -> None:
    fake = adapter()
    initial = read_request()
    first = await fake.read_inbound(initial)
    repeated = await fake.read_inbound(initial)
    assert first.canonical_bytes() == repeated.canonical_bytes()
    assert tuple(envelope.event.sequence_number for envelope in first.envelopes) == (1, 2)
    assert first.has_more is True
    assert (first.next_cursor, first.next_sequence) == ("test-cursor-2", 2)

    continuation = read_request(
        after_cursor=first.next_cursor,
        after_sequence=first.next_sequence,
        snapshot_token=first.snapshot_token,
        read_request_id="test-read-request-2",
    )
    second = await fake.read_inbound(continuation)
    assert tuple(envelope.event.sequence_number for envelope in second.envelopes) == (3,)
    assert second.has_more is False
    assert (second.next_cursor, second.next_sequence) == ("test-cursor-3", 3)

    exhausted = read_request(
        after_cursor=second.next_cursor,
        after_sequence=second.next_sequence,
        snapshot_token=second.snapshot_token,
        read_request_id="test-read-request-3",
    )
    empty = await fake.read_inbound(exhausted)
    assert empty.envelopes == ()
    assert empty.has_more is False
    assert (empty.next_cursor, empty.next_sequence) == ("test-cursor-3", 3)


@pytest.mark.asyncio
async def test_fake_read_rejects_unknown_resume_or_snapshot() -> None:
    fake = adapter()
    with pytest.raises(ValueError, match="resume pair"):
        await fake.read_inbound(
            read_request(
                after_cursor="test-cursor-unknown",
                after_sequence=99,
                snapshot_token="test-fake-im-snapshot-v1",
            )
        )
    with pytest.raises(ValueError, match="snapshot token"):
        await fake.read_inbound(
            read_request(
                after_cursor="test-cursor-1",
                after_sequence=1,
                snapshot_token="test-other-snapshot",
            )
        )


class ExplosiveOutboundRequest:
    def __getattribute__(self, name):
        raise AssertionError(f"outbound request was inspected: {name}")


@pytest.mark.asyncio
async def test_fake_outbound_fails_before_inspecting_request() -> None:
    fake = adapter()
    with pytest.raises(FakeIMOutboundDisabledError, match="^fake IM outbound is disabled$"):
        await fake.dispatch(ExplosiveOutboundRequest())  # type: ignore[arg-type]
    with pytest.raises(FakeIMOutboundDisabledError, match="^fake IM outbound is disabled$"):
        await fake.query_acceptance(ExplosiveOutboundRequest())  # type: ignore[arg-type]


def test_fake_test_outbound_permit_is_immutable_process_local_and_unserializable(
    monkeypatch,
) -> None:
    permit = FakeIMTestOutboundPermit()
    assert repr(permit) == "FakeIMTestOutboundPermit(process_local=True)"
    with pytest.raises(AttributeError, match="immutable"):
        permit._process_id = os.getpid()
    with pytest.raises(TypeError, match="cannot be serialized"):
        pickle.dumps(permit)
    with pytest.raises(TypeError, match="cannot be serialized"):
        copy.copy(permit)
    monkeypatch.setattr(os, "getpid", lambda: permit._process_id + 1)
    with pytest.raises(FakeIMOutboundDisabledError, match="not process-current"):
        outbound_adapter(outbound_permit=permit)


def test_fake_fault_script_is_closed_and_immutable() -> None:
    script = FakeIMFaultScript(
        dispatch_steps=("ack_loss_after_accept",),
        query_steps=("not_final", "ledger"),
    )
    assert repr(script) == "FakeIMFaultScript(dispatch_steps=1, query_steps=2)"
    with pytest.raises(TypeError, match="immutable tuples"):
        FakeIMFaultScript(dispatch_steps=["accept"])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="dispatch fault step"):
        FakeIMFaultScript(dispatch_steps=("future_fault",))
    with pytest.raises(ValueError, match="query fault step"):
        FakeIMFaultScript(query_steps=("future_fault",))


@pytest.mark.asyncio
async def test_fake_receiver_accepts_once_and_replays_the_same_effect() -> None:
    snapshot = capability()
    fake = outbound_adapter(snapshot=snapshot)
    request = dispatch_request(command=action_command(capability=snapshot))
    first = await fake.dispatch(request)
    assert first.state == "succeeded"
    assert validate_im_dispatch_result_v1(request, first) is first
    assert fake.accepted_effect_count == 1

    second_request = dispatch_request(
        command=request.command,
        dispatch_attempt_id="test-dispatch-attempt-2",
        attempt_number=2,
        fence_id="test-fence-2",
        fence_revision="test-fence-revision-2",
    )
    second = await fake.dispatch(second_request)
    assert validate_im_dispatch_result_v1(second_request, second) is second
    assert second.provider_operation_id == first.provider_operation_id
    assert second.provider_message == first.provider_message
    assert second.receiver_evidence_digest == first.receiver_evidence_digest
    assert second.dispatch_request_digest != first.dispatch_request_digest
    assert fake.accepted_effect_count == 1


@pytest.mark.asyncio
async def test_fake_receiver_rejects_changed_intent_without_second_effect() -> None:
    snapshot = capability()
    fake = outbound_adapter(snapshot=snapshot)
    request = dispatch_request(command=action_command(capability=snapshot))
    await fake.dispatch(request)
    changed_intent = action_intent(
        content=content(segments=(text_segment(text="changed body"),), attachments=())
    )
    changed_command = action_command(intent=changed_intent, capability=snapshot)
    assert changed_command.idempotency_key == request.command.idempotency_key
    assert changed_command.intent_digest != request.command.intent_digest
    changed_request = dispatch_request(
        command=changed_command,
        dispatch_attempt_id="test-dispatch-attempt-2",
        attempt_number=2,
    )
    with pytest.raises(
        FakeIMReceiverCollisionError, match="^fake IM receiver idempotency collision$"
    ):
        await fake.dispatch(changed_request)
    assert fake.accepted_effect_count == 1


@pytest.mark.asyncio
async def test_fake_for_test_returns_capability_possible_acceptance_result() -> None:
    snapshot = capability()
    fake = outbound_adapter(snapshot=snapshot)
    request = dispatch_request(command=action_command(capability=snapshot))
    query = acceptance_query(request=request)
    result = await fake.query_acceptance(query)
    assert result.state == "reconciled_rejected"
    assert validate_im_acceptance_result_v1(query, request, snapshot, result) is result


@pytest.mark.asyncio
async def test_fake_query_reconciles_the_exact_accepted_effect() -> None:
    snapshot = capability()
    fake = outbound_adapter(snapshot=snapshot)
    request = dispatch_request(command=action_command(capability=snapshot))
    dispatched = await fake.dispatch(request)
    unknown = action_receipt(
        "effect_unknown",
        request=request,
        provider_operation_id=dispatched.provider_operation_id,
    )
    query = acceptance_query("provider_operation_id", request=request, source=unknown)
    result = await fake.query_acceptance(query)
    assert result.state == "reconciled_succeeded"
    assert result.provider_operation_id == dispatched.provider_operation_id
    assert result.provider_message == dispatched.provider_message
    assert result.receiver_evidence_digest == dispatched.receiver_evidence_digest
    assert validate_im_acceptance_result_v1(query, request, snapshot, result) is result


@pytest.mark.asyncio
async def test_fake_ack_loss_stays_unknown_then_reconciles_without_redispatch() -> None:
    snapshot = capability()
    fake = outbound_adapter(
        snapshot=snapshot,
        fault_script=FakeIMFaultScript(
            dispatch_steps=("ack_loss_after_accept",),
            query_steps=("not_final", "ledger"),
        ),
    )
    request = dispatch_request(command=action_command(capability=snapshot))
    unknown = await fake.dispatch(request)
    assert unknown.state == "effect_unknown"
    assert unknown.provider_operation_id is not None
    assert validate_im_dispatch_result_v1(request, unknown) is unknown
    assert fake.accepted_effect_count == 1

    query = acceptance_query("provider_operation_id", request=request, source=unknown)
    not_final = await fake.query_acceptance(query)
    assert not_final.state == "effect_unknown"
    assert not_final.error_code == "acceptance_not_final"
    assert validate_im_acceptance_result_v1(query, request, snapshot, not_final) is not_final

    reconciled = await fake.query_acceptance(query)
    assert reconciled.state == "reconciled_succeeded"
    assert reconciled.provider_operation_id == unknown.provider_operation_id
    assert validate_im_acceptance_result_v1(query, request, snapshot, reconciled) is reconciled
    assert fake.accepted_effect_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fault_step", "error_code"),
    [
        ("temporary_nack", "temporarily_unavailable_not_accepted"),
        ("rate_limited_nack", "rate_limited_not_accepted"),
    ],
)
async def test_fake_retryable_nack_proves_no_effect_before_retry(
    fault_step: str, error_code: str
) -> None:
    snapshot = capability()
    fake = outbound_adapter(
        snapshot=snapshot,
        fault_script=FakeIMFaultScript(dispatch_steps=(fault_step,)),
    )
    request = dispatch_request(command=action_command(capability=snapshot))
    nack = await fake.dispatch(request)
    assert nack.state == "retryable_not_accepted"
    assert nack.error_code == error_code
    assert nack.receiver_evidence_digest is not None
    assert validate_im_dispatch_result_v1(request, nack) is nack
    assert fake.accepted_effect_count == 0

    retry = dispatch_request(
        command=request.command,
        dispatch_attempt_id="test-dispatch-attempt-2",
        attempt_number=2,
    )
    succeeded = await fake.dispatch(retry)
    assert succeeded.state == "succeeded"
    assert validate_im_dispatch_result_v1(retry, succeeded) is succeeded
    assert fake.accepted_effect_count == 1


@pytest.mark.asyncio
async def test_fake_retention_expiry_never_becomes_false_negative() -> None:
    snapshot = capability()
    fake = outbound_adapter(
        snapshot=snapshot,
        fault_script=FakeIMFaultScript(query_steps=("retention_expired",)),
    )
    request = dispatch_request(command=action_command(capability=snapshot))
    query = acceptance_query(request=request)
    result = await fake.query_acceptance(query)
    assert result.state == "effect_unknown"
    assert result.error_code == "acceptance_retention_expired"
    assert validate_im_acceptance_result_v1(query, request, snapshot, result) is result


@pytest.mark.asyncio
async def test_fake_import_and_runtime_open_no_network(monkeypatch) -> None:
    parsed = ast.parse(SOURCE_PATH.read_text(encoding="utf-8"))
    imported_roots = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(parsed)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_from = {
        (node.module or "").split(".", 1)[0]
        for node in ast.walk(parsed)
        if isinstance(node, ast.ImportFrom) and node.level == 0
    }
    assert imported_roots == {"hashlib", "os"}
    assert imported_from == {"__future__", "dataclasses", "typing"}

    def deny_network(*args, **kwargs):
        raise AssertionError("network capability was opened")

    monkeypatch.setattr(socket, "socket", deny_network)
    monkeypatch.setattr(socket, "create_connection", deny_network)
    monkeypatch.setattr(socket, "getaddrinfo", deny_network)
    fake = adapter()
    await fake.capability_snapshot(capability_request())
    await fake.read_inbound(read_request())


def test_fake_repr_and_errors_do_not_leak_payload_or_immutable_refs() -> None:
    body_canary = "test-body-credential-canary"
    ref_canary = "test-attachment-secret-canary"
    event = inbound_event(
        content=content(
            segments=(text_segment(text=body_canary),),
            attachments=(attachment(immutable_ref=ref_canary),),
        )
    )
    fake = adapter(event_values=(verified_envelope(event=event),))
    rendered = repr(fake)
    assert body_canary not in rendered
    assert ref_canary not in rendered
    assert rendered == "FakeIMAdapter(provider='qe.fake-im.v1', envelopes=1, outbound='disabled')"


def test_fake_has_no_outbound_configuration_surface() -> None:
    source = SOURCE_PATH.read_text(encoding="utf-8").lower()
    for forbidden in ("endpoint", "authorization", "webhook", "websocket", "http", "callback"):
        assert forbidden not in source
    assert "os.environ" not in source
    assert "os.getenv" not in source
    constructor_parameters = inspect.signature(FakeIMAdapter).parameters
    for forbidden in ("url", "token", "secret", "credential", "client", "transport"):
        assert forbidden not in constructor_parameters
