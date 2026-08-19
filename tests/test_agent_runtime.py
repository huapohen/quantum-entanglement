from __future__ import annotations

import asyncio
import threading
import unittest
from dataclasses import dataclass
from typing import Any

from quantum_entanglement.adapters.deepseek_harness import (
    DeepSeekHarnessConfigurationError,
    DeepSeekHarnessDependencyError,
    DeepSeekHarnessRunError,
    DeepSeekHarnessRuntime,
)
from quantum_entanglement.agent_runtime import (
    AgentCancellationUnsupportedError,
    AgentInvocation,
    AgentInvocationConflictError,
    AgentResult,
    AgentRuntimeClosedError,
    AgentRuntimePort,
    CallableAgentRuntime,
)
from quantum_entanglement.context import ContextBundle, ContextItem
from quantum_entanglement.protocol import (
    ActorKind,
    ActorRef,
    CoordinationEnvelope,
    EnvelopeKind,
    HandoffContract,
)
from quantum_entanglement.runtime import AgentRegistration, AgentRegistry, OrchestratorKernel
from quantum_entanglement.scheduler import TaskSpec


@dataclass
class FakeRunResult:
    session_id: str = "dsh-result-session"
    final_response: str = "completed result"
    finish_reason: str | None = "completed"
    events: list[Any] | None = None
    notifications: list[Any] | None = None
    session_root: str | None = "/isolated/sessions"

    def __post_init__(self):
        if self.events is None:
            self.events = [{"type": "assistant/message"}]
        if self.notifications is None:
            self.notifications = [{"method": "session/event"}]


class FakeHarness:
    def __init__(self, outcomes=None, release=None, close_outcomes=None):
        self.outcomes = list(outcomes or [FakeRunResult()])
        self.release = release
        self.close_outcomes = list(close_outcomes or [None])
        self.started = threading.Event()
        self.calls = []
        self.start_calls = 0
        self.close_calls = 0
        self.active_runs = 0
        self.peak_active_runs = 0
        self._lock = threading.Lock()

    def start(self):
        self.start_calls += 1

    def run(self, prompt, *, session_id):
        with self._lock:
            call_index = len(self.calls)
            self.calls.append((prompt, session_id))
            self.active_runs += 1
            self.peak_active_runs = max(self.peak_active_runs, self.active_runs)
        self.started.set()
        try:
            if self.release is not None and not self.release.wait(timeout=2):
                raise TimeoutError("fake harness was not released")
            outcome = self.outcomes[call_index]
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        finally:
            with self._lock:
                self.active_runs -= 1

    def close(self):
        call_index = self.close_calls
        self.close_calls += 1
        outcome = self.close_outcomes[call_index]
        if isinstance(outcome, BaseException):
            raise outcome


class RecordingRuntime:
    def __init__(self):
        self.close_calls = 0

    async def invoke(self, _invocation):
        return AgentResult("recorded")

    async def close(self):
        self.close_calls += 1


def make_invocation(idempotency_key="invoke:task-1", task_id="task-1"):
    handoff = HandoffContract(
        goal="分析协同运行时",
        acceptance_criteria=("结果可验证", "不泄露凭据"),
        deliverables=("analysis.md",),
        constraints=("禁止外部发消息",),
    )
    task = TaskSpec(
        "运行时调研",
        "researcher",
        handoff,
        task_id=task_id,
        metadata={"source": "contract-test"},
    )
    system = ActorRef("orchestrator", "Orchestrator", ActorKind.SYSTEM)
    agent = ActorRef("researcher", "Researcher", ActorKind.AGENT)
    envelope = CoordinationEnvelope.create(
        session_id="coordination-session",
        thread_id=task_id,
        sender=system,
        recipients=(agent,),
        kind=EnvelopeKind.TASK_ASSIGN,
        payload={"task": task.to_dict()},
        correlation_id="correlation-1",
        causation_id=task_id,
        idempotency_key=idempotency_key,
        authority=handoff.authority,
    )
    item = ContextItem(
        "context-1",
        "goal",
        "这是调用前已经记录的上下文。",
        required=True,
        provenance="event-store",
    )
    context = ContextBundle((item,), (), 1_000, item.estimated_tokens, "sha256:context")
    return AgentInvocation(task, envelope, context)


class AgentRuntimeContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_callable_runtime_preserves_original_handler_behavior(self):
        observed = []

        async def handler(invocation):
            observed.append(invocation.task.task_id)
            return AgentResult("legacy result", metadata={"source": "callable"})

        runtime = CallableAgentRuntime(handler)
        self.assertIsInstance(runtime, AgentRuntimePort)

        result = await runtime.invoke(make_invocation())

        self.assertEqual(result.narration, "legacy result")
        self.assertEqual(observed, ["task-1"])
        await runtime.close()
        with self.assertRaises(AgentRuntimeClosedError):
            await runtime.invoke(make_invocation())

    async def test_registration_accepts_runtime_and_keeps_positional_handler_compatibility(self):
        actor = ActorRef("researcher", "Researcher", ActorKind.AGENT)

        async def handler(_invocation):
            return AgentResult("ok")

        legacy = AgentRegistration(actor, handler)
        registry = AgentRegistry()
        registry.register(legacy)
        self.assertEqual((await registry.invoke(make_invocation())).narration, "ok")

        other = ActorRef("other", "Other", ActorKind.AGENT)
        runtime = CallableAgentRuntime(handler)
        registration = AgentRegistration(other, runtime=runtime)
        self.assertIs(registration.runtime, runtime)
        with self.assertRaises(ValueError):
            AgentRegistration(other)
        with self.assertRaises(ValueError):
            AgentRegistration(other, handler, runtime=runtime)

    async def test_kernel_close_closes_shared_runtime_once_and_rejects_new_work(self):
        runtime = RecordingRuntime()
        kernel = OrchestratorKernel()
        first_actor = ActorRef("researcher", "Researcher", ActorKind.AGENT)
        second_actor = ActorRef("reviewer", "Reviewer", ActorKind.AGENT)
        kernel.register_agent(AgentRegistration(first_actor, runtime=runtime))
        kernel.register_agent(AgentRegistration(second_actor, runtime=runtime))

        await kernel.close()
        await kernel.close()

        self.assertEqual(runtime.close_calls, 1)
        with self.assertRaises(AgentRuntimeClosedError):
            await kernel.registry.invoke(make_invocation())
        with self.assertRaises(AgentRuntimeClosedError):
            kernel.register_agent(AgentRegistration(first_actor, runtime=RecordingRuntime()))
        kernel.event_store.close()

    async def test_kernel_close_retries_runtime_that_failed_to_close(self):
        fake = FakeHarness(close_outcomes=[RuntimeError("first close failed"), None])
        runtime = DeepSeekHarnessRuntime(lambda: fake)
        await runtime.invoke(make_invocation())
        kernel = OrchestratorKernel()
        actor = ActorRef("researcher", "Researcher", ActorKind.AGENT)
        kernel.register_agent(AgentRegistration(actor, runtime=runtime))

        with self.assertRaisesRegex(RuntimeError, "first close failed"):
            await kernel.close()
        await kernel.close()

        self.assertEqual(fake.close_calls, 2)
        with self.assertRaises(AgentRuntimeClosedError):
            kernel.register_agent(AgentRegistration(actor, runtime=RecordingRuntime()))
        kernel.event_store.close()

    async def test_prompt_session_and_result_mapping_preserve_contract_metadata(self):
        fake = FakeHarness(
            [
                FakeRunResult(
                    session_id="returned-dsh-session",
                    final_response="调研完成",
                    events=[{}, {}],
                    notifications=[{}, {}, {}],
                )
            ]
        )
        runtime = DeepSeekHarnessRuntime(lambda: fake)
        invocation = make_invocation()

        result = await runtime.invoke(invocation)

        self.assertEqual(result.narration, "调研完成")
        prompt, requested_session_id = fake.calls[0]
        self.assertIn("运行时调研", prompt)
        self.assertIn("分析协同运行时", prompt)
        self.assertIn("禁止外部发消息", prompt)
        self.assertIn("这是调用前已经记录的上下文", prompt)
        self.assertIn("sha256:context", prompt)
        self.assertEqual(requested_session_id, runtime.session_id_for(invocation))
        self.assertEqual(requested_session_id, runtime.session_id_for(make_invocation()))
        self.assertNotEqual(
            requested_session_id,
            runtime.session_id_for(make_invocation(task_id="task-2")),
        )
        self.assertEqual(
            result.metadata,
            {
                "runtime": "deepseek-harness",
                "harnessSessionId": "returned-dsh-session",
                "finishReason": "completed",
                "eventCount": 2,
                "notificationCount": 3,
                "sessionRoot": "/isolated/sessions",
                "contextDigest": "sha256:context",
                "coordinationSessionId": "coordination-session",
                "taskId": "task-1",
            },
        )
        await runtime.close()
        self.assertEqual(fake.start_calls, 1)
        self.assertEqual(fake.close_calls, 1)

    async def test_explicit_factory_is_required_to_prevent_unsafe_sdk_default(self):
        with self.assertRaisesRegex(
            DeepSeekHarnessConfigurationError, "explicit isolated harness_factory"
        ):
            DeepSeekHarnessRuntime(None)

    async def test_harness_is_started_once_before_concurrent_runs(self):
        fake = FakeHarness([FakeRunResult(), FakeRunResult()])
        runtime = DeepSeekHarnessRuntime(lambda: fake, max_concurrency=2)

        await asyncio.gather(
            runtime.invoke(make_invocation("invoke:one", "task-one")),
            runtime.invoke(make_invocation("invoke:two", "task-two")),
        )

        self.assertEqual(fake.start_calls, 1)
        self.assertEqual(len(fake.calls), 2)
        await runtime.close()

    async def test_turns_for_same_dsh_session_are_serialized(self):
        release = threading.Event()
        fake = FakeHarness([FakeRunResult(), FakeRunResult()], release=release)
        runtime = DeepSeekHarnessRuntime(lambda: fake, max_concurrency=2)
        first = asyncio.create_task(runtime.invoke(make_invocation("invoke:first")))
        self.assertTrue(await asyncio.to_thread(fake.started.wait, 1))
        second = asyncio.create_task(runtime.invoke(make_invocation("invoke:second")))

        await asyncio.sleep(0.01)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.peak_active_runs, 1)
        release.set()
        await asyncio.gather(first, second)

        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(fake.peak_active_runs, 1)
        await runtime.close()

    async def test_duplicate_concurrent_invocations_execute_harness_once(self):
        release = threading.Event()
        fake = FakeHarness(release=release)
        runtime = DeepSeekHarnessRuntime(lambda: fake)
        invocation = make_invocation()

        first = asyncio.create_task(runtime.invoke(invocation))
        self.assertTrue(await asyncio.to_thread(fake.started.wait, 1))
        second = asyncio.create_task(runtime.invoke(invocation))
        await asyncio.sleep(0)
        release.set()
        first_result, second_result = await asyncio.gather(first, second)
        cached_result = await runtime.invoke(invocation)

        self.assertEqual(len(fake.calls), 1)
        self.assertIs(first_result, second_result)
        self.assertIs(first_result, cached_result)
        await runtime.close()

    async def test_failed_invocation_is_not_cached_and_can_be_retried(self):
        fake = FakeHarness([RuntimeError("temporary failure"), FakeRunResult()])
        runtime = DeepSeekHarnessRuntime(lambda: fake)
        invocation = make_invocation()

        with self.assertRaisesRegex(RuntimeError, "temporary failure"):
            await runtime.invoke(invocation)
        result = await runtime.invoke(invocation)

        self.assertEqual(result.narration, "completed result")
        self.assertEqual(len(fake.calls), 2)
        await runtime.close()

    async def test_same_idempotency_key_rejects_changed_invocation_content(self):
        fake = FakeHarness()
        runtime = DeepSeekHarnessRuntime(lambda: fake)

        await runtime.invoke(make_invocation("invoke:stable", "task-1"))
        with self.assertRaises(AgentInvocationConflictError):
            await runtime.invoke(make_invocation("invoke:stable", "different-task"))

        self.assertEqual(len(fake.calls), 1)
        await runtime.close()

    async def test_non_success_and_missing_finish_reasons_fail_loudly(self):
        for reason in ("error", "max-tokens", None):
            with self.subTest(reason=reason):
                fake = FakeHarness([FakeRunResult(finish_reason=reason)])
                runtime = DeepSeekHarnessRuntime(lambda fake=fake: fake)
                with self.assertRaises(DeepSeekHarnessRunError):
                    await runtime.invoke(make_invocation())
                await runtime.close()

    async def test_close_rejects_new_work_then_drains_and_closes_harness(self):
        release = threading.Event()
        fake = FakeHarness(release=release)
        runtime = DeepSeekHarnessRuntime(lambda: fake)
        running = asyncio.create_task(runtime.invoke(make_invocation()))
        self.assertTrue(await asyncio.to_thread(fake.started.wait, 1))

        closing = asyncio.create_task(runtime.close())
        await asyncio.sleep(0.01)
        self.assertFalse(closing.done())
        self.assertEqual(fake.close_calls, 0)
        with self.assertRaises(AgentRuntimeClosedError):
            await runtime.invoke(make_invocation("invoke:new", "task-new"))

        release.set()
        result = await running
        await closing
        self.assertEqual(result.narration, "completed result")
        self.assertEqual(fake.close_calls, 1)
        await runtime.close()
        self.assertEqual(fake.close_calls, 1)

    async def test_missing_optional_sdk_has_clear_lazy_error(self):
        def missing_factory():
            raise ModuleNotFoundError(
                "No module named 'deepseek_harness'", name="deepseek_harness"
            )

        runtime = DeepSeekHarnessRuntime(missing_factory)
        with self.assertRaisesRegex(DeepSeekHarnessDependencyError, "optional.*Python 3.10"):
            await runtime.invoke(make_invocation())
        await runtime.close()

    async def test_close_failure_keeps_harness_for_retry(self):
        fake = FakeHarness(close_outcomes=[RuntimeError("reap failed"), None])
        runtime = DeepSeekHarnessRuntime(lambda: fake)
        await runtime.invoke(make_invocation())

        with self.assertRaisesRegex(RuntimeError, "reap failed"):
            await runtime.close()
        self.assertEqual(fake.close_calls, 1)
        with self.assertRaises(AgentRuntimeClosedError):
            await runtime.invoke(make_invocation("invoke:new", "task-new"))

        await runtime.close()
        self.assertEqual(fake.close_calls, 2)

    async def test_success_cache_is_bounded(self):
        fake = FakeHarness([FakeRunResult(), FakeRunResult(), FakeRunResult()])
        runtime = DeepSeekHarnessRuntime(lambda: fake, max_cached_results=1)

        await runtime.invoke(make_invocation("invoke:one", "task-one"))
        await runtime.invoke(make_invocation("invoke:two", "task-two"))
        await runtime.invoke(make_invocation("invoke:one", "task-one"))

        self.assertEqual(len(fake.calls), 3)
        await runtime.close()

    async def test_cancellation_is_explicitly_unsupported(self):
        runtime = DeepSeekHarnessRuntime(lambda: FakeHarness())
        with self.assertRaises(AgentCancellationUnsupportedError):
            await runtime.cancel("invoke:task-1")
        await runtime.close()

    async def test_canceling_waiter_reports_remote_turn_is_still_running(self):
        release = threading.Event()
        fake = FakeHarness(release=release)
        runtime = DeepSeekHarnessRuntime(lambda: fake)
        invocation = make_invocation()
        waiter = asyncio.create_task(runtime.invoke(invocation))
        self.assertTrue(await asyncio.to_thread(fake.started.wait, 1))

        waiter.cancel()
        with self.assertRaisesRegex(AgentCancellationUnsupportedError, "still running"):
            await waiter
        release.set()
        result = await runtime.invoke(invocation)

        self.assertEqual(result.narration, "completed result")
        self.assertEqual(len(fake.calls), 1)
        await runtime.close()


if __name__ == "__main__":
    unittest.main()
