from __future__ import annotations

import ipaddress
from collections.abc import Iterator, Mapping
from dataclasses import replace

import pytest

from quantum_entanglement.service.native_im_config import (
    CanonicalAbsolutePath,
    CanonicalHTTPSOrigin,
    NativeIMConfigurationError,
    NativeIMDisabledConfigV1,
    NativeIMInboundOnlyConfigV1,
    NativeIMSandboxConfig,
    NativeIMSandboxPreflightError,
    parse_approved_ip_addresses,
    validate_native_im_sandbox_preflight_v1,
)
from quantum_entanglement.service.secrets import SecretRef
from tests.test_native_im_provider_profile import profile


class ChangingEnvironment(Mapping[str, str]):
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values
        self.reads: dict[str, int] = {}

    def __getitem__(self, key: str) -> str:
        self.reads[key] = self.reads.get(key, 0) + 1
        if self.reads[key] > 1:
            return "changed-after-first-read"
        return self.values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.values)

    def __len__(self) -> int:
        return len(self.values)


def disabled_environment() -> dict[str, str]:
    return {
        "PATH": "/host/path",
        "QE_CONNECTOR": "fake",
        "QE_NATIVE_IM_CONFIG_VERSION": "1",
        "QE_NATIVE_IM_ENABLED": "false",
    }


def enabled_environment() -> dict[str, str]:
    return {
        "PATH": "/host/path",
        "QE_CONNECTOR": "fake",
        "QE_NATIVE_IM_CONFIG_VERSION": "1",
        "QE_NATIVE_IM_ENABLED": "true",
        "QE_NATIVE_IM_MODE": "inbound_only",
        "QE_NATIVE_IM_PROFILE_ID": "test-profile",
        "QE_NATIVE_IM_PROFILE_REVISION": "test-revision-1",
        "QE_NATIVE_IM_PROFILE_DIGEST": "a" * 64,
        "QE_NATIVE_IM_APPROVAL_ID": "test-approval",
        "QE_NATIVE_IM_APPROVAL_EXPIRES_AT": "2026-09-28T00:00:00.000001Z",
        "QE_NATIVE_IM_PROVIDER": "test-provider",
        "QE_NATIVE_IM_TENANT_ID": "test-tenant",
        "QE_NATIVE_IM_WORKSPACE_ID": "test-workspace",
        "QE_NATIVE_IM_CHANNEL_ID": "test-channel",
        "QE_NATIVE_IM_ORIGIN": "https://sandbox.im.example.com:443",
        "QE_NATIVE_IM_APPROVED_ADDRESSES": "2001:4860:4860::8888,8.8.8.8",
        "QE_NATIVE_IM_HEALTH_PATH": "/v1/health",
        "QE_NATIVE_IM_READ_PATH": "/v1/inbound-events",
        "QE_NATIVE_IM_CREDENTIAL_REF": "file://native-im-read-credential",
        "QE_NATIVE_IM_VERIFICATION_SECRET_REF": "file://native-im-verification-key",
        "QE_NATIVE_IM_VERIFICATION_KEY_ID": "test-verification-key-1",
        "QE_NATIVE_IM_PAGE_LIMIT": "100",
        "QE_NATIVE_IM_MAX_RESPONSE_BYTES": "8388608",
        "QE_NATIVE_IM_CONNECT_TIMEOUT_MS": "5000",
        "QE_NATIVE_IM_READ_TIMEOUT_MS": "30000",
        "QE_NATIVE_IM_OUTBOUND_MODE": "disabled",
        "QE_NATIVE_IM_REDIRECT_MODE": "deny",
    }


def bound_configuration(**environment_changes: str) -> NativeIMInboundOnlyConfigV1:
    provider_profile = profile()
    values = enabled_environment()
    values.update(
        {
            "QE_NATIVE_IM_PROFILE_ID": provider_profile.profile_id,
            "QE_NATIVE_IM_PROFILE_REVISION": provider_profile.revision,
            "QE_NATIVE_IM_PROFILE_DIGEST": provider_profile.canonical_digest(),
            "QE_NATIVE_IM_PROVIDER": provider_profile.provider,
            "QE_NATIVE_IM_TENANT_ID": provider_profile.tenant_id,
            "QE_NATIVE_IM_WORKSPACE_ID": provider_profile.workspace_id,
            "QE_NATIVE_IM_CHANNEL_ID": provider_profile.channel_id,
            **environment_changes,
        }
    )
    configuration = NativeIMSandboxConfig.from_environment(values)
    assert type(configuration) is NativeIMInboundOnlyConfigV1
    return configuration


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


def test_disabled_configuration_has_no_endpoint_or_secret_surface() -> None:
    configuration = NativeIMSandboxConfig.from_environment(disabled_environment())

    assert type(configuration) is NativeIMDisabledConfigV1
    assert configuration.enabled is False
    assert len(configuration.fingerprint) == 16
    assert not hasattr(configuration, "origin")
    assert not hasattr(configuration, "credential_ref")


