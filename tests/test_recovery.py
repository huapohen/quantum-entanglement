import tempfile
import unittest
from pathlib import Path

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
from quantum_entanglement.runtime import AgentRegistration, AgentResult, OrchestratorKernel
from quantum_entanglement.scheduler import TaskSpec, WorkflowPlan
from quantum_entanglement.store import SQLiteEventStore


def handoff(deliverable="result.md"):
    return HandoffContract(
        goal="完成任务",
        acceptance_criteria=("结果可验证",),
        deliverables=(deliverable,),
    )


def registration(agent_id, handler):
    return AgentRegistration(ActorRef(agent_id, agent_id, ActorKind.AGENT), handler)


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tempdir.name) / "events.sqlite3")

    async def asyncTearDown(self):
        self.tempdir.cleanup()

    async def test_completed_plan_is_recovered_without_invoking_agents_again(self):
        calls = 0

        async def handler(invocation):
            nonlocal calls
            calls += 1
            return AgentResult("done", (ArtifactOutput("result.md", "stable"),))

        plan = WorkflowPlan(
            "recover-complete", "完成一次", "user",
            (TaskSpec("task", "worker", handoff(), task_id="task"),),
            plan_id="plan-stable",
        )
        first = OrchestratorKernel(event_store=SQLiteEventStore(self.path))
        first.register_agent(registration("worker", handler))
        result = await first.run(plan)
        event_count = len(first.event_store.read_stream("session:recover-complete"))
        first.event_store.close()

        second = OrchestratorKernel(event_store=SQLiteEventStore(self.path))
        second.register_agent(registration("worker", handler))
        recovered = await second.run(plan)

        self.assertTrue(result.completed)
        self.assertTrue(recovered.completed)
        self.assertEqual(calls, 1)
        self.assertEqual(
            len(second.event_store.read_stream("session:recover-complete")), event_count
        )
        self.assertEqual(recovered.artifacts[0].name, "result.md")
        altered = WorkflowPlan(
            "recover-complete", "悄悄改变目标", "user", plan.tasks,
            plan_id="plan-stable",
        )
        with self.assertRaises(ValueError):
            await second.run(altered)
        second.event_store.close()

    async def test_approval_and_dependency_artifact_survive_multiple_restarts(self):
        research_calls = 0

        async def researcher(invocation):
            nonlocal research_calls
            research_calls += 1
            return AgentResult("facts", (ArtifactOutput("evidence.md", "durable evidence"),))

        async def publisher_never(invocation):
            raise AssertionError("publisher must not run before approval")

        publish_task = TaskSpec(
            "publish", "publisher", handoff("publication.md"), task_id="publish",
            depends_on=("research",),
            action=ActionIntent(
                "publish", "external", risk=RiskLevel.HIGH, external_side_effect=True
            ),
        )
        plan = WorkflowPlan(
            "recover-approval", "研究后发布", "user",
            (
                TaskSpec("research", "researcher", handoff("evidence.md"), task_id="research"),
                publish_task,
            ),
            plan_id="plan-approval",
        )

        first = OrchestratorKernel(event_store=SQLiteEventStore(self.path))
        first.register_agent(registration("researcher", researcher))
        first.register_agent(registration("publisher", publisher_never))
        paused = await first.run(plan)
        request_id = paused.needs_you[0].request_id
        self.assertEqual(paused.statuses["publish"], TaskStatus.WAITING_APPROVAL)
        first.event_store.close()

        # Recover only to record the human decision, then simulate another crash.
        second = OrchestratorKernel(event_store=SQLiteEventStore(self.path))
        recovered_pause = await second.run(plan)
        self.assertEqual(recovered_pause.needs_you[0].request_id, request_id)
        await second.decide(request_id, ApprovalDecision.APPROVE, "owner")
        second.event_store.close()

        observed_context = ""
        publish_calls = 0

        async def publisher(invocation):
            nonlocal observed_context, publish_calls
            publish_calls += 1
            observed_context = invocation.context.render()
            return AgentResult("published", (ArtifactOutput("publication.md", "published"),))

        third = OrchestratorKernel(event_store=SQLiteEventStore(self.path))
        third.register_agent(registration("researcher", researcher))
        third.register_agent(registration("publisher", publisher))
        completed = await third.run(plan)

        self.assertTrue(completed.completed)
        self.assertEqual(research_calls, 1)
        self.assertEqual(publish_calls, 1)
        self.assertIn("durable evidence", observed_context)
        self.assertEqual(completed.needs_you, ())
        self.assertEqual(
            {item.name for item in completed.artifacts}, {"evidence.md", "publication.md"}
        )
        third.event_store.close()


if __name__ == "__main__":
    unittest.main()
