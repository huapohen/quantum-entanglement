"""Stable execution boundary between the coordination kernel and agent runtimes."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .context import ContextBundle
from .protocol import ArtifactOutput, CoordinationEnvelope
from .scheduler import TaskSpec


@dataclass(frozen=True)
class AgentInvocation:
    """A recorded task dispatch passed to an agent runtime."""

    task: TaskSpec
    envelope: CoordinationEnvelope
    context: ContextBundle


@dataclass(frozen=True)
class AgentResult:
    """Provider-neutral result returned to the coordination kernel."""

    narration: str
    artifacts: tuple[ArtifactOutput, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


AgentHandler = Callable[[AgentInvocation], Awaitable[AgentResult]]


class AgentRuntimeClosedError(RuntimeError):
    """Raised when a runtime receives work after shutdown has begun."""


class AgentInvocationConflictError(RuntimeError):
    """Raised when one idempotency key is reused for different invocation content."""


class AgentCancellationUnsupportedError(RuntimeError):
    """Raised when an adapter cannot truthfully guarantee remote cancellation."""


@runtime_checkable
class AgentRuntimePort(Protocol):
    """Minimal lifecycle contract implemented by every agent execution backend."""

    async def invoke(self, invocation: AgentInvocation) -> AgentResult:
        """Execute one recorded invocation and return a provider-neutral result."""

    async def close(self) -> None:
        """Stop accepting new work and release resources owned by the runtime."""


class CallableAgentRuntime:
    """Compatibility adapter for the kernel's original in-process async handlers."""

    def __init__(self, handler: AgentHandler) -> None:
        if not callable(handler):
            raise TypeError("handler must be callable")
        self._handler = handler
        self._state_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._inflight: set[asyncio.Task[Any]] = set()
        self._accepting = True
        self._closed = False

    async def invoke(self, invocation: AgentInvocation) -> AgentResult:
        current = asyncio.current_task()
        if current is None:
            raise RuntimeError("agent invocation requires an asyncio task")
        async with self._state_lock:
            if not self._accepting:
                raise AgentRuntimeClosedError("agent runtime is closing or closed")
            self._inflight.add(current)
        try:
            result = await self._handler(invocation)
            if not isinstance(result, AgentResult):
                raise TypeError("agent handler must return AgentResult")
            return result
        finally:
            async with self._state_lock:
                self._inflight.discard(current)

    async def close(self) -> None:
        async with self._close_lock:
            async with self._state_lock:
                if self._closed:
                    return
                self._accepting = False
                current = asyncio.current_task()
                pending = tuple(task for task in self._inflight if task is not current)
            if pending:
                await asyncio.gather(
                    *(asyncio.shield(task) for task in pending),
                    return_exceptions=True,
                )
            async with self._state_lock:
                self._closed = True


__all__ = [
    "AgentCancellationUnsupportedError",
    "AgentHandler",
    "AgentInvocationConflictError",
    "AgentInvocation",
    "AgentResult",
    "AgentRuntimeClosedError",
    "AgentRuntimePort",
    "CallableAgentRuntime",
]
