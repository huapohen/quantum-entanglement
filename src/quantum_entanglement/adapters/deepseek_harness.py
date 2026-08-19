"""Optional DeepSeek Harness implementation of the stable agent runtime port.

The official SDK is synchronous, owns a reusable JSON-RPC subprocess, and currently
requires Python 3.10+. This module deliberately avoids importing it at module import
time so the dependency-free Python 3.9 kernel remains usable.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import OrderedDict
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..agent_runtime import (
    AgentCancellationUnsupportedError,
    AgentInvocation,
    AgentInvocationConflictError,
    AgentResult,
    AgentRuntimeClosedError,
)


class DeepSeekHarnessDependencyError(RuntimeError):
    """The optional DeepSeek Harness Python SDK is not available."""


class DeepSeekHarnessConfigurationError(ValueError):
    """The adapter was not given an explicit isolated Harness factory."""


class DeepSeekHarnessProtocolError(RuntimeError):
    """The SDK returned a value that violates the adapter contract."""


class DeepSeekHarnessRunError(RuntimeError):
    """The harness turn ended for a reason the caller did not accept."""


class _Harness(Protocol):
    def start(self) -> None:
        ...

    def run(self, input: str, *, session_id: str) -> Any:
        ...

    def close(self) -> None:
        ...


HarnessFactory = Callable[[], _Harness]


@dataclass
class _SessionGate:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class DeepSeekHarnessRuntime:
    """Runs recorded kernel invocations through a reusable DeepSeek Harness SDK.

    Duplicate calls with the same envelope idempotency key share one in-flight turn
    and then reuse its successful result. Failures are never cached.

    ``harness_factory`` is a deliberate security boundary. The official SDK's default
    composition inherits the host environment and exposes filesystem/bash tools, so
    this adapter never constructs that unsafe default. The caller must explicitly
    supply a factory configured with an isolated workspace/session root, a restricted
    Cordis tool composition, and a launcher that does not inherit unrelated secrets.
    """

    def __init__(
        self,
        harness_factory: HarnessFactory | None = None,
        *,
        max_concurrency: int = 4,
        max_cached_results: int = 1_024,
        successful_finish_reasons: Collection[str] = ("completed",),
    ) -> None:
        if harness_factory is None:
            raise DeepSeekHarnessConfigurationError(
                "an explicit isolated harness_factory is required; the official "
                "DeepSeekHarness default inherits host environment and tools"
            )
        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive")
        if max_cached_results <= 0:
            raise ValueError("max_cached_results must be positive")
        reasons = frozenset(successful_finish_reasons)
        if not reasons or any(not isinstance(item, str) or not item.strip() for item in reasons):
            raise ValueError("successful_finish_reasons must contain non-blank strings")
        self._factory = harness_factory
        self._max_cached_results = max_cached_results
        self._successful_finish_reasons = reasons
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._state_lock = asyncio.Lock()
        self._harness_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._harness: _Harness | None = None
        self._harness_started = False
        self._inflight: dict[str, asyncio.Task[AgentResult]] = {}
        self._completed: OrderedDict[str, AgentResult] = OrderedDict()
        self._fingerprints: dict[str, str] = {}
        self._session_gates: dict[str, _SessionGate] = {}
        self._accepting = True
        self._closed = False

    @staticmethod
    def session_id_for(invocation: AgentInvocation) -> str:
        """Return a stable, provider-safe session id for a workflow task."""

        identity = f"{invocation.envelope.session_id}\x00{invocation.task.task_id}"
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return f"qe-{digest[:32]}"

    @staticmethod
    def render_prompt(invocation: AgentInvocation) -> str:
        """Serialize only the explicit dispatch contract and precompiled context."""

        coordination = {
            "sessionId": invocation.envelope.session_id,
            "threadId": invocation.envelope.thread_id,
            "correlationId": invocation.envelope.correlation_id,
            "causationId": invocation.envelope.causation_id,
            "idempotencyKey": invocation.envelope.idempotency_key,
        }
        task = invocation.task.to_dict()
        # Keep the handoff as a first-class section even though TaskSpec also embeds it.
        # This makes the producer-consumer contract unambiguous to a prompt-driven runtime.
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
        return preamble + "\n\n" + "\n\n".join(
            f"{heading}\n{body}" for heading, body in sections
        )

    @staticmethod
    def invocation_fingerprint(invocation: AgentInvocation) -> str:
        """Identify semantic invocation content without volatile envelope fields."""

        content = {
            "sessionId": invocation.envelope.session_id,
            "threadId": invocation.envelope.thread_id,
            "kind": invocation.envelope.kind.value,
            "correlationId": invocation.envelope.correlation_id,
            "causationId": invocation.envelope.causation_id,
            "task": invocation.task.to_dict(),
            "context": invocation.context.to_dict(),
        }
        encoded = json.dumps(
            content, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()

    async def invoke(self, invocation: AgentInvocation) -> AgentResult:
        key = invocation.envelope.idempotency_key
        fingerprint = self.invocation_fingerprint(invocation)
        async with self._state_lock:
            if not self._accepting:
                raise AgentRuntimeClosedError("DeepSeek Harness runtime is closing or closed")
            previous_fingerprint = self._fingerprints.get(key)
            if previous_fingerprint is not None and previous_fingerprint != fingerprint:
                raise AgentInvocationConflictError(
                    "idempotency key was reused for different agent invocation content"
                )
            completed = self._completed.get(key)
            if completed is not None:
                self._completed.move_to_end(key)
                return completed
            task = self._inflight.get(key)
            if task is None:
                self._fingerprints[key] = fingerprint
                task = asyncio.create_task(self._invoke_and_record(key, invocation))
                self._inflight[key] = task
        # One canceled waiter must not cancel a turn shared by other idempotent callers.
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            raise AgentCancellationUnsupportedError(
                "local wait was canceled, but the DeepSeek Harness turn is still running"
            ) from exc

    async def _invoke_and_record(
        self, key: str, invocation: AgentInvocation
    ) -> AgentResult:
        try:
            result = await self._invoke_once(invocation)
            async with self._state_lock:
                self._completed[key] = result
                self._completed.move_to_end(key)
                while len(self._completed) > self._max_cached_results:
                    expired_key, _expired_result = self._completed.popitem(last=False)
                    self._fingerprints.pop(expired_key, None)
            return result
        finally:
            current = asyncio.current_task()
            async with self._state_lock:
                if self._inflight.get(key) is current:
                    self._inflight.pop(key, None)
                    if key not in self._completed:
                        self._fingerprints.pop(key, None)

    async def _invoke_once(self, invocation: AgentInvocation) -> AgentResult:
        prompt = self.render_prompt(invocation)
        session_id = self.session_id_for(invocation)
        gate = await self._acquire_session_gate(session_id)
        try:
            async with self._semaphore:
                harness = await self._get_harness()
                raw = await asyncio.to_thread(harness.run, prompt, session_id=session_id)
        finally:
            await self._release_session_gate(session_id, gate)
        return self._map_result(invocation, raw)

    async def _acquire_session_gate(self, session_id: str) -> _SessionGate:
        async with self._state_lock:
            gate = self._session_gates.get(session_id)
            if gate is None:
                gate = _SessionGate()
                self._session_gates[session_id] = gate
            gate.users += 1
        try:
            await gate.lock.acquire()
        except BaseException:
            async with self._state_lock:
                gate.users -= 1
                if gate.users == 0:
                    self._session_gates.pop(session_id, None)
            raise
        return gate

    async def _release_session_gate(self, session_id: str, gate: _SessionGate) -> None:
        gate.lock.release()
        async with self._state_lock:
            gate.users -= 1
            if gate.users == 0:
                self._session_gates.pop(session_id, None)

    async def _get_harness(self) -> _Harness:
        async with self._harness_lock:
            if self._harness is None:
                try:
                    harness = await asyncio.to_thread(self._factory)
                except ModuleNotFoundError as exc:
                    if exc.name and exc.name.startswith("deepseek_harness"):
                        raise DeepSeekHarnessDependencyError(
                            "DeepSeek Harness is optional and requires Python 3.10+"
                        ) from exc
                    raise
                for method_name in ("start", "run", "close"):
                    if not callable(getattr(harness, method_name, None)):
                        raise DeepSeekHarnessProtocolError(
                            f"harness_factory result has no callable {method_name}()"
                        )
                self._harness = harness
            if not self._harness_started:
                await asyncio.to_thread(self._harness.start)
                self._harness_started = True
            return self._harness

    def _map_result(self, invocation: AgentInvocation, raw: Any) -> AgentResult:
        try:
            response = raw.final_response
            finish_reason = raw.finish_reason
            harness_session_id = raw.session_id
            events = raw.events
            notifications = raw.notifications
            session_root = raw.session_root
        except AttributeError as exc:
            raise DeepSeekHarnessProtocolError(
                "DeepSeek Harness result is missing required fields"
            ) from exc
        if not isinstance(response, str):
            raise DeepSeekHarnessProtocolError("final_response must be a string")
        if not isinstance(harness_session_id, str) or not harness_session_id.strip():
            raise DeepSeekHarnessProtocolError("session_id must be a non-blank string")
        if not isinstance(events, (list, tuple)):
            raise DeepSeekHarnessProtocolError("events must be a sequence")
        if not isinstance(notifications, (list, tuple)):
            raise DeepSeekHarnessProtocolError("notifications must be a sequence")
        if finish_reason not in self._successful_finish_reasons:
            raise DeepSeekHarnessRunError(
                f"DeepSeek Harness turn did not complete successfully: {finish_reason!r}"
            )
        return AgentResult(
            narration=response,
            metadata={
                "runtime": "deepseek-harness",
                "harnessSessionId": harness_session_id,
                "finishReason": finish_reason,
                "eventCount": len(events),
                "notificationCount": len(notifications),
                "sessionRoot": session_root,
                "contextDigest": invocation.context.digest,
                "coordinationSessionId": invocation.envelope.session_id,
                "taskId": invocation.task.task_id,
            },
        )

    async def cancel(self, _idempotency_key: str) -> None:
        """Fail loudly because the high-level SDK has no supported cancel method."""

        raise AgentCancellationUnsupportedError(
            "DeepSeek Harness high-level SDK does not expose reliable session cancellation"
        )

    async def close(self) -> None:
        """Reject new work, drain accepted turns, and reap the SDK subprocess."""

        async with self._close_lock:
            async with self._state_lock:
                if self._closed:
                    return
                self._accepting = False
                pending: tuple[asyncio.Task[AgentResult], ...] = tuple(
                    self._inflight.values()
                )
            if pending:
                await asyncio.gather(
                    *(asyncio.shield(task) for task in pending),
                    return_exceptions=True,
                )
            async with self._harness_lock:
                harness = self._harness
                if harness is not None:
                    await asyncio.to_thread(harness.close)
                    self._harness = None
                    self._harness_started = False
            async with self._state_lock:
                self._completed.clear()
                self._fingerprints.clear()
                self._closed = True


__all__ = [
    "DeepSeekHarnessConfigurationError",
    "DeepSeekHarnessDependencyError",
    "DeepSeekHarnessProtocolError",
    "DeepSeekHarnessRunError",
    "DeepSeekHarnessRuntime",
    "HarnessFactory",
]
