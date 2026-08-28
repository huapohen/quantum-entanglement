# ruff: noqa: UP006, UP035
"""Canonical approval facts for the native-IM provider sandbox.

An approval record is inert data. Decoding or constructing one never grants connection,
secret, transport, mapping, or admission authority. A trusted authority must independently
load, verify, activate, and continuously re-check the record before it can be used.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Dict, SupportsIndex, Tuple, cast

from ._native_im_codec import (
    NATIVE_IM_SCHEMA_VERSION,
    NativeIMCodecTooLargeError,
    _canonical_json_bytes,
    _decode_json_bytes,
    _digest,
    _enum,
    _id,
    _model_digest,
    _ordered_unique_text,
    _plain_dict,
    _plain_list,
    _positive_integer,
    _schema_version,
    _timestamp,
)
from .native_im_provider_profile import IMProviderProfileV1
from .service.native_im_config import (
    CanonicalAbsolutePath,
    CanonicalHTTPSOrigin,
    NativeIMInboundOnlyConfigV1,
    parse_approved_ip_addresses,
)
from .service.secrets import SecretRef

_MAX_APPROVAL_BYTES = 128 * 1_024
_MAX_ALLOWED_CONVERSATIONS = 128
_APPROVAL_FIELDS = {
    "allowedConversationIds",
    "allowedOperations",
    "approvalId",
    "approvedAddresses",
    "authorityKeyId",
    "authorityRevision",
    "channelId",
    "configurationBindingDigest",
    "connectTimeoutMs",
    "credentialMode",
    "credentialRefBindingDigest",
    "dataClassification",
    "deploymentSubjectDigest",
    "environmentClass",
    "expiresAt",
    "healthMethod",
    "healthPath",
    "issuedAt",
    "issuerId",
    "killSwitchId",
    "mapperContractDigest",
    "mapperContractId",
    "maxResponseBytes",
    "notBefore",
    "operatorId",
    "origin",
    "outboundMode",
    "pageLimit",
    "profileDigest",
    "profileId",
    "profileRevision",
    "provider",
    "rateLimitWindowSeconds",
    "readMethod",
    "readPath",
    "readTimeoutMs",
    "redirectMode",
    "revocationId",
    "reviewerId",
    "rollbackPolicyId",
    "schemaVersion",
    "sourceEvidenceDigest",
    "status",
    "tenantId",
    "transportContractDigest",
    "transportContractId",
    "verificationKeyId",
    "verificationSecretRefBindingDigest",
    "workspaceId",
    "requestsPerWindow",
}
_APPROVAL_STATUSES = {"approved"}
_ENVIRONMENT_CLASSES = {"sandbox"}
_DATA_CLASSIFICATIONS = {"synthetic_non_sensitive"}
_HTTP_METHODS = {"GET"}
_CREDENTIAL_MODES = {"reference_only"}
_OUTBOUND_MODES = {"disabled"}
_REDIRECT_MODES = {"deny"}
_ALLOWED_OPERATIONS = ("health", "read")


class NativeIMSandboxApprovalBindingError(ValueError):
    """A trusted approval fact does not exactly bind the requested deployment."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _bounded_positive(value: object, label: str, maximum: int) -> int:
    result = _positive_integer(value, label)
    if result > maximum:
        raise ValueError(f"{label} exceeds the sandbox approval limit")
    return result


def native_im_secret_reference_binding_digest(reference: SecretRef, *, purpose: str) -> str:
    """Bind one exact secret routing reference without rendering it in the record."""

    if type(reference) is not SecretRef:
        raise TypeError("secret binding requires the exact SecretRef class")
    if type(purpose) is not str or purpose not in {"read_credential", "verification_secret"}:
        raise ValueError("secret binding purpose is unsupported")
    return _model_digest(
        "NativeIMSandboxSecretReferenceBindingV1",
        {"purpose": purpose, "reference": reference.canonical},
    )


