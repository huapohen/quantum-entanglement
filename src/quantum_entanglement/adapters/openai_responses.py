"""Dependency-free OpenAI-compatible Responses API runtime.

The adapter intentionally uses only the Python standard library.  It targets providers
that expose ``POST /responses`` and require ``stream=true``; Server-Sent Events are
consumed incrementally and bounded before their text is mapped to :class:`AgentResult`.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import socket
from collections.abc import Mapping
from dataclasses import dataclass, field
from http.client import HTTPException
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from ..agent_runtime import (
    AgentCancellationUnsupportedError,
    AgentInvocation,
    AgentResult,
    AgentRuntimeClosedError,
)

_SAFE_RESPONSE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_USAGE_COUNTERS = frozenset(("input_tokens", "output_tokens", "total_tokens"))
_USAGE_DETAILS = {
    "input_tokens_details": frozenset(("cached_tokens",)),
    "output_tokens_details": frozenset(("reasoning_tokens",)),
}


class OpenAIResponsesConfigurationError(ValueError):
    """Raised when explicit adapter configuration is invalid."""


class OpenAIResponsesError(RuntimeError):
    """Base class for redacted Responses API failures."""


class OpenAIResponsesHTTPError(OpenAIResponsesError):
    """A non-success HTTP response, represented without its untrusted body."""

    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f"Responses API returned HTTP status {status}")


class OpenAIResponsesTransportError(OpenAIResponsesError):
    """A redacted network, TLS, or timeout failure."""

    def __init__(self) -> None:
        super().__init__("Responses API transport failed")


class OpenAIResponsesProtocolError(OpenAIResponsesError):
    """The response stream violated the supported Responses API contract."""


class OpenAIResponsesAPIError(OpenAIResponsesError):
    """The remote stream reported a provider-side error."""

    def __init__(self) -> None:
        # Provider messages are deliberately excluded: gateways sometimes echo request
        # headers or prompt fragments in diagnostic text.
        super().__init__("Responses API stream reported an API error")


class OpenAIResponsesLimitError(OpenAIResponsesError):
    """The bounded response-body allowance was exceeded."""

    def __init__(self) -> None:
        super().__init__("Responses API stream exceeded the configured byte limit")


@dataclass(frozen=True)
class OpenAIResponsesConfig:
    """Explicit connection settings for one OpenAI-compatible provider."""

    api_key: str = field(repr=False)
    base_url: str
    model: str
    timeout_seconds: float = 120.0
    max_response_bytes: int = 8 * 1024 * 1024

    def __post_init__(self) -> None:
        self._validate_text(self.api_key, "api_key", maximum=16_384)
        self._validate_text(self.model, "model", maximum=256)
        self._validate_text(self.base_url, "base_url", maximum=2_048)
        try:
            parsed = urlsplit(self.base_url)
            hostname = parsed.hostname
            port = parsed.port
        except ValueError:
            raise OpenAIResponsesConfigurationError("base_url is invalid") from None
        if (
            parsed.scheme not in {"http", "https"}
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path.endswith("/responses")
        ):
            raise OpenAIResponsesConfigurationError("base_url is invalid")
        # Accessing ``port`` above validates a textual port. Keep the assignment explicit
        # so static checkers do not treat it as an accidentally unused security check.
        del port
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
            or self.timeout_seconds > 600
        ):
            raise OpenAIResponsesConfigurationError("timeout_seconds is invalid")
        if (
            isinstance(self.max_response_bytes, bool)
            or not isinstance(self.max_response_bytes, int)
            or self.max_response_bytes <= 0
            or self.max_response_bytes > 64 * 1024 * 1024
        ):
            raise OpenAIResponsesConfigurationError("max_response_bytes is invalid")

    @staticmethod
    def _validate_text(value: str, field_name: str, *, maximum: int) -> None:
        if (
            type(value) is not str
            or not value
            or value != value.strip()
            or len(value) > maximum
            or any(character in value for character in ("\x00", "\r", "\n"))
        ):
            raise OpenAIResponsesConfigurationError(f"{field_name} is invalid")

    @property
    def responses_url(self) -> str:
        """Return the canonical endpoint without mutating the configured base URL."""

        return self.base_url.rstrip("/") + "/responses"


@dataclass
class _StreamState:
    deltas: list[str] = field(default_factory=list)
    done_texts: list[str] = field(default_factory=list)
    completed_response: dict[str, Any] | None = None
    saw_done_marker: bool = False


class OpenAIResponsesRuntime:
    """Execute recorded agent invocations through a streaming Responses endpoint."""

    def __init__(self, config: OpenAIResponsesConfig) -> None:
        if not isinstance(config, OpenAIResponsesConfig):
            raise TypeError("config must be OpenAIResponsesConfig")
        self._config = config
        self._state_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._inflight: set[asyncio.Task[AgentResult]] = set()
        self._accepting = True
        self._closed = False

    @staticmethod
    def render_input(invocation: AgentInvocation) -> str:
        """Render only the recorded task, handoff, coordination, and compiled context."""

        if not isinstance(invocation, AgentInvocation):
            raise TypeError("invocation must be AgentInvocation")
        coordination = {
            "sessionId": invocation.envelope.session_id,
            "threadId": invocation.envelope.thread_id,
            "correlationId": invocation.envelope.correlation_id,
            "causationId": invocation.envelope.causation_id,
            "idempotencyKey": invocation.envelope.idempotency_key,
        }
        task = invocation.task.to_dict()
        task.pop("handoff", None)
        sections = (
            (
                "# Coordination",
                json.dumps(coordination, ensure_ascii=False, sort_keys=True, indent=2),
            ),
            ("# Task", json.dumps(task, ensure_ascii=False, sort_keys=True, indent=2)),
            (
                "# Handoff contract",
                json.dumps(
                    invocation.task.handoff.to_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                ),
            ),
            (
                "# Recorded context",
                f"digest: {invocation.context.digest}\n\n{invocation.context.render()}",
            ),
        )
        preamble = (
            "Execute this already-recorded coordination task. Respect the handoff, "
            "authority, constraints, and acceptance criteria. Return a concise final result."
        )
        return preamble + "\n\n" + "\n\n".join(f"{heading}\n{body}" for heading, body in sections)

    async def invoke(self, invocation: AgentInvocation) -> AgentResult:
        if not isinstance(invocation, AgentInvocation):
            raise TypeError("invocation must be AgentInvocation")
        async with self._state_lock:
            if not self._accepting:
                raise AgentRuntimeClosedError("OpenAI Responses runtime is closing or closed")
            task = asyncio.create_task(self._invoke_and_release(invocation))
            self._inflight.add(task)
        try:
            # Canceling a Python waiter cannot terminate urllib's blocking socket safely.
            return await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            raise AgentCancellationUnsupportedError(
                "local wait was canceled, but the Responses API request may still be running"
            ) from exc

    async def _invoke_and_release(self, invocation: AgentInvocation) -> AgentResult:
        try:
            return await asyncio.to_thread(self._invoke_sync, invocation)
        finally:
            current = asyncio.current_task()
            if current is not None:
                async with self._state_lock:
                    self._inflight.discard(current)

    def _invoke_sync(self, invocation: AgentInvocation) -> AgentResult:
        prompt = self.render_input(invocation)
        encoded = json.dumps(
            {
                "model": self._config.model,
                "input": [
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": prompt}],
                    }
                ],
                "stream": True,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        request = Request(
            self._config.responses_url,
            data=encoded,
            method="POST",
            headers={
                "Accept": "text/event-stream",
                "Authorization": f"Bearer {self._config.api_key}",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        try:
            with urlopen(request, timeout=float(self._config.timeout_seconds)) as response:
                state = self._consume_sse(response)
        except HTTPError as exc:
            raise OpenAIResponsesHTTPError(exc.code) from None
        except (URLError, socket.timeout, TimeoutError, HTTPException, OSError):
            raise OpenAIResponsesTransportError() from None

        if state.completed_response is None:
            raise OpenAIResponsesProtocolError(
                "Responses API stream ended before response.completed"
            )
        narration = self._select_output_text(state)
        completed = state.completed_response or {}
        metadata: dict[str, Any] = {
            "runtime": "openai-responses",
            "provider": "openai",
            "model": self._config.model,
            "usage": self._copy_usage(completed.get("usage")),
            "contextDigest": invocation.context.digest,
            "coordinationSessionId": invocation.envelope.session_id,
            "taskId": invocation.task.task_id,
        }
        response_id = completed.get("id")
        if isinstance(response_id, str) and _SAFE_RESPONSE_ID.fullmatch(response_id) is not None:
            metadata["responseId"] = response_id
        return AgentResult(narration=narration, metadata=metadata)

    def _consume_sse(self, response: Any) -> _StreamState:
        state = _StreamState()
        total_bytes = 0
        event_name = ""
        data_lines: list[str] = []
        while True:
            remaining = self._config.max_response_bytes - total_bytes
            raw_line = response.readline(remaining + 1)
            if not raw_line:
                self._dispatch_event(state, event_name, data_lines)
                break
            if not isinstance(raw_line, bytes):
                raise OpenAIResponsesProtocolError("Responses API stream was not bytes")
            total_bytes += len(raw_line)
            if total_bytes > self._config.max_response_bytes:
                raise OpenAIResponsesLimitError()
            try:
                line = raw_line.decode("utf-8").rstrip("\r\n")
            except UnicodeDecodeError:
                raise OpenAIResponsesProtocolError(
                    "Responses API stream was not valid UTF-8"
                ) from None
            if not line:
                self._dispatch_event(state, event_name, data_lines)
                event_name = ""
                data_lines = []
                continue
            if line.startswith(":"):
                continue
            field_name, separator, value = line.partition(":")
            if separator and value.startswith(" "):
                value = value[1:]
            if field_name == "event":
                event_name = value
            elif field_name == "data":
                data_lines.append(value if separator else "")
        return state

    @staticmethod
    def _dispatch_event(state: _StreamState, event_name: str, data_lines: list[str]) -> None:
        if not data_lines:
            return
        data = "\n".join(data_lines)
        if data.strip() == "[DONE]":
            state.saw_done_marker = True
            return
        try:
            payload = json.loads(data)
        except (json.JSONDecodeError, RecursionError):
            raise OpenAIResponsesProtocolError(
                "Responses API stream contained invalid JSON"
            ) from None
        if not isinstance(payload, dict):
            raise OpenAIResponsesProtocolError("Responses API event must be a JSON object")
        payload_type = payload.get("type") or event_name
        if not isinstance(payload_type, str):
            raise OpenAIResponsesProtocolError("Responses API event type must be a string")
        if payload_type in {"error", "response.failed", "response.incomplete"}:
            raise OpenAIResponsesAPIError()
        if payload_type == "response.output_text.delta":
            delta = payload.get("delta")
            if not isinstance(delta, str):
                raise OpenAIResponsesProtocolError("output-text delta must be a string")
            state.deltas.append(delta)
            return
        if payload_type == "response.output_text.done":
            text = payload.get("text")
            if not isinstance(text, str):
                raise OpenAIResponsesProtocolError("completed output text must be a string")
            state.done_texts.append(text)
            return
        if payload_type == "response.completed":
            completed = payload.get("response", payload)
            if not isinstance(completed, dict):
                raise OpenAIResponsesProtocolError("completed response must be a JSON object")
            state.completed_response = completed

    @classmethod
    def _select_output_text(cls, state: _StreamState) -> str:
        delta_output = "".join(state.deltas)
        if delta_output:
            output = delta_output
        elif state.done_texts:
            output = "".join(state.done_texts)
        else:
            output = cls._extract_completed_text(state.completed_response)
        if not output:
            raise OpenAIResponsesProtocolError("Responses API stream contained no output text")
        return output

    @staticmethod
    def _extract_completed_text(response: dict[str, Any] | None) -> str:
        if response is None:
            return ""
        direct = response.get("output_text")
        if isinstance(direct, str):
            return direct
        output = response.get("output")
        if not isinstance(output, list):
            return ""
        fragments: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if (
                    isinstance(part, dict)
                    and part.get("type") == "output_text"
                    and isinstance(part.get("text"), str)
                ):
                    fragments.append(part["text"])
        return "".join(fragments)

    @staticmethod
    def _copy_usage(value: Any) -> dict[str, Any]:
        """Copy only bounded standard counters from an untrusted provider payload."""

        if not isinstance(value, Mapping):
            return {}
        usage: dict[str, Any] = {}
        for key in _USAGE_COUNTERS:
            counter = value.get(key)
            if isinstance(counter, int) and not isinstance(counter, bool) and counter >= 0:
                usage[key] = counter
        for key, allowed_fields in _USAGE_DETAILS.items():
            details = value.get(key)
            if not isinstance(details, Mapping):
                continue
            copied: dict[str, int] = {}
            for detail_key in allowed_fields:
                counter = details.get(detail_key)
                if isinstance(counter, int) and not isinstance(counter, bool) and counter >= 0:
                    copied[detail_key] = counter
            if copied:
                usage[key] = copied
        return usage

    async def close(self) -> None:
        """Reject new work and wait for every request accepted before shutdown."""

        async with self._close_lock:
            async with self._state_lock:
                if self._closed:
                    return
                self._accepting = False
                pending = tuple(self._inflight)
            if pending:
                await asyncio.gather(
                    *(asyncio.shield(task) for task in pending),
                    return_exceptions=True,
                )
            async with self._state_lock:
                self._closed = True

    def __repr__(self) -> str:
        state = "open" if self._accepting else "closed"
        return (
            "OpenAIResponsesRuntime("
            f"provider='openai', model={self._config.model!r}, state={state!r})"
        )


__all__ = [
    "OpenAIResponsesAPIError",
    "OpenAIResponsesConfig",
    "OpenAIResponsesConfigurationError",
    "OpenAIResponsesError",
    "OpenAIResponsesHTTPError",
    "OpenAIResponsesLimitError",
    "OpenAIResponsesProtocolError",
    "OpenAIResponsesRuntime",
    "OpenAIResponsesTransportError",
]
