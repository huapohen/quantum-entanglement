"""Fail-closed native-IM sandbox configuration values.

This module parses no ambient environment and opens no network connection.  It defines
canonical endpoint allowlist values that a later E2 configuration union can bind to an
explicit approval record.  Endpoint values never contain credentials or outbound paths.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from itertools import islice

from quantum_entanglement._native_im_codec import _digest, _id, _timestamp
from quantum_entanglement.native_im_provider_profile import (
    IMProviderProfileBindingError,
    IMProviderProfileV1,
    evaluate_e2_profile_readiness_v1,
    validate_profile_binding_v1,
)

from .secrets import SecretRef

_ORIGIN_PATTERN = re.compile(r"https://([^/:?#\\]+):([1-9][0-9]{0,4})\Z")
_HOST_LABEL_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_ABSOLUTE_PATH_PATTERN = re.compile(r"/(?:[A-Za-z0-9._~-]+)(?:/[A-Za-z0-9._~-]+)*\Z")
_FORBIDDEN_HOST_SUFFIXES = (".internal", ".local", ".localhost")
_MAX_APPROVED_ADDRESSES = 32
_MAX_ENVIRONMENT_ITEMS = 4_096
_MAX_ENVIRONMENT_KEY_LENGTH = 256
_INTEGER_PATTERN = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_BASE_CONFIG_KEYS = frozenset(
    {
        "QE_NATIVE_IM_CONFIG_VERSION",
        "QE_NATIVE_IM_ENABLED",
    }
)
_ENABLED_CONFIG_KEYS = frozenset(
    {
        *_BASE_CONFIG_KEYS,
        "QE_NATIVE_IM_MODE",
        "QE_NATIVE_IM_PROFILE_ID",
        "QE_NATIVE_IM_PROFILE_REVISION",
        "QE_NATIVE_IM_PROFILE_DIGEST",
        "QE_NATIVE_IM_APPROVAL_ID",
        "QE_NATIVE_IM_APPROVAL_EXPIRES_AT",
        "QE_NATIVE_IM_PROVIDER",
        "QE_NATIVE_IM_TENANT_ID",
        "QE_NATIVE_IM_WORKSPACE_ID",
        "QE_NATIVE_IM_CHANNEL_ID",
        "QE_NATIVE_IM_ORIGIN",
        "QE_NATIVE_IM_APPROVED_ADDRESSES",
        "QE_NATIVE_IM_HEALTH_PATH",
        "QE_NATIVE_IM_READ_PATH",
        "QE_NATIVE_IM_CREDENTIAL_REF",
        "QE_NATIVE_IM_VERIFICATION_SECRET_REF",
        "QE_NATIVE_IM_PAGE_LIMIT",
        "QE_NATIVE_IM_MAX_RESPONSE_BYTES",
        "QE_NATIVE_IM_CONNECT_TIMEOUT_MS",
        "QE_NATIVE_IM_READ_TIMEOUT_MS",
        "QE_NATIVE_IM_OUTBOUND_MODE",
        "QE_NATIVE_IM_REDIRECT_MODE",
    }
)
_ALL_CONFIG_KEYS = _ENABLED_CONFIG_KEYS

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class NativeIMConfigurationError(ValueError):
    """A redacted native-IM configuration failure with a stable code and field."""

    __slots__ = ("code", "field")

    def __init__(self, code: str, field: str = "native_im_configuration") -> None:
        self.code = code
        self.field = field
        super().__init__(f"{code} ({field})")


class NativeIMSandboxPreflightError(ValueError):
    """A redacted mismatch between approved config, profile, scope, limits, or time."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _fingerprint(domain: str, value: str) -> str:
    return hashlib.sha256(f"{domain}\n{value}".encode()).hexdigest()[:16]


