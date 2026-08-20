"""Secret references and provider boundaries.

This module never reads ambient credentials. A :class:`SecretRef` identifies material
without containing it and renders only a stable, non-reversible locator fingerprint.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, Protocol, SupportsIndex

_REFERENCE = re.compile(
    r"^(?P<scheme>[a-z][a-z0-9-]{0,31})://"
    r"(?P<locator>[A-Za-z0-9][A-Za-z0-9._/-]{0,254})$"
)


class SecretReferenceError(ValueError):
    """A secret reference is not canonical or is unsafe to route."""


class SecretMaterialClosedError(RuntimeError):
    """Secret material was accessed after its lease was closed."""


class SecretProviderError(RuntimeError):
    """A redacted provider failure with a stable machine-readable code."""

    __slots__ = ("code", "reference_fingerprint")

    def __init__(self, code: str, reference: SecretRef) -> None:
        self.code = code
        self.reference_fingerprint = reference.fingerprint
        super().__init__(f"{code} ({self.reference_fingerprint})")


class SecretProvider(Protocol):
    """Resolve an opaque reference into one explicitly closed material lease."""

    def resolve(self, reference: SecretRef) -> SecretMaterial:
        """Return bounded material or raise a redacted provider error."""


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

    @classmethod
    def _from_owned_buffer(cls, value: bytearray) -> SecretMaterial:
        if type(value) is not bytearray:
            raise TypeError("owned secret material must be a bytearray")
        if not value:
            raise ValueError("secret material must not be empty")
        if len(value) > cls.MAX_BYTES:
            raise ValueError("secret material exceeds the configured maximum")
        instance = cls.__new__(cls)
        instance.__buffer = value
        instance.__closed = False
        return instance

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

    def __reduce_ex__(self, protocol: SupportsIndex) -> NoReturn:
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


class FileSecretProvider:
    """Read direct-child secret files through a race-resistant owner-only boundary."""

    __slots__ = ("__root", "__root_fingerprint", "__maximum_bytes")

    def __init__(self, root: Path, *, maximum_bytes: int = SecretMaterial.MAX_BYTES) -> None:
        if not isinstance(root, Path):
            raise TypeError("secret root must be a pathlib.Path")
        if not root.is_absolute():
            raise ValueError("secret root must be absolute")
        if isinstance(maximum_bytes, bool) or not isinstance(maximum_bytes, int):
            raise TypeError("maximum secret bytes must be an integer")
        if maximum_bytes < 1 or maximum_bytes > SecretMaterial.MAX_BYTES:
            raise ValueError("maximum secret bytes is outside the supported range")
        if (
            not getattr(os, "O_NOFOLLOW", 0)
            or not getattr(os, "O_DIRECTORY", 0)
            or os.open not in os.supports_dir_fd
        ):
            raise RuntimeError("secure file secret primitives are unavailable")
        self.__root = root
        self.__root_fingerprint = hashlib.sha256(os.fsencode(root)).hexdigest()[:12]
        self.__maximum_bytes = maximum_bytes

    def resolve(self, reference: SecretRef) -> SecretMaterial:
        if not isinstance(reference, SecretRef):
            raise TypeError("secret reference must be SecretRef")
        if reference.scheme != "file":
            raise SecretProviderError("secret_scheme_unsupported", reference)
        if "/" in reference.locator:
            raise SecretProviderError("secret_locator_unsafe", reference)

        root_descriptor = -1
        secret_descriptor = -1
        owned = bytearray()
        try:
            root_descriptor = os.open(self.__root, self._root_open_flags())
            root_stat = os.fstat(root_descriptor)
            self._validate_root(root_stat, reference)

            secret_descriptor = os.open(
                reference.locator,
                self._secret_open_flags(),
                dir_fd=root_descriptor,
            )
            before = os.fstat(secret_descriptor)
            self._validate_secret_file(before, reference)
            while len(owned) <= self.__maximum_bytes:
                chunk = os.read(secret_descriptor, self.__maximum_bytes + 1 - len(owned))
                if not chunk:
                    break
                owned.extend(chunk)
            after = os.fstat(secret_descriptor)
            self._validate_stable_file(before, after, reference)
            if not owned:
                raise SecretProviderError("secret_empty", reference)
            if len(owned) > self.__maximum_bytes:
                raise SecretProviderError("secret_too_large", reference)
            return SecretMaterial._from_owned_buffer(owned)
        except SecretProviderError:
            self._wipe(owned)
            raise
        except OSError:
            self._wipe(owned)
            raise SecretProviderError("secret_unavailable", reference) from None
        finally:
            if secret_descriptor >= 0:
                self._close_descriptor(secret_descriptor)
            if root_descriptor >= 0:
                self._close_descriptor(root_descriptor)

    @staticmethod
    def _root_open_flags() -> int:
        return (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )

    @staticmethod
    def _secret_open_flags() -> int:
        return (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )

    @staticmethod
    def _current_uid() -> int:
        get_effective_uid = getattr(os, "geteuid", None)
        return int(get_effective_uid()) if get_effective_uid is not None else -1

    @classmethod
    def _validate_root(cls, value: os.stat_result, reference: SecretRef) -> None:
        if not stat.S_ISDIR(value.st_mode):
            raise SecretProviderError("secret_root_unsafe", reference)
        if stat.S_IMODE(value.st_mode) & 0o077:
            raise SecretProviderError("secret_root_unsafe", reference)
        current_uid = cls._current_uid()
        if current_uid >= 0 and value.st_uid != current_uid:
            raise SecretProviderError("secret_root_unsafe", reference)

    @classmethod
    def _validate_secret_file(cls, value: os.stat_result, reference: SecretRef) -> None:
        if not stat.S_ISREG(value.st_mode):
            raise SecretProviderError("secret_file_unsafe", reference)
        if stat.S_IMODE(value.st_mode) & 0o077:
            raise SecretProviderError("secret_file_unsafe", reference)
        if value.st_nlink != 1:
            raise SecretProviderError("secret_file_unsafe", reference)
        current_uid = cls._current_uid()
        if current_uid >= 0 and value.st_uid != current_uid:
            raise SecretProviderError("secret_file_unsafe", reference)

    @staticmethod
    def _validate_stable_file(
        before: os.stat_result,
        after: os.stat_result,
        reference: SecretRef,
    ) -> None:
        identity = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if any(getattr(before, field) != getattr(after, field) for field in identity):
            raise SecretProviderError("secret_changed_during_read", reference)

    @staticmethod
    def _wipe(value: bytearray) -> None:
        for index in range(len(value)):
            value[index] = 0

    @staticmethod
    def _close_descriptor(descriptor: int) -> None:
        try:
            os.close(descriptor)
        except OSError:
            # A read-only descriptor close failure cannot make material safe to retry.
            pass

    def __str__(self) -> str:
        return f"FileSecretProvider<root:{self.__root_fingerprint}>"

    def __repr__(self) -> str:
        return str(self)
