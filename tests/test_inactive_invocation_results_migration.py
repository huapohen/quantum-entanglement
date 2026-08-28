from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from collections import Counter
from dataclasses import replace
from pathlib import Path

import quantum_entanglement
from quantum_entanglement import _stored_event_envelope_codec as envelope_codec
from quantum_entanglement._inactive_invocation_results_migration import (
    _INACTIVE_INVOCATION_RESULTS_CANDIDATE,
    _INACTIVE_INVOCATION_RESULTS_DESCRIPTOR,
    _INACTIVE_INVOCATION_RESULTS_MIGRATIONS,
    _KNOWN_INVOCATION_RESULTS_DOMAIN_REGISTRY,
    _KNOWN_INVOCATION_RESULTS_MIGRATIONS,
    _InactiveInvocationResultsCandidate,
)
from quantum_entanglement.domain_migrations import (
    DOMAIN_MIGRATION_REGISTRY,
    LEGACY_DOMAIN_MIGRATIONS,
    bootstrap_legacy_domain_migration_metadata,
    install_domain_migration_sidecar,
)
from quantum_entanglement.invocation_execution import (
    CANONICAL_ORCHESTRATOR_ACTOR_ID,
    EffectClass,
)
from quantum_entanglement.invocation_results import (
    EMPTY_ACTION_RECEIPT_SET_DIGEST,
    SCOPED_INVOCATION_RESULT_ACCEPTANCE_REQUEST_SCHEMA_VERSION,
    SCOPED_INVOCATION_RESULT_MANIFEST_SCHEMA_VERSION,
    ScopedInvocationResultAcceptanceRequestV2,
    ScopedInvocationResultArtifactCandidateV2,
    ScopedInvocationResultEventCoordinatesV2,
    ScopedInvocationResultEvidenceV2,
    ScopedInvocationResultManifestV2,
    ScopedInvocationResultTerminalTransitionV2,
    build_scoped_invocation_result_receipt_v2,
    build_scoped_invocation_result_terminal_transition_v2,
    scoped_invocation_start_receipt_digest_v3,
)
from quantum_entanglement.migrations import (
    MIGRATIONS,
    MigrationVersionError,
    _sql_statements,
    apply_sqlite_migrations,
    current_schema_version,
    migration_text,
    validate_sqlite_schema,
)
from quantum_entanglement.store import SQLiteEventStore
from tests.test_invocation_result_terminal_transition import evidence_for
from tests.test_scoped_invocation_execution import valid_scoped_start_receipt

_RESULT_TABLES = (
    "invocation_result_artifacts",
    "invocation_result_event_bindings",
    "invocation_result_manifests",
    "invocation_result_publications",
    "invocation_result_receipts",
    "invocation_result_requests",
)

_CANDIDATE_INDEXES = (
    "idx_invocation_result_artifacts_reverse",
    "idx_invocation_result_publications_trigger",
    "idx_invocation_result_receipts_attempt",
    "idx_invocation_result_receipts_manifest",
    "idx_invocation_result_receipts_request",
    "idx_invocation_result_receipts_scope",
    "idx_invocation_result_requests_manifest",
    "idx_invocation_result_requests_scope",
)

_RESULT_COLUMNS = {
    "invocation_result_manifests": (
        "tenant_id",
        "workspace_id",
        "manifest_digest",
        "schema_version",
        "canonical_bytes",
        "byte_size",
        "created_at",
    ),
    "invocation_result_requests": (
        "tenant_id",
        "workspace_id",
        "request_digest",
        "schema_version",
        "acceptance_idempotency_key",
        "request_identity_bytes",
        "request_identity_byte_size",
        "invocation_id",
        "session_id",
        "plan_id",
        "task_id",
        "agent_id",
        "job_idempotency_key",
        "start_receipt_digest",
        "execution_manifest_digest",
        "result_manifest_digest",
        "expected_stream_version",
        "running_task_revision",
        "terminal_task_revision",
        "correlation_id",
        "causation_id",
        "runtime_revision",
        "effect_class",
        "action_receipt_set_digest",
        "result_ref",
        "primary_artifact_id",
        "artifact_count",
        "created_at",
    ),
    "invocation_result_event_bindings": (
        "tenant_id",
        "workspace_id",
        "receipt_id",
        "event_role",
        "event_id",
        "event_type",
        "global_position",
    ),
    "invocation_result_receipts": (
        "tenant_id",
        "workspace_id",
        "receipt_id",
        "schema_version",
        "request_digest",
        "invocation_id",
        "session_id",
        "plan_id",
        "task_id",
        "agent_id",
        "job_idempotency_key",
        "acceptance_idempotency_key",
        "attempt_id",
        "attempt_number",
        "lease_epoch",
        "worker_id",
        "lease_token_digest",
        "start_receipt_digest",
        "execution_manifest_digest",
        "result_manifest_schema_version",
        "result_manifest_digest",
        "result_ref",
        "effect_class",
        "action_receipt_set_digest",
        "expected_stream_version",
        "running_task_revision",
        "terminal_task_revision",
        "accepted_at",
        "artifact_count",
        "result_evidence_digest",
        "terminal_transition_digest",
        "receipt_digest",
        "result_event_id",
        "result_event_stream_id",
        "result_event_type",
        "result_event_timestamp",
        "result_event_sequence",
        "result_event_global_position",
        "result_event_envelope_digest",
        "terminal_event_id",
        "terminal_event_stream_id",
        "terminal_event_type",
        "terminal_event_timestamp",
        "terminal_event_sequence",
        "terminal_event_global_position",
        "terminal_event_envelope_digest",
    ),
    "invocation_result_artifacts": (
        "tenant_id",
        "workspace_id",
        "receipt_id",
        "ordinal",
        "session_id",
        "task_id",
        "artifact_id",
        "name",
        "version",
        "parent_version",
        "media_type",
        "blob_digest",
        "byte_size",
        "metadata_digest",
        "created_by",
        "idempotency_key",
        "artifact_request_digest",
        "candidate_digest",
    ),
    "invocation_result_publications": (
        "tenant_id",
        "workspace_id",
        "receipt_id",
        "publication_kind",
        "message_id",
        "destination",
        "idempotency_key",
        "payload_digest",
        "headers_digest",
        "triggering_event_id",
        "triggering_global_position",
        "created_at",
    ),
}


class InactiveInvocationResultsMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tempdir.name) / "event-store.sqlite3")
        self.store = SQLiteEventStore(self.path)
        self.connection = self.store._connection

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def apply_candidate(self) -> None:
        recorded_at = "2026-08-29T00:00:00Z"
        install_domain_migration_sidecar(self.connection)
        bootstrap_legacy_domain_migration_metadata(
            self.connection,
            clock=lambda: recorded_at,
        )
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            for statement in _sql_statements(
                migration_text(_INACTIVE_INVOCATION_RESULTS_DESCRIPTOR.filename)
            ):
                self.connection.execute(statement)
            self.connection.execute(
                """
                INSERT INTO qe_schema_migrations (
                    version,
                    filename,
                    sha256,
                    applied_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    7,
                    _INACTIVE_INVOCATION_RESULTS_DESCRIPTOR.filename,
                    _INACTIVE_INVOCATION_RESULTS_DESCRIPTOR.sql_sha256,
                    recorded_at,
                ),
            )
            self.connection.execute(
                """
                INSERT INTO qe_schema_migration_metadata (
                    migration_version,
                    domain,
                    domain_version,
                    metadata_kind,
                    descriptor_sha256,
                    owned_schema_sha256,
                    recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    7,
                    "invocation_results",
                    1,
                    "native",
                    _INACTIVE_INVOCATION_RESULTS_DESCRIPTOR.descriptor_sha256,
                    _INACTIVE_INVOCATION_RESULTS_DESCRIPTOR.owned_object_manifest_sha256,
                    recorded_at,
                ),
            )
            for dependency in (1, 2, 4):
                self.connection.execute(
                    """
                    INSERT INTO qe_schema_migration_dependencies (
                        migration_version,
                        depends_on_version
                    ) VALUES (?, ?)
                    """,
                    (7, dependency),
                )
            self.assertEqual(
                validate_sqlite_schema(
                    self.connection,
                    migrations=_KNOWN_INVOCATION_RESULTS_MIGRATIONS,
                ),
                7,
            )
            self.connection.execute("COMMIT")
        except BaseException:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise

    def run_down(self) -> None:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            for statement in _sql_statements(migration_text("0007_invocation_results.down.sql")):
                self.connection.execute(statement)
            self.connection.execute("COMMIT")
        except BaseException:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise

    def exact_result_graph(self) -> dict[str, object]:
        created_at = "2026-08-29T00:01:00.000000Z"
        start = valid_scoped_start_receipt()
        start_evidence = replace(
            start.evidence,
            tenant_id="tenant-1",
            workspace_id="workspace-1",
            invocation_id="invocation-1",
            session_id="session-1",
            plan_id="plan-1",
            task_id="task-1",
            agent_id="agent-1",
            job_idempotency_key="job-key-1",
            attempt_id="attempt-1",
            attempt_number=1,
            lease_epoch=1,
            worker_id="worker-1",
            lease_token_digest="3" * 64,
            claimed_at="2026-08-29T00:00:00.000000Z",
            lease_expires_at="2026-08-29T00:10:00.000000Z",
            manifest_digest="2" * 64,
            runtime_revision="runtime-1",
            correlation_id="correlation-1",
            causation_id="task-1",
        )
        start = replace(
            start,
            event_id="event-start-1",
            stream_id="session:session-1",
            sequence=1,
            global_position=1,
            evidence=start_evidence,
        )
        candidate = ScopedInvocationResultArtifactCandidateV2.from_content_metadata(
            tenant_id="tenant-1",
            workspace_id="workspace-1",
            session_id="session-1",
            task_id="task-1",
            artifact_id="artifact-1",
            name="result.md",
            media_type="text/markdown",
            content=b"x",
            metadata={},
            created_by="agent-1",
            idempotency_key="artifact-key-1",
            expected_head_version=0,
        )
        manifest = ScopedInvocationResultManifestV2(
            schema_version=SCOPED_INVOCATION_RESULT_MANIFEST_SCHEMA_VERSION,
            tenant_id="tenant-1",
            workspace_id="workspace-1",
            invocation_id="invocation-1",
            session_id="session-1",
            plan_id="plan-1",
            task_id="task-1",
            agent_id="agent-1",
            job_idempotency_key="job-key-1",
            task_revision=2,
            correlation_id="correlation-1",
            causation_id="task-1",
            runtime_revision="runtime-1",
            execution_manifest_digest="2" * 64,
            effect_class=EffectClass.PURE,
            action_receipt_set_digest=EMPTY_ACTION_RECEIPT_SET_DIGEST,
            result_ref="result-1",
            narration="canonical result",
            metadata={"provider": "fake"},
            primary_artifact_id="artifact-1",
            artifacts=(candidate.to_descriptor(),),
        )
        request = ScopedInvocationResultAcceptanceRequestV2(
            schema_version=SCOPED_INVOCATION_RESULT_ACCEPTANCE_REQUEST_SCHEMA_VERSION,
            acceptance_idempotency_key="accept-key-1",
            start_receipt=start,
            manifest=manifest,
            artifact_candidates=(candidate,),
            expected_stream_version=1,
        )
        evidence = evidence_for(
            request,
            receipt_id="receipt-1",
            accepted_at=created_at,
        )
        transition = build_scoped_invocation_result_terminal_transition_v2(
            request,
            evidence,
            result_event_id="event-result-1",
        )
        result_payload = evidence.canonical_bytes().decode("utf-8")
        terminal_payload = transition.canonical_bytes().decode("utf-8")
        result_envelope = envelope_codec._stored_event_envelope_from_values(
            event_id="event-result-1",
            stream_id="session:session-1",
            event_type="task.invocation.result.accepted",
            actor_id=CANONICAL_ORCHESTRATOR_ACTOR_ID,
            timestamp=created_at,
            correlation_id="correlation-1",
            causation_id="event-start-1",
            idempotency_key="accept-key-1",
            payload_json=result_payload,
            sequence=2,
            global_position=2,
        )
        terminal_envelope = envelope_codec._stored_event_envelope_from_values(
            event_id="event-terminal-1",
            stream_id="session:session-1",
            event_type="task.status.changed",
            actor_id=transition.actor_id,
            timestamp=created_at,
            correlation_id=transition.correlation_id,
            causation_id=transition.causation_id,
            idempotency_key=transition.idempotency_key,
            payload_json=terminal_payload,
            sequence=3,
            global_position=3,
        )
        result_coordinates = ScopedInvocationResultEventCoordinatesV2(
            event_id="event-result-1",
            stream_id="session:session-1",
            event_type="task.invocation.result.accepted",
            sequence=2,
            global_position=2,
            event_envelope_digest=result_envelope.digest(),
        )
        terminal_coordinates = ScopedInvocationResultEventCoordinatesV2(
            event_id="event-terminal-1",
            stream_id="session:session-1",
            event_type="task.status.changed",
            sequence=3,
            global_position=3,
            event_envelope_digest=terminal_envelope.digest(),
        )
        receipt = build_scoped_invocation_result_receipt_v2(
            request,
            evidence,
            result_event=result_coordinates,
            terminal_event=terminal_coordinates,
            terminal_transition=transition,
        )
        request_identity_bytes = json.dumps(
            ScopedInvocationResultAcceptanceRequestV2._identity_dict(request),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        publication_payload = json.dumps(
            {
                "receiptDigest": receipt.receipt_digest,
                "receiptId": receipt.receipt_id,
            },
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        publication_headers = '{"contentType":"application/json"}'
        return {
            "candidate": candidate,
            "created_at": created_at,
            "evidence": evidence,
            "manifest": manifest,
            "publication_headers": publication_headers,
            "publication_payload": publication_payload,
            "receipt": receipt,
            "request": request,
            "request_identity_bytes": request_identity_bytes,
            "result_envelope": result_envelope,
            "result_payload": result_payload,
            "terminal_envelope": terminal_envelope,
            "terminal_payload": terminal_payload,
            "transition": transition,
        }

    def seed_nonempty_v6_dependencies(self) -> None:
        graph = self.exact_result_graph()
        request = graph["request"]
        candidate = graph["candidate"]
        assert type(request) is ScopedInvocationResultAcceptanceRequestV2
        assert type(candidate) is ScopedInvocationResultArtifactCandidateV2
        start = request.start_receipt
        start_evidence = start.evidence
        available_at = start_evidence.claimed_at
        lease_expires_at = start_evidence.lease_expires_at
        start_payload = json.dumps(
            start_evidence.to_dict(),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        self.connection.execute(
            """
            INSERT INTO events (
                global_position,
                stream_id,
                sequence,
                event_id,
                event_type,
                actor_id,
                timestamp,
                payload_json,
                correlation_id,
                causation_id,
                idempotency_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                start.global_position,
                start.stream_id,
                start.sequence,
                start.event_id,
                "task.invocation.started",
                CANONICAL_ORCHESTRATOR_ACTOR_ID,
                available_at,
                start_payload,
                start_evidence.correlation_id,
                start_evidence.causation_id,
                "start-key-1",
            ),
        )
        self.connection.execute(
            """
            INSERT INTO invocation_jobs (
                invocation_id,
                session_id,
                plan_id,
                task_id,
                agent_id,
                idempotency_key,
                payload_digest,
                priority,
                status,
                max_attempts,
                attempts_started,
                lease_epoch,
                requested_available_at,
                available_at,
                created_at,
                updated_at,
                lease_owner,
                lease_token_digest,
                lease_expires_at,
                heartbeat_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                start_evidence.invocation_id,
                start_evidence.session_id,
                start_evidence.plan_id,
                start_evidence.task_id,
                start_evidence.agent_id,
                start_evidence.job_idempotency_key,
                start_evidence.manifest_digest,
                50,
                "running",
                3,
                1,
                1,
                None,
                available_at,
                available_at,
                available_at,
                start_evidence.worker_id,
                start_evidence.lease_token_digest,
                lease_expires_at,
                available_at,
            ),
        )
        self.connection.execute(
            """
            INSERT INTO invocation_attempts (
                attempt_id,
                invocation_id,
                attempt_number,
                lease_epoch,
                worker_id,
                lease_token_digest,
                status,
                started_at,
                heartbeat_at,
                lease_expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                start_evidence.attempt_id,
                start_evidence.invocation_id,
                start_evidence.attempt_number,
                start_evidence.lease_epoch,
                start_evidence.worker_id,
                start_evidence.lease_token_digest,
                "running",
                available_at,
                available_at,
                lease_expires_at,
            ),
        )
        self.connection.execute(
            """
            INSERT INTO invocation_admissions (
                invocation_id,
                receipt_format,
                session_id,
                task_id,
                stream_id,
                job_idempotency_key,
                original_version,
                event_count,
                event_ids_json,
                first_sequence,
                last_sequence,
                first_global_position,
                last_global_position,
                event_manifest_sha256,
                job_binding_sha256,
                admitted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                start_evidence.invocation_id,
                "qe.invocation-admission-receipt/1",
                start_evidence.session_id,
                start_evidence.task_id,
                start.stream_id,
                start_evidence.job_idempotency_key,
                start.sequence - 1,
                1,
                json.dumps([start.event_id], separators=(",", ":")),
                start.sequence,
                start.sequence,
                start.global_position,
                start.global_position,
                hashlib.sha256(start.event_id.encode("utf-8")).hexdigest(),
                hashlib.sha256(start_evidence.invocation_id.encode("utf-8")).hexdigest(),
                available_at,
            ),
        )
        self.connection.execute(
            """
            INSERT INTO artifact_blobs (
                digest,
                content,
                byte_size,
                created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (
                candidate.blob_digest,
                sqlite3.Binary(candidate.content),
                candidate.byte_size,
                available_at,
            ),
        )
        self.connection.execute(
            """
            INSERT INTO artifact_versions (
                artifact_id,
                tenant_id,
                workspace_id,
                session_id,
                task_id,
                name,
                version,
                parent_version,
                media_type,
                blob_digest,
                byte_size,
                metadata_json,
                created_by,
                created_at,
                idempotency_key,
                request_digest
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate.artifact_id,
                candidate.tenant_id,
                candidate.workspace_id,
                candidate.session_id,
                candidate.task_id,
                candidate.name,
                candidate.version,
                candidate.parent_version,
                candidate.media_type,
                candidate.blob_digest,
                candidate.byte_size,
                candidate.metadata_canonical_bytes.decode("utf-8"),
                candidate.created_by,
                available_at,
                candidate.idempotency_key,
                candidate.artifact_request_digest,
            ),
        )

    def seed_complete_result_graph(self) -> None:
        graph = self.exact_result_graph()
        request = graph["request"]
        evidence = graph["evidence"]
        transition = graph["transition"]
        receipt = graph["receipt"]
        candidate = graph["candidate"]
        request_identity_bytes = graph["request_identity_bytes"]
        assert type(request) is ScopedInvocationResultAcceptanceRequestV2
        assert type(candidate) is ScopedInvocationResultArtifactCandidateV2
        assert type(request_identity_bytes) is bytes
        manifest = request.manifest
        accepted_at = evidence.accepted_at
        manifest_bytes = manifest.canonical_bytes()
        self.connection.execute(
            """
            INSERT INTO invocation_result_manifests (
                tenant_id,
                workspace_id,
                manifest_digest,
                schema_version,
                canonical_bytes,
                byte_size,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                manifest.tenant_id,
                manifest.workspace_id,
                manifest.canonical_digest(),
                manifest.schema_version,
                sqlite3.Binary(manifest_bytes),
                len(manifest_bytes),
                accepted_at,
            ),
        )
        self.connection.execute(
            """
            INSERT INTO invocation_result_requests (
                tenant_id,
                workspace_id,
                request_digest,
                schema_version,
                acceptance_idempotency_key,
                request_identity_bytes,
                request_identity_byte_size,
                invocation_id,
                session_id,
                plan_id,
                task_id,
                agent_id,
                job_idempotency_key,
                start_receipt_digest,
                execution_manifest_digest,
                result_manifest_digest,
                expected_stream_version,
                running_task_revision,
                terminal_task_revision,
                correlation_id,
                causation_id,
                runtime_revision,
                effect_class,
                action_receipt_set_digest,
                result_ref,
                primary_artifact_id,
                artifact_count,
                created_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                manifest.tenant_id,
                manifest.workspace_id,
                request.canonical_digest(),
                request.schema_version,
                request.acceptance_idempotency_key,
                sqlite3.Binary(request_identity_bytes),
                len(request_identity_bytes),
                manifest.invocation_id,
                manifest.session_id,
                manifest.plan_id,
                manifest.task_id,
                manifest.agent_id,
                manifest.job_idempotency_key,
                scoped_invocation_start_receipt_digest_v3(request.start_receipt),
                manifest.execution_manifest_digest,
                manifest.canonical_digest(),
                request.expected_stream_version,
                manifest.task_revision,
                manifest.task_revision + 1,
                manifest.correlation_id,
                manifest.causation_id,
                manifest.runtime_revision,
                manifest.effect_class.value,
                manifest.action_receipt_set_digest,
                manifest.result_ref,
                manifest.primary_artifact_id,
                len(manifest.artifacts),
                accepted_at,
            ),
        )
        for values in (
            (
                2,
                "session:session-1",
                2,
                "event-result-1",
                "task.invocation.result.accepted",
                CANONICAL_ORCHESTRATOR_ACTOR_ID,
                accepted_at,
                graph["result_payload"],
                manifest.correlation_id,
                request.start_receipt.event_id,
                request.acceptance_idempotency_key,
            ),
            (
                3,
                "session:session-1",
                3,
                "event-terminal-1",
                "task.status.changed",
                transition.actor_id,
                accepted_at,
                graph["terminal_payload"],
                transition.correlation_id,
                transition.causation_id,
                transition.idempotency_key,
            ),
        ):
            self.connection.execute(
                """
                INSERT INTO events (
                    global_position,
                    stream_id,
                    sequence,
                    event_id,
                    event_type,
                    actor_id,
                    timestamp,
                    payload_json,
                    correlation_id,
                    causation_id,
                    idempotency_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
        self.connection.executemany(
            """
            INSERT INTO invocation_result_event_bindings (
                tenant_id,
                workspace_id,
                receipt_id,
                event_role,
                event_id,
                event_type,
                global_position
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    evidence.tenant_id,
                    evidence.workspace_id,
                    receipt.receipt_id,
                    "result",
                    receipt.result_event.event_id,
                    receipt.result_event.event_type,
                    receipt.result_event.global_position,
                ),
                (
                    evidence.tenant_id,
                    evidence.workspace_id,
                    receipt.receipt_id,
                    "terminal",
                    receipt.terminal_event.event_id,
                    receipt.terminal_event.event_type,
                    receipt.terminal_event.global_position,
                ),
            ),
        )
        self.connection.execute(
            """
            INSERT INTO invocation_result_receipts (
                tenant_id,
                workspace_id,
                receipt_id,
                schema_version,
                request_digest,
                invocation_id,
                session_id,
                plan_id,
                task_id,
                agent_id,
                job_idempotency_key,
                acceptance_idempotency_key,
                attempt_id,
                attempt_number,
                lease_epoch,
                worker_id,
                lease_token_digest,
                start_receipt_digest,
                execution_manifest_digest,
                result_manifest_schema_version,
                result_manifest_digest,
                result_ref,
                effect_class,
                action_receipt_set_digest,
                expected_stream_version,
                running_task_revision,
                terminal_task_revision,
                accepted_at,
                artifact_count,
                result_evidence_digest,
                terminal_transition_digest,
                receipt_digest,
                result_event_id,
                result_event_stream_id,
                result_event_type,
                result_event_timestamp,
                result_event_sequence,
                result_event_global_position,
                result_event_envelope_digest,
                terminal_event_id,
                terminal_event_stream_id,
                terminal_event_type,
                terminal_event_timestamp,
                terminal_event_sequence,
                terminal_event_global_position,
                terminal_event_envelope_digest
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                evidence.tenant_id,
                evidence.workspace_id,
                receipt.receipt_id,
                receipt.schema_version,
                evidence.request_digest,
                evidence.invocation_id,
                evidence.session_id,
                evidence.plan_id,
                evidence.task_id,
                evidence.agent_id,
                evidence.job_idempotency_key,
                evidence.acceptance_idempotency_key,
                evidence.attempt_id,
                evidence.attempt_number,
                evidence.lease_epoch,
                evidence.worker_id,
                evidence.lease_token_digest,
                evidence.start_receipt_digest,
                evidence.execution_manifest_digest,
                evidence.result_manifest_schema_version,
                evidence.result_manifest_digest,
                evidence.result_ref,
                evidence.effect_class.value,
                evidence.action_receipt_set_digest,
                request.expected_stream_version,
                evidence.running_task_revision,
                evidence.terminal_task_revision,
                accepted_at,
                evidence.artifact_count,
                evidence.canonical_digest(),
                transition.canonical_digest(),
                receipt.receipt_digest,
                receipt.result_event.event_id,
                receipt.result_event.stream_id,
                receipt.result_event.event_type,
                accepted_at,
                receipt.result_event.sequence,
                receipt.result_event.global_position,
                receipt.result_event.event_envelope_digest,
                receipt.terminal_event.event_id,
                receipt.terminal_event.stream_id,
                receipt.terminal_event.event_type,
                accepted_at,
                receipt.terminal_event.sequence,
                receipt.terminal_event.global_position,
                receipt.terminal_event.event_envelope_digest,
            ),
        )
        self.connection.execute(
            """
            INSERT INTO invocation_result_artifacts (
                tenant_id,
                workspace_id,
                receipt_id,
                ordinal,
                session_id,
                task_id,
                artifact_id,
                name,
                version,
                parent_version,
                media_type,
                blob_digest,
                byte_size,
                metadata_digest,
                created_by,
                idempotency_key,
                artifact_request_digest,
                candidate_digest
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate.tenant_id,
                candidate.workspace_id,
                receipt.receipt_id,
                0,
                candidate.session_id,
                candidate.task_id,
                candidate.artifact_id,
                candidate.name,
                candidate.version,
                candidate.parent_version,
                candidate.media_type,
                candidate.blob_digest,
                candidate.byte_size,
                candidate.metadata_digest,
                candidate.created_by,
                candidate.idempotency_key,
                candidate.artifact_request_digest,
                candidate.canonical_digest(),
            ),
        )
        self.connection.execute(
            """
            INSERT INTO outbox (
                message_id,
                destination,
                payload_json,
                headers_json,
                idempotency_key,
                triggering_event_id,
                triggering_global_position,
                status,
                attempt_count,
                available_at,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "message-1",
                "internal:result-projection",
                graph["publication_payload"],
                graph["publication_headers"],
                "publication-key-1",
                "event-terminal-1",
                3,
                "pending",
                0,
                accepted_at,
                accepted_at,
            ),
        )
        self.connection.execute(
            """
            INSERT INTO invocation_result_publications (
                tenant_id,
                workspace_id,
                receipt_id,
                publication_kind,
                message_id,
                destination,
                idempotency_key,
                payload_digest,
                headers_digest,
                triggering_event_id,
                triggering_global_position,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                evidence.tenant_id,
                evidence.workspace_id,
                receipt.receipt_id,
                "result_terminal_outbox_v1",
                "message-1",
                "internal:result-projection",
                "publication-key-1",
                hashlib.sha256(str(graph["publication_payload"]).encode("utf-8")).hexdigest(),
                hashlib.sha256(str(graph["publication_headers"]).encode("utf-8")).hexdigest(),
                "event-terminal-1",
                3,
                accepted_at,
            ),
        )

    def explicit_object_names(self) -> tuple[str, ...]:
        rows = self.connection.execute(
            """
            SELECT name
            FROM main.sqlite_master
            WHERE type IN ('table', 'index')
              AND name NOT LIKE 'sqlite_autoindex_%'
              AND (
                    name LIKE 'invocation_result_%'
                    OR name LIKE 'idx_invocation_result_%'
                    OR name LIKE 'uq_%_result_%'
              )
            ORDER BY name
            """
        ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def copy_row_with_changes(self, table_name: str, **changes: object) -> None:
        columns = tuple(
            str(row[1])
            for row in self.connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        )
        if not columns or not set(changes).issubset(columns):
            raise AssertionError("row-copy mutation names are not exact table columns")
        projections: list[str] = []
        parameters: list[object] = []
        for column in columns:
            if column in changes:
                projections.append("?")
                parameters.append(changes[column])
            else:
                projections.append(column)
        self.connection.execute(
            f"INSERT INTO {table_name} ({', '.join(columns)}) "
            f"SELECT {', '.join(projections)} FROM {table_name} LIMIT 1",
            tuple(parameters),
        )

    def test_candidate_identity_is_exact_disabled_and_separate(self) -> None:
        self.assertEqual(tuple(item.version for item in MIGRATIONS), (1, 2, 3, 4, 5, 6))
        self.assertEqual(
            tuple(item.migration_id for item in LEGACY_DOMAIN_MIGRATIONS),
            (1, 2, 3, 4, 5, 6),
        )
        self.assertEqual(
            tuple(item.migration_id for item in DOMAIN_MIGRATION_REGISTRY.descriptors),
            (1, 2, 3, 4, 5, 6),
        )
        self.assertEqual(
            tuple(
                item.migration_id for item in _KNOWN_INVOCATION_RESULTS_DOMAIN_REGISTRY.descriptors
            ),
            (1, 2, 3, 4, 5, 6, 7),
        )
        self.assertEqual(len(_INACTIVE_INVOCATION_RESULTS_MIGRATIONS), 1)
        self.assertIs(
            _INACTIVE_INVOCATION_RESULTS_MIGRATIONS[0],
            _INACTIVE_INVOCATION_RESULTS_CANDIDATE.migration,
        )
        self.assertFalse(_INACTIVE_INVOCATION_RESULTS_CANDIDATE.enabled)
        self.assertEqual(
            _INACTIVE_INVOCATION_RESULTS_CANDIDATE.component_preconditions,
            ("qe.event-store-core/1",),
        )
        self.assertFalse(hasattr(_INACTIVE_INVOCATION_RESULTS_CANDIDATE, "apply"))
        self.assertFalse(hasattr(_INACTIVE_INVOCATION_RESULTS_CANDIDATE, "executor"))
        self.assertEqual(
            _INACTIVE_INVOCATION_RESULTS_CANDIDATE.candidate_sha256,
            "43f9da085c45c0ae7b135a2b20a348606ad160928e26ca1c05c6b8b0b1a39dcd",
        )
        self.assertEqual(_INACTIVE_INVOCATION_RESULTS_DESCRIPTOR.migration_id, 7)
        self.assertEqual(
            _INACTIVE_INVOCATION_RESULTS_DESCRIPTOR.filename,
            "0007_invocation_results.up.sql",
        )
        self.assertEqual(_INACTIVE_INVOCATION_RESULTS_DESCRIPTOR.domain, "invocation_results")
        self.assertEqual(_INACTIVE_INVOCATION_RESULTS_DESCRIPTOR.domain_version, 1)
        self.assertEqual(_INACTIVE_INVOCATION_RESULTS_DESCRIPTOR.kind, "native")
        self.assertEqual(_INACTIVE_INVOCATION_RESULTS_DESCRIPTOR.dependencies, (1, 2, 4))
        self.assertEqual(
            _INACTIVE_INVOCATION_RESULTS_DESCRIPTOR.owned_object_manifest_sha256,
            "bc9cb5a1e09d3ed6a753fcad782b4412409084d10e73d412aed3e022a3200acf",
        )
        self.assertEqual(
            _INACTIVE_INVOCATION_RESULTS_DESCRIPTOR.descriptor_sha256,
            "656c881002dc6b4517404e1e358d481675fd531a27b32a6cdd2332a0a1d3575e",
        )
        self.assertEqual(
            _KNOWN_INVOCATION_RESULTS_DOMAIN_REGISTRY.registry_sha256,
            "a6d3433d53a19a35299b8968f00dd51d68a8f6785f8ab4913809cf9cc811fb02",
        )
        self.assertNotIn("_INACTIVE_INVOCATION_RESULTS_CANDIDATE", quantum_entanglement.__all__)
        with self.assertRaisesRegex(MigrationVersionError, "active packaged registry prefix"):
            apply_sqlite_migrations(
                self.connection,
                migrations=_KNOWN_INVOCATION_RESULTS_MIGRATIONS,
            )
        with self.assertRaisesRegex(ValueError, "migration is not exact"):
            _InactiveInvocationResultsCandidate(
                migration=MIGRATIONS[0],
                descriptor=LEGACY_DOMAIN_MIGRATIONS[0],
                enabled=False,
                component_preconditions=("qe.event-store-core/1",),
            )
        with self.assertRaisesRegex(ValueError, "descriptor is not exact"):
            _InactiveInvocationResultsCandidate(
                migration=_INACTIVE_INVOCATION_RESULTS_CANDIDATE.migration,
                descriptor=replace(
                    _INACTIVE_INVOCATION_RESULTS_DESCRIPTOR,
                    dependencies=(1, 2),
                ),
                enabled=False,
                component_preconditions=("qe.event-store-core/1",),
            )

    def test_default_store_stays_at_six_without_candidate_objects(self) -> None:
        self.assertEqual(current_schema_version(self.connection), 6)
        self.assertEqual(validate_sqlite_schema(self.connection), 6)
        self.assertEqual(
            tuple(
                int(row[0])
                for row in self.connection.execute(
                    "SELECT version FROM qe_schema_migrations ORDER BY version"
                ).fetchall()
            ),
            (1, 2, 3, 4, 5, 6),
        )
        self.assertEqual(self.explicit_object_names(), ())

    def test_isolated_rehearsal_installs_exact_catalog_and_foreign_keys(self) -> None:
        self.apply_candidate()
        self.assertEqual(
            self.explicit_object_names(),
            tuple(sorted((*_RESULT_TABLES, *_CANDIDATE_INDEXES))),
        )
        self.assertEqual(
            validate_sqlite_schema(
                self.connection, migrations=_KNOWN_INVOCATION_RESULTS_MIGRATIONS
            ),
            7,
        )
        with self.assertRaises(MigrationVersionError):
            validate_sqlite_schema(self.connection)
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

        for table_name, expected_columns in _RESULT_COLUMNS.items():
            with self.subTest(table=table_name):
                rows = self.connection.execute(f"PRAGMA table_info({table_name})").fetchall()
                self.assertEqual(tuple(str(row[1]) for row in rows), expected_columns)

        expected_foreign_tables = {
            "invocation_result_manifests": Counter(),
            "invocation_result_requests": Counter(
                {
                    "invocation_jobs": 1,
                    "invocation_admissions": 1,
                    "invocation_result_manifests": 3,
                }
            ),
            "invocation_result_event_bindings": Counter({"events": 2}),
            "invocation_result_receipts": Counter(
                {
                    "invocation_result_requests": 3,
                    "invocation_attempts": 1,
                    "events": 8,
                    "invocation_result_event_bindings": 12,
                }
            ),
            "invocation_result_artifacts": Counter(
                {"invocation_result_receipts": 3, "artifact_versions": 1}
            ),
            "invocation_result_publications": Counter(
                {"invocation_result_receipts": 5, "outbox": 1}
            ),
        }
        for table_name, expected in expected_foreign_tables.items():
            with self.subTest(foreign_keys=table_name):
                rows = self.connection.execute(f"PRAGMA foreign_key_list({table_name})").fetchall()
                self.assertEqual(Counter(str(row[2]) for row in rows), expected)
                self.assertTrue(all(row[5] == "RESTRICT" and row[6] == "RESTRICT" for row in rows))

    def test_descriptor_owned_objects_are_exact_candidate_creates(self) -> None:
        self.assertEqual(
            tuple(
                (item.object_type, item.name)
                for item in _INACTIVE_INVOCATION_RESULTS_DESCRIPTOR.owned_objects
            ),
            tuple(
                sorted(
                    (
                        *(("table", name) for name in _RESULT_TABLES),
                        *(("index", name) for name in _CANDIDATE_INDEXES),
                    )
                )
            ),
        )
        self.assertEqual(len(_INACTIVE_INVOCATION_RESULTS_DESCRIPTOR.owned_objects), 14)
        self.assertTrue(
            all(
                len(item.ddl_sha256) == 64
                for item in _INACTIVE_INVOCATION_RESULTS_DESCRIPTOR.owned_objects
            )
        )

    def test_empty_down_rehearsal_returns_to_exact_schema_six(self) -> None:
        self.apply_candidate()
        self.run_down()
        self.assertEqual(current_schema_version(self.connection), 6)
        self.assertEqual(validate_sqlite_schema(self.connection), 6)
        self.assertEqual(
            validate_sqlite_schema(
                self.connection,
                migrations=_KNOWN_INVOCATION_RESULTS_MIGRATIONS,
            ),
            6,
        )
        self.assertEqual(self.explicit_object_names(), ())
        self.assertEqual(
            self.connection.execute(
                "SELECT count(*) FROM qe_schema_migration_metadata WHERE migration_version = 7"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.connection.execute(
                """
                SELECT count(*)
                FROM qe_schema_migration_dependencies
                WHERE migration_version = 7 OR depends_on_version = 7
                """
            ).fetchone()[0],
            0,
        )
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_nonempty_down_guard_rolls_back_before_any_drop(self) -> None:
        self.apply_candidate()
        self.connection.execute(
            """
            INSERT INTO invocation_result_manifests (
                tenant_id,
                workspace_id,
                manifest_digest,
                schema_version,
                canonical_bytes,
                byte_size,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "tenant-1",
                "workspace-1",
                "a" * 64,
                2,
                sqlite3.Binary(b"{}"),
                2,
                "2026-08-29T00:00:00.000000Z",
            ),
        )
        before = self.explicit_object_names()
        with self.assertRaises(sqlite3.IntegrityError):
            self.run_down()
        self.assertEqual(self.explicit_object_names(), before)
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM invocation_result_manifests").fetchone()[
                0
            ],
            1,
        )
        self.assertEqual(current_schema_version(self.connection), 7)

    def test_orphan_event_reservation_is_a_guarded_incomplete_graph(self) -> None:
        self.seed_nonempty_v6_dependencies()
        self.apply_candidate()
        self.connection.execute(
            """
            INSERT INTO invocation_result_event_bindings (
                tenant_id,
                workspace_id,
                receipt_id,
                event_role,
                event_id,
                event_type,
                global_position
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "tenant-1",
                "workspace-1",
                "receipt-incomplete",
                "result",
                "event-start-1",
                "task.invocation.result.accepted",
                1,
            ),
        )

        before = self.explicit_object_names()
        with self.assertRaises(sqlite3.IntegrityError):
            self.run_down()
        self.assertEqual(self.explicit_object_names(), before)
        self.assertEqual(
            self.connection.execute(
                "SELECT count(*) FROM invocation_result_event_bindings"
            ).fetchone()[0],
            1,
        )
        self.connection.execute("DELETE FROM invocation_result_event_bindings")
        self.run_down()
        self.assertEqual(current_schema_version(self.connection), 6)

    def test_down_guard_rejects_a_future_sidecar_dependent(self) -> None:
        self.apply_candidate()
        self.connection.execute(
            """
            INSERT INTO qe_schema_migrations (
                version,
                filename,
                sha256,
                applied_at
            ) VALUES (8, '0008_future.up.sql', ?, '2026-08-29T00:00:00Z')
            """,
            ("8" * 64,),
        )
        self.connection.execute(
            """
            INSERT INTO qe_schema_migration_metadata (
                migration_version,
                domain,
                domain_version,
                metadata_kind,
                descriptor_sha256,
                owned_schema_sha256,
                recorded_at
            ) VALUES (8, 'future_result_consumer', 1, 'native', ?, ?, ?)
            """,
            ("8" * 64, "9" * 64, "2026-08-29T00:00:00Z"),
        )
        self.connection.execute(
            """
            INSERT INTO qe_schema_migration_dependencies (
                migration_version,
                depends_on_version
            ) VALUES (8, 7)
            """
        )
        before = self.explicit_object_names()
        with self.assertRaises(sqlite3.IntegrityError):
            self.run_down()
        self.assertEqual(self.explicit_object_names(), before)
        self.assertEqual(
            tuple(
                tuple(row)
                for row in self.connection.execute(
                    """
                    SELECT migration_version, depends_on_version
                    FROM qe_schema_migration_dependencies
                    WHERE migration_version = 8
                    """
                ).fetchall()
            ),
            ((8, 7),),
        )
        self.assertEqual(current_schema_version(self.connection), 8)

    def test_nonempty_v6_upgrade_and_raw_reopen_preserve_complete_graph(self) -> None:
        self.seed_nonempty_v6_dependencies()
        self.apply_candidate()
        self.seed_complete_result_graph()

        self.assertEqual(
            tuple(
                self.connection.execute(f"SELECT count(*) FROM {table_name}").fetchone()[0]
                for table_name in _RESULT_TABLES
            ),
            (1, 2, 1, 1, 1, 1),
        )
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

        graph = self.exact_result_graph()
        request = graph["request"]
        evidence = graph["evidence"]
        transition = graph["transition"]
        receipt = graph["receipt"]
        candidate = graph["candidate"]
        assert type(request) is ScopedInvocationResultAcceptanceRequestV2
        assert type(evidence) is ScopedInvocationResultEvidenceV2
        assert type(transition) is ScopedInvocationResultTerminalTransitionV2
        assert type(candidate) is ScopedInvocationResultArtifactCandidateV2
        manifest_row = self.connection.execute(
            """
            SELECT manifest_digest, canonical_bytes, byte_size
            FROM invocation_result_manifests
            """
        ).fetchone()
        self.assertIsNotNone(manifest_row)
        durable_manifest = ScopedInvocationResultManifestV2.from_dict(
            json.loads(bytes(manifest_row["canonical_bytes"]).decode("utf-8"))
        )
        self.assertEqual(durable_manifest, request.manifest)
        self.assertEqual(
            bytes(manifest_row["canonical_bytes"]),
            request.manifest.canonical_bytes(),
        )
        self.assertEqual(manifest_row["manifest_digest"], request.manifest.canonical_digest())
        self.assertEqual(manifest_row["byte_size"], len(request.manifest.canonical_bytes()))

        request_row = self.connection.execute(
            """
            SELECT request_digest, request_identity_bytes, request_identity_byte_size
            FROM invocation_result_requests
            """
        ).fetchone()
        self.assertEqual(request_row["request_digest"], request.canonical_digest())
        self.assertEqual(
            bytes(request_row["request_identity_bytes"]),
            graph["request_identity_bytes"],
        )
        self.assertEqual(
            request_row["request_identity_byte_size"],
            len(bytes(request_row["request_identity_bytes"])),
        )

        event_rows = self.connection.execute(
            """
            SELECT
                global_position,
                stream_id,
                sequence,
                event_id,
                event_type,
                actor_id,
                timestamp,
                payload_json,
                correlation_id,
                causation_id,
                idempotency_key
            FROM events
            WHERE event_id IN ('event-result-1', 'event-terminal-1')
            ORDER BY global_position
            """
        ).fetchall()
        self.assertEqual(len(event_rows), 2)
        result_envelope = envelope_codec._stored_event_envelope_from_raw_row(event_rows[0])
        terminal_envelope = envelope_codec._stored_event_envelope_from_raw_row(event_rows[1])
        self.assertEqual(
            ScopedInvocationResultEvidenceV2.from_dict(json.loads(event_rows[0]["payload_json"])),
            evidence,
        )
        self.assertEqual(
            ScopedInvocationResultTerminalTransitionV2.from_dict(
                json.loads(event_rows[1]["payload_json"])
            ),
            transition,
        )
        self.assertEqual(result_envelope.digest(), receipt.result_event.event_envelope_digest)
        self.assertEqual(
            terminal_envelope.digest(),
            receipt.terminal_event.event_envelope_digest,
        )

        receipt_row = self.connection.execute(
            """
            SELECT
                receipt_digest,
                result_evidence_digest,
                terminal_transition_digest,
                result_event_envelope_digest,
                terminal_event_envelope_digest
            FROM invocation_result_receipts
            """
        ).fetchone()
        self.assertEqual(receipt_row["receipt_digest"], receipt.receipt_digest)
        self.assertEqual(receipt_row["result_evidence_digest"], evidence.canonical_digest())
        self.assertEqual(
            receipt_row["terminal_transition_digest"],
            transition.canonical_digest(),
        )
        self.assertEqual(receipt_row["result_event_envelope_digest"], result_envelope.digest())
        self.assertEqual(
            receipt_row["terminal_event_envelope_digest"],
            terminal_envelope.digest(),
        )
        binding_rows = self.connection.execute(
            """
            SELECT event_role, event_id, event_type, global_position
            FROM invocation_result_event_bindings
            ORDER BY event_role
            """
        ).fetchall()
        self.assertEqual(
            tuple(tuple(row) for row in binding_rows),
            (
                (
                    "result",
                    receipt.result_event.event_id,
                    receipt.result_event.event_type,
                    receipt.result_event.global_position,
                ),
                (
                    "terminal",
                    receipt.terminal_event.event_id,
                    receipt.terminal_event.event_type,
                    receipt.terminal_event.global_position,
                ),
            ),
        )
        artifact_row = self.connection.execute(
            """
            SELECT
                artifact_id,
                version,
                blob_digest,
                metadata_digest,
                artifact_request_digest,
                candidate_digest
            FROM invocation_result_artifacts
            """
        ).fetchone()
        self.assertEqual(
            tuple(artifact_row),
            (
                candidate.artifact_id,
                candidate.version,
                candidate.blob_digest,
                candidate.metadata_digest,
                candidate.artifact_request_digest,
                candidate.canonical_digest(),
            ),
        )

        reopened = sqlite3.connect(self.path, isolation_level=None)
        try:
            self.assertEqual(
                validate_sqlite_schema(
                    reopened,
                    migrations=_KNOWN_INVOCATION_RESULTS_MIGRATIONS,
                ),
                7,
            )
            self.assertEqual(reopened.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(reopened.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(
                tuple(
                    reopened.execute(f"SELECT count(*) FROM {table_name}").fetchone()[0]
                    for table_name in _RESULT_TABLES
                ),
                (1, 2, 1, 1, 1, 1),
            )
        finally:
            reopened.close()

    def test_scoped_foreign_keys_and_canonical_checks_reject_drift(self) -> None:
        self.seed_nonempty_v6_dependencies()
        self.apply_candidate()
        self.seed_complete_result_graph()
        accepted_at = "2026-08-29T00:01:00.000000Z"
        self.connection.execute(
            """
            INSERT INTO events (
                global_position,
                stream_id,
                sequence,
                event_id,
                event_type,
                actor_id,
                timestamp,
                payload_json,
                correlation_id,
                causation_id,
                idempotency_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                4,
                "session:session-1",
                4,
                "event-result-other",
                "task.invocation.result.accepted",
                "orchestrator",
                accepted_at,
                "{}",
                "correlation-1",
                "event-start-1",
                "other-result-key",
            ),
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """
                UPDATE invocation_result_receipts
                SET result_event_id = 'event-result-missing'
                WHERE receipt_id = 'receipt-1'
                """
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """
                UPDATE invocation_result_artifacts
                SET tenant_id = 'tenant-other'
                WHERE receipt_id = 'receipt-1'
                """
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """
                UPDATE invocation_result_requests
                SET result_manifest_digest = ?
                WHERE invocation_id = ?
                """,
                ("c" * 64, "invocation-1"),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """
                UPDATE invocation_result_publications
                SET triggering_event_id = 'event-missing'
                WHERE receipt_id = 'receipt-1'
                """
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """
                UPDATE invocation_result_publications
                SET triggering_event_id = 'event-result-1',
                    triggering_global_position = 2
                WHERE receipt_id = 'receipt-1'
                """
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """
                UPDATE invocation_result_publications
                SET publication_kind = 'unknown'
                WHERE receipt_id = 'receipt-1'
                """
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "DELETE FROM invocation_result_receipts WHERE receipt_id = 'receipt-1'"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "DELETE FROM invocation_result_event_bindings WHERE event_role = 'result'"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "DELETE FROM artifact_versions WHERE artifact_id = 'artifact-1'"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """
                INSERT INTO invocation_result_manifests (
                    tenant_id,
                    workspace_id,
                    manifest_digest,
                    schema_version,
                    canonical_bytes,
                    byte_size,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "tenant-1",
                    "workspace-1",
                    "A" * 64,
                    2,
                    sqlite3.Binary(b"{}"),
                    2,
                    accepted_at,
                ),
            )
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(
            self.connection.execute(
                "SELECT result_event_id FROM invocation_result_receipts"
            ).fetchone()[0],
            "event-result-1",
        )

    def test_global_parent_identities_cannot_be_reused_across_scope(self) -> None:
        self.seed_nonempty_v6_dependencies()
        self.apply_candidate()
        self.seed_complete_result_graph()
        self.connection.execute("PRAGMA foreign_keys=OFF")
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "manifest_digest"):
                self.copy_row_with_changes(
                    "invocation_result_manifests",
                    tenant_id="tenant-other",
                    workspace_id="workspace-other",
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "request_digest"):
                self.copy_row_with_changes(
                    "invocation_result_requests",
                    tenant_id="tenant-other",
                    workspace_id="workspace-other",
                    invocation_id="invocation-other",
                    session_id="session-other",
                    task_id="task-other",
                    causation_id="task-other",
                )

            receipt_base_changes = {
                "tenant_id": "tenant-other",
                "workspace_id": "workspace-other",
                "receipt_id": "receipt-other",
                "invocation_id": "invocation-other",
                "attempt_id": "attempt-other",
                "result_event_id": "event-result-other",
                "terminal_event_id": "event-terminal-other",
                "result_event_global_position": 100,
                "terminal_event_global_position": 101,
            }
            with self.assertRaisesRegex(sqlite3.IntegrityError, "attempt_id"):
                self.copy_row_with_changes(
                    "invocation_result_receipts",
                    **{**receipt_base_changes, "attempt_id": "attempt-1"},
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "result_event_id"):
                self.copy_row_with_changes(
                    "invocation_result_receipts",
                    **{**receipt_base_changes, "result_event_id": "event-result-1"},
                )
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "event_global_position",
            ):
                self.copy_row_with_changes(
                    "invocation_result_receipts",
                    **{
                        **receipt_base_changes,
                        "result_event_global_position": 2,
                        "terminal_event_global_position": 3,
                    },
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "event_id"):
                self.copy_row_with_changes(
                    "invocation_result_event_bindings",
                    tenant_id="tenant-other",
                    workspace_id="workspace-other",
                    receipt_id="receipt-other",
                    event_role="result",
                    event_id="event-terminal-1",
                    event_type="task.invocation.result.accepted",
                    global_position=100,
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "global_position"):
                self.copy_row_with_changes(
                    "invocation_result_event_bindings",
                    tenant_id="tenant-other",
                    workspace_id="workspace-other",
                    receipt_id="receipt-other",
                    event_role="result",
                    event_id="event-result-other",
                    event_type="task.invocation.result.accepted",
                    global_position=3,
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "artifact_id"):
                self.copy_row_with_changes(
                    "invocation_result_artifacts",
                    tenant_id="tenant-other",
                    workspace_id="workspace-other",
                    receipt_id="receipt-other",
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "message_id"):
                self.copy_row_with_changes(
                    "invocation_result_publications",
                    tenant_id="tenant-other",
                    workspace_id="workspace-other",
                    receipt_id="receipt-other",
                    triggering_event_id="event-terminal-other",
                    triggering_global_position=101,
                )
        finally:
            self.connection.execute("PRAGMA foreign_keys=ON")
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_complete_graph_must_be_explicitly_cleared_before_down(self) -> None:
        self.seed_nonempty_v6_dependencies()
        self.apply_candidate()
        self.seed_complete_result_graph()
        with self.assertRaises(sqlite3.IntegrityError):
            self.run_down()

        for table_name in (
            "invocation_result_publications",
            "invocation_result_artifacts",
            "invocation_result_receipts",
            "invocation_result_event_bindings",
            "invocation_result_requests",
            "invocation_result_manifests",
        ):
            self.connection.execute(f"DELETE FROM {table_name}")
        self.run_down()
        self.assertEqual(current_schema_version(self.connection), 6)
        self.assertEqual(validate_sqlite_schema(self.connection), 6)
        self.assertEqual(self.explicit_object_names(), ())
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_old_default_store_reopen_rejects_rehearsed_seven(self) -> None:
        self.apply_candidate()
        self.store.close()
        with self.assertRaises(MigrationVersionError):
            SQLiteEventStore(self.path)


if __name__ == "__main__":
    unittest.main()