@dataclass(frozen=True, repr=False)
class CanonicalHTTPSOrigin:
    """One exact HTTPS DNS authority with an explicit port and no path."""

    host: str = field(repr=False)
    port: int

    def __post_init__(self) -> None:
        if type(self.host) is not str or type(self.port) is not int:
            raise NativeIMConfigurationError("native_im_origin_type_invalid", "origin")
        _validate_dns_host(self.host)
        if not 1 <= self.port <= 65_535:
            raise NativeIMConfigurationError("native_im_origin_port_invalid", "origin")

    @classmethod
    def parse(cls, value: object) -> CanonicalHTTPSOrigin:
        if type(value) is not str:
            raise NativeIMConfigurationError("native_im_origin_type_invalid", "origin")
        if not value.isascii() or len(value) > 276:
            raise NativeIMConfigurationError("native_im_origin_invalid", "origin")
        match = _ORIGIN_PATTERN.fullmatch(value)
        if match is None:
            raise NativeIMConfigurationError("native_im_origin_invalid", "origin")
        try:
            port = int(match.group(2))
        except ValueError:
            raise NativeIMConfigurationError("native_im_origin_invalid", "origin") from None
        origin = cls(host=match.group(1), port=port)
        if origin.canonical != value:
            raise NativeIMConfigurationError("native_im_origin_not_canonical", "origin")
        return origin

    @property
    def canonical(self) -> str:
        return f"https://{self.host}:{self.port}"

    @property
    def fingerprint(self) -> str:
        return _fingerprint("quantum-entanglement.native-im-origin/1", self.canonical)

    def __str__(self) -> str:
        return f"CanonicalHTTPSOrigin<{self.fingerprint}>"

    def __repr__(self) -> str:
        return f"CanonicalHTTPSOrigin(fingerprint={self.fingerprint!r})"


def _validate_dns_host(host: str) -> None:
    if (
        not host
        or not host.isascii()
        or host != host.lower()
        or len(host) > 253
        or host.endswith(".")
    ):
        raise NativeIMConfigurationError("native_im_origin_host_invalid", "origin")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise NativeIMConfigurationError("native_im_origin_ip_literal_forbidden", "origin")
    labels = host.split(".")
    if len(labels) < 2 or any(_HOST_LABEL_PATTERN.fullmatch(label) is None for label in labels):
        raise NativeIMConfigurationError("native_im_origin_host_invalid", "origin")
    if host == "localhost" or host.endswith(_FORBIDDEN_HOST_SUFFIXES):
        raise NativeIMConfigurationError("native_im_origin_host_class_forbidden", "origin")


@dataclass(frozen=True, repr=False)
class CanonicalAbsolutePath:
    """One bounded, unencoded absolute path with no normalization aliases."""

    value: str = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.value) is not str:
            raise NativeIMConfigurationError("native_im_path_type_invalid", "path")
        if (
            not self.value.isascii()
            or not 1 <= len(self.value.encode("ascii")) <= 256
            or _ABSOLUTE_PATH_PATTERN.fullmatch(self.value) is None
        ):
            raise NativeIMConfigurationError("native_im_path_invalid", "path")
        if any(segment in {".", ".."} for segment in self.value.split("/")):
            raise NativeIMConfigurationError("native_im_path_not_canonical", "path")

    @classmethod
    def parse(cls, value: object) -> CanonicalAbsolutePath:
        if type(value) is not str:
            raise NativeIMConfigurationError("native_im_path_type_invalid", "path")
        return cls(value=value)

    @property
    def canonical(self) -> str:
        return self.value

    @property
    def fingerprint(self) -> str:
        return _fingerprint("quantum-entanglement.native-im-path/1", self.value)

    def __str__(self) -> str:
        return f"CanonicalAbsolutePath<{self.fingerprint}>"

    def __repr__(self) -> str:
        return f"CanonicalAbsolutePath(fingerprint={self.fingerprint!r})"


