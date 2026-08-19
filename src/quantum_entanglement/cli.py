"""Local, dependency-free demonstration of the coordination kernel."""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Sequence

from .chat import InboundChatMessage, MentionRouter
from .protocol import ActorKind, ActorRef, ArtifactOutput, HandoffContract
from .runtime import AgentRegistration, AgentResult, OrchestratorKernel
from .scheduler import TaskSpec, WorkflowPlan


def _handoff(goal: str, deliverable: str) -> HandoffContract:
    return HandoffContract(
        goal=goal,
        acceptance_criteria=("输出有来源", "结果可由下游直接消费"),
        deliverables=(deliverable,),
    )


async def run_demo() -> dict:
    kernel = OrchestratorKernel(max_concurrency=3)
    human = ActorRef("user-1", "产品负责人", ActorKind.HUMAN)
    planner = ActorRef("planner", "协作编排器", ActorKind.AGENT)
    researcher = ActorRef("researcher", "协议研究员", ActorKind.AGENT)
    architect = ActorRef("architect", "系统架构师", ActorKind.AGENT)
    reviewer = ActorRef("reviewer", "安全审阅员", ActorKind.AGENT)

    async def research(invocation):
        return AgentResult(
            "协议研究完成",
            (ArtifactOutput("protocol-notes.md", "A2A 管 Agent 互操作；MCP 管工具与数据。"),),
        )

    async def design(invocation):
        evidence = next(item.content for item in invocation.context.items if item.category == "artifact")
        return AgentResult(
            "架构设计完成",
            (ArtifactOutput("architecture.md", "事件溯源 + DAG + 插件 Harness。\n\n依据：" + evidence),),
        )

    async def review(invocation):
        design_text = next(item.content for item in invocation.context.items if item.category == "artifact")
        return AgentResult(
            "审阅通过",
            (ArtifactOutput("review.md", "已检查因果、幂等、权限和人工审批。\n\n" + design_text),),
        )

    kernel.register_agent(AgentRegistration(researcher, research))
    kernel.register_agent(AgentRegistration(architect, design))
    kernel.register_agent(AgentRegistration(reviewer, review))

    routed = MentionRouter(
        {actor.actor_id: actor for actor in (researcher, architect, reviewer)}, planner
    ).route(
        InboundChatMessage(
            "local-demo", "message-1", "demo-session", "group-thread", human,
            "@协议研究员 先研究协议，再交给架构师和审阅员。", (researcher.actor_id,),
        )
    )

    tasks = (
        TaskSpec(
            "研究协议边界", "researcher", _handoff("研究协议边界", "protocol-notes.md"),
            task_id="research",
        ),
        TaskSpec(
            "设计协作内核", "architect", _handoff("设计内核", "architecture.md"),
            task_id="design", depends_on=("research",),
        ),
        TaskSpec(
            "审阅安全不变量", "reviewer", _handoff("审阅方案", "review.md"),
            task_id="review", depends_on=("design",),
        ),
    )
    try:
        result = await kernel.run(
            WorkflowPlan("demo-session", "设计人和 Agent 的群聊协作内核", human.actor_id, tasks)
        )
        return {
            "chatRoute": routed.route.value,
            "directAgents": [actor.actor_id for actor in routed.direct_agents],
            "run": result.to_dict(),
            "events": [
                {
                    "sequence": item.sequence,
                    "type": item.event.event_type,
                    "actorId": item.event.actor_id,
                }
                for item in kernel.event_store.read_stream("session:demo-session")
            ],
        }
    finally:
        kernel.event_store.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compact", action="store_true", help="print compact JSON")
    args = parser.parse_args(argv)
    result = asyncio.run(run_demo())
    print(json.dumps(result, ensure_ascii=False, indent=None if args.compact else 2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
