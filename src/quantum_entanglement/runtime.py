# ruff: noqa: UP006, UP031, UP035, UP037, UP045
"""Plugin-based, event-sourced multi-agent execution kernel."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, Mapping, Optional, Set, Tuple

from .agent_runtime import (
    AgentHandler,
    AgentInvocation,
    AgentResult,
    AgentRuntimeClosedError,
    AgentRuntimePort,
    CallableAgentRuntime,
)
from .artifacts import ArtifactLedger
from .context import ContextBundle, ContextCompiler, ContextItem
from .events import DomainEvent, StoredEvent
from .plugins import HookPoint, PluginManager
from .policy import ApprovalRequest, NeedsYouQueue, PolicyEngine, PolicyOutcome
from .protocol import (
    ActionIntent,
    ActorKind,
    ActorRef,
    ApprovalDecision,
    ArtifactRef,
    CoordinationEnvelope,
    EnvelopeKind,
    TaskStatus,
)
from .scheduler import FAILED_STATUSES, TaskGraph, TaskSpec, TaskTransition, WorkflowPlan
from .store import SQLiteEventStore

_RECOVERY_PAGE_LIMIT = 1_000
_MAX_RECOVERY_EVENTS = 1_000_000
_MAX_RECOVERY_BYTES = 256 * 1024 * 1024
_MAX_RECOVERY_JSON_NODES = 5_000_000
_MAX_RECOVERY_TEXT_LENGTH = 65_536
_RECOVERY_UTC_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{6})?Z$")
_APPROVAL_PAYLOAD_FIELDS = frozenset(
    {
        "requestId",
        "sessionId",
        "taskId",
        "intent",
        "reason",
        "createdAt",
        "decision",
        "decidedBy",
        "comment",
    }
)
_APPROVAL_DECISION_TARGETS = {
    ApprovalDecision.APPROVE: TaskStatus.READY,
    ApprovalDecision.REVISE: TaskStatus.WAITING_INPUT,
    ApprovalDecision.REJECT: TaskStatus.CANCELED,
}
_LOGGER = logging.getLogger(__name__)


class SessionRecoveryError(RuntimeError):
    """Raised when a session history cannot be replayed safely and completely."""


@dataclass(frozen=True)
class AgentRegistration:
    actor: ActorRef
    handler: Optional[AgentHandler] = None
    skills: Tuple[str, ...] = ()
    protocol: str = "in-process"
    runtime: Optional[AgentRuntimePort] = None

    def __post_init__(self) -> None:
        if (self.handler is None) == (self.runtime is None):
            raise ValueError("register exactly one of handler or runtime")
        if self.handler is not None:
            object.__setattr__(self, "runtime", CallableAgentRuntime(self.handler))

    async def invoke(self, invocation: AgentInvocation) -> AgentResult:
        runtime = self.runtime
        if runtime is None:  # Kept defensive even though __post_init__ enforces this.
            raise RuntimeError("agent registration has no runtime")
        return await runtime.invoke(invocation)


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: Dict[str, AgentRegistration] = {}
        self._accepting = True
        self._closed = False
        self._close_lock = asyncio.Lock()

    def register(self, registration: AgentRegistration) -> None:
        if not self._accepting:
            raise AgentRuntimeClosedError("agent registry is closing or closed")
        if registration.actor.kind != ActorKind.AGENT:
            raise ValueError("only agent actors can be registered")
        if registration.actor.actor_id in self._agents:
            raise ValueError("agent already registered: %s" % registration.actor.actor_id)
        self._agents[registration.actor.actor_id] = registration

    def get(self, agent_id: str) -> AgentRegistration:
        try:
            return self._agents[agent_id]
        except KeyError as exc:
            raise KeyError("agent is not registered: %s" % agent_id) from exc

    async def invoke(self, invocation: AgentInvocation) -> AgentResult:
        if not self._accepting:
            raise AgentRuntimeClosedError("agent registry is closing or closed")
        return await self.get(invocation.task.agent_id).invoke(invocation)

    async def close(self) -> None:
        """Close each distinct registered runtime once; failed closes remain retryable."""

        async with self._close_lock:
            if self._closed:
                return
            self._accepting = False
            runtimes = []
            seen = set()
            for registration in self._agents.values():
                runtime = registration.runtime
                if runtime is not None and id(runtime) not in seen:
                    seen.add(id(runtime))
                    runtimes.append(runtime)
            results = await asyncio.gather(
                *(runtime.close() for runtime in runtimes),
                return_exceptions=True,
            )
            errors = [result for result in results if isinstance(result, BaseException)]
            if errors:
                raise errors[0]
            self._closed = True


@dataclass(frozen=True)
class RunResult:
    session_id: str
    plan_id: str
    statuses: Mapping[str, TaskStatus]
    artifacts: Tuple[ArtifactRef, ...]
    needs_you: Tuple[ApprovalRequest, ...]
    errors: Mapping[str, str]

    @property
    def completed(self) -> bool:
        return all(
            status in (TaskStatus.COMPLETED, TaskStatus.SUPERSEDED)
            for status in self.statuses.values()
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sessionId": self.session_id,
            "planId": self.plan_id,
            "statuses": {key: value.value for key, value in self.statuses.items()},
            "artifacts": [item.to_dict() for item in self.artifacts],
            "needsYou": [item.to_dict() for item in self.needs_you],
            "errors": dict(self.errors),
            "completed": self.completed,
        }


class OrchestratorKernel:
    """Runs task DAGs while keeping model calls replaceable and governance explicit."""

    SYSTEM_ACTOR = ActorRef("orchestrator", "WanWork Orchestrator", ActorKind.SYSTEM)

    def __init__(
        self,
        event_store: Optional[SQLiteEventStore] = None,
        registry: Optional[AgentRegistry] = None,
        context_compiler: Optional[ContextCompiler] = None,
        policy: Optional[PolicyEngine] = None,
        approvals: Optional[NeedsYouQueue] = None,
        plugins: Optional[PluginManager] = None,
        max_concurrency: int = 8,
        default_context_budget: int = 8_000,
    ) -> None:
        if max_concurrency <= 0 or default_context_budget <= 0:
            raise ValueError("concurrency and context budget must be positive")
        self.event_store = event_store or SQLiteEventStore()
        self.registry = registry or AgentRegistry()
        self.context_compiler = context_compiler or ContextCompiler()
        self.policy = policy or PolicyEngine()
        self.approvals = approvals or NeedsYouQueue()
        self.plugins = plugins or PluginManager()
        self.max_concurrency = max_concurrency
        self.default_context_budget = default_context_budget
        self.artifacts = ArtifactLedger(self.event_store)
        self._graphs: Dict[str, TaskGraph] = {}
        self._plans: Dict[str, WorkflowPlan] = {}
        self._session_locks: Dict[str, asyncio.Lock] = {}
        self._event_delivery_locks: Dict[str, asyncio.Lock] = {}
        self._task_artifacts: Dict[Tuple[str, str], Tuple[ArtifactRef, ...]] = {}
        # An approval is a scoped capability for exactly this workflow task. Keeping it
        # separate from delegated authority prevents the next dispatch from requesting
        # the same approval forever.
        self._approved_tasks: Set[Tuple[str, str]] = set()
        self._closing = False
        self._closed = False

    def register_agent(self, registration: AgentRegistration) -> None:
        if self._closing or self._closed:
            raise AgentRuntimeClosedError("orchestrator kernel is closing or closed")
        self.registry.register(registration)

    async def __aenter__(self) -> "OrchestratorKernel":
        if self._closing or self._closed:
            raise AgentRuntimeClosedError("orchestrator kernel is closing or closed")
        return self

    async def __aexit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        await self.close()

    async def close(self) -> None:
        """Close registered agent runtimes without assuming ownership of the event store."""

        if self._closed:
            return
        self._closing = True
        await self.registry.close()
        self._closed = True

    def _stream_id(self, session_id: str) -> str:
        return "session:%s" % session_id

    async def _emit_appended(self, stored: StoredEvent) -> None:
        context: Dict[str, Any] = {"storedEvent": stored, "kernel": self}
        await self.plugins.emit(HookPoint.EVENT_APPENDED, context)

    async def _emit_post_commit_observation(
        self,
        point: HookPoint,
        context: Dict[str, Any],
    ) -> None:
        try:
            await self.plugins.emit(point, context)
        except Exception:
            _LOGGER.exception("%s hook failed after durable commit", point.value)

    async def _emit_appended_batch(self, stored_events: Tuple[StoredEvent, ...]) -> None:
        if not stored_events:
            return
        stream_id = stored_events[0].event.stream_id
        delivery_lock = self._event_delivery_locks.setdefault(stream_id, asyncio.Lock())
        async with delivery_lock:
            for stored in stored_events:
                try:
                    await self._emit_appended(stored)
                except Exception:
                    _LOGGER.exception(
                        "event.appended hook failed after durable commit for stream %s sequence %d",
                        stored.event.stream_id,
                        stored.sequence,
                    )

    @staticmethod
    def _canonical_event_json(event: DomainEvent) -> str:
        return json.dumps(
            event.to_dict(),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    def _reconcile_committed_batch(
        self,
        stream_id: str,
        events: Tuple[DomainEvent, ...],
        *,
        expected_version: int,
    ) -> Optional[Tuple[StoredEvent, ...]]:
        """Return an exact batch committed before an append wrapper failed."""

        if not events:
            return None
        try:
            after_sequence = expected_version
            reconciled: list[StoredEvent] = []
            while len(reconciled) < len(events):
                page_limit = min(
                    _RECOVERY_PAGE_LIMIT,
                    len(events) - len(reconciled),
                )
                page: Tuple[StoredEvent, ...] = self.event_store.read_stream_page(
                    stream_id,
                    after_sequence=after_sequence,
                    limit=page_limit,
                )
                if len(page) != page_limit:
                    return None
                next_sequence = self._validate_recovery_page(
                    page,
                    stream_id=stream_id,
                    after_sequence=after_sequence,
                    requested_limit=page_limit,
                )
                for stored, expected in zip(
                    page,
                    events[len(reconciled) : len(reconciled) + page_limit],
                ):
                    if self._canonical_event_json(stored.event) != self._canonical_event_json(
                        expected
                    ):
                        return None
                reconciled.extend(page)
                after_sequence = next_sequence
        except Exception:
            return None
        return tuple(reconciled)

    def _append_many_reconciled(
        self,
        stream_id: str,
        events: Tuple[DomainEvent, ...],
        *,
        expected_version: int,
    ) -> Tuple[StoredEvent, ...]:
        """Append atomically, accepting only an exactly provable post-commit failure."""

        try:
            stored_events: Tuple[StoredEvent, ...] = self.event_store.append_many(
                stream_id,
                events,
                expected_version=expected_version,
            )
            return stored_events
        except Exception:
            committed = self._reconcile_committed_batch(
                stream_id,
                events,
                expected_version=expected_version,
            )
            if committed is None:
                raise
            return committed

    async def _append(self, event: DomainEvent) -> StoredEvent:
        stored = self.event_store.append(event)
        await self._emit_appended_batch((stored,))
        return stored

    def _transition_event(
        self,
        session_id: str,
        transition: TaskTransition,
        correlation_id: Optional[str],
        *,
        causation_id: Optional[str] = None,
    ) -> DomainEvent:
        return DomainEvent(
            stream_id=self._stream_id(session_id),
            event_type="task.status.changed",
            actor_id=self.SYSTEM_ACTOR.actor_id,
            correlation_id=correlation_id,
            causation_id=causation_id,
            idempotency_key="task-status:%s:%d" % (transition.task_id, transition.revision),
            payload={
                "taskId": transition.task_id,
                "previous": transition.previous.value,
                "current": transition.current.value,
                "reason": transition.reason,
                "revision": transition.revision,
            },
        )

    async def _record_transition(
        self,
        session_id: str,
        transition: TaskTransition,
        correlation_id: Optional[str],
        *,
        causation_id: Optional[str] = None,
    ) -> None:
        await self._append(
            self._transition_event(
                session_id,
                transition,
                correlation_id,
                causation_id=causation_id,
            )
        )

    async def _initialize_plan(self, plan: WorkflowPlan) -> TaskGraph:
        existing = self._plans.get(plan.session_id)
        if existing and existing.plan_id != plan.plan_id:
            raise ValueError("one active workflow plan is allowed per session in this MVP")
        if existing:
            if existing.to_dict() != plan.to_dict():
                raise ValueError("requested workflow content differs from the active plan")
            return self._graphs[plan.session_id]
        graph = TaskGraph(plan.tasks)
        initial_transitions = graph.refresh()
        stream_id = self._stream_id(plan.session_id)
        correlation_id = plan.correlation_id or plan.plan_id
        initialization_events = (
            DomainEvent(
                stream_id=stream_id,
                event_type="workflow.plan.created",
                actor_id=plan.initiated_by,
                correlation_id=correlation_id,
                idempotency_key="plan:%s" % plan.plan_id,
                payload=plan.to_dict(),
            ),
            *(
                DomainEvent(
                    stream_id=stream_id,
                    event_type="task.created",
                    actor_id=plan.initiated_by,
                    correlation_id=correlation_id,
                    idempotency_key="task-created:%s" % task.task_id,
                    payload=task.to_dict(),
                )
                for task in plan.tasks
            ),
            *(
                self._transition_event(
                    plan.session_id,
                    transition,
                    plan.correlation_id,
                )
                for transition in initial_transitions
            ),
        )
        expected_version = self.event_store.stream_version(stream_id)
        stored_events = self._append_many_reconciled(
            stream_id,
            initialization_events,
            expected_version=expected_version,
        )
        self._plans[plan.session_id] = plan
        self._graphs[plan.session_id] = graph
        await self._emit_appended_batch(stored_events)
        await self._emit_post_commit_observation(
            HookPoint.PLAN_CREATED,
            {"plan": plan, "graph": graph},
        )
        return graph

    @staticmethod
    def _validate_recovery_page(
        page: Tuple[StoredEvent, ...],
        *,
        stream_id: str,
        after_sequence: int,
        requested_limit: int,
    ) -> int:
        if type(page) is not tuple:
            raise SessionRecoveryError("session recovery source must return an immutable page")
        if len(page) > requested_limit:
            raise SessionRecoveryError("session recovery source exceeded its requested page limit")
        expected_sequence = after_sequence + 1
        for stored in page:
            if type(stored) is not StoredEvent:
                raise SessionRecoveryError("session recovery source returned an invalid event")
            if stored.event.stream_id != stream_id:
                raise SessionRecoveryError("session recovery source crossed a stream boundary")
            if type(stored.sequence) is not int or stored.sequence != expected_sequence:
                raise SessionRecoveryError("session recovery stream sequence is not contiguous")
            expected_sequence += 1
        return expected_sequence - 1

    @classmethod
    def _measure_recovery_event(cls, stored: StoredEvent) -> Tuple[int, int]:
        """Return canonical encoded bytes and an allocation-oriented JSON node count."""

        try:
            value = stored.event.to_dict()
            encoded_bytes = len(cls._canonical_event_json(stored.event).encode("utf-8"))
            nodes = 0
            pending: list[Any] = [value]
            while pending:
                current = pending.pop()
                nodes += 1
                if type(current) is dict:
                    # Count keys as well as values because both consume memory after decode.
                    nodes += len(current)
                    pending.extend(current.values())
                elif type(current) is list:
                    pending.extend(current)
                elif current is not None and type(current) not in (bool, int, float, str):
                    raise TypeError("event contains a non-JSON value")
        except (OverflowError, RecursionError, TypeError, UnicodeError, ValueError) as exc:
            raise SessionRecoveryError("session recovery event cannot be measured safely") from exc
        return encoded_bytes, nodes

    def _iter_session_events(self, stream_id: str) -> Iterator[StoredEvent]:
        """Yield a verified session history while retaining at most one decoded page."""

        after_sequence = 0
        replayed = 0
        replayed_bytes = 0
        replayed_nodes = 0
        while replayed < _MAX_RECOVERY_EVENTS:
            page_limit = min(
                _RECOVERY_PAGE_LIMIT,
                _MAX_RECOVERY_EVENTS - replayed,
            )
            page = self.event_store.read_stream_page(
                stream_id,
                after_sequence=after_sequence,
                limit=page_limit,
            )
            after_sequence = self._validate_recovery_page(
                page,
                stream_id=stream_id,
                after_sequence=after_sequence,
                requested_limit=page_limit,
            )
            if not page:
                return
            for stored in page:
                event_bytes, event_nodes = self._measure_recovery_event(stored)
                if event_bytes > _MAX_RECOVERY_BYTES - replayed_bytes:
                    raise SessionRecoveryError(
                        f"session recovery exceeds the {_MAX_RECOVERY_BYTES}-byte safety limit"
                    )
                if event_nodes > _MAX_RECOVERY_JSON_NODES - replayed_nodes:
                    raise SessionRecoveryError(
                        "session recovery exceeds the "
                        f"{_MAX_RECOVERY_JSON_NODES}-JSON-node safety limit"
                    )
                replayed_bytes += event_bytes
                replayed_nodes += event_nodes
                replayed += 1
                yield stored
            if len(page) < page_limit:
                return

        probe = self.event_store.read_stream_page(
            stream_id,
            after_sequence=after_sequence,
            limit=1,
        )
        self._validate_recovery_page(
            probe,
            stream_id=stream_id,
            after_sequence=after_sequence,
            requested_limit=1,
        )
        if probe:
            raise SessionRecoveryError(
                f"session recovery exceeds the {_MAX_RECOVERY_EVENTS}-event safety limit"
            )

    @staticmethod
    def _apply_recovered_transition(
        graph: TaskGraph,
        stored: StoredEvent,
        *,
        plan: WorkflowPlan,
        approval_request_id: Optional[str] = None,
    ) -> TaskTransition:
        payload = stored.event.payload
        expected_fields = {"taskId", "previous", "current", "reason", "revision"}
        if type(payload) is not dict or set(payload) != expected_fields:
            raise SessionRecoveryError("task transition payload has an invalid shape")

        task_id = payload["taskId"]
        previous_value = payload["previous"]
        current_value = payload["current"]
        reason = payload["reason"]
        revision = payload["revision"]
        if type(task_id) is not str or not task_id or len(task_id) > _MAX_RECOVERY_TEXT_LENGTH:
            raise SessionRecoveryError("task transition taskId is invalid")
        if type(previous_value) is not str or type(current_value) is not str:
            raise SessionRecoveryError("task transition status is invalid")
        if reason is not None and (
            type(reason) is not str or len(reason) > _MAX_RECOVERY_TEXT_LENGTH
        ):
            raise SessionRecoveryError("task transition reason is invalid")
        if type(revision) is not int or revision < 0:
            raise SessionRecoveryError("task transition revision is invalid")

        try:
            previous = TaskStatus(previous_value)
            current = TaskStatus(current_value)
            actual_previous = graph.statuses[task_id]
            actual_revision = graph.revisions[task_id]
        except (KeyError, ValueError) as exc:
            raise SessionRecoveryError("task transition references unknown state") from exc
        if previous != actual_previous:
            raise SessionRecoveryError("task transition previous status does not match history")
        expected_revision = actual_revision if current == previous else actual_revision + 1
        if revision != expected_revision:
            raise SessionRecoveryError("task transition revision is not contiguous")

        event = stored.event
        expected_correlation = plan.correlation_id or plan.plan_id
        allowed_causal_envelopes: Set[Tuple[Optional[str], Optional[str]]]
        if approval_request_id is not None:
            allowed_causal_envelopes = {
                (expected_correlation, approval_request_id),
            }
        else:
            if previous == TaskStatus.PENDING:
                allowed_correlations = {plan.correlation_id}
            elif previous == TaskStatus.WAITING_APPROVAL:
                allowed_correlations = {None, expected_correlation}
            else:
                allowed_correlations = {expected_correlation}
            allowed_causal_envelopes = {
                (correlation_id, None) for correlation_id in allowed_correlations
            }
        if (
            event.actor_id != OrchestratorKernel.SYSTEM_ACTOR.actor_id
            or (event.correlation_id, event.causation_id) not in allowed_causal_envelopes
            or event.idempotency_key != f"task-status:{task_id}:{revision}"
        ):
            raise SessionRecoveryError("task transition event envelope is inconsistent")
        try:
            transition = graph.transition(task_id, current, reason)
        except (KeyError, ValueError) as exc:
            raise SessionRecoveryError("task transition is not permitted") from exc
        if transition.revision != revision:
            raise SessionRecoveryError("task transition revision does not match replay state")
        return transition

    @staticmethod
    def _decode_recovered_plan(stored: StoredEvent) -> WorkflowPlan:
        payload = stored.event.payload
        if type(payload) is not dict:
            raise SessionRecoveryError("workflow plan payload must be a plain object")
        try:
            plan = WorkflowPlan.from_dict(payload)
            original_json = json.dumps(
                payload,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            decoded_json = json.dumps(
                plan.to_dict(),
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (KeyError, OverflowError, RecursionError, TypeError, ValueError) as exc:
            raise SessionRecoveryError("workflow plan payload is invalid") from exc
        if original_json != decoded_json:
            raise SessionRecoveryError("workflow plan payload is not canonical")

        event = stored.event
        if (
            event.actor_id != plan.initiated_by
            or event.idempotency_key != f"plan:{plan.plan_id}"
            or event.correlation_id != (plan.correlation_id or plan.plan_id)
            or event.causation_id is not None
        ):
            raise SessionRecoveryError("workflow plan event envelope is inconsistent")
        return plan

    @staticmethod
    def _decode_recovered_task_creation(
        stored: StoredEvent,
        *,
        plan: WorkflowPlan,
    ) -> str:
        payload = stored.event.payload
        if type(payload) is not dict:
            raise SessionRecoveryError("task creation payload is invalid")
        task_id = payload.get("taskId")
        if type(task_id) is not str:
            raise SessionRecoveryError("task creation payload is invalid")
        expected_by_id = {task.task_id: task for task in plan.tasks}
        task = expected_by_id.get(task_id)
        if task is None:
            raise SessionRecoveryError("task creation event references an unknown task")
        try:
            actual_json = json.dumps(
                payload,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            expected_json = json.dumps(
                task.to_dict(),
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (OverflowError, RecursionError, TypeError, ValueError) as exc:
            raise SessionRecoveryError("task creation payload is invalid") from exc
        if actual_json != expected_json:
            raise SessionRecoveryError("task creation payload is not canonical")

        event = stored.event
        if (
            event.actor_id != plan.initiated_by
            or event.idempotency_key != f"task-created:{task_id}"
            or event.correlation_id != (plan.correlation_id or plan.plan_id)
            or event.causation_id is not None
        ):
            raise SessionRecoveryError("task creation event envelope is inconsistent")
        return task_id

    @staticmethod
    def _decode_recovered_approval(
        stored: StoredEvent,
        *,
        plan: WorkflowPlan,
        graph: TaskGraph,
        decided: bool,
    ) -> ApprovalRequest:
        event = stored.event
        payload = event.payload
        if type(payload) is not dict or frozenset(payload) != _APPROVAL_PAYLOAD_FIELDS:
            raise SessionRecoveryError("approval payload has an invalid shape")

        required_text_fields = ("requestId", "sessionId", "taskId", "reason", "createdAt")
        for field_name in required_text_fields:
            value = payload[field_name]
            if type(value) is not str or not value or len(value) > _MAX_RECOVERY_TEXT_LENGTH:
                raise SessionRecoveryError(f"approval {field_name} is invalid")
        if type(payload["intent"]) is not dict:
            raise SessionRecoveryError("approval intent is invalid")
        raw_comment = payload["comment"]
        if raw_comment is not None and (
            type(raw_comment) is not str or len(raw_comment) > _MAX_RECOVERY_TEXT_LENGTH
        ):
            raise SessionRecoveryError("approval comment is invalid")

        raw_decision = payload["decision"]
        raw_decided_by = payload["decidedBy"]
        if decided:
            if type(raw_decision) is not str:
                raise SessionRecoveryError("approval decision is invalid")
            if (
                type(raw_decided_by) is not str
                or not raw_decided_by
                or len(raw_decided_by) > _MAX_RECOVERY_TEXT_LENGTH
            ):
                raise SessionRecoveryError("approval decidedBy is invalid")
            try:
                decision = ApprovalDecision(raw_decision)
            except ValueError as exc:
                raise SessionRecoveryError("approval decision is invalid") from exc
        else:
            if raw_decision is not None or raw_decided_by is not None or raw_comment is not None:
                raise SessionRecoveryError("approval request already contains a decision")
            decision = None

        created_at = payload["createdAt"]
        if _RECOVERY_UTC_PATTERN.fullmatch(created_at) is None:
            raise SessionRecoveryError("approval createdAt is not canonical UTC")
        try:
            parsed_created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            parsed_event_at = datetime.fromisoformat(event.timestamp.replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise SessionRecoveryError("approval timestamp is invalid") from exc
        if (
            parsed_created_at.tzinfo is None
            or parsed_event_at.tzinfo is None
            or parsed_created_at.astimezone(timezone.utc) > parsed_event_at.astimezone(timezone.utc)
        ):
            raise SessionRecoveryError("approval timestamp violates event causality")

        try:
            intent = ActionIntent.from_dict(payload["intent"])
            request = ApprovalRequest(
                session_id=payload["sessionId"],
                task_id=payload["taskId"],
                intent=intent,
                reason=payload["reason"],
                request_id=payload["requestId"],
                created_at=created_at,
                decision=decision,
                decided_by=raw_decided_by,
                comment=raw_comment,
            )
            original_json = json.dumps(
                payload,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            decoded_json = json.dumps(
                request.to_dict(),
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (KeyError, OverflowError, RecursionError, TypeError, ValueError) as exc:
            raise SessionRecoveryError("approval payload is invalid") from exc
        if original_json != decoded_json:
            raise SessionRecoveryError("approval payload is not canonical")

        task = graph.tasks.get(request.task_id)
        if task is None:
            raise SessionRecoveryError("approval references an unknown task")
        if request.session_id != plan.session_id:
            raise SessionRecoveryError("approval crosses the workflow session boundary")
        if request.intent != task.action:
            raise SessionRecoveryError("approval intent differs from the task action")
        if decided:
            expected_actor = request.decided_by
            expected_causation = request.request_id
            expected_idempotency = f"approval-decision:{request.request_id}"
        else:
            expected_actor = OrchestratorKernel.SYSTEM_ACTOR.actor_id
            expected_causation = request.task_id
            expected_idempotency = f"approval-request:{request.task_id}"
        if (
            event.actor_id != expected_actor
            or event.correlation_id != (plan.correlation_id or plan.plan_id)
            or event.causation_id != expected_causation
            or event.idempotency_key != expected_idempotency
        ):
            raise SessionRecoveryError("approval event envelope is inconsistent")
        return request

    @staticmethod
    def _same_approval_request(
        requested: ApprovalRequest,
        decided: ApprovalRequest,
    ) -> bool:
        return bool(
            requested.request_id == decided.request_id
            and requested.session_id == decided.session_id
            and requested.task_id == decided.task_id
            and requested.intent == decided.intent
            and requested.reason == decided.reason
            and requested.created_at == decided.created_at
        )

    @staticmethod
    def _require_no_unreconciled_running_tasks(graph: TaskGraph) -> None:
        if any(status is TaskStatus.RUNNING for status in graph.statuses.values()):
            raise SessionRecoveryError(
                "session contains a durably RUNNING task without supported "
                "invocation recovery evidence"
            )

    def _recover_session(self, requested_plan: WorkflowPlan) -> None:
        """Rebuild an active workflow projection without replaying side effects."""

        if requested_plan.session_id in self._plans:
            return
        created: Optional[StoredEvent] = None
        stored_plan: Optional[WorkflowPlan] = None
        graph: Optional[TaskGraph] = None
        actual_task_ids: list[str] = []
        saw_transition = False
        pre_plan_state_event = False
        approval_requests: Dict[str, ApprovalRequest] = {}
        request_ids_by_task: Dict[str, str] = {}
        awaiting_request: Dict[str, int] = {}
        awaiting_decision_transition: Dict[
            str,
            Tuple[str, TaskStatus, Optional[str], int],
        ] = {}
        for stored in self._iter_session_events(self._stream_id(requested_plan.session_id)):
            event = stored.event
            if event.event_type == "workflow.plan.created":
                if created is not None:
                    raise SessionRecoveryError("session contains multiple workflow plan events")
                created = stored
                stored_plan = self._decode_recovered_plan(created)
                if stored_plan.plan_id != requested_plan.plan_id:
                    raise ValueError(
                        "stored workflow plan %s does not match requested plan %s"
                        % (stored_plan.plan_id, requested_plan.plan_id)
                    )
                if stored_plan.to_dict() != requested_plan.to_dict():
                    raise ValueError("requested workflow content differs from the stored plan")
                if pre_plan_state_event:
                    raise SessionRecoveryError("session state events precede the workflow plan")
                graph = TaskGraph(stored_plan.tasks)
                continue

            if event.event_type == "task.created":
                if stored_plan is None:
                    pre_plan_state_event = True
                    continue
                task_id = self._decode_recovered_task_creation(stored, plan=stored_plan)
                if saw_transition:
                    raise SessionRecoveryError(
                        "task creation events overlap task transition history"
                    )
                actual_task_ids.append(task_id)
                continue

            if event.event_type not in {
                "task.status.changed",
                "approval.requested",
                "approval.decided",
            }:
                continue
            if stored_plan is None or graph is None:
                pre_plan_state_event = True
                continue

            if event.event_type == "task.status.changed":
                saw_transition = True
                expected_task_ids = tuple(task.task_id for task in stored_plan.tasks)
                if len(actual_task_ids) >= len(expected_task_ids) and (
                    tuple(actual_task_ids) != expected_task_ids
                ):
                    raise SessionRecoveryError("task creation events do not exactly match the plan")
                payload = event.payload
                pending_decision = None
                if (
                    type(payload) is dict
                    and payload.get("previous") == TaskStatus.WAITING_APPROVAL.value
                    and type(payload.get("taskId")) is str
                ):
                    pending_decision = awaiting_decision_transition.get(payload["taskId"])
                transition = self._apply_recovered_transition(
                    graph,
                    stored,
                    plan=stored_plan,
                    approval_request_id=(pending_decision[0] if pending_decision else None),
                )
                task_id = transition.task_id
                previous = transition.previous
                current = transition.current
                reason = transition.reason
                if previous == TaskStatus.WAITING_APPROVAL:
                    expected = awaiting_decision_transition.pop(task_id, None)
                    if expected is None:
                        raise SessionRecoveryError(
                            "approval-gated task transitioned without a decision"
                        )
                    _request_id, expected_status, expected_reason, _decision_sequence = expected
                    if (current, reason) != (expected_status, expected_reason):
                        raise SessionRecoveryError(
                            "approval decision does not match its task transition"
                        )
                    if stored.sequence != _decision_sequence + 1:
                        raise SessionRecoveryError(
                            "approval decision is not adjacent to its task transition"
                        )
                if current == TaskStatus.WAITING_APPROVAL:
                    if (
                        previous == TaskStatus.WAITING_APPROVAL
                        or task_id in awaiting_request
                        or task_id in request_ids_by_task
                    ):
                        raise SessionRecoveryError(
                            "approval-gated task has duplicate waiting history"
                        )
                    awaiting_request[task_id] = stored.sequence
            elif event.event_type == "approval.requested":
                request = self._decode_recovered_approval(
                    stored,
                    plan=stored_plan,
                    graph=graph,
                    decided=False,
                )
                if (
                    graph.statuses[request.task_id] != TaskStatus.WAITING_APPROVAL
                    or request.task_id not in awaiting_request
                ):
                    raise SessionRecoveryError(
                        "approval request is not caused by a waiting task transition"
                    )
                if (
                    request.request_id in approval_requests
                    or request.task_id in request_ids_by_task
                ):
                    raise SessionRecoveryError("approval request is not unique")
                if request.reason != graph.reasons.get(request.task_id):
                    raise SessionRecoveryError(
                        "approval request reason differs from the waiting transition"
                    )
                waiting_sequence = awaiting_request.pop(request.task_id)
                if stored.sequence != waiting_sequence + 1:
                    raise SessionRecoveryError(
                        "approval request is not adjacent to its waiting transition"
                    )
                approval_requests[request.request_id] = request
                request_ids_by_task[request.task_id] = request.request_id
            elif event.event_type == "approval.decided":
                decided_request = self._decode_recovered_approval(
                    stored,
                    plan=stored_plan,
                    graph=graph,
                    decided=True,
                )
                requested = approval_requests.get(decided_request.request_id)
                if requested is None:
                    raise SessionRecoveryError("approval decision has no prior request")
                if requested.decision is not None:
                    raise SessionRecoveryError("approval request has multiple decisions")
                if not self._same_approval_request(requested, decided_request):
                    raise SessionRecoveryError("approval decision changes its request identity")
                if graph.statuses[decided_request.task_id] != TaskStatus.WAITING_APPROVAL:
                    raise SessionRecoveryError(
                        "approval decision targets a task that is not waiting"
                    )
                if decided_request.task_id in awaiting_decision_transition:
                    raise SessionRecoveryError("approval task has an unresolved decision")
                decision = decided_request.decision
                if decision is None:
                    raise SessionRecoveryError("approval decision is missing")
                approval_requests[decided_request.request_id] = decided_request
                awaiting_decision_transition[decided_request.task_id] = (
                    decided_request.request_id,
                    _APPROVAL_DECISION_TARGETS[decision],
                    decided_request.comment,
                    stored.sequence,
                )

        if created is None:
            return
        if stored_plan is None or graph is None:
            raise SessionRecoveryError("workflow plan recovery state is incomplete")
        expected_task_ids = tuple(task.task_id for task in stored_plan.tasks)
        if tuple(actual_task_ids) != expected_task_ids:
            raise SessionRecoveryError("task creation events do not exactly match the plan")
        if awaiting_request:
            raise SessionRecoveryError("approval waiting transition has no request")
        if awaiting_decision_transition:
            raise SessionRecoveryError("approval decision has no matching task transition")
        for request in approval_requests.values():
            if request.pending and graph.statuses[request.task_id] != TaskStatus.WAITING_APPROVAL:
                raise SessionRecoveryError("pending approval is not attached to a waiting task")
        self._require_no_unreconciled_running_tasks(graph)

        self._plans[requested_plan.session_id] = stored_plan
        self._graphs[requested_plan.session_id] = graph
        for request in approval_requests.values():
            self.approvals.restore(request)
            if request.decision == ApprovalDecision.APPROVE:
                self._approved_tasks.add((request.session_id, request.task_id))
        for task in stored_plan.tasks:
            refs = tuple(
                item.ref for item in self.artifacts.by_task(stored_plan.session_id, task.task_id)
            )
            if refs:
                self._task_artifacts[(stored_plan.session_id, task.task_id)] = refs

    def _artifact_items(self, plan: WorkflowPlan, task: TaskSpec) -> Tuple[ContextItem, ...]:
        items = []
        seen = set()
        dependency_refs: list[ArtifactRef] = []
        for dependency in task.depends_on:
            dependency_refs.extend(self._task_artifacts.get((plan.session_id, dependency), ()))
        for ref in dependency_refs:
            if ref.name in seen:
                continue
            current = self.artifacts.current(plan.session_id, ref.name)
            if current:
                seen.add(ref.name)
                items.append(
                    ContextItem(
                        item_id="artifact:%s:v%d" % (ref.name, current.ref.version),
                        category="artifact",
                        content=current.content,
                        required=True,
                        relevance=1.0,
                        provenance=current.ref.uri,
                        metadata={"ref": current.ref.to_dict()},
                    )
                )
        for name in task.input_artifacts:
            if name in seen:
                continue
            current = self.artifacts.current(plan.session_id, name)
            if current:
                seen.add(name)
                items.append(
                    ContextItem(
                        item_id="artifact:%s:v%d" % (name, current.ref.version),
                        category="artifact",
                        content=current.content,
                        required=True,
                        relevance=1.0,
                        provenance=current.ref.uri,
                        metadata={"ref": current.ref.to_dict()},
                    )
                )
        return tuple(items)

    async def _compile_context(self, plan: WorkflowPlan, task: TaskSpec) -> ContextBundle:
        items = [
            ContextItem("goal", "goal", plan.goal, required=True, relevance=1.0, provenance="user"),
            ContextItem(
                "handoff:%s" % task.task_id,
                "handoff",
                json.dumps(task.handoff.to_dict(), ensure_ascii=False, sort_keys=True),
                required=True,
                relevance=1.0,
                provenance="planner",
            ),
        ]
        items.extend(self._artifact_items(plan, task))
        plugin_context: Dict[str, Any] = {"plan": plan, "task": task, "items": items}
        await self.plugins.emit(HookPoint.CONTEXT_BUILD, plugin_context)
        budget = task.handoff.token_budget or self.default_context_budget
        bundle = self.context_compiler.compile(plugin_context["items"], budget)
        # DSH invariant: anything visible to a model/agent is reconstructable from the event log.
        await self._append(
            DomainEvent(
                stream_id=self._stream_id(plan.session_id),
                event_type="context.compiled",
                actor_id=self.SYSTEM_ACTOR.actor_id,
                correlation_id=plan.correlation_id or plan.plan_id,
                causation_id=task.task_id,
                idempotency_key="context:%s:%s" % (task.task_id, bundle.digest),
                payload={"taskId": task.task_id, "bundle": bundle.to_dict()},
            )
        )
        await self.plugins.emit(
            HookPoint.CONTEXT_COMPILED, {"plan": plan, "task": task, "bundle": bundle}
        )
        return bundle

    async def _run_task(self, plan: WorkflowPlan, graph: TaskGraph, task: TaskSpec) -> None:
        correlation_id = plan.correlation_id or plan.plan_id
        dispatch_context: Dict[str, Any] = {"plan": plan, "task": task, "graph": graph}
        await self.plugins.emit(HookPoint.BEFORE_DISPATCH, dispatch_context)
        approval_key = (plan.session_id, task.task_id)
        decision = self.policy.evaluate(task.action, task.handoff.authority)
        if (
            approval_key in self._approved_tasks
            and decision.outcome == PolicyOutcome.NEEDS_APPROVAL
        ):
            decision = type(decision)(PolicyOutcome.ALLOW, "covered by task-scoped human approval")
        if decision.outcome == PolicyOutcome.DENY:
            running = graph.transition(task.task_id, TaskStatus.RUNNING)
            await self._record_transition(plan.session_id, running, correlation_id)
            failed = graph.transition(task.task_id, TaskStatus.FAILED, decision.reason)
            await self._record_transition(plan.session_id, failed, correlation_id)
            return
        if decision.outcome == PolicyOutcome.NEEDS_APPROVAL:
            if any(
                request.task_id == task.task_id
                for request in self.approvals.pending(plan.session_id)
            ):
                raise RuntimeError("task already has a pending approval request")
            running = TaskTransition(
                task.task_id,
                TaskStatus.READY,
                TaskStatus.RUNNING,
                None,
                graph.revisions[task.task_id] + 1,
            )
            waiting = TaskTransition(
                task.task_id,
                TaskStatus.RUNNING,
                TaskStatus.WAITING_APPROVAL,
                decision.reason,
                graph.revisions[task.task_id] + 2,
            )
            request = ApprovalRequest(
                plan.session_id,
                task.task_id,
                task.action,
                decision.reason,
            )
            stream_id = self._stream_id(plan.session_id)
            expected_version = self.event_store.stream_version(stream_id)
            request_events = (
                self._transition_event(
                    plan.session_id,
                    running,
                    correlation_id,
                ),
                self._transition_event(
                    plan.session_id,
                    waiting,
                    correlation_id,
                ),
                DomainEvent(
                    stream_id=stream_id,
                    event_type="approval.requested",
                    actor_id=self.SYSTEM_ACTOR.actor_id,
                    correlation_id=correlation_id,
                    causation_id=task.task_id,
                    idempotency_key="approval-request:%s" % task.task_id,
                    payload=request.to_dict(),
                ),
            )
            stored_events = self._append_many_reconciled(
                stream_id,
                request_events,
                expected_version=expected_version,
            )
            applied_running = graph.transition(task.task_id, TaskStatus.RUNNING)
            if applied_running != running:
                raise RuntimeError("persisted approval dispatch differs from memory")
            applied_waiting = graph.transition(
                task.task_id,
                TaskStatus.WAITING_APPROVAL,
                decision.reason,
            )
            if applied_waiting != waiting:
                raise RuntimeError("persisted approval transition differs from memory")
            created_request = self.approvals.create(request)
            if created_request.to_dict() != request.to_dict():
                raise RuntimeError("persisted approval request differs from memory")
            await self._emit_appended_batch(stored_events)
            return

        running = graph.transition(task.task_id, TaskStatus.RUNNING)
        await self._record_transition(plan.session_id, running, correlation_id)
        try:
            bundle = await self._compile_context(plan, task)
            agent = self.registry.get(task.agent_id)
            envelope = CoordinationEnvelope.create(
                session_id=plan.session_id,
                thread_id=task.task_id,
                sender=self.SYSTEM_ACTOR,
                recipients=(agent.actor,),
                kind=EnvelopeKind.TASK_ASSIGN,
                payload={"task": task.to_dict(), "handoff": task.handoff.to_dict()},
                correlation_id=correlation_id,
                causation_id=task.task_id,
                idempotency_key="invoke:%s" % task.task_id,
                authority=task.handoff.authority,
            )
            invocation = AgentInvocation(task, envelope, bundle)
            await self._append(
                DomainEvent(
                    stream_id=self._stream_id(plan.session_id),
                    event_type="task.invocation.started",
                    actor_id=self.SYSTEM_ACTOR.actor_id,
                    correlation_id=correlation_id,
                    causation_id=task.task_id,
                    idempotency_key="invocation-started:%s" % task.task_id,
                    payload={
                        "taskId": task.task_id,
                        "agentId": task.agent_id,
                        "envelope": envelope.to_dict(),
                        "contextDigest": bundle.digest,
                    },
                )
            )
            await self.plugins.emit(
                HookPoint.BEFORE_AGENT, {"plan": plan, "task": task, "invocation": invocation}
            )
            result = await self.registry.invoke(invocation)
            await self.plugins.emit(
                HookPoint.AFTER_AGENT,
                {"plan": plan, "task": task, "invocation": invocation, "result": result},
            )
            refs = []
            for output in result.artifacts:
                item = self.artifacts.record(
                    plan.session_id,
                    task.task_id,
                    task.agent_id,
                    output,
                    correlation_id=correlation_id,
                    causation_id=task.task_id,
                )
                refs.append(item.ref)
            self._task_artifacts[(plan.session_id, task.task_id)] = tuple(refs)
            await self._append(
                DomainEvent(
                    stream_id=self._stream_id(plan.session_id),
                    event_type="task.result.received",
                    actor_id=task.agent_id,
                    correlation_id=correlation_id,
                    causation_id=task.task_id,
                    idempotency_key="task-result:%s" % task.task_id,
                    payload={
                        "taskId": task.task_id,
                        "narration": result.narration,
                        "artifacts": [ref.to_dict() for ref in refs],
                        "metadata": dict(result.metadata),
                    },
                )
            )
            completed = graph.transition(task.task_id, TaskStatus.COMPLETED)
            await self._record_transition(plan.session_id, completed, correlation_id)
        except Exception as exc:
            failed = graph.transition(task.task_id, TaskStatus.FAILED, str(exc))
            await self._record_transition(plan.session_id, failed, correlation_id)
        finally:
            await self.plugins.emit(HookPoint.AFTER_DISPATCH, dispatch_context)

    async def run(self, plan: WorkflowPlan) -> RunResult:
        if self._closing or self._closed:
            raise AgentRuntimeClosedError("orchestrator kernel is closing or closed")
        lock = self._session_locks.setdefault(plan.session_id, asyncio.Lock())
        async with lock:
            self._recover_session(plan)
            recovered_graph = self._graphs.get(plan.session_id)
            if recovered_graph is not None:
                self._require_no_unreconciled_running_tasks(recovered_graph)
            graph = await self._initialize_plan(plan)
            active_plan = self._plans[plan.session_id]
            while True:
                for transition in graph.refresh():
                    await self._record_transition(
                        active_plan.session_id, transition, active_plan.correlation_id
                    )
                ready = graph.ready()
                if not ready:
                    break
                semaphore = asyncio.Semaphore(self.max_concurrency)

                async def guarded(task: TaskSpec, limiter: asyncio.Semaphore = semaphore) -> None:
                    async with limiter:
                        await self._run_task(active_plan, graph, task)

                await asyncio.gather(*(guarded(task) for task in ready))
            return self._result(active_plan, graph)

    def _result(self, plan: WorkflowPlan, graph: TaskGraph) -> RunResult:
        return RunResult(
            session_id=plan.session_id,
            plan_id=plan.plan_id,
            statuses=dict(graph.statuses),
            artifacts=tuple(item.ref for item in self.artifacts.current_all(plan.session_id)),
            needs_you=self.approvals.pending(plan.session_id),
            errors={
                task_id: reason
                for task_id, reason in graph.reasons.items()
                if graph.statuses[task_id] in FAILED_STATUSES
            },
        )

    async def decide(
        self,
        request_id: str,
        decision: ApprovalDecision,
        actor_id: str,
        comment: Optional[str] = None,
    ) -> ApprovalRequest:
        if self._closing or self._closed:
            raise AgentRuntimeClosedError("orchestrator kernel is closing or closed")
        if type(decision) is not ApprovalDecision:
            raise TypeError("decision must be an ApprovalDecision")
        if type(actor_id) is not str or not actor_id or len(actor_id) > _MAX_RECOVERY_TEXT_LENGTH:
            raise ValueError("actor_id is invalid")
        if comment is not None and (
            type(comment) is not str or len(comment) > _MAX_RECOVERY_TEXT_LENGTH
        ):
            raise ValueError("comment is invalid")

        initial_request = self.approvals.get(request_id)
        lock = self._session_locks.setdefault(initial_request.session_id, asyncio.Lock())
        async with lock:
            request = self.approvals.get(request_id)
            if not request.pending:
                return self.approvals.decide(request_id, decision, actor_id, comment)
            plan = self._plans[request.session_id]
            graph = self._graphs[request.session_id]
            target = {
                ApprovalDecision.APPROVE: TaskStatus.READY,
                ApprovalDecision.REVISE: TaskStatus.WAITING_INPUT,
                ApprovalDecision.REJECT: TaskStatus.CANCELED,
            }[decision]
            if graph.statuses[request.task_id] != TaskStatus.WAITING_APPROVAL:
                raise RuntimeError("approval task is not waiting for a decision")
            transition = TaskTransition(
                request.task_id,
                TaskStatus.WAITING_APPROVAL,
                target,
                comment,
                graph.revisions[request.task_id] + 1,
            )
            decided_request = ApprovalRequest(
                session_id=request.session_id,
                task_id=request.task_id,
                intent=request.intent,
                reason=request.reason,
                request_id=request.request_id,
                created_at=request.created_at,
                decision=decision,
                decided_by=actor_id,
                comment=comment,
            )
            correlation_id = plan.correlation_id or plan.plan_id
            stream_id = self._stream_id(request.session_id)
            expected_version = self.event_store.stream_version(stream_id)
            decision_events = (
                DomainEvent(
                    stream_id=stream_id,
                    event_type="approval.decided",
                    actor_id=actor_id,
                    correlation_id=correlation_id,
                    causation_id=request.request_id,
                    idempotency_key="approval-decision:%s" % request.request_id,
                    payload=decided_request.to_dict(),
                ),
                self._transition_event(
                    request.session_id,
                    transition,
                    correlation_id,
                    causation_id=request.request_id,
                ),
            )
            stored_events = self._append_many_reconciled(
                stream_id,
                decision_events,
                expected_version=expected_version,
            )
            committed_request = self.approvals.decide(
                request_id,
                decision,
                actor_id,
                comment,
            )
            if committed_request.to_dict() != decided_request.to_dict():
                raise RuntimeError("persisted approval decision differs from memory")
            applied_transition = graph.transition(request.task_id, target, comment)
            if applied_transition != transition:
                raise RuntimeError("persisted approval transition differs from memory")
            approval_key = (request.session_id, request.task_id)
            if decision == ApprovalDecision.APPROVE:
                self._approved_tasks.add(approval_key)
            else:
                self._approved_tasks.discard(approval_key)

        await self._emit_appended_batch(stored_events)
        return committed_request