def test_disabled_configuration_rejects_dormant_endpoint_or_secret_fields() -> None:
    canary = "credential-canary-must-not-render"
    values = disabled_environment()
    values["QE_NATIVE_IM_CREDENTIAL_REF"] = canary
    with pytest.raises(NativeIMConfigurationError) as raised:
        NativeIMSandboxConfig.from_environment(values)
    assert raised.value.code == "native_im_configuration_unknown_field"
    assert canary not in f"{raised.value!r} {raised.value}"


def test_enabled_configuration_parses_exact_inbound_only_snapshot() -> None:
    configuration = NativeIMSandboxConfig.from_environment(enabled_environment())

    assert type(configuration) is NativeIMInboundOnlyConfigV1
    assert configuration.enabled is True
    assert configuration.mode == "inbound_only"
    assert configuration.origin.canonical == "https://sandbox.im.example.com:443"
    assert configuration.health_path.canonical == "/v1/health"
    assert configuration.read_path.canonical == "/v1/inbound-events"
    assert configuration.approved_addresses == (
        ipaddress.ip_address("2001:4860:4860::8888"),
        ipaddress.ip_address("8.8.8.8"),
    )
    assert configuration.credential_ref == SecretRef.parse("file://native-im-read-credential")
    assert configuration.verification_secret_ref == SecretRef.parse(
        "file://native-im-verification-key"
    )
    assert configuration.verification_key_id == "test-verification-key-1"
    assert configuration.outbound_mode == "disabled"
    assert configuration.redirect_mode == "deny"


def test_enabled_configuration_repr_and_errors_hide_all_sensitive_values() -> None:
    values = enabled_environment()
    configuration = NativeIMSandboxConfig.from_environment(values)
    rendered = repr(configuration)
    for field_name in (
        "QE_NATIVE_IM_ORIGIN",
        "QE_NATIVE_IM_APPROVED_ADDRESSES",
        "QE_NATIVE_IM_CREDENTIAL_REF",
        "QE_NATIVE_IM_VERIFICATION_SECRET_REF",
        "QE_NATIVE_IM_VERIFICATION_KEY_ID",
        "QE_NATIVE_IM_PROFILE_DIGEST",
    ):
        assert values[field_name] not in rendered
    assert len(configuration.fingerprint) == 16

    canary = "secret-value-canary"
    values["QE_NATIVE_IM_VERIFICATION_SECRET_REF"] = canary
    with pytest.raises(NativeIMConfigurationError) as raised:
        NativeIMSandboxConfig.from_environment(values)
    assert canary not in f"{raised.value!r} {raised.value}"


def test_approval_binding_digest_is_full_stable_and_not_a_diagnostic_fingerprint() -> None:
    configuration = bound_configuration()

    assert configuration.approval_binding_digest == (
        "e01642b1477ea9f35973a3ff3baf76d68fc0eeb7805efbeb75b95e73ac347754"
    )
    assert len(configuration.approval_binding_digest) == 64
    assert configuration.approval_binding_digest != configuration.fingerprint
    assert configuration.approval_binding_digest not in repr(configuration)


@pytest.mark.parametrize(
    ("field_name", "changed"),
    (
        ("profile_id", "other-profile"),
        ("profile_revision", "other-revision"),
        ("profile_digest", "f" * 64),
        ("approval_id", "other-approval"),
        ("approval_expires_at", "2026-09-29T00:00:00.000001Z"),
        ("provider", "other-provider"),
        ("tenant_id", "other-tenant"),
        ("workspace_id", "other-workspace"),
        ("channel_id", "other-channel"),
        ("origin", CanonicalHTTPSOrigin.parse("https://other.im.example.com:443")),
        ("approved_addresses", parse_approved_ip_addresses("1.1.1.1,8.8.8.8")),
        ("health_path", CanonicalAbsolutePath.parse("/v2/health")),
        ("read_path", CanonicalAbsolutePath.parse("/v2/inbound-events")),
        ("credential_ref", SecretRef.parse("file://other-read-credential")),
        ("verification_secret_ref", SecretRef.parse("file://other-verification-key")),
        ("verification_key_id", "other-verification-key"),
        ("page_limit", 99),
        ("max_response_bytes", 8_388_607),
        ("connect_timeout_ms", 4_999),
        ("read_timeout_ms", 29_999),
    ),
)
def test_approval_binding_digest_covers_every_mutable_configuration_field(
    field_name: str,
    changed: object,
) -> None:
    configuration = bound_configuration()
    changed_configuration = replace(configuration, **{field_name: changed})

    assert (
        changed_configuration.approval_binding_digest
        != configuration.approval_binding_digest
    )