def parse_approved_ip_addresses(value: object) -> tuple[IPAddress, ...]:
    """Parse an exact, sorted, public IP pin set without resolving a hostname."""

    field_name = "approved_addresses"
    if type(value) is not str:
        raise NativeIMConfigurationError("native_im_addresses_type_invalid", field_name)
    if not value or len(value) > 2_048 or not value.isascii():
        raise NativeIMConfigurationError("native_im_addresses_invalid", field_name)
    raw_addresses = value.split(",")
    if len(raw_addresses) > _MAX_APPROVED_ADDRESSES:
        raise NativeIMConfigurationError("native_im_addresses_too_many", field_name)
    if any(not item or item != item.strip() for item in raw_addresses):
        raise NativeIMConfigurationError("native_im_addresses_not_canonical", field_name)
    addresses: list[IPAddress] = []
    canonical: list[str] = []
    for item in raw_addresses:
        try:
            address = ipaddress.ip_address(item)
        except ValueError:
            raise NativeIMConfigurationError("native_im_address_invalid", field_name) from None
        if address.compressed != item:
            raise NativeIMConfigurationError("native_im_addresses_not_canonical", field_name)
        if (
            not address.is_global
            or address.is_multicast
            or address.is_unspecified
            or address.is_loopback
            or address.is_link_local
            or address.is_private
            or address.is_reserved
        ):
            raise NativeIMConfigurationError("native_im_address_class_forbidden", field_name)
        addresses.append(address)
        canonical.append(address.compressed)
    expected_order = sorted(canonical, key=lambda item: item.encode("ascii"))
    if canonical != expected_order or len(set(canonical)) != len(canonical):
        raise NativeIMConfigurationError("native_im_addresses_not_canonical", field_name)
    return tuple(addresses)


def _validate_exact_integer(value: int, field_name: str, minimum: int, maximum: int) -> None:
    if type(value) is not int:
        raise NativeIMConfigurationError("native_im_configuration_type_invalid", field_name)
    if not minimum <= value <= maximum:
        raise NativeIMConfigurationError("native_im_configuration_integer_out_of_range", field_name)


@dataclass(frozen=True, repr=False)
class NativeIMDisabledConfigV1:
    """An explicit zero-endpoint, zero-secret disabled configuration."""

    schema_version: int
    enabled: bool

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise NativeIMConfigurationError(
                "native_im_configuration_version_unsupported",
                "QE_NATIVE_IM_CONFIG_VERSION",
            )
        if type(self.enabled) is not bool or self.enabled is not False:
            raise NativeIMConfigurationError(
                "native_im_disabled_configuration_invalid",
                "QE_NATIVE_IM_ENABLED",
            )

    @property
    def fingerprint(self) -> str:
        return _fingerprint("quantum-entanglement.native-im-disabled-config/1", "1:false")

    def __repr__(self) -> str:
        return f"NativeIMDisabledConfigV1(fingerprint={self.fingerprint!r})"


