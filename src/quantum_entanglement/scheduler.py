"""Deterministic task graph and status transitions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .protocol import ActionIntent, HandoffContract, TaskStatus, new_id

DONE_STATUSES = (TaskStatus.COMPLETED, TaskStatus.SUPERSEDED)
FAILED_STATUSES = (TaskStatus.FAILED, TaskStatus.BLOCKED, TaskStatus.CANCELED)
TERMINAL_STATUSES = DONE_STATUSES + FAILED_STATUSES


@dataclass(frozen=True)
class TaskSpec:
    title: str
    agent_id: str
    handoff: HandoffContract
    task_id: str = field(default_factory=lambda: new_id("task"))
    depends_on: tuple[str, ...] = ()
    input_artifacts: tuple[str, ...] = ()
    action: ActionIntent = field(default_factory=lambda: ActionIntent("analyze", "workspace"))
    priority: int = 50
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.title.strip() or not self.agent_id.strip():
            raise ValueError("task title and agent_id are required")
        if not 0 <= self.priority <= 100:
            raise ValueError("task priority must be between 0 and 100")
        if self.task_id in self.depends_on:
            raise ValueError("task cannot depend on itself")

    def to_dict(self) -> dict[str, Any]:
        return {
            "taskId": self.task_id,
            "title": self.title,
            "agentId": self.agent_id,
            "handoff": self.handoff.to_dict(),
            "dependsOn": list(self.depends_on),
            "inputArtifacts": list(self.input_artifacts),
            "action": self.action.to_dict(),
            "priority": self.priority,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> TaskSpec:
        return cls(
            title=str(value["title"]),
            agent_id=str(value["agentId"]),
            handoff=HandoffContract.from_dict(value["handoff"]),
            task_id=str(value["taskId"]),
            depends_on=tuple(str(item) for item in value.get("dependsOn", ())),
            input_artifacts=tuple(str(item) for item in value.get("inputArtifacts", ())),
            action=ActionIntent.from_dict(
                value.get("action", {"action": "analyze", "target": "workspace"})
            ),
            priority=int(value.get("priority", 50)),
            metadata=dict(value.get("metadata", {})),
        )


@dataclass(frozen=True)
class TaskTransition:
    task_id: str
    previous: TaskStatus
    current: TaskStatus
    reason: str | None = None
    revision: int = 0


class TaskGraph:
    """Mutable projection whose decisions are deterministic and replayable."""

    _ALLOWED = {
        TaskStatus.PENDING: (TaskStatus.READY, TaskStatus.BLOCKED, TaskStatus.CANCELED),
        TaskStatus.READY: (TaskStatus.RUNNING, TaskStatus.BLOCKED, TaskStatus.CANCELED),
        TaskStatus.RUNNING: (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.WAITING_INPUT,
            TaskStatus.WAITING_APPROVAL,
            TaskStatus.CANCELED,
        ),
        TaskStatus.WAITING_APPROVAL: (
            TaskStatus.READY,
            TaskStatus.CANCELED,
            TaskStatus.WAITING_INPUT,
        ),
        TaskStatus.WAITING_INPUT: (TaskStatus.READY, TaskStatus.CANCELED),
        TaskStatus.COMPLETED: (TaskStatus.SUPERSEDED,),
        TaskStatus.FAILED: (),
        TaskStatus.BLOCKED: (),
        TaskStatus.CANCELED: (),
        TaskStatus.SUPERSEDED: (),
    }

    def __init__(self, tasks: Sequence[TaskSpec]) -> None:
        if not tasks:
            raise ValueError("a workflow needs at least one task")
        self.tasks: dict[str, TaskSpec] = {}
        for task in tasks:
            if task.task_id in self.tasks:
                raise ValueError(f"duplicate task id: {task.task_id}")
            self.tasks[task.task_id] = task
        self._validate_dependencies()
        self._validate_acyclic()
        self.statuses: dict[str, TaskStatus] = {
            task_id: TaskStatus.PENDING for task_id in self.tasks
        }
        self.reasons: dict[str, str] = {}
        self.revisions: dict[str, int] = {task_id: 0 for task_id in self.tasks}

    def _validate_dependencies(self) -> None:
        known = set(self.tasks)
        for task in self.tasks.values():
            missing = set(task.depends_on) - known
            if missing:
                raise ValueError(f"task {task.task_id} has missing dependencies: {sorted(missing)}")

    def _validate_acyclic(self) -> None:
        visiting = set()
        visited = set()

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise ValueError(f"task graph contains a cycle at {task_id}")
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency in self.tasks[task_id].depends_on:
                visit(dependency)
            visiting.remove(task_id)
            visited.add(task_id)

        for candidate in self.tasks:
            visit(candidate)

    def transition(
        self, task_id: str, target: TaskStatus, reason: str | None = None
    ) -> TaskTransition:
        planned = self.preview_transition(task_id, target, reason)
        if target == planned.previous:
            return planned
        self.statuses[task_id] = target
        self.revisions[task_id] = planned.revision
        if reason:
            self.reasons[task_id] = reason
        return planned

    def preview_transition(
        self, task_id: str, target: TaskStatus, reason: str | None = None
    ) -> TaskTransition:
        """Validate and describe one transition without mutating the projection."""

        previous = self.statuses[task_id]
        if target == previous:
            return TaskTransition(task_id, previous, target, reason, self.revisions[task_id])
        if target not in self._ALLOWED[previous]:
            raise ValueError(f"invalid task transition {previous.value} -> {target.value}")
        return TaskTransition(task_id, previous, target, reason, self.revisions[task_id] + 1)

    def restore_status(
        self,
        task_id: str,
        status: TaskStatus,
        reason: str | None = None,
        revision: int | None = None,
    ) -> None:
        """Set a replayed status without re-validating historical intermediate states."""

        if task_id not in self.tasks:
            raise KeyError(task_id)
        self.statuses[task_id] = status
        if revision is not None:
            if revision < 0:
                raise ValueError("task revision cannot be negative")
            self.revisions[task_id] = revision
        if reason:
            self.reasons[task_id] = reason

    def refresh(self) -> tuple[TaskTransition, ...]:
        transitions = self.preview_refresh()
        for planned in transitions:
            applied = self.transition(planned.task_id, planned.current, planned.reason)
            if applied != planned:  # pragma: no cover - deterministic internal invariant.
                raise RuntimeError("refreshed task transition changed after preview")
        return transitions

    def preview_refresh(self) -> tuple[TaskTransition, ...]:
        """Describe deterministic dependency transitions without mutating the graph."""

        transitions = []
        for task_id, task in self.tasks.items():
            if self.statuses[task_id] != TaskStatus.PENDING:
                continue
            dependency_states = [self.statuses[item] for item in task.depends_on]
            failed = [
                dependency
                for dependency in task.depends_on
                if self.statuses[dependency] in FAILED_STATUSES
            ]
            if failed:
                transitions.append(
                    self.preview_transition(
                        task_id,
                        TaskStatus.BLOCKED,
                        "dependencies failed: {}".format(", ".join(failed)),
                    )
                )
            elif all(status in DONE_STATUSES for status in dependency_states):
                transitions.append(self.preview_transition(task_id, TaskStatus.READY))
        return tuple(transitions)

    def ready(self, limit: int | None = None) -> tuple[TaskSpec, ...]:
        candidates = [
            task
            for task_id, task in self.tasks.items()
            if self.statuses[task_id] == TaskStatus.READY
        ]
        candidates.sort(key=lambda item: (-item.priority, item.task_id))
        return tuple(candidates[:limit] if limit is not None else candidates)

    def is_terminal(self) -> bool:
        return all(status in TERMINAL_STATUSES for status in self.statuses.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "tasks": [self.tasks[key].to_dict() for key in sorted(self.tasks)],
            "statuses": {key: value.value for key, value in sorted(self.statuses.items())},
            "revisions": dict(sorted(self.revisions.items())),
            "reasons": dict(sorted(self.reasons.items())),
        }


@dataclass(frozen=True)
class WorkflowPlan:
    session_id: str
    goal: str
    initiated_by: str
    tasks: tuple[TaskSpec, ...]
    plan_id: str = field(default_factory=lambda: new_id("plan"))
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        if not self.session_id.strip() or not self.goal.strip() or not self.initiated_by.strip():
            raise ValueError("session_id, goal, and initiated_by are required")
        if not self.tasks:
            raise ValueError("workflow plan needs tasks")

    def to_dict(self) -> dict[str, Any]:
        return {
            "planId": self.plan_id,
            "sessionId": self.session_id,
            "goal": self.goal,
            "initiatedBy": self.initiated_by,
            "correlationId": self.correlation_id,
            "tasks": [task.to_dict() for task in self.tasks],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> WorkflowPlan:
        return cls(
            session_id=str(value["sessionId"]),
            goal=str(value["goal"]),
            initiated_by=str(value["initiatedBy"]),
            tasks=tuple(TaskSpec.from_dict(item) for item in value["tasks"]),
            plan_id=str(value["planId"]),
            correlation_id=(str(value["correlationId"]) if value.get("correlationId") else None),
        )
