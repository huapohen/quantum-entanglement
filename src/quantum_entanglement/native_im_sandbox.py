"""Default-off native-IM sandbox adapter and transport contracts.

This module deliberately contains no HTTP, WebSocket, DNS, socket, environment, or
credential-provider composition.  The default composition returns a disabled adapter.  A
later inbound-only adapter may receive an explicit transport object, but no concrete network
transport is registered here.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import NoReturn, Protocol, runtime_checkable

from ._native_im_codec import _digest, _id, _schema_version, _timestamp
from .native_im import (
    IMAcceptanceQueryV1,
    IMActionReceiptV1,
    IMCapabilityRequestV1,
    IMCapabilitySnapshotV1,
    IMDispatchRequestV1,
    IMInboundPageV1,
    IMInboundReadRequestV1,
)
from .native_im_auth import (
    NativeIMDetachedSignatureV1,
    NativeIMRawVerificationResultV1,
)
from .native_im_gateway import validate_im_inbound_result_v1
from .native_im_provider_profile import IMProviderProfileV1
from .service.native_im_config import (
    NativeIMConfigV1,
    NativeIMDisabledConfigV1,
    NativeIMInboundOnlyConfigV1,
)
from .service.secrets import SecretMaterial

_MAX_RAW_RESPONSE_BYTES = 16 * 1_024 * 1_024


class NativeIMSandboxDisabledError(RuntimeError):
    """The default composition has no enabled native-IM transport."""

    code = "native_im_sandbox_disabled"

    def __init__(self) -> None:
        super().__init__(self.code)


class NativeIMOutboundForbiddenError(RuntimeError):
    """Outbound is absent from the E2 inbound-only boundary."""

    code = "native_im_outbound_forbidden"

    def __init__(self) -> None:
        super().__init__(self.code)


class NativeIMTransportContractError(RuntimeError):
    """A transport crossed the bounded, typed response contract."""

    code = "native_im_transport_contract_failed"

    def __init__(self) -> None:
        super().__init__(self.code)


class NativeIMInboundParseError(ValueError):
    """A stable content-free rejection from the bounded canonical page parser."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, repr=False)
class NativeIMHealthEvidenceV1:
    """Content-free health evidence returned by an explicit inbound transport."""

    schema_version: int
    healthy: bool
    observed_at: str
    evidence_digest: str

    def __post_init__(self) -> None:
        if type(self) is not NativeIMHealthEvidenceV1:
            raise TypeError("health evidence requires the exact V1 class")
        _schema_version(self.schema_version)
        if type(self.healthy) is not bool:
            raise TypeError("health evidence healthy must be an exact boolean")
        _timestamp(self.observed_at, "observedAt")
        _digest(self.evidence_digest, "evidenceDigest")

    def __repr__(self) -> str:
        return (
            "NativeIMHealthEvidenceV1("
            f"healthy={self.healthy!r}, evidence={self.evidence_digest[:12]!r})"
        )


@dataclass(frozen=True, repr=False)
class NativeIMInboundRawResponseV1:
    """One bounded immutable response body plus detached authentication metadata."""

    schema_version: int
    read_request_id: str
    status_code: int
    metadata: NativeIMDetachedSignatureV1 = field(repr=False)
    raw_body: bytes = field(repr=False)
    received_at: str
    transport_evidence_digest: str

    def __post_init__(self) -> None:
        if type(self) is not NativeIMInboundRawResponseV1:
            raise TypeError("raw inbound response requires the exact V1 class")
        _schema_version(self.schema_version)
        _id(self.read_request_id, "readRequestId")
        if type(self.status_code) is not int or self.status_code != 200:
            raise ValueError("raw inbound response status must be exact 200")
        if type(self.metadata) is not NativeIMDetachedSignatureV1:
            raise TypeError("raw inbound response metadata must be the exact V1 class")
        if type(self.raw_body) is not bytes:
            raise TypeError("raw inbound response body must be immutable bytes")
        if not self.raw_body or len(self.raw_body) > _MAX_RAW_RESPONSE_BYTES:
            raise ValueError("raw inbound response body is outside its hard byte bound")
        _timestamp(self.received_at, "receivedAt")
        _digest(self.transport_evidence_digest, "transportEvidenceDigest")

    def __repr__(self) -> str:
        return (
            "NativeIMInboundRawResponseV1("
            f"body_bytes={len(self.raw_body)}, "
            f"evidence={self.transport_evidence_digest[:12]!r})"
        )


