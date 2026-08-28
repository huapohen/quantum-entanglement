# ruff: noqa: UP006, UP035
"""Canonical provenance required for native-IM sandbox page admission."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Dict, Union, cast

from ._native_im_codec import (
    NativeIMCodecTooLargeError,
    _canonical_json_bytes,
    _decode_json_bytes,
    _digest,
    _id,
    _model_digest,
    _plain_dict,
    _positive_integer,
    _schema_version,
)
from .native_im_read_exchange import NativeIMInboundReadExchangeEvidenceV1

_MAX_PROVENANCE_BYTES = 32 * 1_024
_PROVENANCE_FIELDS = {
    "approvalDigest",
    "approvalId",
    "authorityRevision",
    "configurationBindingDigest",
    "mapperContractDigest",
    "mapperContractId",
    "mappingEvidenceDigest",
    "pageDigest",
    "profileDigest",
    "profileId",
    "profileRevision",
    "providerManifestDigest",
    "readRequestDigest",
    "schemaVersion",
    "transportContractDigest",
    "transportContractId",
    "transportEvidenceDigest",
}
_EXCHANGE_PROVENANCE_FIELDS = _PROVENANCE_FIELDS | {"readExchangeEvidence"}


@dataclass(frozen=True, repr=False)
class NativeIMSandboxAdmissionProvenanceV1:
    """Exact approval/build/evidence binding carried into atomic durable admission."""

    schema_version: int
    approval_id: str
    authority_revision: int
    approval_digest: str = field(repr=False)
    configuration_binding_digest: str = field(repr=False)
    profile_id: str
    profile_revision: str
    profile_digest: str = field(repr=False)
    provider_manifest_digest: str = field(repr=False)
    transport_contract_id: str
    transport_contract_digest: str = field(repr=False)
    mapper_contract_id: str
    mapper_contract_digest: str = field(repr=False)
    read_request_digest: str = field(repr=False)
    page_digest: str = field(repr=False)
    transport_evidence_digest: str = field(repr=False)
    mapping_evidence_digest: str = field(repr=False)

    _MODEL_NAME: ClassVar[str] = "NativeIMSandboxAdmissionProvenanceV1"

    def __post_init__(self) -> None:
        if type(self) is not NativeIMSandboxAdmissionProvenanceV1:
            raise TypeError("sandbox admission provenance requires the exact V1 class")
        _schema_version(self.schema_version)
        for value, label in (
            (self.approval_id, "approvalId"),
            (self.profile_id, "profileId"),
            (self.profile_revision, "profileRevision"),
            (self.transport_contract_id, "transportContractId"),
            (self.mapper_contract_id, "mapperContractId"),
        ):
            _id(value, label)
        _positive_integer(self.authority_revision, "authorityRevision")
        for value, label in (
            (self.approval_digest, "approvalDigest"),
            (self.configuration_binding_digest, "configurationBindingDigest"),
            (self.profile_digest, "profileDigest"),
            (self.provider_manifest_digest, "providerManifestDigest"),
            (self.transport_contract_digest, "transportContractDigest"),
            (self.mapper_contract_digest, "mapperContractDigest"),
            (self.read_request_digest, "readRequestDigest"),
            (self.page_digest, "pageDigest"),
            (self.transport_evidence_digest, "transportEvidenceDigest"),
            (self.mapping_evidence_digest, "mappingEvidenceDigest"),
        ):
            _digest(value, label)
        self.canonical_bytes()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "approvalDigest": self.approval_digest,
            "approvalId": self.approval_id,
            "authorityRevision": self.authority_revision,
            "configurationBindingDigest": self.configuration_binding_digest,
            "mapperContractDigest": self.mapper_contract_digest,
            "mapperContractId": self.mapper_contract_id,
            "mappingEvidenceDigest": self.mapping_evidence_digest,
            "pageDigest": self.page_digest,
            "profileDigest": self.profile_digest,
            "profileId": self.profile_id,
            "profileRevision": self.profile_revision,
            "providerManifestDigest": self.provider_manifest_digest,
            "readRequestDigest": self.read_request_digest,
            "schemaVersion": self.schema_version,
            "transportContractDigest": self.transport_contract_digest,
            "transportContractId": self.transport_contract_id,
            "transportEvidenceDigest": self.transport_evidence_digest,
        }

    @classmethod
    def from_dict(cls, value: object) -> NativeIMSandboxAdmissionProvenanceV1:
        if cls is not NativeIMSandboxAdmissionProvenanceV1:
            raise TypeError("sandbox admission provenance decoder requires the exact V1 class")
        body = _plain_dict(value, _PROVENANCE_FIELDS, "native IM sandbox admission provenance")
        return cls(
            schema_version=body["schemaVersion"],
            approval_id=body["approvalId"],
            authority_revision=body["authorityRevision"],
            approval_digest=body["approvalDigest"],
            configuration_binding_digest=body["configurationBindingDigest"],
            profile_id=body["profileId"],
            profile_revision=body["profileRevision"],
            profile_digest=body["profileDigest"],
            provider_manifest_digest=body["providerManifestDigest"],
            transport_contract_id=body["transportContractId"],
            transport_contract_digest=body["transportContractDigest"],
            mapper_contract_id=body["mapperContractId"],
            mapper_contract_digest=body["mapperContractDigest"],
            read_request_digest=body["readRequestDigest"],
            page_digest=body["pageDigest"],
            transport_evidence_digest=body["transportEvidenceDigest"],
            mapping_evidence_digest=body["mappingEvidenceDigest"],
        )

    @classmethod
    def from_json_bytes(cls, encoded: object) -> NativeIMSandboxAdmissionProvenanceV1:
        if cls is not NativeIMSandboxAdmissionProvenanceV1:
            raise TypeError("sandbox admission provenance decoder requires the exact V1 class")
        return cls.from_dict(
            _decode_json_bytes(
                encoded,
                "native IM sandbox admission provenance",
                maximum_bytes=_MAX_PROVENANCE_BYTES,
            )
        )

    def canonical_bytes(self) -> bytes:
        encoded = _canonical_json_bytes(self.to_dict())
        if len(encoded) > _MAX_PROVENANCE_BYTES:
            raise NativeIMCodecTooLargeError("sandbox admission provenance exceeds its byte limit")
        return encoded

    def canonical_digest(self) -> str:
        return _model_digest(self._MODEL_NAME, self.to_dict())

    def __repr__(self) -> str:
        return (
            "NativeIMSandboxAdmissionProvenanceV1("
            f"revision={self.authority_revision}, digest={self.canonical_digest()[:12]!r})"
        )


@dataclass(frozen=True, repr=False)
class NativeIMSandboxExchangeAdmissionProvenanceV1:
    """Admission provenance with one durable, request-correlated read exchange."""

    schema_version: int
    approval_id: str
    authority_revision: int
    approval_digest: str = field(repr=False)
    configuration_binding_digest: str = field(repr=False)
    profile_id: str
    profile_revision: str
    profile_digest: str = field(repr=False)
    provider_manifest_digest: str = field(repr=False)
    transport_contract_id: str
    transport_contract_digest: str = field(repr=False)
    mapper_contract_id: str
    mapper_contract_digest: str = field(repr=False)
    read_request_digest: str = field(repr=False)
    page_digest: str = field(repr=False)
    transport_evidence_digest: str = field(repr=False)
    mapping_evidence_digest: str = field(repr=False)
    read_exchange_evidence: NativeIMInboundReadExchangeEvidenceV1 = field(repr=False)

    _MODEL_NAME: ClassVar[str] = "NativeIMSandboxExchangeAdmissionProvenanceV1"

    def __post_init__(self) -> None:
        if type(self) is not NativeIMSandboxExchangeAdmissionProvenanceV1:
            raise TypeError("sandbox exchange provenance requires the exact V1 class")
        _schema_version(self.schema_version)
        for value, label in (
            (self.approval_id, "approvalId"),
            (self.profile_id, "profileId"),
            (self.profile_revision, "profileRevision"),
            (self.transport_contract_id, "transportContractId"),
            (self.mapper_contract_id, "mapperContractId"),
        ):
            _id(value, label)
        _positive_integer(self.authority_revision, "authorityRevision")
        for value, label in (
            (self.approval_digest, "approvalDigest"),
            (self.configuration_binding_digest, "configurationBindingDigest"),
            (self.profile_digest, "profileDigest"),
            (self.provider_manifest_digest, "providerManifestDigest"),
            (self.transport_contract_digest, "transportContractDigest"),
            (self.mapper_contract_digest, "mapperContractDigest"),
            (self.read_request_digest, "readRequestDigest"),
            (self.page_digest, "pageDigest"),
            (self.transport_evidence_digest, "transportEvidenceDigest"),
            (self.mapping_evidence_digest, "mappingEvidenceDigest"),
        ):
            _digest(value, label)
        if type(self.read_exchange_evidence) is not NativeIMInboundReadExchangeEvidenceV1:
            raise TypeError("sandbox exchange provenance requires exact exchange evidence")
        if (
            self.read_exchange_evidence.read_request_digest != self.read_request_digest
            or self.read_exchange_evidence.event_source_evidence_digest
            != self.transport_evidence_digest
        ):
            raise ValueError("sandbox provenance does not bind its read exchange evidence")
        self.canonical_bytes()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "approvalDigest": self.approval_digest,
            "approvalId": self.approval_id,
            "authorityRevision": self.authority_revision,
            "configurationBindingDigest": self.configuration_binding_digest,
            "mapperContractDigest": self.mapper_contract_digest,
            "mapperContractId": self.mapper_contract_id,
            "mappingEvidenceDigest": self.mapping_evidence_digest,
            "pageDigest": self.page_digest,
            "profileDigest": self.profile_digest,
            "profileId": self.profile_id,
            "profileRevision": self.profile_revision,
            "providerManifestDigest": self.provider_manifest_digest,
            "readExchangeEvidence": self.read_exchange_evidence.to_dict(),
            "readRequestDigest": self.read_request_digest,
            "schemaVersion": self.schema_version,
            "transportContractDigest": self.transport_contract_digest,
            "transportContractId": self.transport_contract_id,
            "transportEvidenceDigest": self.transport_evidence_digest,
        }

    @classmethod
    def from_dict(
        cls,
        value: object,
    ) -> NativeIMSandboxExchangeAdmissionProvenanceV1:
        if cls is not NativeIMSandboxExchangeAdmissionProvenanceV1:
            raise TypeError("sandbox exchange provenance decoder requires the exact V1 class")
        body = _plain_dict(
            value,
            _EXCHANGE_PROVENANCE_FIELDS,
            "native IM sandbox exchange admission provenance",
        )
        return cls(
            schema_version=body["schemaVersion"],
            approval_id=body["approvalId"],
            authority_revision=body["authorityRevision"],
            approval_digest=body["approvalDigest"],
            configuration_binding_digest=body["configurationBindingDigest"],
            profile_id=body["profileId"],
            profile_revision=body["profileRevision"],
            profile_digest=body["profileDigest"],
            provider_manifest_digest=body["providerManifestDigest"],
            transport_contract_id=body["transportContractId"],
            transport_contract_digest=body["transportContractDigest"],
            mapper_contract_id=body["mapperContractId"],
            mapper_contract_digest=body["mapperContractDigest"],
            read_request_digest=body["readRequestDigest"],
            page_digest=body["pageDigest"],
            transport_evidence_digest=body["transportEvidenceDigest"],
            mapping_evidence_digest=body["mappingEvidenceDigest"],
            read_exchange_evidence=NativeIMInboundReadExchangeEvidenceV1.from_dict(
                body["readExchangeEvidence"]
            ),
        )

    @classmethod
    def from_json_bytes(
        cls,
        encoded: object,
    ) -> NativeIMSandboxExchangeAdmissionProvenanceV1:
        if cls is not NativeIMSandboxExchangeAdmissionProvenanceV1:
            raise TypeError("sandbox exchange provenance decoder requires the exact V1 class")
        return cls.from_dict(
            _decode_json_bytes(
                encoded,
                "native IM sandbox exchange admission provenance",
                maximum_bytes=_MAX_PROVENANCE_BYTES,
            )
        )

    def canonical_bytes(self) -> bytes:
        encoded = _canonical_json_bytes(self.to_dict())
        if len(encoded) > _MAX_PROVENANCE_BYTES:
            raise NativeIMCodecTooLargeError(
                "sandbox exchange admission provenance exceeds its byte limit"
            )
        return encoded

    def canonical_digest(self) -> str:
        return _model_digest(self._MODEL_NAME, self.to_dict())

    def __repr__(self) -> str:
        return (
            "NativeIMSandboxExchangeAdmissionProvenanceV1("
            f"revision={self.authority_revision}, digest={self.canonical_digest()[:12]!r})"
        )


NativeIMSandboxAdmissionProvenance = Union[
    NativeIMSandboxAdmissionProvenanceV1,
    NativeIMSandboxExchangeAdmissionProvenanceV1,
]


def decode_native_im_sandbox_admission_provenance_v1(
    encoded: object,
) -> NativeIMSandboxAdmissionProvenance:
    """Decode either canonical legacy or exchange-enhanced admission provenance."""

    decoded = _decode_json_bytes(
        encoded,
        "native IM sandbox admission provenance",
        maximum_bytes=_MAX_PROVENANCE_BYTES,
    )
    if type(decoded) is not dict:
        raise TypeError("native IM sandbox admission provenance must be a plain object")
    body = cast(Dict[str, Any], decoded)
    fields = set(body)
    if fields == _EXCHANGE_PROVENANCE_FIELDS:
        return NativeIMSandboxExchangeAdmissionProvenanceV1.from_dict(body)
    if fields == _PROVENANCE_FIELDS:
        return NativeIMSandboxAdmissionProvenanceV1.from_dict(body)
    raise ValueError("native IM sandbox admission provenance fields do not match")


__all__ = [
    "NativeIMSandboxAdmissionProvenance",
    "NativeIMSandboxAdmissionProvenanceV1",
    "NativeIMSandboxExchangeAdmissionProvenanceV1",
    "decode_native_im_sandbox_admission_provenance_v1",
]