def test_environment_snapshot_reads_each_native_field_exactly_once() -> None:
    changing = ChangingEnvironment(enabled_environment())
    configuration = NativeIMSandboxConfig.from_environment(changing)

    assert type(configuration) is NativeIMInboundOnlyConfigV1
    assert all(
        count == 1 for key, count in changing.reads.items() if key.startswith("QE_NATIVE_IM_")
    )
    assert set(changing.reads) == {
        key for key in changing.values if key.startswith("QE_NATIVE_IM_")
    }


def test_parser_ignores_other_namespaces_but_rejects_unknown_native_names() -> None:
    values = disabled_environment()
    values["AWS_SECRET_ACCESS_KEY"] = "not-read"
    values["QE_UNRELATED"] = "owned-by-another-parser"
    NativeIMSandboxConfig.from_environment(values)

    values["QE_NATIVE_IM_FUTURE"] = "must-not-render"
    with pytest.raises(NativeIMConfigurationError) as raised:
        NativeIMSandboxConfig.from_environment(values)
    assert raised.value.code == "native_im_configuration_unknown_field"
    assert "FUTURE" not in str(raised.value)


def test_enabled_configuration_requires_every_exact_field() -> None:
    for field_name in sorted(
        key for key in enabled_environment() if key.startswith("QE_NATIVE_IM_")
    ):
        values = enabled_environment()
        del values[field_name]
        with pytest.raises(NativeIMConfigurationError) as raised:
            NativeIMSandboxConfig.from_environment(values)
        assert raised.value.code == "native_im_configuration_missing_field"


@pytest.mark.parametrize(
    ("field_name", "value", "expected_code"),
    (
        ("QE_NATIVE_IM_CONFIG_VERSION", "2", "native_im_configuration_version_unsupported"),
        ("QE_NATIVE_IM_ENABLED", "yes", "native_im_configuration_boolean_invalid"),
        ("QE_NATIVE_IM_MODE", "outbound", "native_im_mode_forbidden"),
        ("QE_NATIVE_IM_PROFILE_DIGEST", "A" * 64, "native_im_configuration_digest_invalid"),
        (
            "QE_NATIVE_IM_APPROVAL_EXPIRES_AT",
            "2026-09-28T00:00:00Z",
            "native_im_configuration_timestamp_invalid",
        ),
        ("QE_NATIVE_IM_PAGE_LIMIT", "0", "native_im_configuration_integer_out_of_range"),
        ("QE_NATIVE_IM_PAGE_LIMIT", "1001", "native_im_configuration_integer_out_of_range"),
        ("QE_NATIVE_IM_PAGE_LIMIT", "01", "native_im_configuration_integer_invalid"),
        (
            "QE_NATIVE_IM_MAX_RESPONSE_BYTES",
            "1023",
            "native_im_configuration_integer_out_of_range",
        ),
        (
            "QE_NATIVE_IM_CONNECT_TIMEOUT_MS",
            "99",
            "native_im_configuration_integer_out_of_range",
        ),
        (
            "QE_NATIVE_IM_READ_TIMEOUT_MS",
            "120001",
            "native_im_configuration_integer_out_of_range",
        ),
        ("QE_NATIVE_IM_OUTBOUND_MODE", "enabled", "native_im_outbound_mode_forbidden"),
        ("QE_NATIVE_IM_REDIRECT_MODE", "follow", "native_im_redirect_mode_forbidden"),
    ),
)
def test_enabled_configuration_fails_closed_on_scalar_drift(
    field_name: str, value: str, expected_code: str
) -> None:
    values = enabled_environment()
    values[field_name] = value
    with pytest.raises(NativeIMConfigurationError) as raised:
        NativeIMSandboxConfig.from_environment(values)
    assert raised.value.code == expected_code
    assert value not in str(raised.value)


def test_enabled_configuration_requires_distinct_paths_and_secret_purposes() -> None:
    values = enabled_environment()
    values["QE_NATIVE_IM_READ_PATH"] = values["QE_NATIVE_IM_HEALTH_PATH"]
    with pytest.raises(NativeIMConfigurationError) as paths:
        NativeIMSandboxConfig.from_environment(values)
    assert paths.value.code == "native_im_configuration_paths_not_distinct"

    values = enabled_environment()
    values["QE_NATIVE_IM_VERIFICATION_SECRET_REF"] = values["QE_NATIVE_IM_CREDENTIAL_REF"]
    with pytest.raises(NativeIMConfigurationError) as secrets:
        NativeIMSandboxConfig.from_environment(values)
    assert secrets.value.code == "native_im_secret_purpose_alias_forbidden"


