import unittest
from datetime import datetime, timedelta, timezone

from quantum_entanglement.operation_authorization import (
    CurrentAuthorizationState,
    CurrentAuthorizationStateProvider,
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


if __name__ == "__main__":
    unittest.main()
