"""Production service boundary primitives.

The package is intentionally not a network-service entry point yet. Components are
introduced behind tested fail-closed contracts before they are composed into a service.
"""

from .config import ConfigurationError, RuntimeMode, ServiceConfig
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
    "FileSecretProvider",
    "RuntimeMode",
    "RedactionPolicy",
    "Redactor",
    "SecretMaterial",
    "SecretMaterialClosedError",
    "SecretProvider",
    "SecretProviderError",
    "SecretRef",
    "SecretReferenceError",
    "ServiceConfig",
]
