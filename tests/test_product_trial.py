from __future__ import annotations

import unittest

from quantum_entanglement.agent_runtime import AgentInvocation, AgentResult
from quantum_entanglement.product_trial import ProductTrialRunError, run_custom_instruction


class RecordingRuntime:
    def __init__(self, *, fail_at: int | None = None) -> None:
        self.fail_at = fail_at
        self.invocations: list[AgentInvocation] = []
        self.closed = False

    async def invoke(self, invocation: AgentInvocation) -> AgentResult:
        self.invocations.append(invocation)
        if self.fail_at == len(self.invocations):
            raise RuntimeError("provider failed with redacted diagnostic")
        return AgentResult(
            f"# {invocation.task.task_id}\n\n模型结果 {len(self.invocations)}",
            metadata={"runtime": "fake-model", "model": "test-model"},
        )

    async def close(self) -> None:
        self.closed = True


class ProductTrialWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_instruction_runs_three_recorded_model_stages_and_maps_artifacts(self) -> None:
        runtime = RecordingRuntime()

        payload = await run_custom_instruction("制定一次发布计划", runtime)

        self.assertTrue(runtime.closed)
        self.assertEqual(
            [item.task.task_id for item in runtime.invocations], ["research", "design", "review"]
        )
        for invocation in runtime.invocations:
            self.assertEqual(invocation.task.metadata["userInstruction"], "制定一次发布计划")
            self.assertTrue(invocation.envelope.idempotency_key.startswith("invoke:"))
        self.assertFalse(
            any(item.category == "artifact" for item in runtime.invocations[0].context.items)
        )
        self.assertTrue(
            any(item.category == "artifact" for item in runtime.invocations[1].context.items)
        )
        self.assertTrue(
            any(item.category == "artifact" for item in runtime.invocations[2].context.items)
        )
        self.assertEqual(
            payload["run"]["statuses"],
            {"research": "completed", "design": "completed", "review": "completed"},
        )
        artifacts = payload["run"]["artifacts"]
        self.assertEqual(
            [item["name"] for item in artifacts],
            ["01_analysis.md", "02_result.md", "03_final_review.md"],
        )
        self.assertEqual(len(payload["narration"]), 3)
        self.assertEqual(payload["instruction"], "制定一次发布计划")

    async def test_model_failure_never_becomes_a_synthetic_success(self) -> None:
        runtime = RecordingRuntime(fail_at=2)

        with self.assertRaises(ProductTrialRunError):
            await run_custom_instruction("需要真实结果", runtime)

        self.assertTrue(runtime.closed)
        self.assertEqual(len(runtime.invocations), 2)

    async def test_blank_instruction_is_rejected_before_runtime_use(self) -> None:
        runtime = RecordingRuntime()

        with self.assertRaisesRegex(ValueError, "instruction must not be blank"):
            await run_custom_instruction("   ", runtime)

        self.assertFalse(runtime.closed)
        self.assertEqual(runtime.invocations, [])


if __name__ == "__main__":
    unittest.main()