@dataclass(frozen=True, repr=False)
class NativeIMInboundOnlyConfigV1:
    """A fully pinned E2 deployment configuration containing references, never secrets."""

    schema_version: int
    enabled: bool
    mode: str
    profile_id: str
    profile_revision: str
    profile_digest: str = field(repr=False)
    approval_id: str
    approval_expires_at: str
    provider: str
    tenant_id: str
    workspace_id: str
    channel_id: str
    origin: CanonicalHTTPSOrigin = field(repr=False)
    approved_addresses: tuple[IPAddress, ...] = field(repr=False)
    health_path: CanonicalAbsolutePath = field(repr=False)
    read_path: CanonicalAbsolutePath = field(repr=False)
    credential_ref: SecretRef = field(repr=False)
    verification_secret_ref: SecretRef = field(repr=False)
    page_limit: int
    max_response_bytes: int
    connect_timeout_ms: int
    read_timeout_ms: int
    outbound_mode: str
    redirect_mode: str

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise NativeIMConfigurationError(
                "native_im_configuration_version_unsupported",
                "QE_NATIVE_IM_CONFIG_VERSION",
            )
        if type(self.enabled) is not bool or self.enabled is not True:
            raise NativeIMConfigurationError(
                "native_im_enabled_configuration_invalid",
                "QE_NATIVE_IM_ENABLED",
            )
        if self.mode != "inbound_only":
            raise NativeIMConfigurationError("native_im_mode_forbidden", "QE_NATIVE_IM_MODE")
        for value, field_name in (
            (self.profile_id, "QE_NATIVE_IM_PROFILE_ID"),
            (self.profile_revision, "QE_NATIVE_IM_PROFILE_REVISION"),
            (self.approval_id, "QE_NATIVE_IM_APPROVAL_ID"),
            (self.provider, "QE_NATIVE_IM_PROVIDER"),
            (self.tenant_id, "QE_NATIVE_IM_TENANT_ID"),
            (self.workspace_id, "QE_NATIVE_IM_WORKSPACE_ID"),
            (self.channel_id, "QE_NATIVE_IM_CHANNEL_ID"),
        ):
            try:
                _id(value, field_name)
            except (TypeError, ValueError):
                raise NativeIMConfigurationError(
                    "native_im_configuration_identifier_invalid", field_name
                ) from None
        try:
            _digest(self.profile_digest, "QE_NATIVE_IM_PROFILE_DIGEST")
        except (TypeError, ValueError):
            raise NativeIMConfigurationError(
                "native_im_configuration_digest_invalid", "QE_NATIVE_IM_PROFILE_DIGEST"
            ) from None
        try:
            _timestamp(self.approval_expires_at, "QE_NATIVE_IM_APPROVAL_EXPIRES_AT")
        except (TypeError, ValueError):
            raise NativeIMConfigurationError(
                "native_im_configuration_timestamp_invalid",
                "QE_NATIVE_IM_APPROVAL_EXPIRES_AT",
            ) from None
        if type(self.origin) is not CanonicalHTTPSOrigin:
            raise NativeIMConfigurationError(
                "native_im_configuration_type_invalid", "QE_NATIVE_IM_ORIGIN"
            )
        if type(self.approved_addresses) is not tuple or not self.approved_addresses:
            raise NativeIMConfigurationError(
                "native_im_configuration_type_invalid", "QE_NATIVE_IM_APPROVED_ADDRESSES"
            )
        address_text = ",".join(
            address.compressed
            if type(address) in {ipaddress.IPv4Address, ipaddress.IPv6Address}
            else "invalid"
            for address in self.approved_addresses
        )
        try:
            parsed_addresses = parse_approved_ip_addresses(address_text)
        except NativeIMConfigurationError:
            raise NativeIMConfigurationError(
                "native_im_configuration_addresses_invalid",
                "QE_NATIVE_IM_APPROVED_ADDRESSES",
            ) from None
        if parsed_addresses != self.approved_addresses:
            raise NativeIMConfigurationError(
                "native_im_configuration_addresses_invalid",
                "QE_NATIVE_IM_APPROVED_ADDRESSES",
            )
        for path, field_name in (
            (self.health_path, "QE_NATIVE_IM_HEALTH_PATH"),
            (self.read_path, "QE_NATIVE_IM_READ_PATH"),
        ):
            if type(path) is not CanonicalAbsolutePath:
                raise NativeIMConfigurationError("native_im_configuration_type_invalid", field_name)
        if self.health_path == self.read_path:
            raise NativeIMConfigurationError(
                "native_im_configuration_paths_not_distinct", "QE_NATIVE_IM_READ_PATH"
            )
        for reference, field_name in (
            (self.credential_ref, "QE_NATIVE_IM_CREDENTIAL_REF"),
            (self.verification_secret_ref, "QE_NATIVE_IM_VERIFICATION_SECRET_REF"),
        ):
            if type(reference) is not SecretRef:
                raise NativeIMConfigurationError("native_im_configuration_type_invalid", field_name)
            if reference.scheme != "file":
                raise NativeIMConfigurationError("native_im_secret_scheme_forbidden", field_name)
        if self.credential_ref == self.verification_secret_ref:
            raise NativeIMConfigurationError(
                "native_im_secret_purpose_alias_forbidden",
                "QE_NATIVE_IM_VERIFICATION_SECRET_REF",
            )
        _validate_exact_integer(self.page_limit, "QE_NATIVE_IM_PAGE_LIMIT", 1, 1_000)
        _validate_exact_integer(
            self.max_response_bytes,
            "QE_NATIVE_IM_MAX_RESPONSE_BYTES",
            1_024,
            16 * 1_024 * 1_024,
        )
        _validate_exact_integer(
            self.connect_timeout_ms,
            "QE_NATIVE_IM_CONNECT_TIMEOUT_MS",
            100,
            30_000,
        )
        _validate_exact_integer(
            self.read_timeout_ms,
            "QE_NATIVE_IM_READ_TIMEOUT_MS",
            100,
            120_000,
        )
        if self.outbound_mode != "disabled":
            raise NativeIMConfigurationError(
                "native_im_outbound_mode_forbidden", "QE_NATIVE_IM_OUTBOUND_MODE"
            )
        if self.redirect_mode != "deny":
            raise NativeIMConfigurationError(
                "native_im_redirect_mode_forbidden", "QE_NATIVE_IM_REDIRECT_MODE"
            )

    @property
    def fingerprint(self) -> str:
        address_fingerprints = ",".join(address.compressed for address in self.approved_addresses)
        body = "\n".join(
            (
                str(self.schema_version),
                self.mode,
                self.profile_id,
                self.profile_revision,
                self.profile_digest,
                self.approval_id,
                self.approval_expires_at,
                self.provider,
                self.tenant_id,
                self.workspace_id,
                self.channel_id,
                self.origin.fingerprint,
                address_fingerprints,
                self.health_path.fingerprint,
                self.read_path.fingerprint,
                self.credential_ref.fingerprint,
                self.verification_secret_ref.fingerprint,
                str(self.page_limit),
                str(self.max_response_bytes),
                str(self.connect_timeout_ms),
                str(self.read_timeout_ms),
                self.outbound_mode,
                self.redirect_mode,
            )
        )
        return _fingerprint("quantum-entanglement.native-im-inbound-config/1", body)

    def __repr__(self) -> str:
        return f"NativeIMInboundOnlyConfigV1(fingerprint={self.fingerprint!r})"


