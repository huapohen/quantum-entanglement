from __future__ import annotations

import json
import pickle
from dataclasses import replace

import pytest

from quantum_entanglement._native_im_codec import NativeIMCodecTooLargeError
from quantum_entanglement.native_im_sandbox_approval import (
    NativeIMSandboxApprovalBindingError,
    NativeIMSandboxApprovalV1,
    native_im_secret_reference_binding_digest,
    validate_native_im_sandbox_approval_binding_v1,
)
from quantum_entanglement.service.native_im_config import (
    CanonicalAbsolutePath,
    CanonicalHTTPSOrigin,
    parse_approved_ip_addresses,
)
from quantum_entanglement.service.secrets import SecretRef
from tests.test_native_im_provider_profile import profile
from tests.test_native_im_sandbox_config import bound_configuration


def sandbox_approval(**changes: object) -> NativeIMSandboxApprovalV1:
    configuration = bound_configuration()
    provider_profile = profile()
    values: dict[str, object] = {
        "schema_version": 1,
        "approval_id": configuration.approval_id,
        "authority_revision": 7,
        "status": "approved",
        "issued_at": "2026-08-28T08:00:00.000001Z",
        "not_before": "2026-08-28T08:05:00.000001Z",
        "expires_at": configuration.approval_expires_at,
        "issuer_id": "test-security-issuer",
        "reviewer_id": "test-security-reviewer",
        "operator_id": "test-sandbox-operator",
        "authority_key_id": "test-approval-authority-key",
        "environment_class": "sandbox",
        "data_classification": "synthetic_non_sensitive",
        "provider": configuration.provider,
        "tenant_id": configuration.tenant_id,
        "workspace_id": configuration.workspace_id,
        "channel_id": configuration.channel_id,
        "allowed_conversation_ids": provider_profile.allowed_conversation_ids,
        "profile_id": provider_profile.profile_id,
        "profile_revision": provider_profile.revision,
        "profile_digest": provider_profile.canonical_digest(),
        "configuration_binding_digest": configuration.approval_binding_digest,
        "deployment_subject_digest": "1" * 64,
        "origin": configuration.origin.canonical,
        "approved_addresses": tuple(
            address.compressed for address in configuration.approved_addresses
        ),
        "health_method": "GET",
        "health_path": configuration.health_path.canonical,
        "read_method": "GET",
        "read_path": configuration.read_path.canonical,
        "credential_ref_binding_digest": native_im_secret_reference_binding_digest(
            configuration.credential_ref,
            purpose="read_credential",
        ),
        "verification_secret_ref_binding_digest": (
            native_im_secret_reference_binding_digest(
                configuration.verification_secret_ref,
                purpose="verification_secret",
            )
        ),
        "verification_key_id": configuration.verification_key_id,
        "page_limit": configuration.page_limit,
        "max_response_bytes": configuration.max_response_bytes,
        "connect_timeout_ms": configuration.connect_timeout_ms,
        "read_timeout_ms": configuration.read_timeout_ms,
        "requests_per_window": provider_profile.limits.requests_per_window,
        "rate_limit_window_seconds": provider_profile.limits.rate_limit_window_seconds,
        "allowed_operations": ("health", "read"),
        "outbound_mode": "disabled",
        "redirect_mode": "deny",
        "credential_mode": "reference_only",
        "transport_contract_id": "test-native-im-transport-v1",
        "transport_contract_digest": "2" * 64,
        "mapper_contract_id": "test-native-im-mapper-v1",
        "mapper_contract_digest": "3" * 64,
        "revocation_id": "test-native-im-revocation",
        "kill_switch_id": "test-native-im-kill-switch",
        "rollback_policy_id": "test-native-im-rollback-policy",
        "source_evidence_digest": "4" * 64,
    }
    values.update(changes)
    return NativeIMSandboxApprovalV1(**values)  # type: ignore[arg-type]


