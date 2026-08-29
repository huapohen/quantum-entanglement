from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Iterable
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import tests.test_result_acceptance_durable_prerequisites as durable_prerequisites
from quantum_entanglement.events import StoredEvent
from quantum_entanglement.operation_authorization import (
    CurrentAuthorizationState,
    ProtectedOperationComposer,
)
from quantum_entanglement.projections import ProjectionLeaseConflictError
from quantum_entanglement.request_context import (
    AuthenticatedRequestBinding,
    CallerRequestContext,
    RequestContextIssuer,
)
from quantum_entanglement.result_projection import (
    RESULT_PROJECTION_READ_ACTION,
    RESULT_PROJECTION_RESOURCE_TYPE,
    RESULT_PROJECTION_TABLE,
    ResultProjectionAuthorizationError,
    ResultProjectionConflictError,
    ResultProjectionProcessMismatchError,
    ResultProjectionSchemaError,
    ResultProjectionStatus,
    SQLiteResultProjectionStore,
)
from quantum_entanglement.service.secrets import SecretMaterial
from quantum_entanglement.store import SQLiteEventStore
from quantum_entanglement.tenancy import (
    AccessRequest,
    CapabilitySigningKey,
    CapabilityVerifier,
    InMemoryRevocationRevisionGuard,
    KeyStatus,
    KeyUsage,
    Member,
    ResourceRef,
    ResourceScope,
    RevocationSnapshot,
    Role,
    RoleBinding,
    RotatingHMACKeyRing,
    TenantAuthorizer,
    TenantId,
    WorkspaceId,
)

_AUTH_NOW = datetime(2026, 8, 27, 10, 0, 0, tzinfo=timezone.utc)
_AUTH_EVIDENCE = "ab" * 32


class _ProjectionAuthClock:
    def __init__(self) -> None:
        self.current = _AUTH_NOW

    def now(self) -> datetime:
        return self.current


class _ProjectionAuthenticator:
    def authenticate(self, claims, credential, *, audience, at):
        if bytes(credential) != b"projection-credential":
            raise RuntimeError("invalid projection test credential")
        return AuthenticatedRequestBinding(
            authenticator_id="projection-authenticator",
            audience=audience,
            request_id=claims.request_id,
            principal_id="projection-principal",
            subject_id=claims.subject_id,
            tenant_id=claims.tenant_id,
            workspace_id=claims.workspace_id,
            identity_revision="projection-identity-1",
            scope_revision="projection-scope-1",
            evidence_fingerprint=_AUTH_EVIDENCE,
            authenticated_at=at,
            expires_at=at + timedelta(minutes=5),
        )


class _ProjectionStateProvider:
    def __init__(self, clock: _ProjectionAuthClock) -> None:
        self.clock = clock
        self.calls = 0

    def load_current_state(self, basis, request):
        self.calls += 1
        return CurrentAuthorizationState(
            context_id=basis.context_id,
            authenticator_id=basis.authenticator_id,
            audience=basis.audience,
            request_id=basis.request_id,
            principal_id=basis.principal_id,
            subject_id=basis.subject_id,
            tenant_id=basis.tenant_id,
            workspace_id=basis.workspace_id,
            identity_revision=basis.identity_revision,
            scope_revision=basis.scope_revision,
            observed_at=self.clock.now(),
            member=Member(
                member_id=basis.subject_id,
                tenant_id=basis.tenant_id,
                role_bindings=(
                    RoleBinding(
                        role=Role.OWNER,
                        scope=ResourceScope(
                            tenant_id=basis.tenant_id,
                            workspace_id=basis.workspace_id,
                        ),
                    ),
                ),
            ),
            revocations=RevocationSnapshot.empty(
                basis.tenant_id,
                self.clock.now(),
                revision=1,
            ),
            verified_capabilities=(),
        )


class _TupleSource:
    def __init__(self, events: Iterable[StoredEvent]) -> None:
        self.events = tuple(events)

    def read_all(self, after_position: int = 0, limit: int = 1000) -> tuple[StoredEvent, ...]:
        return tuple(
            event for event in self.events if event.global_position > after_position
        )[:limit]


class ResultProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = str(Path(self.directory.name) / "event-store.sqlite3")
        self.event_store = SQLiteEventStore(
            self.path,
            clock=lambda: "2026-08-27T10:00:00.000000Z",
            enable_result_acceptance_schema=True,
        )
        helper = durable_prerequisites.ResultAcceptanceDurablePrerequisiteTests(
            methodName="runTest"
        )
        helper.store = self.event_store
        prepared = helper.fresh_prepared()
        self.event_store._clock = lambda: "2026-08-27T10:00:02.000000Z"
        with patch(
            "quantum_entanglement.store.new_id",
            side_effect=(
                "receipt-projection-1",
                "event-result-projection-1",
                "event-terminal-projection-1",
            ),
        ):
            self.event_store.accept_scoped_invocation_result_v2(
                prepared.request,
                prepared.claimed,
            )
        self.events = self.event_store.read_all()
        self.result_event = next(
            event
            for event in self.events
            if event.event.event_type == "task.invocation.result.accepted"
        )
        self.terminal_event = next(
            event
            for event in self.events
            if event.event.event_type == "task.status.changed"
            and "resultReceiptId" in event.event.payload
        )
        self.scope = (
            self.result_event.event.payload["tenantId"],
            self.result_event.event.payload["workspaceId"],
            self.result_event.event.payload["invocationId"],
        )
        self.projection = SQLiteResultProjectionStore(self.event_store, self.path)
        self.auth_clock = _ProjectionAuthClock()
        self.auth_tenant = TenantId(self.scope[0])
        self.auth_workspace = WorkspaceId(self.scope[1])
        key_ring = RotatingHMACKeyRing(
            trust_domain="projection-auth-tests",
            policy_version="projection-policy-1",
            keys=(
                CapabilitySigningKey(
                    kid="projection-root-key",
                    principal_id="projection-root",
                    secret=b"projection-signing-secret-canary",
                    not_before=_AUTH_NOW - timedelta(days=1),
                    expires_at=_AUTH_NOW + timedelta(days=1),
                    status=KeyStatus.ACTIVE,
                    usages=frozenset((KeyUsage.ROOT,)),
                    root_tenants=frozenset((self.auth_tenant,)),
                ),
            ),
        )
        verifier = CapabilityVerifier(
            proof_verifier=key_ring,
            trust_domain="projection-auth-tests",
            policy_version="projection-policy-1",
            audience="projection-runtime",
            clock=self.auth_clock,
        )
        authorizer = TenantAuthorizer(
            capability_verifier=verifier,
            trust_domain="projection-auth-tests",
            policy_version="projection-policy-1",
            revision_guard=InMemoryRevocationRevisionGuard(),
            audience="projection-runtime",
            clock=self.auth_clock,
        )
        self.auth_issuer = RequestContextIssuer(
            authenticator=_ProjectionAuthenticator(),
            authenticator_id="projection-authenticator",
            audience="projection-runtime",
            clock=self.auth_clock,
        )
        self.auth_composer = ProtectedOperationComposer(
            issuer=self.auth_issuer,
            state_provider=_ProjectionStateProvider(self.auth_clock),
            authorizer=authorizer,
            clock=self.auth_clock,
            operation_ttl=timedelta(seconds=20),
            max_state_age=timedelta(seconds=30),
        )
        self.auth_context = self.auth_issuer.issue(
            CallerRequestContext(
                request_id="projection-request-1",
                subject_id="projection-user-1",
                tenant_id=self.auth_tenant,
                workspace_id=self.auth_workspace,
            ),
            SecretMaterial(b"projection-credential"),
        )

    def tearDown(self) -> None:
        self.auth_composer.close()
        self.auth_issuer.close()
        self.projection.close()
        self.event_store.close()
        self.directory.cleanup()

    def authorized_request(self, **changes):
        values = {
            "request_id": "projection-request-1",
            "subject_id": "projection-user-1",
            "tenant_id": self.auth_tenant,
            "action": RESULT_PROJECTION_READ_ACTION,
            "resource": ResourceRef(
                tenant_id=self.auth_tenant,
                workspace_id=self.auth_workspace,
                resource_type=RESULT_PROJECTION_RESOURCE_TYPE,
                resource_id=self.scope[2],
            ),
        }
        values.update(changes)
        return AccessRequest(**values)

    def test_complete_result_and_terminal_events_materialize_completed_view(self) -> None:
        run = self.projection.run_once()
        self.assertEqual(run.scanned_count, len(self.events))
        self.assertEqual(run.applied_count, len(self.events))
        view = self.projection.read(*self.scope)
        self.assertIsNotNone(view)
        assert view is not None
        self.assertEqual(view.status, ResultProjectionStatus.COMPLETED)
        self.assertEqual(view.result_ref, "result:durable-prerequisite-1")
        self.assertEqual(view.artifact_count, 1)
        self.assertEqual(view.result_event_id, self.result_event.event.event_id)
        self.assertEqual(view.terminal_event_id, self.terminal_event.event.event_id)
        self.assertNotIn("durable result", repr(view))
        self.assertNotIn("lease-token", repr(view))

    def test_scope_isolation_does_not_make_projection_enumerable(self) -> None:
        self.projection.run_once()
        tenant_id, workspace_id, invocation_id = self.scope
        self.assertIsNone(self.projection.read("other-tenant", workspace_id, invocation_id))
        self.assertIsNone(self.projection.read(tenant_id, "other-workspace", invocation_id))
        self.assertIsNone(self.projection.read(tenant_id, workspace_id, "other-invocation"))

    def test_repeated_projector_run_is_idempotent(self) -> None:
        first = self.projection.run_once()
        second = self.projection.run_once()
        self.assertEqual(first.applied_count, len(self.events))
        self.assertEqual(second.scanned_count, 0)
        self.assertEqual(second.applied_count, 0)
        self.assertEqual(self.projection.read(*self.scope).status, ResultProjectionStatus.COMPLETED)

    def test_authorized_read_derives_scope_from_reauthorized_request(self) -> None:
        self.projection.run_once()
        request = self.authorized_request()
        operation = self.auth_composer.authorize(self.auth_context, request)
        with patch.object(self.projection, "read", wraps=self.projection.read) as read:
            view = self.projection.read_authorized(
                self.auth_composer,
                operation,
                self.auth_context,
                request,
            )
        self.assertIsNotNone(view)
        assert view is not None
        self.assertEqual(view.status, ResultProjectionStatus.COMPLETED)
        read.assert_called_once_with(*self.scope)

    def test_authorized_read_rejects_request_scope_or_action_before_sqlite_read(self) -> None:
        self.projection.run_once()
        operation = self.auth_composer.authorize(
            self.auth_context,
            self.authorized_request(),
        )
        invalid_requests = (
            self.authorized_request(action="workflow.read"),
            self.authorized_request(
                resource=ResourceRef(
                    tenant_id=self.auth_tenant,
                    workspace_id=self.auth_workspace,
                    resource_type="artifact",
                    resource_id=self.scope[2],
                )
            ),
            self.authorized_request(
                resource=ResourceRef(
                    tenant_id=TenantId("other-tenant"),
                    workspace_id=self.auth_workspace,
                    resource_type=RESULT_PROJECTION_RESOURCE_TYPE,
                    resource_id=self.scope[2],
                )
            ),
        )
        for request in invalid_requests:
            with self.subTest(request=request), patch.object(
                self.projection,
                "read",
                wraps=self.projection.read,
            ) as read:
                with self.assertRaises(ResultProjectionAuthorizationError) as captured:
                    self.projection.read_authorized(
                        self.auth_composer,
                        operation,
                        self.auth_context,
                        request,
                    )
            self.assertEqual(
                captured.exception.code,
                "result_projection_authorization_request_invalid",
            )
            read.assert_not_called()

    def test_authorized_read_rejects_subject_drift_without_reading(self) -> None:
        self.projection.run_once()
        request = self.authorized_request()
        operation = self.auth_composer.authorize(self.auth_context, request)
        drifted = replace(request, subject_id="projection-attacker")
        with patch.object(self.projection, "read", wraps=self.projection.read) as read:
            with self.assertRaises(ResultProjectionAuthorizationError) as captured:
                self.projection.read_authorized(
                    self.auth_composer,
                    operation,
                    self.auth_context,
                    drifted,
                )
        self.assertEqual(captured.exception.code, "result_projection_authorization_denied")
        read.assert_not_called()

    def test_authorized_read_rejects_forged_dependencies(self) -> None:
        request = self.authorized_request()
        operation = self.auth_composer.authorize(self.auth_context, request)
        invalid_dependencies = (
            (object(), operation, self.auth_context, request),
            (self.auth_composer, object(), self.auth_context, request),
            (self.auth_composer, operation, object(), request),
            (self.auth_composer, operation, self.auth_context, object()),
        )
        for dependencies in invalid_dependencies:
            with self.subTest(dependencies=dependencies):
                with self.assertRaises(ResultProjectionAuthorizationError) as captured:
                    self.projection.read_authorized(*dependencies)
                self.assertEqual(
                    captured.exception.code,
                    "result_projection_authorization_dependency_invalid"
                    if dependencies[0] is not self.auth_composer or dependencies[1] is not operation
                    or dependencies[2] is not self.auth_context
                    else "result_projection_authorization_request_invalid",
                )

    def test_reopen_and_second_connection_reuse_durable_offset(self) -> None:
        self.projection.run_once()
        reopened = SQLiteResultProjectionStore(
            self.event_store,
            self.path,
            owner_id="result-projector-reopened",
        )
        try:
            run = reopened.run_once()
            self.assertEqual(run.scanned_count, 0)
            view = reopened.read(*self.scope)
            self.assertIsNotNone(view)
            assert view is not None
            self.assertEqual(view.status, ResultProjectionStatus.COMPLETED)
        finally:
            reopened.close()

    def test_two_projection_connections_fence_competing_owner(self) -> None:
        class BlockingSource(_TupleSource):
            def __init__(self, events: Iterable[StoredEvent]) -> None:
                super().__init__(events)
                self.started = threading.Event()
                self.release = threading.Event()

            def read_all(
                self,
                after_position: int = 0,
                limit: int = 1000,
            ) -> tuple[StoredEvent, ...]:
                self.started.set()
                if not self.release.wait(timeout=5):
                    raise AssertionError("blocking source was not released")
                return super().read_all(after_position, limit)

        source = BlockingSource(self.events)
        path = self.path + ".dual-connection"
        first = SQLiteResultProjectionStore(source, path, owner_id="result-projector-first")
        second = SQLiteResultProjectionStore(source, path, owner_id="result-projector-second")
        first_results: list[object] = []
        second_errors: list[BaseException] = []

        def run_first() -> None:
            try:
                first_results.append(first.run_once())
            except BaseException as error:
                first_results.append(error)

        def run_second() -> None:
            try:
                second.run_once()
            except BaseException as error:
                second_errors.append(error)

        first_thread = threading.Thread(target=run_first)
        second_thread = threading.Thread(target=run_second)
        first_thread.start()
        self.assertTrue(source.started.wait(timeout=5))
        second_thread.start()
        second_thread.join(timeout=5)
        self.assertFalse(second_thread.is_alive())
        source.release.set()
        first_thread.join(timeout=5)
        self.assertFalse(first_thread.is_alive())
        try:
            self.assertEqual(len(first_results), 1)
            self.assertIsNotNone(first_results[0])
            self.assertEqual(len(second_errors), 1)
            self.assertIs(type(second_errors[0]), ProjectionLeaseConflictError)
        finally:
            first.close()
            second.close()

    def test_sigkill_after_lease_claim_is_recovered_by_new_owner(self) -> None:
        signal_path = str(Path(self.directory.name) / "projection-child-ready")
        child_code = f"""
import time
from pathlib import Path
from quantum_entanglement.result_projection import SQLiteResultProjectionStore
from quantum_entanglement.store import SQLiteEventStore

class SignalSource:
    def __init__(self, store, signal_path):
        self.store = store
        self.signal_path = signal_path

    def read_all(self, after_position=0, limit=1000):
        Path(self.signal_path).touch()
        while True:
            time.sleep(1)

store = SQLiteEventStore({self.path!r}, enable_result_acceptance_schema=True)
source = SignalSource(store, {signal_path!r})
projection = SQLiteResultProjectionStore(
    source,
    {self.path!r},
    owner_id="result-projector-killed",
    lease_seconds=0.2,
)
projection.run_once()
"""
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.pathsep.join(
            (str(Path(__file__).resolve().parents[1] / "src"), environment.get("PYTHONPATH", ""))
        ).rstrip(os.pathsep)
        child = subprocess.Popen(
            [sys.executable, "-c", child_code],
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 5
            while not Path(signal_path).exists() and child.poll() is None:
                if time.monotonic() >= deadline:
                    self.fail("killed projection child did not claim and enter its source")
                time.sleep(0.01)
            self.assertTrue(Path(signal_path).exists())
            child.kill()
            child.wait(timeout=5)
            self.assertNotEqual(child.returncode, 0)
            time.sleep(0.35)
            recovered = SQLiteResultProjectionStore(
                self.event_store,
                self.path,
                owner_id="result-projector-recovery",
                lease_seconds=1.0,
            )
            try:
                run = recovered.run_once()
                self.assertEqual(run.applied_count, len(self.events))
                view = recovered.read(*self.scope)
                self.assertIsNotNone(view)
                assert view is not None
                self.assertEqual(view.status, ResultProjectionStatus.COMPLETED)
            finally:
                recovered.close()
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)

    def test_terminal_event_without_result_fails_closed(self) -> None:
        source = _TupleSource((replace(self.terminal_event, global_position=1, sequence=1),))
        candidate = SQLiteResultProjectionStore(
            self.event_store,
            self.path + ".terminal-only",
            owner_id="terminal-only",
        )
        try:
            with self.assertRaises(ResultProjectionConflictError):
                candidate._projector.event_source = source
                candidate.run_once()
            self.assertIsNone(candidate.read(*self.scope))
        finally:
            candidate.close()

    def test_terminal_binding_drift_fails_closed(self) -> None:
        payload = dict(self.terminal_event.event.payload)
        payload["taskId"] = "task-projection-drift"
        drifted_terminal = replace(
            self.terminal_event,
            sequence=2,
            global_position=2,
            event=replace(self.terminal_event.event, payload=payload),
        )
        result = replace(self.result_event, sequence=1, global_position=1)
        candidate = SQLiteResultProjectionStore(
            _TupleSource((result, drifted_terminal)),
            self.path + ".terminal-drift",
            owner_id="terminal-drift",
        )
        try:
            with self.assertRaises(ResultProjectionConflictError):
                candidate.run_once()
            view = candidate.read(*self.scope)
            self.assertIsNotNone(view)
            assert view is not None
            self.assertEqual(view.status, ResultProjectionStatus.RESULT_ACCEPTED)
            self.assertIsNone(view.terminal_event_id)
        finally:
            candidate.close()

    def test_result_identity_conflict_fails_closed(self) -> None:
        duplicate = replace(
            self.result_event,
            sequence=self.result_event.sequence + 10,
            global_position=len(self.events) + 1,
            event=replace(self.result_event.event, event_id="event-result-projection-duplicate"),
        )
        source = _TupleSource((*self.events, duplicate))
        candidate = SQLiteResultProjectionStore(
            self.event_store,
            self.path + ".conflict",
            owner_id="conflict",
        )
        try:
            candidate._projector.event_source = source
            with self.assertRaises(ResultProjectionConflictError):
                candidate.run_once()
            view = candidate.read(*self.scope)
            self.assertIsNotNone(view)
            assert view is not None
            self.assertEqual(view.result_event_id, self.result_event.event.event_id)
        finally:
            candidate.close()

    def test_projection_schema_drift_is_rejected(self) -> None:
        path = self.path + ".drift"
        connection = sqlite3.connect(path)
        try:
            connection.execute(f"CREATE TABLE {RESULT_PROJECTION_TABLE} (wrong TEXT)")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(ResultProjectionSchemaError):
            SQLiteResultProjectionStore(self.event_store, path, owner_id="schema-drift")

    def test_handler_only_writes_projection_owned_table(self) -> None:
        statements: list[str] = []
        self.projection._connection.set_trace_callback(statements.append)
        self.projection.run_once()
        forbidden = ("events", "invocation_jobs", "invocation_attempts", "outbox")
        handler_sql = tuple(statement.lower() for statement in statements)
        self.assertFalse(
            any(any(table in statement for table in forbidden) for statement in handler_sql)
        )
        self.assertTrue(any(RESULT_PROJECTION_TABLE in statement for statement in handler_sql))

    def test_fork_inherited_projection_rejects_before_touching_sqlite(self) -> None:
        self.projection.run_once()
        read_fd, write_fd = os.pipe()
        child_pid = os.fork()
        if child_pid == 0:
            os.close(read_fd)
            try:
                self.projection.run_once()
            except ResultProjectionProcessMismatchError:
                os.write(write_fd, b"ok")
                os._exit(0)
            except BaseException:
                os.write(write_fd, b"bad")
                os._exit(1)
            os.write(write_fd, b"missing")
            os._exit(1)
        os.close(write_fd)
        try:
            result = os.read(read_fd, 32)
        finally:
            os.close(read_fd)
        _, status = os.waitpid(child_pid, 0)
        self.assertEqual(result, b"ok")
        self.assertTrue(os.WIFEXITED(status))
        self.assertEqual(os.WEXITSTATUS(status), 0)
        self.assertEqual(self.projection.run_once().scanned_count, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
