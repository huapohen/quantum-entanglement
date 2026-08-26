"""Model-backed product-trial workflow built on the coordination kernel."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .agent_runtime import AgentInvocation, AgentResult, AgentRuntimePort
from .protocol import ActorKind, ActorRef, ArtifactOutput, HandoffContract, new_id
from .runtime import AgentRegistration, OrchestratorKernel
from .scheduler import TaskSpec, WorkflowPlan


class ProductTrialRunError(RuntimeError):
    """The governed workflow did not produce a complete result."""

    def __init__(self, task_errors: Mapping[str, str]) -> None:
        self.task_errors = dict(task_errors)
        super().__init__("model-backed workflow did not complete")


class _ArtifactMappingRuntime:
    """Map provider-neutral narration into the task's declared deliverable."""

    def __init__(self, runtime: AgentRuntimePort) -> None:
        self._runtime = runtime

    async def invoke(self, invocation: AgentInvocation) -> AgentResult:
        result = await self._runtime.invoke(invocation)
        deliverables = invocation.task.handoff.deliverables
        artifacts = result.artifacts
        if not artifacts and deliverables:
            artifacts = tuple(
                ArtifactOutput(
                    name,
                    result.narration,
                    metadata={
                        "runtime": str(result.metadata.get("runtime", "unknown")),
                        "taskId": invocation.task.task_id,
                    },
                )
                for name in deliverables
            )
        return AgentResult(result.narration, artifacts, result.metadata)

    async def close(self) -> None:
        await self._runtime.close()


def _handoff(
    *,
    goal: str,
    deliverable: str,
    acceptance: tuple[str, ...],
) -> HandoffContract:
    return HandoffContract(
        goal=goal,
        acceptance_criteria=acceptance,
        deliverables=(deliverable,),
    )


def _task_metadata(instruction: str, stage: str) -> Mapping[str, Any]:
    return {
        "userInstruction": instruction,
        "stage": stage,
        "outputFormat": "Markdown",
        "externalMessagingForbidden": True,
    }


async def run_custom_instruction(
    instruction: str,
    runtime: AgentRuntimePort,
) -> dict[str, object]:
    """Run one user instruction through a recorded three-stage Agent DAG."""

    normalized = instruction.strip()
    if not normalized:
        raise ValueError("instruction must not be blank")

    session_id = new_id("trial")
    human = ActorRef("local-user", "本地验收用户", ActorKind.HUMAN)
    analyst = ActorRef("researcher", "需求分析 Agent", ActorKind.AGENT)
    producer = ActorRef("architect", "方案生成 Agent", ActorKind.AGENT)
    reviewer = ActorRef("reviewer", "质量复核 Agent", ActorKind.AGENT)
    mapped_runtime = _ArtifactMappingRuntime(runtime)
    kernel = OrchestratorKernel(max_concurrency=1)
    for actor in (analyst, producer, reviewer):
        kernel.register_agent(
            AgentRegistration(
                actor,
                protocol="openai-responses",
                runtime=mapped_runtime,
            )
        )

    tasks = (
        TaskSpec(
            "分析用户指令、目标、约束与验收标准",
            analyst.actor_id,
            _handoff(
                goal="准确分析用户指令，指出必要假设、约束和完成标准",
                deliverable="01_analysis.md",
                acceptance=(
                    "忠实覆盖用户指令",
                    "区分事实、假设与待验证项",
                    "不执行任何外部消息发送",
                ),
            ),
            task_id="research",
            metadata=_task_metadata(normalized, "analysis"),
        ),
        TaskSpec(
            "基于分析结果生成可直接使用的完整成果",
            producer.actor_id,
            _handoff(
                goal="生成对用户指令的完整、具体、可执行答复或方案",
                deliverable="02_result.md",
                acceptance=(
                    "直接解决用户指令",
                    "利用上游分析而不是复述它",
                    "输出结构清晰的 Markdown",
                ),
            ),
            task_id="design",
            depends_on=("research",),
            metadata=_task_metadata(normalized, "production"),
        ),
        TaskSpec(
            "复核成果并给出最终交付版本",
            reviewer.actor_id,
            _handoff(
                goal="检查遗漏、矛盾、风险和可操作性，输出修订后的最终交付",
                deliverable="03_final_review.md",
                acceptance=(
                    "逐项核对原始用户指令",
                    "修正发现的问题",
                    "最终文本可直接交付用户",
                ),
            ),
            task_id="review",
            depends_on=("design",),
            metadata=_task_metadata(normalized, "review"),
        ),
    )

    try:
        result = await kernel.run(
            WorkflowPlan(
                session_id,
                normalized,
                human.actor_id,
                tasks,
            )
        )
        if not result.completed:
            raise ProductTrialRunError(result.errors)

        run_payload = result.to_dict()
        run_payload["artifacts"] = [
            {
                **item.ref.to_dict(),
                "content": item.content,
                "createdAt": item.created_at,
            }
            for item in kernel.artifacts.current_all(result.session_id)
        ]
        events = kernel.event_store.read_stream(f"session:{session_id}")
        narrations = [
            {
                "taskId": str(item.event.payload.get("taskId", "")),
                "agentId": item.event.actor_id,
                "content": str(item.event.payload.get("narration", "")),
                "metadata": dict(item.event.payload.get("metadata", {})),
            }
            for item in events
            if item.event.event_type == "task.result.received"
        ]
        return {
            "chatRoute": "local-direct",
            "directAgents": [analyst.actor_id, producer.actor_id, reviewer.actor_id],
            "instruction": normalized,
            "narration": narrations,
            "run": run_payload,
            "events": [
                {
                    "sequence": item.sequence,
                    "type": item.event.event_type,
                    "actorId": item.event.actor_id,
                }
                for item in events
            ],
        }
    finally:
        try:
            await kernel.close()
        finally:
            kernel.event_store.close()


__all__ = ["ProductTrialRunError", "run_custom_instruction"]