@runtime_checkable
class NativeIMInboundTransportPort(Protocol):
    """The only transport shape an E2 inbound adapter may receive explicitly."""

    async def probe_health(self, credential: SecretMaterial) -> NativeIMHealthEvidenceV1:
        """Probe the approved health path without returning provider content."""

    async def read_inbound(
        self,
        request: IMInboundReadRequestV1,
        credential: SecretMaterial,
    ) -> NativeIMInboundRawResponseV1:
        """Read one bounded response for an exact provider-neutral request."""

    async def aclose(self) -> None:
        """Close transport-owned resources without performing an external action."""


def _scope(value: object) -> tuple[object, object, object, object]:
    return (
        getattr(value, "tenant_id", None),
        getattr(value, "workspace_id", None),
        getattr(value, "provider", None),
        getattr(value, "channel_id", None),
    )


def _raise_clean_parse_error(code: str) -> NoReturn:
    raise NativeIMInboundParseError(code) from None


def _detach_exception(error: BaseException) -> None:
    error.__traceback__ = None
    error.__cause__ = None
    error.__context__ = None


def parse_native_im_inbound_page_v1(
    response: NativeIMInboundRawResponseV1,
    request: IMInboundReadRequestV1,
    capability: IMCapabilitySnapshotV1,
    raw_verification: NativeIMRawVerificationResultV1,
    configuration: NativeIMInboundOnlyConfigV1,
    profile: IMProviderProfileV1,
) -> IMInboundPageV1:
    """Decode one canonical page under config, profile, request, and auth bounds."""

    for value, expected, label in (
        (response, NativeIMInboundRawResponseV1, "response"),
        (request, IMInboundReadRequestV1, "request"),
        (capability, IMCapabilitySnapshotV1, "capability"),
        (raw_verification, NativeIMRawVerificationResultV1, "raw verification"),
        (configuration, NativeIMInboundOnlyConfigV1, "configuration"),
        (profile, IMProviderProfileV1, "profile"),
    ):
        if type(value) is not expected:
            raise TypeError(f"native IM parser requires the exact {label} V1 value")

    expected_scope = (
        configuration.tenant_id,
        configuration.workspace_id,
        configuration.provider,
        configuration.channel_id,
    )
    if _scope(request) != expected_scope or _scope(capability) != expected_scope:
        _raise_clean_parse_error("native_im_parse_scope_mismatch")
    if _scope(profile) != expected_scope:
        _raise_clean_parse_error("native_im_parse_profile_scope_mismatch")
    if response.read_request_id != request.read_request_id:
        _raise_clean_parse_error("native_im_parse_request_mismatch")
    if request.limit > configuration.page_limit or request.limit > profile.limits.max_page_events:
        _raise_clean_parse_error("native_im_parse_request_limit_exceeded")
    raw_body = response.raw_body
    maximum_body_bytes = min(
        configuration.max_response_bytes,
        profile.limits.max_raw_page_bytes,
        _MAX_RAW_RESPONSE_BYTES,
    )
    if len(raw_body) > maximum_body_bytes:
        _raise_clean_parse_error("native_im_parse_body_too_large")
    if hashlib.sha256(raw_body).hexdigest() != raw_verification.body_digest:
        _raise_clean_parse_error("native_im_parse_body_digest_mismatch")

    page: IMInboundPageV1 | None = None
    decode_failed = False
    try:
        page = IMInboundPageV1.from_json_bytes(raw_body)
    except Exception as error:
        decode_failed = True
        _detach_exception(error)
    if decode_failed or type(page) is not IMInboundPageV1:
        _raise_clean_parse_error("native_im_parse_body_invalid")
    if raw_body != page.canonical_bytes():
        _raise_clean_parse_error("native_im_parse_body_not_canonical")

    binding_failed = False
    try:
        validate_im_inbound_result_v1(request, capability, page)
    except (TypeError, ValueError) as error:
        binding_failed = True
        _detach_exception(error)
    if binding_failed:
        _raise_clean_parse_error("native_im_parse_binding_failed")
    if len(page.envelopes) > configuration.page_limit:
        _raise_clean_parse_error("native_im_parse_page_limit_exceeded")

    supported_events = {
        mapping.event_type for mapping in profile.event_mappings if mapping.status == "supported"
    }
    allowed_conversations = set(profile.allowed_conversation_ids)
    verification_ids: set[str] = set()
    for envelope in page.envelopes:
        event = envelope.event
        if event.event_type not in supported_events:
            _raise_clean_parse_error("native_im_parse_event_mapping_unsupported")
        if event.conversation.conversation_id not in allowed_conversations:
            _raise_clean_parse_error("native_im_parse_conversation_forbidden")
        if envelope.tenant_mapping_revision != profile.tenant_mapping_revision:
            _raise_clean_parse_error("native_im_parse_tenant_mapping_mismatch")
        if (
            envelope.verifier_id != raw_verification.verifier_id
            or envelope.authentication_evidence_digest
            != raw_verification.authentication_evidence_digest
            or envelope.verified_at != raw_verification.verified_at
        ):
            _raise_clean_parse_error("native_im_parse_authentication_binding_failed")
        if event.transport_evidence_digest != response.transport_evidence_digest:
            _raise_clean_parse_error("native_im_parse_transport_binding_failed")
        if envelope.verification_id in verification_ids:
            _raise_clean_parse_error("native_im_parse_verification_id_duplicate")
        verification_ids.add(envelope.verification_id)
        if len(envelope.canonical_bytes()) > profile.limits.max_raw_event_bytes:
            _raise_clean_parse_error("native_im_parse_event_too_large")
    return page


