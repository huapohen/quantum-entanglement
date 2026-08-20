"""Typed, allowlisted operational logging.

No API accepts free-form log text or exceptions. Event schemas are fixed in code and every
field is converted to a bounded primitive or a one-way identifier hash before a record is
handed to the standard logging framework.
"""

from __future__ import annotations

import hashlib
import json
import logging as standard_logging
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from itertools import islice
from types import MappingProxyType
from typing import Any

_EVENT_CODE = re.compile(r"^qe(?:\.[a-z][a-z0-9_]*){2,8}$")
_FIELD_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_REJECTED_RECORD = '{"event":"qe.logging.event_rejected","fields":{}}'
_MAX_SCHEMAS = 512
_MAX_FIELDS = 32


class LogFieldKind(str, Enum):
    BOOLEAN = "boolean"
    COUNT = "count"
    DURATION_MS = "duration_ms"
    CODE = "code"
    IDENTIFIER_HASH = "identifier_hash"
    DIGEST = "digest"


@dataclass(frozen=True)
class LogField:
    """One field admitted by an operational event schema."""

    name: str
    kind: LogFieldKind
    required: bool = True
    allowed_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.name) is not str or _FIELD_NAME.fullmatch(self.name) is None:
            raise ValueError("log field name is invalid")
        if not isinstance(self.kind, LogFieldKind):
            raise TypeError("log field kind must be LogFieldKind")
        if type(self.required) is not bool:
            raise TypeError("log field required flag must be boolean")
        if type(self.allowed_codes) is not tuple or len(self.allowed_codes) > 64:
            raise TypeError("log field allowed codes must be a bounded tuple")
        if self.kind is LogFieldKind.CODE:
            if not self.allowed_codes:
                raise ValueError("code log field requires an explicit value allowlist")
            if any(
                type(value) is not str or _SAFE_CODE.fullmatch(value) is None
                for value in self.allowed_codes
            ):
                raise ValueError("code log field allowlist is invalid")
            if len(set(self.allowed_codes)) != len(self.allowed_codes):
                raise ValueError("code log field values must be unique")
        elif self.allowed_codes:
            raise ValueError("only code log fields may define allowed codes")


@dataclass(frozen=True)
class LogEventSchema:
    """Fixed event code, severity and exact typed field allowlist."""

    event_code: str
    level: int
    fields: tuple[LogField, ...] = ()

    def __post_init__(self) -> None:
        if type(self.event_code) is not str or _EVENT_CODE.fullmatch(self.event_code) is None:
            raise ValueError("log event code is invalid")
        if self.level not in {
            standard_logging.DEBUG,
            standard_logging.INFO,
            standard_logging.WARNING,
            standard_logging.ERROR,
            standard_logging.CRITICAL,
        }:
            raise ValueError("log event level is invalid")
        if type(self.fields) is not tuple or len(self.fields) > _MAX_FIELDS:
            raise ValueError("log event field list is invalid")
        names: set[str] = set()
        for field in self.fields:
            if not isinstance(field, LogField):
                raise TypeError("log event fields must be LogField instances")
            if field.name in names:
                raise ValueError("log event field names must be unique")
            names.add(field.name)


class SafeLogCatalog:
    """Immutable bounded snapshot of registered operational events."""

    __slots__ = ("__schemas",)

    def __init__(self, schemas: Iterable[LogEventSchema]) -> None:
        try:
            snapshot = tuple(islice(iter(schemas), _MAX_SCHEMAS + 1))
        except Exception:
            raise ValueError("log schema catalog cannot be read") from None
        if len(snapshot) > _MAX_SCHEMAS:
            raise ValueError("log schema catalog is too large")
        values: dict[str, LogEventSchema] = {}
        for schema in snapshot:
            if not isinstance(schema, LogEventSchema):
                raise TypeError("log catalog entries must be LogEventSchema")
            if schema.event_code in values:
                raise ValueError("log event codes must be unique")
            values[schema.event_code] = schema
        self.__schemas = MappingProxyType(values)

    def get(self, event_code: str) -> LogEventSchema | None:
        return self.__schemas.get(event_code)

    def __len__(self) -> int:
        return len(self.__schemas)

    def __repr__(self) -> str:
        return f"SafeLogCatalog(size={len(self)})"


