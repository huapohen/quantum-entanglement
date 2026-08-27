from __future__ import annotations

import pytest

from quantum_entanglement.service.native_im_config import (
    NativeIMDisabledConfigV1,
    NativeIMInboundOnlyConfigV1,
)
from quantum_entanglement.service.native_im_secrets import (
    NativeIMSecretLoader,
    NativeIMSecretLoadError,
)
from quantum_entanglement.service.secrets import SecretMaterial, SecretRef
from tests.test_native_im_sandbox_config import bound_configuration


class RecordingProvider:
    def __init__(self) -> None:
        self.references: list[SecretRef] = []

    def resolve(self, reference: SecretRef) -> SecretMaterial:
        self.references.append(reference)
        return SecretMaterial(f"material-for-{len(self.references)}".encode())


class HostileProvider:
    def __init__(self, canary: str) -> None:
        self.canary = canary
        self.calls = 0

    def resolve(self, reference: SecretRef) -> SecretMaterial:
        self.calls += 1
        raise RuntimeError(self.canary)

    def __repr__(self) -> str:
        return self.canary


def test_loader_resolves_only_the_reference_bound_to_each_exact_purpose() -> None:
    configuration = bound_configuration()
    provider = RecordingProvider()
    loader = NativeIMSecretLoader(configuration, provider)

    read_material = loader.resolve("read_credential")
    verification_material = loader.resolve("verification_key")
    try:
        assert provider.references == [
            configuration.credential_ref,
            configuration.verification_secret_ref,
        ]
        assert read_material.view().tobytes() == b"material-for-1"
        assert verification_material.view().tobytes() == b"material-for-2"
    finally:
        read_material.close()
        verification_material.close()


def test_secret_material_remains_a_short_caller_closed_lease() -> None:
    provider = RecordingProvider()
    material = NativeIMSecretLoader(bound_configuration(), provider).resolve("read_credential")

    with material as view:
        assert view.readonly
        assert view.tobytes() == b"material-for-1"
    assert material.closed is True
    assert view.tobytes() == bytes(len(view))


def test_invalid_purpose_fails_before_provider_or_config_reference_selection() -> None:
    canary = "invalid-purpose-canary"
    provider = RecordingProvider()
    loader = NativeIMSecretLoader(bound_configuration(), provider)

    for purpose in (canary, "credential", "verification", None, 1):
        with pytest.raises(NativeIMSecretLoadError) as raised:
            loader.resolve(purpose)  # type: ignore[arg-type]
        assert raised.value.code == "native_im_secret_purpose_invalid"
        assert canary not in str(raised.value)
    assert provider.references == []


def test_hostile_provider_failure_has_no_value_cause_context_or_provider_rendering() -> None:
    canary = "hostile-provider-secret-canary"
    provider = HostileProvider(canary)
    loader = NativeIMSecretLoader(bound_configuration(), provider)

    with pytest.raises(NativeIMSecretLoadError) as raised:
        loader.resolve("verification_key")

    rendered = f"{raised.value!r} {raised.value} {loader!r}"
    assert provider.calls == 1
    assert raised.value.code == "native_im_secret_provider_failed"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert canary not in rendered


def test_wrong_provider_return_type_is_rejected_without_coercion() -> None:
    class WrongProvider:
        def resolve(self, reference: SecretRef) -> object:
            return b"plaintext-must-not-be-accepted"

    with pytest.raises(NativeIMSecretLoadError) as raised:
        NativeIMSecretLoader(bound_configuration(), WrongProvider()).resolve("read_credential")
    assert raised.value.code == "native_im_secret_material_invalid"
    assert "plaintext" not in str(raised.value)


def test_loader_constructor_requires_exact_enabled_config_without_touching_provider() -> None:
    class ConfigSubclass(NativeIMInboundOnlyConfigV1):
        pass

    canary = "provider-repr-must-not-run"
    provider = HostileProvider(canary)
    for configuration in (
        NativeIMDisabledConfigV1(schema_version=1, enabled=False),
        object.__new__(ConfigSubclass),
    ):
        with pytest.raises(TypeError):
            NativeIMSecretLoader(configuration, provider)  # type: ignore[arg-type]
    assert provider.calls == 0


def test_loader_repr_exposes_only_config_fingerprint() -> None:
    configuration = bound_configuration()
    canary = "provider-render-canary"
    loader = NativeIMSecretLoader(configuration, HostileProvider(canary))
    rendered = repr(loader)

    assert configuration.fingerprint in rendered
    assert configuration.credential_ref.locator not in rendered
    assert configuration.verification_secret_ref.locator not in rendered
    assert canary not in rendered