def test_approval_round_trip_and_domain_separated_digest_are_stable() -> None:
    approval = sandbox_approval()
    encoded = approval.canonical_bytes()

    assert NativeIMSandboxApprovalV1.from_dict(approval.to_dict()) == approval
    assert NativeIMSandboxApprovalV1.from_json_bytes(encoded) == approval
    assert pickle.loads(pickle.dumps(approval)) == approval
    assert encoded == json.dumps(
        approval.to_dict(),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    assert approval.canonical_digest() == (
        "9b5f0dca5e3933aeef5344a74d3345d5a9e2fd421d3e935e5a9bad1fa196b147"
    )


def test_approval_is_inert_frozen_and_strictly_exact_typed() -> None:
    approval = sandbox_approval()
    with pytest.raises(AttributeError):
        approval.status = "revoked"  # type: ignore[misc]

    class ApprovalSubclass(NativeIMSandboxApprovalV1):
        pass

    with pytest.raises(TypeError):
        ApprovalSubclass(**approval.__dict__)
    with pytest.raises(TypeError):
        ApprovalSubclass.from_dict(approval.to_dict())

    class DictSubclass(dict[str, object]):
        pass

    with pytest.raises(TypeError):
        NativeIMSandboxApprovalV1.from_dict(DictSubclass(approval.to_dict()))
    with pytest.raises(TypeError):
        sandbox_approval(allowed_conversation_ids=["test-conversation-a"])
    with pytest.raises(TypeError):
        sandbox_approval(approved_addresses=["8.8.8.8"])
    with pytest.raises(TypeError):
        sandbox_approval(allowed_operations=["health", "read"])


def test_approval_decoder_rejects_missing_unknown_duplicate_and_oversized_input() -> None:
    approval = sandbox_approval()
    wire = approval.to_dict()
    missing = dict(wire)
    del missing["approvalId"]
    unknown = {**wire, "futureAuthority": "forbidden"}

    with pytest.raises(ValueError):
        NativeIMSandboxApprovalV1.from_dict(missing)
    with pytest.raises(ValueError):
        NativeIMSandboxApprovalV1.from_dict(unknown)
    duplicate = b'{"approvalId":"duplicate",' + approval.canonical_bytes()[1:]
    with pytest.raises(ValueError):
        NativeIMSandboxApprovalV1.from_json_bytes(duplicate)
    with pytest.raises(NativeIMCodecTooLargeError):
        NativeIMSandboxApprovalV1.from_json_bytes(b" " * (128 * 1_024 + 1))


@pytest.mark.parametrize(
    ("changes", "error_type"),
    (
        ({"authority_revision": True}, TypeError),
        ({"authority_revision": 0}, ValueError),
        ({"status": "draft"}, ValueError),
        ({"environment_class": "production"}, ValueError),
        ({"data_classification": "sensitive"}, ValueError),
        ({"not_before": "2026-09-28T00:00:00.000001Z"}, ValueError),
        ({"expires_at": "2026-08-28T08:05:00.000001Z"}, ValueError),
        ({"allowed_conversation_ids": ()}, ValueError),
        (
            {"allowed_conversation_ids": ("test-conversation-b", "test-conversation-a")},
            ValueError,
        ),
        (
            {"allowed_conversation_ids": ("test-conversation-a", "test-conversation-a")},
            ValueError,
        ),
        ({"origin": "http://sandbox.im.example.com:443"}, ValueError),
        ({"approved_addresses": ("127.0.0.1",)}, ValueError),
        ({"health_method": "POST"}, ValueError),
        ({"read_method": "POST"}, ValueError),
        ({"read_path": "/v1/health"}, ValueError),
        ({"allowed_operations": ("read",)}, ValueError),
        ({"allowed_operations": ("health", "read", "send_message")}, ValueError),
        ({"outbound_mode": "enabled"}, ValueError),
        ({"redirect_mode": "follow"}, ValueError),
        ({"credential_mode": "ambient"}, ValueError),
        ({"page_limit": 1_001}, ValueError),
        ({"max_response_bytes": 1_023}, ValueError),
        ({"connect_timeout_ms": 99}, ValueError),
        ({"read_timeout_ms": 120_001}, ValueError),
        ({"requests_per_window": 10_001}, ValueError),
        ({"rate_limit_window_seconds": 86_401}, ValueError),
    ),
)
def test_approval_contract_rejects_authority_expansion_and_aliases(
    changes: dict[str, object],
    error_type: type[BaseException],
) -> None:
    with pytest.raises(error_type):
        sandbox_approval(**changes)


def test_approval_repr_and_errors_redact_endpoint_refs_evidence_and_people() -> None:
    approval = sandbox_approval()
    rendered = f"{approval!r} {approval}"
    for hidden in (
        approval.origin,
        approval.health_path,
        approval.read_path,
        approval.issuer_id,
        approval.reviewer_id,
        approval.operator_id,
        approval.source_evidence_digest,
        approval.configuration_binding_digest,
        approval.credential_ref_binding_digest,
    ):
        assert hidden not in rendered

    canary = "approval-value-canary-must-not-render"
    with pytest.raises(ValueError) as raised:
        sandbox_approval(status=canary)
    assert canary not in f"{raised.value!r} {raised.value}"


def test_secret_reference_bindings_are_full_purpose_separated_and_redacted() -> None:
    reference = SecretRef.parse("file://native-im-secret-canary")
    read_digest = native_im_secret_reference_binding_digest(
        reference,
        purpose="read_credential",
    )
    verification_digest = native_im_secret_reference_binding_digest(
        reference,
        purpose="verification_secret",
    )

    assert len(read_digest) == 64
    assert read_digest != verification_digest
    assert reference.locator not in read_digest
    with pytest.raises(ValueError):
        native_im_secret_reference_binding_digest(reference, purpose="outbound")


def test_static_approval_binding_matches_exact_config_and_profile_without_activating_it() -> None:
    validate_native_im_sandbox_approval_binding_v1(
        sandbox_approval(),
        bound_configuration(),
        profile(),
    )


@pytest.mark.parametrize(
    ("field_name", "changed"),
    (
        ("approval_id", "other-approval"),
        ("expires_at", "2026-09-29T00:00:00.000001Z"),
        ("provider", "other-provider"),
        ("tenant_id", "other-tenant"),
        ("workspace_id", "other-workspace"),
        ("channel_id", "other-channel"),
        ("profile_id", "other-profile"),
        ("profile_revision", "other-revision"),
        ("profile_digest", "5" * 64),
        ("configuration_binding_digest", "6" * 64),
        ("origin", "https://other.im.example.com:443"),
        ("approved_addresses", ("1.1.1.1", "8.8.8.8")),
        ("health_path", "/v2/health"),
        ("read_path", "/v2/inbound-events"),
        ("credential_ref_binding_digest", "7" * 64),
        ("verification_secret_ref_binding_digest", "8" * 64),
        ("verification_key_id", "other-key"),
        ("page_limit", 99),
        ("max_response_bytes", 8_388_607),
        ("connect_timeout_ms", 4_999),
        ("read_timeout_ms", 29_999),
        ("requests_per_window", 99),
        ("rate_limit_window_seconds", 59),
    ),
)
def test_static_approval_binding_rejects_every_config_profile_and_secret_drift(
    field_name: str,
    changed: object,
) -> None:
    with pytest.raises(NativeIMSandboxApprovalBindingError) as raised:
        validate_native_im_sandbox_approval_binding_v1(
            sandbox_approval(**{field_name: changed}),
            bound_configuration(),
            profile(),
        )
    assert raised.value.code == "native_im_sandbox_approval_binding_mismatch"
    assert str(changed) not in str(raised.value)


def test_static_binding_rejects_profile_conversation_drift_even_with_new_profile_digest() -> None:
    changed_profile = replace(
        profile(),
        allowed_conversation_ids=("test-conversation-a",),
    )
    approval = sandbox_approval(profile_digest=changed_profile.canonical_digest())
    with pytest.raises(NativeIMSandboxApprovalBindingError):
        validate_native_im_sandbox_approval_binding_v1(
            approval,
            bound_configuration(),
            changed_profile,
        )


def test_static_binding_rejects_subclasses_before_reading_hostile_fields() -> None:
    class ApprovalSubclass(NativeIMSandboxApprovalV1):
        pass

    class ConfigSubclass(type(bound_configuration())):
        pass

    class ProfileSubclass(type(profile())):
        pass

    with pytest.raises(TypeError):
        validate_native_im_sandbox_approval_binding_v1(
            object.__new__(ApprovalSubclass),
            bound_configuration(),
            profile(),
        )
    with pytest.raises(TypeError):
        validate_native_im_sandbox_approval_binding_v1(
            sandbox_approval(),
            object.__new__(ConfigSubclass),
            profile(),
        )
    with pytest.raises(TypeError):
        validate_native_im_sandbox_approval_binding_v1(
            sandbox_approval(),
            bound_configuration(),
            object.__new__(ProfileSubclass),
        )


def test_wire_endpoint_values_remain_canonical_under_direct_construction() -> None:
    approval = sandbox_approval(
        origin=CanonicalHTTPSOrigin.parse("https://sandbox.im.example.com:443").canonical,
        approved_addresses=tuple(
            address.compressed
            for address in parse_approved_ip_addresses("2001:4860:4860::8888,8.8.8.8")
        ),
        health_path=CanonicalAbsolutePath.parse("/v1/health").canonical,
        read_path=CanonicalAbsolutePath.parse("/v1/inbound-events").canonical,
    )
    assert NativeIMSandboxApprovalV1.from_json_bytes(approval.canonical_bytes()) == approval
