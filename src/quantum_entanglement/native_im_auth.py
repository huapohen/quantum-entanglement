"""Detached raw-body authentication for native-IM E2 inbound observations."""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol

from ._native_im_codec import (
    NATIVE_IM_SCHEMA_VERSION,
    _digest,
    _id,
    _model_digest,
    _schema_version,
    _timestamp,
    _utf8_text,
)
from .native_im_provider_profile import IMProviderProfileV1
from .service.native_im_config import (
    NativeIMInboundOnlyConfigV1,
    NativeIMSandboxPreflightError,
    validate_native_im_sandbox_preflight_v1,
)
from .service.secrets import SecretMaterial

_SIGNATURE_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_UNIX_SECONDS_PATTERN = re.compile(r"(?:0|[1-9][0-9]{0,18})\Z")
_VERIFIER_CONTRACT_ID = "qe.native-im.hmac-sha256.v1"
_SIGNATURE_DOMAIN = b"quantum-entanglement.native-im-inbound-signature/1\n"
_NONCE_DOMAIN = b"quantum-entanglement.native-im-inbound-nonce/1\n"


class NativeIMNonceReplayGuardPort(Protocol):
    """Claim one verified nonce identity in a durable replay ledger."""

    def claim(
        self,
        *,
        scope: tuple[str, str, str, str],
        key_id: str,
        nonce_digest: str,
        signed_at: str,
        expires_at: str,
        authentication_evidence_digest: str,
    ) -> bool:
        """Return exact True only for the first durable claim."""


