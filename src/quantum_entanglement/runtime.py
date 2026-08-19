"""Plugin-based, event-sourced multi-agent execution kernel."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Set, Tuple

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
            if existing.to_dict() != plan.to_dict():
                raise ValueError("requested workflow content differs from the active plan")
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

    def _recover_session(self, requested_plan: WorkflowPlan) -> None:
        """Rebuild an active workflow projection without replaying side effects."""

        if requested_plan.session_id in self._plans:
            return
        events = self.event_store.read_stream(self._stream_id(requested_plan.session_id))
        created = next(
            (item.event for item in events if item.event.event_type == "workflow.plan.created"),
            None,
        )
        if created is None:
            return
        stored_plan = WorkflowPlan.from_dict(created.payload)
        if stored_plan.plan_id != requested_plan.plan_id:
            raise ValueError(
                "stored workflow plan %s does not match requested plan %s"
                % (stored_plan.plan_id, requested_plan.plan_id)
            )
        if stored_plan.to_dict() != requested_plan.to_dict():
            raise ValueError("requested workflow content differs from the stored plan")
        graph = TaskGraph(stored_plan.tasks)
        approval_requests: Dict[str, ApprovalRequest] = {}
        for stored in events:
            event = stored.event
            if event.event_type == "task.status.changed":
                payload = event.payload
                graph.restore_status(
                    str(payload["taskId"]),
                    TaskStatus(str(payload["current"])),
                    str(payload["reason"]) if payload.get("reason") else None,
                    int(payload.get("revision", 0)),
                )
            elif event.event_type in ("approval.requested", "approval.decided"):
                request = ApprovalRequest.from_dict(dict(event.payload))
                approval_requests[request.request_id] = request

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
        if self._closing or self._closed:
            raise AgentRuntimeClosedError("orchestrator kernel is closing or closed")
        lock = self._session_locks.setdefault(plan.session_id, asyncio.Lock())
        async with lock:
            self._recover_session(plan)
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

                async def guarded(
                    task: TaskSpec, limiter: asyncio.Semaphore = semaphore
                ) -> None:
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
