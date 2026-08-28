"""Default-off native-IM sandbox adapter and transport contracts.

This module deliberately contains no HTTP, WebSocket, DNS, socket, environment, or
credential-provider composition.  The default composition returns a disabled adapter.  A
later inbound-only adapter may receive an explicit transport object, but no concrete network
transport is registered here.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, NoReturn, Protocol, SupportsIndex, runtime_checkable

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
    NativeIMHMACRawBodyVerifier,
    NativeIMNonceReplayGuardPort,
    NativeIMRawVerificationResultV1,
)
from .native_im_gateway import (
    validate_im_capability_result_v1,
    validate_im_inbound_result_v1,
)
from .native_im_provider_profile import (
    IMProviderProfileV1,
    derive_inbound_only_capability_snapshot_v1,
)
from .native_im_sandbox_approval import NativeIMSandboxApprovalV1
from .native_im_sandbox_authority import (
    InMemoryNativeIMSandboxApprovalAuthorityV1,
    NativeIMSandboxApprovalPermitV1,
)
from .native_im_sandbox_provenance import NativeIMSandboxAdmissionProvenanceV1
from .service.native_im_config import (
    CanonicalAbsolutePath,
    CanonicalHTTPSOrigin,
    NativeIMConfigV1,
    NativeIMDisabledConfigV1,
    NativeIMInboundOnlyConfigV1,
    validate_native_im_sandbox_preflight_v1,
)
from .service.native_im_secrets import NativeIMSecretLoader
from .service.secrets import SecretMaterial, SecretRef

_MAX_RAW_RESPONSE_BYTES = 16 * 1_024 * 1_024
_APPROVED_COMPOSITION_TOKEN = object()


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


class NativeIMSandboxAdapterClosedError(RuntimeError):
    """The inbound adapter has permanently released its transport."""

    code = "native_im_sandbox_adapter_closed"

    def __init__(self) -> None:
        super().__init__(self.code)


class NativeIMSandboxAdapterProcessMismatchError(RuntimeError):
    """A process-inherited adapter cannot retain transport or secret-loader authority."""

    code = "native_im_sandbox_adapter_process_mismatch"

    def __init__(self) -> None:
        super().__init__(self.code)


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


@dataclass(frozen=True, repr=False)
class NativeIMMappedPageV1:
    """Canonical mapper output bound to, but distinct from, the signed provider body."""

    schema_version: int
    source_body_digest: str
    canonical_page_body: bytes = field(repr=False)
    mapping_evidence_digest: str

    def __post_init__(self) -> None:
        if type(self) is not NativeIMMappedPageV1:
            raise TypeError("mapped page requires the exact V1 class")
        _schema_version(self.schema_version)
        _digest(self.source_body_digest, "sourceBodyDigest")
        if type(self.canonical_page_body) is not bytes:
            raise TypeError("mapped page body must be immutable bytes")
        if not self.canonical_page_body or len(self.canonical_page_body) > _MAX_RAW_RESPONSE_BYTES:
            raise ValueError("mapped page body is outside its hard byte bound")
        _digest(self.mapping_evidence_digest, "mappingEvidenceDigest")

    def __repr__(self) -> str:
        return (
            "NativeIMMappedPageV1("
            f"body_bytes={len(self.canonical_page_body)}, "
            f"evidence={self.mapping_evidence_digest[:12]!r})"
        )


@dataclass(frozen=True, repr=False)
class NativeIMVerifiedInboundReadV1:
    """Ephemeral page plus the raw evidence required for atomic durable admission."""

    request: IMInboundReadRequestV1 = field(repr=False)
    capability: IMCapabilitySnapshotV1 = field(repr=False)
    page: IMInboundPageV1 = field(repr=False)
    raw_verification: NativeIMRawVerificationResultV1 = field(repr=False)
    mapping_evidence_digest: str
    provenance: NativeIMSandboxAdmissionProvenanceV1 = field(repr=False)

    def __post_init__(self) -> None:
        if type(self) is not NativeIMVerifiedInboundReadV1:
            raise TypeError("verified inbound read requires the exact V1 class")
        if type(self.request) is not IMInboundReadRequestV1:
            raise TypeError("verified inbound read request must be the exact V1 class")
        if type(self.capability) is not IMCapabilitySnapshotV1:
            raise TypeError("verified inbound read capability must be the exact V1 class")
        if type(self.page) is not IMInboundPageV1:
            raise TypeError("verified inbound read page must be the exact V1 class")
        if type(self.raw_verification) is not NativeIMRawVerificationResultV1:
            raise TypeError("verified inbound read evidence must be the exact V1 class")
        if type(self.provenance) is not NativeIMSandboxAdmissionProvenanceV1:
            raise TypeError("verified inbound read provenance must be the exact V1 class")
        _digest(self.mapping_evidence_digest, "mappingEvidenceDigest")
        self.page.validate_request_binding(self.request)
        self.page.validate_capability_binding(self.capability)
        for envelope in self.page.envelopes:
            if (
                envelope.verifier_id != self.raw_verification.verifier_id
                or envelope.authentication_evidence_digest
                != self.raw_verification.authentication_evidence_digest
                or envelope.verified_at != self.raw_verification.verified_at
            ):
                raise ValueError("verified inbound read authentication binding mismatch")
        if (
            self.provenance.read_request_digest != self.request.canonical_digest()
            or self.provenance.page_digest != self.page.canonical_digest()
            or self.provenance.mapping_evidence_digest != self.mapping_evidence_digest
            or any(
                envelope.event.transport_evidence_digest
                != self.provenance.transport_evidence_digest
                for envelope in self.page.envelopes
            )
        ):
            raise ValueError("verified inbound read provenance binding mismatch")

    def __repr__(self) -> str:
        return (
            "NativeIMVerifiedInboundReadV1("
            f"events={len(self.page.envelopes)}, "
            f"page={self.page.canonical_digest()[:12]!r}, "
            f"mapping={self.mapping_evidence_digest[:12]!r})"
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


@runtime_checkable
class NativeIMInboundMapperPort(Protocol):
    """Provider-specific pure mapping seam; it receives no secret or transport authority."""

    def map_inbound(
        self,
        response: NativeIMInboundRawResponseV1,
        request: IMInboundReadRequestV1,
        capability: IMCapabilitySnapshotV1,
        raw_verification: NativeIMRawVerificationResultV1,
        profile: IMProviderProfileV1,
    ) -> NativeIMMappedPageV1:
        """Map one verified provider payload to canonical provider-neutral bytes."""


@runtime_checkable
class NativeIMSecretResolverPort(Protocol):
    """Resolve only opaque references; the adapter owns every returned lease."""

    def resolve(self, reference: SecretRef) -> SecretMaterial:
        """Return one bounded caller-closed material lease."""


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
    mapped: NativeIMMappedPageV1,
    request: IMInboundReadRequestV1,
    capability: IMCapabilitySnapshotV1,
    raw_verification: NativeIMRawVerificationResultV1,
    configuration: NativeIMInboundOnlyConfigV1,
    profile: IMProviderProfileV1,
) -> IMInboundPageV1:
    """Decode one canonical page under config, profile, request, and auth bounds."""

    for value, expected, label in (
        (response, NativeIMInboundRawResponseV1, "response"),
        (mapped, NativeIMMappedPageV1, "mapped page"),
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
    if mapped.source_body_digest != raw_verification.body_digest:
        _raise_clean_parse_error("native_im_parse_mapping_source_mismatch")
    mapped_body = mapped.canonical_page_body
    if len(mapped_body) > maximum_body_bytes:
        _raise_clean_parse_error("native_im_parse_mapped_body_too_large")

    page: IMInboundPageV1 | None = None
    decode_failed = False
    try:
        page = IMInboundPageV1.from_json_bytes(mapped_body)
    except Exception as error:
        decode_failed = True
        _detach_exception(error)
    if decode_failed or type(page) is not IMInboundPageV1:
        _raise_clean_parse_error("native_im_parse_body_invalid")
    if mapped_body != page.canonical_bytes():
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


def _snapshot_configuration(
    value: NativeIMInboundOnlyConfigV1,
) -> NativeIMInboundOnlyConfigV1:
    if type(value) is not NativeIMInboundOnlyConfigV1:
        raise TypeError("inbound adapter requires the exact inbound-only configuration")
    return NativeIMInboundOnlyConfigV1(
        schema_version=value.schema_version,
        enabled=value.enabled,
        mode=value.mode,
        profile_id=value.profile_id,
        profile_revision=value.profile_revision,
        profile_digest=value.profile_digest,
        approval_id=value.approval_id,
        approval_digest=value.approval_digest,
        authority_revision=value.authority_revision,
        approval_expires_at=value.approval_expires_at,
        deployment_subject_digest=value.deployment_subject_digest,
        provider=value.provider,
        tenant_id=value.tenant_id,
        workspace_id=value.workspace_id,
        channel_id=value.channel_id,
        origin=CanonicalHTTPSOrigin(host=value.origin.host, port=value.origin.port),
        approved_addresses=tuple(
            ipaddress.ip_address(address.compressed) for address in value.approved_addresses
        ),
        health_path=CanonicalAbsolutePath(value=value.health_path.value),
        read_path=CanonicalAbsolutePath(value=value.read_path.value),
        credential_ref=SecretRef(
            scheme=value.credential_ref.scheme,
            locator=value.credential_ref.locator,
        ),
        verification_secret_ref=SecretRef(
            scheme=value.verification_secret_ref.scheme,
            locator=value.verification_secret_ref.locator,
        ),
        verification_key_id=value.verification_key_id,
        page_limit=value.page_limit,
        max_response_bytes=value.max_response_bytes,
        connect_timeout_ms=value.connect_timeout_ms,
        read_timeout_ms=value.read_timeout_ms,
        outbound_mode=value.outbound_mode,
        redirect_mode=value.redirect_mode,
    )


class NativeIMInboundOnlySandboxAdapter:
    """Explicit E2 adapter with injected transport/mapper and no outbound surface."""

    __slots__ = (
        "__clock",
        "__close_lock",
        "__configuration",
        "__closed",
        "__approval_authority",
        "__approval_permit",
        "__mapper",
        "__process_id",
        "__provider_manifest_digest",
        "__profile",
        "__secret_loader",
        "__transport",
        "__verifier",
    )

    def __init__(
        self,
        configuration: NativeIMInboundOnlyConfigV1,
        profile: IMProviderProfileV1,
        approval_authority: InMemoryNativeIMSandboxApprovalAuthorityV1,
        approval_permit: NativeIMSandboxApprovalPermitV1,
        provider_manifest_digest: str,
        transport: NativeIMInboundTransportPort,
        mapper: NativeIMInboundMapperPort,
        secret_provider: NativeIMSecretResolverPort,
        replay_guard: NativeIMNonceReplayGuardPort,
        *,
        clock: Callable[[], str],
        _composition_token: object,
    ) -> None:
        if _composition_token is not _APPROVED_COMPOSITION_TOKEN:
            raise TypeError("inbound adapters are created by approved composition only")
        configuration_snapshot = _snapshot_configuration(configuration)
        if type(profile) is not IMProviderProfileV1:
            raise TypeError("inbound adapter requires the exact provider profile")
        profile_snapshot = IMProviderProfileV1.from_json_bytes(profile.canonical_bytes())
        if type(approval_authority) is not InMemoryNativeIMSandboxApprovalAuthorityV1:
            raise TypeError("inbound adapter requires the exact sandbox approval authority")
        if type(approval_permit) is not NativeIMSandboxApprovalPermitV1:
            raise TypeError("inbound adapter requires the exact live approval permit")
        _digest(provider_manifest_digest, "providerManifestDigest")
        approval_authority.require_current(approval_permit, operation="health")
        if not isinstance(transport, NativeIMInboundTransportPort):
            raise TypeError("inbound adapter requires the transport port")
        if not isinstance(mapper, NativeIMInboundMapperPort):
            raise TypeError("inbound adapter requires the mapper port")
        if not isinstance(secret_provider, NativeIMSecretResolverPort):
            raise TypeError("inbound adapter requires a secret provider")
        if replay_guard is None:
            raise TypeError("inbound adapter requires a nonce replay guard")
        if not callable(clock):
            raise TypeError("inbound adapter clock must be callable")
        self.__process_id = os.getpid()
        self.__configuration = configuration_snapshot
        self.__profile = profile_snapshot
        self.__approval_authority = approval_authority
        self.__approval_permit = approval_permit
        self.__provider_manifest_digest = provider_manifest_digest
        self.__transport = transport
        self.__mapper = mapper
        self.__secret_loader = NativeIMSecretLoader(configuration_snapshot, secret_provider)
        self.__verifier = NativeIMHMACRawBodyVerifier(
            configuration_snapshot,
            profile_snapshot,
            replay_guard,
        )
        self.__clock = clock
        self.__close_lock = asyncio.Lock()
        self.__closed = False

    def _require_open(self) -> None:
        self._require_current_process()
        if self.__closed:
            raise NativeIMSandboxAdapterClosedError() from None

    def _require_current_process(self) -> None:
        if os.getpid() != self.__process_id:
            raise NativeIMSandboxAdapterProcessMismatchError() from None

    def _now(self) -> str:
        failed = False
        value: object = None
        try:
            value = self.__clock()
        except Exception as error:
            failed = True
            _detach_exception(error)
        if failed or type(value) is not str:
            raise NativeIMTransportContractError() from None
        return value

    def _preflight(self, now: str) -> None:
        validate_native_im_sandbox_preflight_v1(
            self.__configuration,
            self.__profile,
            now=now,
        )

    def _require_approval(self, operation: str) -> NativeIMSandboxApprovalV1:
        self._require_open()
        return self.__approval_authority.require_current(
            self.__approval_permit,
            operation=operation,
        )

    @contextmanager
    def approval_admission_guard(self) -> Iterator[None]:
        """Hold live and durable approval authority across final local admission."""

        self._require_open()
        with self.__approval_authority.admission_guard(
            self.__approval_permit,
            operation="read",
        ):
            yield

    def require_current_approval(self) -> None:
        """Fail closed when lifecycle readiness no longer has live health authority."""

        self._require_approval("health")

    async def capability_snapshot(
        self,
        request: IMCapabilityRequestV1,
    ) -> IMCapabilitySnapshotV1:
        self._require_open()
        self._require_approval("read")
        if type(request) is not IMCapabilityRequestV1:
            raise TypeError("capability request must be the exact V1 class")
        request_snapshot = IMCapabilityRequestV1.from_json_bytes(request.canonical_bytes())
        now = self._now()
        self._preflight(now)
        snapshot = derive_inbound_only_capability_snapshot_v1(
            self.__profile,
            request_snapshot,
            observed_at=now,
        )
        return validate_im_capability_result_v1(request_snapshot, snapshot)

    async def probe_health(self) -> NativeIMHealthEvidenceV1:
        self._require_open()
        self._require_approval("health")
        self._preflight(self._now())
        self._require_approval("health")
        credential = self.__secret_loader.resolve("read_credential")
        failed = False
        result: object = None
        try:
            result = await self.__transport.probe_health(credential)
        except Exception as error:
            failed = True
            _detach_exception(error)
        finally:
            credential.close()
        if failed or type(result) is not NativeIMHealthEvidenceV1 or result.healthy is not True:
            raise NativeIMTransportContractError() from None
        self._require_approval("health")
        return result

    async def read_verified_inbound(
        self,
        request: IMInboundReadRequestV1,
    ) -> NativeIMVerifiedInboundReadV1:
        self._require_open()
        self._require_approval("read")
        if type(request) is not IMInboundReadRequestV1:
            raise TypeError("inbound read request must be the exact V1 class")
        request_snapshot = IMInboundReadRequestV1.from_json_bytes(request.canonical_bytes())
        capability_request = IMCapabilityRequestV1(
            schema_version=1,
            tenant_id=request_snapshot.tenant_id,
            workspace_id=request_snapshot.workspace_id,
            provider=request_snapshot.provider,
            channel_id=request_snapshot.channel_id,
            request_id=(
                "native-im-capability-"
                + hashlib.sha256(request_snapshot.canonical_bytes()).hexdigest()
            ),
        )
        capability = await self.capability_snapshot(capability_request)
        self._require_approval("read")
        credential = self.__secret_loader.resolve("read_credential")
        transport_failed = False
        response: object = None
        try:
            response = await self.__transport.read_inbound(request_snapshot, credential)
        except Exception as error:
            transport_failed = True
            _detach_exception(error)
        finally:
            credential.close()
        if transport_failed or type(response) is not NativeIMInboundRawResponseV1:
            raise NativeIMTransportContractError() from None
        self._require_approval("read")

        verification_material = self.__secret_loader.resolve("verification_key")
        raw_verification = self.__verifier.verify_for_atomic_admission(
            response.metadata,
            response.raw_body,
            verification_material,
            now=self._now(),
        )
        self._require_approval("read")
        mapping_failed = False
        mapped: object = None
        try:
            mapped = self.__mapper.map_inbound(
                response,
                request_snapshot,
                capability,
                raw_verification,
                self.__profile,
            )
        except Exception as error:
            mapping_failed = True
            _detach_exception(error)
        if mapping_failed or type(mapped) is not NativeIMMappedPageV1:
            raise NativeIMTransportContractError() from None
        self._require_approval("read")
        page = parse_native_im_inbound_page_v1(
            response,
            mapped,
            request_snapshot,
            capability,
            raw_verification,
            self.__configuration,
            self.__profile,
        )
        approval = self._require_approval("read")
        provenance = NativeIMSandboxAdmissionProvenanceV1(
            schema_version=1,
            approval_id=approval.approval_id,
            authority_revision=approval.authority_revision,
            approval_digest=approval.canonical_digest(),
            configuration_binding_digest=approval.configuration_binding_digest,
            profile_id=self.__profile.profile_id,
            profile_revision=self.__profile.revision,
            profile_digest=self.__profile.canonical_digest(),
            provider_manifest_digest=self.__provider_manifest_digest,
            transport_contract_id=approval.transport_contract_id,
            transport_contract_digest=approval.transport_contract_digest,
            mapper_contract_id=approval.mapper_contract_id,
            mapper_contract_digest=approval.mapper_contract_digest,
            read_request_digest=request_snapshot.canonical_digest(),
            page_digest=page.canonical_digest(),
            transport_evidence_digest=response.transport_evidence_digest,
            mapping_evidence_digest=mapped.mapping_evidence_digest,
        )
        return NativeIMVerifiedInboundReadV1(
            request=request_snapshot,
            capability=capability,
            page=page,
            raw_verification=raw_verification,
            mapping_evidence_digest=mapped.mapping_evidence_digest,
            provenance=provenance,
        )

    async def read_inbound(self, request: IMInboundReadRequestV1) -> IMInboundPageV1:
        return (await self.read_verified_inbound(request)).page

    async def dispatch(self, request: IMDispatchRequestV1) -> IMActionReceiptV1:
        raise NativeIMOutboundForbiddenError() from None

    async def query_acceptance(self, query: IMAcceptanceQueryV1) -> IMActionReceiptV1:
        raise NativeIMOutboundForbiddenError() from None

    async def aclose(self) -> None:
        self._require_current_process()
        async with self.__close_lock:
            self._require_current_process()
            if self.__closed:
                return
            failed = False
            try:
                await self.__transport.aclose()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                failed = True
                _detach_exception(error)
            if failed:
                raise NativeIMTransportContractError() from None
            self.__closed = True

    @property
    def closed(self) -> bool:
        self._require_current_process()
        return self.__closed

    def __repr__(self) -> str:
        self._require_current_process()
        return (
            "NativeIMInboundOnlySandboxAdapter("
            f"config={self.__configuration.fingerprint!r}, "
            f"profile={self.__profile.canonical_digest()[:12]!r}, "
            f"closed={self.__closed!r})"
        )

    def __reduce_ex__(self, protocol: SupportsIndex) -> NoReturn:
        raise TypeError("native IM inbound sandbox adapter cannot be serialized")


class NativeIMDisabledSandboxAdapter:
    """The only adapter produced by the default service composition."""

    __slots__ = ("__configuration_fingerprint", "__closed", "__process_id")

    def __init__(self, configuration: NativeIMDisabledConfigV1) -> None:
        if type(configuration) is not NativeIMDisabledConfigV1:
            raise TypeError("disabled adapter requires the exact disabled configuration")
        self.__process_id = os.getpid()
        self.__configuration_fingerprint = configuration.fingerprint
        self.__closed = False

    def _require_current_process(self) -> None:
        if os.getpid() != self.__process_id:
            raise NativeIMSandboxAdapterProcessMismatchError() from None

    def _reject(self) -> NoReturn:
        self._require_current_process()
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
        self._require_current_process()
        self.__closed = True

    @property
    def closed(self) -> bool:
        self._require_current_process()
        return self.__closed

    def __repr__(self) -> str:
        self._require_current_process()
        return (
            "NativeIMDisabledSandboxAdapter("
            f"config={self.__configuration_fingerprint!r}, closed={self.__closed!r})"
        )

    def __reduce_ex__(self, protocol: SupportsIndex) -> NoReturn:
        raise TypeError("native IM disabled sandbox adapter cannot be serialized")


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
    "NativeIMInboundMapperPort",
    "NativeIMInboundOnlySandboxAdapter",
    "NativeIMInboundRawResponseV1",
    "NativeIMInboundTransportPort",
    "NativeIMMappedPageV1",
    "NativeIMOutboundForbiddenError",
    "NativeIMSandboxDisabledError",
    "NativeIMSecretResolverPort",
    "NativeIMSandboxAdapterClosedError",
    "NativeIMSandboxAdapterProcessMismatchError",
    "NativeIMTransportContractError",
    "NativeIMVerifiedInboundReadV1",
    "compose_default_native_im_sandbox_v1",
    "parse_native_im_inbound_page_v1",
]
