# ruff: noqa: UP006, UP035
"""Explicit approved composition root for one native-IM provider sandbox bundle.

Nothing is registered at import time. The default composer remains disabled. This module
only joins a durable live approval to an explicitly supplied, immutable provider bundle
whose transport and mapper build contracts are already named in that approval.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, ClassVar, Dict, NoReturn, SupportsIndex, cast

from . import process_identity as _process_identity
from ._native_im_codec import (
    NativeIMCodecTooLargeError,
    _canonical_json_bytes,
    _decode_json_bytes,
    _digest,
    _id,
    _model_digest,
    _plain_dict,
    _schema_version,
)
from .native_im_auth import NativeIMNonceReplayGuardPort
from .native_im_provider_profile import IMProviderProfileV1
from .native_im_sandbox import (
    _APPROVED_COMPOSITION_TOKEN,
    NativeIMInboundMapperPort,
    NativeIMInboundOnlySandboxAdapter,
    NativeIMInboundTransportPort,
    NativeIMSecretResolverPort,
)
from .native_im_sandbox_approval import NativeIMSandboxApprovalV1
from .native_im_sandbox_authority import (
    InMemoryNativeIMSandboxApprovalAuthorityV1,
)
from .service.native_im_config import NativeIMInboundOnlyConfigV1

_REGISTRATION_TOKEN = object()
_MAX_MANIFEST_BYTES = 32 * 1_024
_MANIFEST_FIELDS = {
    "mapperContractDigest",
    "mapperContractId",
    "profileDigest",
    "profileId",
    "profileRevision",
    "provider",
    "registrationId",
    "schemaVersion",
    "sourceEvidenceDigest",
    "transportContractDigest",
    "transportContractId",
}


class NativeIMSandboxCompositionError(RuntimeError):
    """Stable redacted rejection from explicit approved composition."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, repr=False)
class NativeIMProviderSandboxManifestV1:
    """Canonical identity of one reviewed provider transport/mapper build bundle."""

    schema_version: int
    registration_id: str
    provider: str
    profile_id: str
    profile_revision: str
    profile_digest: str = field(repr=False)
    transport_contract_id: str
    transport_contract_digest: str = field(repr=False)
    mapper_contract_id: str
    mapper_contract_digest: str = field(repr=False)
    source_evidence_digest: str = field(repr=False)

    _MODEL_NAME: ClassVar[str] = "NativeIMProviderSandboxManifestV1"

    def __post_init__(self) -> None:
        if type(self) is not NativeIMProviderSandboxManifestV1:
            raise TypeError("provider sandbox manifest requires the exact V1 class")
        _schema_version(self.schema_version)
        for value, label in (
            (self.registration_id, "registrationId"),
            (self.provider, "provider"),
            (self.profile_id, "profileId"),
            (self.profile_revision, "profileRevision"),
            (self.transport_contract_id, "transportContractId"),
            (self.mapper_contract_id, "mapperContractId"),
        ):
            _id(value, label)
        for value, label in (
            (self.profile_digest, "profileDigest"),
            (self.transport_contract_digest, "transportContractDigest"),
            (self.mapper_contract_digest, "mapperContractDigest"),
            (self.source_evidence_digest, "sourceEvidenceDigest"),
        ):
            _digest(value, label)
        self.canonical_bytes()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mapperContractDigest": self.mapper_contract_digest,
            "mapperContractId": self.mapper_contract_id,
            "profileDigest": self.profile_digest,
            "profileId": self.profile_id,
            "profileRevision": self.profile_revision,
            "provider": self.provider,
            "registrationId": self.registration_id,
            "schemaVersion": self.schema_version,
            "sourceEvidenceDigest": self.source_evidence_digest,
            "transportContractDigest": self.transport_contract_digest,
            "transportContractId": self.transport_contract_id,
        }

    @classmethod
    def from_dict(cls, value: object) -> NativeIMProviderSandboxManifestV1:
        if cls is not NativeIMProviderSandboxManifestV1:
            raise TypeError("provider sandbox manifest decoder requires the exact V1 class")
        body = _plain_dict(value, _MANIFEST_FIELDS, "native IM provider sandbox manifest")
        return cls(
            schema_version=body["schemaVersion"],
            registration_id=body["registrationId"],
            provider=body["provider"],
            profile_id=body["profileId"],
            profile_revision=body["profileRevision"],
            profile_digest=body["profileDigest"],
            transport_contract_id=body["transportContractId"],
            transport_contract_digest=body["transportContractDigest"],
            mapper_contract_id=body["mapperContractId"],
            mapper_contract_digest=body["mapperContractDigest"],
            source_evidence_digest=body["sourceEvidenceDigest"],
        )

    @classmethod
    def from_json_bytes(cls, encoded: object) -> NativeIMProviderSandboxManifestV1:
        if cls is not NativeIMProviderSandboxManifestV1:
            raise TypeError("provider sandbox manifest decoder requires the exact V1 class")
        return cls.from_dict(
            _decode_json_bytes(
                encoded,
                "native IM provider sandbox manifest",
                maximum_bytes=_MAX_MANIFEST_BYTES,
            )
        )

    def canonical_bytes(self) -> bytes:
        encoded = _canonical_json_bytes(self.to_dict())
        if len(encoded) > _MAX_MANIFEST_BYTES:
            raise NativeIMCodecTooLargeError("provider sandbox manifest exceeds its byte limit")
        return encoded

    def canonical_digest(self) -> str:
        return _model_digest(self._MODEL_NAME, self.to_dict())

    def __repr__(self) -> str:
        return f"NativeIMProviderSandboxManifestV1(digest={self.canonical_digest()[:12]!r})"


