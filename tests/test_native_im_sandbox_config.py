from __future__ import annotations

import ipaddress

import pytest

from quantum_entanglement.service.native_im_config import (
    CanonicalAbsolutePath,
    CanonicalHTTPSOrigin,
    NativeIMConfigurationError,
    parse_approved_ip_addresses,
)


def test_https_origin_requires_one_canonical_dns_authority_and_explicit_port() -> None:
    origin = CanonicalHTTPSOrigin.parse("https://sandbox.im.example.com:443")

    assert origin.host == "sandbox.im.example.com"
    assert origin.port == 443
    assert origin.canonical == "https://sandbox.im.example.com:443"
    assert len(origin.fingerprint) == 16
    assert origin.canonical not in repr(origin)
    assert origin.host not in str(origin)


@pytest.mark.parametrize(
    "value",
    (
        "http://sandbox.im.example.com:443",
        "https://sandbox.im.example.com",
        "https://sandbox.im.example.com:0443",
        "https://sandbox.im.example.com:0",
        "https://sandbox.im.example.com:65536",
        "https://SANDBOX.im.example.com:443",
        "https://sandbox.im.example.com.:443",
        "https://sandbox_im.example.com:443",
        "https://-sandbox.im.example.com:443",
        "https://sandbox-.im.example.com:443",
        "https://single-label:443",
        "https://127.0.0.1:443",
        "https://[2001:4860:4860::8888]:443",
        "https://user@sandbox.im.example.com:443",
        "https://sandbox.im.example.com:443/",
        "https://sandbox.im.example.com:443/read",
        "https://sandbox.im.example.com:443?query=true",
        "https://sandbox.im.example.com:443#fragment",
        "https://sandbox.im.example.com:443\\read",
        "https://sandbox%2eim.example.com:443",
        "https://服务.example.com:443",
        "https://service.local:443",
        "https://service.internal:443",
        "https://service.localhost:443",
    ),
)
def test_https_origin_rejects_aliases_unsafe_hosts_and_url_components(value: str) -> None:
    with pytest.raises(NativeIMConfigurationError) as raised:
        CanonicalHTTPSOrigin.parse(value)
    assert value not in str(raised.value)


@pytest.mark.parametrize("value", (None, 443, b"https://sandbox.im.example.com:443"))
def test_https_origin_rejects_non_text_without_rendering(value: object) -> None:
    with pytest.raises(NativeIMConfigurationError):
        CanonicalHTTPSOrigin.parse(value)


def test_absolute_paths_are_unencoded_bounded_and_redacted() -> None:
    path = CanonicalAbsolutePath.parse("/v1/inbound-events")

    assert path.canonical == "/v1/inbound-events"
    assert len(path.fingerprint) == 16
    assert path.canonical not in repr(path)
    assert path.canonical not in str(path)


@pytest.mark.parametrize(
    "value",
    (
        "",
        "/",
        "relative/path",
        "//v1/read",
        "/v1//read",
        "/v1/read/",
        "/v1/./read",
        "/v1/../read",
        "/v1/%2e%2e/read",
        "/v1/read%2fnext",
        "/v1\\read",
        "/v1/read?cursor=x",
        "/v1/read#fragment",
        "/v1/read:alternate",
        "/v1/读取",
        "/v1/read\nforged",
        "/" + "a" * 256,
    ),
)
def test_absolute_paths_reject_normalization_aliases_and_dynamic_components(value: str) -> None:
    with pytest.raises(NativeIMConfigurationError) as raised:
        CanonicalAbsolutePath.parse(value)
    if value:
        assert value not in str(raised.value)


def test_approved_addresses_are_exact_sorted_unique_and_public() -> None:
    addresses = parse_approved_ip_addresses("2001:4860:4860::8888,8.8.8.8")

    assert addresses == (
        ipaddress.ip_address("2001:4860:4860::8888"),
        ipaddress.ip_address("8.8.8.8"),
    )


@pytest.mark.parametrize(
    "value",
    (
        "",
        "8.8.8.8,2001:4860:4860::8888",
        "8.8.8.8,8.8.8.8",
        "8.8.8.8, 9.9.9.9",
        "8.8.8.8,",
        "008.008.008.008",
        "2001:4860:4860:0:0:0:0:8888",
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "224.0.0.1",
        "255.255.255.255",
        "::1",
        "fe80::1",
        "not-an-address",
    ),
)
def test_approved_addresses_reject_ambiguous_duplicate_and_non_public_values(value: str) -> None:
    with pytest.raises(NativeIMConfigurationError) as raised:
        parse_approved_ip_addresses(value)
    if value:
        assert value not in str(raised.value)


def test_approved_addresses_enforce_a_hard_item_limit() -> None:
    addresses = ",".join(f"8.8.4.{index}" for index in range(1, 34))
    with pytest.raises(NativeIMConfigurationError) as raised:
        parse_approved_ip_addresses(addresses)
    assert raised.value.code == "native_im_addresses_too_many"
