"""Fail-closed redaction for bounded diagnostic structures.

Operational logs should use typed allowlisted fields instead of arbitrary values. This
redactor is the final containment layer for diagnostic structures that still need a safe
JSON representation; exceptions and unknown objects are never stringified.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from itertools import islice
from typing import Any

_SAFE_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_MINIMUM_INTEGER = -(2**63)
_MAXIMUM_INTEGER = 2**63 - 1
_SENSITIVE_KEYS = frozenset(
    {
        "accesstoken",
        "apikey",
        "artifact",
        "authorization",
        "body",
        "cookie",
        "credential",
        "headers",
        "leasetoken",
        "password",
        "passwd",
        "payload",
        "privatekey",
        "prompt",
        "proxyauthorization",
        "refreshtoken",
        "secret",
        "setcookie",
        "token",
    }
)
_SENSITIVE_KEY_FRAGMENTS = (
    "accesskey",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "password",
    "passwd",
    "privatekey",
    "secret",
    "token",
)
_TEXT_PATTERNS = (
    (
        re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/-]{4,}=*"),
        "authorization <redacted>",
    ),
    (
        re.compile(r"(?i)\bhttps?://[^\s/@:]+:[^\s/@]+@"),
        "https://<redacted>@",
    ),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"), "sk-<redacted>"),
    (
        re.compile(r"\b[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b"),
        "<redacted-jwt>",
    ),
)


@dataclass(frozen=True)
class RedactionPolicy:
    """Hard bounds for one diagnostic sanitization operation."""

    maximum_depth: int = 6
    maximum_items: int = 64
    maximum_string: int = 256
    maximum_nodes: int = 1_024

    def __post_init__(self) -> None:
        for field, value, upper in (
            ("maximum_depth", self.maximum_depth, 32),
            ("maximum_items", self.maximum_items, 1_024),
            ("maximum_string", self.maximum_string, 4_096),
            ("maximum_nodes", self.maximum_nodes, 100_000),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{field} must be an integer")
            if value < 1 or value > upper:
                raise ValueError(f"{field} is outside the supported range")


class Redactor:
    """Convert a bounded subset of Python values into redacted JSON-safe data."""

    __slots__ = ("policy",)

    def __init__(self, policy: RedactionPolicy | None = None) -> None:
        self.policy = policy or RedactionPolicy()

    def sanitize(self, value: Any) -> Any:
        """Return a JSON-safe result; internal failure returns a constant marker."""

        try:
            return self._sanitize(
                value,
                depth=0,
                active=set(),
                remaining_nodes=[self.policy.maximum_nodes],
            )
        except Exception:
            return "<redaction-failed>"

    def _sanitize(
        self,
        value: Any,
        *,
        depth: int,
        active: set[int],
        remaining_nodes: list[int],
    ) -> Any:
        if remaining_nodes[0] == 0:
            return "<redacted:node-budget>"
        remaining_nodes[0] -= 1
        value_type = type(value)
        if value is None or value_type is bool:
            return value
        if value_type is int:
            if _MINIMUM_INTEGER <= value <= _MAXIMUM_INTEGER:
                return value
            return "<redacted:integer-out-of-range>"
        if value_type is float:
            return value if math.isfinite(value) else "<redacted:non-finite-number>"
        if value_type is str:
            return self._sanitize_text(value)
        if value_type is bytes or value_type is bytearray or value_type is memoryview:
            return "<redacted:bytes>"
        if isinstance(value, BaseException):
            return {"errorType": self._safe_type_name(value)}
        if depth >= self.policy.maximum_depth:
            return "<redacted:depth-limit>"
        if value_type is dict:
            return self._sanitize_dict(
                value,
                depth=depth,
                active=active,
                remaining_nodes=remaining_nodes,
            )
        if value_type is list or value_type is tuple:
            return self._sanitize_sequence(
                value,
                depth=depth,
                active=active,
                remaining_nodes=remaining_nodes,
            )
        return "<redacted:object>"

    def _sanitize_dict(
        self,
        value: dict[Any, Any],
        *,
        depth: int,
        active: set[int],
        remaining_nodes: list[int],
    ) -> Any:
        identity = id(value)
        if identity in active:
            return "<redacted:cycle>"
        active.add(identity)
        try:
            items = tuple(islice(value.items(), self.policy.maximum_items))
            output: dict[str, Any] = {}
            processed = 0
            for index, (key, item) in enumerate(items):
                if remaining_nodes[0] == 0:
                    break
                if type(key) is not str or _SAFE_KEY.fullmatch(key) is None:
                    safe_key = f"invalidField{index}"
                else:
                    safe_key = key
                if self._is_sensitive_key(safe_key):
                    remaining_nodes[0] -= 1
                    output[safe_key] = "<redacted>"
                else:
                    output[safe_key] = self._sanitize(
                        item,
                        depth=depth + 1,
                        active=active,
                        remaining_nodes=remaining_nodes,
                    )
                processed += 1
            if len(value) > processed:
                output["truncatedFields"] = len(value) - processed
            if remaining_nodes[0] == 0 and processed < len(value):
                output["redactionBudgetExceeded"] = True
            return output
        finally:
            active.remove(identity)

    def _sanitize_sequence(
        self,
        value: list[Any] | tuple[Any, ...],
        *,
        depth: int,
        active: set[int],
        remaining_nodes: list[int],
    ) -> Any:
        identity = id(value)
        if identity in active:
            return "<redacted:cycle>"
        active.add(identity)
        try:
            output = []
            for item in value[: self.policy.maximum_items]:
                if remaining_nodes[0] == 0:
                    break
                output.append(
                    self._sanitize(
                        item,
                        depth=depth + 1,
                        active=active,
                        remaining_nodes=remaining_nodes,
                    )
                )
            if len(value) > len(output):
                if remaining_nodes[0] == 0:
                    output.append("<redacted:node-budget>")
                else:
                    output.append(f"<truncated:{len(value) - len(output)}>")
            return output
        finally:
            active.remove(identity)

    def _sanitize_text(self, value: str) -> str:
        scan_limit = max(1_024, self.policy.maximum_string * 4)
        rendered = value[:scan_limit]
        for pattern, replacement in _TEXT_PATTERNS:
            rendered = pattern.sub(replacement, rendered)
        if len(rendered) > self.policy.maximum_string:
            return rendered[: self.policy.maximum_string] + "<truncated>"
        if len(value) > scan_limit:
            return rendered + "<truncated>"
        return rendered

    @staticmethod
    def _is_sensitive_key(value: str) -> bool:
        normalized = re.sub(r"[^a-z0-9]", "", value.lower())
        return normalized in _SENSITIVE_KEYS or any(
            fragment in normalized for fragment in _SENSITIVE_KEY_FRAGMENTS
        )

    @staticmethod
    def _safe_type_name(value: BaseException) -> str:
        name = type(value).__name__
        if type(name) is str and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name):
            return name
        return "Exception"

    def __repr__(self) -> str:
        return f"Redactor(policy={self.policy!r})"
