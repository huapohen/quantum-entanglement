"""Plugin-based, event-sourced multi-agent execution kernel."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional, Set, Tuple

from .artifacts import ArtifactLedger
from .context import ContextBundle, ContextCompiler, ContextItem
from .events import DomainEvent, StoredEvent
from .plugins import HookPoint, PluginManager
from .policy import ApprovalRequest, NeedsYouQueue, PolicyEngine, PolicyOutcome
from .protocol import (
    ActorKind,
    ActorRef,
    ApprovalDecision,
    ArtifactOutput,
    ArtifactRef,
    CoordinationEnvelope,
    EnvelopeKind,
    TaskStatus,
)
from .scheduler import FAILED_STATUSES, TaskGraph, TaskSpec, TaskTransition, WorkflowPlan
from .store import SQLiteEventStore


@dataclass(frozen=True)
class AgentInvocation:
    task: TaskSpec
    envelope: CoordinationEnvelope
    context: ContextBundle


@dataclass(frozen=True)
class AgentResult:
    narration: str
    artifacts: Tuple[ArtifactOutput, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


AgentHandler = Callable[[AgentInvocation], Awaitable[AgentResult]]


@dataclass(frozen=True)
class AgentRegistration:
    actor: ActorRef
    handler: AgentHandler
    skills: Tuple[str, ...] = ()
    protocol: str = "in-process"


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: Dict[str, AgentRegistration] = {}

    def register(self, registration: AgentRegistration) -> None:
        if registration.actor.kind != ActorKind.AGENT:
            raise ValueError("only agent actors can be registered")
        if registration.actor.actor_id in self._agents:
            raise ValueError("agent already registered: %s" % registration.actor.actor_id)
        self._agents[registration.actor.actor_id] = registration

    def get(self, agent_id: str) -> AgentRegistration:
        try:
            return self._agents[agent_id]
        except KeyError:
            raise KeyError("agent is not registered: %s" % agent_id)

    async def invoke(self, invocation: AgentInvocation) -> AgentResult:
        return await self.get(invocation.task.agent_id).handler(invocation)


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
        self._task_artifacts: Dict[Tuple[str, str], Tuple[ArtifactRef, ...]] = {}
        # An approval is a scoped capability for exactly this workflow task. Keeping it
        # separate from delegated authority prevents the next dispatch from requesting
        # the same approval forever.
        self._approved_tasks: Set[Tuple[str, str]] = set()

    def register_agent(self, registration: AgentRegistration) -> None:
        self.registry.register(registration)

    def _stream_id(self, session_id: str) -> str:
        return "session:%s" % session_id

    async def _append(self, event: DomainEvent) -> StoredEvent:
        stored = self.event_store.append(event)
        context: Dict[str, Any] = {"storedEvent": stored, "kernel": self}
        await self.plugins.emit(HookPoint.EVENT_APPENDED, context)
        return stored

    async def _record_transition(
        self, session_id: str, transition: TaskTransition, correlation_id: Optional[str]
    ) -> None:
        await self._append(
            DomainEvent(
                stream_id=self._stream_id(session_id),
                event_type="task.status.changed",
                actor_id=self.SYSTEM_ACTOR.actor_id,
                correlation_id=correlation_id,
                idempotency_key="task-status:%s:%d"
                % (transition.task_id, transition.revision),
                payload={
                    "taskId": transition.task_id,
                    "previous": transition.previous.value,
                    "current": transition.current.value,
                    "reason": transition.reason,
                    "revision": transition.revision,
                },
            )
        )

    async def _initialize_plan(self, plan: WorkflowPlan) -> TaskGraph:
        existing = self._plans.get(plan.session_id)
        if existing and existing.plan_id != plan.plan_id:
            raise ValueError("one active workflow plan is allowed per session in this MVP")
        if existing:
            return self._graphs[plan.session_id]
        graph = TaskGraph(plan.tasks)
        self._plans[plan.session_id] = plan
        self._graphs[plan.session_id] = graph
        await self._append(
            DomainEvent(
                stream_id=self._stream_id(plan.session_id),
                event_type="workflow.plan.created",
                actor_id=plan.initiated_by,
                correlation_id=plan.correlation_id or plan.plan_id,
                idempotency_key="plan:%s" % plan.plan_id,
                payload=plan.to_dict(),
            )
        )
        for task in plan.tasks:
            await self._append(
                DomainEvent(
                    stream_id=self._stream_id(plan.session_id),
                    event_type="task.created",
                    actor_id=plan.initiated_by,
                    correlation_id=plan.correlation_id or plan.plan_id,
                    idempotency_key="task-created:%s" % task.task_id,
                    payload=task.to_dict(),
                )
            )
        for transition in graph.refresh():
            await self._record_transition(plan.session_id, transition, plan.correlation_id)
        await self.plugins.emit(HookPoint.PLAN_CREATED, {"plan": plan, "graph": graph})
        return graph

    def _artifact_items(self, plan: WorkflowPlan, task: TaskSpec) -> Tuple[ContextItem, ...]:
        items = []
        seen = set()
        dependency_refs = []
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
        if approval_key in self._approved_tasks and decision.outcome == PolicyOutcome.NEEDS_APPROVAL:
            decision = type(decision)(PolicyOutcome.ALLOW, "covered by task-scoped human approval")
        if decision.outcome == PolicyOutcome.DENY:
            running = graph.transition(task.task_id, TaskStatus.RUNNING)
            await self._record_transition(plan.session_id, running, correlation_id)
            failed = graph.transition(task.task_id, TaskStatus.FAILED, decision.reason)
            await self._record_transition(plan.session_id, failed, correlation_id)
            return
        if decision.outcome == PolicyOutcome.NEEDS_APPROVAL:
            # Enter RUNNING first so the lifecycle records the attempted dispatch.
            running = graph.transition(task.task_id, TaskStatus.RUNNING)
            await self._record_transition(plan.session_id, running, correlation_id)
            waiting = graph.transition(task.task_id, TaskStatus.WAITING_APPROVAL, decision.reason)
            await self._record_transition(plan.session_id, waiting, correlation_id)
            request = self.approvals.create(
                ApprovalRequest(plan.session_id, task.task_id, task.action, decision.reason)
            )
            await self._append(
                DomainEvent(
                    stream_id=self._stream_id(plan.session_id),
                    event_type="approval.requested",
                    actor_id=self.SYSTEM_ACTOR.actor_id,
                    correlation_id=correlation_id,
                    causation_id=task.task_id,
                    idempotency_key="approval-request:%s" % task.task_id,
                    payload=request.to_dict(),
                )
            )
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
        lock = self._session_locks.setdefault(plan.session_id, asyncio.Lock())
        async with lock:
            graph = await self._initialize_plan(plan)
            while True:
                for transition in graph.refresh():
                    await self._record_transition(plan.session_id, transition, plan.correlation_id)
                ready = graph.ready()
                if not ready:
                    break
                semaphore = asyncio.Semaphore(self.max_concurrency)

                async def guarded(task: TaskSpec) -> None:
                    async with semaphore:
                        await self._run_task(plan, graph, task)

                await asyncio.gather(*(guarded(task) for task in ready))
            return self._result(plan, graph)

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
        request = self.approvals.decide(request_id, decision, actor_id, comment)
        graph = self._graphs[request.session_id]
        if decision == ApprovalDecision.APPROVE:
            self._approved_tasks.add((request.session_id, request.task_id))
            transition = graph.transition(request.task_id, TaskStatus.READY, comment)
        elif decision == ApprovalDecision.REVISE:
            self._approved_tasks.discard((request.session_id, request.task_id))
            transition = graph.transition(request.task_id, TaskStatus.WAITING_INPUT, comment)
        else:
            self._approved_tasks.discard((request.session_id, request.task_id))
            transition = graph.transition(request.task_id, TaskStatus.CANCELED, comment)
        await self._append(
            DomainEvent(
                stream_id=self._stream_id(request.session_id),
                event_type="approval.decided",
                actor_id=actor_id,
                correlation_id=self._plans[request.session_id].correlation_id,
                causation_id=request.request_id,
                idempotency_key="approval-decision:%s" % request.request_id,
                payload=request.to_dict(),
            )
        )
        await self._record_transition(request.session_id, transition, None)
        return request
