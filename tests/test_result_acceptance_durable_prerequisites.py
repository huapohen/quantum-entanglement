from __future__ import annotations

import copy
import hashlib
import pickle
import sqlite3
import traceback
import unittest
from contextlib import ExitStack
from dataclasses import replace
from unittest.mock import patch

import quantum_entanglement
import quantum_entanglement._result_artifact_transaction as result_artifact_transaction_module
from quantum_entanglement._result_acceptance import (
    _RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN,
    _build_scoped_invocation_result_events_from_plan_v2,
    _build_scoped_invocation_result_terminal_transition_from_plan_v2,
    _EventedFreshResultAcceptancePlanV2,
    _EvidencedFreshResultAcceptancePlanV2,
    _ExistingResultAcceptanceGraphCandidateV2,
    _FreshResultAcceptancePrerequisitesV2,
    _FreshResultAcceptanceWritePlanV2,
    _IdentifiedFreshResultAcceptancePlanV2,
    _MaterializedFreshResultAcceptancePlanV2,
    _prepare_scoped_invocation_result_acceptance_v2,
    _PreparedScopedInvocationResultAcceptanceV2,
    _ResultAcceptanceConflictError,
    _ResultAcceptanceIntegrityError,
    _ResultAcceptanceQuarantineCategory,
    _ResultAcceptanceQuarantineError,
    _ResultAcceptanceSchemaUnavailableError,
    _TransitionedFreshResultAcceptancePlanV2,
)
from quantum_entanglement._result_artifact_transaction import (
    _ResultArtifactConflictError,
    _ResultArtifactTransactionContinuityError,
)
from quantum_entanglement.attempts import InvocationLease
from quantum_entanglement.events import DomainEvent
from quantum_entanglement.invocation_execution import (
    CANONICAL_ORCHESTRATOR_ACTOR_ID,
    EffectClass,
    ScopedInvocationStartClaimedV3,
)
from quantum_entanglement.invocation_results import (
    EMPTY_ACTION_RECEIPT_SET_DIGEST,
    SCOPED_INVOCATION_RESULT_ACCEPTANCE_REQUEST_SCHEMA_VERSION,
    SCOPED_INVOCATION_RESULT_EVIDENCE_SCHEMA_VERSION,
    SCOPED_INVOCATION_RESULT_MANIFEST_SCHEMA_VERSION,
    TASK_INVOCATION_RESULT_ACCEPTED_EVENT_TYPE,
    TASK_STATUS_CHANGED_EVENT_TYPE,
    ScopedInvocationResultAcceptanceRequestV2,
    ScopedInvocationResultArtifactCandidateV2,
    ScopedInvocationResultEvidenceV2,
    ScopedInvocationResultManifestV2,
    ScopedInvocationResultReceiptV2,
    ScopedInvocationResultTerminalTransitionV2,
)
from quantum_entanglement.protocol import TaskStatus
from quantum_entanglement.store import (
    SQLiteEventStore,
    _CompletedFreshResultAcceptancePlanV2,
    _InsertedFreshResultAcceptancePlanV2,
    _PersistedFreshResultAcceptancePlanV2,
    _ReceiptedFreshResultAcceptancePlanV2,
)
from tests import test_inactive_invocation_results_migration as inactive_migration_module
from tests.test_scoped_task_invocation_admission import (
    CLAIMED_AT,
    STORE_TIME,
    scoped_request,
)


def install_inactive_result_schema(store: SQLiteEventStore) -> None:
    helper = inactive_migration_module.InactiveInvocationResultsMigrationTests(methodName="runTest")
    helper.store = store
    helper.connection = store._connection
    helper.apply_candidate()


def result_request_for_claim(
    claimed: ScopedInvocationStartClaimedV3,
) -> ScopedInvocationResultAcceptanceRequestV2:
    evidence = claimed.receipt.evidence
    candidate = ScopedInvocationResultArtifactCandidateV2.from_content_metadata(
        tenant_id=evidence.tenant_id,
        workspace_id=evidence.workspace_id,
        session_id=evidence.session_id,
        task_id=evidence.task_id,
        artifact_id="artifact-result-prerequisite-1",
        name="result.md",
        media_type="text/markdown",
        content=b"durable prerequisite result",
        metadata={"source": "test"},
        created_by=evidence.agent_id,
        idempotency_key="artifact-result-prerequisite:1",
        expected_head_version=0,
    )
    manifest = ScopedInvocationResultManifestV2(
        schema_version=SCOPED_INVOCATION_RESULT_MANIFEST_SCHEMA_VERSION,
        tenant_id=evidence.tenant_id,
        workspace_id=evidence.workspace_id,
        invocation_id=evidence.invocation_id,
        session_id=evidence.session_id,
        plan_id=evidence.plan_id,
        task_id=evidence.task_id,
        agent_id=evidence.agent_id,
        job_idempotency_key=evidence.job_idempotency_key,
        task_revision=5,
        correlation_id=evidence.correlation_id,
        causation_id=evidence.causation_id,
        runtime_revision=evidence.runtime_revision,
        execution_manifest_digest=evidence.manifest_digest,
        effect_class=EffectClass.PURE,
        action_receipt_set_digest=EMPTY_ACTION_RECEIPT_SET_DIGEST,
        result_ref="result:durable-prerequisite-1",
        narration="durable result",
        metadata={"provider": "fake"},
        primary_artifact_id=candidate.artifact_id,
        artifacts=(candidate.to_descriptor(),),
    )
    return ScopedInvocationResultAcceptanceRequestV2(
        schema_version=SCOPED_INVOCATION_RESULT_ACCEPTANCE_REQUEST_SCHEMA_VERSION,
        acceptance_idempotency_key="accept:durable-prerequisite-1",
        start_receipt=claimed.receipt,
        manifest=manifest,
        artifact_candidates=(candidate,),
        expected_stream_version=claimed.receipt.sequence,
    )


def existing_graph_prepared(
    helper: inactive_migration_module.InactiveInvocationResultsMigrationTests,
) -> _PreparedScopedInvocationResultAcceptanceV2:
    graph = helper.exact_result_graph()
    request = graph["request"]
    assert type(request) is ScopedInvocationResultAcceptanceRequestV2
    lease_token = "existing-result-lease-token-canary"
    evidence = replace(
        request.start_receipt.evidence,
        lease_token_digest=hashlib.sha256(lease_token.encode("utf-8")).hexdigest(),
    )
    receipt = replace(request.start_receipt, evidence=evidence)
    exact_request = replace(request, start_receipt=receipt)
    lease = InvocationLease(
        invocation_id=evidence.invocation_id,
        session_id=evidence.session_id,
        plan_id=evidence.plan_id,
        task_id=evidence.task_id,
        agent_id=evidence.agent_id,
        idempotency_key=evidence.job_idempotency_key,
        payload_digest=evidence.manifest_digest,
        attempt_id=evidence.attempt_id,
        attempt_number=evidence.attempt_number,
        max_attempts=1,
        lease_epoch=evidence.lease_epoch,
        worker_id=evidence.worker_id,
        lease_token=lease_token,
        claimed_at=evidence.claimed_at,
        lease_expires_at=evidence.lease_expires_at,
    )
    return _prepare_scoped_invocation_result_acceptance_v2(
        exact_request,
        ScopedInvocationStartClaimedV3(receipt, lease),
    )


class ResultAcceptanceDurablePrerequisiteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = SQLiteEventStore(":memory:", clock=lambda: STORE_TIME)

    def tearDown(self) -> None:
        self.store.close()

    def fresh_prepared(self) -> _PreparedScopedInvocationResultAcceptanceV2:
        admission = scoped_request()
        self.store.append_scoped_task_invocation_admission_v2(
            admission,
            expected_version=0,
        )
        self.store._clock = lambda: CLAIMED_AT
        claimed = self.store.claim_scoped_invocation_start_v3(
            admission.manifest.tenant_id,
            admission.manifest.workspace_id,
            admission.manifest.invocation_id,
            "worker-scoped-store-1",
            lease_seconds=60,
            expected_version=2,
        )
        self.assertIs(type(claimed), ScopedInvocationStartClaimedV3)
        assert type(claimed) is ScopedInvocationStartClaimedV3
        return _prepare_scoped_invocation_result_acceptance_v2(
            result_request_for_claim(claimed),
            claimed,
        )

    def narration_only_prepared(self) -> _PreparedScopedInvocationResultAcceptanceV2:
        prepared = self.fresh_prepared()
        request = replace(
            prepared.request,
            manifest=replace(
                prepared.request.manifest,
                primary_artifact_id=None,
                artifacts=(),
            ),
            artifact_candidates=(),
        )
        return _prepare_scoped_invocation_result_acceptance_v2(
            request,
            prepared.claimed,
        )

    def validate(
        self,
        prepared: _PreparedScopedInvocationResultAcceptanceV2,
    ) -> _ExistingResultAcceptanceGraphCandidateV2 | _FreshResultAcceptancePrerequisitesV2:
        with self.store._transaction() as connection:
            return self.store._validate_result_acceptance_durable_prerequisites_in_transaction(
                connection,
                prepared,
            )

    def test_fresh_prerequisites_bind_exact_durable_start_without_clock_or_id(self) -> None:
        prepared = self.fresh_prepared()
        install_inactive_result_schema(self.store)
        lease_token = prepared.claimed.lease.lease_token
        self.store._clock = lambda: (_ for _ in ()).throw(AssertionError("clock must not be read"))
        with patch(
            "quantum_entanglement.protocol.new_id",
            side_effect=AssertionError("ID provider must not be read"),
        ) as new_id:
            result = self.validate(prepared)

        self.assertIs(type(result), _FreshResultAcceptancePrerequisitesV2)
        self.assertNotIn(lease_token, repr(result))
        assert type(result) is _FreshResultAcceptancePrerequisitesV2
        self.assertEqual(result.expected_stream_version, 3)
        self.assertEqual(result.running_task_revision, 5)
        new_id.assert_not_called()

    def test_missing_or_temp_shadowed_schema_fails_before_start_or_lease_read(self) -> None:
        prepared = self.fresh_prepared()
        with patch.object(
            SQLiteEventStore,
            "_load_scoped_invocation_start_in_transaction",
            side_effect=AssertionError("fresh start must not be inspected"),
        ):
            with self.assertRaises(_ResultAcceptanceSchemaUnavailableError):
                self.validate(prepared)

        install_inactive_result_schema(self.store)
        self.store._connection.execute("CREATE TEMP TABLE invocation_result_receipts(value TEXT)")
        with patch.object(
            SQLiteEventStore,
            "_load_scoped_invocation_start_in_transaction",
            side_effect=AssertionError("fresh start must not be inspected"),
        ):
            with self.assertRaises(_ResultAcceptanceSchemaUnavailableError):
                self.validate(prepared)

    def test_stale_durable_lease_fails_closed(self) -> None:
        prepared = self.fresh_prepared()
        install_inactive_result_schema(self.store)
        self.store._connection.execute(
            """
            UPDATE invocation_jobs
            SET lease_token_digest = ?
            WHERE invocation_id = ?
            """,
            ("f" * 64, prepared.request.manifest.invocation_id),
        )
        with self.assertRaises(_ResultAcceptanceConflictError):
            self.validate(prepared)

    def test_matching_manifest_without_receipt_is_partial_and_never_repaired(self) -> None:
        prepared = self.fresh_prepared()
        install_inactive_result_schema(self.store)
        manifest = prepared.request.manifest
        encoded = manifest.canonical_bytes()
        self.store._connection.execute(
            """
            INSERT INTO invocation_result_manifests(
                tenant_id, workspace_id, manifest_digest, schema_version,
                canonical_bytes, byte_size, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                manifest.tenant_id,
                manifest.workspace_id,
                manifest.canonical_digest(),
                manifest.schema_version,
                sqlite3.Binary(encoded),
                len(encoded),
                "2026-08-29T00:00:00.000000Z",
            ),
        )
        with patch.object(
            SQLiteEventStore,
            "_load_scoped_invocation_start_in_transaction",
            side_effect=AssertionError("partial graph must precede fresh lease"),
        ):
            with self.assertRaises(_ResultAcceptanceQuarantineError) as captured:
                self.validate(prepared)
        self.assertIs(captured.exception.category, _ResultAcceptanceQuarantineCategory.PARTIAL)
        self.assertEqual(captured.exception.code, "result_acceptance_graph_quarantined")
        self.assertNotIn(prepared.claimed.lease.lease_token, str(captured.exception))

    def test_structurally_complete_graph_precedes_fresh_lease_classification(self) -> None:
        helper = inactive_migration_module.InactiveInvocationResultsMigrationTests(
            methodName="runTest"
        )
        helper.store = self.store
        helper.connection = self.store._connection
        helper.seed_nonempty_v6_dependencies()
        helper.apply_candidate()
        helper.seed_complete_result_graph()
        prepared = existing_graph_prepared(helper)

        with patch.object(
            SQLiteEventStore,
            "_load_scoped_invocation_start_in_transaction",
            side_effect=AssertionError("fresh lease must not classify an existing graph"),
        ) as fresh_load:
            result = self.validate(prepared)

        self.assertIs(type(result), _ExistingResultAcceptanceGraphCandidateV2)
        assert type(result) is _ExistingResultAcceptanceGraphCandidateV2
        self.assertEqual(result.invocation_id, "invocation-1")
        fresh_load.assert_not_called()

    def test_structural_prefix_is_partial_before_fresh_lease_classification(self) -> None:
        helper = inactive_migration_module.InactiveInvocationResultsMigrationTests(
            methodName="runTest"
        )
        helper.store = self.store
        helper.connection = self.store._connection
        helper.seed_nonempty_v6_dependencies()
        helper.apply_candidate()
        helper.seed_complete_result_graph()
        self.store._connection.execute(
            "DELETE FROM invocation_result_publications WHERE receipt_id = 'receipt-1'"
        )
        prepared = existing_graph_prepared(helper)

        with patch.object(
            SQLiteEventStore,
            "_load_scoped_invocation_start_in_transaction",
            side_effect=AssertionError("partial graph must precede fresh lease"),
        ) as fresh_load:
            with self.assertRaises(_ResultAcceptanceQuarantineError) as captured:
                self.validate(prepared)
        fresh_load.assert_not_called()
        self.assertIs(captured.exception.category, _ResultAcceptanceQuarantineCategory.PARTIAL)
        self.assertEqual(captured.exception.code, "result_acceptance_graph_quarantined")

    def test_fresh_owner_preflight_yields_opaque_plan_without_clock_or_dml(self) -> None:
        prepared = self.fresh_prepared()
        install_inactive_result_schema(self.store)
        lease_token = prepared.claimed.lease.lease_token
        self.store._clock = lambda: (_ for _ in ()).throw(
            AssertionError("write preflight must not read clock")
        )

        with self.store._result_artifact_transaction() as handle:
            connection = self.store._connection_for_result_artifact_transaction(handle)
            before_total_changes = connection.total_changes
            with self.store._preflight_result_acceptance_write_in_owner_transaction(
                handle,
                prepared,
            ) as plan:
                self.assertIs(type(plan), _FreshResultAcceptanceWritePlanV2)
                assert type(plan) is _FreshResultAcceptanceWritePlanV2
                frozen, prerequisites, artifact_plan = plan._validated(
                    token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN
                )
                self.assertIs(frozen, prepared)
                self.assertIs(type(prerequisites), _FreshResultAcceptancePrerequisitesV2)
                self.assertNotIn(lease_token, repr(plan))
                self.assertEqual(connection.total_changes, before_total_changes)
                self.assertEqual(
                    connection.execute("SELECT count(*) FROM main.artifact_versions").fetchone()[0],
                    0,
                )
                self.assertIsNotNone(artifact_plan)
                for operation in (
                    lambda: copy.copy(plan),
                    lambda: copy.deepcopy(plan),
                    lambda: pickle.dumps(plan),
                ):
                    with self.subTest(operation=operation):
                        with self.assertRaisesRegex(
                            TypeError,
                            "cannot be (copied|serialized)",
                        ):
                            operation()
            with self.assertRaisesRegex(RuntimeError, "no longer active"):
                plan._validated(token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN)
            self.assertEqual(connection.total_changes, before_total_changes)

    def test_artifact_head_preflight_fails_without_creating_a_result_prefix(self) -> None:
        prepared = self.fresh_prepared()
        install_inactive_result_schema(self.store)
        with self.store._result_artifact_transaction() as handle:
            self.store._write_result_artifacts_in_owner_transaction(
                handle,
                prepared.artifact_batch,
            )
        before = tuple(
            self.store._connection.execute(f"SELECT count(*) FROM {table_name}").fetchone()[0]
            for table_name in (
                "invocation_result_requests",
                "invocation_result_receipts",
                "invocation_result_artifacts",
            )
        )

        with self.store._result_artifact_transaction() as handle:
            with self.assertRaises(_ResultArtifactConflictError):
                with self.store._preflight_result_acceptance_write_in_owner_transaction(
                    handle,
                    prepared,
                ):
                    self.fail("conflicting Artifact head unexpectedly produced a plan")

        after = tuple(
            self.store._connection.execute(f"SELECT count(*) FROM {table_name}").fetchone()[0]
            for table_name in (
                "invocation_result_requests",
                "invocation_result_receipts",
                "invocation_result_artifacts",
            )
        )
        self.assertEqual(after, before)

    def test_existing_graph_skips_fresh_artifact_preflight(self) -> None:
        helper = inactive_migration_module.InactiveInvocationResultsMigrationTests(
            methodName="runTest"
        )
        helper.store = self.store
        helper.connection = self.store._connection
        helper.seed_nonempty_v6_dependencies()
        helper.apply_candidate()
        helper.seed_complete_result_graph()
        prepared = existing_graph_prepared(helper)

        with patch(
            "quantum_entanglement.store._preflight_prepared_result_artifacts_in_transaction",
            side_effect=AssertionError("existing graph must skip Artifact preflight"),
        ) as artifact_preflight:
            with self.store._result_artifact_transaction() as handle:
                with self.store._preflight_result_acceptance_write_in_owner_transaction(
                    handle,
                    prepared,
                ) as result:
                    self.assertIs(
                        type(result),
                        _ExistingResultAcceptanceGraphCandidateV2,
                    )
        artifact_preflight.assert_not_called()

    def test_artifact_materialization_samples_one_canonical_live_accepted_at(self) -> None:
        prepared = self.fresh_prepared()
        install_inactive_result_schema(self.store)
        clock_values = ["2026-08-27T18:00:02+08:00"]
        self.store._clock = lambda: clock_values.pop(0)

        with self.store._result_artifact_transaction() as handle:
            with self.store._materialize_result_acceptance_artifacts_in_owner_transaction(
                handle,
                prepared,
            ) as materialized:
                self.assertIs(
                    type(materialized),
                    _MaterializedFreshResultAcceptancePlanV2,
                )
                assert type(materialized) is _MaterializedFreshResultAcceptancePlanV2
                frozen, prerequisites, accepted_at, artifacts = materialized._validated(
                    token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN
                )
                self.assertIs(frozen, prepared)
                self.assertIs(type(prerequisites), _FreshResultAcceptancePrerequisitesV2)
                lease_token = prepared.claimed.lease.lease_token
                self.assertNotIn(lease_token, repr(materialized))
                self.assertEqual(accepted_at, "2026-08-27T10:00:02.000000Z")
                self.assertEqual(
                    artifacts,
                    tuple(item.descriptor for item in prepared.artifact_batch.items),
                )
                for operation in (
                    lambda: copy.copy(materialized),
                    lambda: copy.deepcopy(materialized),
                    lambda: pickle.dumps(materialized),
                ):
                    with self.subTest(operation=operation):
                        with self.assertRaisesRegex(
                            TypeError,
                            "cannot be (copied|serialized)",
                        ) as captured:
                            operation()
                        self.assertNotIn(
                            lease_token,
                            "".join(traceback.format_exception(captured.exception)),
                        )

        self.assertEqual(clock_values, [])
        created_at = self.store._connection.execute(
            "SELECT created_at FROM main.artifact_versions"
        ).fetchone()[0]
        self.assertEqual(created_at, accepted_at)
        self.assertEqual(
            self.store._connection.execute(
                "SELECT count(*) FROM main.invocation_result_requests"
            ).fetchone()[0],
            0,
        )
        with self.assertRaisesRegex(RuntimeError, "no longer active"):
            materialized._validated(token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN)

    def test_narration_only_materialization_uses_the_same_clock_fence(self) -> None:
        prepared = self.narration_only_prepared()
        install_inactive_result_schema(self.store)
        calls = []

        def clock() -> str:
            calls.append("sampled")
            return "2026-08-27T10:00:02.000000Z"

        self.store._clock = clock
        with self.store._result_artifact_transaction() as handle:
            with self.store._materialize_result_acceptance_artifacts_in_owner_transaction(
                handle,
                prepared,
            ) as materialized:
                assert type(materialized) is _MaterializedFreshResultAcceptancePlanV2
                _, _, accepted_at, artifacts = materialized._validated(
                    token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN
                )
                self.assertEqual(accepted_at, "2026-08-27T10:00:02.000000Z")
                self.assertEqual(artifacts, ())
        self.assertEqual(calls, ["sampled"])
        self.assertEqual(
            self.store._connection.execute(
                "SELECT count(*) FROM main.artifact_versions"
            ).fetchone()[0],
            0,
        )

    def test_narration_only_clock_cannot_replace_the_owner_transaction(self) -> None:
        prepared = self.narration_only_prepared()
        install_inactive_result_schema(self.store)

        def replacing_clock() -> str:
            self.store._connection.set_authorizer(None)
            self.store._connection.execute("COMMIT")
            self.store._connection.execute("BEGIN IMMEDIATE")
            return "2026-08-27T10:00:02.000000Z"

        self.store._clock = replacing_clock
        with self.assertRaisesRegex(
            _ResultArtifactTransactionContinuityError,
            "changed during clock sampling",
        ):
            with self.store._result_artifact_transaction() as handle:
                with self.store._materialize_result_acceptance_artifacts_in_owner_transaction(
                    handle,
                    prepared,
                ):
                    self.fail("a replacement transaction unexpectedly produced a plan")
        self.assertTrue(self.store._poisoned)

    def test_cleanup_continuity_failure_poisons_after_clock_commits_then_raises(self) -> None:
        prepared = self.narration_only_prepared()
        install_inactive_result_schema(self.store)

        def committing_failing_clock() -> str:
            self.store._connection.set_authorizer(None)
            self.store._connection.execute(
                """
                INSERT INTO main.artifact_blobs(digest, content, byte_size, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    "sha256:" + ("b" * 64),
                    sqlite3.Binary(b"committed-clock-write"),
                    len(b"committed-clock-write"),
                    "2026-08-27T10:00:02.000000Z",
                ),
            )
            self.store._connection.execute("COMMIT")
            self.store._connection.execute("BEGIN IMMEDIATE")
            raise RuntimeError("clock failed after replacing the owner transaction")

        self.store._clock = committing_failing_clock
        with self.assertRaises(_ResultArtifactTransactionContinuityError):
            with self.store._result_artifact_transaction() as handle:
                with self.store._materialize_result_acceptance_artifacts_in_owner_transaction(
                    handle,
                    prepared,
                ):
                    self.fail("a failing replacement clock unexpectedly produced a plan")
        self.assertTrue(self.store._poisoned)
        self.assertEqual(
            self.store._connection.execute("SELECT count(*) FROM main.artifact_blobs").fetchone()[
                0
            ],
            1,
        )

    def test_narration_only_clock_dml_forces_owner_rollback(self) -> None:
        prepared = self.narration_only_prepared()
        install_inactive_result_schema(self.store)

        def writing_clock() -> str:
            self.store._connection.execute(
                """
                INSERT INTO main.artifact_blobs(digest, content, byte_size, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    "sha256:" + ("a" * 64),
                    sqlite3.Binary(b"clock-write"),
                    len(b"clock-write"),
                    "2026-08-27T10:00:02.000000Z",
                ),
            )
            return "2026-08-27T10:00:02.000000Z"

        self.store._clock = writing_clock
        with self.assertRaisesRegex(_ResultAcceptanceIntegrityError, "clock changed"):
            with self.store._result_artifact_transaction() as handle:
                with self.store._materialize_result_acceptance_artifacts_in_owner_transaction(
                    handle,
                    prepared,
                ):
                    self.fail("clock DML unexpectedly produced a materialized plan")
        self.assertEqual(
            self.store._connection.execute("SELECT count(*) FROM main.artifact_blobs").fetchone()[
                0
            ],
            0,
        )

    def test_clock_keyboard_interrupt_is_clean_and_rolls_back(self) -> None:
        prepared = self.fresh_prepared()
        install_inactive_result_schema(self.store)
        self.store._clock = lambda: (_ for _ in ()).throw(KeyboardInterrupt())

        with self.assertRaises(KeyboardInterrupt) as captured:
            with self.store._result_artifact_transaction() as handle:
                with self.store._materialize_result_acceptance_artifacts_in_owner_transaction(
                    handle,
                    prepared,
                ):
                    self.fail("interrupted acceptedAt unexpectedly produced a plan")
        self.assertIsNone(captured.exception.__cause__)
        self.assertIsNone(captured.exception.__context__)
        self.assertFalse(self.store._poisoned)
        self.assertEqual(
            self.store._connection.execute(
                "SELECT count(*) FROM main.artifact_versions"
            ).fetchone()[0],
            0,
        )

    def test_existing_graph_materialization_path_never_samples_or_writes(self) -> None:
        helper = inactive_migration_module.InactiveInvocationResultsMigrationTests(
            methodName="runTest"
        )
        helper.store = self.store
        helper.connection = self.store._connection
        helper.seed_nonempty_v6_dependencies()
        helper.apply_candidate()
        helper.seed_complete_result_graph()
        prepared = existing_graph_prepared(helper)
        before_changes = self.store._connection.total_changes
        self.store._clock = lambda: (_ for _ in ()).throw(
            AssertionError("existing graph must not sample acceptedAt")
        )

        with patch(
            "quantum_entanglement.store._materialize_prepared_result_artifacts_in_transaction",
            side_effect=AssertionError("existing graph must not materialize Artifacts"),
        ) as artifact_materialize:
            with self.store._result_artifact_transaction() as handle:
                with self.store._materialize_result_acceptance_artifacts_in_owner_transaction(
                    handle,
                    prepared,
                ) as result:
                    self.assertIs(
                        type(result),
                        _ExistingResultAcceptanceGraphCandidateV2,
                    )
        artifact_materialize.assert_not_called()
        self.assertEqual(self.store._connection.total_changes, before_changes)

    def test_fresh_identity_plan_uses_three_store_owned_distinct_ids(self) -> None:
        prepared = self.fresh_prepared()
        install_inactive_result_schema(self.store)
        clock_values = ["2026-08-27T10:00:02.000000Z"]
        self.store._clock = lambda: clock_values.pop(0)
        generated = (
            "result_receipt_store_1",
            "event_result_store_1",
            "event_terminal_store_1",
        )

        with patch(
            "quantum_entanglement.store.new_id",
            side_effect=generated,
        ) as id_provider:
            with self.store._result_artifact_transaction() as handle:
                with self.store._identify_result_acceptance_write_in_owner_transaction(
                    handle,
                    prepared,
                ) as identified:
                    self.assertIs(
                        type(identified),
                        _IdentifiedFreshResultAcceptancePlanV2,
                    )
                    assert type(identified) is _IdentifiedFreshResultAcceptancePlanV2
                    materialized, receipt_id, result_event_id, terminal_event_id = (
                        identified._validated(token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN)
                    )
                    self.assertEqual(
                        (receipt_id, result_event_id, terminal_event_id),
                        generated,
                    )
                    _, _, accepted_at, artifacts = materialized._validated(
                        token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN
                    )
                    self.assertEqual(accepted_at, "2026-08-27T10:00:02.000000Z")
                    self.assertEqual(
                        artifacts,
                        tuple(item.descriptor for item in prepared.artifact_batch.items),
                    )
                    for operation in (
                        lambda: copy.copy(identified),
                        lambda: copy.deepcopy(identified),
                        lambda: pickle.dumps(identified),
                    ):
                        with self.assertRaisesRegex(
                            TypeError,
                            "cannot be (copied|serialized)",
                        ):
                            operation()
        self.assertEqual(clock_values, [])
        self.assertEqual(
            tuple(call.args for call in id_provider.call_args_list),
            (("result_receipt",), ("evt",), ("evt",)),
        )
        with self.assertRaisesRegex(RuntimeError, "no longer active"):
            identified._validated(token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN)

    def test_existing_graph_identity_path_never_samples_clock_or_ids(self) -> None:
        helper = inactive_migration_module.InactiveInvocationResultsMigrationTests(
            methodName="runTest"
        )
        helper.store = self.store
        helper.connection = self.store._connection
        helper.seed_nonempty_v6_dependencies()
        helper.apply_candidate()
        helper.seed_complete_result_graph()
        prepared = existing_graph_prepared(helper)
        self.store._clock = lambda: (_ for _ in ()).throw(
            AssertionError("existing graph must not sample acceptedAt")
        )

        with patch(
            "quantum_entanglement.store.new_id",
            side_effect=AssertionError("existing graph must not allocate identities"),
        ) as id_provider:
            with self.store._result_artifact_transaction() as handle:
                with self.store._identify_result_acceptance_write_in_owner_transaction(
                    handle,
                    prepared,
                ) as result:
                    self.assertIs(
                        type(result),
                        _ExistingResultAcceptanceGraphCandidateV2,
                    )
        id_provider.assert_not_called()

    def test_duplicate_store_ids_fail_once_and_roll_back_artifacts(self) -> None:
        prepared = self.fresh_prepared()
        install_inactive_result_schema(self.store)
        self.store._clock = lambda: "2026-08-27T10:00:02.000000Z"
        generated = []

        def duplicate_id(_prefix: str) -> str:
            generated.append("duplicate_result_identity")
            return "duplicate_result_identity"

        with patch("quantum_entanglement.store.new_id", side_effect=duplicate_id):
            with self.assertRaisesRegex(_ResultAcceptanceConflictError, "not distinct"):
                with self.store._result_artifact_transaction() as handle:
                    with self.store._identify_result_acceptance_write_in_owner_transaction(
                        handle,
                        prepared,
                    ):
                        self.fail("duplicate store IDs unexpectedly produced a plan")
        self.assertEqual(len(generated), 3)
        self.assertEqual(
            self.store._connection.execute(
                "SELECT count(*) FROM main.artifact_versions"
            ).fetchone()[0],
            0,
        )

    def test_store_id_collision_rolls_back_materialized_artifacts(self) -> None:
        prepared = self.fresh_prepared()
        install_inactive_result_schema(self.store)
        self.store._clock = lambda: "2026-08-27T10:00:02.000000Z"

        with patch(
            "quantum_entanglement.store.new_id",
            side_effect=(
                "result_receipt_collision",
                "event-scoped-request-store-1",
                "event_terminal_collision",
            ),
        ):
            with self.assertRaisesRegex(_ResultAcceptanceConflictError, "already durable"):
                with self.store._result_artifact_transaction() as handle:
                    with self.store._identify_result_acceptance_write_in_owner_transaction(
                        handle,
                        prepared,
                    ):
                        self.fail("durable ID collision unexpectedly produced a plan")
        self.assertEqual(
            self.store._connection.execute(
                "SELECT count(*) FROM main.artifact_versions"
            ).fetchone()[0],
            0,
        )

    def test_identity_provider_dml_is_rolled_back_with_materialized_artifacts(self) -> None:
        prepared = self.fresh_prepared()
        install_inactive_result_schema(self.store)
        self.store._clock = lambda: "2026-08-27T10:00:02.000000Z"
        values = iter(
            (
                "result_receipt_store_dml",
                "event_result_store_dml",
                "event_terminal_store_dml",
            )
        )

        def writing_id_provider(_prefix: str) -> str:
            self.store._connection.execute(
                """
                INSERT INTO main.artifact_blobs(digest, content, byte_size, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(digest) DO NOTHING
                """,
                (
                    "sha256:" + ("c" * 64),
                    sqlite3.Binary(b"identity-provider-write"),
                    len(b"identity-provider-write"),
                    "2026-08-27T10:00:02.000000Z",
                ),
            )
            return next(values)

        with patch(
            "quantum_entanglement.store.new_id",
            side_effect=writing_id_provider,
        ):
            with self.assertRaisesRegex(_ResultAcceptanceIntegrityError, "provider changed"):
                with self.store._result_artifact_transaction() as handle:
                    with self.store._identify_result_acceptance_write_in_owner_transaction(
                        handle,
                        prepared,
                    ):
                        self.fail("identity-provider DML unexpectedly produced a plan")
        self.assertEqual(
            self.store._connection.execute(
                "SELECT count(*) FROM main.artifact_versions"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.store._connection.execute("SELECT count(*) FROM main.artifact_blobs").fetchone()[
                0
            ],
            0,
        )

    def test_identity_provider_transaction_replacement_poisons_store(self) -> None:
        prepared = self.fresh_prepared()
        install_inactive_result_schema(self.store)
        self.store._clock = lambda: "2026-08-27T10:00:02.000000Z"
        calls = []

        def replacing_id_provider(prefix: str) -> str:
            calls.append(prefix)
            if len(calls) == 1:
                self.store._connection.set_authorizer(None)
                self.store._connection.execute("COMMIT")
                self.store._connection.execute("BEGIN IMMEDIATE")
            return f"{prefix}_replacement_{len(calls)}"

        with patch(
            "quantum_entanglement.store.new_id",
            side_effect=replacing_id_provider,
        ):
            with self.assertRaises(_ResultArtifactTransactionContinuityError):
                with self.store._result_artifact_transaction() as handle:
                    with self.store._identify_result_acceptance_write_in_owner_transaction(
                        handle,
                        prepared,
                    ):
                        self.fail("replacement identity transaction unexpectedly produced a plan")
        self.assertEqual(calls, ["result_receipt", "evt", "evt"])
        self.assertTrue(self.store._poisoned)
        self.assertEqual(
            self.store._connection.execute(
                "SELECT count(*) FROM main.artifact_versions"
            ).fetchone()[0],
            0,
        )

    def test_narration_only_identity_provider_dml_has_no_durable_prefix(self) -> None:
        prepared = self.narration_only_prepared()
        install_inactive_result_schema(self.store)
        self.store._clock = lambda: "2026-08-27T10:00:02.000000Z"
        values = iter(
            (
                "result_receipt_narration_dml",
                "event_result_narration_dml",
                "event_terminal_narration_dml",
            )
        )

        def writing_id_provider(_prefix: str) -> str:
            self.store._connection.execute(
                """
                INSERT INTO main.artifact_blobs(digest, content, byte_size, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(digest) DO NOTHING
                """,
                (
                    "sha256:" + ("d" * 64),
                    sqlite3.Binary(b"narration-identity-provider-write"),
                    len(b"narration-identity-provider-write"),
                    "2026-08-27T10:00:02.000000Z",
                ),
            )
            return next(values)

        with patch(
            "quantum_entanglement.store.new_id",
            side_effect=writing_id_provider,
        ):
            with self.assertRaisesRegex(_ResultAcceptanceIntegrityError, "provider changed"):
                with self.store._result_artifact_transaction() as handle:
                    with self.store._identify_result_acceptance_write_in_owner_transaction(
                        handle,
                        prepared,
                    ):
                        self.fail("narration-only provider DML unexpectedly produced a plan")
        self.assertEqual(
            self.store._connection.execute(
                "SELECT count(*) FROM main.artifact_versions"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.store._connection.execute("SELECT count(*) FROM main.artifact_blobs").fetchone()[
                0
            ],
            0,
        )

    def test_narration_only_identity_transaction_replacement_poisons_store(self) -> None:
        prepared = self.narration_only_prepared()
        install_inactive_result_schema(self.store)
        self.store._clock = lambda: "2026-08-27T10:00:02.000000Z"
        calls = []

        def replacing_id_provider(prefix: str) -> str:
            calls.append(prefix)
            if len(calls) == 1:
                self.store._connection.set_authorizer(None)
                self.store._connection.execute("COMMIT")
                self.store._connection.execute("BEGIN IMMEDIATE")
            return f"{prefix}_narration_replacement_{len(calls)}"

        with patch(
            "quantum_entanglement.store.new_id",
            side_effect=replacing_id_provider,
        ):
            with self.assertRaises(_ResultArtifactTransactionContinuityError):
                with self.store._result_artifact_transaction() as handle:
                    with self.store._identify_result_acceptance_write_in_owner_transaction(
                        handle,
                        prepared,
                    ):
                        self.fail("narration-only replacement unexpectedly produced a plan")
        self.assertEqual(calls, ["result_receipt", "evt", "evt"])
        self.assertTrue(self.store._poisoned)
        self.assertEqual(
            self.store._connection.execute(
                "SELECT count(*) FROM main.artifact_versions"
            ).fetchone()[0],
            0,
        )

    def test_fresh_evidence_plan_binds_every_store_and_request_coordinate(self) -> None:
        prepared = self.fresh_prepared()
        install_inactive_result_schema(self.store)
        accepted_at = "2026-08-27T10:00:02.000000Z"
        self.store._clock = lambda: accepted_at
        generated = (
            "result_receipt_evidence_1",
            "event_result_evidence_1",
            "event_terminal_evidence_1",
        )

        with patch("quantum_entanglement.store.new_id", side_effect=generated):
            with self.store._result_artifact_transaction() as handle:
                with self.store._construct_result_acceptance_evidence_in_owner_transaction(
                    handle,
                    prepared,
                ) as evidenced:
                    self.assertIs(type(evidenced), _EvidencedFreshResultAcceptancePlanV2)
                    assert type(evidenced) is _EvidencedFreshResultAcceptancePlanV2
                    identified, evidence = evidenced._validated(
                        token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN
                    )
                    self.assertIs(type(evidence), ScopedInvocationResultEvidenceV2)
                    materialized, receipt_id, result_event_id, terminal_event_id = (
                        identified._validated(token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN)
                    )
                    _, prerequisites, materialized_at, artifacts = materialized._validated(
                        token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN
                    )
                    manifest = prepared.request.manifest
                    start = prepared.request.start_receipt.evidence
                    self.assertEqual(
                        (receipt_id, result_event_id, terminal_event_id),
                        generated,
                    )
                    self.assertEqual(
                        evidence,
                        ScopedInvocationResultEvidenceV2.from_dict(evidence.to_dict()),
                    )
                    self.assertEqual(
                        (
                            evidence.schema_version,
                            evidence.evidence_kind,
                            evidence.receipt_id,
                            evidence.tenant_id,
                            evidence.workspace_id,
                            evidence.invocation_id,
                            evidence.session_id,
                            evidence.plan_id,
                            evidence.task_id,
                            evidence.agent_id,
                            evidence.job_idempotency_key,
                        ),
                        (
                            SCOPED_INVOCATION_RESULT_EVIDENCE_SCHEMA_VERSION,
                            "attempt_bound",
                            receipt_id,
                            manifest.tenant_id,
                            manifest.workspace_id,
                            manifest.invocation_id,
                            manifest.session_id,
                            manifest.plan_id,
                            manifest.task_id,
                            manifest.agent_id,
                            manifest.job_idempotency_key,
                        ),
                    )
                    self.assertEqual(
                        (
                            evidence.running_task_revision,
                            evidence.terminal_task_revision,
                            evidence.attempt_id,
                            evidence.attempt_number,
                            evidence.lease_epoch,
                            evidence.worker_id,
                            evidence.lease_token_digest,
                            evidence.start_receipt_digest,
                        ),
                        (
                            prerequisites.running_task_revision,
                            prerequisites.running_task_revision + 1,
                            prerequisites.attempt_id,
                            start.attempt_number,
                            prerequisites.lease_epoch,
                            prerequisites.worker_id,
                            prerequisites.lease_token_digest,
                            prerequisites.start_receipt_digest,
                        ),
                    )
                    self.assertEqual(
                        (
                            evidence.execution_manifest_digest,
                            evidence.result_manifest_schema_version,
                            evidence.result_manifest_digest,
                            evidence.result_ref,
                            evidence.effect_class,
                            evidence.action_receipt_set_digest,
                            evidence.acceptance_idempotency_key,
                            evidence.request_digest,
                            evidence.accepted_at,
                            evidence.artifact_count,
                        ),
                        (
                            manifest.execution_manifest_digest,
                            manifest.schema_version,
                            manifest.canonical_digest(),
                            manifest.result_ref,
                            manifest.effect_class,
                            manifest.action_receipt_set_digest,
                            prepared.request.acceptance_idempotency_key,
                            prerequisites.request_digest,
                            materialized_at,
                            len(artifacts),
                        ),
                    )
                    for operation in (
                        lambda: copy.copy(evidenced),
                        lambda: copy.deepcopy(evidenced),
                        lambda: pickle.dumps(evidenced),
                    ):
                        with self.assertRaisesRegex(
                            TypeError,
                            "cannot be (copied|serialized)",
                        ):
                            operation()
                    with self.assertRaisesRegex(RuntimeError, "already started"):
                        identified._begin_evidence_construction(
                            token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN
                        )
        with self.assertRaisesRegex(RuntimeError, "no longer active"):
            evidenced._validated(token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN)
        with self.assertRaisesRegex(RuntimeError, "no longer active"):
            identified._validated(token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN)

    def test_narration_only_evidence_has_exact_zero_artifact_count(self) -> None:
        prepared = self.narration_only_prepared()
        install_inactive_result_schema(self.store)
        self.store._clock = lambda: "2026-08-27T10:00:02.000000Z"
        with patch(
            "quantum_entanglement.store.new_id",
            side_effect=("receipt_zero", "event_result_zero", "event_terminal_zero"),
        ):
            with self.store._result_artifact_transaction() as handle:
                with self.store._construct_result_acceptance_evidence_in_owner_transaction(
                    handle,
                    prepared,
                ) as evidenced:
                    assert type(evidenced) is _EvidencedFreshResultAcceptancePlanV2
                    _, evidence = evidenced._validated(token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN)
                    self.assertEqual(evidence.artifact_count, 0)
                    self.assertEqual(prepared.request.manifest.artifacts, ())

    def test_fresh_terminal_transition_binds_evidence_and_store_result_event(self) -> None:
        prepared = self.fresh_prepared()
        install_inactive_result_schema(self.store)
        accepted_at = "2026-08-27T10:00:02.000000Z"
        self.store._clock = lambda: accepted_at
        generated = (
            "result_receipt_transition_1",
            "event_result_transition_1",
            "event_terminal_transition_1",
        )

        with patch("quantum_entanglement.store.new_id", side_effect=generated):
            with self.store._result_artifact_transaction() as handle:
                construct = (
                    self.store._construct_result_acceptance_terminal_transition_in_owner_transaction
                )
                with construct(
                    handle,
                    prepared,
                ) as transitioned:
                    self.assertIs(
                        type(transitioned),
                        _TransitionedFreshResultAcceptancePlanV2,
                    )
                    assert type(transitioned) is _TransitionedFreshResultAcceptancePlanV2
                    evidenced, transition = transitioned._validated(
                        token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN
                    )
                    identified, evidence = evidenced._validated(
                        token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN
                    )
                    _, receipt_id, result_event_id, terminal_event_id = identified._validated(
                        token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN
                    )
                    manifest = prepared.request.manifest
                    self.assertIs(
                        type(transition),
                        ScopedInvocationResultTerminalTransitionV2,
                    )
                    self.assertEqual(
                        (receipt_id, result_event_id, terminal_event_id),
                        generated,
                    )
                    self.assertEqual(
                        transition,
                        ScopedInvocationResultTerminalTransitionV2.from_dict(transition.to_dict()),
                    )
                    self.assertEqual(
                        (
                            transition.tenant_id,
                            transition.workspace_id,
                            transition.invocation_id,
                            transition.session_id,
                            transition.plan_id,
                            transition.task_id,
                            transition.agent_id,
                            transition.job_idempotency_key,
                            transition.runtime_revision,
                            transition.correlation_id,
                        ),
                        (
                            manifest.tenant_id,
                            manifest.workspace_id,
                            manifest.invocation_id,
                            manifest.session_id,
                            manifest.plan_id,
                            manifest.task_id,
                            manifest.agent_id,
                            manifest.job_idempotency_key,
                            manifest.runtime_revision,
                            manifest.correlation_id,
                        ),
                    )
                    self.assertEqual(
                        (
                            transition.previous,
                            transition.current,
                            transition.reason,
                            transition.running_task_revision,
                            transition.terminal_task_revision,
                            transition.result_receipt_id,
                            transition.result_event_id,
                            transition.result_evidence_digest,
                        ),
                        (
                            TaskStatus.RUNNING,
                            TaskStatus.COMPLETED,
                            None,
                            evidence.running_task_revision,
                            evidence.terminal_task_revision,
                            receipt_id,
                            result_event_id,
                            evidence.canonical_digest(),
                        ),
                    )
                    self.assertEqual(transition.stream_id, "session:" + manifest.session_id)
                    self.assertEqual(transition.causation_id, result_event_id)
                    self.assertEqual(
                        transition.idempotency_key,
                        f"task-status:{manifest.task_id}:{evidence.terminal_task_revision}",
                    )
                    for operation in (
                        lambda: copy.copy(transitioned),
                        lambda: copy.deepcopy(transitioned),
                        lambda: pickle.dumps(transitioned),
                    ):
                        with self.assertRaisesRegex(
                            TypeError,
                            "cannot be (copied|serialized)",
                        ):
                            operation()
                    with self.assertRaisesRegex(RuntimeError, "already started"):
                        evidenced._begin_terminal_transition_construction(
                            token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN
                        )
        for plan in (transitioned, evidenced, identified):
            with self.assertRaisesRegex(RuntimeError, "no longer active"):
                plan._validated(token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN)

    def test_existing_graph_terminal_path_does_not_construct_fresh_payload(self) -> None:
        helper = inactive_migration_module.InactiveInvocationResultsMigrationTests(
            methodName="runTest"
        )
        helper.store = self.store
        helper.connection = self.store._connection
        helper.seed_nonempty_v6_dependencies()
        helper.apply_candidate()
        helper.seed_complete_result_graph()
        prepared = existing_graph_prepared(helper)

        with patch(
            "quantum_entanglement.store._build_scoped_invocation_result_terminal_transition_from_plan_v2",
            side_effect=AssertionError(
                "existing graph must not construct a fresh terminal transition"
            ),
        ) as builder:
            with self.store._result_artifact_transaction() as handle:
                construct = (
                    self.store._construct_result_acceptance_terminal_transition_in_owner_transaction
                )
                with construct(
                    handle,
                    prepared,
                ) as result:
                    self.assertIs(type(result), _ExistingResultAcceptanceGraphCandidateV2)
        builder.assert_not_called()

    def test_terminal_transition_failure_rolls_back_materialized_artifacts(self) -> None:
        prepared = self.fresh_prepared()
        install_inactive_result_schema(self.store)
        self.store._clock = lambda: "2026-08-27T10:00:02.000000Z"
        with (
            patch(
                "quantum_entanglement.store.new_id",
                side_effect=(
                    "receipt_transition_failure",
                    "event_result_transition_failure",
                    "event_terminal_transition_failure",
                ),
            ),
            patch(
                "quantum_entanglement.store._build_scoped_invocation_result_terminal_transition_from_plan_v2",
                side_effect=RuntimeError("terminal transition construction failed"),
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "terminal transition construction failed",
            ):
                with self.store._result_artifact_transaction() as handle:
                    construct = self.store._construct_result_acceptance_terminal_transition_in_owner_transaction  # noqa: E501
                    with construct(
                        handle,
                        prepared,
                    ):
                        self.fail(
                            "failed terminal transition construction unexpectedly yielded a plan"
                        )
        for table in ("artifact_versions", "artifact_blobs"):
            with self.subTest(table=table):
                self.assertEqual(
                    self.store._connection.execute(f"SELECT count(*) FROM main.{table}").fetchone()[
                        0
                    ],
                    0,
                )

    def test_caught_terminal_failure_still_forces_owner_transaction_rollback(self) -> None:
        prepared = self.fresh_prepared()
        install_inactive_result_schema(self.store)
        self.store._clock = lambda: "2026-08-27T10:00:02.000000Z"
        with (
            patch(
                "quantum_entanglement.store.new_id",
                side_effect=(
                    "receipt_terminal_caught",
                    "event_result_terminal_caught",
                    "event_terminal_caught",
                ),
            ),
            patch(
                "quantum_entanglement.store._build_scoped_invocation_result_terminal_transition_from_plan_v2",
                side_effect=RuntimeError("caught terminal transition failure"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "rollback-only"):
                with self.store._result_artifact_transaction() as handle:
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "caught terminal transition failure",
                    ):
                        construct = self.store._construct_result_acceptance_terminal_transition_in_owner_transaction  # noqa: E501
                        with construct(handle, prepared):
                            self.fail(
                                "caught terminal transition failure unexpectedly yielded a plan"
                            )
        for table in ("artifact_versions", "artifact_blobs"):
            with self.subTest(table=table):
                self.assertEqual(
                    self.store._connection.execute(f"SELECT count(*) FROM main.{table}").fetchone()[
                        0
                    ],
                    0,
                )

    def test_terminal_stage_rejects_wrong_builder_outputs_and_rolls_back(self) -> None:
        prepared = self.fresh_prepared()
        install_inactive_result_schema(self.store)
        self.store._clock = lambda: "2026-08-27T10:00:02.000000Z"

        def drifted_transition(
            evidenced: _EvidencedFreshResultAcceptancePlanV2,
        ) -> ScopedInvocationResultTerminalTransitionV2:
            transition = _build_scoped_invocation_result_terminal_transition_from_plan_v2(evidenced)
            return replace(transition, result_event_id="event-result-drifted")

        cases = (
            ("wrong_type", lambda _evidenced: object(), TypeError, "not exact"),
            (
                "binding_drift",
                drifted_transition,
                ValueError,
                "differs from its evidenced plan",
            ),
        )
        for ordinal, (label, builder, error_type, message) in enumerate(cases, start=1):
            with (
                self.subTest(case=label),
                patch(
                    "quantum_entanglement.store.new_id",
                    side_effect=(
                        f"receipt_terminal_bad_{ordinal}",
                        f"event_result_terminal_bad_{ordinal}",
                        f"event_terminal_bad_{ordinal}",
                    ),
                ),
                patch(
                    "quantum_entanglement.store._build_scoped_invocation_result_terminal_transition_from_plan_v2",
                    side_effect=builder,
                ),
            ):
                with self.assertRaisesRegex(error_type, message):
                    with self.store._result_artifact_transaction() as handle:
                        construct = self.store._construct_result_acceptance_terminal_transition_in_owner_transaction  # noqa: E501
                        with construct(handle, prepared):
                            self.fail("invalid terminal builder output unexpectedly yielded")
            for table in ("artifact_versions", "artifact_blobs"):
                self.assertEqual(
                    self.store._connection.execute(f"SELECT count(*) FROM main.{table}").fetchone()[
                        0
                    ],
                    0,
                )

    def test_fresh_event_pair_binds_exact_canonical_domain_events(self) -> None:
        prepared = self.fresh_prepared()
        install_inactive_result_schema(self.store)
        accepted_at = "2026-08-27T10:00:02.000000Z"
        self.store._clock = lambda: accepted_at
        generated = (
            "result_receipt_events_1",
            "event_result_events_1",
            "event_terminal_events_1",
        )

        with patch("quantum_entanglement.store.new_id", side_effect=generated):
            with self.store._result_artifact_transaction() as handle:
                construct = self.store._construct_result_acceptance_event_pair_in_owner_transaction
                with construct(handle, prepared) as evented:
                    self.assertIs(type(evented), _EventedFreshResultAcceptancePlanV2)
                    assert type(evented) is _EventedFreshResultAcceptancePlanV2
                    transitioned, result_event, terminal_event = evented._validated(
                        token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN
                    )
                    evidenced, transition = transitioned._validated(
                        token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN
                    )
                    identified, evidence = evidenced._validated(
                        token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN
                    )
                    _, receipt_id, result_event_id, terminal_event_id = identified._validated(
                        token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN
                    )
                    manifest = prepared.request.manifest
                    self.assertIs(type(result_event), DomainEvent)
                    self.assertIs(type(terminal_event), DomainEvent)
                    self.assertEqual(
                        (receipt_id, result_event_id, terminal_event_id),
                        generated,
                    )
                    self.assertEqual(
                        (
                            result_event.stream_id,
                            result_event.event_type,
                            result_event.actor_id,
                            result_event.event_id,
                            result_event.timestamp,
                            result_event.correlation_id,
                            result_event.causation_id,
                            result_event.idempotency_key,
                        ),
                        (
                            "session:" + manifest.session_id,
                            TASK_INVOCATION_RESULT_ACCEPTED_EVENT_TYPE,
                            CANONICAL_ORCHESTRATOR_ACTOR_ID,
                            result_event_id,
                            accepted_at,
                            manifest.correlation_id,
                            prepared.request.start_receipt.event_id,
                            prepared.request.acceptance_idempotency_key,
                        ),
                    )
                    self.assertEqual(
                        ScopedInvocationResultEvidenceV2.from_dict(result_event.payload),
                        evidence,
                    )
                    self.assertEqual(
                        (
                            terminal_event.stream_id,
                            terminal_event.event_type,
                            terminal_event.actor_id,
                            terminal_event.event_id,
                            terminal_event.timestamp,
                            terminal_event.correlation_id,
                            terminal_event.causation_id,
                            terminal_event.idempotency_key,
                        ),
                        (
                            result_event.stream_id,
                            TASK_STATUS_CHANGED_EVENT_TYPE,
                            CANONICAL_ORCHESTRATOR_ACTOR_ID,
                            terminal_event_id,
                            accepted_at,
                            result_event.correlation_id,
                            result_event_id,
                            transition.idempotency_key,
                        ),
                    )
                    self.assertEqual(
                        ScopedInvocationResultTerminalTransitionV2.from_dict(
                            terminal_event.payload
                        ),
                        transition,
                    )
                    for operation in (
                        lambda: copy.copy(evented),
                        lambda: copy.deepcopy(evented),
                        lambda: pickle.dumps(evented),
                    ):
                        with self.assertRaisesRegex(
                            TypeError,
                            "cannot be (copied|serialized)",
                        ):
                            operation()
                    with self.assertRaisesRegex(RuntimeError, "already started"):
                        transitioned._begin_event_construction(
                            token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN
                        )
        for plan in (evented, transitioned, evidenced, identified):
            with self.assertRaisesRegex(RuntimeError, "no longer active"):
                plan._validated(token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN)

    def test_existing_graph_event_path_does_not_construct_fresh_events(self) -> None:
        helper = inactive_migration_module.InactiveInvocationResultsMigrationTests(
            methodName="runTest"
        )
        helper.store = self.store
        helper.connection = self.store._connection
        helper.seed_nonempty_v6_dependencies()
        helper.apply_candidate()
        helper.seed_complete_result_graph()
        prepared = existing_graph_prepared(helper)

        with patch(
            "quantum_entanglement.store._build_scoped_invocation_result_events_from_plan_v2",
            side_effect=AssertionError("existing graph must not construct fresh events"),
        ) as builder:
            with self.store._result_artifact_transaction() as handle:
                construct = self.store._construct_result_acceptance_event_pair_in_owner_transaction
                with construct(handle, prepared) as result:
                    self.assertIs(type(result), _ExistingResultAcceptanceGraphCandidateV2)
        builder.assert_not_called()

    def test_event_pair_construction_failure_rolls_back_materialized_artifacts(self) -> None:
        prepared = self.fresh_prepared()
        install_inactive_result_schema(self.store)
        self.store._clock = lambda: "2026-08-27T10:00:02.000000Z"
        with (
            patch(
                "quantum_entanglement.store.new_id",
                side_effect=(
                    "receipt_event_failure",
                    "event_result_event_failure",
                    "event_terminal_event_failure",
                ),
            ),
            patch(
                "quantum_entanglement.store._build_scoped_invocation_result_events_from_plan_v2",
                side_effect=RuntimeError("event pair construction failed"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "event pair construction failed"):
                with self.store._result_artifact_transaction() as handle:
                    construct = (
                        self.store._construct_result_acceptance_event_pair_in_owner_transaction
                    )
                    with construct(handle, prepared):
                        self.fail("failed event construction unexpectedly yielded a plan")
        for table in ("artifact_versions", "artifact_blobs"):
            with self.subTest(table=table):
                self.assertEqual(
                    self.store._connection.execute(f"SELECT count(*) FROM main.{table}").fetchone()[
                        0
                    ],
                    0,
                )

    def test_inserted_event_pair_is_fresh_consecutive_and_raw_verified(self) -> None:
        prepared = self.fresh_prepared()
        install_inactive_result_schema(self.store)
        self.store._clock = lambda: "2026-08-27T10:00:02.000000Z"
        generated = (
            "receipt_inserted_1",
            "event_result_inserted_1",
            "event_terminal_inserted_1",
        )
        before_events = self.store._connection.execute("SELECT count(*) FROM events").fetchone()[0]

        with patch("quantum_entanglement.store.new_id", side_effect=generated):
            with self.store._result_artifact_transaction() as handle:
                insert_pair = self.store._insert_result_acceptance_event_pair_in_owner_transaction
                with insert_pair(handle, prepared) as inserted:
                    self.assertIs(type(inserted), _InsertedFreshResultAcceptancePlanV2)
                    assert type(inserted) is _InsertedFreshResultAcceptancePlanV2
                    (
                        evented,
                        result_stored,
                        result_envelope,
                        terminal_stored,
                        terminal_envelope,
                    ) = inserted._validated(token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN)
                    self.assertEqual(
                        (result_stored.sequence, terminal_stored.sequence),
                        (
                            prepared.request.expected_stream_version + 1,
                            prepared.request.expected_stream_version + 2,
                        ),
                    )
                    self.assertEqual(
                        terminal_stored.global_position,
                        result_stored.global_position + 1,
                    )
                    self.assertEqual(
                        result_stored.event.idempotency_key,
                        prepared.request.acceptance_idempotency_key,
                    )
                    self.assertEqual(
                        terminal_stored.event.causation_id,
                        result_stored.event.event_id,
                    )
                    self.assertNotEqual(result_envelope.digest(), terminal_envelope.digest())
                    self.assertEqual(
                        result_envelope.to_dict()["eventId"],
                        result_stored.event.event_id,
                    )
                    self.assertEqual(
                        terminal_envelope.to_dict()["eventId"],
                        terminal_stored.event.event_id,
                    )
                    self.assertEqual(
                        ScopedInvocationResultEvidenceV2.from_dict(
                            result_stored.event.payload
                        ).accepted_at,
                        result_stored.event.timestamp,
                    )
                    self.assertEqual(
                        ScopedInvocationResultTerminalTransitionV2.from_dict(
                            terminal_stored.event.payload
                        ).result_event_id,
                        result_stored.event.event_id,
                    )
                    for operation in (
                        lambda: copy.copy(inserted),
                        lambda: copy.deepcopy(inserted),
                        lambda: pickle.dumps(inserted),
                    ):
                        with self.assertRaisesRegex(
                            TypeError,
                            "cannot be (copied|serialized)",
                        ):
                            operation()
        self.assertEqual(
            self.store._connection.execute("SELECT count(*) FROM events").fetchone()[0],
            before_events + 2,
        )
        with self.assertRaisesRegex(RuntimeError, "no longer active"):
            inserted._validated(token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN)
        with self.assertRaisesRegex(RuntimeError, "no longer active"):
            evented._validated(token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN)

    def test_existing_graph_insert_path_does_not_touch_event_rows(self) -> None:
        helper = inactive_migration_module.InactiveInvocationResultsMigrationTests(
            methodName="runTest"
        )
        helper.store = self.store
        helper.connection = self.store._connection
        helper.seed_nonempty_v6_dependencies()
        helper.apply_candidate()
        helper.seed_complete_result_graph()
        prepared = existing_graph_prepared(helper)
        before = tuple(tuple(row) for row in self.store._connection.execute("SELECT * FROM events"))
        with patch.object(
            SQLiteEventStore,
            "_insert_with_verified_envelope_in_transaction",
            side_effect=AssertionError("existing graph must not insert events"),
        ) as insert:
            with self.store._result_artifact_transaction() as handle:
                with self.store._insert_result_acceptance_event_pair_in_owner_transaction(
                    handle,
                    prepared,
                ) as result:
                    self.assertIs(type(result), _ExistingResultAcceptanceGraphCandidateV2)
        insert.assert_not_called()
        after = tuple(tuple(row) for row in self.store._connection.execute("SELECT * FROM events"))
        self.assertEqual(after, before)

    def test_second_event_insert_failure_rolls_back_pair_and_artifacts(self) -> None:
        prepared = self.fresh_prepared()
        install_inactive_result_schema(self.store)
        self.store._clock = lambda: "2026-08-27T10:00:02.000000Z"
        generated = (
            "receipt_second_failure",
            "event_result_second_failure",
            "event_terminal_second_failure",
        )
        before_events = tuple(
            tuple(row) for row in self.store._connection.execute("SELECT * FROM events")
        )
        original_insert = SQLiteEventStore._insert_with_verified_envelope_in_transaction
        calls = 0

        def fail_second(*args: object, **kwargs: object) -> object:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("terminal event insert failed")
            return original_insert(*args, **kwargs)  # type: ignore[arg-type]

        with (
            patch("quantum_entanglement.store.new_id", side_effect=generated),
            patch.object(
                SQLiteEventStore,
                "_insert_with_verified_envelope_in_transaction",
                side_effect=fail_second,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "terminal event insert failed"):
                with self.store._result_artifact_transaction() as handle:
                    with self.store._insert_result_acceptance_event_pair_in_owner_transaction(
                        handle,
                        prepared,
                    ):
                        self.fail("failed terminal insertion unexpectedly yielded a plan")
        self.assertEqual(calls, 2)
        self.assertEqual(
            tuple(tuple(row) for row in self.store._connection.execute("SELECT * FROM events")),
            before_events,
        )
        for table in ("artifact_versions", "artifact_blobs"):
            self.assertEqual(
                self.store._connection.execute(f"SELECT count(*) FROM main.{table}").fetchone()[0],
                0,
            )

    def test_receipt_plan_rebuilds_complete_graph_without_receipt_dml(self) -> None:
        prepared = self.fresh_prepared()
        install_inactive_result_schema(self.store)
        self.store._clock = lambda: "2026-08-27T10:00:02.000000Z"
        generated = (
            "receipt_receipted_1",
            "event_result_receipted_1",
            "event_terminal_receipted_1",
        )

        with patch("quantum_entanglement.store.new_id", side_effect=generated):
            with self.store._result_artifact_transaction() as handle:
                construct = self.store._construct_result_acceptance_receipt_in_owner_transaction
                with construct(handle, prepared) as receipted:
                    self.assertIs(type(receipted), _ReceiptedFreshResultAcceptancePlanV2)
                    assert type(receipted) is _ReceiptedFreshResultAcceptancePlanV2
                    inserted, receipt = receipted._validated(
                        token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN
                    )
                    evented, result_stored, result_envelope, terminal_stored, terminal_envelope = (
                        inserted._validated(token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN)
                    )
                    self.assertEqual(receipt.receipt_id, generated[0])
                    self.assertEqual(
                        receipt.result_event.event_id,
                        result_stored.event.event_id,
                    )
                    self.assertEqual(
                        receipt.terminal_event.event_id,
                        terminal_stored.event.event_id,
                    )
                    self.assertEqual(
                        receipt.result_event.event_envelope_digest,
                        result_envelope.digest(),
                    )
                    self.assertEqual(
                        receipt.terminal_event.event_envelope_digest,
                        terminal_envelope.digest(),
                    )
                    self.assertEqual(receipt.canonical_digest(), receipt.receipt_digest)
                    self.assertEqual(
                        self.store._connection.execute(
                            "SELECT count(*) FROM invocation_result_receipts"
                        ).fetchone()[0],
                        0,
                    )
                    for operation in (
                        lambda: copy.copy(receipted),
                        lambda: copy.deepcopy(receipted),
                        lambda: pickle.dumps(receipted),
                    ):
                        with self.assertRaisesRegex(
                            TypeError,
                            "cannot be (copied|serialized)",
                        ):
                            operation()
        for plan in (receipted, inserted, evented):
            with self.assertRaisesRegex(RuntimeError, "no longer active"):
                plan._validated(token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN)

    def test_existing_graph_receipt_path_does_not_build_or_insert(self) -> None:
        helper = inactive_migration_module.InactiveInvocationResultsMigrationTests(
            methodName="runTest"
        )
        helper.store = self.store
        helper.connection = self.store._connection
        helper.seed_nonempty_v6_dependencies()
        helper.apply_candidate()
        helper.seed_complete_result_graph()
        prepared = existing_graph_prepared(helper)
        with (
            patch(
                "quantum_entanglement.store._build_scoped_invocation_result_receipt_v2",
                side_effect=AssertionError("existing graph must not build a receipt"),
            ) as builder,
            patch.object(
                SQLiteEventStore,
                "_insert_with_verified_envelope_in_transaction",
                side_effect=AssertionError("existing graph must not insert events"),
            ) as insert,
        ):
            with self.store._result_artifact_transaction() as handle:
                with self.store._construct_result_acceptance_receipt_in_owner_transaction(
                    handle,
                    prepared,
                ) as result:
                    self.assertIs(type(result), _ExistingResultAcceptanceGraphCandidateV2)
        builder.assert_not_called()
        insert.assert_not_called()

    def test_receipt_construction_failure_rolls_back_events_and_artifacts(self) -> None:
        prepared = self.fresh_prepared()
        install_inactive_result_schema(self.store)
        self.store._clock = lambda: "2026-08-27T10:00:02.000000Z"
        before_events = tuple(
            tuple(row) for row in self.store._connection.execute("SELECT * FROM events")
        )
        with (
            patch(
                "quantum_entanglement.store.new_id",
                side_effect=(
                    "receipt_receipt_failure",
                    "event_result_receipt_failure",
                    "event_terminal_receipt_failure",
                ),
            ),
            patch(
                "quantum_entanglement.store._build_scoped_invocation_result_receipt_v2",
                side_effect=RuntimeError("receipt construction failed"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "receipt construction failed"):
                with self.store._result_artifact_transaction() as handle:
                    with self.store._construct_result_acceptance_receipt_in_owner_transaction(
                        handle,
                        prepared,
                    ):
                        self.fail("failed receipt construction unexpectedly yielded a plan")
        self.assertEqual(
            tuple(tuple(row) for row in self.store._connection.execute("SELECT * FROM events")),
            before_events,
        )
        for table in ("artifact_versions", "artifact_blobs", "invocation_result_receipts"):
            self.assertEqual(
                self.store._connection.execute(f"SELECT count(*) FROM main.{table}").fetchone()[0],
                0,
            )

    def test_persisted_result_graph_writes_all_local_rows_without_publication(self) -> None:
        prepared = self.fresh_prepared()
        install_inactive_result_schema(self.store)
        self.store._clock = lambda: "2026-08-27T10:00:02.000000Z"
        generated = (
            "receipt_persisted_1",
            "event_result_persisted_1",
            "event_terminal_persisted_1",
        )
        with patch("quantum_entanglement.store.new_id", side_effect=generated):
            with self.store._result_artifact_transaction() as handle:
                persist = self.store._persist_result_acceptance_graph_in_owner_transaction
                with persist(handle, prepared) as persisted:
                    self.assertIs(type(persisted), _PersistedFreshResultAcceptancePlanV2)
                    assert type(persisted) is _PersistedFreshResultAcceptancePlanV2
                    receipted, receipt = persisted._validated(
                        token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN
                    )
                    self.assertIs(type(receipted), _ReceiptedFreshResultAcceptancePlanV2)
                    self.assertEqual(receipt.receipt_id, generated[0])
                    self.assertEqual(
                        tuple(
                            self.store._connection.execute(
                                f"SELECT count(*) FROM main.{table}"
                            ).fetchone()[0]
                            for table in (
                                "invocation_result_manifests",
                                "invocation_result_requests",
                                "invocation_result_event_bindings",
                                "invocation_result_receipts",
                                "invocation_result_artifacts",
                                "invocation_result_publications",
                            )
                        ),
                        (1, 1, 2, 1, 1, 0),
                    )
                    manifest_row = self.store._connection.execute(
                        "SELECT manifest_digest, canonical_bytes, created_at "
                        "FROM invocation_result_manifests"
                    ).fetchone()
                    self.assertEqual(
                        manifest_row["manifest_digest"], receipt.evidence.result_manifest_digest
                    )
                    self.assertEqual(
                        bytes(manifest_row["canonical_bytes"]),
                        prepared.request.manifest.canonical_bytes(),
                    )
                    self.assertEqual(manifest_row["created_at"], receipt.evidence.accepted_at)
                    request_row = self.store._connection.execute(
                        "SELECT request_digest, request_identity_bytes, created_at "
                        "FROM invocation_result_requests"
                    ).fetchone()
                    self.assertEqual(request_row["request_digest"], receipt.evidence.request_digest)
                    self.assertGreater(request_row["request_identity_bytes"], b"")
                    self.assertEqual(request_row["created_at"], receipt.evidence.accepted_at)
                    receipt_row = self.store._connection.execute(
                        "SELECT receipt_id, result_event_id, terminal_event_id, receipt_digest "
                        "FROM invocation_result_receipts"
                    ).fetchone()
                    self.assertEqual(receipt_row["receipt_id"], receipt.receipt_id)
                    self.assertEqual(receipt_row["result_event_id"], receipt.result_event.event_id)
                    self.assertEqual(
                        receipt_row["terminal_event_id"],
                        receipt.terminal_event.event_id,
                    )
                    self.assertEqual(receipt_row["receipt_digest"], receipt.receipt_digest)
                    for operation in (
                        lambda: copy.copy(persisted),
                        lambda: copy.deepcopy(persisted),
                        lambda: pickle.dumps(persisted),
                    ):
                        with self.assertRaisesRegex(
                            TypeError,
                            "cannot be (copied|serialized)",
                        ):
                            operation()
        with self.assertRaisesRegex(RuntimeError, "no longer active"):
            persisted._validated(token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN)
        with self.assertRaisesRegex(RuntimeError, "no longer active"):
            receipted._validated(token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN)

    def test_existing_graph_persistence_path_does_not_write_any_rows(self) -> None:
        helper = inactive_migration_module.InactiveInvocationResultsMigrationTests(
            methodName="runTest"
        )
        helper.store = self.store
        helper.connection = self.store._connection
        helper.seed_nonempty_v6_dependencies()
        helper.apply_candidate()
        helper.seed_complete_result_graph()
        prepared = existing_graph_prepared(helper)
        before = tuple(
            tuple(self.store._connection.execute(f"SELECT * FROM {table}"))
            for table in (
                "events",
                "invocation_result_manifests",
                "invocation_result_requests",
                "invocation_result_event_bindings",
                "invocation_result_receipts",
                "invocation_result_artifacts",
            )
        )
        with patch.object(
            SQLiteEventStore,
            "_insert_exact_result_acceptance_row_in_owner_transaction",
            side_effect=AssertionError("existing graph must not persist rows"),
        ) as insert:
            with self.store._result_artifact_transaction() as handle:
                with self.store._persist_result_acceptance_graph_in_owner_transaction(
                    handle,
                    prepared,
                ) as result:
                    self.assertIs(type(result), _ExistingResultAcceptanceGraphCandidateV2)
        insert.assert_not_called()
        after = tuple(
            tuple(self.store._connection.execute(f"SELECT * FROM {table}"))
            for table in (
                "events",
                "invocation_result_manifests",
                "invocation_result_requests",
                "invocation_result_event_bindings",
                "invocation_result_receipts",
                "invocation_result_artifacts",
            )
        )
        self.assertEqual(after, before)

    def test_persistence_trigger_side_effect_rolls_back_complete_local_graph(self) -> None:
        prepared = self.fresh_prepared()
        install_inactive_result_schema(self.store)
        self.store._clock = lambda: "2026-08-27T10:00:02.000000Z"
        before_events = tuple(
            tuple(row) for row in self.store._connection.execute("SELECT * FROM events")
        )
        self.store._connection.execute("CREATE TABLE manifest_audit (digest TEXT NOT NULL)")
        self.store._connection.execute(
            """
            CREATE TRIGGER manifest_persistence_audit
            AFTER INSERT ON invocation_result_manifests
            BEGIN
                INSERT INTO manifest_audit (digest) VALUES (NEW.manifest_digest);
            END
            """
        )
        with patch(
            "quantum_entanglement.store.new_id",
            side_effect=(
                "receipt_persist_trigger",
                "event_result_persist_trigger",
                "event_terminal_persist_trigger",
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "changed an unexpected row count"):
                with self.store._result_artifact_transaction() as handle:
                    with self.store._persist_result_acceptance_graph_in_owner_transaction(
                        handle,
                        prepared,
                    ):
                        self.fail("triggered persistence unexpectedly yielded a plan")
        self.assertEqual(
            tuple(tuple(row) for row in self.store._connection.execute("SELECT * FROM events")),
            before_events,
        )
        for table in (
            "invocation_result_manifests",
            "invocation_result_requests",
            "invocation_result_event_bindings",
            "invocation_result_receipts",
            "invocation_result_artifacts",
            "manifest_audit",
        ):
            self.assertEqual(
                self.store._connection.execute(f"SELECT count(*) FROM main.{table}").fetchone()[0],
                0,
            )

    def test_completion_cas_moves_exact_job_and_attempt_to_succeeded(self) -> None:
        prepared = self.fresh_prepared()
        install_inactive_result_schema(self.store)
        self.store._clock = lambda: "2026-08-27T10:00:02.000000Z"
        generated = (
            "receipt_completed_1",
            "event_result_completed_1",
            "event_terminal_completed_1",
        )
        with patch("quantum_entanglement.store.new_id", side_effect=generated):
            with self.store._result_artifact_transaction() as handle:
                complete = (
                    self.store._complete_result_acceptance_job_and_attempt_in_owner_transaction
                )
                with complete(handle, prepared) as completed:
                    self.assertIs(type(completed), _CompletedFreshResultAcceptancePlanV2)
                    assert type(completed) is _CompletedFreshResultAcceptancePlanV2
                    persisted, receipt = completed._validated(
                        token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN
                    )
                    self.assertEqual(receipt.receipt_id, generated[0])
                    self.assertIs(type(persisted), _PersistedFreshResultAcceptancePlanV2)
                    job = self.store._connection.execute(
                        "SELECT status, result_ref, updated_at, finished_at, "
                        "lease_owner, lease_token_digest, lease_expires_at, heartbeat_at "
                        "FROM invocation_jobs WHERE invocation_id = ?",
                        (prepared.request.manifest.invocation_id,),
                    ).fetchone()
                    self.assertEqual(job["status"], "succeeded")
                    self.assertEqual(job["result_ref"], receipt.evidence.result_ref)
                    self.assertEqual(job["updated_at"], receipt.evidence.accepted_at)
                    self.assertEqual(job["finished_at"], receipt.evidence.accepted_at)
                    self.assertIsNone(job["lease_owner"])
                    self.assertIsNone(job["lease_token_digest"])
                    self.assertIsNone(job["lease_expires_at"])
                    self.assertIsNone(job["heartbeat_at"])
                    attempt = self.store._connection.execute(
                        "SELECT status, result_ref, finished_at, error, worker_id, lease_epoch "
                        "FROM invocation_attempts WHERE attempt_id = ?",
                        (receipt.evidence.attempt_id,),
                    ).fetchone()
                    self.assertEqual(attempt["status"], "succeeded")
                    self.assertEqual(attempt["result_ref"], receipt.evidence.result_ref)
                    self.assertEqual(attempt["finished_at"], receipt.evidence.accepted_at)
                    self.assertIsNone(attempt["error"])
                    self.assertEqual(attempt["worker_id"], receipt.evidence.worker_id)
                    self.assertEqual(attempt["lease_epoch"], receipt.evidence.lease_epoch)
                    for operation in (
                        lambda: copy.copy(completed),
                        lambda: copy.deepcopy(completed),
                        lambda: pickle.dumps(completed),
                    ):
                        with self.assertRaisesRegex(
                            TypeError,
                            "cannot be (copied|serialized)",
                        ):
                            operation()
        with self.assertRaisesRegex(RuntimeError, "no longer active"):
            completed._validated(token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN)

    def test_every_result_dml_boundary_rolls_back_the_complete_graph(self) -> None:
        """A post-DML failure cannot leave a result prefix or terminal CAS behind."""

        prepared = self.fresh_prepared()
        install_inactive_result_schema(self.store)
        self.store._clock = lambda: "2026-08-27T10:00:02.000000Z"
        table_names = (
            "events",
            "artifact_versions",
            "artifact_blobs",
            "invocation_result_manifests",
            "invocation_result_requests",
            "invocation_result_event_bindings",
            "invocation_result_receipts",
            "invocation_result_artifacts",
            "invocation_result_publications",
        )
        before_tables = {
            table: tuple(
                tuple(row) for row in self.store._connection.execute(f"SELECT * FROM {table}")
            )
            for table in table_names
        }
        before_jobs = tuple(
            tuple(row) for row in self.store._connection.execute("SELECT * FROM invocation_jobs")
        )
        before_attempts = tuple(
            tuple(row)
            for row in self.store._connection.execute("SELECT * FROM invocation_attempts")
        )

        def make_fault_patch(label: str, target: str, fail_after: int):
            calls = [0]
            if target == "event":
                original = SQLiteEventStore._insert_with_verified_envelope_in_transaction

                def injected(
                    *args: object,
                    _original=original,
                    _fail_after=fail_after,
                    _label=label,
                    **kwargs: object,
                ) -> object:
                    calls[0] += 1
                    result = _original(*args, **kwargs)  # type: ignore[arg-type]
                    if calls[0] == _fail_after:
                        raise RuntimeError(f"injected result DML fault: {_label}")
                    return result

                patch_target = patch.object(
                    SQLiteEventStore,
                    "_insert_with_verified_envelope_in_transaction",
                    side_effect=injected,
                )
            elif target == "artifact":
                original = result_artifact_transaction_module._guarded_execute

                def injected(
                    *args: object,
                    _original=original,
                    _fail_after=fail_after,
                    _label=label,
                    **kwargs: object,
                ) -> object:
                    sql = args[2]
                    is_artifact_insert = type(sql) is str and "INSERT INTO main.artifact_" in sql
                    if is_artifact_insert:
                        calls[0] += 1
                    result = _original(*args, **kwargs)  # type: ignore[arg-type]
                    if is_artifact_insert and calls[0] == _fail_after:
                        raise RuntimeError(f"injected result DML fault: {_label}")
                    return result

                patch_target = patch.object(
                    result_artifact_transaction_module,
                    "_guarded_execute",
                    side_effect=injected,
                )
            elif target == "row":
                original = SQLiteEventStore._insert_exact_result_acceptance_row_in_owner_transaction

                def injected(
                    *args: object,
                    _original=original,
                    _fail_after=fail_after,
                    _label=label,
                    **kwargs: object,
                ) -> object:
                    calls[0] += 1
                    result = _original(self.store, *args, **kwargs)  # type: ignore[arg-type]
                    if calls[0] == _fail_after:
                        raise RuntimeError(f"injected result DML fault: {_label}")
                    return result

                patch_target = patch.object(
                    SQLiteEventStore,
                    "_insert_exact_result_acceptance_row_in_owner_transaction",
                    side_effect=injected,
                )
            else:
                original = SQLiteEventStore._update_exact_result_acceptance_row_in_owner_transaction

                def injected(
                    *args: object,
                    _original=original,
                    _fail_after=fail_after,
                    _label=label,
                    **kwargs: object,
                ) -> object:
                    calls[0] += 1
                    result = _original(self.store, *args, **kwargs)  # type: ignore[arg-type]
                    if calls[0] == _fail_after:
                        raise RuntimeError(f"injected result DML fault: {_label}")
                    return result

                patch_target = patch.object(
                    SQLiteEventStore,
                    "_update_exact_result_acceptance_row_in_owner_transaction",
                    side_effect=injected,
                )
            return patch_target, calls

        fault_points = (
            ("result event", "event", 1),
            ("terminal event", "event", 2),
            ("Artifact blob", "artifact", 1),
            ("Artifact version", "artifact", 2),
            ("manifest", "row", 1),
            ("request", "row", 2),
            ("result binding", "row", 3),
            ("terminal binding", "row", 4),
            ("receipt", "row", 5),
            ("Artifact binding", "row", 6),
            ("job terminal CAS", "update", 1),
            ("attempt terminal CAS", "update", 2),
        )
        for ordinal, (label, target, fail_after) in enumerate(fault_points, start=1):
            with self.subTest(boundary=label):
                patch_target, calls = make_fault_patch(label, target, fail_after)

                with ExitStack() as stack:
                    stack.enter_context(
                        patch(
                            "quantum_entanglement.store.new_id",
                            side_effect=(
                                f"result_receipt_fault_{ordinal}",
                                f"event_result_fault_{ordinal}",
                                f"event_terminal_fault_{ordinal}",
                            ),
                        )
                    )
                    stack.enter_context(patch_target)
                    with self.assertRaisesRegex(RuntimeError, "injected result DML fault"):
                        with self.store._result_artifact_transaction() as handle:
                            complete = self.store._complete_result_acceptance_job_and_attempt_in_owner_transaction  # noqa: E501
                            with complete(handle, prepared):
                                self.fail("faulted result acceptance unexpectedly completed")

                self.assertEqual(calls[0], fail_after)
                for table in table_names:
                    self.assertEqual(
                        tuple(
                            tuple(row)
                            for row in self.store._connection.execute(f"SELECT * FROM {table}")
                        ),
                        before_tables[table],
                        table,
                    )
                self.assertEqual(
                    tuple(
                        tuple(row)
                        for row in self.store._connection.execute("SELECT * FROM invocation_jobs")
                    ),
                    before_jobs,
                )
                self.assertEqual(
                    tuple(
                        tuple(row)
                        for row in self.store._connection.execute(
                            "SELECT * FROM invocation_attempts"
                        )
                    ),
                    before_attempts,
                )

    def test_completion_readback_accepts_a_shared_preexisting_blob(self) -> None:
        prepared = self.fresh_prepared()
        candidate = prepared.request.artifact_candidates[0]
        self.store._connection.execute(
            """
            INSERT INTO artifact_blobs (digest, content, byte_size, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                candidate.blob_digest,
                sqlite3.Binary(candidate.content),
                candidate.byte_size,
                "2026-08-27T10:00:00.000000Z",
            ),
        )
        install_inactive_result_schema(self.store)
        self.store._clock = lambda: "2026-08-27T10:00:02.000000Z"
        with patch(
            "quantum_entanglement.store.new_id",
            side_effect=(
                "receipt_shared_blob",
                "event_result_shared_blob",
                "event_terminal_shared_blob",
            ),
        ):
            with self.store._result_artifact_transaction() as handle:
                with self.store._complete_result_acceptance_job_and_attempt_in_owner_transaction(
                    handle,
                    prepared,
                ) as completed:
                    self.assertIs(type(completed), _CompletedFreshResultAcceptancePlanV2)
        self.assertEqual(
            self.store._connection.execute("SELECT count(*) FROM artifact_blobs").fetchone()[0],
            1,
        )
        self.assertEqual(
            self.store._connection.execute("SELECT count(*) FROM artifact_versions").fetchone()[0],
            1,
        )

    def test_completion_readback_drift_rolls_back_the_entire_graph(self) -> None:
        prepared = self.fresh_prepared()
        install_inactive_result_schema(self.store)
        self.store._clock = lambda: "2026-08-27T10:00:02.000000Z"
        original = SQLiteEventStore._readback_result_acceptance_graph_body

        def tamper(
            store: SQLiteEventStore,
            connection: sqlite3.Connection,
            request: _PreparedScopedInvocationResultAcceptanceV2,
            receipt: ScopedInvocationResultReceiptV2,
        ) -> None:
            connection.execute(
                "UPDATE invocation_result_receipts SET receipt_digest = ? WHERE receipt_id = ?",
                ("0" * 64, receipt.receipt_id),
            )
            original(store, connection, request, receipt)

        with patch.object(SQLiteEventStore, "_readback_result_acceptance_graph_body", tamper):
            with patch(
                "quantum_entanglement.store.new_id",
                side_effect=(
                    "receipt_readback_drift",
                    "event_result_readback_drift",
                    "event_terminal_readback_drift",
                ),
            ):
                with self.assertRaises(_ResultAcceptanceQuarantineError) as captured:
                    with self.store._result_artifact_transaction() as handle:
                        complete = self.store._complete_result_acceptance_job_and_attempt_in_owner_transaction  # noqa: E501
                        with complete(handle, prepared):
                            self.fail("tampered result graph unexpectedly completed")
        self.assertIs(captured.exception.category, _ResultAcceptanceQuarantineCategory.DRIFT)
        self.assertEqual(captured.exception.code, "result_acceptance_graph_quarantined")
        self.assertNotIn(prepared.claimed.lease.lease_token, str(captured.exception))
        for table in (
            "invocation_result_manifests",
            "invocation_result_requests",
            "invocation_result_event_bindings",
            "invocation_result_receipts",
            "invocation_result_artifacts",
            "artifact_versions",
            "artifact_blobs",
        ):
            self.assertEqual(
                self.store._connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0],
                0,
            )
        job = self.store._connection.execute(
            "SELECT status FROM invocation_jobs WHERE invocation_id = ?",
            (prepared.request.manifest.invocation_id,),
        ).fetchone()
        self.assertEqual(job["status"], "running")

    def test_completion_readback_supports_a_narration_only_result(self) -> None:
        prepared = self.narration_only_prepared()
        install_inactive_result_schema(self.store)
        self.store._clock = lambda: "2026-08-27T10:00:02.000000Z"
        with patch(
            "quantum_entanglement.store.new_id",
            side_effect=(
                "receipt_narration_only",
                "event_result_narration_only",
                "event_terminal_narration_only",
            ),
        ):
            with self.store._result_artifact_transaction() as handle:
                with self.store._complete_result_acceptance_job_and_attempt_in_owner_transaction(
                    handle,
                    prepared,
                ) as completed:
                    self.assertIs(type(completed), _CompletedFreshResultAcceptancePlanV2)
        self.assertEqual(
            self.store._connection.execute(
                "SELECT count(*) FROM invocation_result_artifacts"
            ).fetchone()[0],
            0,
        )

    def test_existing_graph_completion_path_does_not_cas_job_or_attempt(self) -> None:
        helper = inactive_migration_module.InactiveInvocationResultsMigrationTests(
            methodName="runTest"
        )
        helper.store = self.store
        helper.connection = self.store._connection
        helper.seed_nonempty_v6_dependencies()
        helper.apply_candidate()
        helper.seed_complete_result_graph()
        prepared = existing_graph_prepared(helper)
        with patch.object(
            SQLiteEventStore,
            "_update_exact_result_acceptance_row_in_owner_transaction",
            side_effect=AssertionError("existing graph must not perform terminal CAS"),
        ) as update:
            with self.store._result_artifact_transaction() as handle:
                with self.store._complete_result_acceptance_job_and_attempt_in_owner_transaction(
                    handle,
                    prepared,
                ) as result:
                    self.assertIs(type(result), _ExistingResultAcceptanceGraphCandidateV2)
        update.assert_not_called()

    def test_job_cas_trigger_side_effect_rolls_back_complete_graph(self) -> None:
        prepared = self.fresh_prepared()
        install_inactive_result_schema(self.store)
        self.store._clock = lambda: "2026-08-27T10:00:02.000000Z"
        before_events = tuple(
            tuple(row) for row in self.store._connection.execute("SELECT * FROM events")
        )
        self.store._connection.execute(
            """
            CREATE TRIGGER result_job_cas_side_effect
            AFTER UPDATE OF status ON invocation_jobs
            WHEN NEW.invocation_id = 'invocation-scoped-store-1' AND NEW.status = 'succeeded'
            BEGIN
                UPDATE invocation_attempts SET error = 'unexpected'
                WHERE invocation_id = NEW.invocation_id AND status = 'running';
            END
            """
        )
        with patch(
            "quantum_entanglement.store.new_id",
            side_effect=(
                "receipt_cas_trigger",
                "event_result_cas_trigger",
                "event_terminal_cas_trigger",
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "changed an unexpected row count"):
                with self.store._result_artifact_transaction() as handle:
                    with (
                        self.store._complete_result_acceptance_job_and_attempt_in_owner_transaction(
                            handle,
                            prepared,
                        )
                    ):
                        self.fail("triggered job CAS unexpectedly yielded a plan")
        self.assertEqual(
            tuple(tuple(row) for row in self.store._connection.execute("SELECT * FROM events")),
            before_events,
        )
        for table in (
            "invocation_result_manifests",
            "invocation_result_requests",
            "invocation_result_event_bindings",
            "invocation_result_receipts",
            "invocation_result_artifacts",
        ):
            self.assertEqual(
                self.store._connection.execute(f"SELECT count(*) FROM main.{table}").fetchone()[0],
                0,
            )
        job = self.store._connection.execute(
            "SELECT status FROM invocation_jobs WHERE invocation_id = ?",
            (prepared.request.manifest.invocation_id,),
        ).fetchone()
        self.assertEqual(job["status"], "running")
        attempt = self.store._connection.execute(
            "SELECT status, error FROM invocation_attempts WHERE attempt_id = ?",
            (prepared.claimed.receipt.evidence.attempt_id,),
        ).fetchone()
        self.assertEqual(attempt["status"], "running")
        self.assertIsNone(attempt["error"])

    def test_caught_event_pair_failure_still_forces_owner_transaction_rollback(self) -> None:
        prepared = self.fresh_prepared()
        install_inactive_result_schema(self.store)
        self.store._clock = lambda: "2026-08-27T10:00:02.000000Z"
        with (
            patch(
                "quantum_entanglement.store.new_id",
                side_effect=(
                    "receipt_events_caught",
                    "event_result_events_caught",
                    "event_terminal_events_caught",
                ),
            ),
            patch(
                "quantum_entanglement.store._build_scoped_invocation_result_events_from_plan_v2",
                side_effect=RuntimeError("caught event pair failure"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "rollback-only"):
                with self.store._result_artifact_transaction() as handle:
                    with self.assertRaisesRegex(RuntimeError, "caught event pair failure"):
                        construct = (
                            self.store._construct_result_acceptance_event_pair_in_owner_transaction
                        )
                        with construct(handle, prepared):
                            self.fail("caught event failure unexpectedly yielded a plan")
        for table in ("artifact_versions", "artifact_blobs"):
            with self.subTest(table=table):
                self.assertEqual(
                    self.store._connection.execute(f"SELECT count(*) FROM main.{table}").fetchone()[
                        0
                    ],
                    0,
                )

    def test_event_stage_rejects_wrong_builder_outputs_and_rolls_back(self) -> None:
        prepared = self.fresh_prepared()
        install_inactive_result_schema(self.store)
        self.store._clock = lambda: "2026-08-27T10:00:02.000000Z"

        def drifted_pair(
            transitioned: _TransitionedFreshResultAcceptancePlanV2,
        ) -> tuple[DomainEvent, DomainEvent]:
            result_event, terminal_event = _build_scoped_invocation_result_events_from_plan_v2(
                transitioned
            )
            return replace(result_event, event_id="event-result-drifted"), terminal_event

        cases = (
            (
                "wrong_type",
                lambda _transitioned: (object(), object()),
                TypeError,
                "exact DomainEvent",
            ),
            (
                "binding_drift",
                drifted_pair,
                ValueError,
                "differs from its transitioned plan",
            ),
        )
        for ordinal, (label, builder, error_type, message) in enumerate(cases, start=1):
            with (
                self.subTest(case=label),
                patch(
                    "quantum_entanglement.store.new_id",
                    side_effect=(
                        f"receipt_events_bad_{ordinal}",
                        f"event_result_events_bad_{ordinal}",
                        f"event_terminal_events_bad_{ordinal}",
                    ),
                ),
                patch(
                    "quantum_entanglement.store._build_scoped_invocation_result_events_from_plan_v2",
                    side_effect=builder,
                ),
            ):
                with self.assertRaisesRegex(error_type, message):
                    with self.store._result_artifact_transaction() as handle:
                        construct = (
                            self.store._construct_result_acceptance_event_pair_in_owner_transaction
                        )
                        with construct(handle, prepared):
                            self.fail("invalid event builder output unexpectedly yielded")
            for table in ("artifact_versions", "artifact_blobs"):
                self.assertEqual(
                    self.store._connection.execute(f"SELECT count(*) FROM main.{table}").fetchone()[
                        0
                    ],
                    0,
                )

    def test_existing_graph_evidence_path_does_not_construct_fresh_payload(self) -> None:
        helper = inactive_migration_module.InactiveInvocationResultsMigrationTests(
            methodName="runTest"
        )
        helper.store = self.store
        helper.connection = self.store._connection
        helper.seed_nonempty_v6_dependencies()
        helper.apply_candidate()
        helper.seed_complete_result_graph()
        prepared = existing_graph_prepared(helper)

        with patch(
            "quantum_entanglement.store._build_scoped_invocation_result_evidence_v2",
            side_effect=AssertionError("existing graph must not construct fresh evidence"),
        ) as builder:
            with self.store._result_artifact_transaction() as handle:
                with self.store._construct_result_acceptance_evidence_in_owner_transaction(
                    handle,
                    prepared,
                ) as result:
                    self.assertIs(type(result), _ExistingResultAcceptanceGraphCandidateV2)
        builder.assert_not_called()

    def test_evidence_construction_failure_rolls_back_materialized_artifacts(self) -> None:
        prepared = self.fresh_prepared()
        install_inactive_result_schema(self.store)
        self.store._clock = lambda: "2026-08-27T10:00:02.000000Z"
        with (
            patch(
                "quantum_entanglement.store.new_id",
                side_effect=("receipt_failure", "event_result_failure", "event_terminal_failure"),
            ),
            patch(
                "quantum_entanglement.store._build_scoped_invocation_result_evidence_v2",
                side_effect=RuntimeError("evidence construction failed"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "evidence construction failed"):
                with self.store._result_artifact_transaction() as handle:
                    with self.store._construct_result_acceptance_evidence_in_owner_transaction(
                        handle,
                        prepared,
                    ):
                        self.fail("failed evidence construction unexpectedly yielded a plan")
        self.assertEqual(
            self.store._connection.execute(
                "SELECT count(*) FROM main.artifact_versions"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.store._connection.execute("SELECT count(*) FROM main.artifact_blobs").fetchone()[
                0
            ],
            0,
        )

    def test_caught_evidence_failure_still_forces_owner_transaction_rollback(self) -> None:
        prepared = self.fresh_prepared()
        install_inactive_result_schema(self.store)
        self.store._clock = lambda: "2026-08-27T10:00:02.000000Z"
        with (
            patch(
                "quantum_entanglement.store.new_id",
                side_effect=("receipt_caught", "event_result_caught", "event_terminal_caught"),
            ),
            patch(
                "quantum_entanglement.store._build_scoped_invocation_result_evidence_v2",
                side_effect=RuntimeError("caught evidence failure"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "rollback-only"):
                with self.store._result_artifact_transaction() as handle:
                    with self.assertRaisesRegex(RuntimeError, "caught evidence failure"):
                        with self.store._construct_result_acceptance_evidence_in_owner_transaction(
                            handle,
                            prepared,
                        ):
                            self.fail("caught evidence failure unexpectedly yielded a plan")
        self.assertEqual(
            self.store._connection.execute(
                "SELECT count(*) FROM main.artifact_versions"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.store._connection.execute("SELECT count(*) FROM main.artifact_blobs").fetchone()[
                0
            ],
            0,
        )

    def assert_materialization_time_rejected(
        self,
        clock_value: str,
        error_type: type[Exception],
        message: str,
    ) -> None:
        prepared = self.fresh_prepared()
        install_inactive_result_schema(self.store)
        calls = []

        def clock() -> str:
            calls.append(clock_value)
            return clock_value

        self.store._clock = clock
        with self.assertRaisesRegex(error_type, message):
            with self.store._result_artifact_transaction() as handle:
                with self.store._materialize_result_acceptance_artifacts_in_owner_transaction(
                    handle,
                    prepared,
                ):
                    self.fail("invalid acceptedAt unexpectedly materialized Artifacts")
        self.assertEqual(calls, [clock_value])
        self.assertEqual(
            self.store._connection.execute(
                "SELECT count(*) FROM main.artifact_versions"
            ).fetchone()[0],
            0,
        )

    def test_artifact_materialization_rejects_expired_clock(self) -> None:
        self.assert_materialization_time_rejected(
            "2026-08-27T10:01:01.000000Z",
            _ResultAcceptanceConflictError,
            "expired",
        )

    def test_artifact_materialization_rejects_regressing_clock(self) -> None:
        self.assert_materialization_time_rejected(
            "2026-08-27T10:00:00.999999Z",
            _ResultAcceptanceIntegrityError,
            "precedes",
        )

    def test_artifact_materialization_cannot_resample_after_failure_is_caught(self) -> None:
        prepared = self.fresh_prepared()
        install_inactive_result_schema(self.store)
        calls = []

        def expired_clock() -> str:
            calls.append(prepared.claimed.lease.lease_expires_at)
            return prepared.claimed.lease.lease_expires_at

        self.store._clock = expired_clock
        with self.assertRaisesRegex(RuntimeError, "rollback-only"):
            with self.store._result_artifact_transaction() as handle:
                with self.store._preflight_result_acceptance_write_in_owner_transaction(
                    handle,
                    prepared,
                ) as plan:
                    assert type(plan) is _FreshResultAcceptanceWritePlanV2
                    with self.assertRaises(_ResultAcceptanceConflictError):
                        self.store._consume_result_acceptance_artifact_plan_in_owner_transaction(
                            handle,
                            plan,
                        )
                    with self.assertRaisesRegex(RuntimeError, "already started"):
                        self.store._consume_result_acceptance_artifact_plan_in_owner_transaction(
                            handle,
                            plan,
                        )
        self.assertEqual(calls, [prepared.claimed.lease.lease_expires_at])

    def test_post_clock_durable_drift_rolls_back_materialized_artifacts(self) -> None:
        prepared = self.fresh_prepared()
        install_inactive_result_schema(self.store)
        self.store._clock = lambda: "2026-08-27T10:00:02.000000Z"
        original = self.store._validate_result_acceptance_durable_prerequisites_in_transaction
        validations = []

        def validate(
            connection: sqlite3.Connection,
            frozen: _PreparedScopedInvocationResultAcceptanceV2,
        ) -> object:
            validations.append(connection.total_changes)
            if len(validations) == 2:
                connection.execute(
                    """
                    UPDATE invocation_jobs
                    SET heartbeat_at = ?, updated_at = ?
                    WHERE invocation_id = ?
                    """,
                    (
                        "2026-08-27T10:00:01.500000Z",
                        "2026-08-27T10:00:01.500000Z",
                        prepared.request.manifest.invocation_id,
                    ),
                )
            return original(connection, frozen)

        with patch.object(
            self.store,
            "_validate_result_acceptance_durable_prerequisites_in_transaction",
            side_effect=validate,
        ):
            with self.assertRaises(_ResultAcceptanceConflictError):
                with self.store._result_artifact_transaction() as handle:
                    with self.store._materialize_result_acceptance_artifacts_in_owner_transaction(
                        handle,
                        prepared,
                    ):
                        self.fail("durable drift unexpectedly produced a materialized plan")
        self.assertEqual(len(validations), 2)
        self.assertEqual(
            self.store._connection.execute(
                "SELECT count(*) FROM main.artifact_versions"
            ).fetchone()[0],
            0,
        )

    def test_private_prerequisites_add_no_writer_or_accepted_export(self) -> None:
        for name in (
            "_ExistingResultAcceptanceGraphCandidateV2",
            "_EvidencedFreshResultAcceptancePlanV2",
            "_EventedFreshResultAcceptancePlanV2",
            "_FreshResultAcceptancePrerequisitesV2",
            "_FreshResultAcceptanceWritePlanV2",
            "_IdentifiedFreshResultAcceptancePlanV2",
            "_MaterializedFreshResultAcceptancePlanV2",
            "_validate_result_acceptance_durable_prerequisites_in_transaction",
            "_build_scoped_invocation_result_evidence_v2",
            "_TransitionedFreshResultAcceptancePlanV2",
            "_ResultAcceptanceQuarantineCategory",
            "_ResultAcceptanceQuarantineError",
            "_build_scoped_invocation_result_terminal_transition_from_plan_v2",
            "_build_scoped_invocation_result_events_from_plan_v2",
            "_construct_result_acceptance_event_pair_in_owner_transaction",
            "_ReceiptedFreshResultAcceptancePlanV2",
            "_PersistedFreshResultAcceptancePlanV2",
            "_CompletedFreshResultAcceptancePlanV2",
            "_build_scoped_invocation_result_receipt_v2",
            "_construct_result_acceptance_receipt_in_owner_transaction",
            "_persist_result_acceptance_graph_in_owner_transaction",
            "_complete_result_acceptance_job_and_attempt_in_owner_transaction",
            "_construct_result_acceptance_terminal_transition_in_owner_transaction",
            "accept_scoped_invocation_result_v2",
            "ScopedInvocationResultAcceptedV2",
        ):
            with self.subTest(name=name):
                self.assertNotIn(name, quantum_entanglement.__all__)
                self.assertFalse(hasattr(quantum_entanglement, name))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
