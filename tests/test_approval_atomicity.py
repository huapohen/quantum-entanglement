import asyncio
import unittest
from unittest.mock import patch

from quantum_entanglement.plugins import HookPoint, KernelPlugin
from quantum_entanglement.protocol import (
    ActionIntent,
    ActorKind,
    ActorRef,
    ApprovalDecision,
    HandoffContract,
    RiskLevel,
    TaskStatus,
)
from quantum_entanglement.runtime import AgentRegistration, AgentResult, OrchestratorKernel
from quantum_entanglement.scheduler import TaskSpec, WorkflowPlan


def handoff() -> HandoffContract:
    return HandoffContract(
        goal="完成任务",
        acceptance_criteria=("结果可验证",),
        deliverables=("result.md",),
    )


def registration(agent_id, handler) -> AgentRegistration:
    return AgentRegistration(ActorRef(agent_id, agent_id, ActorKind.AGENT), handler)


def approval_task(task_id: str) -> TaskSpec:
    return TaskSpec(
        f"publish-{task_id}",
        "publisher",
        handoff(),
        task_id=task_id,
        action=ActionIntent(
            "publish",
            "external",
            risk=RiskLevel.HIGH,
            external_side_effect=True,
        ),
    )


class ApprovalAtomicityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.kernel = OrchestratorKernel()

    async def asyncTearDown(self) -> None:
        self.kernel.event_store.close()

    async def test_failed_decision_batch_never_grants_in_memory_authority(self) -> None:
        publish_calls = 0

        async def publisher(invocation):
            nonlocal publish_calls
            publish_calls += 1
            return AgentResult("published")

        self.kernel.register_agent(registration("publisher", publisher))
        plan = WorkflowPlan(
            "decision-append-failure",
            "审批决定必须完整落盘",
            "user",
            (approval_task("publish"),),
            plan_id="plan-decision-append-failure",
        )
        paused = await self.kernel.run(plan)
        request = paused.needs_you[0]

        with patch.object(
            self.kernel.event_store,
            "append_many",
            side_effect=RuntimeError("injected durable batch failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "injected durable batch failure"):
                await self.kernel.decide(
                    request.request_id,
                    ApprovalDecision.APPROVE,
                    "owner",
                )

        self.assertTrue(self.kernel.approvals.get(request.request_id).pending)
        self.assertEqual(
            self.kernel._graphs[plan.session_id].statuses["publish"],
            TaskStatus.WAITING_APPROVAL,
        )
        self.assertNotIn((plan.session_id, "publish"), self.kernel._approved_tasks)
        durable_events = self.kernel.event_store.read_stream(f"session:{plan.session_id}")
        self.assertFalse(
            any(stored.event.event_type == "approval.decided" for stored in durable_events)
        )

        rerun = await self.kernel.run(plan)
        self.assertEqual(rerun.statuses["publish"], TaskStatus.WAITING_APPROVAL)
        self.assertEqual(len(rerun.needs_you), 1)
        self.assertEqual(publish_calls, 0)

    async def test_exposed_snapshot_cannot_retarget_live_authority(self) -> None:
        agent_calls = []

        async def publisher(invocation):
            agent_calls.append(invocation.task.task_id)
            return AgentResult("published")

        self.kernel.register_agent(registration("publisher", publisher))
        plan = WorkflowPlan(
            "mutable-approval",
            "审批绑定不可篡改",
            "user",
            (approval_task("a"), approval_task("b")),
            plan_id="plan-mutable-approval",
        )
        paused = await self.kernel.run(plan)
        requests = {request.task_id: request for request in paused.needs_you}
        request_a_id = requests["a"].request_id

        object.__setattr__(requests["a"], "session_id", "forged-session")
        object.__setattr__(requests["a"], "task_id", "b")
        object.__setattr__(requests["a"], "reason", "forged-reason")
        object.__setattr__(requests["a"], "created_at", "9999-12-31T23:59:59.999999Z")
        object.__setattr__(requests["a"].intent, "target", "forged-target")

        decided = await self.kernel.decide(
            request_a_id,
            ApprovalDecision.APPROVE,
            "owner-a",
        )
        self.assertEqual(decided.session_id, plan.session_id)
        self.assertEqual(decided.task_id, "a")
        self.assertEqual(decided.reason, "high-impact action needs a human")
        self.assertEqual(decided.intent.target, "external")
        self.assertTrue(self.kernel.approvals.get(requests["b"].request_id).pending)

        result = await self.kernel.run(plan)
        self.assertEqual(agent_calls, ["a"])
        self.assertEqual(result.statuses["a"], TaskStatus.COMPLETED)
        self.assertEqual(result.statuses["b"], TaskStatus.WAITING_APPROVAL)
        self.assertEqual(result.needs_you[0].task_id, "b")

    async def test_concurrent_decision_batches_remain_contiguous(self) -> None:
        plan = WorkflowPlan(
            "approval-interleaved",
            "交错审批",
            "user",
            (approval_task("a"), approval_task("b")),
            plan_id="plan-approval-interleaved",
            correlation_id="correlation-approval-interleaved",
        )
        paused = await self.kernel.run(plan)
        requests = {request.task_id: request for request in paused.needs_you}
        a_decision_emitted = asyncio.Event()
        b_transition_emitted = asyncio.Event()

        async def interleave(context):
            event = context["storedEvent"].event
            if event.event_type == "approval.decided" and event.payload["taskId"] == "a":
                a_decision_emitted.set()
                await asyncio.wait_for(b_transition_emitted.wait(), timeout=1)
            elif (
                event.event_type == "task.status.changed"
                and event.payload["taskId"] == "b"
                and event.payload["previous"] == TaskStatus.WAITING_APPROVAL.value
            ):
                b_transition_emitted.set()

        self.kernel.plugins.install(
            KernelPlugin(
                "interleave-decisions",
                {HookPoint.EVENT_APPENDED: interleave},
            )
        )
        decide_a = asyncio.create_task(
            self.kernel.decide(
                requests["a"].request_id,
                ApprovalDecision.APPROVE,
                "owner-a",
            )
        )
        await asyncio.wait_for(a_decision_emitted.wait(), timeout=1)
        await self.kernel.decide(
            requests["b"].request_id,
            ApprovalDecision.APPROVE,
            "owner-b",
        )
        await decide_a

        stored_events = self.kernel.event_store.read_stream("session:approval-interleaved")
        for task_id, request in requests.items():
            decided = next(
                stored
                for stored in stored_events
                if stored.event.event_type == "approval.decided"
                and stored.event.payload["taskId"] == task_id
            )
            transitioned = next(
                stored
                for stored in stored_events
                if stored.event.event_type == "task.status.changed"
                and stored.event.payload["taskId"] == task_id
                and stored.event.payload["previous"] == TaskStatus.WAITING_APPROVAL.value
            )
            self.assertEqual(transitioned.sequence, decided.sequence + 1)
            self.assertEqual(transitioned.event.correlation_id, plan.correlation_id)
            self.assertEqual(transitioned.event.causation_id, request.request_id)


if __name__ == "__main__":
    unittest.main()
