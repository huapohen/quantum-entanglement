"""Secret references and provider boundaries.

This module never reads ambient credentials. A :class:`SecretRef` identifies material
without containing it and renders only a stable, non-reversible locator fingerprint.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

_REFERENCE = re.compile(
    r"^(?P<scheme>[a-z][a-z0-9-]{0,31})://"
    r"(?P<locator>[A-Za-z0-9][A-Za-z0-9._/-]{0,254})$"
)


class SecretReferenceError(ValueError):
    """A secret reference is not canonical or is unsafe to route."""


@dataclass(frozen=True)
class SecretRef:
    """An immutable reference to secret material, never the material itself."""

    scheme: str
    locator: str

    def __post_init__(self) -> None:
        if type(self.scheme) is not str or type(self.locator) is not str:
            raise TypeError("secret reference scheme and locator must be strings")
        canonical = f"{self.scheme}://{self.locator}"
        match = _REFERENCE.fullmatch(canonical)
        if match is None:
            raise SecretReferenceError("secret reference must use the canonical safe form")
        segments = self.locator.split("/")
        if any(segment in {"", ".", ".."} for segment in segments):
            raise SecretReferenceError("secret reference locator contains an unsafe path segment")

    @classmethod
    def parse(cls, value: str) -> SecretRef:
        """Parse one canonical ``scheme://locator`` reference."""

        if type(value) is not str:
            raise TypeError("secret reference must be a string")
        match = _REFERENCE.fullmatch(value)
        if match is None:
            raise SecretReferenceError("secret reference must use the canonical safe form")
        return cls(scheme=match.group("scheme"), locator=match.group("locator"))

    @property
    def canonical(self) -> str:
        """Return the provider-routing value for explicit trusted use."""

        return f"{self.scheme}://{self.locator}"

    @property
    def fingerprint(self) -> str:
        """Return a short non-reversible locator identifier for diagnostics."""

        digest = hashlib.sha256(self.locator.encode("utf-8")).hexdigest()[:12]
        return f"{self.scheme}:{digest}"

    def __str__(self) -> str:
        return f"SecretRef<{self.fingerprint}>"

    def __repr__(self) -> str:
        return f"SecretRef(fingerprint={self.fingerprint!r})"
