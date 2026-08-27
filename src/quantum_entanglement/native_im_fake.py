"""Deterministic zero-network fake for the provider-neutral native-IM V1 port."""

from __future__ import annotations

import hashlib
import os
from typing import NoReturn

from .native_im import (
    IMAcceptanceQueryV1,
    IMActionReceiptV1,
    IMCapabilityRequestV1,
    IMCapabilitySnapshotV1,
    IMDispatchRequestV1,
    IMInboundPageV1,
    IMInboundReadRequestV1,
    IMVerifiedInboundEnvelopeV1,
)

FAKE_IM_PROVIDER = "qe.fake-im.v1"
_FAKE_SNAPSHOT_TOKEN = "test-fake-im-snapshot-v1"
_FAKE_OBSERVED_AT = "2026-08-28T00:00:02.000001Z"
_OUTBOUND_PERMIT_SENTINEL = object()


class FakeIMOutboundDisabledError(RuntimeError):
    """Raised before the default fake observes any outbound request content."""


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
    ) -> FakeIMAdapter:
        _require_exact(outbound_permit, FakeIMTestOutboundPermit, "outbound permit")
        if not outbound_permit._is_current():
            raise FakeIMOutboundDisabledError("fake IM outbound permit is not process-current")
        adapter = cls(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            channel_id=channel_id,
            capability=capability,
            envelopes=envelopes,
        )
        object.__setattr__(adapter, "_outbound_permit", outbound_permit)
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
        digest = self._outbound_digest("terminal-rejection", request.canonical_bytes())
        return IMActionReceiptV1(
            schema_version=1,
            receipt_id=f"test-fake-receipt-{digest}",
            tenant_id=request.command.intent.tenant_id,
            workspace_id=request.command.intent.workspace_id,
            provider=FAKE_IM_PROVIDER,
            channel_id=request.command.intent.conversation.channel_id,
            action_id=request.command.intent.action_id,
            command_id=request.command.command_id,
            dispatch_attempt_id=request.dispatch_attempt_id,
            dispatch_request_digest=request.canonical_digest(),
            intent_digest=request.command.intent_digest,
            command_digest=request.command_digest,
            idempotency_key=request.command.idempotency_key,
            attempt_number=request.attempt_number,
            state="rejected",
            provider_operation_id=None,
            provider_message=None,
            receiver_evidence_digest=digest,
            error_code="terminal_not_accepted",
            retry_after_seconds=None,
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
        authoritative = any(
            lookup.lookup_mode == query.lookup_mode
            and lookup.negative_acceptance_mode == "authoritative_terminal"
            for operation in self._capability.operations
            for lookup in operation.acceptance_lookups
        )
        state = "reconciled_rejected" if authoritative else "effect_unknown"
        digest = self._outbound_digest("acceptance-query", query.canonical_bytes())
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
            provider_operation_id=query.provider_operation_id,
            provider_message=None,
            receiver_evidence_digest=digest if authoritative else None,
            error_code=("terminal_not_accepted" if authoritative else "query_not_final"),
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


__all__ = [
    "FAKE_IM_PROVIDER",
    "FakeIMAdapter",
    "FakeIMOutboundDisabledError",
    "FakeIMTestOutboundPermit",
]
