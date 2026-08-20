"""Production service boundary primitives.

The package is intentionally not a network-service entry point yet. Components are
introduced behind tested fail-closed contracts before they are composed into a service.
"""

from .secrets import (
    SecretMaterial,
    SecretMaterialClosedError,
    SecretRef,
    SecretReferenceError,
)

__all__ = [
    "SecretMaterial",
    "SecretMaterialClosedError",
    "SecretRef",
    "SecretReferenceError",
]
