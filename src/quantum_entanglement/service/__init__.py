"""Production service boundary primitives.

The package is intentionally not a network-service entry point yet. Components are
introduced behind tested fail-closed contracts before they are composed into a service.
"""

from .config import ConfigurationError, RuntimeMode, ServiceConfig
from .logging import (
    LogEventSchema,
    LogField,
    LogFieldKind,
    SafeLogCatalog,
    SafeLogger,
)
from .native_im_config import (
    CanonicalAbsolutePath,
    CanonicalHTTPSOrigin,
    NativeIMConfigurationError,
    NativeIMConfigV1,
    NativeIMDisabledConfigV1,
    NativeIMInboundOnlyConfigV1,
    NativeIMSandboxConfig,
    NativeIMSandboxPreflightError,
    parse_approved_ip_addresses,
    validate_native_im_sandbox_preflight_v1,
)
from .redaction import RedactionPolicy, Redactor
from .secrets import (
    FileSecretProvider,
    SecretMaterial,
    SecretMaterialClosedError,
    SecretProvider,
    SecretProviderError,
    SecretRef,
    SecretReferenceError,
)

__all__ = [
    "ConfigurationError",
    "CanonicalAbsolutePath",
    "CanonicalHTTPSOrigin",
    "FileSecretProvider",
    "LogEventSchema",
    "LogField",
    "LogFieldKind",
    "NativeIMConfigV1",
    "NativeIMConfigurationError",
    "NativeIMDisabledConfigV1",
    "NativeIMInboundOnlyConfigV1",
    "NativeIMSandboxConfig",
    "NativeIMSandboxPreflightError",
    "RuntimeMode",
    "RedactionPolicy",
    "Redactor",
    "SecretMaterial",
    "SecretMaterialClosedError",
    "SecretProvider",
    "SecretProviderError",
    "SecretRef",
    "SecretReferenceError",
    "SafeLogCatalog",
    "SafeLogger",
    "ServiceConfig",
    "parse_approved_ip_addresses",
    "validate_native_im_sandbox_preflight_v1",
]
