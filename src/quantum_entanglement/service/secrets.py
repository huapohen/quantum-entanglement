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


class SecretMaterialClosedError(RuntimeError):
    """Secret material was accessed after its lease was closed."""


class SecretMaterial:
    """A bounded, redacted byte buffer that is wiped when its lease closes.

    Python cannot revoke copies deliberately made by an adapter. The read-only view keeps
    the normal path explicit and ensures retained views observe the zeroed backing buffer.
    """

    __slots__ = ("__buffer", "__closed")

    MAX_BYTES = 65_536

    def __init__(self, value: bytes) -> None:
        if type(value) is not bytes:
            raise TypeError("secret material must be bytes")
        if not value:
            raise ValueError("secret material must not be empty")
        if len(value) > self.MAX_BYTES:
            raise ValueError("secret material exceeds the configured maximum")
        self.__buffer = bytearray(value)
        self.__closed = False

    @property
    def closed(self) -> bool:
        return self.__closed

    def view(self) -> memoryview:
        """Return a read-only view whose backing bytes are wiped by :meth:`close`."""

        if self.__closed:
            raise SecretMaterialClosedError("secret material lease is closed")
        return memoryview(self.__buffer).toreadonly()

    def close(self) -> None:
        """Overwrite the owned buffer. The operation is idempotent."""

        if self.__closed:
            return
        for index in range(len(self.__buffer)):
            self.__buffer[index] = 0
        self.__closed = True

    def __enter__(self) -> memoryview:
        return self.view()

    def __exit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        self.close()

    def __str__(self) -> str:
        return "SecretMaterial<redacted>"

    def __repr__(self) -> str:
        state = "closed" if self.__closed else "open"
        return f"SecretMaterial(<redacted>, state={state!r})"

    def __copy__(self) -> SecretMaterial:
        raise TypeError("secret material cannot be copied")

    def __deepcopy__(self, memo: object) -> SecretMaterial:
        raise TypeError("secret material cannot be copied")

    def __reduce_ex__(self, protocol: int) -> object:
        raise TypeError("secret material cannot be serialized")

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            # Finalizers must never mask interpreter shutdown or allocation failures.
            pass


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