class SafeLogger:
    """Emit canonical JSON only after an event passes its exact typed schema."""

    __slots__ = ("__catalog", "__logger")

    def __init__(self, logger: standard_logging.Logger, catalog: SafeLogCatalog) -> None:
        if not isinstance(logger, standard_logging.Logger):
            raise TypeError("logger must be logging.Logger")
        if not isinstance(catalog, SafeLogCatalog):
            raise TypeError("catalog must be SafeLogCatalog")
        self.__logger = logger
        self.__catalog = catalog

    def emit(self, event_code: str, fields: dict[str, Any] | None = None) -> bool:
        """Emit one event, or a constant rejection record when validation fails."""

        try:
            if type(event_code) is not str:
                return self._emit_rejected()
            schema = self.__catalog.get(event_code)
            if schema is None:
                return self._emit_rejected()
            if fields is None:
                snapshot: dict[str, Any] = {}
            elif type(fields) is dict:
                snapshot = fields.copy()
            else:
                return self._emit_rejected()
            rendered = self._render_fields(schema, snapshot)
            record = json.dumps(
                {"event": schema.event_code, "fields": rendered},
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except Exception:
            return self._emit_rejected()
        try:
            self.__logger.log(schema.level, record)
        except Exception:
            return False
        return True

    def _emit_rejected(self) -> bool:
        try:
            self.__logger.error(_REJECTED_RECORD)
        except Exception:
            return False
        return False

    @classmethod
    def _render_fields(cls, schema: LogEventSchema, values: dict[str, Any]) -> dict[str, Any]:
        if any(type(key) is not str for key in values):
            raise ValueError("log field keys must be strings")
        specifications = {field.name: field for field in schema.fields}
        if set(values) - set(specifications):
            raise ValueError("log event contains an unknown field")
        if any(field.required and field.name not in values for field in schema.fields):
            raise ValueError("log event is missing a required field")
        return {
            field.name: cls._render_field(field, values[field.name])
            for field in schema.fields
            if field.name in values
        }

    @staticmethod
    def _render_field(field: LogField, value: Any) -> Any:
        kind = field.kind
        if kind is LogFieldKind.BOOLEAN:
            if type(value) is not bool:
                raise TypeError("boolean log field is invalid")
            return value
        if kind is LogFieldKind.COUNT:
            if type(value) is not int or value < 0 or value > 2**63 - 1:
                raise TypeError("count log field is invalid")
            return value
        if kind is LogFieldKind.DURATION_MS:
            if type(value) not in {int, float}:
                raise TypeError("duration log field is invalid")
            rendered = float(value)
            if not math.isfinite(rendered) or rendered < 0 or rendered > 86_400_000:
                raise ValueError("duration log field is invalid")
            return round(rendered, 3)
        if kind is LogFieldKind.CODE:
            if type(value) is not str or value not in field.allowed_codes:
                raise ValueError("code log field is invalid")
            return value
        if kind is LogFieldKind.IDENTIFIER_HASH:
            if (
                type(value) is not str
                or not value
                or value != value.strip()
                or len(value) > 256
                or any(character in value for character in ("\x00", "\r", "\n"))
            ):
                raise ValueError("identifier log field is invalid")
            digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
            return f"sha256:{digest}"
        if kind is LogFieldKind.DIGEST:
            if type(value) is not str or _DIGEST.fullmatch(value) is None:
                raise ValueError("digest log field is invalid")
            return value
        raise TypeError("log field kind is unsupported")

    def __repr__(self) -> str:
        return "SafeLogger(<redacted>)"
