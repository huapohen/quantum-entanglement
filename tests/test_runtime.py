import asyncio
import unittest
from unittest.mock import patch

from quantum_entanglement.plugins import HookPoint, KernelPlugin
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
    SessionRecoveryError,
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

    async def test_failed_plan_initialization_batch_publishes_no_partial_memory(self):
        calls = 0

        async def worker(invocation):
            nonlocal calls
            calls += 1
            return AgentResult("done")

        self.kernel.register_agent(registration("worker", worker))
        plan = WorkflowPlan(
            "plan-initialization-failure",
            "初始化必须原子",
            "user",
            (TaskSpec("task", "worker", handoff(), task_id="task"),),
            plan_id="plan-initialization-failure",
        )

        with patch.object(
            self.kernel.event_store,
            "append_many",
            side_effect=RuntimeError("injected initialization batch failure"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "injected initialization batch failure",
            ):
                await self.kernel.run(plan)

        self.assertNotIn(plan.session_id, self.kernel._plans)
        self.assertNotIn(plan.session_id, self.kernel._graphs)
        self.assertEqual(
            self.kernel.event_store.read_stream(f"session:{plan.session_id}"),
            (),
        )
        self.assertEqual(calls, 0)

        result = await self.kernel.run(plan)
        self.assertTrue(result.completed)
        self.assertEqual(calls, 1)

    async def test_committed_plan_initialization_is_reconciled_after_wrapper_failure(self):
        calls = 0

        async def worker(invocation):
            nonlocal calls
            calls += 1
            return AgentResult("done")

        self.kernel.register_agent(registration("worker", worker))
        plan = WorkflowPlan(
            "plan-initialization-post-commit",
            "初始化提交成功后的包装器异常必须协调",
            "user",
            (TaskSpec("task", "worker", handoff(), task_id="task"),),
            plan_id="plan-initialization-post-commit",
        )
        original_append_many = self.kernel.event_store.append_many

        def commit_then_raise(*args, **kwargs):
            original_append_many(*args, **kwargs)
            raise RuntimeError("injected initialization post-commit failure")

        with patch.object(
            self.kernel.event_store,
            "append_many",
            side_effect=commit_then_raise,
        ):
            first = await self.kernel.run(plan)

        second = await self.kernel.run(plan)
        self.assertTrue(first.completed)
        self.assertTrue(second.completed)
        self.assertEqual(calls, 1)
        self.assertIn(plan.session_id, self.kernel._plans)
        self.assertIn(plan.session_id, self.kernel._graphs)
        events = self.kernel.event_store.read_stream(f"session:{plan.session_id}")
        self.assertEqual(
            sum(event.event.event_type == "workflow.plan.created" for event in events),
            1,
        )
        self.assertEqual(
            sum(event.event.event_type == "task.created" for event in events),
            1,
        )

    async def test_failed_plan_created_hook_does_not_reverse_committed_initialization(self):
        calls = 0

        async def worker(invocation):
            nonlocal calls
            calls += 1
            return AgentResult("done")

        async def fail_plan_created(context):
            raise RuntimeError("injected plan-created hook failure")

        self.kernel.register_agent(registration("worker", worker))
        self.kernel.plugins.install(
            KernelPlugin(
                "fail-plan-created",
                {HookPoint.PLAN_CREATED: fail_plan_created},
            )
        )
        plan = WorkflowPlan(
            "plan-hook-failure",
            "初始化提交后的观察器失败不得反转命令",
            "user",
            (TaskSpec("task", "worker", handoff(), task_id="task"),),
            plan_id="plan-hook-failure",
        )

        with self.assertLogs("quantum_entanglement.runtime", level="ERROR") as captured:
            result = await self.kernel.run(plan)

        self.assertTrue(result.completed)
        self.assertEqual(calls, 1)
        self.assertTrue(any("plan.created hook failed" in message for message in captured.output))
        events = self.kernel.event_store.read_stream(f"session:{plan.session_id}")
        self.assertEqual(
            sum(event.event.event_type == "workflow.plan.created" for event in events),
            1,
        )

    async def test_running_transition_precommit_failure_keeps_memory_ready(self):
        calls = 0

        async def worker(invocation):
            nonlocal calls
            calls += 1
            return AgentResult("done")

        self.kernel.register_agent(registration("worker", worker))
        plan = WorkflowPlan(
            "running-precommit",
            "运行状态必须先持久化",
            "user",
            (TaskSpec("task", "worker", handoff(), task_id="task"),),
        )
        original_append_many = self.kernel.event_store.append_many

        def fail_running(stream_id, events, expected_version=None):
            batch = tuple(events)
            if any(
                event.event_type == "task.status.changed"
                and event.payload["current"] == TaskStatus.RUNNING.value
                for event in batch
            ):
                raise RuntimeError("injected running precommit failure")
            return original_append_many(
                stream_id,
                batch,
                expected_version=expected_version,
            )

        with patch.object(
            self.kernel.event_store,
            "append_many",
            side_effect=fail_running,
        ):
            with self.assertRaisesRegex(RuntimeError, "running precommit"):
                await self.kernel.run(plan)

        self.assertEqual(calls, 0)
        self.assertEqual(self.kernel._graphs[plan.session_id].statuses["task"], TaskStatus.READY)
        self.assertFalse(
            any(
                stored.event.event_type == "task.status.changed"
                and stored.event.payload["current"] == TaskStatus.RUNNING.value
                for stored in self.kernel.event_store.read_stream(f"session:{plan.session_id}")
            )
        )
        result = await self.kernel.run(plan)
        self.assertTrue(result.completed)
        self.assertEqual(calls, 1)

    async def test_running_transition_postcommit_failure_is_reconciled_once(self):
        calls = 0

        async def worker(invocation):
            nonlocal calls
            calls += 1
            return AgentResult("done")

        self.kernel.register_agent(registration("worker", worker))
        plan = WorkflowPlan(
            "running-postcommit",
            "提交后包装器异常必须协调",
            "user",
            (TaskSpec("task", "worker", handoff(), task_id="task"),),
        )
        original_append_many = self.kernel.event_store.append_many
        injected = False

        def commit_running_then_raise(stream_id, events, expected_version=None):
            nonlocal injected
            batch = tuple(events)
            stored = original_append_many(
                stream_id,
                batch,
                expected_version=expected_version,
            )
            if not injected and any(
                event.event_type == "task.status.changed"
                and event.payload["current"] == TaskStatus.RUNNING.value
                for event in batch
            ):
                injected = True
                raise RuntimeError("injected running postcommit failure")
            return stored

        with patch.object(
            self.kernel.event_store,
            "append_many",
            side_effect=commit_running_then_raise,
        ):
            result = await self.kernel.run(plan)

        self.assertTrue(injected)
        self.assertTrue(result.completed)
        self.assertEqual(calls, 1)
        events = self.kernel.event_store.read_stream(f"session:{plan.session_id}")
        self.assertEqual(
            sum(
                stored.event.event_type == "task.status.changed"
                and stored.event.payload["current"] == TaskStatus.RUNNING.value
                for stored in events
            ),
            1,
        )

    async def test_invocation_started_postcommit_failure_is_reconciled_once(self):
        calls = 0

        async def worker(invocation):
            nonlocal calls
            calls += 1
            return AgentResult("done")

        self.kernel.register_agent(registration("worker", worker))
        plan = WorkflowPlan(
            "invocation-started-postcommit",
            "调用开始事件提交后异常必须协调",
            "user",
            (TaskSpec("task", "worker", handoff(), task_id="task"),),
        )
        original_append_many = self.kernel.event_store.append_many
        injected = False

        def commit_invocation_then_raise(stream_id, events, expected_version=None):
            nonlocal injected
            batch = tuple(events)
            stored = original_append_many(
                stream_id,
                batch,
                expected_version=expected_version,
            )
            if not injected and any(
                event.event_type == "task.invocation.started" for event in batch
            ):
                injected = True
                raise RuntimeError("injected invocation postcommit failure")
            return stored

        with patch.object(
            self.kernel.event_store,
            "append_many",
            side_effect=commit_invocation_then_raise,
        ):
            result = await self.kernel.run(plan)

        self.assertTrue(injected)
        self.assertTrue(result.completed)
        self.assertEqual(calls, 1)
        events = self.kernel.event_store.read_stream(f"session:{plan.session_id}")
        self.assertEqual(
            sum(stored.event.event_type == "task.invocation.started" for stored in events),
            1,
        )

    async def test_result_postcommit_failure_is_reconciled_without_false_failure(self):
        calls = 0

        async def worker(invocation):
            nonlocal calls
            calls += 1
            return AgentResult(
                "done",
                (ArtifactOutput("result.md", "stable"),),
            )

        self.kernel.register_agent(registration("worker", worker))
        plan = WorkflowPlan(
            "result-postcommit",
            "结果事件提交后异常必须协调",
            "user",
            (TaskSpec("task", "worker", handoff(), task_id="task"),),
        )
        original_append_many = self.kernel.event_store.append_many
        injected = False

        def commit_result_then_raise(stream_id, events, expected_version=None):
            nonlocal injected
            batch = tuple(events)
            stored = original_append_many(
                stream_id,
                batch,
                expected_version=expected_version,
            )
            if not injected and any(event.event_type == "task.result.received" for event in batch):
                injected = True
                raise RuntimeError("injected result postcommit failure")
            return stored

        with patch.object(
            self.kernel.event_store,
            "append_many",
            side_effect=commit_result_then_raise,
        ):
            result = await self.kernel.run(plan)

        self.assertTrue(injected)
        self.assertTrue(result.completed)
        self.assertEqual(calls, 1)
        self.assertEqual({ref.name for ref in result.artifacts}, {"result.md"})
        events = self.kernel.event_store.read_stream(f"session:{plan.session_id}")
        self.assertEqual(
            sum(stored.event.event_type == "task.result.received" for stored in events),
            1,
        )
        self.assertFalse(
            any(
                stored.event.event_type == "task.status.changed"
                and stored.event.payload["current"] == TaskStatus.FAILED.value
                for stored in events
            )
        )

    async def test_result_precommit_failure_quarantines_without_publishing_task_refs(self):
        calls = 0

        async def worker(invocation):
            nonlocal calls
            calls += 1
            return AgentResult(
                "done",
                (ArtifactOutput("result.md", "durable partial result"),),
            )

        self.kernel.register_agent(registration("worker", worker))
        plan = WorkflowPlan(
            "result-precommit",
            "结果事件提交前失败必须隔离",
            "user",
            (TaskSpec("task", "worker", handoff(), task_id="task"),),
        )
        original_append_many = self.kernel.event_store.append_many

        def fail_result(stream_id, events, expected_version=None):
            batch = tuple(events)
            if any(event.event_type == "task.result.received" for event in batch):
                raise RuntimeError("injected result precommit failure")
            return original_append_many(
                stream_id,
                batch,
                expected_version=expected_version,
            )

        with patch.object(
            self.kernel.event_store,
            "append_many",
            side_effect=fail_result,
        ):
            with self.assertRaisesRegex(RuntimeError, "result precommit"):
                await self.kernel.run(plan)

        self.assertEqual(calls, 1)
        self.assertEqual(
            self.kernel._graphs[plan.session_id].statuses["task"],
            TaskStatus.RUNNING,
        )
        self.assertNotIn((plan.session_id, "task"), self.kernel._task_artifacts)
        self.assertIsNotNone(self.kernel.artifacts.current(plan.session_id, "result.md"))
        events = self.kernel.event_store.read_stream(f"session:{plan.session_id}")
        event_count = len(events)
        self.assertEqual(
            sum(stored.event.event_type == "artifact.versioned" for stored in events),
            1,
        )
        self.assertFalse(
            any(
                stored.event.event_type == "task.result.received"
                or (
                    stored.event.event_type == "task.status.changed"
                    and stored.event.payload["current"]
                    in {TaskStatus.COMPLETED.value, TaskStatus.FAILED.value}
                )
                for stored in events
            )
        )

        with self.assertRaisesRegex(
            SessionRecoveryError,
            "durably RUNNING task without supported invocation recovery evidence",
        ):
            await self.kernel.run(plan)

        self.assertEqual(calls, 1)
        self.assertEqual(
            len(self.kernel.event_store.read_stream(f"session:{plan.session_id}")),
            event_count,
        )

    async def test_artifact_precommit_failure_after_agent_return_quarantines(self):
        calls = 0

        async def worker(invocation):
            nonlocal calls
            calls += 1
            return AgentResult(
                "done",
                (ArtifactOutput("result.md", "must not publish"),),
            )

        self.kernel.register_agent(registration("worker", worker))
        plan = WorkflowPlan(
            "artifact-precommit",
            "产物提交前失败必须隔离",
            "user",
            (TaskSpec("task", "worker", handoff(), task_id="task"),),
        )
        original_append = self.kernel.event_store.append

        def fail_artifact(event, expected_version=None):
            if event.event_type == "artifact.versioned":
                raise RuntimeError("injected artifact precommit failure")
            return original_append(event, expected_version)

        with patch.object(
            self.kernel.event_store,
            "append",
            side_effect=fail_artifact,
        ):
            with self.assertRaisesRegex(RuntimeError, "artifact precommit"):
                await self.kernel.run(plan)

        self.assertEqual(calls, 1)
        self.assertEqual(
            self.kernel._graphs[plan.session_id].statuses["task"],
            TaskStatus.RUNNING,
        )
        self.assertNotIn((plan.session_id, "task"), self.kernel._task_artifacts)
        self.assertIsNone(self.kernel.artifacts.current(plan.session_id, "result.md"))
        events = self.kernel.event_store.read_stream(f"session:{plan.session_id}")
        event_count = len(events)
        self.assertFalse(
            any(
                stored.event.event_type in {"artifact.versioned", "task.result.received"}
                or (
                    stored.event.event_type == "task.status.changed"
                    and stored.event.payload["current"]
                    in {TaskStatus.COMPLETED.value, TaskStatus.FAILED.value}
                )
                for stored in events
            )
        )

        with self.assertRaisesRegex(
            SessionRecoveryError,
            "durably RUNNING task without supported invocation recovery evidence",
        ):
            await self.kernel.run(plan)

        self.assertEqual(calls, 1)
        self.assertEqual(
            len(self.kernel.event_store.read_stream(f"session:{plan.session_id}")),
            event_count,
        )

    async def test_completed_transition_precommit_failure_quarantines_effect_unknown(self):
        calls = 0

        async def worker(invocation):
            nonlocal calls
            calls += 1
            return AgentResult("done")

        self.kernel.register_agent(registration("worker", worker))
        plan = WorkflowPlan(
            "completed-precommit",
            "完成终态提交失败后必须隔离",
            "user",
            (TaskSpec("task", "worker", handoff(), task_id="task"),),
        )
        original_append_many = self.kernel.event_store.append_many

        def fail_completed(stream_id, events, expected_version=None):
            batch = tuple(events)
            if any(
                event.event_type == "task.status.changed"
                and event.payload["current"] == TaskStatus.COMPLETED.value
                for event in batch
            ):
                raise RuntimeError("injected completed precommit failure")
            return original_append_many(
                stream_id,
                batch,
                expected_version=expected_version,
            )

        with patch.object(
            self.kernel.event_store,
            "append_many",
            side_effect=fail_completed,
        ):
            with self.assertRaisesRegex(RuntimeError, "completed precommit"):
                await self.kernel.run(plan)

        self.assertEqual(calls, 1)
        self.assertEqual(
            self.kernel._graphs[plan.session_id].statuses["task"],
            TaskStatus.RUNNING,
        )
        events = self.kernel.event_store.read_stream(f"session:{plan.session_id}")
        event_count = len(events)
        self.assertEqual(
            sum(stored.event.event_type == "task.result.received" for stored in events),
            1,
        )
        self.assertFalse(
            any(
                stored.event.event_type == "task.status.changed"
                and stored.event.payload["current"]
                in {TaskStatus.COMPLETED.value, TaskStatus.FAILED.value}
                for stored in events
            )
        )

        with self.assertRaisesRegex(
            SessionRecoveryError,
            "durably RUNNING task without supported invocation recovery evidence",
        ):
            await self.kernel.run(plan)

        self.assertEqual(calls, 1)
        self.assertEqual(
            len(self.kernel.event_store.read_stream(f"session:{plan.session_id}")),
            event_count,
        )

    async def test_completed_transition_postcommit_failure_is_reconciled_once(self):
        calls = 0

        async def worker(invocation):
            nonlocal calls
            calls += 1
            return AgentResult("done")

        self.kernel.register_agent(registration("worker", worker))
        plan = WorkflowPlan(
            "completed-postcommit",
            "完成终态提交后包装器异常必须协调",
            "user",
            (TaskSpec("task", "worker", handoff(), task_id="task"),),
        )
        original_append_many = self.kernel.event_store.append_many
        injected = False

        def commit_completed_then_raise(stream_id, events, expected_version=None):
            nonlocal injected
            batch = tuple(events)
            stored = original_append_many(
                stream_id,
                batch,
                expected_version=expected_version,
            )
            if not injected and any(
                event.event_type == "task.status.changed"
                and event.payload["current"] == TaskStatus.COMPLETED.value
                for event in batch
            ):
                injected = True
                raise RuntimeError("injected completed postcommit failure")
            return stored

        with patch.object(
            self.kernel.event_store,
            "append_many",
            side_effect=commit_completed_then_raise,
        ):
            result = await self.kernel.run(plan)

        self.assertTrue(injected)
        self.assertTrue(result.completed)
        self.assertEqual(calls, 1)
        events = self.kernel.event_store.read_stream(f"session:{plan.session_id}")
        self.assertEqual(
            sum(
                stored.event.event_type == "task.status.changed"
                and stored.event.payload["current"] == TaskStatus.COMPLETED.value
                for stored in events
            ),
            1,
        )

    async def test_failed_transition_precommit_failure_quarantines_effect_unknown(self):
        calls = 0

        async def worker(invocation):
            nonlocal calls
            calls += 1
            raise RuntimeError("agent failed")

        self.kernel.register_agent(registration("worker", worker))
        plan = WorkflowPlan(
            "failed-precommit",
            "失败终态提交失败后必须隔离",
            "user",
            (TaskSpec("task", "worker", handoff(), task_id="task"),),
        )
        original_append_many = self.kernel.event_store.append_many

        def fail_terminal_failure(stream_id, events, expected_version=None):
            batch = tuple(events)
            if any(
                event.event_type == "task.status.changed"
                and event.payload["current"] == TaskStatus.FAILED.value
                for event in batch
            ):
                raise RuntimeError("injected failed precommit failure")
            return original_append_many(
                stream_id,
                batch,
                expected_version=expected_version,
            )

        with patch.object(
            self.kernel.event_store,
            "append_many",
            side_effect=fail_terminal_failure,
        ):
            with self.assertRaisesRegex(RuntimeError, "failed precommit"):
                await self.kernel.run(plan)

        self.assertEqual(calls, 1)
        self.assertEqual(
            self.kernel._graphs[plan.session_id].statuses["task"],
            TaskStatus.RUNNING,
        )
        events = self.kernel.event_store.read_stream(f"session:{plan.session_id}")
        event_count = len(events)
        self.assertFalse(
            any(
                stored.event.event_type == "task.status.changed"
                and stored.event.payload["current"] == TaskStatus.FAILED.value
                for stored in events
            )
        )

        with self.assertRaisesRegex(
            SessionRecoveryError,
            "durably RUNNING task without supported invocation recovery evidence",
        ):
            await self.kernel.run(plan)

        self.assertEqual(calls, 1)
        self.assertEqual(
            len(self.kernel.event_store.read_stream(f"session:{plan.session_id}")),
            event_count,
        )

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

    async def test_failed_approval_request_batch_leaves_no_partial_authority(self):
        task = TaskSpec(
            "发布",
            "publisher",
            handoff(),
            task_id="publish-atomic-request",
            action=ActionIntent(
                "publish",
                "external",
                risk=RiskLevel.HIGH,
                external_side_effect=True,
            ),
        )
        plan = WorkflowPlan(
            "approval-request-atomicity",
            "审批请求必须完整落盘",
            "user",
            (task,),
        )
        original_append_many = self.kernel.event_store.append_many

        def fail_approval_request(stream_id, events, expected_version=None):
            batch = tuple(events)
            if any(event.event_type == "approval.requested" for event in batch):
                raise RuntimeError("injected approval batch failure")
            return original_append_many(
                stream_id,
                batch,
                expected_version=expected_version,
            )

        with patch.object(
            self.kernel.event_store,
            "append_many",
            side_effect=fail_approval_request,
        ):
            with self.assertRaisesRegex(RuntimeError, "injected approval batch failure"):
                await self.kernel.run(plan)

        self.assertEqual(
            self.kernel._graphs[plan.session_id].statuses[task.task_id],
            TaskStatus.READY,
        )
        self.assertEqual(self.kernel.approvals.pending(plan.session_id), ())
        failed_events = self.kernel.event_store.read_stream(f"session:{plan.session_id}")
        self.assertFalse(
            any(stored.event.event_type == "approval.requested" for stored in failed_events)
        )
        self.assertFalse(
            any(
                stored.event.event_type == "task.status.changed"
                and stored.event.payload["current"]
                in {TaskStatus.RUNNING.value, TaskStatus.WAITING_APPROVAL.value}
                for stored in failed_events
            )
        )

        paused = await self.kernel.run(plan)
        self.assertEqual(paused.statuses[task.task_id], TaskStatus.WAITING_APPROVAL)
        self.assertEqual(len(paused.needs_you), 1)

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

    async def test_dependency_refresh_precommit_failure_keeps_task_pending(self):
        calls = {"upstream": 0, "downstream": 0}

        async def upstream(invocation):
            calls["upstream"] += 1
            return AgentResult("upstream done")

        async def downstream(invocation):
            calls["downstream"] += 1
            return AgentResult("downstream done")

        self.kernel.register_agent(registration("upstream", upstream))
        self.kernel.register_agent(registration("downstream", downstream))
        plan = WorkflowPlan(
            "refresh-precommit",
            "依赖刷新必须先持久化",
            "user",
            (
                TaskSpec("upstream", "upstream", handoff(), task_id="upstream"),
                TaskSpec(
                    "downstream",
                    "downstream",
                    handoff(),
                    task_id="downstream",
                    depends_on=("upstream",),
                ),
            ),
        )
        original_append_many = self.kernel.event_store.append_many

        def fail_downstream_ready(stream_id, events, expected_version=None):
            batch = tuple(events)
            if any(
                event.event_type == "task.status.changed"
                and event.payload["taskId"] == "downstream"
                and event.payload["previous"] == TaskStatus.PENDING.value
                and event.payload["current"] == TaskStatus.READY.value
                for event in batch
            ):
                raise RuntimeError("injected refresh precommit failure")
            return original_append_many(
                stream_id,
                batch,
                expected_version=expected_version,
            )

        with patch.object(
            self.kernel.event_store,
            "append_many",
            side_effect=fail_downstream_ready,
        ):
            with self.assertRaisesRegex(RuntimeError, "refresh precommit"):
                await self.kernel.run(plan)

        self.assertEqual(calls, {"upstream": 1, "downstream": 0})
        self.assertEqual(
            self.kernel._graphs[plan.session_id].statuses["downstream"],
            TaskStatus.PENDING,
        )
        result = await self.kernel.run(plan)
        self.assertTrue(result.completed)
        self.assertEqual(calls, {"upstream": 1, "downstream": 1})

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

    async def test_policy_denial_transition_batch_is_atomic_before_memory(self):
        self.kernel.policy = PolicyEngine(forbidden_actions=("analyze",))

        async def handler(invocation):
            raise AssertionError("denied Agent must not run")

        self.kernel.register_agent(registration("worker", handler))
        plan = WorkflowPlan(
            "denied-precommit",
            "拒绝状态批次必须原子",
            "user",
            (TaskSpec("x", "worker", handoff(), task_id="x"),),
        )
        original_append_many = self.kernel.event_store.append_many

        def fail_denial_batch(stream_id, events, expected_version=None):
            batch = tuple(events)
            currents = tuple(
                event.payload["current"]
                for event in batch
                if event.event_type == "task.status.changed"
            )
            if currents == (TaskStatus.RUNNING.value, TaskStatus.FAILED.value):
                raise RuntimeError("injected denial precommit failure")
            return original_append_many(
                stream_id,
                batch,
                expected_version=expected_version,
            )

        with patch.object(
            self.kernel.event_store,
            "append_many",
            side_effect=fail_denial_batch,
        ):
            with self.assertRaisesRegex(RuntimeError, "denial precommit"):
                await self.kernel.run(plan)

        self.assertEqual(self.kernel._graphs[plan.session_id].statuses["x"], TaskStatus.READY)
        events = self.kernel.event_store.read_stream(f"session:{plan.session_id}")
        self.assertFalse(
            any(
                stored.event.event_type == "task.status.changed"
                and stored.event.payload["current"]
                in {TaskStatus.RUNNING.value, TaskStatus.FAILED.value}
                for stored in events
            )
        )
        result = await self.kernel.run(plan)
        self.assertEqual(result.statuses["x"], TaskStatus.FAILED)


if __name__ == "__main__":
    unittest.main()
