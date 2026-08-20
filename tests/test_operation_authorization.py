import copy
import pickle
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from quantum_entanglement.operation_authorization import (
    AuthorizedOperation,
    CurrentAuthorizationState,
    CurrentAuthorizationStateProvider,
    OperationAuthorizationError,
    _AuthorizedOperationRegistry,
    _OperationBinding,
)
from quantum_entanglement.request_context import ReauthorizationBasis
from quantum_entanglement.tenancy import (
    AccessRequest,
    Member,
    ResourceRef,
    RevocationSnapshot,
    TenantId,
    WorkspaceId,
)

NOW = datetime(2026, 8, 20, 8, 0, 0, tzinfo=timezone.utc)
EVIDENCE = "ab" * 32


class FakeCurrentStateProvider:
    def __init__(self, state):
        self.state = state

    def load_current_state(self, basis, request):
        return self.state


class FixedClock:
    def __init__(self, current=NOW):
        self.current = current
        self.lock = threading.Lock()

    def now(self):
        with self.lock:
            return self.current

    def set(self, value):
        with self.lock:
            self.current = value

    def advance(self, delta):
        with self.lock:
            self.current += delta


class CurrentAuthorizationStateTests(unittest.TestCase):
    def setUp(self):
        self.tenant = TenantId("tenant-a")
        self.workspace = WorkspaceId("workspace-a")

    def state(self, **changes):
        values = {
            "context_id": "context-1",
            "authenticator_id": "authenticator-1",
            "audience": "qe-runtime",
            "request_id": "request-1",
            "principal_id": "principal-1",
            "subject_id": "subject-1",
            "tenant_id": self.tenant,
            "workspace_id": self.workspace,
            "identity_revision": "identity-7",
            "scope_revision": "scope-11",
            "observed_at": NOW,
            "member": Member(member_id="subject-1", tenant_id=self.tenant),
            "revocations": RevocationSnapshot.empty(self.tenant, NOW, revision=9),
            "verified_capabilities": (),
        }
        values.update(changes)
        return CurrentAuthorizationState(**values)

    def basis(self):
        return ReauthorizationBasis(
            context_id="context-1",
            authenticator_id="authenticator-1",
            audience="qe-runtime",
            request_id="request-1",
            principal_id="principal-1",
            subject_id="subject-1",
            tenant_id=self.tenant,
            workspace_id=self.workspace,
            identity_revision="identity-7",
            scope_revision="scope-11",
            evidence_fingerprint=EVIDENCE,
            authenticated_at=NOW - timedelta(minutes=1),
            context_issued_at=NOW - timedelta(minutes=1),
            context_expires_at=NOW + timedelta(minutes=4),
            prepared_at=NOW,
        )

    def request(self):
        return AccessRequest(
            request_id="request-1",
            subject_id="subject-1",
            tenant_id=self.tenant,
            action="artifact.read",
            resource=ResourceRef(
                tenant_id=self.tenant,
                workspace_id=self.workspace,
                resource_type="artifact",
                resource_id="same-resource-id",
            ),
        )

    def test_provider_port_is_structural_and_receives_non_authorizing_basis(self):
        state = self.state()
        provider: CurrentAuthorizationStateProvider = FakeCurrentStateProvider(state)

        loaded = provider.load_current_state(self.basis(), self.request())

        self.assertIs(loaded, state)

    def test_state_requires_exact_tenant_workspace_and_capability_container_types(self):
        invalid = (
            {"tenant_id": "tenant-a"},
            {"workspace_id": "workspace-a"},
            {"workspace_id": None},
            {"verified_capabilities": []},
        )

        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(TypeError):
                self.state(**changes)

    def test_state_rejects_invalid_identifiers_and_unaware_observation_time(self):
        invalid = (
            {"context_id": " context-1"},
            {"audience": "qe-runtime\nforged"},
            {"identity_revision": "identity/revision"},
            {"scope_revision": "scope revision"},
            {"observed_at": NOW.replace(tzinfo=None)},
        )

        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises((TypeError, ValueError)):
                self.state(**changes)

    def test_state_normalizes_aware_observation_time_to_utc(self):
        offset = timezone(timedelta(hours=8))

        state = self.state(observed_at=NOW.astimezone(offset))

        self.assertEqual(state.observed_at, NOW)
        self.assertIs(state.observed_at.tzinfo, timezone.utc)

    def test_state_snapshots_member_and_revocation_values(self):
        member = Member(member_id="subject-1", tenant_id=self.tenant)
        revocations = RevocationSnapshot.empty(self.tenant, NOW, revision=9)

        state = self.state(member=member, revocations=revocations)

        self.assertEqual(state.member, member)
        self.assertIsNot(state.member, member)
        self.assertEqual(state.revocations, revocations)
        self.assertIsNot(state.revocations, revocations)

    def test_state_rejects_member_and_revocation_scope_substitution(self):
        other_tenant = TenantId("tenant-b")
        invalid = (
            {"member": Member(member_id="subject-b", tenant_id=self.tenant)},
            {"member": Member(member_id="subject-1", tenant_id=other_tenant)},
            {"revocations": RevocationSnapshot.empty(other_tenant, NOW)},
        )

        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.state(**changes)

    def test_state_representation_redacts_identity_scope_and_revisions(self):
        state = self.state()
        rendered = " ".join((str(state), repr(state)))

        for canary in (
            "context-1",
            "authenticator-1",
            "qe-runtime",
            "request-1",
            "principal-1",
            "subject-1",
            "tenant-a",
            "workspace-a",
            "identity-7",
            "scope-11",
        ):
            self.assertNotIn(canary, rendered)
        self.assertIn("non-authorizing", rendered)


