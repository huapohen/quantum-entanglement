import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import quantum_entanglement
import quantum_entanglement.runtime as runtime_module
from quantum_entanglement.events import DomainEvent
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
from quantum_entanglement.store import SQLiteEventStore


def handoff(deliverable="result.md"):
    return HandoffContract(
        goal="完成任务",
        acceptance_criteria=("结果可验证",),
        deliverables=(deliverable,),
    )


def registration(agent_id, handler):
    return AgentRegistration(ActorRef(agent_id, agent_id, ActorKind.AGENT), handler)


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tempdir.name) / "events.sqlite3")

    async def asyncTearDown(self):
        self.tempdir.cleanup()

    @staticmethod
    def _approval_plan(session_id):
        return WorkflowPlan(
            session_id,
            "严格审批恢复",
            "user",
            (
                TaskSpec(
                    "publish",
                    "publisher",
                    handoff(),
                    task_id="publish",
                    action=ActionIntent(
                        "publish",
                        "external",
                        risk=RiskLevel.HIGH,
                        external_side_effect=True,
                    ),
                ),
            ),
            plan_id=f"plan-{session_id}",
            correlation_id=f"correlation-{session_id}",
        )

    async def _seed_approval_history(self, path, session_id, *, decided):
        plan = self._approval_plan(session_id)
        kernel = OrchestratorKernel(event_store=SQLiteEventStore(path))
        try:
            paused = await kernel.run(plan)
            request = paused.needs_you[0]
            if decided:
                await kernel.decide(
                    request.request_id,
                    ApprovalDecision.APPROVE,
                    "owner",
                    "approved",
                )
            return plan
        finally:
            kernel.event_store.close()

    @staticmethod
    def _mutate_event_payload(path, session_id, event_type, mutate):
        store = SQLiteEventStore(path)
        try:
            row = store._connection.execute(
                """
                SELECT global_position, payload_json
                FROM events
                WHERE stream_id = ? AND event_type = ?
                ORDER BY sequence
                LIMIT 1
                """,
                (f"session:{session_id}", event_type),
            ).fetchone()
            if row is None:
                raise AssertionError(f"missing seeded {event_type} event")
            payload = json.loads(row["payload_json"])
            mutate(payload)
            store._connection.execute(
                "UPDATE events SET payload_json = ? WHERE global_position = ?",
                (
                    json.dumps(
                        payload,
                        allow_nan=False,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    row["global_position"],
                ),
            )
        finally:
            store.close()

    @staticmethod
    def _mutate_event_envelope(path, session_id, event_type, column, value):
        if column not in {"actor_id", "correlation_id", "causation_id", "idempotency_key"}:
            raise AssertionError("test requested an unsafe event column")
        store = SQLiteEventStore(path)
        try:
            store._connection.execute(
                f"""
                UPDATE events SET {column} = ?
                WHERE stream_id = ? AND event_type = ?
                """,
                (value, f"session:{session_id}", event_type),
            )
        finally:
            store.close()

    def _assert_approval_recovery_rejected(self, path, plan, message):
        candidate = OrchestratorKernel(event_store=SQLiteEventStore(path))
        try:
            with self.assertRaisesRegex(SessionRecoveryError, message):
                candidate._recover_session(plan)
            self.assertNotIn(plan.session_id, candidate._plans)
            self.assertNotIn(plan.session_id, candidate._graphs)
            self.assertEqual(candidate.approvals.pending(plan.session_id), ())
        finally:
            candidate.event_store.close()

    async def test_completed_plan_is_recovered_without_invoking_agents_again(self):
        calls = 0

        async def handler(invocation):
            nonlocal calls
            calls += 1
            return AgentResult("done", (ArtifactOutput("result.md", "stable"),))

        plan = WorkflowPlan(
            "recover-complete",
            "完成一次",
            "user",
            (TaskSpec("task", "worker", handoff(), task_id="task"),),
            plan_id="plan-stable",
        )
        first = OrchestratorKernel(event_store=SQLiteEventStore(self.path))
        first.register_agent(registration("worker", handler))
        result = await first.run(plan)
        event_count = len(first.event_store.read_stream("session:recover-complete"))
        first.event_store.close()

        second = OrchestratorKernel(event_store=SQLiteEventStore(self.path))
        second.register_agent(registration("worker", handler))
        recovered = await second.run(plan)

        self.assertTrue(result.completed)
        self.assertTrue(recovered.completed)
        self.assertEqual(calls, 1)
        self.assertEqual(
            len(second.event_store.read_stream("session:recover-complete")), event_count
        )
        self.assertEqual(recovered.artifacts[0].name, "result.md")
        altered = WorkflowPlan(
            "recover-complete",
            "悄悄改变目标",
            "user",
            plan.tasks,
            plan_id="plan-stable",
        )
        with self.assertRaises(ValueError):
            await second.run(altered)
        second.event_store.close()

    async def test_durably_running_task_is_quarantined_without_reinvocation(self):
        calls = 0

        async def handler(invocation):
            nonlocal calls
            calls += 1
            return AgentResult("must not run")

        plan = WorkflowPlan(
            "recover-running",
            "不可重复执行",
            "user",
            (TaskSpec("task", "worker", handoff(), task_id="task"),),
            plan_id="plan-running",
        )
        seeded = OrchestratorKernel(event_store=SQLiteEventStore(self.path))
        graph = await seeded._initialize_plan(plan)
        running = graph.transition("task", TaskStatus.RUNNING)
        await seeded._record_transition(plan.session_id, running, plan.plan_id)
        event_count = len(seeded.event_store.read_stream("session:recover-running"))
        seeded.event_store.close()

        recovered = OrchestratorKernel(event_store=SQLiteEventStore(self.path))
        recovered.register_agent(registration("worker", handler))
        with self.assertRaisesRegex(
            SessionRecoveryError,
            "durably RUNNING task without supported invocation recovery evidence",
        ):
            await recovered.run(plan)

        self.assertEqual(calls, 0)
        self.assertEqual(
            len(recovered.event_store.read_stream("session:recover-running")),
            event_count,
        )
        self.assertNotIn(plan.session_id, recovered._plans)
        self.assertNotIn(plan.session_id, recovered._graphs)
        recovered.event_store.close()

    async def test_cancelled_in_process_run_is_quarantined_without_reinvocation(self):
        calls = 0
        started = asyncio.Event()

        async def handler(invocation):
            nonlocal calls
            calls += 1
            started.set()
            await asyncio.Event().wait()
            return AgentResult("must not complete")

        plan = WorkflowPlan(
            "recover-cancelled-running",
            "取消后不可重复执行",
            "user",
            (TaskSpec("task", "worker", handoff(), task_id="task"),),
            plan_id="plan-cancelled-running",
        )
        kernel = OrchestratorKernel(event_store=SQLiteEventStore(self.path))
        kernel.register_agent(registration("worker", handler))
        running = asyncio.create_task(kernel.run(plan))
        await asyncio.wait_for(started.wait(), timeout=1)
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running

        event_count = len(kernel.event_store.read_stream(f"session:{plan.session_id}"))
        self.assertEqual(kernel._graphs[plan.session_id].statuses["task"], TaskStatus.RUNNING)
        with self.assertRaisesRegex(
            SessionRecoveryError,
            "durably RUNNING task without supported invocation recovery evidence",
        ):
            await kernel.run(plan)

        self.assertEqual(calls, 1)
        self.assertEqual(
            len(kernel.event_store.read_stream(f"session:{plan.session_id}")),
            event_count,
        )
        kernel.event_store.close()

    async def test_approval_and_dependency_artifact_survive_multiple_restarts(self):
        research_calls = 0

        async def researcher(invocation):
            nonlocal research_calls
            research_calls += 1
            return AgentResult("facts", (ArtifactOutput("evidence.md", "durable evidence"),))

        async def publisher_never(invocation):
            raise AssertionError("publisher must not run before approval")

        publish_task = TaskSpec(
            "publish",
            "publisher",
            handoff("publication.md"),
            task_id="publish",
            depends_on=("research",),
            action=ActionIntent(
                "publish", "external", risk=RiskLevel.HIGH, external_side_effect=True
            ),
        )
        plan = WorkflowPlan(
            "recover-approval",
            "研究后发布",
            "user",
            (
                TaskSpec("research", "researcher", handoff("evidence.md"), task_id="research"),
                publish_task,
            ),
            plan_id="plan-approval",
        )

        first = OrchestratorKernel(event_store=SQLiteEventStore(self.path))
        first.register_agent(registration("researcher", researcher))
        first.register_agent(registration("publisher", publisher_never))
        paused = await first.run(plan)
        request_id = paused.needs_you[0].request_id
        self.assertEqual(paused.statuses["publish"], TaskStatus.WAITING_APPROVAL)
        first.event_store.close()

        # Recover only to record the human decision, then simulate another crash.
        second = OrchestratorKernel(event_store=SQLiteEventStore(self.path))
        recovered_pause = await second.run(plan)
        self.assertEqual(recovered_pause.needs_you[0].request_id, request_id)
        await second.decide(request_id, ApprovalDecision.APPROVE, "owner")
        second.event_store.close()

        observed_context = ""
        publish_calls = 0

        async def publisher(invocation):
            nonlocal observed_context, publish_calls
            publish_calls += 1
            observed_context = invocation.context.render()
            return AgentResult("published", (ArtifactOutput("publication.md", "published"),))

        third = OrchestratorKernel(event_store=SQLiteEventStore(self.path))
        third.register_agent(registration("researcher", researcher))
        third.register_agent(registration("publisher", publisher))
        completed = await third.run(plan)

        self.assertTrue(completed.completed)
        self.assertEqual(research_calls, 1)
        self.assertEqual(publish_calls, 1)
        self.assertIn("durable evidence", observed_context)
        self.assertEqual(completed.needs_you, ())
        self.assertEqual(
            {item.name for item in completed.artifacts}, {"evidence.md", "publication.md"}
        )
        third.event_store.close()

    async def test_session_recovery_rejects_noncanonical_approval_payloads(self):
        cases = (
            (
                "request-extra-field",
                False,
                "approval.requested",
                lambda payload: payload.__setitem__("unexpected", None),
                "invalid shape",
            ),
            (
                "request-coerced-session",
                False,
                "approval.requested",
                lambda payload: payload.__setitem__("sessionId", True),
                "sessionId is invalid",
            ),
            (
                "request-noncanonical-time",
                False,
                "approval.requested",
                lambda payload: payload.__setitem__(
                    "createdAt",
                    "2026-08-20T08:00:00+08:00",
                ),
                "not canonical UTC",
            ),
            (
                "request-predecided",
                False,
                "approval.requested",
                lambda payload: payload.__setitem__("decision", "approve"),
                "already contains a decision",
            ),
            (
                "request-intent-drift",
                False,
                "approval.requested",
                lambda payload: payload["intent"].__setitem__("target", "other"),
                "intent differs",
            ),
            (
                "request-reason-drift",
                False,
                "approval.requested",
                lambda payload: payload.__setitem__("reason", "forged"),
                "reason differs",
            ),
            (
                "decision-extra-field",
                True,
                "approval.decided",
                lambda payload: payload.__setitem__("unexpected", None),
                "invalid shape",
            ),
            (
                "decision-coerced-value",
                True,
                "approval.decided",
                lambda payload: payload.__setitem__("decision", True),
                "decision is invalid",
            ),
            (
                "decision-comment-type",
                True,
                "approval.decided",
                lambda payload: payload.__setitem__("comment", 7),
                "comment is invalid",
            ),
            (
                "decision-request-drift",
                True,
                "approval.decided",
                lambda payload: payload.__setitem__("reason", "forged"),
                "changes its request identity",
            ),
            (
                "decision-future-created-at",
                True,
                "approval.decided",
                lambda payload: payload.__setitem__(
                    "createdAt",
                    "9999-12-31T23:59:59.999999Z",
                ),
                "timestamp violates event causality",
            ),
        )
        for index, (label, decided, event_type, mutate, message) in enumerate(cases):
            with self.subTest(label=label):
                path = str(Path(self.tempdir.name) / f"approval-payload-{index}.sqlite3")
                session_id = f"approval-payload-{index}"
                plan = await self._seed_approval_history(
                    path,
                    session_id,
                    decided=decided,
                )
                self._mutate_event_payload(path, session_id, event_type, mutate)
                self._assert_approval_recovery_rejected(path, plan, message)

    async def test_session_recovery_binds_approval_event_envelopes(self):
        cases = (
            ("request-actor", False, "approval.requested", "actor_id", "intruder"),
            (
                "request-correlation",
                False,
                "approval.requested",
                "correlation_id",
                "other-correlation",
            ),
            (
                "request-causation",
                False,
                "approval.requested",
                "causation_id",
                "other-task",
            ),
            (
                "request-idempotency",
                False,
                "approval.requested",
                "idempotency_key",
                "approval-request:other",
            ),
            ("decision-actor", True, "approval.decided", "actor_id", "intruder"),
            (
                "decision-correlation",
                True,
                "approval.decided",
                "correlation_id",
                "other-correlation",
            ),
            (
                "decision-causation",
                True,
                "approval.decided",
                "causation_id",
                "other-request",
            ),
            (
                "decision-idempotency",
                True,
                "approval.decided",
                "idempotency_key",
                "approval-decision:other",
            ),
        )
        for index, (label, decided, event_type, column, value) in enumerate(cases):
            with self.subTest(label=label):
                path = str(Path(self.tempdir.name) / f"approval-envelope-{index}.sqlite3")
                session_id = f"approval-envelope-{index}"
                plan = await self._seed_approval_history(
                    path,
                    session_id,
                    decided=decided,
                )
                self._mutate_event_envelope(
                    path,
                    session_id,
                    event_type,
                    column,
                    value,
                )
                self._assert_approval_recovery_rejected(
                    path,
                    plan,
                    "approval event envelope is inconsistent",
                )

    async def test_session_recovery_enforces_the_approval_causal_chain(self):
        cases = (
            ("missing-request", False, "approval.requested", "no request"),
            ("orphan-decision", True, "approval.requested", "no prior request"),
            ("missing-decision", True, "approval.decided", "event envelope"),
        )
        for index, (label, decided, deleted_type, message) in enumerate(cases):
            with self.subTest(label=label):
                path = str(Path(self.tempdir.name) / f"approval-chain-{index}.sqlite3")
                session_id = f"approval-chain-{index}"
                plan = await self._seed_approval_history(
                    path,
                    session_id,
                    decided=decided,
                )
                store = SQLiteEventStore(path)
                try:
                    store._connection.execute(
                        """
                        UPDATE events SET event_type = ?
                        WHERE stream_id = ? AND event_type = ?
                        """,
                        (f"test.removed.{index}", f"session:{session_id}", deleted_type),
                    )
                finally:
                    store.close()
                self._assert_approval_recovery_rejected(path, plan, message)

        path = str(Path(self.tempdir.name) / "approval-chain-missing-transition.sqlite3")
        session_id = "approval-chain-missing-transition"
        plan = await self._seed_approval_history(path, session_id, decided=True)
        store = SQLiteEventStore(path)
        try:
            rows = store._connection.execute(
                """
                SELECT global_position, payload_json
                FROM events
                WHERE stream_id = ? AND event_type = 'task.status.changed'
                ORDER BY sequence
                """,
                (f"session:{session_id}",),
            ).fetchall()
            final_transition = next(
                row
                for row in rows
                if json.loads(row["payload_json"])["previous"] == "waiting_approval"
            )
            store._connection.execute(
                "UPDATE events SET event_type = ? WHERE global_position = ?",
                ("test.removed.transition", final_transition["global_position"]),
            )
        finally:
            store.close()
        self._assert_approval_recovery_rejected(
            path,
            plan,
            "no matching task transition",
        )

        path = str(Path(self.tempdir.name) / "approval-chain-wrong-transition.sqlite3")
        session_id = "approval-chain-wrong-transition"
        plan = await self._seed_approval_history(path, session_id, decided=True)
        store = SQLiteEventStore(path)
        try:
            rows = store._connection.execute(
                """
                SELECT global_position, payload_json
                FROM events
                WHERE stream_id = ? AND event_type = 'task.status.changed'
                ORDER BY sequence
                """,
                (f"session:{session_id}",),
            ).fetchall()
            final_transition = next(
                row
                for row in rows
                if json.loads(row["payload_json"])["previous"] == "waiting_approval"
            )
            payload = json.loads(final_transition["payload_json"])
            payload["current"] = "canceled"
            store._connection.execute(
                "UPDATE events SET payload_json = ? WHERE global_position = ?",
                (
                    json.dumps(payload, sort_keys=True, separators=(",", ":")),
                    final_transition["global_position"],
                ),
            )
        finally:
            store.close()
        self._assert_approval_recovery_rejected(
            path,
            plan,
            "does not match its task transition",
        )

    async def test_session_recovery_rejects_unbound_legacy_decision_correlation(self):
        session_id = "approval-legacy-correlation"
        plan = WorkflowPlan(
            session_id,
            "兼容旧审批历史",
            "user",
            self._approval_plan(session_id).tasks,
            plan_id=f"plan-{session_id}",
        )
        kernel = OrchestratorKernel(event_store=SQLiteEventStore(self.path))
        paused = await kernel.run(plan)
        await kernel.decide(
            paused.needs_you[0].request_id,
            ApprovalDecision.APPROVE,
            "owner",
        )
        kernel.event_store._connection.execute(
            """
            UPDATE events SET correlation_id = NULL
            WHERE stream_id = ? AND event_type = 'approval.decided'
            """,
            (f"session:{session_id}",),
        )
        kernel.event_store.close()

        self._assert_approval_recovery_rejected(
            self.path,
            plan,
            "approval event envelope is inconsistent",
        )

    async def test_session_recovery_rejects_incomplete_approval_tail_writes(self):
        request_path = str(Path(self.tempdir.name) / "repair-request.sqlite3")
        request_plan = await self._seed_approval_history(
            request_path,
            "repair-request",
            decided=False,
        )
        request_store = SQLiteEventStore(request_path)
        request_row = request_store._connection.execute(
            """
            SELECT global_position, sequence FROM events
            WHERE stream_id = ? AND event_type = 'approval.requested'
            """,
            ("session:repair-request",),
        ).fetchone()
        self.assertIsNotNone(request_row)
        request_store._connection.execute(
            "DELETE FROM events WHERE global_position = ?",
            (request_row["global_position"],),
        )
        request_store.close()

        self._assert_approval_recovery_rejected(
            request_path,
            request_plan,
            "waiting transition has no request",
        )
        request_check = SQLiteEventStore(request_path)
        self.assertEqual(
            request_check.stream_version("session:repair-request"),
            request_row["sequence"] - 1,
        )
        request_check.close()

        transition_path = str(Path(self.tempdir.name) / "repair-transition.sqlite3")
        transition_plan = await self._seed_approval_history(
            transition_path,
            "repair-transition",
            decided=True,
        )
        transition_store = SQLiteEventStore(transition_path)
        transition_row = transition_store._connection.execute(
            """
            SELECT global_position, sequence FROM events
            WHERE stream_id = ? AND event_type = 'task.status.changed'
            ORDER BY sequence DESC LIMIT 1
            """,
            ("session:repair-transition",),
        ).fetchone()
        self.assertIsNotNone(transition_row)
        transition_store._connection.execute(
            "DELETE FROM events WHERE global_position = ?",
            (transition_row["global_position"],),
        )
        transition_store.close()

        self._assert_approval_recovery_rejected(
            transition_path,
            transition_plan,
            "decision has no matching task transition",
        )
        transition_check = SQLiteEventStore(transition_path)
        self.assertEqual(
            transition_check.stream_version("session:repair-transition"),
            transition_row["sequence"] - 1,
        )
        transition_check.close()

    async def test_session_recovery_binds_approval_transition_envelopes(self):
        cases = (
            ("actor_id", "intruder"),
            ("correlation_id", "other-correlation"),
            ("causation_id", "other-request"),
            ("idempotency_key", "task-status:other:4"),
        )
        for index, (column, value) in enumerate(cases):
            with self.subTest(column=column):
                path = str(Path(self.tempdir.name) / f"approval-transition-{index}.sqlite3")
                session_id = f"approval-transition-{index}"
                plan = await self._seed_approval_history(path, session_id, decided=True)
                store = SQLiteEventStore(path)
                store._connection.execute(
                    f"""
                    UPDATE events SET {column} = ?
                    WHERE global_position = (
                        SELECT global_position FROM events
                        WHERE stream_id = ? AND event_type = 'task.status.changed'
                        ORDER BY sequence DESC LIMIT 1
                    )
                    """,
                    (value, f"session:{session_id}"),
                )
                store.close()
                self._assert_approval_recovery_rejected(
                    path,
                    plan,
                    "task transition event envelope is inconsistent",
                )

        path = str(Path(self.tempdir.name) / "approval-transition-paired-null.sqlite3")
        session_id = "approval-transition-paired-null"
        plan = await self._seed_approval_history(path, session_id, decided=True)
        store = SQLiteEventStore(path)
        store._connection.execute(
            """
            UPDATE events SET correlation_id = NULL, causation_id = NULL
            WHERE global_position = (
                SELECT global_position FROM events
                WHERE stream_id = ? AND event_type = 'task.status.changed'
                ORDER BY sequence DESC LIMIT 1
            )
            """,
            (f"session:{session_id}",),
        )
        store.close()
        self._assert_approval_recovery_rejected(
            path,
            plan,
            "task transition event envelope is inconsistent",
        )

    async def test_session_recovery_uses_bounded_contiguous_pages(self):
        calls = 0

        async def handler(invocation):
            nonlocal calls
            calls += 1
            return AgentResult("done")

        plan = WorkflowPlan(
            "recover-pages",
            "分页恢复",
            "user",
            (TaskSpec("task", "worker", handoff(), task_id="task"),),
            plan_id="plan-pages",
        )
        first = OrchestratorKernel(event_store=SQLiteEventStore(self.path))
        first.register_agent(registration("worker", handler))
        self.assertTrue((await first.run(plan)).completed)
        first.event_store.close()

        second = OrchestratorKernel(event_store=SQLiteEventStore(self.path))
        second.register_agent(registration("worker", handler))
        original_page_reader = second.event_store.read_stream_page
        with patch.object(runtime_module, "_RECOVERY_PAGE_LIMIT", 2):
            with patch.object(
                second.event_store,
                "read_stream",
                side_effect=AssertionError("unbounded recovery read is forbidden"),
            ):
                with patch.object(
                    second.event_store,
                    "read_stream_page",
                    wraps=original_page_reader,
                ) as page_reader:
                    recovered = await second.run(plan)

        self.assertTrue(recovered.completed)
        self.assertEqual(calls, 1)
        calls_by_cursor = [
            (call.kwargs["after_sequence"], call.kwargs["limit"])
            for call in page_reader.call_args_list
        ]
        self.assertGreater(len(calls_by_cursor), 1)
        self.assertEqual(calls_by_cursor[0], (0, 2))
        self.assertTrue(all(limit <= 2 for _cursor, limit in calls_by_cursor))
        self.assertEqual(
            [cursor for cursor, _limit in calls_by_cursor],
            sorted({cursor for cursor, _limit in calls_by_cursor}),
        )
        second.event_store.close()

    async def test_session_recovery_streams_pages_into_candidate_replay(self):
        async def handler(invocation):
            return AgentResult("done")

        plan = WorkflowPlan(
            "recover-streaming",
            "流式恢复",
            "user",
            (TaskSpec("task", "worker", handoff(), task_id="task"),),
            plan_id="plan-streaming",
        )
        first = OrchestratorKernel(event_store=SQLiteEventStore(self.path))
        first.register_agent(registration("worker", handler))
        self.assertTrue((await first.run(plan)).completed)
        first.event_store.close()

        second = OrchestratorKernel(event_store=SQLiteEventStore(self.path))
        second.register_agent(registration("worker", handler))
        operations = []
        original_page_reader = second.event_store.read_stream_page
        original_transition = OrchestratorKernel._apply_recovered_transition

        def read_page(*args, **kwargs):
            operations.append(("read", kwargs["after_sequence"]))
            return original_page_reader(*args, **kwargs)

        def apply_transition(*args, **kwargs):
            operations.append(("apply", args[1].sequence))
            return original_transition(*args, **kwargs)

        with patch.object(runtime_module, "_RECOVERY_PAGE_LIMIT", 2):
            with patch.object(second.event_store, "read_stream_page", side_effect=read_page):
                with patch.object(
                    OrchestratorKernel,
                    "_apply_recovered_transition",
                    side_effect=apply_transition,
                ):
                    recovered = await second.run(plan)

        self.assertTrue(recovered.completed)
        first_apply = next(index for index, item in enumerate(operations) if item[0] == "apply")
        last_read = max(index for index, item in enumerate(operations) if item[0] == "read")
        self.assertLess(first_apply, last_read)
        second.event_store.close()

    async def test_session_recovery_probes_exact_limit_and_rejects_overflow(self):
        async def handler(invocation):
            return AgentResult("done")

        plan = WorkflowPlan(
            "recover-limit",
            "恢复上限",
            "user",
            (TaskSpec("task", "worker", handoff(), task_id="task"),),
            plan_id="plan-limit",
        )
        first = OrchestratorKernel(event_store=SQLiteEventStore(self.path))
        first.register_agent(registration("worker", handler))
        self.assertTrue((await first.run(plan)).completed)
        event_count = len(first.event_store.read_stream("session:recover-limit"))
        first.event_store.close()

        exact = OrchestratorKernel(event_store=SQLiteEventStore(self.path))
        exact.register_agent(registration("worker", handler))
        with patch.object(runtime_module, "_RECOVERY_PAGE_LIMIT", 2):
            with patch.object(runtime_module, "_MAX_RECOVERY_EVENTS", event_count):
                self.assertTrue((await exact.run(plan)).completed)
        exact.event_store.append(
            DomainEvent(
                stream_id="session:recover-limit",
                event_type="test.extra",
                actor_id="test",
                payload={},
            )
        )
        exact.event_store.close()

        overflow = OrchestratorKernel(event_store=SQLiteEventStore(self.path))
        overflow.register_agent(registration("worker", handler))
        with patch.object(runtime_module, "_RECOVERY_PAGE_LIMIT", 2):
            with patch.object(runtime_module, "_MAX_RECOVERY_EVENTS", event_count):
                with self.assertRaisesRegex(
                    SessionRecoveryError,
                    f"{event_count}-event safety limit",
                ):
                    await overflow.run(plan)
        self.assertNotIn("recover-limit", overflow._plans)
        self.assertIs(quantum_entanglement.SessionRecoveryError, SessionRecoveryError)
        overflow.event_store.close()

    async def test_session_recovery_enforces_cumulative_byte_and_node_budgets(self):
        async def handler(invocation):
            return AgentResult("done")

        plan = WorkflowPlan(
            "recover-budget",
            "恢复预算",
            "user",
            (TaskSpec("task", "worker", handoff(), task_id="task"),),
            plan_id="plan-budget",
        )
        first = OrchestratorKernel(event_store=SQLiteEventStore(self.path))
        first.register_agent(registration("worker", handler))
        self.assertTrue((await first.run(plan)).completed)
        persisted = first.event_store.read_stream("session:recover-budget")
        measurements = tuple(first._measure_recovery_event(stored) for stored in persisted)
        required_bytes = sum(item[0] for item in measurements)
        required_nodes = sum(item[1] for item in measurements)
        first.event_store.close()

        exact = OrchestratorKernel(event_store=SQLiteEventStore(self.path))
        exact.register_agent(registration("worker", handler))
        with patch.object(runtime_module, "_MAX_RECOVERY_BYTES", required_bytes):
            with patch.object(runtime_module, "_MAX_RECOVERY_JSON_NODES", required_nodes):
                self.assertTrue((await exact.run(plan)).completed)
        exact.event_store.close()

        byte_overflow = OrchestratorKernel(event_store=SQLiteEventStore(self.path))
        byte_overflow.register_agent(registration("worker", handler))
        with patch.object(runtime_module, "_MAX_RECOVERY_BYTES", required_bytes - 1):
            with self.assertRaisesRegex(SessionRecoveryError, "byte safety limit"):
                await byte_overflow.run(plan)
        self.assertNotIn(plan.session_id, byte_overflow._plans)
        self.assertNotIn(plan.session_id, byte_overflow._graphs)
        byte_overflow.event_store.close()

        node_overflow = OrchestratorKernel(event_store=SQLiteEventStore(self.path))
        node_overflow.register_agent(registration("worker", handler))
        with patch.object(runtime_module, "_MAX_RECOVERY_JSON_NODES", required_nodes - 1):
            with self.assertRaisesRegex(SessionRecoveryError, "JSON-node safety limit"):
                await node_overflow.run(plan)
        self.assertNotIn(plan.session_id, node_overflow._plans)
        self.assertNotIn(plan.session_id, node_overflow._graphs)
        node_overflow.event_store.close()

    async def test_session_recovery_rejects_a_late_sequence_gap_without_partial_state(self):
        async def handler(invocation):
            return AgentResult("done")

        plan = WorkflowPlan(
            "recover-gap",
            "拒绝缺口",
            "user",
            (TaskSpec("task", "worker", handoff(), task_id="task"),),
            plan_id="plan-gap",
        )
        first = OrchestratorKernel(event_store=SQLiteEventStore(self.path))
        first.register_agent(registration("worker", handler))
        self.assertTrue((await first.run(plan)).completed)
        first.event_store._connection.execute(
            "DELETE FROM events WHERE stream_id = ? AND sequence = ?",
            ("session:recover-gap", 3),
        )
        first.event_store.close()

        recovered = OrchestratorKernel(event_store=SQLiteEventStore(self.path))
        recovered.register_agent(registration("worker", handler))
        with patch.object(runtime_module, "_RECOVERY_PAGE_LIMIT", 2):
            with self.assertRaisesRegex(SessionRecoveryError, "not contiguous"):
                await recovered.run(plan)
        self.assertNotIn("recover-gap", recovered._plans)
        self.assertNotIn("recover-gap", recovered._graphs)
        recovered.event_store.close()

    async def test_session_recovery_rejects_invalid_task_transition_contracts(self):
        cases = (
            ("bool-revision", "revision", True, "revision is invalid"),
            ("float-revision", "revision", 1.0, "revision is invalid"),
            ("revision-gap", "revision", 2, "revision is not contiguous"),
            ("previous-drift", "previous", "ready", "previous status"),
            ("invalid-edge", "current", "completed", "not permitted"),
            ("unknown-task", "taskId", "unknown", "unknown state"),
            ("numeric-reason", "reason", 7, "reason is invalid"),
            ("bool-status", "current", True, "status is invalid"),
        )

        async def handler(invocation):
            return AgentResult("done")

        for index, (label, field, value, message) in enumerate(cases):
            with self.subTest(label=label):
                path = str(Path(self.tempdir.name) / f"transition-{index}.sqlite3")
                session_id = f"transition-{index}"
                plan = WorkflowPlan(
                    session_id,
                    "严格恢复",
                    "user",
                    (TaskSpec("task", "worker", handoff(), task_id="task"),),
                    plan_id=f"plan-transition-{index}",
                )
                first = OrchestratorKernel(event_store=SQLiteEventStore(path))
                first.register_agent(registration("worker", handler))
                self.assertTrue((await first.run(plan)).completed)
                row = first.event_store._connection.execute(
                    """
                    SELECT global_position, payload_json
                    FROM events
                    WHERE stream_id = ? AND event_type = 'task.status.changed'
                    ORDER BY sequence LIMIT 1
                    """,
                    (f"session:{session_id}",),
                ).fetchone()
                self.assertIsNotNone(row)
                payload = json.loads(row["payload_json"])
                payload[field] = value
                first.event_store._connection.execute(
                    "UPDATE events SET payload_json = ? WHERE global_position = ?",
                    (
                        json.dumps(payload, sort_keys=True, separators=(",", ":")),
                        row["global_position"],
                    ),
                )
                first.event_store.close()

                candidate = OrchestratorKernel(event_store=SQLiteEventStore(path))
                candidate.register_agent(registration("worker", handler))
                with self.assertRaisesRegex(SessionRecoveryError, message):
                    await candidate.run(plan)
                self.assertNotIn(session_id, candidate._plans)
                self.assertNotIn(session_id, candidate._graphs)
                candidate.event_store.close()

    async def test_late_invalid_transition_shape_never_publishes_partial_recovery(self):
        async def handler(invocation):
            return AgentResult("done")

        plan = WorkflowPlan(
            "transition-late",
            "晚页损坏",
            "user",
            (TaskSpec("task", "worker", handoff(), task_id="task"),),
            plan_id="plan-transition-late",
        )
        first = OrchestratorKernel(event_store=SQLiteEventStore(self.path))
        first.register_agent(registration("worker", handler))
        self.assertTrue((await first.run(plan)).completed)
        row = first.event_store._connection.execute(
            """
            SELECT global_position, payload_json
            FROM events
            WHERE stream_id = ? AND event_type = 'task.status.changed'
            ORDER BY sequence DESC LIMIT 1
            """,
            ("session:transition-late",),
        ).fetchone()
        self.assertIsNotNone(row)
        payload = json.loads(row["payload_json"])
        payload["unexpected"] = "field"
        first.event_store._connection.execute(
            "UPDATE events SET payload_json = ? WHERE global_position = ?",
            (
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                row["global_position"],
            ),
        )
        first.event_store.close()

        candidate = OrchestratorKernel(event_store=SQLiteEventStore(self.path))
        candidate.register_agent(registration("worker", handler))
        with self.assertRaisesRegex(SessionRecoveryError, "invalid shape"):
            await candidate.run(plan)
        self.assertNotIn("transition-late", candidate._plans)
        self.assertNotIn("transition-late", candidate._graphs)
        self.assertEqual(candidate.approvals.pending("transition-late"), ())
        candidate.event_store.close()

    async def test_session_recovery_rejects_noncanonical_plan_payloads(self):
        cases = (
            ("coerced-session", lambda payload: payload.__setitem__("sessionId", True)),
            ("extra-key", lambda payload: payload.__setitem__("unexpected", None)),
            ("missing-key", lambda payload: payload.pop("correlationId")),
            (
                "coerced-priority",
                lambda payload: payload["tasks"][0].__setitem__("priority", True),
            ),
            (
                "coerced-authority",
                lambda payload: payload["tasks"][0]["handoff"]["authority"].__setitem__(
                    "externalSideEffects", "false"
                ),
            ),
        )

        async def handler(invocation):
            return AgentResult("done")

        for index, (label, mutate) in enumerate(cases):
            with self.subTest(label=label):
                path = str(Path(self.tempdir.name) / f"plan-payload-{index}.sqlite3")
                session_id = f"plan-payload-{index}"
                plan = WorkflowPlan(
                    session_id,
                    "严格计划",
                    "user",
                    (TaskSpec("task", "worker", handoff(), task_id="task"),),
                    plan_id=f"plan-payload-{index}",
                )
                first = OrchestratorKernel(event_store=SQLiteEventStore(path))
                first.register_agent(registration("worker", handler))
                self.assertTrue((await first.run(plan)).completed)
                row = first.event_store._connection.execute(
                    """
                    SELECT global_position, payload_json
                    FROM events
                    WHERE stream_id = ? AND event_type = 'workflow.plan.created'
                    """,
                    (f"session:{session_id}",),
                ).fetchone()
                self.assertIsNotNone(row)
                payload = json.loads(row["payload_json"])
                mutate(payload)
                first.event_store._connection.execute(
                    "UPDATE events SET payload_json = ? WHERE global_position = ?",
                    (
                        json.dumps(payload, sort_keys=True, separators=(",", ":")),
                        row["global_position"],
                    ),
                )
                first.event_store.close()

                candidate = OrchestratorKernel(event_store=SQLiteEventStore(path))
                candidate.register_agent(registration("worker", handler))
                with self.assertRaisesRegex(SessionRecoveryError, "plan payload"):
                    await candidate.run(plan)
                self.assertNotIn(session_id, candidate._plans)
                candidate.event_store.close()

    async def test_session_recovery_binds_one_plan_to_its_event_envelope(self):
        async def handler(invocation):
            return AgentResult("done")

        envelope_cases = (
            ("actor_id", "other"),
            ("idempotency_key", "plan:other"),
            ("correlation_id", "correlation-other"),
            ("causation_id", "unexpected-cause"),
        )
        for index, (column, value) in enumerate(envelope_cases):
            with self.subTest(column=column):
                path = str(Path(self.tempdir.name) / f"plan-envelope-{index}.sqlite3")
                session_id = f"plan-envelope-{index}"
                plan = WorkflowPlan(
                    session_id,
                    "事件绑定",
                    "user",
                    (TaskSpec("task", "worker", handoff(), task_id="task"),),
                    plan_id=f"plan-envelope-{index}",
                )
                first = OrchestratorKernel(event_store=SQLiteEventStore(path))
                first.register_agent(registration("worker", handler))
                self.assertTrue((await first.run(plan)).completed)
                first.event_store._connection.execute(
                    f"""
                    UPDATE events SET {column} = ?
                    WHERE stream_id = ? AND event_type = 'workflow.plan.created'
                    """,
                    (value, f"session:{session_id}"),
                )
                first.event_store.close()

                candidate = OrchestratorKernel(event_store=SQLiteEventStore(path))
                candidate.register_agent(registration("worker", handler))
                with self.assertRaisesRegex(SessionRecoveryError, "event envelope"):
                    await candidate.run(plan)
                self.assertNotIn(session_id, candidate._plans)
                candidate.event_store.close()

        duplicate_path = str(Path(self.tempdir.name) / "plan-duplicate.sqlite3")
        duplicate_plan = WorkflowPlan(
            "plan-duplicate",
            "唯一计划",
            "user",
            (TaskSpec("task", "worker", handoff(), task_id="task"),),
            plan_id="plan-original",
        )
        seeded = OrchestratorKernel(event_store=SQLiteEventStore(duplicate_path))
        seeded.register_agent(registration("worker", handler))
        self.assertTrue((await seeded.run(duplicate_plan)).completed)
        seeded.event_store.append(
            DomainEvent(
                stream_id="session:plan-duplicate",
                event_type="workflow.plan.created",
                actor_id="user",
                correlation_id="plan-original",
                idempotency_key="plan:duplicate",
                payload=duplicate_plan.to_dict(),
            )
        )
        seeded.event_store.close()

        duplicate = OrchestratorKernel(event_store=SQLiteEventStore(duplicate_path))
        duplicate.register_agent(registration("worker", handler))
        with self.assertRaisesRegex(SessionRecoveryError, "multiple workflow plan"):
            await duplicate.run(duplicate_plan)
        self.assertNotIn("plan-duplicate", duplicate._plans)
        duplicate.event_store.close()

    async def test_session_recovery_requires_an_exact_task_creation_manifest(self):
        cases = (
            ("missing", "exactly match"),
            ("payload-coercion", "not canonical"),
            ("envelope", "event envelope"),
            ("unknown", "unknown task"),
            ("overlap", "overlap task transition"),
        )

        async def handler(invocation):
            return AgentResult("done")

        for index, (label, message) in enumerate(cases):
            with self.subTest(label=label):
                path = str(Path(self.tempdir.name) / f"task-manifest-{index}.sqlite3")
                session_id = f"task-manifest-{index}"
                stream_id = f"session:{session_id}"
                plan = WorkflowPlan(
                    session_id,
                    "任务清单",
                    "user",
                    (TaskSpec("task", "worker", handoff(), task_id="task"),),
                    plan_id=f"plan-task-manifest-{index}",
                )
                first = OrchestratorKernel(event_store=SQLiteEventStore(path))
                first.register_agent(registration("worker", handler))
                self.assertTrue((await first.run(plan)).completed)
                connection = first.event_store._connection
                task_row = connection.execute(
                    """
                    SELECT sequence, global_position, payload_json
                    FROM events
                    WHERE stream_id = ? AND event_type = 'task.created'
                    """,
                    (stream_id,),
                ).fetchone()
                self.assertIsNotNone(task_row)

                if label == "missing":
                    connection.execute(
                        "DELETE FROM events WHERE global_position = ?",
                        (task_row["global_position"],),
                    )
                    connection.execute(
                        """
                        UPDATE events SET sequence = sequence + 1000
                        WHERE stream_id = ? AND sequence > ?
                        """,
                        (stream_id, task_row["sequence"]),
                    )
                    connection.execute(
                        """
                        UPDATE events SET sequence = sequence - 1001
                        WHERE stream_id = ? AND sequence > ?
                        """,
                        (stream_id, task_row["sequence"] + 1000),
                    )
                elif label == "payload-coercion":
                    payload = json.loads(task_row["payload_json"])
                    payload["priority"] = True
                    connection.execute(
                        "UPDATE events SET payload_json = ? WHERE global_position = ?",
                        (
                            json.dumps(payload, sort_keys=True, separators=(",", ":")),
                            task_row["global_position"],
                        ),
                    )
                elif label == "envelope":
                    connection.execute(
                        "UPDATE events SET actor_id = 'other' WHERE global_position = ?",
                        (task_row["global_position"],),
                    )
                elif label == "unknown":
                    payload = json.loads(task_row["payload_json"])
                    payload["taskId"] = "unknown"
                    connection.execute(
                        "UPDATE events SET payload_json = ? WHERE global_position = ?",
                        (
                            json.dumps(payload, sort_keys=True, separators=(",", ":")),
                            task_row["global_position"],
                        ),
                    )
                else:
                    transition_row = connection.execute(
                        """
                        SELECT sequence, global_position
                        FROM events
                        WHERE stream_id = ? AND event_type = 'task.status.changed'
                        ORDER BY sequence LIMIT 1
                        """,
                        (stream_id,),
                    ).fetchone()
                    self.assertIsNotNone(transition_row)
                    connection.execute(
                        "UPDATE events SET sequence = 1000 WHERE global_position = ?",
                        (task_row["global_position"],),
                    )
                    connection.execute(
                        "UPDATE events SET sequence = ? WHERE global_position = ?",
                        (task_row["sequence"], transition_row["global_position"]),
                    )
                    connection.execute(
                        "UPDATE events SET sequence = ? WHERE global_position = ?",
                        (transition_row["sequence"], task_row["global_position"]),
                    )
                first.event_store.close()

                candidate = OrchestratorKernel(event_store=SQLiteEventStore(path))
                candidate.register_agent(registration("worker", handler))
                with self.assertRaisesRegex(SessionRecoveryError, message):
                    await candidate.run(plan)
                self.assertNotIn(session_id, candidate._plans)
                self.assertNotIn(session_id, candidate._graphs)
                candidate.event_store.close()


if __name__ == "__main__":
    unittest.main()