class NativeIMDisabledSandboxAdapter:
    """The only adapter produced by the default service composition."""

    __slots__ = ("__configuration_fingerprint", "__closed")

    def __init__(self, configuration: NativeIMDisabledConfigV1) -> None:
        if type(configuration) is not NativeIMDisabledConfigV1:
            raise TypeError("disabled adapter requires the exact disabled configuration")
        self.__configuration_fingerprint = configuration.fingerprint
        self.__closed = False

    @staticmethod
    def _reject() -> NoReturn:
        raise NativeIMSandboxDisabledError() from None

    async def capability_snapshot(
        self,
        request: IMCapabilityRequestV1,
    ) -> IMCapabilitySnapshotV1:
        self._reject()

    async def read_inbound(self, request: IMInboundReadRequestV1) -> IMInboundPageV1:
        self._reject()

    async def dispatch(self, request: IMDispatchRequestV1) -> IMActionReceiptV1:
        self._reject()

    async def query_acceptance(self, query: IMAcceptanceQueryV1) -> IMActionReceiptV1:
        self._reject()

    async def aclose(self) -> None:
        self.__closed = True

    @property
    def closed(self) -> bool:
        return self.__closed

    def __repr__(self) -> str:
        return (
            "NativeIMDisabledSandboxAdapter("
            f"config={self.__configuration_fingerprint!r}, closed={self.__closed!r})"
        )


def compose_default_native_im_sandbox_v1(
    configuration: NativeIMConfigV1,
) -> NativeIMDisabledSandboxAdapter:
    """Compose no transport: enabled config remains mechanically unregistered."""

    if type(configuration) is NativeIMDisabledConfigV1:
        return NativeIMDisabledSandboxAdapter(configuration)
    if type(configuration) is NativeIMInboundOnlyConfigV1:
        raise NativeIMSandboxDisabledError() from None
    raise TypeError("native IM composition requires an exact V1 configuration")


__all__ = [
    "NativeIMDisabledSandboxAdapter",
    "NativeIMHealthEvidenceV1",
    "NativeIMInboundParseError",
    "NativeIMInboundRawResponseV1",
    "NativeIMInboundTransportPort",
    "NativeIMOutboundForbiddenError",
    "NativeIMSandboxDisabledError",
    "NativeIMTransportContractError",
    "compose_default_native_im_sandbox_v1",
    "parse_native_im_inbound_page_v1",
]