def test_enabled_configuration_accepts_only_file_secret_references() -> None:
    values = enabled_environment()
    values["QE_NATIVE_IM_CREDENTIAL_REF"] = "vault://native-im-read-credential"
    with pytest.raises(NativeIMConfigurationError) as raised:
        NativeIMSandboxConfig.from_environment(values)
    assert raised.value.code == "native_im_secret_scheme_forbidden"


def test_direct_config_models_reject_bool_as_integer_and_subclass_values() -> None:
    with pytest.raises(NativeIMConfigurationError):
        NativeIMDisabledConfigV1(schema_version=True, enabled=False)  # type: ignore[arg-type]

    class OriginSubclass(CanonicalHTTPSOrigin):
        pass

    baseline = NativeIMSandboxConfig.from_environment(enabled_environment())
    assert type(baseline) is NativeIMInboundOnlyConfigV1
    with pytest.raises(NativeIMConfigurationError):
        NativeIMInboundOnlyConfigV1(
            **{
                **baseline.__dict__,
                "origin": OriginSubclass(host=baseline.origin.host, port=baseline.origin.port),
            }
        )


def test_preflight_binds_exact_profile_scope_limits_and_unexpired_approval() -> None:
    provider_profile = profile()
    validate_native_im_sandbox_preflight_v1(
        bound_configuration(),
        provider_profile,
        now="2026-08-28T12:00:00.000001Z",
    )


@pytest.mark.parametrize(
    ("field_name", "changed", "expected_code"),
    (
        ("profile_id", "other-profile", "native_im_preflight_profile_mismatch"),
        ("profile_revision", "other-revision", "native_im_preflight_profile_mismatch"),
        ("profile_digest", "f" * 64, "native_im_preflight_profile_mismatch"),
        ("tenant_id", "other-tenant", "native_im_preflight_scope_mismatch"),
        ("workspace_id", "other-workspace", "native_im_preflight_scope_mismatch"),
        ("provider", "other-provider", "native_im_preflight_scope_mismatch"),
        ("channel_id", "other-channel", "native_im_preflight_scope_mismatch"),
    ),
)
def test_preflight_rejects_every_profile_and_scope_binding_drift(
    field_name: str, changed: str, expected_code: str
) -> None:
    with pytest.raises(NativeIMSandboxPreflightError) as raised:
        validate_native_im_sandbox_preflight_v1(
            replace(bound_configuration(), **{field_name: changed}),
            profile(),
            now="2026-08-28T12:00:00.000001Z",
        )
    assert raised.value.code == expected_code
    assert changed not in str(raised.value)


def test_preflight_rejects_expired_or_invalid_injected_time() -> None:
    for now, expected_code in (
        ("2026-09-28T00:00:00.000001Z", "native_im_preflight_approval_expired"),
        ("2026-09-28T00:00:00Z", "native_im_preflight_clock_invalid"),
    ):
        with pytest.raises(NativeIMSandboxPreflightError) as raised:
            validate_native_im_sandbox_preflight_v1(
                bound_configuration(),
                profile(),
                now=now,
            )
        assert raised.value.code == expected_code


def test_preflight_rejects_unready_profile_after_exact_digest_binding() -> None:
    provider_profile = replace(profile(), environment_class="production")
    configuration = replace(
        bound_configuration(),
        profile_digest=provider_profile.canonical_digest(),
    )
    with pytest.raises(NativeIMSandboxPreflightError) as raised:
        validate_native_im_sandbox_preflight_v1(
            configuration,
            provider_profile,
            now="2026-08-28T12:00:00.000001Z",
        )
    assert raised.value.code == "native_im_preflight_profile_not_ready"


def test_preflight_never_expands_provider_page_or_response_limits() -> None:
    for configuration, expected_code in (
        (
            replace(bound_configuration(), page_limit=101),
            "native_im_preflight_page_limit_exceeds_profile",
        ),
        (
            replace(bound_configuration(), max_response_bytes=8 * 1_024 * 1_024 + 1),
            "native_im_preflight_response_limit_exceeds_profile",
        ),
    ):
        with pytest.raises(NativeIMSandboxPreflightError) as raised:
            validate_native_im_sandbox_preflight_v1(
                configuration,
                profile(),
                now="2026-08-28T12:00:00.000001Z",
            )
        assert raised.value.code == expected_code


def test_preflight_rejects_subclasses_before_reading_fields() -> None:
    class ConfigSubclass(NativeIMInboundOnlyConfigV1):
        pass

    class ProfileSubclass(type(profile())):
        pass

    with pytest.raises(TypeError):
        validate_native_im_sandbox_preflight_v1(
            object.__new__(ConfigSubclass),
            profile(),
            now="2026-08-28T12:00:00.000001Z",
        )
    with pytest.raises(TypeError):
        validate_native_im_sandbox_preflight_v1(
            bound_configuration(),
            object.__new__(ProfileSubclass),
            now="2026-08-28T12:00:00.000001Z",
        )
