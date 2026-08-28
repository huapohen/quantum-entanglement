from __future__ import annotations

import hashlib
import hmac
from dataclasses import replace
from datetime import datetime

import pytest

from quantum_entanglement.native_im_auth import (
    NativeIMAuthenticationError,
    NativeIMDetachedSignatureV1,
    NativeIMHMACRawBodyVerifier,
    NativeIMRawVerificationResultV1,
)
from quantum_entanglement.native_im_provider_profile import IMProviderProfileV1
from quantum_entanglement.service.native_im_config import (
    CanonicalAbsolutePath,
    NativeIMInboundOnlyConfigV1,
)
from quantum_entanglement.service.secrets import SecretMaterial
from tests.test_native_im_provider_profile import profile
from tests.test_native_im_sandbox_config import bound_configuration

SCHEMA = 1
NOW = "2026-08-28T12:00:00.000000Z"
SIGNED_UNIX_SECONDS = str(int(datetime.fromisoformat(NOW[:-1] + "+00:00").timestamp()))
KEY = b"test-verification-secret"
BODY = b'{"events":[],"hasMore":false}'
SIGNATURE_DOMAIN = b"quantum-entanglement.native-im-inbound-signature/1\n"


class ReplayGuard:
    def __init__(self) -> None:
        self.claims: list[dict[str, object]] = []
        self.nonces: set[tuple[tuple[str, str, str, str], str, str]] = set()

    def claim(self, **values: object) -> bool:
        self.claims.append(values)
        identity = (
            values["scope"],
            values["key_id"],
            values["nonce_digest"],
        )
        assert type(identity[0]) is tuple
        typed_identity = (identity[0], str(identity[1]), str(identity[2]))
        if typed_identity in self.nonces:
            return False
        self.nonces.add(typed_identity)
        return True


def authentication_profile(**auth_changes: object) -> IMProviderProfileV1:
    baseline = profile()
    authentication = replace(
        baseline.authentication,
        verifier_contract_id="qe.native-im.hmac-sha256.v1",
        **auth_changes,
    )
    return replace(baseline, authentication=authentication)


def configuration_for(value: IMProviderProfileV1) -> NativeIMInboundOnlyConfigV1:
    return replace(
        bound_configuration(),
        profile_id=value.profile_id,
        profile_revision=value.revision,
        profile_digest=value.canonical_digest(),
        provider=value.provider,
        tenant_id=value.tenant_id,
        workspace_id=value.workspace_id,
        channel_id=value.channel_id,
    )


def signature_for(
    configuration: NativeIMInboundOnlyConfigV1,
    *,
    body: bytes = BODY,
    timestamp: str = SIGNED_UNIX_SECONDS,
    nonce: str = "test-nonce-1",
    key: bytes = KEY,
) -> str:
    body_digest = hashlib.sha256(body).hexdigest()
    signed = (
        SIGNATURE_DOMAIN
        + "\n".join(
            (
                "GET",
                configuration.origin.canonical,
                configuration.read_path.canonical,
                timestamp,
                nonce,
                body_digest,
            )
        ).encode()
    )
    return hmac.new(key, signed, hashlib.sha256).hexdigest()


def metadata_for(
    configuration: NativeIMInboundOnlyConfigV1,
    **changes: object,
) -> NativeIMDetachedSignatureV1:
    values: dict[str, object] = {
        "schema_version": SCHEMA,
        "timestamp": SIGNED_UNIX_SECONDS,
        "nonce": "test-nonce-1",
        "key_id": configuration.verification_key_id,
        "signature": signature_for(configuration),
    }
    values.update(changes)
    return NativeIMDetachedSignatureV1(**values)  # type: ignore[arg-type]


def verifier_and_inputs() -> tuple[
    NativeIMHMACRawBodyVerifier,
    NativeIMInboundOnlyConfigV1,
    ReplayGuard,
]:
    value = authentication_profile()
    configuration = configuration_for(value)
    guard = ReplayGuard()
    return NativeIMHMACRawBodyVerifier(configuration, value, guard), configuration, guard


