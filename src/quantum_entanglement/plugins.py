"""Composable runtime hooks inspired by DeepSeek Harness's plugin spine."""

from __future__ import annotations

import inspect
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, Mapping, MutableMapping, Optional, Tuple


class HookPoint(str, Enum):
    PLAN_CREATED = "plan.created"
    BEFORE_DISPATCH = "dispatch.before"
    CONTEXT_BUILD = "context.build"
    CONTEXT_COMPILED = "context.compiled"
    BEFORE_AGENT = "agent.before"
    AFTER_AGENT = "agent.after"
    AFTER_DISPATCH = "dispatch.after"
    EVENT_APPENDED = "event.appended"


Hook = Callable[[MutableMapping[str, Any]], Any]


@dataclass(frozen=True)
class KernelPlugin:
    name: str
    hooks: Mapping[HookPoint, Hook]
    priority: int = 50
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("plugin name is required")


class PluginManager:
    """Deterministic hook ordering and reversible plugin installation."""

    def __init__(self) -> None:
        self._plugins: Dict[str, KernelPlugin] = {}
        self._lock = threading.RLock()

    def install(self, plugin: KernelPlugin) -> None:
        with self._lock:
            if plugin.name in self._plugins:
                raise ValueError("plugin already installed: %s" % plugin.name)
            self._plugins[plugin.name] = plugin

    def uninstall(self, name: str) -> Optional[KernelPlugin]:
        with self._lock:
            return self._plugins.pop(name, None)

    def installed(self) -> Tuple[KernelPlugin, ...]:
        with self._lock:
            return tuple(sorted(self._plugins.values(), key=lambda item: (item.priority, item.name)))

    async def emit(self, point: HookPoint, context: MutableMapping[str, Any]) -> None:
        for plugin in self.installed():
            hook = plugin.hooks.get(point)
            if hook is None:
                continue
            result = hook(context)
            if inspect.isawaitable(result):
                await result