@dataclass(frozen=True, repr=False)
class NativeIMSandboxApprovalV1:
    """One exact, persistable approval fact that remains inert until trusted activation."""

    schema_version: int
    approval_id: str
    authority_revision: int
    status: str
    issued_at: str
    not_before: str
    expires_at: str
    issuer_id: str
    reviewer_id: str
    operator_id: str
    authority_key_id: str
    environment_class: str
    data_classification: str
    provider: str
    tenant_id: str
    workspace_id: str
    channel_id: str
    allowed_conversation_ids: Tuple[str, ...]
    profile_id: str
    profile_revision: str
    profile_digest: str = field(repr=False)
    configuration_binding_digest: str = field(repr=False)
    deployment_subject_digest: str = field(repr=False)
    origin: str = field(repr=False)
    approved_addresses: Tuple[str, ...] = field(repr=False)
    health_method: str
    health_path: str = field(repr=False)
    read_method: str
    read_path: str = field(repr=False)
    credential_ref_binding_digest: str = field(repr=False)
    verification_secret_ref_binding_digest: str = field(repr=False)
    verification_key_id: str
    page_limit: int
    max_response_bytes: int
    connect_timeout_ms: int
    read_timeout_ms: int
    requests_per_window: int
    rate_limit_window_seconds: int
    allowed_operations: Tuple[str, ...]
    outbound_mode: str
    redirect_mode: str
    credential_mode: str
    transport_contract_id: str
    transport_contract_digest: str = field(repr=False)
    mapper_contract_id: str
    mapper_contract_digest: str = field(repr=False)
    revocation_id: str
    kill_switch_id: str
    rollback_policy_id: str
    source_evidence_digest: str = field(repr=False)

    _MODEL_NAME: ClassVar[str] = "NativeIMSandboxApprovalV1"

    def __post_init__(self) -> None:
        if type(self) is not NativeIMSandboxApprovalV1:
            raise TypeError("approval requires the exact native-IM sandbox V1 class")
        _schema_version(self.schema_version)
        _bounded_positive(self.authority_revision, "authorityRevision", (1 << 63) - 1)
        for value, label in (
            (self.approval_id, "approvalId"),
            (self.issuer_id, "issuerId"),
            (self.reviewer_id, "reviewerId"),
            (self.operator_id, "operatorId"),
            (self.authority_key_id, "authorityKeyId"),
            (self.provider, "provider"),
            (self.tenant_id, "tenantId"),
            (self.workspace_id, "workspaceId"),
            (self.channel_id, "channelId"),
            (self.profile_id, "profileId"),
            (self.profile_revision, "profileRevision"),
            (self.verification_key_id, "verificationKeyId"),
            (self.transport_contract_id, "transportContractId"),
            (self.mapper_contract_id, "mapperContractId"),
            (self.revocation_id, "revocationId"),
            (self.kill_switch_id, "killSwitchId"),
            (self.rollback_policy_id, "rollbackPolicyId"),
        ):
            _id(value, label)
        _enum(self.status, _APPROVAL_STATUSES, "status")
        _timestamp(self.issued_at, "issuedAt")
        _timestamp(self.not_before, "notBefore")
        _timestamp(self.expires_at, "expiresAt")
        if not self.issued_at <= self.not_before < self.expires_at:
            raise ValueError("approval time interval is not canonical")
        _enum(self.environment_class, _ENVIRONMENT_CLASSES, "environmentClass")
        _enum(self.data_classification, _DATA_CLASSIFICATIONS, "dataClassification")
        for value, label in (
            (self.profile_digest, "profileDigest"),
            (self.configuration_binding_digest, "configurationBindingDigest"),
            (self.deployment_subject_digest, "deploymentSubjectDigest"),
            (self.credential_ref_binding_digest, "credentialRefBindingDigest"),
            (
                self.verification_secret_ref_binding_digest,
                "verificationSecretRefBindingDigest",
            ),
            (self.transport_contract_digest, "transportContractDigest"),
            (self.mapper_contract_digest, "mapperContractDigest"),
            (self.source_evidence_digest, "sourceEvidenceDigest"),
        ):
            _digest(value, label)
        if type(self.allowed_conversation_ids) is not tuple:
            raise TypeError("allowedConversationIds must be an immutable tuple")
        if not self.allowed_conversation_ids:
            raise ValueError("allowedConversationIds must be non-empty")
        if len(self.allowed_conversation_ids) > _MAX_ALLOWED_CONVERSATIONS:
            raise NativeIMCodecTooLargeError("allowedConversationIds exceeds its item limit")
        conversations = tuple(
            _id(item, f"allowedConversationIds[{index}]")
            for index, item in enumerate(self.allowed_conversation_ids)
        )
        _ordered_unique_text(conversations, "allowedConversationIds")
        if type(self.approved_addresses) is not tuple:
            raise TypeError("approvedAddresses must be an immutable tuple")
        addresses = parse_approved_ip_addresses(",".join(self.approved_addresses))
        if tuple(address.compressed for address in addresses) != self.approved_addresses:
            raise ValueError("approvedAddresses must be the exact canonical pin set")
        if CanonicalHTTPSOrigin.parse(self.origin).canonical != self.origin:
            raise ValueError("origin must be canonical")
        if CanonicalAbsolutePath.parse(self.health_path).canonical != self.health_path:
            raise ValueError("healthPath must be canonical")
        if CanonicalAbsolutePath.parse(self.read_path).canonical != self.read_path:
            raise ValueError("readPath must be canonical")
        if self.health_path == self.read_path:
            raise ValueError("healthPath and readPath must be distinct")
        _enum(self.health_method, _HTTP_METHODS, "healthMethod")
        _enum(self.read_method, _HTTP_METHODS, "readMethod")
        _bounded_positive(self.page_limit, "pageLimit", 1_000)
        _bounded_positive(self.max_response_bytes, "maxResponseBytes", 16 * 1_024 * 1_024)
        if self.max_response_bytes < 1_024:
            raise ValueError("maxResponseBytes is below the sandbox approval minimum")
        _bounded_positive(self.connect_timeout_ms, "connectTimeoutMs", 30_000)
        if self.connect_timeout_ms < 100:
            raise ValueError("connectTimeoutMs is below the sandbox approval minimum")
        _bounded_positive(self.read_timeout_ms, "readTimeoutMs", 120_000)
        if self.read_timeout_ms < 100:
            raise ValueError("readTimeoutMs is below the sandbox approval minimum")
        _bounded_positive(self.requests_per_window, "requestsPerWindow", 10_000)
        _bounded_positive(self.rate_limit_window_seconds, "rateLimitWindowSeconds", 86_400)
        if type(self.allowed_operations) is not tuple:
            raise TypeError("allowedOperations must be an immutable tuple")
        operations = tuple(
            _id(item, f"allowedOperations[{index}]")
            for index, item in enumerate(self.allowed_operations)
        )
        if operations != _ALLOWED_OPERATIONS:
            raise ValueError("allowedOperations must be the exact inbound sandbox set")
        _enum(self.outbound_mode, _OUTBOUND_MODES, "outboundMode")
        _enum(self.redirect_mode, _REDIRECT_MODES, "redirectMode")
        _enum(self.credential_mode, _CREDENTIAL_MODES, "credentialMode")
        self.canonical_bytes()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowedConversationIds": list(self.allowed_conversation_ids),
            "allowedOperations": list(self.allowed_operations),
            "approvalId": self.approval_id,
            "approvedAddresses": list(self.approved_addresses),
            "authorityKeyId": self.authority_key_id,
            "authorityRevision": self.authority_revision,
            "channelId": self.channel_id,
            "configurationBindingDigest": self.configuration_binding_digest,
            "connectTimeoutMs": self.connect_timeout_ms,
            "credentialMode": self.credential_mode,
            "credentialRefBindingDigest": self.credential_ref_binding_digest,
            "dataClassification": self.data_classification,
            "deploymentSubjectDigest": self.deployment_subject_digest,
            "environmentClass": self.environment_class,
            "expiresAt": self.expires_at,
            "healthMethod": self.health_method,
            "healthPath": self.health_path,
            "issuedAt": self.issued_at,
            "issuerId": self.issuer_id,
            "killSwitchId": self.kill_switch_id,
            "mapperContractDigest": self.mapper_contract_digest,
            "mapperContractId": self.mapper_contract_id,
            "maxResponseBytes": self.max_response_bytes,
            "notBefore": self.not_before,
            "operatorId": self.operator_id,
            "origin": self.origin,
            "outboundMode": self.outbound_mode,
            "pageLimit": self.page_limit,
            "profileDigest": self.profile_digest,
            "profileId": self.profile_id,
            "profileRevision": self.profile_revision,
            "provider": self.provider,
            "rateLimitWindowSeconds": self.rate_limit_window_seconds,
            "readMethod": self.read_method,
            "readPath": self.read_path,
            "readTimeoutMs": self.read_timeout_ms,
            "redirectMode": self.redirect_mode,
            "requestsPerWindow": self.requests_per_window,
            "revocationId": self.revocation_id,
            "reviewerId": self.reviewer_id,
            "rollbackPolicyId": self.rollback_policy_id,
            "schemaVersion": self.schema_version,
            "sourceEvidenceDigest": self.source_evidence_digest,
            "status": self.status,
            "tenantId": self.tenant_id,
            "transportContractDigest": self.transport_contract_digest,
            "transportContractId": self.transport_contract_id,
            "verificationKeyId": self.verification_key_id,
            "verificationSecretRefBindingDigest": (
                self.verification_secret_ref_binding_digest
            ),
            "workspaceId": self.workspace_id,
        }

    @classmethod
    def from_dict(cls, value: object) -> NativeIMSandboxApprovalV1:
        if cls is not NativeIMSandboxApprovalV1:
            raise TypeError("approval decoder requires the exact native-IM sandbox V1 class")
        body = _plain_dict(value, _APPROVAL_FIELDS, "native IM sandbox approval")
        conversations = _plain_list(
            body["allowedConversationIds"],
            "allowedConversationIds",
            maximum_items=_MAX_ALLOWED_CONVERSATIONS,
        )
        operations = _plain_list(
            body["allowedOperations"],
            "allowedOperations",
            maximum_items=len(_ALLOWED_OPERATIONS),
        )
        addresses = _plain_list(
            body["approvedAddresses"],
            "approvedAddresses",
            maximum_items=32,
        )
        return cls(
            schema_version=body["schemaVersion"],
            approval_id=body["approvalId"],
            authority_revision=body["authorityRevision"],
            status=body["status"],
            issued_at=body["issuedAt"],
            not_before=body["notBefore"],
            expires_at=body["expiresAt"],
            issuer_id=body["issuerId"],
            reviewer_id=body["reviewerId"],
            operator_id=body["operatorId"],
            authority_key_id=body["authorityKeyId"],
            environment_class=body["environmentClass"],
            data_classification=body["dataClassification"],
            provider=body["provider"],
            tenant_id=body["tenantId"],
            workspace_id=body["workspaceId"],
            channel_id=body["channelId"],
            allowed_conversation_ids=tuple(cast(list[str], conversations)),
            profile_id=body["profileId"],
            profile_revision=body["profileRevision"],
            profile_digest=body["profileDigest"],
            configuration_binding_digest=body["configurationBindingDigest"],
            deployment_subject_digest=body["deploymentSubjectDigest"],
            origin=body["origin"],
            approved_addresses=tuple(cast(list[str], addresses)),
            health_method=body["healthMethod"],
            health_path=body["healthPath"],
            read_method=body["readMethod"],
            read_path=body["readPath"],
            credential_ref_binding_digest=body["credentialRefBindingDigest"],
            verification_secret_ref_binding_digest=(
                body["verificationSecretRefBindingDigest"]
            ),
            verification_key_id=body["verificationKeyId"],
            page_limit=body["pageLimit"],
            max_response_bytes=body["maxResponseBytes"],
            connect_timeout_ms=body["connectTimeoutMs"],
            read_timeout_ms=body["readTimeoutMs"],
            requests_per_window=body["requestsPerWindow"],
            rate_limit_window_seconds=body["rateLimitWindowSeconds"],
            allowed_operations=tuple(cast(list[str], operations)),
            outbound_mode=body["outboundMode"],
            redirect_mode=body["redirectMode"],
            credential_mode=body["credentialMode"],
            transport_contract_id=body["transportContractId"],
            transport_contract_digest=body["transportContractDigest"],
            mapper_contract_id=body["mapperContractId"],
            mapper_contract_digest=body["mapperContractDigest"],
            revocation_id=body["revocationId"],
            kill_switch_id=body["killSwitchId"],
            rollback_policy_id=body["rollbackPolicyId"],
            source_evidence_digest=body["sourceEvidenceDigest"],
        )

    @classmethod
    def from_json_bytes(cls, encoded: object) -> NativeIMSandboxApprovalV1:
        if cls is not NativeIMSandboxApprovalV1:
            raise TypeError("approval decoder requires the exact native-IM sandbox V1 class")
        decoded = _decode_json_bytes(
            encoded,
            "native IM sandbox approval",
            maximum_bytes=_MAX_APPROVAL_BYTES,
        )
        return cls.from_dict(decoded)

    def canonical_bytes(self) -> bytes:
        encoded = _canonical_json_bytes(self.to_dict())
        if len(encoded) > _MAX_APPROVAL_BYTES:
            raise NativeIMCodecTooLargeError("native IM sandbox approval exceeds its byte limit")
        return encoded

    def canonical_digest(self) -> str:
        self.canonical_bytes()
        return _model_digest(self._MODEL_NAME, self.to_dict())

    def __str__(self) -> str:
        return f"NativeIMSandboxApprovalV1<{self.canonical_digest()[:12]}>"

    def __repr__(self) -> str:
        return f"NativeIMSandboxApprovalV1(digest={self.canonical_digest()[:12]!r})"

    def __reduce_ex__(self, protocol: SupportsIndex) -> tuple[object, tuple[bytes]]:
        # Persisting the inert record is allowed. Unpickling still runs strict JSON decode;
        # it does not create the process-local authority introduced by the next layer.
        return (NativeIMSandboxApprovalV1.from_json_bytes, (self.canonical_bytes(),))