NativeIMConfigV1 = NativeIMDisabledConfigV1 | NativeIMInboundOnlyConfigV1


class NativeIMSandboxConfig:
    """Strict parser for an explicit native-IM environment snapshot."""

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> NativeIMConfigV1:
        if not isinstance(environ, Mapping):
            raise TypeError("native IM environment must be a mapping")
        try:
            keys = tuple(islice(iter(environ), _MAX_ENVIRONMENT_ITEMS + 1))
        except Exception:
            raise NativeIMConfigurationError("native_im_configuration_snapshot_failed") from None
        if len(keys) > _MAX_ENVIRONMENT_ITEMS:
            raise NativeIMConfigurationError("native_im_configuration_snapshot_too_large")
        seen: set[str] = set()
        for key in keys:
            if type(key) is not str:
                raise NativeIMConfigurationError("native_im_configuration_key_invalid")
            if (
                not key
                or len(key) > _MAX_ENVIRONMENT_KEY_LENGTH
                or any(character in key for character in ("\x00", "\r", "\n"))
                or key in seen
            ):
                raise NativeIMConfigurationError("native_im_configuration_key_invalid")
            seen.add(key)
        namespace_keys = {key for key in seen if key.startswith("QE_NATIVE_IM_")}
        unknown = namespace_keys - _ALL_CONFIG_KEYS
        if unknown:
            raise NativeIMConfigurationError("native_im_configuration_unknown_field")
        missing_base = _BASE_CONFIG_KEYS - namespace_keys
        if missing_base:
            raise NativeIMConfigurationError(
                "native_im_configuration_missing_field", sorted(missing_base)[0]
            )
        snapshot: dict[str, str] = {}
        for key in sorted(_BASE_CONFIG_KEYS):
            snapshot[key] = cls._read_raw_value(environ, key)
        version = cls._parse_integer(
            snapshot["QE_NATIVE_IM_CONFIG_VERSION"], "QE_NATIVE_IM_CONFIG_VERSION"
        )
        if version != 1:
            raise NativeIMConfigurationError(
                "native_im_configuration_version_unsupported", "QE_NATIVE_IM_CONFIG_VERSION"
            )
        enabled = cls._parse_boolean(snapshot["QE_NATIVE_IM_ENABLED"], "QE_NATIVE_IM_ENABLED")
        expected = _ENABLED_CONFIG_KEYS if enabled else _BASE_CONFIG_KEYS
        unexpected = namespace_keys - expected
        if unexpected:
            raise NativeIMConfigurationError("native_im_configuration_unknown_field")
        missing = expected - namespace_keys
        if missing:
            raise NativeIMConfigurationError(
                "native_im_configuration_missing_field", sorted(missing)[0]
            )
        if not enabled:
            return NativeIMDisabledConfigV1(schema_version=version, enabled=False)
        for key in sorted(expected - _BASE_CONFIG_KEYS):
            snapshot[key] = cls._read_raw_value(environ, key)
        return cls._build_enabled(version, snapshot)

    @staticmethod
    def _read_raw_value(environ: Mapping[str, str], field_name: str) -> str:
        try:
            value = environ[field_name]
        except Exception:
            raise NativeIMConfigurationError("native_im_configuration_snapshot_failed") from None
        if type(value) is not str:
            raise NativeIMConfigurationError("native_im_configuration_type_invalid", field_name)
        if (
            not value
            or value != value.strip()
            or len(value) > 4_096
            or any(character in value for character in ("\x00", "\r", "\n"))
        ):
            raise NativeIMConfigurationError("native_im_configuration_value_invalid", field_name)
        return value

    @staticmethod
    def _parse_integer(value: str, field_name: str) -> int:
        if _INTEGER_PATTERN.fullmatch(value) is None:
            raise NativeIMConfigurationError("native_im_configuration_integer_invalid", field_name)
        return int(value)

    @staticmethod
    def _parse_boolean(value: str, field_name: str) -> bool:
        if value not in {"true", "false"}:
            raise NativeIMConfigurationError("native_im_configuration_boolean_invalid", field_name)
        return value == "true"

    @classmethod
    def _build_enabled(cls, version: int, values: Mapping[str, str]) -> NativeIMInboundOnlyConfigV1:
        try:
            origin = CanonicalHTTPSOrigin.parse(values["QE_NATIVE_IM_ORIGIN"])
            addresses = parse_approved_ip_addresses(values["QE_NATIVE_IM_APPROVED_ADDRESSES"])
            health_path = CanonicalAbsolutePath.parse(values["QE_NATIVE_IM_HEALTH_PATH"])
            read_path = CanonicalAbsolutePath.parse(values["QE_NATIVE_IM_READ_PATH"])
            credential_ref = SecretRef.parse(values["QE_NATIVE_IM_CREDENTIAL_REF"])
            verification_ref = SecretRef.parse(values["QE_NATIVE_IM_VERIFICATION_SECRET_REF"])
        except (NativeIMConfigurationError, TypeError, ValueError):
            raise NativeIMConfigurationError("native_im_configuration_value_invalid") from None
        return NativeIMInboundOnlyConfigV1(
            schema_version=version,
            enabled=True,
            mode=values["QE_NATIVE_IM_MODE"],
            profile_id=values["QE_NATIVE_IM_PROFILE_ID"],
            profile_revision=values["QE_NATIVE_IM_PROFILE_REVISION"],
            profile_digest=values["QE_NATIVE_IM_PROFILE_DIGEST"],
            approval_id=values["QE_NATIVE_IM_APPROVAL_ID"],
            approval_expires_at=values["QE_NATIVE_IM_APPROVAL_EXPIRES_AT"],
            provider=values["QE_NATIVE_IM_PROVIDER"],
            tenant_id=values["QE_NATIVE_IM_TENANT_ID"],
            workspace_id=values["QE_NATIVE_IM_WORKSPACE_ID"],
            channel_id=values["QE_NATIVE_IM_CHANNEL_ID"],
            origin=origin,
            approved_addresses=addresses,
            health_path=health_path,
            read_path=read_path,
            credential_ref=credential_ref,
            verification_secret_ref=verification_ref,
            page_limit=cls._parse_integer(
                values["QE_NATIVE_IM_PAGE_LIMIT"], "QE_NATIVE_IM_PAGE_LIMIT"
            ),
            max_response_bytes=cls._parse_integer(
                values["QE_NATIVE_IM_MAX_RESPONSE_BYTES"], "QE_NATIVE_IM_MAX_RESPONSE_BYTES"
            ),
            connect_timeout_ms=cls._parse_integer(
                values["QE_NATIVE_IM_CONNECT_TIMEOUT_MS"], "QE_NATIVE_IM_CONNECT_TIMEOUT_MS"
            ),
            read_timeout_ms=cls._parse_integer(
                values["QE_NATIVE_IM_READ_TIMEOUT_MS"], "QE_NATIVE_IM_READ_TIMEOUT_MS"
            ),
            outbound_mode=values["QE_NATIVE_IM_OUTBOUND_MODE"],
            redirect_mode=values["QE_NATIVE_IM_REDIRECT_MODE"],
        )


