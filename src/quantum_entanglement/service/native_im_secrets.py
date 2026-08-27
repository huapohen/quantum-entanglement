"""Purpose-bound secret leases for the native-IM inbound-only boundary."""

from __future__ import annotations

from typing import Protocol

from .native_im_config import NativeIMInboundOnlyConfigV1
from .secrets import SecretMaterial, SecretRef

_SECRET_PURPOSES = {"read_credential", "verification_key"}


class _SecretResolver(Protocol):
    def resolve(self, reference: SecretRef) -> SecretMaterial:
        """Resolve one configured reference into a caller-closed lease."""


class NativeIMSecretLoadError(RuntimeError):
    """A redacted provider or material failure at the purpose boundary."""

    __slots__ = ("code", "purpose")

    def __init__(self, code: str, purpose: str) -> None:
        self.code = code
        self.purpose = purpose
        super().__init__(f"{code} ({purpose})")


class NativeIMSecretLoader:
    """Resolve only the two exact references pinned by one E2 config."""

    __slots__ = ("__configuration", "__provider")

    def __init__(
        self,
        configuration: NativeIMInboundOnlyConfigV1,
        provider: _SecretResolver,
    ) -> None:
        if type(configuration) is not NativeIMInboundOnlyConfigV1:
            raise TypeError("native IM secret loader requires the exact inbound-only config")
        if provider is None:
            raise TypeError("native IM secret loader requires a secret provider")
        self.__configuration = configuration
        self.__provider = provider

    def resolve(self, purpose: str) -> SecretMaterial:
        """Return one caller-owned lease; provider failures never cross this boundary."""

        if type(purpose) is not str or purpose not in _SECRET_PURPOSES:
            raise NativeIMSecretLoadError("native_im_secret_purpose_invalid", "invalid")
        reference = (
            self.__configuration.credential_ref
            if purpose == "read_credential"
            else self.__configuration.verification_secret_ref
        )
        failed = False
        material: object | None = None
        try:
            material = self.__provider.resolve(reference)
        except Exception:
            failed = True
        if failed:
            raise NativeIMSecretLoadError("native_im_secret_provider_failed", purpose) from None
        if type(material) is not SecretMaterial:
            raise NativeIMSecretLoadError("native_im_secret_material_invalid", purpose) from None
        return material

    def __repr__(self) -> str:
        return f"NativeIMSecretLoader(config={self.__configuration.fingerprint!r})"


__all__ = ["NativeIMSecretLoadError", "NativeIMSecretLoader"]
