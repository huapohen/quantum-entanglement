import unittest

from quantum_entanglement.context import ContextBudgetError, ContextCompiler, ContextItem
from quantum_entanglement.protocol import HandoffContract, TaskStatus
from quantum_entanglement.scheduler import TaskGraph, TaskSpec


def handoff(goal="完成任务"):
    return HandoffContract(
        goal=goal,
        acceptance_criteria=("结果可验证",),
        deliverables=("result.md",),
    )


class TaskGraphTests(unittest.TestCase):
    def test_initial_ready_transitions_are_explicit_and_revisioned(self):
        first = TaskSpec("先做", "agent-a", handoff(), task_id="first")
        second = TaskSpec("后做", "agent-b", handoff(), task_id="second", depends_on=("first",))
        graph = TaskGraph((first, second))

        self.assertEqual(graph.statuses["first"], TaskStatus.PENDING)
        transitions = graph.refresh()
        self.assertEqual([(item.task_id, item.revision) for item in transitions], [("first", 1)])
        graph.transition("first", TaskStatus.RUNNING)
        graph.transition("first", TaskStatus.COMPLETED)
        self.assertEqual(graph.refresh()[0].task_id, "second")
        self.assertEqual(graph.revisions, {"first": 3, "second": 1})

    def test_cycle_and_missing_dependency_are_rejected(self):
        with self.assertRaises(ValueError):
            TaskGraph((TaskSpec("x", "a", handoff(), task_id="x", depends_on=("missing",)),))
        with self.assertRaises(ValueError):
            TaskGraph(
                (
                    TaskSpec("x", "a", handoff(), task_id="x", depends_on=("y",)),
                    TaskSpec("y", "a", handoff(), task_id="y", depends_on=("x",)),
                )
            )

    def test_failure_deterministically_blocks_dependents(self):
        first = TaskSpec("先做", "agent-a", handoff(), task_id="first")
        second = TaskSpec("后做", "agent-b", handoff(), task_id="second", depends_on=("first",))
        graph = TaskGraph((first, second))
        graph.refresh()
        graph.transition("first", TaskStatus.RUNNING)
        graph.transition("first", TaskStatus.FAILED, "boom")

        transition = graph.refresh()[0]

        self.assertEqual(transition.current, TaskStatus.BLOCKED)
        self.assertIn("first", transition.reason)


class ContextCompilerTests(unittest.TestCase):
    def test_required_items_are_kept_and_optional_omissions_are_visible(self):
        items = (
            ContextItem("goal", "goal", "必须保留", required=True),
            ContextItem("high", "decision", "重要", relevance=1.0),
            ContextItem("low", "chat", "x" * 80, relevance=0.1),
        )
        required_tokens = items[0].estimated_tokens
        high_tokens = items[1].estimated_tokens

        bundle = ContextCompiler().compile(items, required_tokens + high_tokens)

        self.assertEqual([item.item_id for item in bundle.items], ["goal", "high"])
        self.assertEqual(bundle.omitted_item_ids, ("low",))
        self.assertIn("## omitted", bundle.render())

    def test_required_context_is_never_silently_truncated(self):
        with self.assertRaises(ContextBudgetError):
            ContextCompiler().compile((ContextItem("goal", "goal", "x" * 100, required=True),), 1)


if __name__ == "__main__":
    unittest.main()
