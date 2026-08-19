"""Optional LangGraph workflow bridge.

LangGraph checkpoints track control-flow position; WanWork domain events remain
the source of business truth. The bridge accepts a compiled graph-like object so
the core package does not require LangGraph at import time.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from importlib import import_module
from typing import Any, Callable


class BridgeStatus(str, Enum):
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True)
class LangGraphResult:
    status: BridgeStatus
    state: Mapping[str, Any]
    interrupts: tuple[Any, ...] = ()


class LangGraphBridge:
    def __init__(
        self,
        compiled_graph: Any,
        command_factory: Callable[[Any], Any] | None = None,
    ) -> None:
        if not hasattr(compiled_graph, "ainvoke"):
            raise TypeError("compiled graph must expose ainvoke")
        self.graph = compiled_graph
        self._command_factory = command_factory

    @staticmethod
    def _config(thread_id: str, checkpoint_ns: str = "") -> Mapping[str, Any]:
        if not thread_id.strip():
            raise ValueError("LangGraph thread id is required")
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
            }
        }

    @staticmethod
    def _normalize(value: Any) -> LangGraphResult:
        if not isinstance(value, Mapping):
            raise TypeError("LangGraph output must be a mapping")
        interrupts = value.get("__interrupt__", ())
        if interrupts is None:
            interrupts = ()
        elif not isinstance(interrupts, (tuple, list)):
            interrupts = (interrupts,)
        status = BridgeStatus.INTERRUPTED if interrupts else BridgeStatus.COMPLETED
        return LangGraphResult(status, dict(value), tuple(interrupts))

    def _resume_command(self, value: Any) -> Any:
        if self._command_factory is not None:
            return self._command_factory(value)
        try:
            command_type = import_module("langgraph.types").Command
        except (AttributeError, ImportError) as exc:
            raise RuntimeError(
                "resuming requires the optional langgraph dependency or a command_factory"
            ) from exc
        return command_type(resume=value)

    async def start(
        self,
        thread_id: str,
        state: Mapping[str, Any],
        checkpoint_ns: str = "",
    ) -> LangGraphResult:
        output = await self.graph.ainvoke(dict(state), self._config(thread_id, checkpoint_ns))
        return self._normalize(output)

    async def resume(
        self,
        thread_id: str,
        value: Any,
        checkpoint_ns: str = "",
    ) -> LangGraphResult:
        output = await self.graph.ainvoke(
            self._resume_command(value), self._config(thread_id, checkpoint_ns)
        )
        return self._normalize(output)