def validate_native_im_sandbox_approval_binding_v1(
    approval: NativeIMSandboxApprovalV1,
    configuration: NativeIMInboundOnlyConfigV1,
    profile: IMProviderProfileV1,
) -> None:
    """Validate static exact binding only; this function does not activate authority."""

    if type(approval) is not NativeIMSandboxApprovalV1:
        raise TypeError("approval binding requires the exact sandbox approval V1 class")
    if type(configuration) is not NativeIMInboundOnlyConfigV1:
        raise TypeError("approval binding requires the exact inbound-only configuration")
    if type(profile) is not IMProviderProfileV1:
        raise TypeError("approval binding requires the exact provider profile V1 class")
    expected_secret_bindings = (
        native_im_secret_reference_binding_digest(
            configuration.credential_ref,
            purpose="read_credential",
        ),
        native_im_secret_reference_binding_digest(
            configuration.verification_secret_ref,
            purpose="verification_secret",
        ),
    )
    approval_secret_bindings = (
        approval.credential_ref_binding_digest,
        approval.verification_secret_ref_binding_digest,
    )
    expected = (
        configuration.approval_id,
        configuration.authority_revision,
        configuration.approval_expires_at,
        configuration.deployment_subject_digest,
        configuration.provider,
        configuration.tenant_id,
        configuration.workspace_id,
        configuration.channel_id,
        profile.profile_id,
        profile.revision,
        profile.canonical_digest(),
        configuration.approval_binding_digest,
        configuration.origin.canonical,
        tuple(address.compressed for address in configuration.approved_addresses),
        configuration.health_path.canonical,
        configuration.read_path.canonical,
        configuration.verification_key_id,
        configuration.page_limit,
        configuration.max_response_bytes,
        configuration.connect_timeout_ms,
        configuration.read_timeout_ms,
        configuration.outbound_mode,
        configuration.redirect_mode,
        profile.allowed_conversation_ids,
        profile.limits.requests_per_window,
        profile.limits.rate_limit_window_seconds,
    )
    actual = (
        approval.approval_id,
        approval.authority_revision,
        approval.expires_at,
        approval.deployment_subject_digest,
        approval.provider,
        approval.tenant_id,
        approval.workspace_id,
        approval.channel_id,
        approval.profile_id,
        approval.profile_revision,
        approval.profile_digest,
        approval.configuration_binding_digest,
        approval.origin,
        approval.approved_addresses,
        approval.health_path,
        approval.read_path,
        approval.verification_key_id,
        approval.page_limit,
        approval.max_response_bytes,
        approval.connect_timeout_ms,
        approval.read_timeout_ms,
        approval.outbound_mode,
        approval.redirect_mode,
        approval.allowed_conversation_ids,
        approval.requests_per_window,
        approval.rate_limit_window_seconds,
    )
    if (
        actual != expected
        or approval_secret_bindings != expected_secret_bindings
        or approval.canonical_digest() != configuration.approval_digest
    ):
        raise NativeIMSandboxApprovalBindingError(
            "native_im_sandbox_approval_binding_mismatch"
        ) from None


__all__ = [
    "NATIVE_IM_SCHEMA_VERSION",
    "NativeIMSandboxApprovalBindingError",
    "NativeIMSandboxApprovalV1",
    "native_im_secret_reference_binding_digest",
    "validate_native_im_sandbox_approval_binding_v1",
]
