"""Strict, allowlisted service configuration.

Configuration parsing is explicit: callers pass a mapping, unknown ``QE_*`` names fail,
and values never appear in errors. Filesystem checks are a startup preflight and must be
repeated by the component that opens a path to close the preflight/use race.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from itertools import islice
from pathlib import Path

_INTEGER = re.compile(r"^(?:0|[1-9][0-9]*)$")
_MAX_ENVIRONMENT_ITEMS = 4_096
_MAX_ENVIRONMENT_KEY_LENGTH = 256
_CONFIG_KEYS = frozenset(
    {
        "QE_BIND_HOST",
        "QE_BIND_PORT",
        "QE_CONFIG_VERSION",
        "QE_CONNECTOR",
        "QE_DATABASE_PATH",
        "QE_DATA_DIR",
        "QE_DEBUG",
        "QE_MAX_CONCURRENCY",
        "QE_MAX_REQUEST_BYTES",
        "QE_RUNTIME_MODE",
        "QE_SECRET_ROOT",
        "QE_SHUTDOWN_GRACE_SECONDS",
    }
)


class ConfigurationError(ValueError):
    """A redacted configuration failure with stable code and field name."""

    __slots__ = ("code", "field")

    def __init__(self, code: str, field: str = "configuration") -> None:
        self.code = code
        self.field = field
        super().__init__(f"{code} ({field})")


class RuntimeMode(str, Enum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


@dataclass(frozen=True)
class ServiceConfig:
    """Validated service configuration containing no secret material."""

    config_version: int
    runtime_mode: RuntimeMode
    data_directory: Path
    database_path: Path
    secret_root: Path
    connector: str
    bind_host: str
    bind_port: int
    debug: bool
    max_request_bytes: int
    max_concurrency: int
    shutdown_grace_seconds: int

    def __post_init__(self) -> None:
        if self.config_version != 1:
            raise ConfigurationError("configuration_version_unsupported", "QE_CONFIG_VERSION")
        if not isinstance(self.runtime_mode, RuntimeMode):
            raise ConfigurationError("configuration_type_invalid", "QE_RUNTIME_MODE")
        for field, value in (
            ("QE_DATA_DIR", self.data_directory),
            ("QE_DATABASE_PATH", self.database_path),
            ("QE_SECRET_ROOT", self.secret_root),
        ):
            if not isinstance(value, Path):
                raise ConfigurationError("configuration_type_invalid", field)
        if self.connector != "fake":
            raise ConfigurationError("connector_not_permitted", "QE_CONNECTOR")
        self._validate_loopback(self.bind_host)
        self._validate_bounded_integer(self.bind_port, "QE_BIND_PORT", 1, 65_535)
        if type(self.debug) is not bool:
            raise ConfigurationError("configuration_type_invalid", "QE_DEBUG")
        if self.runtime_mode is RuntimeMode.PRODUCTION and self.debug:
            raise ConfigurationError("production_debug_forbidden", "QE_DEBUG")
        self._validate_bounded_integer(
            self.max_request_bytes,
            "QE_MAX_REQUEST_BYTES",
            1_024,
            16 * 1_024 * 1_024,
        )
        self._validate_bounded_integer(
            self.max_concurrency,
            "QE_MAX_CONCURRENCY",
            1,
            1_024,
        )
        self._validate_bounded_integer(
            self.shutdown_grace_seconds,
            "QE_SHUTDOWN_GRACE_SECONDS",
            1,
            300,
        )
        self._validate_filesystem()

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> ServiceConfig:
        """Build from an explicit environment snapshot without ambient inheritance."""

        if not isinstance(environ, Mapping):
            raise TypeError("environment must be a mapping")
        try:
            keys = tuple(islice(iter(environ), _MAX_ENVIRONMENT_ITEMS + 1))
        except Exception:
            raise ConfigurationError("configuration_snapshot_failed") from None
        if len(keys) > _MAX_ENVIRONMENT_ITEMS:
            raise ConfigurationError("configuration_snapshot_too_large")

        seen: set[str] = set()
        for key in keys:
            if type(key) is not str:
                raise ConfigurationError("configuration_type_invalid")
            if (
                not key
                or len(key) > _MAX_ENVIRONMENT_KEY_LENGTH
                or any(character in key for character in ("\x00", "\r", "\n"))
            ):
                raise ConfigurationError("configuration_key_invalid")
            if key in seen:
                raise ConfigurationError("configuration_duplicate_field")
            seen.add(key)
        if any(key.startswith("QE_") and key not in _CONFIG_KEYS for key in seen):
            raise ConfigurationError("configuration_unknown_field")
        missing = sorted(_CONFIG_KEYS - seen)
        if missing:
            raise ConfigurationError("configuration_missing_field", missing[0])

        snapshot: dict[str, str] = {}
        for key in sorted(_CONFIG_KEYS):
            try:
                value = environ[key]
            except Exception:
                raise ConfigurationError("configuration_snapshot_failed") from None
            if type(value) is not str:
                raise ConfigurationError("configuration_type_invalid")
            snapshot[key] = value

        values = {key: cls._validate_raw_value(key, snapshot[key]) for key in _CONFIG_KEYS}
        try:
            runtime_mode = RuntimeMode(values["QE_RUNTIME_MODE"])
        except ValueError:
            raise ConfigurationError("runtime_mode_invalid", "QE_RUNTIME_MODE") from None

        return cls(
            config_version=cls._parse_integer(values, "QE_CONFIG_VERSION"),
            runtime_mode=runtime_mode,
            data_directory=cls._parse_path(values, "QE_DATA_DIR"),
            database_path=cls._parse_path(values, "QE_DATABASE_PATH"),
            secret_root=cls._parse_path(values, "QE_SECRET_ROOT"),
            connector=values["QE_CONNECTOR"],
            bind_host=values["QE_BIND_HOST"],
            bind_port=cls._parse_integer(values, "QE_BIND_PORT"),
            debug=cls._parse_boolean(values, "QE_DEBUG"),
            max_request_bytes=cls._parse_integer(values, "QE_MAX_REQUEST_BYTES"),
            max_concurrency=cls._parse_integer(values, "QE_MAX_CONCURRENCY"),
            shutdown_grace_seconds=cls._parse_integer(values, "QE_SHUTDOWN_GRACE_SECONDS"),
        )

    @staticmethod
    def _validate_raw_value(field: str, value: str) -> str:
        if not value or value != value.strip() or len(value) > 4_096:
            raise ConfigurationError("configuration_value_invalid", field)
        if any(character in value for character in ("\x00", "\r", "\n")):
            raise ConfigurationError("configuration_value_invalid", field)
        return value

    @staticmethod
    def _parse_integer(values: Mapping[str, str], field: str) -> int:
        value = values[field]
        if _INTEGER.fullmatch(value) is None:
            raise ConfigurationError("configuration_integer_invalid", field)
        return int(value)

    @staticmethod
    def _parse_boolean(values: Mapping[str, str], field: str) -> bool:
        value = values[field]
        if value not in {"true", "false"}:
            raise ConfigurationError("configuration_boolean_invalid", field)
        return value == "true"

    @staticmethod
    def _parse_path(values: Mapping[str, str], field: str) -> Path:
        value = values[field]
        path = Path(value)
        if not path.is_absolute() or os.path.normpath(value) != value:
            raise ConfigurationError("configuration_path_not_canonical", field)
        if any(part in {".", ".."} for part in path.parts):
            raise ConfigurationError("configuration_path_not_canonical", field)
        return path

    @staticmethod
    def _validate_loopback(value: str) -> None:
        if type(value) is not str:
            raise ConfigurationError("configuration_type_invalid", "QE_BIND_HOST")
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            raise ConfigurationError("bind_host_not_literal_loopback", "QE_BIND_HOST") from None
        if not address.is_loopback:
            raise ConfigurationError("bind_host_not_literal_loopback", "QE_BIND_HOST")

    @staticmethod
    def _validate_bounded_integer(value: int, field: str, minimum: int, maximum: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigurationError("configuration_type_invalid", field)
        if value < minimum or value > maximum:
            raise ConfigurationError("configuration_integer_out_of_range", field)

    def _validate_filesystem(self) -> None:
        data = self.data_directory
        database = self.database_path
        secrets = self.secret_root
        if database.parent != data:
            raise ConfigurationError("database_outside_data_directory", "QE_DATABASE_PATH")
        if data == secrets or data in secrets.parents or secrets in data.parents:
            raise ConfigurationError("secret_root_overlaps_data", "QE_SECRET_ROOT")

        self._validate_path_chain(data, "QE_DATA_DIR", allow_missing_leaf=False)
        self._validate_path_chain(secrets, "QE_SECRET_ROOT", allow_missing_leaf=False)
        self._validate_path_chain(database, "QE_DATABASE_PATH", allow_missing_leaf=True)
        self._validate_directory(data, "QE_DATA_DIR")
        self._validate_directory(secrets, "QE_SECRET_ROOT")
        if database.exists():
            self._validate_database(database)

    @staticmethod
    def _validate_path_chain(path: Path, field: str, *, allow_missing_leaf: bool) -> None:
        current = Path(path.anchor)
        for index, part in enumerate(path.parts[1:], start=1):
            current /= part
            try:
                metadata = current.lstat()
            except FileNotFoundError:
                is_leaf = index == len(path.parts) - 1
                if allow_missing_leaf and is_leaf:
                    return
                raise ConfigurationError("configuration_path_missing", field) from None
            except OSError:
                raise ConfigurationError("configuration_path_unavailable", field) from None
            if stat.S_ISLNK(metadata.st_mode):
                raise ConfigurationError("configuration_path_symlink", field)
            is_leaf = index == len(path.parts) - 1
            if not is_leaf and stat.S_ISDIR(metadata.st_mode):
                if not ServiceConfig._has_trusted_ancestor_owner(metadata):
                    raise ConfigurationError("configuration_path_ancestor_owner", field)
                if (
                    stat.S_IMODE(metadata.st_mode) & 0o022
                    and not ServiceConfig._is_protected_writable_ancestor(metadata)
                ):
                    raise ConfigurationError("configuration_path_ancestor_permissions", field)

    @staticmethod
    def _has_trusted_ancestor_owner(metadata: os.stat_result) -> bool:
        if os.name != "posix":
            return True
        get_effective_uid = getattr(os, "geteuid", None)
        if get_effective_uid is None:
            return False
        return metadata.st_uid in {0, get_effective_uid()}

    @staticmethod
    def _is_protected_writable_ancestor(metadata: os.stat_result) -> bool:
        """Return whether POSIX sticky semantics protect a writable ancestor entry."""

        if os.name != "posix" or not metadata.st_mode & stat.S_ISVTX:
            return False
        return ServiceConfig._has_trusted_ancestor_owner(metadata)

    @classmethod
    def _validate_directory(cls, path: Path, field: str) -> None:
        try:
            metadata = path.lstat()
        except OSError:
            raise ConfigurationError("configuration_path_unavailable", field) from None
        if not stat.S_ISDIR(metadata.st_mode):
            raise ConfigurationError("configuration_path_not_directory", field)
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ConfigurationError("configuration_path_permissions", field)
        cls._validate_owner(metadata, field)

    @classmethod
    def _validate_database(cls, path: Path) -> None:
        try:
            metadata = path.lstat()
        except OSError:
            raise ConfigurationError("configuration_path_unavailable", "QE_DATABASE_PATH") from None
        if not stat.S_ISREG(metadata.st_mode):
            raise ConfigurationError("database_not_regular_file", "QE_DATABASE_PATH")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ConfigurationError("configuration_path_permissions", "QE_DATABASE_PATH")
        if metadata.st_nlink != 1:
            raise ConfigurationError("database_link_count_unsafe", "QE_DATABASE_PATH")
        cls._validate_owner(metadata, "QE_DATABASE_PATH")

    @staticmethod
    def _validate_owner(metadata: os.stat_result, field: str) -> None:
        get_effective_uid = getattr(os, "geteuid", None)
        if get_effective_uid is not None and metadata.st_uid != get_effective_uid():
            raise ConfigurationError("configuration_path_owner", field)

    @property
    def fingerprint(self) -> str:
        """Return a stable identifier without rendering full filesystem paths."""

        values = (
            str(self.config_version),
            self.runtime_mode.value,
            hashlib.sha256(os.fsencode(self.data_directory)).hexdigest(),
            hashlib.sha256(os.fsencode(self.database_path)).hexdigest(),
            hashlib.sha256(os.fsencode(self.secret_root)).hexdigest(),
            self.connector,
            self.bind_host,
            str(self.bind_port),
            str(self.debug),
            str(self.max_request_bytes),
            str(self.max_concurrency),
            str(self.shutdown_grace_seconds),
        )
        return hashlib.sha256("\x00".join(values).encode("utf-8")).hexdigest()[:16]

    def __repr__(self) -> str:
        return f"ServiceConfig(mode={self.runtime_mode.value!r}, fingerprint={self.fingerprint!r})"
