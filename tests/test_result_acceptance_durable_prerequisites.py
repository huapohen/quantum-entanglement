from __future__ import annotations

import copy
import hashlib
import pickle
import sqlite3
import unittest
from dataclasses import replace
from unittest.mock import patch

import quantum_entanglement
from quantum_entanglement._result_acceptance import (
    _RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN,
    _ExistingResultAcceptanceGraphCandidateV2,
    _FreshResultAcceptancePrerequisitesV2,
    _FreshResultAcceptanceWritePlanV2,
    _prepare_scoped_invocation_result_acceptance_v2,
    _PreparedScopedInvocationResultAcceptanceV2,
    _ResultAcceptanceConflictError,
    _ResultAcceptanceIntegrityError,
    _ResultAcceptanceSchemaUnavailableError,
)
from quantum_entanglement._result_artifact_transaction import (
    _ResultArtifactConflictError,
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

    def test_private_prerequisites_add_no_writer_or_accepted_export(self) -> None:
        for name in (
            "_ExistingResultAcceptanceGraphCandidateV2",
            "_FreshResultAcceptancePrerequisitesV2",
            "_FreshResultAcceptanceWritePlanV2",
            "_validate_result_acceptance_durable_prerequisites_in_transaction",
            "accept_scoped_invocation_result_v2",
            "ScopedInvocationResultAcceptedV2",
        ):
            with self.subTest(name=name):
                self.assertNotIn(name, quantum_entanglement.__all__)
                self.assertFalse(hasattr(quantum_entanglement, name))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