def validate_native_im_sandbox_preflight_v1(
    configuration: NativeIMInboundOnlyConfigV1,
    profile: IMProviderProfileV1,
    *,
    now: str,
) -> None:
    """Bind approved config to one ready profile without reading secrets or using network."""

    if type(configuration) is not NativeIMInboundOnlyConfigV1:
        raise TypeError("preflight requires the exact inbound-only configuration")
    if type(profile) is not IMProviderProfileV1:
        raise TypeError("preflight requires the exact provider profile")
    try:
        _timestamp(now, "now")
    except (TypeError, ValueError):
        raise NativeIMSandboxPreflightError("native_im_preflight_clock_invalid") from None
    if configuration.approval_expires_at <= now:
        raise NativeIMSandboxPreflightError("native_im_preflight_approval_expired") from None
    if configuration.profile_id != profile.profile_id:
        raise NativeIMSandboxPreflightError("native_im_preflight_profile_mismatch") from None
    try:
        validate_profile_binding_v1(
            profile,
            expected_revision=configuration.profile_revision,
            expected_digest=configuration.profile_digest,
        )
    except IMProviderProfileBindingError:
        raise NativeIMSandboxPreflightError("native_im_preflight_profile_mismatch") from None
    configured_scope = (
        configuration.tenant_id,
        configuration.workspace_id,
        configuration.provider,
        configuration.channel_id,
    )
    profile_scope = (
        profile.tenant_id,
        profile.workspace_id,
        profile.provider,
        profile.channel_id,
    )
    if configured_scope != profile_scope:
        raise NativeIMSandboxPreflightError("native_im_preflight_scope_mismatch") from None
    if evaluate_e2_profile_readiness_v1(profile):
        raise NativeIMSandboxPreflightError("native_im_preflight_profile_not_ready") from None
    if configuration.page_limit > profile.limits.max_page_events:
        raise NativeIMSandboxPreflightError(
            "native_im_preflight_page_limit_exceeds_profile"
        ) from None
    if configuration.max_response_bytes > profile.limits.max_raw_page_bytes:
        raise NativeIMSandboxPreflightError(
            "native_im_preflight_response_limit_exceeds_profile"
        ) from None


__all__ = [
    "CanonicalAbsolutePath",
    "CanonicalHTTPSOrigin",
    "IPAddress",
    "NativeIMConfigV1",
    "NativeIMConfigurationError",
    "NativeIMDisabledConfigV1",
    "NativeIMInboundOnlyConfigV1",
    "NativeIMSandboxConfig",
    "NativeIMSandboxPreflightError",
    "parse_approved_ip_addresses",
    "validate_native_im_sandbox_preflight_v1",
]
