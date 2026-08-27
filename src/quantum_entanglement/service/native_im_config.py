"""Fail-closed native-IM sandbox configuration values.

This module parses no ambient environment and opens no network connection.  It defines
canonical endpoint allowlist values that a later E2 configuration union can bind to an
explicit approval record.  Endpoint values never contain credentials or outbound paths.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from dataclasses import dataclass, field

_ORIGIN_PATTERN = re.compile(r"https://([^/:?#\\]+):([1-9][0-9]{0,4})\Z")
_HOST_LABEL_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_ABSOLUTE_PATH_PATTERN = re.compile(r"/(?:[A-Za-z0-9._~-]+)(?:/[A-Za-z0-9._~-]+)*\Z")
_FORBIDDEN_HOST_SUFFIXES = (".internal", ".local", ".localhost")
_MAX_APPROVED_ADDRESSES = 32

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class NativeIMConfigurationError(ValueError):
    """A redacted native-IM configuration failure with a stable code and field."""

    __slots__ = ("code", "field")

    def __init__(self, code: str, field: str = "native_im_configuration") -> None:
        self.code = code
        self.field = field
        super().__init__(f"{code} ({field})")


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


__all__ = [
    "CanonicalAbsolutePath",
    "CanonicalHTTPSOrigin",
    "IPAddress",
    "NativeIMConfigurationError",
    "parse_approved_ip_addresses",
]
