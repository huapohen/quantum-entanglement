"""Deterministic zero-network fake for the provider-neutral native-IM V1 port."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import NoReturn

from .native_im import (
    IMAcceptanceQueryV1,
    IMActionReceiptV1,
    IMCapabilityRequestV1,
    IMCapabilitySnapshotV1,
    IMDispatchRequestV1,
    IMInboundPageV1,
    IMInboundReadRequestV1,
    IMMessageRefV1,
    IMVerifiedInboundEnvelopeV1,
)

FAKE_IM_PROVIDER = "qe.fake-im.v1"
_FAKE_SNAPSHOT_TOKEN = "test-fake-im-snapshot-v1"
_FAKE_OBSERVED_AT = "2026-08-28T00:00:02.000001Z"
_OUTBOUND_PERMIT_SENTINEL = object()
_DISPATCH_FAULT_STEPS = {
    "accept",
    "ack_loss_after_accept",
    "effect_unknown",
    "terminal_reject",
    "temporary_nack",
    "rate_limited_nack",
}
_QUERY_FAULT_STEPS = {
    "ledger",
    "not_final",
    "retention_expired",
    "authoritative_negative",
}


class FakeIMOutboundDisabledError(RuntimeError):
    """Raised before the default fake observes any outbound request content."""


class FakeIMReceiverCollisionError(RuntimeError):
    """Raised when a fake receiver identity is reused for a different effect."""


@dataclass(frozen=True)
class _FakeAcceptedEffect:
    action_id: str
    idempotency_key: str
    intent_digest: str
    provider_operation_id: str
    provider_message: IMMessageRefV1 | None
    receiver_evidence_digest: str


@dataclass(frozen=True)
class FakeIMFaultScript:
    """Finite deterministic fault steps; exhausted sequences return to ledger truth."""

    dispatch_steps: tuple[str, ...] = ()
    query_steps: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.dispatch_steps) is not tuple or type(self.query_steps) is not tuple:
            raise TypeError("fake IM fault steps must be exact immutable tuples")
        if len(self.dispatch_steps) > 1_000 or len(self.query_steps) > 1_000:
            raise ValueError("fake IM fault script exceeds its step limit")
        if any(
            type(step) is not str or step not in _DISPATCH_FAULT_STEPS
            for step in self.dispatch_steps
        ):
            raise ValueError("fake IM dispatch fault step is unsupported")
        if any(
            type(step) is not str or step not in _QUERY_FAULT_STEPS for step in self.query_steps
        ):
            raise ValueError("fake IM query fault step is unsupported")

    def __repr__(self) -> str:
        return (
            f"FakeIMFaultScript(dispatch_steps={len(self.dispatch_steps)}, "
            f"query_steps={len(self.query_steps)})"
        )


class FakeIMTestOutboundPermit:
    """Explicit process-local authority for fake-only outbound contract tests."""

    __slots__ = ("_process_id", "_sentinel")
    _process_id: int
    _sentinel: object

    def __init__(self) -> None:
        object.__setattr__(self, "_process_id", os.getpid())
        object.__setattr__(self, "_sentinel", _OUTBOUND_PERMIT_SENTINEL)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("fake IM test outbound permit is immutable")

    def __repr__(self) -> str:
        return "FakeIMTestOutboundPermit(process_local=True)"

    def __reduce__(self) -> NoReturn:
        raise TypeError("fake IM test outbound permit cannot be serialized")

    def _is_current(self) -> bool:
        return self._sentinel is _OUTBOUND_PERMIT_SENTINEL and self._process_id == os.getpid()


def _require_exact(value: object, expected: type[object], label: str) -> None:
    if type(value) is not expected:
        raise TypeError(f"{label} must be the exact {expected.__name__} model")


def _require_test_scope_id(value: object, label: str) -> str:
    if type(value) is not str or not value.startswith("test-"):
        raise ValueError(f"{label} must use the reserved test- prefix")
    return value


class FakeIMAdapter:
    """Read-only-by-default fake with immutable events and deterministic cursor paging."""

    __slots__ = (
        "_tenant_id",
        "_workspace_id",
        "_channel_id",
        "_capability",
        "_envelopes",
        "_outbound_permit",
        "_receiver_by_action",
        "_receiver_by_key",
        "_fault_script",
        "_dispatch_fault_index",
        "_query_fault_index",
    )

    def __setattr__(self, name: str, value: object) -> None:
        if hasattr(self, name):
            raise AttributeError("fake IM adapter state is immutable")
        object.__setattr__(self, name, value)

    def __init__(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        channel_id: str,
        capability: IMCapabilitySnapshotV1,
        envelopes: tuple[IMVerifiedInboundEnvelopeV1, ...],
    ) -> None:
        self._tenant_id = _require_test_scope_id(tenant_id, "tenant_id")
        self._workspace_id = _require_test_scope_id(workspace_id, "workspace_id")
        self._channel_id = _require_test_scope_id(channel_id, "channel_id")
        _require_exact(capability, IMCapabilitySnapshotV1, "capability")
        if type(envelopes) is not tuple:
            raise TypeError("envelopes must be an exact immutable tuple")
        expected_scope = self._scope
        if self._model_scope(capability) != expected_scope:
            raise ValueError("capability scope does not match the fake scope")

        previous_sequence: int | None = None
        event_ids: set[str] = set()
        for envelope in envelopes:
            _require_exact(envelope, IMVerifiedInboundEnvelopeV1, "envelope")
            event = envelope.event
            if self._model_scope(event.conversation) != expected_scope:
                raise ValueError("envelope scope does not match the fake scope")
            if previous_sequence is not None and event.sequence_number <= previous_sequence:
                raise ValueError("fake envelope sequence numbers must strictly increase")
            if event.event_id in event_ids:
                raise ValueError("fake envelope event IDs must be unique")
            previous_sequence = event.sequence_number
            event_ids.add(event.event_id)

        self._capability = capability
        self._envelopes = envelopes
        self._outbound_permit: FakeIMTestOutboundPermit | None = None
        self._receiver_by_action: dict[tuple[str, ...], _FakeAcceptedEffect] = {}
        self._receiver_by_key: dict[tuple[str, ...], _FakeAcceptedEffect] = {}
        self._fault_script = FakeIMFaultScript()
        self._dispatch_fault_index = 0
        self._query_fault_index = 0

    @classmethod
    def for_test(
        cls,
        *,
        tenant_id: str,
        workspace_id: str,
        channel_id: str,
        capability: IMCapabilitySnapshotV1,
        envelopes: tuple[IMVerifiedInboundEnvelopeV1, ...],
        outbound_permit: FakeIMTestOutboundPermit,
        fault_script: FakeIMFaultScript | None = None,
    ) -> FakeIMAdapter:
        _require_exact(outbound_permit, FakeIMTestOutboundPermit, "outbound permit")
        if not outbound_permit._is_current():
            raise FakeIMOutboundDisabledError("fake IM outbound permit is not process-current")
        selected_faults = FakeIMFaultScript() if fault_script is None else fault_script
        _require_exact(selected_faults, FakeIMFaultScript, "fault script")
        adapter = cls(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            channel_id=channel_id,
            capability=capability,
            envelopes=envelopes,
        )
        object.__setattr__(adapter, "_outbound_permit", outbound_permit)
        object.__setattr__(adapter, "_fault_script", selected_faults)
        return adapter

    @property
    def _scope(self) -> tuple[str, str, str, str]:
        return (
            self._tenant_id,
            self._workspace_id,
            FAKE_IM_PROVIDER,
            self._channel_id,
        )

    @staticmethod
    def _model_scope(value: object) -> tuple[object, object, object, object]:
        return (
            getattr(value, "tenant_id", None),
            getattr(value, "workspace_id", None),
            getattr(value, "provider", None),
            getattr(value, "channel_id", None),
        )

    def __repr__(self) -> str:
        return (
            "FakeIMAdapter(provider='qe.fake-im.v1', "
            f"envelopes={len(self._envelopes)}, outbound='disabled')"
        )

    @property
    def accepted_effect_count(self) -> int:
        return len(self._receiver_by_action)

    async def capability_snapshot(self, request: IMCapabilityRequestV1) -> IMCapabilitySnapshotV1:
        _require_exact(request, IMCapabilityRequestV1, "capability request")
        if self._model_scope(request) != self._scope:
            raise ValueError("capability request scope does not match the fake scope")
        return self._capability

    async def read_inbound(self, request: IMInboundReadRequestV1) -> IMInboundPageV1:
        _require_exact(request, IMInboundReadRequestV1, "inbound read request")
        if self._model_scope(request) != self._scope:
            raise ValueError("inbound request scope does not match the fake scope")
        if request.snapshot_token not in {None, _FAKE_SNAPSHOT_TOKEN}:
            raise ValueError("inbound request snapshot token does not match the fake snapshot")

        start = 0
        if request.after_cursor is not None:
            for index, envelope in enumerate(self._envelopes):
                event = envelope.event
                if (event.cursor, event.sequence_number) == (
                    request.after_cursor,
                    request.after_sequence,
                ):
                    start = index + 1
                    break
            else:
                raise ValueError("inbound request resume pair is not present in the fake snapshot")

        selected = self._envelopes[start : start + request.limit]
        has_more = start + len(selected) < len(self._envelopes)
        next_cursor: str | None
        next_sequence: int | None
        if selected:
            final_event = selected[-1].event
            next_cursor = final_event.cursor
            next_sequence = final_event.sequence_number
        else:
            next_cursor = request.after_cursor
            next_sequence = request.after_sequence
        result = IMInboundPageV1(
            schema_version=1,
            tenant_id=self._tenant_id,
            workspace_id=self._workspace_id,
            provider=FAKE_IM_PROVIDER,
            channel_id=self._channel_id,
            read_request_id=request.read_request_id,
            read_request_digest=request.canonical_digest(),
            snapshot_token=_FAKE_SNAPSHOT_TOKEN,
            envelopes=selected,
            next_cursor=next_cursor,
            next_sequence=next_sequence,
            has_more=has_more,
            capability_revision=self._capability.revision,
            capability_digest=self._capability.canonical_digest(),
        )
        result.validate_request_binding(request)
        result.validate_capability_binding(self._capability)
        return result

    async def dispatch(self, request: IMDispatchRequestV1) -> IMActionReceiptV1:
        if self._outbound_permit is None or not self._outbound_permit._is_current():
            raise FakeIMOutboundDisabledError("fake IM outbound is disabled")
        _require_exact(request, IMDispatchRequestV1, "dispatch request")
        request.command.validate_capability_binding(self._capability)
        fault_step = self._next_dispatch_fault()
        intent = request.command.intent
        scope = self._scope
        action_key = scope + (intent.action_id,)
        idempotency_key = scope + (request.command.idempotency_key,)
        by_action = self._receiver_by_action.get(action_key)
        by_key = self._receiver_by_key.get(idempotency_key)
        if by_action is not None and (
            by_action.idempotency_key != request.command.idempotency_key
            or by_action.intent_digest != request.command.intent_digest
        ):
            raise FakeIMReceiverCollisionError("fake IM receiver idempotency collision")
        if by_key is not None and (
            by_key.action_id != intent.action_id
            or by_key.intent_digest != request.command.intent_digest
        ):
            raise FakeIMReceiverCollisionError("fake IM receiver idempotency collision")
        if by_action is not None and by_key is not None and by_action is not by_key:
            raise FakeIMReceiverCollisionError("fake IM receiver ledger conflict")

        effect = by_action or by_key
        if effect is None and fault_step in {"accept", "ack_loss_after_accept"}:
            effect = self._accept_effect(request)
            self._receiver_by_action[action_key] = effect
            self._receiver_by_key[idempotency_key] = effect

        if effect is not None and fault_step not in {
            "ack_loss_after_accept",
            "effect_unknown",
        }:
            state = "succeeded"
        elif fault_step == "terminal_reject":
            state = "rejected"
        elif fault_step in {"temporary_nack", "rate_limited_nack"}:
            state = "retryable_not_accepted"
        else:
            state = "effect_unknown"

        evidence = self._outbound_digest(f"dispatch-{fault_step}", request.canonical_bytes())
        if state == "succeeded":
            assert effect is not None
            provider_operation_id = effect.provider_operation_id
            provider_message = effect.provider_message
            receiver_evidence_digest = effect.receiver_evidence_digest
            error_code = None
            retry_after_seconds = None
        elif state == "rejected":
            provider_operation_id = None
            provider_message = None
            receiver_evidence_digest = evidence
            error_code = "terminal_not_accepted"
            retry_after_seconds = None
        elif state == "retryable_not_accepted":
            provider_operation_id = None
            provider_message = None
            receiver_evidence_digest = evidence
            error_code = (
                "rate_limited_not_accepted"
                if fault_step == "rate_limited_nack"
                else "temporarily_unavailable_not_accepted"
            )
            retry_after_seconds = 1
        else:
            provider_operation_id = None if effect is None else effect.provider_operation_id
            provider_message = None
            receiver_evidence_digest = None
            error_code = "delivery_outcome_unknown"
            retry_after_seconds = None

        receipt_digest = self._outbound_digest(
            f"dispatch-receipt-{state}", request.canonical_bytes()
        )
        return IMActionReceiptV1(
            schema_version=1,
            receipt_id=f"test-fake-receipt-{receipt_digest}",
            tenant_id=intent.tenant_id,
            workspace_id=intent.workspace_id,
            provider=FAKE_IM_PROVIDER,
            channel_id=intent.conversation.channel_id,
            action_id=intent.action_id,
            command_id=request.command.command_id,
            dispatch_attempt_id=request.dispatch_attempt_id,
            dispatch_request_digest=request.canonical_digest(),
            intent_digest=request.command.intent_digest,
            command_digest=request.command_digest,
            idempotency_key=request.command.idempotency_key,
            attempt_number=request.attempt_number,
            state=state,
            provider_operation_id=provider_operation_id,
            provider_message=provider_message,
            receiver_evidence_digest=receiver_evidence_digest,
            error_code=error_code,
            retry_after_seconds=retry_after_seconds,
            observed_at=_FAKE_OBSERVED_AT,
            correlation_id=request.correlation_id,
            causation_id=request.dispatch_attempt_id,
            traceparent=request.traceparent,
        )

    async def query_acceptance(self, query: IMAcceptanceQueryV1) -> IMActionReceiptV1:
        if self._outbound_permit is None or not self._outbound_permit._is_current():
            raise FakeIMOutboundDisabledError("fake IM outbound is disabled")
        _require_exact(query, IMAcceptanceQueryV1, "acceptance query")
        if self._model_scope(query) != self._scope:
            raise ValueError("acceptance query scope does not match the fake scope")
        fault_step = self._next_query_fault()
        effect = self._receiver_by_action.get(self._scope + (query.action_id,))
        by_key = self._receiver_by_key.get(self._scope + (query.idempotency_key,))
        if effect is not None and (
            effect.idempotency_key != query.idempotency_key
            or effect.intent_digest != query.intent_digest
        ):
            raise FakeIMReceiverCollisionError("fake IM receiver query collision")
        if by_key is not None and (
            by_key.action_id != query.action_id or by_key.intent_digest != query.intent_digest
        ):
            raise FakeIMReceiverCollisionError("fake IM receiver query collision")
        if effect is not None and by_key is not None and effect is not by_key:
            raise FakeIMReceiverCollisionError("fake IM receiver ledger conflict")
        effect = effect or by_key
        if (
            effect is not None
            and query.lookup_mode == "provider_operation_id"
            and query.provider_operation_id != effect.provider_operation_id
        ):
            raise FakeIMReceiverCollisionError("fake IM receiver provider operation conflict")
        authoritative = any(
            lookup.lookup_mode == query.lookup_mode
            and lookup.negative_acceptance_mode == "authoritative_terminal"
            for operation in self._capability.operations
            for lookup in operation.acceptance_lookups
        )
        forced_unknown = fault_step in {"not_final", "retention_expired"}
        if effect is not None and not forced_unknown:
            state = "reconciled_succeeded"
        elif forced_unknown:
            state = "effect_unknown"
        else:
            state = "reconciled_rejected" if authoritative else "effect_unknown"
        digest = self._outbound_digest(f"acceptance-query-{fault_step}", query.canonical_bytes())
        provider_operation_id: str | None
        provider_message: IMMessageRefV1 | None
        receiver_evidence_digest: str | None
        error_code: str | None
        if state == "reconciled_succeeded":
            assert effect is not None
            provider_operation_id = effect.provider_operation_id
            provider_message = effect.provider_message
            receiver_evidence_digest = effect.receiver_evidence_digest
            error_code = None
        elif state == "reconciled_rejected":
            provider_operation_id = query.provider_operation_id
            provider_message = None
            receiver_evidence_digest = digest
            error_code = "terminal_not_accepted"
        else:
            provider_operation_id = query.provider_operation_id
            provider_message = None
            receiver_evidence_digest = None
            error_code = (
                "acceptance_retention_expired"
                if fault_step == "retention_expired"
                else "acceptance_not_final"
            )
        return IMActionReceiptV1(
            schema_version=1,
            receipt_id=f"test-fake-receipt-{digest}",
            tenant_id=query.tenant_id,
            workspace_id=query.workspace_id,
            provider=FAKE_IM_PROVIDER,
            channel_id=query.channel_id,
            action_id=query.action_id,
            command_id=query.command_id,
            dispatch_attempt_id=query.dispatch_attempt_id,
            dispatch_request_digest=query.dispatch_request_digest,
            intent_digest=query.intent_digest,
            command_digest=query.command_digest,
            idempotency_key=query.idempotency_key,
            attempt_number=query.attempt_number,
            state=state,
            provider_operation_id=provider_operation_id,
            provider_message=provider_message,
            receiver_evidence_digest=receiver_evidence_digest,
            error_code=error_code,
            retry_after_seconds=None,
            observed_at=_FAKE_OBSERVED_AT,
            correlation_id=query.correlation_id,
            causation_id=query.query_id,
            traceparent=query.traceparent,
        )

    @staticmethod
    def _outbound_digest(domain: str, body: bytes) -> str:
        return hashlib.sha256(
            f"quantum-entanglement.native-im.fake/{domain}/1\n".encode() + body
        ).hexdigest()

    def _next_dispatch_fault(self) -> str:
        index = self._dispatch_fault_index
        object.__setattr__(self, "_dispatch_fault_index", index + 1)
        if index < len(self._fault_script.dispatch_steps):
            return self._fault_script.dispatch_steps[index]
        return "accept"

    def _next_query_fault(self) -> str:
        index = self._query_fault_index
        object.__setattr__(self, "_query_fault_index", index + 1)
        if index < len(self._fault_script.query_steps):
            return self._fault_script.query_steps[index]
        return "ledger"

    def _accept_effect(self, request: IMDispatchRequestV1) -> _FakeAcceptedEffect:
        intent = request.command.intent
        evidence = self._outbound_digest(
            "accepted-effect", request.command.intent_digest.encode("ascii")
        )
        operation_id = f"test-fake-operation-{evidence}"
        provider_message = None
        if intent.operation == "send_message":
            provider_message = IMMessageRefV1(
                schema_version=1,
                conversation=intent.conversation,
                message_id=f"test-fake-message-{evidence}",
                revision="test-fake-message-revision-1",
                created_at=_FAKE_OBSERVED_AT,
            )
        return _FakeAcceptedEffect(
            action_id=intent.action_id,
            idempotency_key=request.command.idempotency_key,
            intent_digest=request.command.intent_digest,
            provider_operation_id=operation_id,
            provider_message=provider_message,
            receiver_evidence_digest=evidence,
        )


__all__ = [
    "FAKE_IM_PROVIDER",
    "FakeIMAdapter",
    "FakeIMFaultScript",
    "FakeIMOutboundDisabledError",
    "FakeIMReceiverCollisionError",
    "FakeIMTestOutboundPermit",
]
