from __future__ import annotations

import copy
import hashlib
import pickle
import sqlite3
import traceback
import unittest
from dataclasses import replace
from unittest.mock import patch

import quantum_entanglement
from quantum_entanglement._result_acceptance import (
    _RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN,
    _ExistingResultAcceptanceGraphCandidateV2,
    _FreshResultAcceptancePrerequisitesV2,
    _FreshResultAcceptanceWritePlanV2,
    _IdentifiedFreshResultAcceptancePlanV2,
    _MaterializedFreshResultAcceptancePlanV2,
    _prepare_scoped_invocation_result_acceptance_v2,
    _PreparedScopedInvocationResultAcceptanceV2,
    _ResultAcceptanceConflictError,
    _ResultAcceptanceIntegrityError,
    _ResultAcceptanceSchemaUnavailableError,
)
from quantum_entanglement._result_artifact_transaction import (
    _ResultArtifactConflictError,
    _ResultArtifactTransactionContinuityError,
)
from quantum_entanglement.attempts import InvocationLease
from quantum_entanglement.invocation_execution import (
    EffectClass,
    ScopedInvocationStartClaimedV3,
)
from quantum_entanglement.invocation_results import (
    EMPTY_ACTION_RECEIPT_SET_DIGEST,
    SCOPED_INVOCATION_RESULT_ACCEPTANCE_REQUEST_SCHEMA_VERSION,
    SCOPED_INVOCATION_RESULT_MANIFEST_SCHEMA_VERSION,
    ScopedInvocationResultAcceptanceRequestV2,
    ScopedInvocationResultArtifactCandidateV2,
    ScopedInvocationResultManifestV2,
)
from quantum_entanglement.store import SQLiteEventStore
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
            with self.assertRaises(_ResultAcceptanceIntegrityError):
                self.validate(prepared)

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
            with self.assertRaises(_ResultAcceptanceIntegrityError):
                self.validate(prepared)
        fresh_load.assert_not_called()

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
            "_FreshResultAcceptancePrerequisitesV2",
            "_FreshResultAcceptanceWritePlanV2",
            "_IdentifiedFreshResultAcceptancePlanV2",
            "_MaterializedFreshResultAcceptancePlanV2",
            "_validate_result_acceptance_durable_prerequisites_in_transaction",
            "accept_scoped_invocation_result_v2",
            "ScopedInvocationResultAcceptedV2",
        ):
            with self.subTest(name=name):
                self.assertNotIn(name, quantum_entanglement.__all__)
                self.assertFalse(hasattr(quantum_entanglement, name))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
