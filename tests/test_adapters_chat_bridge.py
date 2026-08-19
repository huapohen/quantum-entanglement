import unittest
from unittest.mock import patch

from quantum_entanglement.adapters.a2a import A2AAgentCard, A2AJsonRpcAdapter
from quantum_entanglement.chat import ChatRoute, InboundChatMessage, MentionRouter
from quantum_entanglement.langgraph_bridge import BridgeStatus, LangGraphBridge
from quantum_entanglement.protocol import (
    ActorKind,
    ActorRef,
    CoordinationEnvelope,
    EnvelopeKind,
)


class A2AAdapterTests(unittest.TestCase):
    def test_agent_card_round_trip_preserves_unknown_extensions(self):
        raw = {
            "name": "Researcher",
            "description": "Finds evidence",
            "url": "https://agents.example/researcher",
            "version": "1.2.0",
            "protocolVersion": "0.3.0",
            "preferredTransport": "JSONRPC",
            "capabilities": {"streaming": True},
            "defaultInputModes": ["text/plain"],
            "defaultOutputModes": ["text/markdown"],
            "skills": [
                {
                    "id": "research",
                    "name": "Research",
                    "description": "Research public evidence",
                    "tags": ["web"],
                    "examples": ["Compare two protocols"],
                }
            ],
            "x-wanwork-store": {"category": "research"},
        }

        card = A2AAgentCard.from_dict(raw)

        self.assertEqual(card.to_dict(), raw)

    def test_message_mapping_preserves_internal_envelope_and_causation(self):
        local = ActorRef("orchestrator", "Orchestrator", ActorKind.SYSTEM)
        remote = ActorRef("remote", "Remote", ActorKind.AGENT)
        envelope = CoordinationEnvelope.create(
            "session",
            "task",
            local,
            (remote,),
            EnvelopeKind.TASK_ASSIGN,
            {"goal": "research"},
            correlation_id="correlation",
            causation_id="parent",
            idempotency_key="once",
        )
        adapter = A2AJsonRpcAdapter(local)

        request = adapter.message_send_request(envelope, blocking=True)
        restored = adapter.result_envelope(
            envelope,
            remote,
            {"jsonrpc": "2.0", "id": envelope.message_id, "result": {"kind": "task", "id": "r1"}},
        )

        embedded = request["params"]["message"]["parts"][0]["data"]["wanworkEnvelope"]
        self.assertEqual(CoordinationEnvelope.from_dict(embedded), envelope)
        self.assertEqual(restored.correlation_id, "correlation")
        self.assertEqual(restored.causation_id, envelope.message_id)
        self.assertEqual(restored.kind, EnvelopeKind.TASK_RESULT)


class MentionRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_mention_never_calls_planner_but_keeps_ingress_metadata(self):
        human = ActorRef("human", "Human", ActorKind.HUMAN)
        agent = ActorRef("agent", "Agent", ActorKind.AGENT)
        planner = ActorRef("planner", "Planner", ActorKind.AGENT)
        router = MentionRouter({agent.actor_id: agent}, planner)
        planner_calls = 0
        direct_calls = 0

        async def direct(routed):
            nonlocal direct_calls
            direct_calls += 1
            return routed

        async def plan(routed):
            nonlocal planner_calls
            planner_calls += 1
            return routed

        routed = await router.dispatch(
            InboundChatMessage("im", "m-1", "s", "t", human, "@Agent help", ("agent",)),
            direct,
            plan,
        )

        self.assertEqual(routed.route, ChatRoute.DIRECT)
        self.assertEqual(direct_calls, 1)
        self.assertEqual(planner_calls, 0)
        self.assertEqual(routed.envelope.idempotency_key, "chat:im:m-1")
        self.assertEqual(routed.envelope.causation_id, "m-1")

    async def test_unmentioned_message_goes_to_planner(self):
        human = ActorRef("human", "Human", ActorKind.HUMAN)
        planner = ActorRef("planner", "Planner", ActorKind.AGENT)
        router = MentionRouter({}, planner)

        async def direct(routed):
            raise AssertionError("direct handler should not be called")

        async def plan(routed):
            return routed.route

        result = await router.dispatch(
            InboundChatMessage("im", "m-2", "s", "t", human, "plan this"), direct, plan
        )
        self.assertEqual(result, ChatRoute.PLANNER)


class FakeGraph:
    def __init__(self):
        self.calls = []

    async def ainvoke(self, value, config):
        self.calls.append((value, config))
        if isinstance(value, dict) and value.get("pause"):
            return {"value": 1, "__interrupt__": ({"question": "approve?"},)}
        return {"value": 2}


class LangGraphBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_resume_without_optional_dependency_has_stable_error(self):
        bridge = LangGraphBridge(FakeGraph())

        with patch(
            "quantum_entanglement.langgraph_bridge.import_module",
            side_effect=ModuleNotFoundError("optional dependency is absent"),
        ):
            with self.assertRaisesRegex(RuntimeError, "optional langgraph dependency"):
                await bridge.resume("thread-1", "approve")

    async def test_start_and_resume_use_stable_thread_config(self):
        graph = FakeGraph()
        bridge = LangGraphBridge(graph, command_factory=lambda value: ("resume", value))

        paused = await bridge.start("thread-1", {"pause": True})
        resumed = await bridge.resume("thread-1", "approve")

        self.assertEqual(paused.status, BridgeStatus.INTERRUPTED)
        self.assertEqual(paused.interrupts[0]["question"], "approve?")
        self.assertEqual(resumed.status, BridgeStatus.COMPLETED)
        self.assertEqual(graph.calls[1][0], ("resume", "approve"))
        self.assertEqual(graph.calls[1][1]["configurable"]["thread_id"], "thread-1")


if __name__ == "__main__":
    unittest.main()