class NativeIMAuthenticationError(ValueError):
    """A stable redacted authentication or replay failure."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, repr=False)
class NativeIMDetachedSignatureV1:
    schema_version: int
    timestamp: str = field(repr=False)
    nonce: str = field(repr=False)
    key_id: str
    signature: str = field(repr=False)

    def __post_init__(self) -> None:
        _schema_version(self.schema_version)
        _utf8_text(
            self.timestamp,
            "timestamp",
            maximum_bytes=64,
            allow_empty=False,
            allow_message_controls=False,
        )
        _id(self.nonce, "nonce")
        _id(self.key_id, "keyId")
        signature = _utf8_text(
            self.signature,
            "signature",
            maximum_bytes=64,
            allow_empty=False,
            allow_message_controls=False,
        )
        if _SIGNATURE_PATTERN.fullmatch(signature) is None:
            raise ValueError("signature must be canonical lowercase HMAC-SHA256")

    def __repr__(self) -> str:
        fingerprint = hashlib.sha256(
            f"{self.key_id}\n{self.timestamp}\n{self.nonce}\n{self.signature}".encode()
        ).hexdigest()[:16]
        return f"NativeIMDetachedSignatureV1(fingerprint={fingerprint!r})"


@dataclass(frozen=True, repr=False)
class NativeIMRawVerificationResultV1:
    schema_version: int
    verifier_id: str
    key_id: str
    signed_at: str
    verified_at: str
    body_digest: str
    nonce_digest: str
    authentication_evidence_digest: str

    def __post_init__(self) -> None:
        if type(self) is not NativeIMRawVerificationResultV1:
            raise TypeError("raw verification result requires the exact V1 class")
        _schema_version(self.schema_version)
        _id(self.verifier_id, "verifierId")
        _id(self.key_id, "keyId")
        _timestamp(self.signed_at, "signedAt")
        _timestamp(self.verified_at, "verifiedAt")
        _digest(self.body_digest, "bodyDigest")
        _digest(self.nonce_digest, "nonceDigest")
        _digest(self.authentication_evidence_digest, "authenticationEvidenceDigest")

    def __repr__(self) -> str:
        return (
            "NativeIMRawVerificationResultV1("
            f"evidence={self.authentication_evidence_digest[:12]!r})"
        )


class NativeIMHMACRawBodyVerifier:
    """Verify one bounded read response before any body decoding or durable admission."""

    __slots__ = ("__configuration", "__profile", "__replay_guard")

    def __init__(
        self,
        configuration: NativeIMInboundOnlyConfigV1,
        profile: IMProviderProfileV1,
        replay_guard: NativeIMNonceReplayGuardPort,
    ) -> None:
        if type(configuration) is not NativeIMInboundOnlyConfigV1:
            raise TypeError("verifier requires the exact inbound-only configuration")
        if type(profile) is not IMProviderProfileV1:
            raise TypeError("verifier requires the exact provider profile")
        if replay_guard is None:
            raise TypeError("verifier requires a nonce replay guard")
        self.__configuration = configuration
        self.__profile = profile
        self.__replay_guard = replay_guard

    def verify(
        self,
        metadata: NativeIMDetachedSignatureV1,
        raw_body: bytes,
        verification_material: SecretMaterial,
        *,
        now: str,
    ) -> NativeIMRawVerificationResultV1:
        if type(verification_material) is not SecretMaterial:
            raise TypeError("verification material must be an exact secret lease")
        try:
            return self._verify(metadata, raw_body, verification_material, now=now)
        finally:
            verification_material.close()

    def _verify(
        self,
        metadata: NativeIMDetachedSignatureV1,
        raw_body: bytes,
        verification_material: SecretMaterial,
        *,
        now: str,
    ) -> NativeIMRawVerificationResultV1:
        if type(metadata) is not NativeIMDetachedSignatureV1:
            raise NativeIMAuthenticationError("native_im_auth_metadata_invalid") from None
        if type(raw_body) is not bytes or not raw_body:
            raise NativeIMAuthenticationError("native_im_auth_body_invalid") from None
        if len(raw_body) > self.__configuration.max_response_bytes:
            raise NativeIMAuthenticationError("native_im_auth_body_too_large") from None
        try:
            validate_native_im_sandbox_preflight_v1(
                self.__configuration,
                self.__profile,
                now=now,
            )
        except NativeIMSandboxPreflightError:
            raise NativeIMAuthenticationError("native_im_auth_preflight_failed") from None
        authentication = self.__profile.authentication
        if (
            authentication.status != "supported"
            or authentication.verifier_contract_id != _VERIFIER_CONTRACT_ID
            or authentication.signature_mode != "detached_raw_body"
            or authentication.nonce_mode != "signed_unique"
            or authentication.endpoint_binding_mode != "method_host_port_path_body"
            or authentication.replay_window_seconds is None
        ):
            raise NativeIMAuthenticationError("native_im_auth_contract_unsupported") from None
        if metadata.key_id != self.__configuration.verification_key_id:
            raise NativeIMAuthenticationError("native_im_auth_key_mismatch") from None
        signed_at = self._canonical_signed_at(metadata.timestamp, authentication.timestamp_mode)
        self._validate_window(
            signed_at,
            now,
            replay_window_seconds=authentication.replay_window_seconds,
        )
        body_digest = hashlib.sha256(raw_body).hexdigest()
        signed_message = self._signed_message(metadata, body_digest)
        owned_key = bytearray(verification_material.view())
        try:
            expected_signature = hmac.new(owned_key, signed_message, hashlib.sha256).hexdigest()
        finally:
            for index in range(len(owned_key)):
                owned_key[index] = 0
        if not hmac.compare_digest(expected_signature, metadata.signature):
            raise NativeIMAuthenticationError("native_im_auth_signature_invalid") from None
        nonce_digest = hashlib.sha256(_NONCE_DOMAIN + metadata.nonce.encode()).hexdigest()
        evidence_body = {
            "bodyDigest": body_digest,
            "keyId": metadata.key_id,
            "nonceDigest": nonce_digest,
            "profileDigest": self.__profile.canonical_digest(),
            "signatureDigest": hashlib.sha256(metadata.signature.encode()).hexdigest(),
            "signedAt": signed_at,
        }
        evidence_digest = _model_digest("NativeIMAuthenticationEvidenceV1", evidence_body)
        expires_at = self._offset_timestamp(
            signed_at,
            seconds=authentication.replay_window_seconds,
        )
        guard_failed = False
        claimed: object = False
        try:
            claimed = self.__replay_guard.claim(
                scope=(
                    self.__profile.tenant_id,
                    self.__profile.workspace_id,
                    self.__profile.provider,
                    self.__profile.channel_id,
                ),
                key_id=metadata.key_id,
                nonce_digest=nonce_digest,
                signed_at=signed_at,
                expires_at=expires_at,
                authentication_evidence_digest=evidence_digest,
            )
        except Exception:
            guard_failed = True
        if guard_failed or type(claimed) is not bool:
            raise NativeIMAuthenticationError("native_im_auth_replay_guard_failed") from None
        if claimed is not True:
            raise NativeIMAuthenticationError("native_im_auth_nonce_replay") from None
        return NativeIMRawVerificationResultV1(
            schema_version=NATIVE_IM_SCHEMA_VERSION,
            verifier_id=_VERIFIER_CONTRACT_ID,
            key_id=metadata.key_id,
            signed_at=signed_at,
            verified_at=now,
            body_digest=body_digest,
            nonce_digest=nonce_digest,
            authentication_evidence_digest=evidence_digest,
        )

    def _signed_message(
        self,
        metadata: NativeIMDetachedSignatureV1,
        body_digest: str,
    ) -> bytes:
        components = (
            "GET",
            self.__configuration.origin.canonical,
            self.__configuration.read_path.canonical,
            metadata.timestamp,
            metadata.nonce,
            body_digest,
        )
        return _SIGNATURE_DOMAIN + "\n".join(components).encode()

    @staticmethod
    def _canonical_signed_at(timestamp: str, mode: str | None) -> str:
        if mode == "signed_canonical_utc":
            try:
                return _timestamp(timestamp, "timestamp")
            except (TypeError, ValueError):
                raise NativeIMAuthenticationError("native_im_auth_timestamp_invalid") from None
        if mode == "signed_unix_seconds" and _UNIX_SECONDS_PATTERN.fullmatch(timestamp):
            try:
                parsed = datetime.fromtimestamp(int(timestamp), timezone.utc)
            except (OverflowError, OSError, ValueError):
                raise NativeIMAuthenticationError("native_im_auth_timestamp_invalid") from None
            return parsed.isoformat(timespec="microseconds").replace("+00:00", "Z")
        raise NativeIMAuthenticationError("native_im_auth_timestamp_invalid") from None

    @staticmethod
    def _validate_window(signed_at: str, now: str, *, replay_window_seconds: int) -> None:
        try:
            _timestamp(now, "now")
            signed = datetime.fromisoformat(signed_at[:-1] + "+00:00")
            observed = datetime.fromisoformat(now[:-1] + "+00:00")
        except (TypeError, ValueError):
            raise NativeIMAuthenticationError("native_im_auth_clock_invalid") from None
        delta = abs((observed - signed).total_seconds())
        if delta > replay_window_seconds:
            raise NativeIMAuthenticationError("native_im_auth_timestamp_expired") from None

    @staticmethod
    def _offset_timestamp(value: str, *, seconds: int) -> str:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
        return (
            (parsed + timedelta(seconds=seconds))
            .astimezone(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )

    def __repr__(self) -> str:
        fingerprint = hashlib.sha256(
            (self.__configuration.fingerprint + "\n" + self.__profile.canonical_digest()).encode()
        ).hexdigest()[:16]
        return f"NativeIMHMACRawBodyVerifier(fingerprint={fingerprint!r})"


__all__ = [
    "NativeIMAuthenticationError",
    "NativeIMDetachedSignatureV1",
    "NativeIMHMACRawBodyVerifier",
    "NativeIMNonceReplayGuardPort",
    "NativeIMRawVerificationResultV1",
]
