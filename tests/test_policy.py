import unittest

from quantum_entanglement.policy import ApprovalRequest, NeedsYouQueue
from quantum_entanglement.protocol import ActionIntent, ApprovalDecision, RiskLevel


class NeedsYouQueueTests(unittest.TestCase):
    @staticmethod
    def _request() -> ApprovalRequest:
        return ApprovalRequest(
            "session-a",
            "task-a",
            ActionIntent(
                "publish",
                "external",
                risk=RiskLevel.HIGH,
                external_side_effect=True,
            ),
            "high-impact action needs a human",
            request_id="approval-a",
            created_at="2026-08-20T12:00:00.000000Z",
        )

    def test_caller_and_returned_snapshots_cannot_mutate_internal_authority(self) -> None:
        queue = NeedsYouQueue()
        caller_owned = self._request()
        created = queue.create(caller_owned)

        object.__setattr__(caller_owned, "session_id", "forged-session")
        object.__setattr__(caller_owned, "task_id", "forged-task")
        object.__setattr__(caller_owned, "request_id", "forged-request")
        object.__setattr__(caller_owned, "reason", "forged-reason")
        object.__setattr__(caller_owned, "created_at", "9999-12-31T23:59:59.999999Z")
        object.__setattr__(caller_owned.intent, "action", "delete")
        object.__setattr__(created, "task_id", "created-forged-task")
        object.__setattr__(created.intent, "target", "created-forged-target")

        stored = queue.get("approval-a")
        self.assertEqual(stored.session_id, "session-a")
        self.assertEqual(stored.task_id, "task-a")
        self.assertEqual(stored.request_id, "approval-a")
        self.assertEqual(stored.reason, "high-impact action needs a human")
        self.assertEqual(stored.created_at, "2026-08-20T12:00:00.000000Z")
        self.assertEqual(stored.intent.action, "publish")
        self.assertEqual(stored.intent.target, "external")

        pending = queue.pending("session-a")[0]
        object.__setattr__(pending, "task_id", "pending-forged-task")
        object.__setattr__(pending.intent, "action", "pending-forged-action")
        self.assertEqual(queue.get("approval-a").task_id, "task-a")
        self.assertEqual(queue.get("approval-a").intent.action, "publish")

        decided = queue.decide(
            "approval-a",
            ApprovalDecision.APPROVE,
            "owner",
            "approved",
        )
        object.__setattr__(decided, "task_id", "decided-forged-task")
        object.__setattr__(decided.intent, "target", "decided-forged-target")
        durable = queue.get("approval-a")
        self.assertEqual(durable.task_id, "task-a")
        self.assertEqual(durable.intent.target, "external")
        self.assertEqual(durable.decision, ApprovalDecision.APPROVE)
        self.assertEqual(durable.decided_by, "owner")
        self.assertEqual(durable.comment, "approved")

    def test_restore_also_detaches_input_and_output_snapshots(self) -> None:
        queue = NeedsYouQueue()
        caller_owned = self._request()
        restored = queue.restore(caller_owned)

        object.__setattr__(caller_owned, "task_id", "caller-forged")
        object.__setattr__(caller_owned.intent, "action", "caller-forged")
        object.__setattr__(restored, "task_id", "return-forged")
        object.__setattr__(restored.intent, "action", "return-forged")

        stored = queue.get("approval-a")
        self.assertEqual(stored.task_id, "task-a")
        self.assertEqual(stored.intent.action, "publish")


if __name__ == "__main__":
    unittest.main()