def test_valid_raw_body_is_verified_then_nonce_is_claimed_once() -> None:
    verifier, configuration, guard = verifier_and_inputs()
    material = SecretMaterial(KEY)
    retained_view = material.view()

    result = verifier.verify(
        metadata_for(configuration),
        BODY,
        material,
        now=NOW,
    )

    assert type(result) is NativeIMRawVerificationResultV1
    assert result.verifier_id == "qe.native-im.hmac-sha256.v1"
    assert result.key_id == configuration.verification_key_id
    assert result.body_digest == hashlib.sha256(BODY).hexdigest()
    assert result.signed_at == NOW
    assert result.expires_at == "2026-08-28T12:05:00.000000Z"
    assert result.verified_at == NOW
    assert len(result.nonce_digest) == 64
    assert len(result.authentication_evidence_digest) == 64
    assert len(guard.claims) == 1
    assert guard.claims[0]["scope"] == (
        "test-tenant",
        "test-workspace",
        "test-provider",
        "test-channel",
    )
    assert material.closed is True
    assert retained_view.tobytes() == bytes(len(retained_view))


def test_atomic_admission_verification_defers_nonce_claim_and_closes_material() -> None:
    verifier, configuration, guard = verifier_and_inputs()
    material = SecretMaterial(KEY)

    result = verifier.verify_for_atomic_admission(
        metadata_for(configuration),
        BODY,
        material,
        now=NOW,
    )

    assert type(result) is NativeIMRawVerificationResultV1
    assert result.expires_at == "2026-08-28T12:05:00.000000Z"
    assert guard.claims == []
    assert material.closed is True


def test_exact_replay_is_rejected_after_signature_verification() -> None:
    verifier, configuration, guard = verifier_and_inputs()
    metadata = metadata_for(configuration)
    verifier.verify(metadata, BODY, SecretMaterial(KEY), now=NOW)

    with pytest.raises(NativeIMAuthenticationError) as raised:
        verifier.verify(metadata, BODY, SecretMaterial(KEY), now=NOW)
    assert raised.value.code == "native_im_auth_nonce_replay"
    assert len(guard.claims) == 2


@pytest.mark.parametrize(
    ("metadata_changes", "body", "key", "expected_code"),
    (
        ({"signature": "0" * 64}, BODY, KEY, "native_im_auth_signature_invalid"),
        ({"key_id": "other-key"}, BODY, KEY, "native_im_auth_key_mismatch"),
        ({}, b'{"events":[1]}', KEY, "native_im_auth_signature_invalid"),
        ({}, BODY, b"wrong-secret", "native_im_auth_signature_invalid"),
        (
            {"timestamp": "0", "signature": "0" * 64},
            BODY,
            KEY,
            "native_im_auth_timestamp_expired",
        ),
    ),
)
def test_authentication_fails_closed_before_nonce_claim(
    metadata_changes: dict[str, object],
    body: bytes,
    key: bytes,
    expected_code: str,
) -> None:
    verifier, configuration, guard = verifier_and_inputs()
    material = SecretMaterial(key)
    with pytest.raises(NativeIMAuthenticationError) as raised:
        verifier.verify(
            metadata_for(configuration, **metadata_changes),
            body,
            material,
            now=NOW,
        )
    assert raised.value.code == expected_code
    assert material.closed is True
    assert guard.claims == []


def test_signature_binds_fixed_method_origin_path_timestamp_nonce_and_body() -> None:
    verifier, configuration, _ = verifier_and_inputs()
    original = metadata_for(configuration)
    for changed in (
        replace(configuration, read_path=CanonicalAbsolutePath.parse("/v1/alternate-read")),
        replace(configuration, origin=replace(configuration.origin, port=8443)),
    ):
        changed_profile = authentication_profile()
        changed = replace(changed, profile_digest=changed_profile.canonical_digest())
        changed_verifier = NativeIMHMACRawBodyVerifier(changed, changed_profile, ReplayGuard())
        with pytest.raises(NativeIMAuthenticationError) as raised:
            changed_verifier.verify(original, BODY, SecretMaterial(KEY), now=NOW)
        assert raised.value.code == "native_im_auth_signature_invalid"

    for nonce in ("test-nonce-other", "test-nonce-1-suffix"):
        with pytest.raises(NativeIMAuthenticationError) as raised:
            verifier.verify(
                replace(original, nonce=nonce),
                BODY,
                SecretMaterial(KEY),
                now=NOW,
            )
        assert raised.value.code == "native_im_auth_signature_invalid"