class NativeIMProviderSandboxRegistrationV1:
    """Process-bound immutable registration of exact provider component instances."""

    __slots__ = (
        "__manifest",
        "__mapper",
        "__mapper_type",
        "__process_owner",
        "__replay_guard",
        "__replay_guard_type",
        "__secret_provider",
        "__secret_provider_type",
        "__transport",
        "__transport_type",
    )

    def __init__(
        self,
        manifest: NativeIMProviderSandboxManifestV1,
        *,
        transport: object,
        mapper: object,
        secret_provider: object,
        replay_guard: object,
    ) -> None:
        if type(manifest) is not NativeIMProviderSandboxManifestV1:
            raise TypeError("registration requires the exact provider sandbox manifest")
        for component in (transport, mapper, secret_provider, replay_guard):
            if component is None:
                raise TypeError("registration components must be explicit objects")
        self.__process_owner = _process_identity.capture_process_owner()
        self.__manifest = NativeIMProviderSandboxManifestV1.from_json_bytes(
            manifest.canonical_bytes()
        )
        self.__transport = transport
        self.__transport_type = type(transport)
        self.__mapper = mapper
        self.__mapper_type = type(mapper)
        self.__secret_provider = secret_provider
        self.__secret_provider_type = type(secret_provider)
        self.__replay_guard = replay_guard
        self.__replay_guard_type = type(replay_guard)

    def _require_process(self) -> None:
        _process_identity.require_current_process(
            self.__process_owner,
            lambda: NativeIMSandboxCompositionError(
                "native_im_sandbox_registration_process_mismatch"
            ),
        )

    def _manifest_snapshot(self, token: object) -> NativeIMProviderSandboxManifestV1:
        self._require_process()
        if token is not _REGISTRATION_TOKEN:
            raise TypeError("provider registration is consumed by approved composition")
        return NativeIMProviderSandboxManifestV1.from_json_bytes(
            self.__manifest.canonical_bytes()
        )

    def _components(self, token: object) -> tuple[object, object, object, object]:
        self._require_process()
        if token is not _REGISTRATION_TOKEN:
            raise TypeError("provider registration is consumed by approved composition")
        components = (
            self.__transport,
            self.__mapper,
            self.__secret_provider,
            self.__replay_guard,
        )
        expected_types = (
            self.__transport_type,
            self.__mapper_type,
            self.__secret_provider_type,
            self.__replay_guard_type,
        )
        if tuple(type(component) for component in components) != expected_types:
            raise NativeIMSandboxCompositionError(
                "native_im_sandbox_registration_component_drift"
            ) from None
        return components

    def __copy__(self) -> NoReturn:
        raise TypeError("native IM provider registrations cannot be copied")

    def __deepcopy__(self, memo: object) -> NoReturn:
        raise TypeError("native IM provider registrations cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("native IM provider registrations cannot be serialized")

    def __reduce_ex__(self, protocol: SupportsIndex) -> NoReturn:
        raise TypeError("native IM provider registrations cannot be serialized")

    def __repr__(self) -> str:
        self._require_process()
        return (
            "NativeIMProviderSandboxRegistrationV1("
            f"manifest={self.__manifest.canonical_digest()[:12]!r})"
        )


def _validate_manifest_binding(
    manifest: NativeIMProviderSandboxManifestV1,
    approval: NativeIMSandboxApprovalV1,
    configuration: NativeIMInboundOnlyConfigV1,
    profile: IMProviderProfileV1,
) -> None:
    expected = (
        configuration.provider,
        profile.profile_id,
        profile.revision,
        profile.canonical_digest(),
        approval.transport_contract_id,
        approval.transport_contract_digest,
        approval.mapper_contract_id,
        approval.mapper_contract_digest,
    )
    actual = (
        manifest.provider,
        manifest.profile_id,
        manifest.profile_revision,
        manifest.profile_digest,
        manifest.transport_contract_id,
        manifest.transport_contract_digest,
        manifest.mapper_contract_id,
        manifest.mapper_contract_digest,
    )
    if actual != expected:
        raise NativeIMSandboxCompositionError(
            "native_im_sandbox_provider_manifest_mismatch"
        ) from None


def compose_approved_native_im_sandbox_v1(
    configuration: NativeIMInboundOnlyConfigV1,
    profile: IMProviderProfileV1,
    authority: InMemoryNativeIMSandboxApprovalAuthorityV1,
    registration: NativeIMProviderSandboxRegistrationV1,
    *,
    clock: Callable[[], str],
) -> NativeIMInboundOnlySandboxAdapter:
    """Compose exactly one approved bundle; never called by default composition."""

    if type(configuration) is not NativeIMInboundOnlyConfigV1:
        raise TypeError("approved composition requires the exact inbound configuration")
    if type(profile) is not IMProviderProfileV1:
        raise TypeError("approved composition requires the exact provider profile")
    if type(authority) is not InMemoryNativeIMSandboxApprovalAuthorityV1:
        raise TypeError("approved composition requires the exact approval authority")
    if type(registration) is not NativeIMProviderSandboxRegistrationV1:
        raise TypeError("approved composition requires the exact provider registration")
    if not callable(clock):
        raise TypeError("approved composition clock must be callable")
    if not authority.durable:
        raise NativeIMSandboxCompositionError(
            "native_im_sandbox_durable_authority_required"
        ) from None
    permit = authority.activate(configuration, profile)
    approval = authority.require_current(permit, operation="health")
    manifest = registration._manifest_snapshot(_REGISTRATION_TOKEN)
    _validate_manifest_binding(manifest, approval, configuration, profile)
    transport, mapper, secret_provider, replay_guard = registration._components(
        _REGISTRATION_TOKEN
    )
    if not isinstance(transport, NativeIMInboundTransportPort):
        raise NativeIMSandboxCompositionError(
            "native_im_sandbox_registered_transport_invalid"
        ) from None
    if not isinstance(mapper, NativeIMInboundMapperPort):
        raise NativeIMSandboxCompositionError(
            "native_im_sandbox_registered_mapper_invalid"
        ) from None
    if not isinstance(secret_provider, NativeIMSecretResolverPort):
        raise NativeIMSandboxCompositionError(
            "native_im_sandbox_registered_secret_provider_invalid"
        ) from None
    return NativeIMInboundOnlySandboxAdapter(
        configuration,
        profile,
        authority,
        permit,
        transport,
        mapper,
        secret_provider,
        cast(NativeIMNonceReplayGuardPort, replay_guard),
        clock=clock,
        _composition_token=_APPROVED_COMPOSITION_TOKEN,
    )


__all__ = [
    "NativeIMProviderSandboxManifestV1",
    "NativeIMProviderSandboxRegistrationV1",
    "NativeIMSandboxCompositionError",
    "compose_approved_native_im_sandbox_v1",
]
