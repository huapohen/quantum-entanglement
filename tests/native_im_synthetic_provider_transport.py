"""Scripted zero-network transport candidate used only by the offline provider TCK."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable, NoReturn

from quantum_entanglement.native_im import IMInboundReadRequestV1
from quantum_entanglement.native_im_auth import NativeIMDetachedSignatureV1
from quantum_entanglement.native_im_provider_exchange import (
    NativeIMProviderExchangePortV1,
    NativeIMProviderRequestIntentV1,
    NativeIMProviderWireResponseV1,
)
from quantum_entanglement.native_im_provider_profile import IMProviderProfileV1
from quantum_entanglement.native_im_sandbox import (
    NativeIMHealthEvidenceV1,
    NativeIMInboundRawResponseV1,
    NativeIMTransportContractError,
    derive_native_im_health_evidence_digest_v1,
)
from quantum_entanglement.service.native_im_config import (
    CanonicalAbsolutePath,
    CanonicalHTTPSOrigin,
    NativeIMInboundOnlyConfigV1,
)
from quantum_entanglement.service.secrets import SecretMaterial

TRANSPORT_CONTRACT_ID = "test-native-im-transport-v1"
TRANSPORT_CONTRACT_DIGEST = "2" * 64
HEALTH_BODY = b'{"healthy":true,"schemaVersion":1}'

_READ_HEADER_NAMES = {
    "x-native-im-event-source-digest",
    "x-native-im-key-id",
    "x-native-im-nonce",
    "x-native-im-read-request-id",
    "x-native-im-signature",
    "x-native-im-timestamp",
}


def _contract_failure() -> NoReturn:
    raise NativeIMTransportContractError() from None


@dataclass(frozen=True, repr=False)
class ScriptedNativeIMExchangeStepV1:
    expected_intent: NativeIMProviderRequestIntentV1 = field(repr=False)
    response: NativeIMProviderWireResponseV1 | None = field(default=None, repr=False)
    fault_code: str | None = None

    def __post_init__(self) -> None:
        if type(self) is not ScriptedNativeIMExchangeStepV1:
            raise TypeError("scripted exchange step requires the exact V1 class")
        if type(self.expected_intent) is not NativeIMProviderRequestIntentV1:
            raise TypeError("scripted exchange step requires an exact request intent")
        if (self.response is None) == (self.fault_code is None):
            raise ValueError("scripted exchange step requires exactly one outcome")
        if self.response is not None and type(self.response) is not NativeIMProviderWireResponseV1:
            raise TypeError("scripted exchange response requires the exact V1 class")
        if self.fault_code is not None and self.fault_code not in {
            "disconnect",
            "timeout",
        }:
            raise ValueError("scripted exchange fault is not registered")


class ScriptedNativeIMProviderExchangeV1:
    """Consume an exact immutable response script without opening any external resource."""

    __slots__ = ("__closed", "__position", "__steps", "credential_ids", "intents")

    def __init__(self, steps: tuple[ScriptedNativeIMExchangeStepV1, ...]) -> None:
        if type(steps) is not tuple or not steps:
            raise TypeError("scripted exchange requires a non-empty tuple")
        if any(type(step) is not ScriptedNativeIMExchangeStepV1 for step in steps):
            raise TypeError("scripted exchange requires exact V1 steps")
        self.__steps = steps
        self.__position = 0
        self.__closed = False
        self.intents: list[NativeIMProviderRequestIntentV1] = []
        self.credential_ids: list[int] = []

    async def exchange(
        self,
        intent: NativeIMProviderRequestIntentV1,
        credential: SecretMaterial,
    ) -> NativeIMProviderWireResponseV1:
        if self.__closed or self.__position >= len(self.__steps):
            raise AssertionError("scripted_native_im_exchange_unexpected_call")
        if type(intent) is not NativeIMProviderRequestIntentV1:
            raise AssertionError("scripted_native_im_exchange_intent_type")
        if type(credential) is not SecretMaterial:
            raise AssertionError("scripted_native_im_exchange_credential_type")
        step = self.__steps[self.__position]
        if intent != step.expected_intent:
            raise AssertionError("scripted_native_im_exchange_intent_mismatch")
        self.__position += 1
        self.intents.append(intent)
        self.credential_ids.append(id(credential))
        if step.fault_code is not None:
            if step.fault_code == "timeout":
                raise TimeoutError("scripted-timeout-provider-content-canary")
            raise ConnectionError("scripted-disconnect-provider-content-canary")
        if type(step.response) is not NativeIMProviderWireResponseV1:
            raise AssertionError("scripted_native_im_exchange_response_missing")
        return step.response

    async def aclose(self) -> None:
        self.__closed = True

    @property
    def consumed(self) -> bool:
        return self.__position == len(self.__steps)

    @property
    def closed(self) -> bool:
        return self.__closed

    def __repr__(self) -> str:
        return (
            "ScriptedNativeIMProviderExchangeV1("
            f"position={self.__position}, steps={len(self.__steps)}, closed={self.__closed})"
        )


class SyntheticSemanticProviderTransportV1:
    """Provider request/response mapping over an injected exchange and no network stack."""

    __slots__ = (
        "__closed",
        "__configuration",
        "__exchange",
        "__profile",
        "__provider_manifest_digest",
        "__clock",
    )

    def __init__(
        self,
        configuration: NativeIMInboundOnlyConfigV1,
        profile: IMProviderProfileV1,
        provider_manifest_digest: str,
        exchange: NativeIMProviderExchangePortV1,
        *,
        clock: Callable[[], str],
    ) -> None:
        if type(configuration) is not NativeIMInboundOnlyConfigV1:
            raise TypeError("synthetic provider transport requires an exact configuration")
        if type(profile) is not IMProviderProfileV1:
            raise TypeError("synthetic provider transport requires an exact profile")
        if type(provider_manifest_digest) is not str or len(provider_manifest_digest) != 64:
            raise TypeError("synthetic provider transport requires a manifest digest")
        int(provider_manifest_digest, 16)
        if not isinstance(exchange, NativeIMProviderExchangePortV1):
            raise TypeError("synthetic provider transport requires an exchange port")
        if not callable(clock):
            raise TypeError("synthetic provider transport requires a clock")
        self.__configuration = configuration
        self.__profile = profile
        self.__provider_manifest_digest = provider_manifest_digest
        self.__exchange = exchange
        self.__clock = clock
        self.__closed = False

    def _require_open(self) -> None:
        if self.__closed:
            _contract_failure()

    def health_intent(self) -> NativeIMProviderRequestIntentV1:
        self._require_open()
        configuration = self.__configuration
        return NativeIMProviderRequestIntentV1(
            schema_version=1,
            operation="health",
            method="GET",
            origin=CanonicalHTTPSOrigin(
                host=configuration.origin.host,
                port=configuration.origin.port,
            ),
            path=CanonicalAbsolutePath(configuration.health_path.value),
            query=(),
            read_request_id=None,
            read_request_digest=None,
            connect_timeout_ms=configuration.connect_timeout_ms,
            read_timeout_ms=configuration.read_timeout_ms,
            max_response_bytes=configuration.max_response_bytes,
            redirect_mode="deny",
        )

    def read_intent(self, request: IMInboundReadRequestV1) -> NativeIMProviderRequestIntentV1:
        self._require_open()
        if type(request) is not IMInboundReadRequestV1:
            raise TypeError("synthetic provider transport requires an exact read request")
        configuration = self.__configuration
        query = [
            ("limit", str(request.limit)),
            ("readRequestDigest", request.canonical_digest()),
            ("readRequestId", request.read_request_id),
        ]
        if request.after_cursor is not None:
            query.append(("afterCursor", request.after_cursor))
            query.append(("afterSequence", str(request.after_sequence)))
        if request.snapshot_token is not None:
            query.append(("snapshotToken", request.snapshot_token))
        return NativeIMProviderRequestIntentV1(
            schema_version=1,
            operation="read",
            method="GET",
            origin=CanonicalHTTPSOrigin(
                host=configuration.origin.host,
                port=configuration.origin.port,
            ),
            path=CanonicalAbsolutePath(configuration.read_path.value),
            query=tuple(sorted(query)),
            read_request_id=request.read_request_id,
            read_request_digest=request.canonical_digest(),
            connect_timeout_ms=configuration.connect_timeout_ms,
            read_timeout_ms=configuration.read_timeout_ms,
            max_response_bytes=configuration.max_response_bytes,
            redirect_mode="deny",
        )

    async def probe_health(self, credential: SecretMaterial) -> NativeIMHealthEvidenceV1:
        self._require_open()
        if type(credential) is not SecretMaterial:
            raise TypeError("synthetic provider transport requires an exact credential lease")
        intent = self.health_intent()
        response: NativeIMProviderWireResponseV1 | None
        try:
            response = await self.__exchange.exchange(intent, credential)
        except Exception as error:
            error.__traceback__ = None
            error.__cause__ = None
            error.__context__ = None
            response = None
        if (
            response is None
            or type(response) is not NativeIMProviderWireResponseV1
            or response.status_code != 200
            or response.raw_body != HEALTH_BODY
            or response.received_at != self.__clock()
        ):
            _contract_failure()
        try:
            decoded = json.loads(response.raw_body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            _contract_failure()
        if decoded != {"healthy": True, "schemaVersion": 1}:
            _contract_failure()
        configuration_binding_digest = self.__configuration.approval_binding_digest
        profile_digest = self.__profile.canonical_digest()
        request_intent_digest = intent.canonical_digest()
        exchange_security_evidence_digest = response.exchange_security_evidence_digest
        evidence_digest = derive_native_im_health_evidence_digest_v1(
            healthy=True,
            observed_at=response.received_at,
            status_code=response.status_code,
            configuration_binding_digest=configuration_binding_digest,
            profile_digest=profile_digest,
            provider_manifest_digest=self.__provider_manifest_digest,
            transport_contract_id=TRANSPORT_CONTRACT_ID,
            transport_contract_digest=TRANSPORT_CONTRACT_DIGEST,
            request_intent_digest=request_intent_digest,
            exchange_security_evidence_digest=exchange_security_evidence_digest,
        )
        return NativeIMHealthEvidenceV1(
            schema_version=1,
            healthy=True,
            observed_at=response.received_at,
            status_code=response.status_code,
            configuration_binding_digest=configuration_binding_digest,
            profile_digest=profile_digest,
            provider_manifest_digest=self.__provider_manifest_digest,
            transport_contract_id=TRANSPORT_CONTRACT_ID,
            transport_contract_digest=TRANSPORT_CONTRACT_DIGEST,
            request_intent_digest=request_intent_digest,
            exchange_security_evidence_digest=exchange_security_evidence_digest,
            evidence_digest=evidence_digest,
        )

    async def read_inbound(
        self,
        request: IMInboundReadRequestV1,
        credential: SecretMaterial,
    ) -> NativeIMInboundRawResponseV1:
        self._require_open()
        if type(credential) is not SecretMaterial:
            raise TypeError("synthetic provider transport requires an exact credential lease")
        intent = self.read_intent(request)
        response: NativeIMProviderWireResponseV1 | None
        try:
            response = await self.__exchange.exchange(intent, credential)
        except Exception as error:
            error.__traceback__ = None
            error.__cause__ = None
            error.__context__ = None
            response = None
        if (
            response is None
            or type(response) is not NativeIMProviderWireResponseV1
            or response.status_code != 200
            or not response.raw_body
            or len(response.raw_body) > self.__configuration.max_response_bytes
            or response.received_at != self.__clock()
        ):
            _contract_failure()
        headers = dict(response.headers)
        if set(headers) != _READ_HEADER_NAMES:
            _contract_failure()
        if headers["x-native-im-read-request-id"] != request.read_request_id:
            _contract_failure()
        try:
            metadata = NativeIMDetachedSignatureV1(
                schema_version=1,
                timestamp=headers["x-native-im-timestamp"],
                nonce=headers["x-native-im-nonce"],
                key_id=headers["x-native-im-key-id"],
                signature=headers["x-native-im-signature"],
            )
            return NativeIMInboundRawResponseV1(
                schema_version=1,
                read_request_id=request.read_request_id,
                status_code=response.status_code,
                metadata=metadata,
                raw_body=response.raw_body,
                received_at=response.received_at,
                transport_evidence_digest=headers["x-native-im-event-source-digest"],
            )
        except (TypeError, ValueError):
            _contract_failure()

    async def aclose(self) -> None:
        if self.__closed:
            return
        await self.__exchange.aclose()
        self.__closed = True

    @property
    def closed(self) -> bool:
        return self.__closed


__all__ = [
    "HEALTH_BODY",
    "ScriptedNativeIMExchangeStepV1",
    "ScriptedNativeIMProviderExchangeV1",
    "SyntheticSemanticProviderTransportV1",
    "TRANSPORT_CONTRACT_DIGEST",
    "TRANSPORT_CONTRACT_ID",
]