class AuthorizedOperationTests(unittest.TestCase):
    def setUp(self):
        self.clock = FixedClock()
        self.registry = _AuthorizedOperationRegistry(clock=self.clock)

    def binding(self, **changes):
        values = {
            "context_id": "context-1",
            "authenticator_id": "authenticator-1",
            "audience": "qe-runtime",
            "request_id": "request-1",
            "principal_id": "principal-1",
            "subject_id": "subject-1",
            "tenant_id": TenantId("tenant-a"),
            "workspace_id": WorkspaceId("workspace-a"),
            "action": "artifact.write",
            "resource_type": "artifact",
            "resource_id": "same-resource-id",
            "decision_id": "cd" * 32,
            "identity_revision": "identity-7",
            "scope_revision": "scope-11",
        }
        values.update(changes)
        return _OperationBinding(**values)

    def issue(self, binding=None):
        selected = self.binding() if binding is None else binding
        return self.registry.issue(selected, expires_at=NOW + timedelta(seconds=20))

    def test_handle_cannot_be_constructed_directly_copied_or_pickled(self):
        with self.assertRaises(TypeError):
            AuthorizedOperation(None, object())

        operation = self.issue()

        with self.assertRaises(TypeError):
            copy.copy(operation)
        with self.assertRaises(TypeError):
            copy.deepcopy(operation)
        with self.assertRaises(TypeError):
            pickle.dumps(operation)

    def test_issuing_registry_verifies_exact_binding(self):
        binding = self.binding()

        operation = self.issue(binding)

        self.registry.verify(operation, binding)
        self.assertTrue(operation.operation_id.startswith("op_"))
        self.assertEqual(operation.issued_at, NOW)
        self.assertEqual(operation.expires_at, NOW + timedelta(seconds=20))

    def test_same_resource_identifier_remains_tenant_and_workspace_scoped(self):
        tenant_a = self.binding()
        tenant_b = self.binding(
            tenant_id=TenantId("tenant-b"),
            workspace_id=WorkspaceId("workspace-b"),
        )
        operation = self.issue(tenant_a)

        with self.assertRaises(OperationAuthorizationError) as caught:
            self.registry.verify(operation, tenant_b)

        self.assertEqual(caught.exception.code, "protected_operation_scope_mismatch")

    def test_handle_is_rejected_by_a_different_registry(self):
        binding = self.binding()
        operation = self.issue(binding)
        other = _AuthorizedOperationRegistry(clock=self.clock)

        with self.assertRaises(OperationAuthorizationError) as caught:
            other.verify(operation, binding)

        self.assertEqual(caught.exception.code, "protected_operation_untrusted")

    def test_forged_and_tampered_handles_fail_closed(self):
        binding = self.binding()
        forged = object.__new__(AuthorizedOperation)
        with self.assertRaises(OperationAuthorizationError) as forged_error:
            self.registry.verify(forged, binding)
        self.assertEqual(forged_error.exception.code, "protected_operation_untrusted")

        operation = self.issue(binding)
        object.__setattr__(
            operation,
            "_AuthorizedOperation__request_id",
            "substituted-request",
        )
        with self.assertRaises(OperationAuthorizationError) as tampered_error:
            self.registry.verify(operation, binding)
        self.assertEqual(tampered_error.exception.code, "protected_operation_tampered")

    def test_expired_retired_and_closed_handles_fail_closed(self):
        binding = self.binding()
        expired = self.issue(binding)
        self.clock.advance(timedelta(seconds=20))
        with self.assertRaises(OperationAuthorizationError) as expired_error:
            self.registry.verify(expired, binding)
        self.assertEqual(expired_error.exception.code, "protected_operation_expired")

        self.clock.set(NOW + timedelta(seconds=21))
        retired = self.registry.issue(
            binding,
            expires_at=NOW + timedelta(seconds=40),
        )
        self.registry.retire(retired)
        with self.assertRaises(OperationAuthorizationError) as retired_error:
            self.registry.verify(retired, binding)
        self.assertEqual(retired_error.exception.code, "protected_operation_untrusted")

        active = self.registry.issue(
            binding,
            expires_at=NOW + timedelta(seconds=40),
        )
        self.registry.close()
        with self.assertRaises(OperationAuthorizationError) as closed_error:
            self.registry.verify(active, binding)
        self.assertEqual(closed_error.exception.code, "protected_operation_registry_closed")

    def test_clock_rollback_and_invalid_expiry_fail_closed(self):
        binding = self.binding()
        operation = self.issue(binding)
        self.clock.advance(timedelta(seconds=1))
        self.registry.verify(operation, binding)
        self.clock.set(NOW - timedelta(minutes=1))

        with self.assertRaises(OperationAuthorizationError) as rollback_error:
            self.registry.verify(operation, binding)
        self.assertEqual(rollback_error.exception.code, "protected_operation_time_regressed")

        fresh = _AuthorizedOperationRegistry(clock=FixedClock())
        with self.assertRaises(OperationAuthorizationError) as expiry_error:
            fresh.issue(binding, expires_at=NOW)
        self.assertEqual(expiry_error.exception.code, "protected_operation_expiry_invalid")

    def test_hard_capacity_is_atomic_under_concurrent_issuance(self):
        registry = _AuthorizedOperationRegistry(
            clock=self.clock,
            max_active_operations=1,
        )
        barrier = threading.Barrier(2)

        def issue_one():
            barrier.wait()
            try:
                return registry.issue(
                    self.binding(),
                    expires_at=NOW + timedelta(seconds=20),
                )
            except OperationAuthorizationError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: issue_one(), range(2)))

        handles = [item for item in results if isinstance(item, AuthorizedOperation)]
        failures = [item for item in results if isinstance(item, str)]
        self.assertEqual(len(handles), 1)
        self.assertEqual(failures, ["protected_operation_capacity_exceeded"])

    def test_handle_and_internal_binding_representations_are_redacted(self):
        binding = self.binding()
        operation = self.issue(binding)
        rendered = " ".join((str(binding), repr(binding), str(operation), repr(operation)))

        for canary in (
            "context-1",
            "request-1",
            "principal-1",
            "subject-1",
            "tenant-a",
            "workspace-a",
            "same-resource-id",
            "identity-7",
            "scope-11",
        ):
            self.assertNotIn(canary, rendered)


if __name__ == "__main__":
    unittest.main()
