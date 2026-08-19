import asyncio
import unittest

from quantum_entanglement.policy import PolicyEngine
from quantum_entanglement.protocol import (
    ActionIntent,
    ActorKind,
    ActorRef,
    ApprovalDecision,
    ArtifactOutput,
    HandoffContract,
    RiskLevel,
    TaskStatus,
)
from quantum_entanglement.runtime import (
    AgentRegistration,
    AgentResult,
    OrchestratorKernel,
)
from quantum_entanglement.scheduler import TaskSpec, WorkflowPlan


def handoff(goal="完成任务"):
    return HandoffContract(
        goal=goal,
        acceptance_criteria=("结果可验证",),
        deliverables=("result.md",),
    )


def registration(agent_id, handler):
    return AgentRegistration(ActorRef(agent_id, agent_id, ActorKind.AGENT), handler)


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.kernel = OrchestratorKernel(max_concurrency=4)

    async def asyncTearDown(self):
        self.kernel.event_store.close()

    async def test_independent_tasks_run_in_parallel_and_initial_ready_is_recorded(self):
        active = 0
        peak = 0
        both_started = asyncio.Event()

        async def handler(invocation):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=1)
            active -= 1
            return AgentResult("done")

        self.kernel.register_agent(registration("worker", handler))
        plan = WorkflowPlan(
            "parallel",
            "并行处理",
            "user",
            (
                TaskSpec("a", "worker", handoff(), task_id="a"),
                TaskSpec("b", "worker", handoff(), task_id="b"),
            ),
        )

        result = await self.kernel.run(plan)

        self.assertTrue(result.completed)
        self.assertEqual(peak, 2)
        events = self.kernel.event_store.read_stream("session:parallel")
        initial_ready = [
            event.event.payload["taskId"]
            for event in events
            if event.event.event_type == "task.status.changed"
            and event.event.payload["previous"] == "pending"
            and event.event.payload["current"] == "ready"
        ]
        self.assertEqual(initial_ready, ["a", "b"])

    async def test_dependency_receives_versioned_artifact_and_context_precedes_invocation(self):
        observed = {}

        async def researcher(invocation):
            return AgentResult("research", (ArtifactOutput("evidence.md", "source facts"),))

        async def writer(invocation):
            observed["context"] = invocation.context.render()
            return AgentResult("written", (ArtifactOutput("report.md", "final"),))

        self.kernel.register_agent(registration("researcher", researcher))
        self.kernel.register_agent(registration("writer", writer))
        plan = WorkflowPlan(
            "handoff",
            "交接",
            "user",
            (
                TaskSpec("调研", "researcher", handoff(), task_id="research"),
                TaskSpec("写作", "writer", handoff(), task_id="write", depends_on=("research",)),
            ),
        )

        result = await self.kernel.run(plan)

        self.assertTrue(result.completed)
        self.assertIn("source facts", observed["context"])
        self.assertEqual({ref.name for ref in result.artifacts}, {"evidence.md", "report.md"})
        events = self.kernel.event_store.read_stream("session:handoff")
        for task_id in ("research", "write"):
            context_sequence = next(
                event.sequence
                for event in events
                if event.event.event_type == "context.compiled"
                and event.event.payload["taskId"] == task_id
            )
            invocation_sequence = next(
                event.sequence
                for event in events
                if event.event.event_type == "task.invocation.started"
                and event.event.payload["taskId"] == task_id
            )
            self.assertLess(context_sequence, invocation_sequence)

    async def test_approval_pauses_then_resumes_exactly_once(self):
        calls = 0

        async def publisher(invocation):
            nonlocal calls
            calls += 1
            return AgentResult("published")

        self.kernel.register_agent(registration("publisher", publisher))
        task = TaskSpec(
            "发布",
            "publisher",
            handoff(),
            task_id="publish",
            action=ActionIntent(
                "publish", "external", risk=RiskLevel.HIGH, external_side_effect=True
            ),
        )
        plan = WorkflowPlan("approval", "受控发布", "user", (task,))

        paused = await self.kernel.run(plan)
        self.assertEqual(paused.statuses["publish"], TaskStatus.WAITING_APPROVAL)
        self.assertEqual(calls, 0)
        request = paused.needs_you[0]

        await self.kernel.decide(request.request_id, ApprovalDecision.APPROVE, "owner")
        resumed = await self.kernel.run(plan)
        rerun = await self.kernel.run(plan)

        self.assertTrue(resumed.completed)
        self.assertTrue(rerun.completed)
        self.assertEqual(calls, 1)
        self.assertEqual(resumed.needs_you, ())
        stored_events = self.kernel.event_store.read_stream("session:approval")
        approval_events = [
            stored.event
            for stored in stored_events
            if stored.event.event_type in ("approval.requested", "approval.decided")
        ]
        self.assertEqual(len(approval_events), 2)
        self.assertTrue(all(event.correlation_id == plan.plan_id for event in approval_events))
        self.assertEqual(approval_events[0].causation_id, task.task_id)
        self.assertEqual(approval_events[1].causation_id, request.request_id)
        status_events = [
            stored.event
            for stored in stored_events
            if stored.event.event_type == "task.status.changed"
        ]
        self.assertEqual(
            [event.payload["revision"] for event in status_events],
            list(range(1, len(status_events) + 1)),
        )

    async def test_failed_task_blocks_downstream_without_model_guessing(self):
        downstream_calls = 0

        async def failing(invocation):
            raise RuntimeError("source unavailable")

        async def downstream(invocation):
            nonlocal downstream_calls
            downstream_calls += 1
            return AgentResult("should not run")

        self.kernel.register_agent(registration("failing", failing))
        self.kernel.register_agent(registration("downstream", downstream))
        plan = WorkflowPlan(
            "failure",
            "失败传播",
            "user",
            (
                TaskSpec("上游", "failing", handoff(), task_id="upstream"),
                TaskSpec(
                    "下游",
                    "downstream",
                    handoff(),
                    task_id="downstream",
                    depends_on=("upstream",),
                ),
            ),
        )

        result = await self.kernel.run(plan)

        self.assertEqual(result.statuses["upstream"], TaskStatus.FAILED)
        self.assertEqual(result.statuses["downstream"], TaskStatus.BLOCKED)
        self.assertEqual(downstream_calls, 0)
        self.assertIn("source unavailable", result.errors["upstream"])
        self.assertIn("upstream", result.errors["downstream"])

    async def test_workspace_policy_denial_is_a_recorded_failure(self):
        self.kernel.policy = PolicyEngine(forbidden_actions=("analyze",))

        async def handler(invocation):
            return AgentResult("not reached")

        self.kernel.register_agent(registration("worker", handler))
        plan = WorkflowPlan(
            "denied",
            "禁止操作",
            "user",
            (TaskSpec("x", "worker", handoff(), task_id="x"),),
        )

        result = await self.kernel.run(plan)

        self.assertEqual(result.statuses["x"], TaskStatus.FAILED)
        self.assertIn("forbidden", result.errors["x"])


if __name__ == "__main__":
    unittest.main()