def test_canonical_utc_timestamp_mode_is_supported_without_aliases() -> None:
    value = authentication_profile(timestamp_mode="signed_canonical_utc")
    configuration = configuration_for(value)
    guard = ReplayGuard()
    verifier = NativeIMHMACRawBodyVerifier(configuration, value, guard)
    timestamp = NOW
    metadata = metadata_for(
        configuration,
        timestamp=timestamp,
        signature=signature_for(configuration, timestamp=timestamp),
    )
    result = verifier.verify(metadata, BODY, SecretMaterial(KEY), now=NOW)
    assert result.signed_at == NOW

    for invalid in ("2026-08-28T12:00:00Z", "2026-08-28T12:00:00.000000+00:00"):
        with pytest.raises(NativeIMAuthenticationError) as raised:
            verifier.verify(
                replace(metadata, timestamp=invalid),
                BODY,
                SecretMaterial(KEY),
                now=NOW,
            )
        assert raised.value.code == "native_im_auth_timestamp_invalid"


def test_body_bounds_and_exact_types_are_checked_and_material_is_closed() -> None:
    verifier, configuration, _ = verifier_and_inputs()
    for body in (b"", bytearray(BODY), b"x" * (configuration.max_response_bytes + 1)):
        material = SecretMaterial(KEY)
        with pytest.raises(NativeIMAuthenticationError):
            verifier.verify(
                metadata_for(configuration),
                body,  # type: ignore[arg-type]
                material,
                now=NOW,
            )
        assert material.closed is True


def test_unsupported_verifier_contract_and_preflight_are_redacted() -> None:
    baseline = profile()
    configuration = configuration_for(baseline)
    verifier = NativeIMHMACRawBodyVerifier(configuration, baseline, ReplayGuard())
    with pytest.raises(NativeIMAuthenticationError) as unsupported:
        verifier.verify(
            metadata_for(configuration),
            BODY,
            SecretMaterial(KEY),
            now=NOW,
        )
    assert unsupported.value.code == "native_im_auth_contract_unsupported"

    value = authentication_profile()
    expired = replace(configuration_for(value), approval_expires_at=NOW)
    verifier = NativeIMHMACRawBodyVerifier(expired, value, ReplayGuard())
    with pytest.raises(NativeIMAuthenticationError) as preflight:
        verifier.verify(
            metadata_for(expired),
            BODY,
            SecretMaterial(KEY),
            now=NOW,
        )
    assert preflight.value.code == "native_im_auth_preflight_failed"


def test_hostile_replay_guard_failure_has_no_canary_cause_or_context() -> None:
    canary = "replay-guard-secret-canary"

    class HostileGuard:
        def claim(self, **values: object) -> bool:
            raise RuntimeError(canary)

    value = authentication_profile()
    configuration = configuration_for(value)
    verifier = NativeIMHMACRawBodyVerifier(configuration, value, HostileGuard())
    with pytest.raises(NativeIMAuthenticationError) as raised:
        verifier.verify(
            metadata_for(configuration),
            BODY,
            SecretMaterial(KEY),
            now=NOW,
        )
    assert raised.value.code == "native_im_auth_replay_guard_failed"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert canary not in str(raised.value)


def test_metadata_and_verifier_rendering_hide_nonce_signature_endpoint_and_secret() -> None:
    verifier, configuration, _ = verifier_and_inputs()
    metadata = metadata_for(configuration)
    rendered = f"{metadata!r} {verifier!r}"
    for canary in (
        metadata.nonce,
        metadata.signature,
        configuration.origin.host,
        configuration.read_path.canonical,
        KEY.decode(),
    ):
        assert canary not in rendered


def test_result_rejects_subclass_and_metadata_subclass_before_field_access() -> None:
    class MetadataSubclass(NativeIMDetachedSignatureV1):
        pass

    class ResultSubclass(NativeIMRawVerificationResultV1):
        pass

    verifier, _, _ = verifier_and_inputs()
    with pytest.raises(NativeIMAuthenticationError):
        verifier.verify(
            object.__new__(MetadataSubclass),
            BODY,
            SecretMaterial(KEY),
            now=NOW,
        )
    with pytest.raises(TypeError):
        ResultSubclass(
            schema_version=1,
            verifier_id="test-verifier",
            key_id="test-key",
            signed_at=NOW,
            expires_at="2026-08-28T12:05:00.000000Z",
            verified_at=NOW,
            body_digest="a" * 64,
            nonce_digest="b" * 64,
            authentication_evidence_digest="c" * 64,
        )
