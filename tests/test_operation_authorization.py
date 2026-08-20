import copy
import pickle
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from quantum_entanglement.operation_authorization import (
    AuthorizedOperation,
    CurrentAuthorizationState,
    CurrentAuthorizationStateProvider,
    OperationAuthorizationError,
    ProtectedOperationComposer,
    _AuthorizedOperationRegistry,
    _OperationBinding,
)
from quantum_entanglement.request_context import (
    AuthenticatedRequestBinding,
    CallerRequestContext,
    ReauthorizationBasis,
    RequestContextIssuer,
)
from quantum_entanglement.service.secrets import SecretMaterial
from quantum_entanglement.tenancy import (
    AccessRequest,
    CapabilityClaims,
    CapabilityEnvelope,
    CapabilityNonce,
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

NOW = datetime(2026, 8, 20, 8, 0, 0, tzinfo=timezone.utc)
EVIDENCE = "ab" * 32


class HostileBoundaryFailure(BaseException):
    pass


class FakeCurrentStateProvider:
    def __init__(self, state, *, failure=None):
        self.state = state
        self.failure = failure
        self.calls = []

    def load_current_state(self, basis, request):
        self.calls.append((basis, request))
        if self.failure is not None:
            raise self.failure
        if callable(self.state):
            return self.state(basis, request)
        return self.state


class FakeAuthenticator:
    def authenticate(self, claims, credential, *, audience, at):
        if bytes(credential) != b"operation-composer-credential":
            raise RuntimeError("invalid test credential")
        return AuthenticatedRequestBinding(
            authenticator_id="authenticator-1",
            audience=audience,
            request_id=claims.request_id,
            principal_id="principal-1",
            subject_id=claims.subject_id,
            tenant_id=claims.tenant_id,
            workspace_id=claims.workspace_id,
            identity_revision="identity-7",
            scope_revision="scope-11",
            evidence_fingerprint=EVIDENCE,
            authenticated_at=at,
            expires_at=at + timedelta(minutes=5),
        )


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


class ProtectedOperationComposerTests(unittest.TestCase):
    def setUp(self):
        self.clock = FixedClock()
        self.tenant = TenantId("tenant-a")
        self.workspace = WorkspaceId("workspace-a")
        self.issuer = RequestContextIssuer(
            authenticator=FakeAuthenticator(),
            authenticator_id="authenticator-1",
            audience="qe-runtime",
            clock=self.clock,
        )
        self.key_ring = RotatingHMACKeyRing(
            trust_domain="operation-tests",
            policy_version="policy-v1",
            keys=(
                CapabilitySigningKey(
                    kid="issuer-1-key",
                    principal_id="issuer-1",
                    secret=b"\x01" * 32,
                    not_before=NOW - timedelta(days=1),
                    expires_at=NOW + timedelta(days=1),
                    status=KeyStatus.ACTIVE,
                    usages=frozenset((KeyUsage.ROOT,)),
                    root_tenants=frozenset((self.tenant,)),
                ),
            ),
        )
        self.verifier = CapabilityVerifier(
            proof_verifier=self.key_ring,
            trust_domain="operation-tests",
            policy_version="policy-v1",
            audience="qe-runtime",
            clock=self.clock,
        )
        self.authorizer = TenantAuthorizer(
            capability_verifier=self.verifier,
            trust_domain="operation-tests",
            policy_version="policy-v1",
            revision_guard=InMemoryRevocationRevisionGuard(),
            audience="qe-runtime",
            clock=self.clock,
        )
        self.provider = FakeCurrentStateProvider(self.current_state)
        self.composer = self.make_composer()
        self.context = self.issue_context()
        self.request = self.access_request()

    def make_composer(self, *, provider=None, **changes):
        values = {
            "issuer": self.issuer,
            "state_provider": provider or self.provider,
            "authorizer": self.authorizer,
            "clock": self.clock,
            "operation_ttl": timedelta(seconds=20),
            "max_state_age": timedelta(seconds=30),
        }
        values.update(changes)
        return ProtectedOperationComposer(**values)

    def issue_context(
        self,
        *,
        tenant=None,
        workspace="default",
        request_id="request-1",
        subject_id="subject-1",
        issuer=None,
    ):
        selected_tenant = tenant or self.tenant
        selected_workspace = self.workspace if workspace == "default" else workspace
        selected_issuer = issuer or self.issuer
        return selected_issuer.issue(
            CallerRequestContext(
                request_id=request_id,
                subject_id=subject_id,
                tenant_id=selected_tenant,
                workspace_id=selected_workspace,
            ),
            SecretMaterial(b"operation-composer-credential"),
        )

    def access_request(
        self,
        *,
        tenant=None,
        resource_tenant=None,
        workspace="default",
        request_id="request-1",
        subject_id="subject-1",
        action="artifact.write",
        resource_type="artifact",
        resource_id="same-resource-id",
    ):
        selected_tenant = tenant or self.tenant
        selected_resource_tenant = resource_tenant or selected_tenant
        selected_workspace = self.workspace if workspace == "default" else workspace
        return AccessRequest(
            request_id=request_id,
            subject_id=subject_id,
            tenant_id=selected_tenant,
            action=action,
            resource=ResourceRef(
                tenant_id=selected_resource_tenant,
                workspace_id=selected_workspace,
                resource_type=resource_type,
                resource_id=resource_id,
            ),
        )

    def current_state(self, basis, request, **changes):
        selected_tenant = changes.get("tenant_id", basis.tenant_id)
        selected_workspace = changes.get("workspace_id", basis.workspace_id)
        selected_subject = changes.get("subject_id", basis.subject_id)
        if selected_workspace is None:
            raise AssertionError("the default test provider requires workspace scope")
        values = {
            "context_id": basis.context_id,
            "authenticator_id": basis.authenticator_id,
            "audience": basis.audience,
            "request_id": basis.request_id,
            "principal_id": basis.principal_id,
            "subject_id": selected_subject,
            "tenant_id": selected_tenant,
            "workspace_id": selected_workspace,
            "identity_revision": basis.identity_revision,
            "scope_revision": basis.scope_revision,
            "observed_at": self.clock.now(),
            "member": Member(
                member_id=selected_subject,
                tenant_id=selected_tenant,
                role_bindings=(
                    RoleBinding(
                        Role.OWNER,
                        ResourceScope(
                            tenant_id=selected_tenant,
                            workspace_id=selected_workspace,
                        ),
                    ),
                ),
            ),
            "revocations": RevocationSnapshot.empty(
                selected_tenant,
                self.clock.now(),
                revision=9,
            ),
            "verified_capabilities": (),
        }
        values.update(changes)
        return CurrentAuthorizationState(**values)

    def verified_capability(self):
        claims = CapabilityClaims(
            issuer_id="issuer-1",
            subject_id="subject-1",
            action="artifact.write",
            resource=ResourceScope(
                tenant_id=self.tenant,
                workspace_id=self.workspace,
                resource_type="artifact",
                resource_id="same-resource-id",
            ),
            issued_at=NOW - timedelta(seconds=1),
            not_before=NOW - timedelta(seconds=1),
            expires_at=NOW + timedelta(minutes=5),
            audience="qe-runtime",
            nonce=CapabilityNonce("01" * 16),
        )
        envelope = CapabilityEnvelope.signed(
            (claims,),
            signer=self.key_ring,
            root_kid="issuer-1-key",
            clock=self.clock,
        )
        return claims, self.verifier.verify(envelope)

    def assert_code(self, code, callback):
        with self.assertRaises(OperationAuthorizationError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        return caught.exception

    def capture_error(self, code, callback):
        captured = None
        try:
            callback()
        except OperationAuthorizationError as error:
            captured = error
        else:
            self.fail(f"expected OperationAuthorizationError with code {code}")
        self.assertEqual(captured.code, code)
        self.assertIsNone(captured.__cause__)
        self.assertIsNone(captured.__context__)
        return captured

    def capture_control_signal(self, expected_type, callback):
        captured = None
        try:
            callback()
        except BaseException as error:
            captured = error
        else:
            self.fail(f"expected control signal {expected_type.__name__}")
        self.assertIs(type(captured), expected_type)
        self.assertIsNone(captured.__cause__)
        self.assertIsNone(captured.__context__)
        return captured

    def assert_detached_traceback(self, error, *canaries, expected_method="authorize"):
        trace = error.__traceback__
        library_frames = []
        while trace is not None:
            if trace.tb_frame.f_code.co_filename.endswith("/operation_authorization.py"):
                library_frames.append(trace.tb_frame)
            trace = trace.tb_next
        self.assertEqual(
            [frame.f_code.co_name for frame in library_frames],
            [expected_method],
        )
        for frame in library_frames:
            for value in list(frame.f_locals.values()):
                if isinstance(value, str):
                    for canary in canaries:
                        self.assertNotIn(canary, value)
                self.assertNotIsInstance(
                    value,
                    (AccessRequest, CurrentAuthorizationState, ReauthorizationBasis),
                )

    def assert_detached_control_traceback(self, error, expected_method, *canaries):
        trace = error.__traceback__
        library_frames = []
        while trace is not None:
            if trace.tb_frame.f_code.co_filename.endswith("/operation_authorization.py"):
                library_frames.append(trace.tb_frame)
            trace = trace.tb_next
        self.assertEqual(
            [frame.f_code.co_name for frame in library_frames],
            [expected_method, "_raise_control_signal"],
        )
        for frame in library_frames:
            for value in list(frame.f_locals.values()):
                if isinstance(value, str):
                    for canary in canaries:
                        self.assertNotIn(canary, value)
                self.assertNotIsInstance(
                    value,
                    (AccessRequest, CurrentAuthorizationState, ReauthorizationBasis),
                )

    def test_explicit_allow_issues_one_time_non_replayable_opaque_operation(self):
        operation = self.composer.authorize(self.context, self.request)

        self.assertIsInstance(operation, AuthorizedOperation)
        self.composer.consume(operation, self.context, self.request)
        self.assert_code(
            "protected_operation_untrusted",
            lambda: self.composer.consume(operation, self.context, self.request),
        )
        self.assertEqual(len(self.provider.calls), 2)
        for basis, loaded_request in self.provider.calls:
            self.assertIs(type(basis), ReauthorizationBasis)
            self.assertIs(type(loaded_request), AccessRequest)
            self.assertIsNot(loaded_request, self.request)

    def test_direct_state_basis_and_decision_values_are_not_operation_handles(self):
        basis = self.issuer.prepare_reauthorization(self.context, self.request)
        state = self.current_state(basis, self.request)
        decision = self.authorizer.evaluate(
            self.request,
            state.member,
            state.revocations,
            (),
        )

        for value in (basis, state, decision):
            self.assertNotIsInstance(value, AuthorizedOperation)
            self.assert_code(
                "protected_operation_untrusted",
                lambda selected=value: self.composer.consume(
                    selected,
                    self.context,
                    self.request,
                ),
            )

    def test_denial_and_non_decision_truthy_values_never_issue_operations(self):
        self.provider.state = lambda basis, request: self.current_state(
            basis,
            request,
            member=None,
        )
        self.assert_code(
            "protected_operation_denied",
            lambda: self.composer.authorize(self.context, self.request),
        )

        self.provider.state = self.current_state
        with patch.object(TenantAuthorizer, "evaluate", return_value=object()):
            self.assert_code(
                "protected_operation_decision_invalid",
                lambda: self.composer.authorize(self.context, self.request),
            )

    def test_same_request_and_resource_ids_remain_isolated_between_tenants(self):
        other_tenant = TenantId("tenant-b")
        shared_workspace = WorkspaceId("workspace-shared")
        context_a = self.issue_context(workspace=shared_workspace)
        request_a = self.access_request(workspace=shared_workspace)
        context_b = self.issue_context(tenant=other_tenant, workspace=shared_workspace)
        request_b = self.access_request(tenant=other_tenant, workspace=shared_workspace)

        operation_a = self.composer.authorize(context_a, request_a)
        operation_b = self.composer.authorize(context_b, request_b)

        self.assert_code(
            "protected_operation_scope_mismatch",
            lambda: self.composer.consume(operation_a, context_b, request_b),
        )
        self.assert_code(
            "protected_operation_scope_mismatch",
            lambda: self.composer.consume(operation_b, context_a, request_a),
        )
        self.composer.consume(operation_a, context_a, request_a)
        self.composer.consume(operation_b, context_b, request_b)

    def test_workspace_is_mandatory_and_none_is_never_a_wildcard(self):
        context = self.issue_context(workspace=None)
        request = self.access_request(workspace=None)
        calls_before = len(self.provider.calls)

        self.assert_code(
            "protected_operation_workspace_required",
            lambda: self.composer.authorize(context, request),
        )
        self.assertEqual(len(self.provider.calls), calls_before)

    def test_request_identity_and_scope_substitutions_fail_at_admission(self):
        substitutions = (
            self.access_request(request_id="request-2"),
            self.access_request(subject_id="subject-2"),
            self.access_request(tenant=TenantId("tenant-b")),
            self.access_request(resource_tenant=TenantId("tenant-b")),
            self.access_request(workspace=WorkspaceId("workspace-b")),
        )

        for request in substitutions:
            with self.subTest(request=request.to_dict()):
                self.assert_code(
                    "protected_operation_context_rejected",
                    lambda selected=request: self.composer.authorize(self.context, selected),
                )

    def test_valid_handle_rejects_every_presented_operation_substitution(self):
        operation = self.composer.authorize(self.context, self.request)
        substitutions = (
            (
                self.access_request(request_id="request-2"),
                "protected_operation_context_rejected",
            ),
            (
                self.access_request(subject_id="subject-2"),
                "protected_operation_context_rejected",
            ),
            (
                self.access_request(tenant=TenantId("tenant-b")),
                "protected_operation_context_rejected",
            ),
            (
                self.access_request(resource_tenant=TenantId("tenant-b")),
                "protected_operation_context_rejected",
            ),
            (
                self.access_request(workspace=WorkspaceId("workspace-b")),
                "protected_operation_context_rejected",
            ),
            (self.access_request(action="artifact.read"), "protected_operation_scope_mismatch"),
            (
                self.access_request(resource_type="document"),
                "protected_operation_scope_mismatch",
            ),
            (
                self.access_request(resource_id="other-resource"),
                "protected_operation_scope_mismatch",
            ),
        )

        for request, expected_code in substitutions:
            with self.subTest(request=request.to_dict()):
                self.assert_code(
                    expected_code,
                    lambda selected=request: self.composer.consume(
                        operation,
                        self.context,
                        selected,
                    ),
                )
        self.composer.consume(operation, self.context, self.request)

    def test_context_from_wrong_issuer_and_token_at_wrong_composer_are_rejected(self):
        other_issuer = RequestContextIssuer(
            authenticator=FakeAuthenticator(),
            authenticator_id="authenticator-1",
            audience="qe-runtime",
            clock=self.clock,
        )
        foreign_context = self.issue_context(issuer=other_issuer)
        self.assert_code(
            "protected_operation_context_rejected",
            lambda: self.composer.authorize(foreign_context, self.request),
        )

        operation = self.composer.authorize(self.context, self.request)
        other_composer = self.make_composer()
        self.assert_code(
            "protected_operation_untrusted",
            lambda: other_composer.consume(operation, self.context, self.request),
        )
        self.composer.consume(operation, self.context, self.request)

    def test_token_is_bound_to_the_exact_authenticated_actor_context(self):
        operation = self.composer.authorize(self.context, self.request)
        replacement_context = self.issue_context()

        self.assert_code(
            "protected_operation_scope_mismatch",
            lambda: self.composer.consume(
                operation,
                replacement_context,
                self.request,
            ),
        )
        self.composer.consume(operation, self.context, self.request)

    def test_forged_and_reflectively_tampered_tokens_are_rejected(self):
        forged = object.__new__(AuthorizedOperation)
        self.assert_code(
            "protected_operation_untrusted",
            lambda: self.composer.consume(forged, self.context, self.request),
        )

        operation = self.composer.authorize(self.context, self.request)
        object.__setattr__(
            operation,
            "_AuthorizedOperation__workspace_value",
            "workspace-substituted",
        )
        self.assert_code(
            "protected_operation_tampered",
            lambda: self.composer.consume(operation, self.context, self.request),
        )

    def test_current_state_must_match_every_issuer_validated_identity_field(self):
        substitutions = (
            {"context_id": "context-substituted"},
            {"authenticator_id": "authenticator-substituted"},
            {"audience": "other-runtime"},
            {"request_id": "request-substituted"},
            {"principal_id": "principal-substituted"},
            {"subject_id": "subject-substituted"},
            {"tenant_id": TenantId("tenant-b")},
            {"workspace_id": WorkspaceId("workspace-b")},
        )

        for changes in substitutions:
            with self.subTest(changes=changes):
                self.provider.state = lambda basis, request, selected=changes: self.current_state(
                    basis,
                    request,
                    **selected,
                )
                self.assert_code(
                    "protected_operation_state_mismatch",
                    lambda: self.composer.authorize(self.context, self.request),
                )

    def test_identity_and_scope_revision_staleness_are_distinct_failures(self):
        for field, code in (
            ("identity_revision", "protected_operation_identity_revision_stale"),
            ("scope_revision", "protected_operation_scope_revision_stale"),
        ):
            self.provider.state = lambda basis, request, selected=field: self.current_state(
                basis,
                request,
                **{selected: "stale-revision"},
            )
            with self.subTest(field=field):
                self.assert_code(
                    code,
                    lambda: self.composer.authorize(self.context, self.request),
                )

    def test_membership_downgrade_after_issuance_is_denied_at_consume_time(self):
        operation = self.composer.authorize(self.context, self.request)
        self.provider.state = lambda basis, request: self.current_state(
            basis,
            request,
            member=None,
        )

        self.assert_code(
            "protected_operation_denied",
            lambda: self.composer.consume(operation, self.context, self.request),
        )

        self.provider.state = self.current_state
        self.composer.consume(operation, self.context, self.request)

    def test_revision_change_after_issuance_is_denied_at_consume_time(self):
        for field, code in (
            ("identity_revision", "protected_operation_identity_revision_stale"),
            ("scope_revision", "protected_operation_scope_revision_stale"),
        ):
            operation = self.composer.authorize(self.context, self.request)
            self.provider.state = lambda basis, request, selected=field: self.current_state(
                basis,
                request,
                **{selected: "changed-after-issuance"},
            )
            with self.subTest(field=field):
                self.assert_code(
                    code,
                    lambda selected=operation: self.composer.consume(
                        selected,
                        self.context,
                        self.request,
                    ),
                )
            self.provider.state = self.current_state
            self.composer.consume(operation, self.context, self.request)

    def test_capability_revocation_after_issuance_is_denied_at_consume_time(self):
        claims, verified = self.verified_capability()
        unprivileged_member = Member(member_id="subject-1", tenant_id=self.tenant)
        initial_revocations = RevocationSnapshot.empty(self.tenant, NOW, revision=9)
        self.provider.state = lambda basis, request: self.current_state(
            basis,
            request,
            member=unprivileged_member,
            revocations=initial_revocations,
            verified_capabilities=(verified,),
        )
        operation = self.composer.authorize(self.context, self.request)

        revoked = RevocationSnapshot(
            tenant_id=self.tenant,
            revision=10,
            captured_at=NOW,
            revoked_ids=frozenset((claims.revocation_id,)),
        )
        self.provider.state = lambda basis, request: self.current_state(
            basis,
            request,
            member=unprivileged_member,
            revocations=revoked,
            verified_capabilities=(verified,),
        )

        self.assert_code(
            "protected_operation_denied",
            lambda: self.composer.consume(operation, self.context, self.request),
        )

    def test_context_retired_during_action_time_refresh_cannot_consume(self):
        operation = self.composer.authorize(self.context, self.request)

        def retire_during_refresh(basis, request):
            state = self.current_state(basis, request)
            self.issuer.retire(self.context)
            return state

        self.provider.state = retire_during_refresh

        self.assert_code(
            "protected_operation_context_rejected",
            lambda: self.composer.consume(operation, self.context, self.request),
        )

    def test_provider_observation_must_be_fresh_and_not_from_the_future(self):
        cases = (
            (
                NOW - timedelta(seconds=30),
                "protected_operation_state_stale",
            ),
            (
                NOW + timedelta(seconds=31),
                "protected_operation_state_time_invalid",
            ),
        )

        for observed_at, code in cases:
            self.provider.state = lambda basis, request, selected=observed_at: self.current_state(
                basis,
                request,
                observed_at=selected,
            )
            with self.subTest(code=code):
                self.assert_code(
                    code,
                    lambda: self.composer.authorize(self.context, self.request),
                )

    def test_provider_failure_and_invalid_state_are_stable_redacted_errors(self):
        class ExplodingProvider:
            def load_current_state(self, basis, request):
                provider_secret = "provider-traceback-secret-canary"
                try:
                    raise ValueError(provider_secret)
                except ValueError as cause:
                    raise RuntimeError("provider-chain-secret-canary") from cause

        composer = self.make_composer(provider=ExplodingProvider())
        error = self.capture_error(
            "protected_operation_state_unavailable",
            lambda: composer.authorize(self.context, self.request),
        )
        self.assert_detached_traceback(
            error,
            "provider-traceback-secret-canary",
            "provider-chain-secret-canary",
        )
        self.assertNotIn("canary", repr(error))

        self.provider.state = object()
        self.assert_code(
            "protected_operation_state_invalid",
            lambda: self.composer.authorize(self.context, self.request),
        )

    def test_authorizer_exception_chain_and_traceback_state_are_detached(self):
        def explode(*args, **kwargs):
            authorizer_secret = "authorizer-traceback-secret-canary"
            try:
                raise ValueError(authorizer_secret)
            except ValueError as cause:
                raise RuntimeError("authorizer-chain-secret-canary") from cause

        with patch.object(TenantAuthorizer, "evaluate", explode):
            error = self.capture_error(
                "protected_operation_authorizer_failed",
                lambda: self.composer.authorize(self.context, self.request),
            )

        self.assert_detached_traceback(
            error,
            "authorizer-traceback-secret-canary",
            "authorizer-chain-secret-canary",
        )
        self.assertNotIn("canary", repr(error))

    def test_hostile_base_exception_from_dependencies_is_contained(self):
        class HostileProvider:
            def load_current_state(self, basis, request):
                provider_secret = "provider-base-exception-secret-canary"
                try:
                    raise ValueError(provider_secret)
                except ValueError as cause:
                    raise HostileBoundaryFailure("provider-base-chain-canary") from cause

        provider_composer = self.make_composer(provider=HostileProvider())
        provider_error = self.capture_error(
            "protected_operation_state_unavailable",
            lambda: provider_composer.authorize(self.context, self.request),
        )
        self.assert_detached_traceback(
            provider_error,
            "provider-base-exception-secret-canary",
            "provider-base-chain-canary",
        )

        def hostile_authorizer(*args, **kwargs):
            authorizer_secret = "authorizer-base-exception-secret-canary"
            try:
                raise ValueError(authorizer_secret)
            except ValueError as cause:
                raise HostileBoundaryFailure("authorizer-base-chain-canary") from cause

        with patch.object(TenantAuthorizer, "evaluate", hostile_authorizer):
            authorizer_error = self.capture_error(
                "protected_operation_authorizer_failed",
                lambda: self.composer.authorize(self.context, self.request),
            )
        self.assert_detached_traceback(
            authorizer_error,
            "authorizer-base-exception-secret-canary",
            "authorizer-base-chain-canary",
        )

        class HostileClock:
            def now(self):
                clock_secret = "clock-base-exception-secret-canary"
                try:
                    raise ValueError(clock_secret)
                except ValueError as cause:
                    raise HostileBoundaryFailure("clock-base-chain-canary") from cause

        clock_composer = self.make_composer(clock=HostileClock())
        clock_error = self.capture_error(
            "protected_operation_clock_unavailable",
            lambda: clock_composer.authorize(self.context, self.request),
        )
        self.assert_detached_traceback(
            clock_error,
            "clock-base-exception-secret-canary",
            "clock-base-chain-canary",
        )

    def test_consume_and_retire_contain_hostile_base_exceptions(self):
        operation = self.composer.authorize(self.context, self.request)
        self.provider.failure = HostileBoundaryFailure("consume-provider-base-secret-canary")
        consume_error = self.capture_error(
            "protected_operation_state_unavailable",
            lambda: self.composer.consume(operation, self.context, self.request),
        )
        self.assert_detached_traceback(
            consume_error,
            "consume-provider-base-secret-canary",
            expected_method="consume",
        )
        self.provider.failure = None

        def hostile_consume_authorizer(*args, **kwargs):
            raise HostileBoundaryFailure("consume-authorizer-base-secret-canary")

        with patch.object(TenantAuthorizer, "evaluate", hostile_consume_authorizer):
            authorizer_error = self.capture_error(
                "protected_operation_authorizer_failed",
                lambda: self.composer.consume(operation, self.context, self.request),
            )
        self.assert_detached_traceback(
            authorizer_error,
            "consume-authorizer-base-secret-canary",
            expected_method="consume",
        )

        def hostile_retire(registry, selected_operation):
            raise HostileBoundaryFailure("retire-base-exception-secret-canary")

        with patch.object(_AuthorizedOperationRegistry, "retire", hostile_retire):
            retire_error = self.capture_error(
                "protected_operation_internal_failure",
                lambda: self.composer.retire(operation),
            )
        self.assert_detached_traceback(
            retire_error,
            "retire-base-exception-secret-canary",
            expected_method="retire",
        )
        self.composer.retire(operation)

    def test_exact_control_signals_are_reissued_without_third_party_state(self):
        for original in (
            KeyboardInterrupt("keyboard-control-secret-canary"),
            SystemExit("system-control-secret-canary"),
            GeneratorExit("generator-control-secret-canary"),
        ):
            provider = FakeCurrentStateProvider(self.current_state, failure=original)
            composer = self.make_composer(provider=provider)

            signal = self.capture_control_signal(
                type(original),
                lambda selected=composer: selected.authorize(self.context, self.request),
            )

            self.assertIsNot(signal, original)
            rendered = " ".join((str(signal), repr(signal)))
            self.assertNotIn("secret-canary", rendered)
            self.assert_detached_control_traceback(
                signal,
                "authorize",
                "keyboard-control-secret-canary",
                "system-control-secret-canary",
                "generator-control-secret-canary",
            )

    def test_consume_scope_failure_detaches_actor_and_resource_values(self):
        operation = self.composer.authorize(self.context, self.request)
        substituted = self.access_request(resource_id="consume-traceback-secret-canary")

        error = self.capture_error(
            "protected_operation_scope_mismatch",
            lambda: self.composer.consume(operation, self.context, substituted),
        )

        self.assert_detached_traceback(
            error,
            "consume-traceback-secret-canary",
            "tenant-a",
            "workspace-a",
            "subject-1",
            expected_method="consume",
        )
        rendered = " ".join((str(error), repr(error)))
        for canary in (
            "consume-traceback-secret-canary",
            "tenant-a",
            "workspace-a",
            "subject-1",
            "principal-1",
            "identity-7",
            "scope-11",
        ):
            self.assertNotIn(canary, rendered)
        self.composer.consume(operation, self.context, self.request)

    def test_token_expiry_clock_rollback_retire_and_close_fail_closed(self):
        expired = self.composer.authorize(self.context, self.request)
        self.clock.advance(timedelta(seconds=20))
        self.assert_code(
            "protected_operation_expired",
            lambda: self.composer.consume(expired, self.context, self.request),
        )

        self.clock.advance(timedelta(seconds=1))
        retired = self.composer.authorize(self.context, self.request)
        self.composer.retire(retired)
        self.assert_code(
            "protected_operation_untrusted",
            lambda: self.composer.consume(retired, self.context, self.request),
        )

        operation_clock = FixedClock(self.clock.now())
        rollback_composer = self.make_composer(clock=operation_clock)
        active = rollback_composer.authorize(self.context, self.request)
        operation_clock.advance(timedelta(seconds=1))
        operation_clock.set(operation_clock.now() - timedelta(minutes=1))
        self.assert_code(
            "protected_operation_time_regressed",
            lambda: rollback_composer.consume(active, self.context, self.request),
        )

        self.clock.advance(timedelta(seconds=1))
        active = self.composer.authorize(self.context, self.request)
        self.composer.close()
        self.composer.close()
        self.assert_code(
            "protected_operation_composer_closed",
            lambda: self.composer.consume(active, self.context, self.request),
        )
        self.assert_code(
            "protected_operation_composer_closed",
            lambda: self.composer.authorize(self.context, self.request),
        )

    def test_composer_capacity_is_hard_under_concurrent_authorization(self):
        composer = self.make_composer(max_active_operations=1)
        barrier = threading.Barrier(2)

        def authorize_one():
            barrier.wait()
            try:
                return composer.authorize(self.context, self.request)
            except OperationAuthorizationError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: authorize_one(), range(2)))

        handles = [item for item in results if isinstance(item, AuthorizedOperation)]
        failures = [item for item in results if isinstance(item, str)]
        self.assertEqual(len(handles), 1)
        self.assertEqual(failures, ["protected_operation_capacity_exceeded"])

    def test_concurrent_replay_has_exactly_one_successful_consumer(self):
        operation = self.composer.authorize(self.context, self.request)
        barrier = threading.Barrier(2)

        def consume_once():
            barrier.wait()
            try:
                self.composer.consume(operation, self.context, self.request)
                return "consumed"
            except OperationAuthorizationError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: consume_once(), range(2)))

        self.assertEqual(sorted(results), ["consumed", "protected_operation_untrusted"])

    def test_composer_is_non_transferable_and_its_representation_is_redacted(self):
        rendered = repr(self.composer)
        for canary in (
            "tenant-a",
            "workspace-a",
            "subject-1",
            "principal-1",
            "identity-7",
            "scope-11",
        ):
            self.assertNotIn(canary, rendered)

        with self.assertRaises(TypeError):
            copy.copy(self.composer)
        with self.assertRaises(TypeError):
            copy.deepcopy(self.composer)
        with self.assertRaises(TypeError):
            pickle.dumps(self.composer)


if __name__ == "__main__":
    unittest.main()
