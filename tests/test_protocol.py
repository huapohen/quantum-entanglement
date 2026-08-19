import unittest

from quantum_entanglement.protocol import (
    ActorKind,
    ActorRef,
    ArtifactRef,
    Authority,
    ContextRef,
    CoordinationEnvelope,
    EnvelopeKind,
    HandoffContract,
    RiskLevel,
)


class ProtocolTests(unittest.TestCase):
    def test_envelope_round_trip_preserves_causation_and_authority(self):
        human = ActorRef("u-1", "用户", ActorKind.HUMAN, "owner")
        agent = ActorRef("a-1", "研究员", ActorKind.AGENT, "researcher")
        envelope = CoordinationEnvelope.create(
            session_id="s-1",
            thread_id="t-1",
            sender=human,
            recipients=[agent],
            kind=EnvelopeKind.TASK_ASSIGN,
            payload={"goal": "调研协议"},
            causation_id="msg-parent",
            correlation_id="corr-1",
            idempotency_key="request-1",
            authority=Authority(
                allowed_actions=("research",),
                data_scopes=("public-web",),
                max_risk=RiskLevel.LOW,
            ),
        )

        restored = CoordinationEnvelope.from_dict(envelope.to_dict())

        self.assertEqual(restored, envelope)
        self.assertEqual(restored.causation_id, "msg-parent")
        self.assertTrue(restored.authority.permits("research", RiskLevel.LOW))
        self.assertFalse(restored.authority.permits("publish", RiskLevel.LOW))

    def test_handoff_requires_acceptance_criteria_and_deliverables(self):
        artifact = ArtifactRef(
            artifact_id="art-1",
            name="brief.md",
            version=1,
            media_type="text/markdown",
            uri="artifact://s/brief.md/v1",
            digest="sha256:abc",
            created_by="a-1",
            task_id="task-1",
        )
        context = ContextRef(
            ref_id="policy-1",
            category="policy",
            version="1",
            digest="sha256:def",
            required=True,
            relevance=1.0,
        )
        handoff = HandoffContract(
            goal="基于调研设计协议",
            acceptance_criteria=("包含状态机", "包含幂等规则"),
            deliverables=("protocol.md",),
            inputs=(artifact,),
            context_refs=(context,),
            authority=Authority(("write_draft",), max_risk=RiskLevel.LOW),
        )

        restored = HandoffContract.from_dict(handoff.to_dict())
        self.assertEqual(restored, handoff)

        with self.assertRaises(ValueError):
            HandoffContract(goal="x", acceptance_criteria=(), deliverables=("x",))

    def test_invalid_priority_is_rejected(self):
        actor = ActorRef("u", "User", ActorKind.HUMAN)
        with self.assertRaises(ValueError):
            CoordinationEnvelope.create(
                "s", "t", actor, (), EnvelopeKind.CHAT, {}, priority=101
            )


if __name__ == "__main__":
    unittest.main()

