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

    async def test_committed_request_batch_is_reconciled_after_wrapper_failure(self) -> None:
        self.kernel.register_agent(
            registration("publisher", lambda invocation: AgentResult("published"))
        )
        plan = WorkflowPlan(
            "request-post-commit-failure",
            "审批请求提交成功后的包装器异常必须协调",
            "user",
            (approval_task("publish"),),
            plan_id="plan-request-post-commit-failure",
        )
        original_append_many = self.kernel.event_store.append_many

        def commit_then_raise(*args, **kwargs):
            original_append_many(*args, **kwargs)
            raise RuntimeError("injected post-commit wrapper failure")

        with patch.object(
            self.kernel.event_store,
            "append_many",
            side_effect=commit_then_raise,
        ):
            paused = await self.kernel.run(plan)

        self.assertEqual(paused.statuses["publish"], TaskStatus.WAITING_APPROVAL)
        self.assertEqual(len(paused.needs_you), 1)
        request = paused.needs_you[0]
        self.assertEqual(self.kernel.approvals.get(request.request_id), request)
        durable_events = self.kernel.event_store.read_stream(f"session:{plan.session_id}")
        request_events = tuple(
            stored for stored in durable_events if stored.event.event_type == "approval.requested"
        )
        self.assertEqual(len(request_events), 1)
        self.assertEqual(request_events[0].event.payload, request.to_dict())

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

    async def test_partial_post_commit_batch_is_never_reconciled(self) -> None:
        self.kernel.register_agent(
            registration("publisher", lambda invocation: AgentResult("published"))
        )
        plan = WorkflowPlan(
            "decision-partial-post-commit",
            "部分提交不得被误认成完整审批",
            "user",
            (approval_task("publish"),),
            plan_id="plan-decision-partial-post-commit",
        )
        paused = await self.kernel.run(plan)
        request = paused.needs_you[0]
        original_append_many = self.kernel.event_store.append_many

        def commit_prefix_then_raise(stream_id, events, expected_version=None):
            batch = tuple(events)
            original_append_many(
                stream_id,
                batch[:1],
                expected_version=expected_version,
            )
            raise RuntimeError("injected partial post-commit failure")

        with patch.object(
            self.kernel.event_store,
            "append_many",
            side_effect=commit_prefix_then_raise,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "injected partial post-commit failure",
            ):
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
        decision_events = tuple(
            stored for stored in durable_events if stored.event.event_type == "approval.decided"
        )
        self.assertEqual(len(decision_events), 1)
        self.assertFalse(
            any(
                stored.event.event_type == "task.status.changed"
                and stored.event.payload["previous"] == TaskStatus.WAITING_APPROVAL.value
                for stored in durable_events
            )
        )

    async def test_committed_decision_batch_is_reconciled_after_wrapper_failure(self) -> None:
        publish_calls = 0

        async def publisher(invocation):
            nonlocal publish_calls
            publish_calls += 1
            return AgentResult("published")

        self.kernel.register_agent(registration("publisher", publisher))
        plan = WorkflowPlan(
            "decision-post-commit-failure",
            "提交成功后的包装器异常必须协调",
            "user",
            (approval_task("publish"),),
            plan_id="plan-decision-post-commit-failure",
        )
        paused = await self.kernel.run(plan)
        request = paused.needs_you[0]
        original_append_many = self.kernel.event_store.append_many

        def commit_then_raise(*args, **kwargs):
            original_append_many(*args, **kwargs)
            raise RuntimeError("injected post-commit wrapper failure")

        with patch.object(
            self.kernel.event_store,
            "append_many",
            side_effect=commit_then_raise,
        ):
            decided = await self.kernel.decide(
                request.request_id,
                ApprovalDecision.APPROVE,
                "owner",
            )

        self.assertEqual(decided.decision, ApprovalDecision.APPROVE)
        self.assertEqual(
            self.kernel._graphs[plan.session_id].statuses["publish"],
            TaskStatus.READY,
        )
        self.assertIn((plan.session_id, "publish"), self.kernel._approved_tasks)
        durable_events = self.kernel.event_store.read_stream(f"session:{plan.session_id}")
        self.assertEqual(
            sum(stored.event.event_type == "approval.decided" for stored in durable_events),
            1,
        )

        result = await self.kernel.run(plan)
        self.assertEqual(result.statuses["publish"], TaskStatus.COMPLETED)
        self.assertEqual(publish_calls, 1)

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
        stream_id = f"session:{plan.session_id}"
        decision_start_version = self.kernel.event_store.stream_version(stream_id)
        a_decision_emitted = asyncio.Event()
        release_a_decision = asyncio.Event()
        delivered_sequences = []

        async def interleave(context):
            stored = context["storedEvent"]
            event = stored.event
            if stored.sequence > decision_start_version:
                delivered_sequences.append(stored.sequence)
            if event.event_type == "approval.decided" and event.payload["taskId"] == "a":
                a_decision_emitted.set()
                await asyncio.wait_for(release_a_decision.wait(), timeout=1)

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
        decide_b = asyncio.create_task(
            self.kernel.decide(
                requests["b"].request_id,
                ApprovalDecision.APPROVE,
                "owner-b",
            )
        )
        await asyncio.sleep(0)
        self.assertEqual(
            self.kernel.event_store.stream_version(stream_id),
            decision_start_version + 4,
        )
        release_a_decision.set()
        await asyncio.gather(decide_a, decide_b)

        self.assertEqual(
            delivered_sequences,
            list(range(decision_start_version + 1, decision_start_version + 5)),
        )

        stored_events = self.kernel.event_store.read_stream(stream_id)
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

    async def test_failed_post_commit_hook_does_not_fail_or_truncate_decision(self) -> None:
        plan = WorkflowPlan(
            "approval-hook-failure",
            "审批落盘后插件失败不得改变命令结果",
            "user",
            (approval_task("publish"),),
            plan_id="plan-approval-hook-failure",
        )
        paused = await self.kernel.run(plan)
        request = paused.needs_you[0]
        delivered_event_types = []

        async def fail_decision_hook(context):
            event_type = context["storedEvent"].event.event_type
            delivered_event_types.append(event_type)
            if event_type == "approval.decided":
                raise RuntimeError("injected post-commit hook failure")

        self.kernel.plugins.install(
            KernelPlugin(
                "fail-decision-hook",
                {HookPoint.EVENT_APPENDED: fail_decision_hook},
            )
        )
        with self.assertLogs("quantum_entanglement.runtime", level="ERROR") as captured:
            decided = await self.kernel.decide(
                request.request_id,
                ApprovalDecision.APPROVE,
                "owner",
            )

        self.assertEqual(decided.decision, ApprovalDecision.APPROVE)
        self.assertEqual(
            delivered_event_types,
            ["approval.decided", "task.status.changed"],
        )
        self.assertTrue(
            any("sequence" in message and "hook failure" in message for message in captured.output)
        )
        self.assertEqual(
            self.kernel._graphs[plan.session_id].statuses["publish"],
            TaskStatus.READY,
        )
        self.assertIn((plan.session_id, "publish"), self.kernel._approved_tasks)


if __name__ == "__main__":
    unittest.main()
