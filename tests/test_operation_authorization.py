import asyncio
import copy
import multiprocessing
import os
import pickle
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import quantum_entanglement.operation_authorization as operation_authorization
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
    RequestContext,
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


def _capture_process_call(callback):
    try:
        callback()
    except OperationAuthorizationError as error:
        return (
            "operation_error",
            error.code,
            error.__cause__ is None,
            error.__context__ is None,
        )
    except BaseException as error:
        return ("unexpected_error", type(error).__name__)
    return ("unexpected_success",)


def _fork_consume_probe(connection, composer, operation, context, request):
    try:
        connection.send(
            _capture_process_call(lambda: composer.consume(operation, context, request))
        )
    finally:
        connection.close()


def _fork_public_path_probe(
    connection,
    composer,
    registry,
    operation,
    binding,
    basis,
    context,
    request,
):
    composer_owner_pid = object.__getattribute__(
        composer,
        "_ProtectedOperationComposer__owner_pid",
    )
    composer_owner_epoch = object.__getattribute__(
        composer,
        "_ProtectedOperationComposer__owner_epoch",
    )
    current_pid, current_epoch = operation_authorization._current_process_identity()
    calls = (
        ("composer_authorize", lambda: composer.authorize(context, request)),
        ("composer_consume", lambda: composer.consume(operation, context, request)),
        ("composer_retire", lambda: composer.retire(operation)),
        ("composer_close", composer.close),
        ("composer_enter", composer.__enter__),
        (
            "composer_exit",
            lambda: composer.__exit__(RuntimeError, RuntimeError("fork-exit-canary"), None),
        ),
        (
            "registry_issue",
            lambda: registry.issue(binding, expires_at=NOW + timedelta(seconds=20)),
        ),
        ("registry_observe_now", registry.observe_now),
        ("registry_verify", lambda: registry.verify(operation, binding)),
        (
            "registry_check_request",
            lambda: registry.check_request(operation, basis, request),
        ),
        (
            "registry_consume_request",
            lambda: registry.consume_request(operation, basis, request),
        ),
        ("registry_retire", lambda: registry.retire(operation)),
        ("registry_close", registry.close),
    )
    try:
        connection.send(
            {
                "identityChanged": (
                    current_pid != composer_owner_pid and current_epoch is not composer_owner_epoch
                ),
                "results": {name: _capture_process_call(callback) for name, callback in calls},
            }
        )
    finally:
        connection.close()


def _fork_inherited_issuer_composer_probe(
    connection,
    issuer,
    state_provider,
    authorizer,
    clock,
):
    try:
        connection.send(
            _capture_process_call(
                lambda: ProtectedOperationComposer(
                    issuer=issuer,
                    state_provider=state_provider,
                    authorizer=authorizer,
                    clock=clock,
                )
            )
        )
    finally:
        connection.close()


def _multiprocessing_transfer_probe(value):
    raise AssertionError(f"non-transferable value reached child: {type(value).__name__}")


class HostileMutationFailure(BaseException):
    pass


class HostileBoundaryFailure(BaseException):
    """Fault carrying secrets through every normal exception surface.

    Once constructed it rejects attribute reads and writes with a second hostile
    exception.  Production boundary code therefore cannot safely inspect or detach
    this object; tests use the base implementation directly for post-call auditing.
    """

    _blocked_reads = frozenset(
        ("args", "__notes__", "__dict__", "__cause__", "__context__", "__traceback__")
    )

    def __init__(self, canary):
        super().__init__(f"{canary}:args")
        self.payload = f"{canary}:custom-attribute"
        self.mutation_attempts = []
        self.__notes__ = [f"{canary}:note"]
        self.__cause__ = ValueError(f"{canary}:stored-cause")
        self.__context__ = RuntimeError(f"{canary}:stored-context")
        self.armed = True

    def __getattribute__(self, name):
        if name in type(self)._blocked_reads and BaseException.__getattribute__(
            self, "__dict__"
        ).get("armed", False):
            payload = BaseException.__getattribute__(self, "__dict__")["payload"]
            raise HostileMutationFailure(f"{payload}:getter:{name}")
        return BaseException.__getattribute__(self, name)

    def __setattr__(self, name, value):
        state = BaseException.__getattribute__(self, "__dict__")
        if state.get("armed", False):
            state["mutation_attempts"].append(name)
            try:
                raise ValueError(f"{state['payload']}:setter-cause:{name}")
            except ValueError as cause:
                raise HostileMutationFailure(f"{state['payload']}:setter:{name}") from cause
        BaseException.__setattr__(self, name, value)


class FaultingDependencyDescriptor:
    """Dependency whose structural method lookup raises one selected fault."""

    def __init__(self, method_name, failure):
        self.method_name = method_name
        self.failure = failure

    def __getattribute__(self, name):
        if name == object.__getattribute__(self, "method_name"):
            raise object.__getattribute__(self, "failure")
        return object.__getattribute__(self, name)


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

    def test_registry_cannot_be_copied_or_pickled(self):
        with self.assertRaises(TypeError):
            copy.copy(self.registry)
        with self.assertRaises(TypeError):
            copy.deepcopy(self.registry)
        with self.assertRaises(TypeError):
            pickle.dumps(self.registry)

    def test_pid_drift_lazily_refreshes_epoch_without_at_fork_hook(self):
        stale_epoch = object()
        with patch.object(operation_authorization, "_PROCESS_PID", os.getpid() + 1):
            with patch.object(operation_authorization, "_PROCESS_EPOCH", stale_epoch):
                process_pid, process_epoch = operation_authorization._current_process_identity()

        self.assertEqual(process_pid, os.getpid())
        self.assertIsNot(process_epoch, stale_epoch)

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
                    secret=b"signing-secret-canary-1234567890",
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

    def assert_detached_traceback(
        self,
        error,
        *canaries,
        expected_method="authorize",
        forbidden=(),
    ):
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
                for selected in forbidden:
                    self.assertIsNot(value, selected)
                if isinstance(value, str):
                    for canary in canaries:
                        self.assertNotIn(canary, value)
                self.assertNotIsInstance(
                    value,
                    (AccessRequest, CurrentAuthorizationState, ReauthorizationBasis),
                )

    def assert_detached_control_traceback(
        self,
        error,
        expected_method,
        *canaries,
        forbidden=(),
    ):
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
                for selected in forbidden:
                    self.assertIsNot(value, selected)
                if isinstance(value, str):
                    for canary in canaries:
                        self.assertNotIn(canary, value)
                self.assertNotIsInstance(
                    value,
                    (
                        AccessRequest,
                        AuthorizedOperation,
                        CurrentAuthorizationState,
                        FakeCurrentStateProvider,
                        ProtectedOperationComposer,
                        ReauthorizationBasis,
                        RequestContext,
                        RequestContextIssuer,
                        RotatingHMACKeyRing,
                        SecretMaterial,
                        TenantAuthorizer,
                        _AuthorizedOperationRegistry,
                        _OperationBinding,
                    ),
                )

    def assert_detached_process_failure(self, error, expected_method, *forbidden):
        self.assertIs(type(error), OperationAuthorizationError)
        self.assertEqual(error.code, "protected_operation_process_mismatch")
        self.assertEqual(error.args, ("protected_operation_process_mismatch",))
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        self.assertEqual(getattr(error, "__notes__", ()), ())
        self.assertEqual(getattr(error, "__dict__", {}), {})
        library_frames = []
        trace = error.__traceback__
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
                for selected in forbidden:
                    self.assertIsNot(value, selected)
                self.assertNotIsInstance(
                    value,
                    (
                        AccessRequest,
                        AuthorizedOperation,
                        CurrentAuthorizationState,
                        FakeCurrentStateProvider,
                        ProtectedOperationComposer,
                        ReauthorizationBasis,
                        RequestContext,
                        RequestContextIssuer,
                        RotatingHMACKeyRing,
                        SecretMaterial,
                        TenantAuthorizer,
                        _AuthorizedOperationRegistry,
                        _OperationBinding,
                    ),
                )
                if isinstance(value, bytes):
                    self.assertNotIn(b"signing-secret-canary", value)
                if isinstance(value, str):
                    self.assertNotIn("secret-canary", value)

    def assert_original_control_traceback_is_scrubbed(
        self,
        original,
        *canaries,
        forbidden=(),
    ):
        trace = original.__traceback__
        self.assertIsNotNone(trace)
        while trace is not None:
            for value in list(trace.tb_frame.f_locals.values()):
                for selected in forbidden:
                    self.assertIsNot(value, selected)
                self.assertIsNot(value, original)
                self.assertNotIsInstance(
                    value,
                    (
                        AccessRequest,
                        AuthorizedOperation,
                        CurrentAuthorizationState,
                        FakeCurrentStateProvider,
                        ProtectedOperationComposer,
                        ReauthorizationBasis,
                        RequestContext,
                        RequestContextIssuer,
                        RotatingHMACKeyRing,
                        SecretMaterial,
                        TenantAuthorizer,
                        _AuthorizedOperationRegistry,
                        _OperationBinding,
                    ),
                )
                if isinstance(value, str):
                    for canary in canaries:
                        self.assertNotIn(canary, value)
            trace = trace.tb_next

    def assert_hostile_original_is_contained(self, original, canary, *, forbidden=()):
        state = BaseException.__getattribute__(original, "__dict__")
        self.assertEqual(state["mutation_attempts"], [])
        for field in HostileBoundaryFailure._blocked_reads:
            with self.subTest(field=field), self.assertRaises(HostileMutationFailure):
                getattr(original, field)

        raw_surfaces = (
            BaseException.__getattribute__(original, "args"),
            BaseException.__getattribute__(original, "__notes__"),
            state,
            BaseException.__getattribute__(original, "__cause__"),
            BaseException.__getattribute__(original, "__context__"),
        )
        self.assertIn(canary, repr(raw_surfaces))

        trace = BaseException.__getattribute__(original, "__traceback__")
        self.assertIsNotNone(trace)
        while trace is not None:
            frame = trace.tb_frame
            for value in list(frame.f_locals.values()):
                for selected in forbidden:
                    self.assertIsNot(value, selected)
                self.assertIsNot(value, original)
                self.assertNotIsInstance(
                    value,
                    (
                        AccessRequest,
                        AuthorizedOperation,
                        CurrentAuthorizationState,
                        ReauthorizationBasis,
                        RequestContext,
                    ),
                )
                if isinstance(value, str):
                    self.assertNotIn(canary, value)
            trace = trace.tb_next

    def assert_clean_public_failure(
        self,
        error,
        original,
        canary,
        *,
        method,
        forbidden=(),
    ):
        self.assertIs(type(error), OperationAuthorizationError)
        self.assertIsNot(error, original)
        rendered = repr(
            (
                error.args,
                getattr(error, "__notes__", ()),
                error.__dict__,
                error.__cause__,
                error.__context__,
                str(error),
                repr(error),
            )
        )
        self.assertNotIn(canary, rendered)
        self.assert_detached_traceback(
            error,
            canary,
            expected_method=method,
            forbidden=forbidden,
        )
        self.assert_hostile_original_is_contained(original, canary, forbidden=forbidden)

    def test_constructor_descriptor_base_exceptions_are_contained(self):
        cases = (
            (
                "provider",
                "load_current_state",
                "protected_operation_state_unavailable",
            ),
            (
                "clock",
                "now",
                "protected_operation_clock_unavailable",
            ),
        )
        for label, method_name, expected_code in cases:
            with self.subTest(dependency=label):
                canary = f"constructor-{label}-descriptor-hostile-secret-canary"
                original = HostileBoundaryFailure(canary)
                dependency = FaultingDependencyDescriptor(method_name, original)
                uninitialized = object.__new__(ProtectedOperationComposer)
                active_error = RuntimeError(f"{canary}-active-caller")
                active_error.issuer = self.issuer
                active_error.provider = dependency
                active_error.authorizer = self.authorizer
                active_error.clock = self.clock
                values = {
                    "issuer": self.issuer,
                    "state_provider": (dependency if label == "provider" else self.provider),
                    "authorizer": self.authorizer,
                    "clock": dependency if label == "clock" else self.clock,
                }

                try:
                    raise active_error
                except RuntimeError:
                    error = self.capture_error(
                        expected_code,
                        lambda selected=uninitialized, selected_values=values: (
                            ProtectedOperationComposer.__init__(
                                selected,
                                **selected_values,
                            )
                        ),
                    )

                forbidden = (
                    uninitialized,
                    dependency,
                    self.issuer,
                    self.provider,
                    self.authorizer,
                    self.verifier,
                    self.key_ring,
                    self.clock,
                    self.context,
                    self.request,
                    original,
                    active_error,
                    values,
                    b"signing-secret-canary-1234567890",
                )
                self.assert_clean_public_failure(
                    error,
                    original,
                    canary,
                    method="__init__",
                    forbidden=forbidden,
                )

    def test_constructor_descriptor_attribute_errors_clear_dependency_frames(self):
        cases = (
            (
                "provider",
                "load_current_state",
                "protected_operation_state_unavailable",
            ),
            (
                "clock",
                "now",
                "protected_operation_clock_unavailable",
            ),
        )
        for label, method_name, expected_code in cases:
            with self.subTest(dependency=label):
                canary = f"constructor-{label}-attribute-error-secret-canary"
                original = AttributeError(canary)
                dependency = FaultingDependencyDescriptor(method_name, original)
                uninitialized = object.__new__(ProtectedOperationComposer)
                values = {
                    "issuer": self.issuer,
                    "state_provider": (dependency if label == "provider" else self.provider),
                    "authorizer": self.authorizer,
                    "clock": dependency if label == "clock" else self.clock,
                }

                error = self.capture_error(
                    expected_code,
                    lambda selected=uninitialized, selected_values=values: (
                        ProtectedOperationComposer.__init__(
                            selected,
                            **selected_values,
                        )
                    ),
                )

                forbidden = (
                    uninitialized,
                    dependency,
                    self.issuer,
                    self.provider,
                    self.authorizer,
                    self.verifier,
                    self.key_ring,
                    self.clock,
                    self.context,
                    self.request,
                    original,
                    values,
                    b"signing-secret-canary-1234567890",
                )
                self.assert_detached_traceback(
                    error,
                    canary,
                    expected_method="__init__",
                    forbidden=forbidden,
                )
                self.assert_original_control_traceback_is_scrubbed(
                    original,
                    canary,
                    forbidden=forbidden,
                )

    def test_constructor_statically_rejects_hostile_subclass_initializer(self):
        lookups = []
        composer_fault = HostileBoundaryFailure("constructor-composer-initializer-secret-canary")
        registry_fault = HostileBoundaryFailure("constructor-registry-initializer-secret-canary")

        class LookupHostileComposer(ProtectedOperationComposer):
            def __getattribute__(self, name):
                if name == "_initialize":
                    lookups.append("composer")
                    raise composer_fault
                return super().__getattribute__(name)

        class LookupHostileRegistry(_AuthorizedOperationRegistry):
            def __getattribute__(self, name):
                if name == "_initialize":
                    lookups.append("registry")
                    raise registry_fault
                return super().__getattribute__(name)

        composer = object.__new__(LookupHostileComposer)
        registry = object.__new__(LookupHostileRegistry)
        composer_error = self.capture_error(
            "protected_operation_internal_failure",
            lambda: ProtectedOperationComposer.__init__(
                composer,
                issuer=self.issuer,
                state_provider=self.provider,
                authorizer=self.authorizer,
                clock=self.clock,
                operation_ttl=timedelta(seconds=20),
                max_state_age=timedelta(seconds=30),
            ),
        )
        registry_error = self.capture_error(
            "protected_operation_internal_failure",
            lambda: _AuthorizedOperationRegistry.__init__(registry, clock=self.clock),
        )

        self.assertEqual(lookups, [])
        forbidden = (
            composer,
            registry,
            self.issuer,
            self.provider,
            self.authorizer,
            self.verifier,
            self.key_ring,
            self.clock,
            self.context,
            self.request,
            composer_fault,
            registry_fault,
            b"signing-secret-canary-1234567890",
        )
        for error in (composer_error, registry_error):
            self.assert_detached_traceback(
                error,
                "constructor-composer-initializer-secret-canary",
                "constructor-registry-initializer-secret-canary",
                expected_method="__init__",
                forbidden=forbidden,
            )

    def test_constructor_lock_failures_leave_exact_objects_uninitialized(self):
        real_rlock = threading.RLock
        composer_failure = RuntimeError("composer-lock-secret-canary")
        composer_lock_calls = 0

        def fail_second_lock():
            nonlocal composer_lock_calls
            composer_lock_calls += 1
            if composer_lock_calls == 2:
                raise composer_failure
            return real_rlock()

        composer = object.__new__(ProtectedOperationComposer)
        with patch.object(operation_authorization.threading, "RLock", fail_second_lock):
            composer_error = self.capture_error(
                "protected_operation_internal_failure",
                lambda: ProtectedOperationComposer.__init__(
                    composer,
                    issuer=self.issuer,
                    state_provider=self.provider,
                    authorizer=self.authorizer,
                    clock=self.clock,
                    operation_ttl=timedelta(seconds=20),
                    max_state_age=timedelta(seconds=30),
                ),
            )

        self.assertEqual(composer_lock_calls, 2)
        for slot in ProtectedOperationComposer.__slots__:
            with self.subTest(owner="composer", slot=slot), self.assertRaises(AttributeError):
                object.__getattribute__(composer, f"_ProtectedOperationComposer{slot}")

        registry_failure = RuntimeError("registry-lock-secret-canary")

        def fail_registry_lock():
            raise registry_failure

        registry = object.__new__(_AuthorizedOperationRegistry)
        with patch.object(operation_authorization.threading, "RLock", fail_registry_lock):
            registry_error = self.capture_error(
                "protected_operation_internal_failure",
                lambda: _AuthorizedOperationRegistry.__init__(registry, clock=self.clock),
            )

        for slot in _AuthorizedOperationRegistry.__slots__:
            with self.subTest(owner="registry", slot=slot), self.assertRaises(AttributeError):
                object.__getattribute__(registry, f"_AuthorizedOperationRegistry{slot}")

        forbidden = (
            composer,
            registry,
            self.issuer,
            self.provider,
            self.authorizer,
            self.verifier,
            self.key_ring,
            self.clock,
            self.context,
            self.request,
            composer_failure,
            registry_failure,
            b"signing-secret-canary-1234567890",
        )
        for error in (composer_error, registry_error):
            self.assert_detached_traceback(
                error,
                "composer-lock-secret-canary",
                "registry-lock-secret-canary",
                expected_method="__init__",
                forbidden=forbidden,
            )

    def test_constructor_descriptor_control_signals_are_reissued_cleanly(self):
        cases = (
            (KeyboardInterrupt, "keyboard"),
            (SystemExit, "system"),
            (GeneratorExit, "generator"),
            (asyncio.CancelledError, "cancelled"),
        )
        dependencies = (
            ("provider", "load_current_state"),
            ("clock", "now"),
        )
        for label, method_name in dependencies:
            for signal_type, signal_label in cases:
                with self.subTest(dependency=label, signal=signal_type.__name__):
                    canary = f"constructor-{label}-{signal_label}-descriptor-control-secret-canary"
                    original = signal_type(canary)
                    dependency = FaultingDependencyDescriptor(method_name, original)
                    uninitialized = object.__new__(ProtectedOperationComposer)
                    active_error = RuntimeError(f"{canary}-active-caller")
                    active_error.issuer = self.issuer
                    active_error.provider = dependency
                    active_error.authorizer = self.authorizer
                    active_error.clock = self.clock
                    values = {
                        "issuer": self.issuer,
                        "state_provider": (dependency if label == "provider" else self.provider),
                        "authorizer": self.authorizer,
                        "clock": dependency if label == "clock" else self.clock,
                    }

                    try:
                        raise active_error
                    except RuntimeError:
                        signal = self.capture_control_signal(
                            signal_type,
                            lambda selected=uninitialized, selected_values=values: (
                                ProtectedOperationComposer.__init__(
                                    selected,
                                    **selected_values,
                                )
                            ),
                        )

                    self.assertIsNot(signal, original)
                    self.assertEqual(signal.args, (1,) if signal_type is SystemExit else ())
                    self.assertNotIn(canary, " ".join((str(signal), repr(signal))))
                    forbidden = (
                        uninitialized,
                        dependency,
                        self.issuer,
                        self.provider,
                        self.authorizer,
                        self.verifier,
                        self.key_ring,
                        self.clock,
                        self.context,
                        self.request,
                        original,
                        active_error,
                        values,
                        b"signing-secret-canary-1234567890",
                    )
                    self.assert_detached_control_traceback(
                        signal,
                        "__init__",
                        canary,
                        forbidden=forbidden,
                    )
                    self.assert_original_control_traceback_is_scrubbed(
                        original,
                        canary,
                        forbidden=forbidden,
                    )

    def test_constructor_preserves_normal_dependency_identity_and_default_deny(self):
        provider = FakeCurrentStateProvider(
            lambda basis, request: self.current_state(
                basis,
                request,
                member=None,
            )
        )
        composer = self.make_composer(provider=provider)
        registry = object.__getattribute__(
            composer,
            "_ProtectedOperationComposer__registry",
        )

        self.assertIs(
            object.__getattribute__(composer, "_ProtectedOperationComposer__issuer"),
            self.issuer,
        )
        self.assertIs(
            object.__getattribute__(composer, "_ProtectedOperationComposer__provider"),
            provider,
        )
        self.assertIs(
            object.__getattribute__(composer, "_ProtectedOperationComposer__authorizer"),
            self.authorizer,
        )
        self.assertIs(
            object.__getattribute__(registry, "_AuthorizedOperationRegistry__clock"),
            self.clock,
        )
        self.assert_code(
            "protected_operation_denied",
            lambda: composer.authorize(self.context, self.request),
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

    def test_authorize_revalidates_issuer_process_before_state_or_authorizer_access(self):
        owner_epoch = object.__getattribute__(
            self.issuer,
            "_RequestContextIssuer__owner_epoch",
        )
        object.__setattr__(self.issuer, "_RequestContextIssuer__owner_epoch", object())
        try:
            self.assert_code(
                "protected_operation_process_mismatch",
                lambda: self.composer.authorize(self.context, self.request),
            )
            self.assertEqual(self.provider.calls, [])
        finally:
            object.__setattr__(
                self.issuer,
                "_RequestContextIssuer__owner_epoch",
                owner_epoch,
            )

        operation = self.composer.authorize(self.context, self.request)
        self.assertIsInstance(operation, AuthorizedOperation)
        self.composer.consume(operation, self.context, self.request)

    def test_fresh_composer_process_failure_detaches_every_constructor_dependency(self):
        uninitialized = object.__new__(ProtectedOperationComposer)
        signing_secret = b"signing-secret-canary-1234567890"
        active_error = RuntimeError("constructor-body-secret-canary")
        active_error.request = self.request
        active_error.provider = self.provider
        active_error.authorizer = self.authorizer
        active_error.key_ring = self.key_ring
        owner_epoch = object.__getattribute__(
            self.issuer,
            "_RequestContextIssuer__owner_epoch",
        )
        object.__setattr__(self.issuer, "_RequestContextIssuer__owner_epoch", object())
        try:
            try:
                raise active_error
            except RuntimeError:
                error = self.capture_error(
                    "protected_operation_process_mismatch",
                    lambda: ProtectedOperationComposer.__init__(
                        uninitialized,
                        issuer=self.issuer,
                        state_provider=self.provider,
                        authorizer=self.authorizer,
                        clock=self.clock,
                        operation_ttl=timedelta(seconds=20),
                        max_state_age=timedelta(seconds=30),
                    ),
                )
        finally:
            object.__setattr__(
                self.issuer,
                "_RequestContextIssuer__owner_epoch",
                owner_epoch,
            )

        self.assert_detached_process_failure(
            error,
            "__init__",
            uninitialized,
            self.issuer,
            self.provider,
            self.authorizer,
            self.verifier,
            self.key_ring,
            self.clock,
            signing_secret,
            active_error,
        )

    def test_every_composer_process_failure_detaches_all_authorization_state(self):
        operation = self.composer.authorize(self.context, self.request)
        registry = object.__getattribute__(
            self.composer,
            "_ProtectedOperationComposer__registry",
        )
        exit_error = RuntimeError("composer-exit-secret-canary")
        exit_trace = object()
        owner_epoch = object.__getattribute__(
            self.composer,
            "_ProtectedOperationComposer__owner_epoch",
        )
        object.__setattr__(self.composer, "_ProtectedOperationComposer__owner_epoch", object())
        calls = (
            ("authorize", lambda: self.composer.authorize(self.context, self.request)),
            (
                "consume",
                lambda: self.composer.consume(operation, self.context, self.request),
            ),
            ("retire", lambda: self.composer.retire(operation)),
            ("close", self.composer.close),
            ("__enter__", self.composer.__enter__),
            (
                "__exit__",
                lambda: self.composer.__exit__(RuntimeError, exit_error, exit_trace),
            ),
        )
        forbidden = (
            self.composer,
            self.issuer,
            registry,
            self.provider,
            self.authorizer,
            self.verifier,
            self.key_ring,
            self.clock,
            self.context,
            self.request,
            operation,
            exit_error,
            exit_trace,
            b"signing-secret-canary-1234567890",
        )
        try:
            for expected_method, call in calls:
                with self.subTest(method=expected_method):
                    active_error = RuntimeError(f"composer-{expected_method}-body-secret-canary")
                    active_error.composer = self.composer
                    active_error.registry = registry
                    active_error.request = self.request
                    active_error.provider = self.provider
                    active_error.authorizer = self.authorizer
                    active_error.key_ring = self.key_ring
                    try:
                        raise active_error
                    except RuntimeError:
                        error = self.capture_error(
                            "protected_operation_process_mismatch",
                            call,
                        )
                    self.assert_detached_process_failure(
                        error,
                        expected_method,
                        *forbidden,
                        active_error,
                    )
        finally:
            object.__setattr__(
                self.composer,
                "_ProtectedOperationComposer__owner_epoch",
                owner_epoch,
            )

        self.composer.consume(operation, self.context, self.request)

    def test_every_registry_process_failure_detaches_all_operation_state(self):
        operation = self.composer.authorize(self.context, self.request)
        registry = object.__getattribute__(
            self.composer,
            "_ProtectedOperationComposer__registry",
        )
        basis = self.issuer.prepare_reauthorization(self.context, self.request)
        binding = _OperationBinding(
            context_id=basis.context_id,
            authenticator_id=basis.authenticator_id,
            audience=basis.audience,
            request_id=basis.request_id,
            principal_id=basis.principal_id,
            subject_id=basis.subject_id,
            tenant_id=basis.tenant_id,
            workspace_id=self.workspace,
            action=self.request.action,
            resource_type=self.request.resource.resource_type,
            resource_id=self.request.resource.resource_id,
            decision_id="cd" * 32,
            identity_revision=basis.identity_revision,
            scope_revision=basis.scope_revision,
        )
        expires_at = self.clock.now() + timedelta(seconds=20)
        owner_epoch = object.__getattribute__(
            registry,
            "_AuthorizedOperationRegistry__owner_epoch",
        )
        object.__setattr__(registry, "_AuthorizedOperationRegistry__owner_epoch", object())
        calls = (
            ("issue", lambda: registry.issue(binding, expires_at=expires_at)),
            ("observe_now", registry.observe_now),
            ("verify", lambda: registry.verify(operation, binding)),
            (
                "check_request",
                lambda: registry.check_request(operation, basis, self.request),
            ),
            (
                "consume_request",
                lambda: registry.consume_request(operation, basis, self.request),
            ),
            ("retire", lambda: registry.retire(operation)),
            ("close", registry.close),
        )
        forbidden = (
            self.composer,
            self.issuer,
            registry,
            self.provider,
            self.authorizer,
            self.verifier,
            self.key_ring,
            self.clock,
            self.context,
            self.request,
            operation,
            basis,
            binding,
            expires_at,
            b"signing-secret-canary-1234567890",
        )
        try:
            for expected_method, call in calls:
                with self.subTest(method=expected_method):
                    active_error = RuntimeError(f"registry-{expected_method}-body-secret-canary")
                    active_error.composer = self.composer
                    active_error.registry = registry
                    active_error.request = self.request
                    active_error.provider = self.provider
                    active_error.authorizer = self.authorizer
                    active_error.key_ring = self.key_ring
                    try:
                        raise active_error
                    except RuntimeError:
                        error = self.capture_error(
                            "protected_operation_process_mismatch",
                            call,
                        )
                    self.assert_detached_process_failure(
                        error,
                        expected_method,
                        *forbidden,
                        active_error,
                    )
        finally:
            object.__setattr__(
                registry,
                "_AuthorizedOperationRegistry__owner_epoch",
                owner_epoch,
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

    def test_final_consume_reauthorization_rejects_every_context_binding_drift(self):
        substitutions = (
            {"context_id": "context-substituted"},
            {"authenticator_id": "authenticator-substituted"},
            {"audience": "other-runtime"},
            {"request_id": "request-substituted"},
            {"principal_id": "principal-substituted"},
            {"subject_id": "subject-substituted"},
            {"tenant_id": TenantId("tenant-b")},
            {"workspace_id": WorkspaceId("workspace-b")},
            {"identity_revision": "identity-substituted"},
            {"scope_revision": "scope-substituted"},
            {"evidence_fingerprint": "cd" * 32},
            {"authenticated_at": NOW + timedelta(microseconds=1)},
            {"context_issued_at": NOW + timedelta(microseconds=1)},
            {"context_expires_at": NOW + timedelta(minutes=6)},
        )
        real_prepare = RequestContextIssuer.prepare_reauthorization

        for changes in substitutions:
            with self.subTest(changes=changes):
                operation = self.composer.authorize(self.context, self.request)
                calls = 0

                def drift_final_basis(issuer, context, request, selected=changes):
                    nonlocal calls
                    calls += 1
                    basis = real_prepare(issuer, context, request)
                    return replace(basis, **selected) if calls == 2 else basis

                with patch.object(
                    RequestContextIssuer,
                    "prepare_reauthorization",
                    drift_final_basis,
                ):
                    self.assert_code(
                        "protected_operation_context_rejected",
                        lambda selected=operation: self.composer.consume(
                            selected,
                            self.context,
                            self.request,
                        ),
                    )
                self.assertEqual(calls, 2)
                self.composer.consume(operation, self.context, self.request)

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
        provider_canary = "provider-hostile-surface-secret-canary"
        provider_fault = HostileBoundaryFailure(provider_canary)
        provider = FakeCurrentStateProvider(self.current_state, failure=provider_fault)
        provider_composer = self.make_composer(provider=provider)
        provider_error = self.capture_error(
            "protected_operation_state_unavailable",
            lambda: provider_composer.authorize(self.context, self.request),
        )
        self.assert_clean_public_failure(
            provider_error,
            provider_fault,
            provider_canary,
            method="authorize",
        )

        authorizer_canary = "authorizer-hostile-surface-secret-canary"
        authorizer_fault = HostileBoundaryFailure(authorizer_canary)

        def hostile_authorizer(*args, **kwargs):
            raise authorizer_fault

        with patch.object(TenantAuthorizer, "evaluate", hostile_authorizer):
            authorizer_error = self.capture_error(
                "protected_operation_authorizer_failed",
                lambda: self.composer.authorize(self.context, self.request),
            )
        self.assert_clean_public_failure(
            authorizer_error,
            authorizer_fault,
            authorizer_canary,
            method="authorize",
        )

        clock_canary = "clock-hostile-surface-secret-canary"
        clock_fault = HostileBoundaryFailure(clock_canary)

        class HostileClock:
            def now(self):
                raise clock_fault

        clock_composer = self.make_composer(clock=HostileClock())
        clock_error = self.capture_error(
            "protected_operation_clock_unavailable",
            lambda: clock_composer.authorize(self.context, self.request),
        )
        self.assert_clean_public_failure(
            clock_error,
            clock_fault,
            clock_canary,
            method="authorize",
        )

    def test_consume_and_retire_contain_hostile_base_exceptions(self):
        operation = self.composer.authorize(self.context, self.request)
        provider_canary = "consume-provider-hostile-secret-canary"
        provider_fault = HostileBoundaryFailure(provider_canary)
        self.provider.failure = provider_fault
        consume_error = self.capture_error(
            "protected_operation_state_unavailable",
            lambda: self.composer.consume(operation, self.context, self.request),
        )
        self.assert_clean_public_failure(
            consume_error,
            provider_fault,
            provider_canary,
            method="consume",
        )
        self.provider.failure = None

        authorizer_canary = "consume-authorizer-hostile-secret-canary"
        authorizer_fault = HostileBoundaryFailure(authorizer_canary)

        def hostile_consume_authorizer(*args, **kwargs):
            raise authorizer_fault

        with patch.object(TenantAuthorizer, "evaluate", hostile_consume_authorizer):
            authorizer_error = self.capture_error(
                "protected_operation_authorizer_failed",
                lambda: self.composer.consume(operation, self.context, self.request),
            )
        self.assert_clean_public_failure(
            authorizer_error,
            authorizer_fault,
            authorizer_canary,
            method="consume",
        )

        retire_canary = "retire-registry-hostile-secret-canary"
        retire_fault = HostileBoundaryFailure(retire_canary)

        def hostile_retire(registry, selected_operation):
            raise retire_fault

        with patch.object(_AuthorizedOperationRegistry, "retire", hostile_retire):
            retire_error = self.capture_error(
                "protected_operation_internal_failure",
                lambda: self.composer.retire(operation),
            )
        self.assert_clean_public_failure(
            retire_error,
            retire_fault,
            retire_canary,
            method="retire",
        )
        self.composer.retire(operation)

    def test_registry_and_action_time_clock_hostile_faults_are_contained(self):
        issue_canary = "authorize-registry-issue-hostile-secret-canary"
        issue_fault = HostileBoundaryFailure(issue_canary)
        issue_composer = self.make_composer()

        def hostile_issue(*args, **kwargs):
            raise issue_fault

        with patch.object(_AuthorizedOperationRegistry, "issue", hostile_issue):
            issue_error = self.capture_error(
                "protected_operation_internal_failure",
                lambda: issue_composer.authorize(self.context, self.request),
            )
        self.assert_clean_public_failure(
            issue_error,
            issue_fault,
            issue_canary,
            method="authorize",
        )

        for method_name in ("check_request", "consume_request"):
            with self.subTest(method=method_name):
                operation = self.composer.authorize(self.context, self.request)
                registry_canary = f"consume-registry-{method_name}-hostile-secret-canary"
                registry_fault = HostileBoundaryFailure(registry_canary)

                def hostile_registry_call(*args, selected=registry_fault, **kwargs):
                    raise selected

                with patch.object(
                    _AuthorizedOperationRegistry,
                    method_name,
                    hostile_registry_call,
                ):
                    registry_error = self.capture_error(
                        "protected_operation_internal_failure",
                        lambda selected=operation: self.composer.consume(
                            selected,
                            self.context,
                            self.request,
                        ),
                    )
                self.assert_clean_public_failure(
                    registry_error,
                    registry_fault,
                    registry_canary,
                    method="consume",
                )
                self.composer.consume(operation, self.context, self.request)

        class SwitchableClock(FixedClock):
            def __init__(self):
                super().__init__()
                self.failure = None

            def now(self):
                if self.failure is not None:
                    raise self.failure
                return super().now()

        operation_clock = SwitchableClock()
        clock_composer = self.make_composer(clock=operation_clock)
        clock_operation = clock_composer.authorize(self.context, self.request)
        clock_canary = "consume-clock-hostile-secret-canary"
        clock_fault = HostileBoundaryFailure(clock_canary)
        operation_clock.failure = clock_fault

        clock_error = self.capture_error(
            "protected_operation_clock_unavailable",
            lambda: clock_composer.consume(clock_operation, self.context, self.request),
        )
        self.assert_clean_public_failure(
            clock_error,
            clock_fault,
            clock_canary,
            method="consume",
        )
        operation_clock.failure = None
        clock_composer.consume(clock_operation, self.context, self.request)

    def test_exact_control_signals_are_reissued_without_third_party_state(self):
        for original in (
            KeyboardInterrupt("keyboard-control-secret-canary"),
            SystemExit("system-control-secret-canary"),
            GeneratorExit("generator-control-secret-canary"),
            asyncio.CancelledError("cancelled-control-secret-canary"),
        ):
            provider = FakeCurrentStateProvider(self.current_state, failure=original)
            composer = self.make_composer(provider=provider)

            signal = self.capture_control_signal(
                type(original),
                lambda selected=composer: selected.authorize(self.context, self.request),
            )

            self.assertIsNot(signal, original)
            self.assertEqual(signal.args, (1,) if type(original) is SystemExit else ())
            rendered = " ".join((str(signal), repr(signal)))
            self.assertNotIn("secret-canary", rendered)
            self.assert_detached_control_traceback(
                signal,
                "authorize",
                "keyboard-control-secret-canary",
                "system-control-secret-canary",
                "generator-control-secret-canary",
                "cancelled-control-secret-canary",
            )
            self.assert_original_control_traceback_is_scrubbed(
                original,
                "keyboard-control-secret-canary",
                "system-control-secret-canary",
                "generator-control-secret-canary",
                "cancelled-control-secret-canary",
            )

    def test_system_exit_status_is_preserved_only_for_bounded_exact_values(self):
        class IntSubclass(int):
            pass

        invalid_object = object()
        cases = (
            ("none", None, None),
            ("false", False, False),
            ("true", True, True),
            ("zero", 0, 0),
            ("ordinary", 17, 17),
            ("maximum", 255, 255),
            ("negative", -1, 1),
            ("above-maximum", 256, 1),
            ("text", "system-exit-status-secret-canary", 1),
            ("object", invalid_object, 1),
            ("int-subclass", IntSubclass(17), 1),
        )
        for label, status, expected_status in cases:
            with self.subTest(status=label):
                original = SystemExit(status)
                provider = FakeCurrentStateProvider(self.current_state, failure=original)
                composer = self.make_composer(provider=provider)
                active_error = RuntimeError(f"system-exit-{label}-body-secret-canary")
                active_error.composer = composer
                active_error.request = self.request
                active_error.provider = provider
                active_error.authorizer = self.authorizer
                active_error.key_ring = self.key_ring

                try:
                    raise active_error
                except RuntimeError:
                    signal = self.capture_control_signal(
                        SystemExit,
                        lambda selected=composer: selected.authorize(
                            self.context,
                            self.request,
                        ),
                    )

                self.assertIsNot(signal, original)
                if expected_status is None:
                    self.assertIsNone(signal.code)
                    self.assertEqual(signal.args, ())
                else:
                    self.assertIs(type(signal.code), type(expected_status))
                    self.assertEqual(signal.code, expected_status)
                    self.assertEqual(signal.args, (expected_status,))
                self.assertNotIn(
                    "system-exit-status-secret-canary",
                    " ".join((str(signal), repr(signal))),
                )
                forbidden_status = (
                    (status,)
                    if not (
                        status is None
                        or type(status) is bool
                        or (type(status) is int and 0 <= status <= 255)
                    )
                    else ()
                )
                self.assert_detached_control_traceback(
                    signal,
                    "authorize",
                    "system-exit-status-secret-canary",
                    forbidden=(
                        composer,
                        provider,
                        self.issuer,
                        self.authorizer,
                        self.verifier,
                        self.key_ring,
                        self.clock,
                        self.context,
                        self.request,
                        original,
                        active_error,
                        *forbidden_status,
                    ),
                )
                self.assert_original_control_traceback_is_scrubbed(
                    original,
                    "system-exit-status-secret-canary",
                )

    def test_lifecycle_boundaries_reissue_clean_exact_control_signals(self):
        paths = (
            "constructor",
            "composer_close",
            "composer_enter",
            "composer_exit",
            "registry_close",
        )
        signal_types = (KeyboardInterrupt, SystemExit, GeneratorExit, asyncio.CancelledError)
        for path in paths:
            for signal_type in signal_types:
                with self.subTest(path=path, signal=signal_type.__name__):
                    canary = f"{path}-{signal_type.__name__}-lifecycle-secret-canary"
                    original = SystemExit(17) if signal_type is SystemExit else signal_type(canary)
                    active_error = RuntimeError(f"{canary}-body")
                    active_error.request = self.request
                    active_error.provider = self.provider
                    active_error.authorizer = self.authorizer
                    active_error.key_ring = self.key_ring
                    composer = None
                    registry = None
                    uninitialized = None
                    exit_error = None
                    exit_trace = None

                    if path == "constructor":
                        uninitialized = object.__new__(ProtectedOperationComposer)

                        def fail_owner(_issuer, *, selected=original):
                            raise selected

                        def constructor_callback(selected=uninitialized):
                            ProtectedOperationComposer.__init__(
                                selected,
                                issuer=self.issuer,
                                state_provider=self.provider,
                                authorizer=self.authorizer,
                                clock=self.clock,
                            )

                        callback = constructor_callback
                        expected_method = "__init__"
                        boundary_patch = patch.object(
                            RequestContextIssuer,
                            "_is_current_process_owner",
                            fail_owner,
                        )
                    else:
                        composer = self.make_composer()
                        registry = object.__getattribute__(
                            composer,
                            "_ProtectedOperationComposer__registry",
                        )
                        if path == "composer_enter":

                            def fail_owner(_issuer, *, selected=original):
                                raise selected

                            callback = composer.__enter__
                            expected_method = "__enter__"
                            boundary_patch = patch.object(
                                RequestContextIssuer,
                                "_is_current_process_owner",
                                fail_owner,
                            )
                        else:

                            def fail_registry_close(_registry, *, selected=original):
                                raise selected

                            boundary_patch = patch.object(
                                _AuthorizedOperationRegistry,
                                "_close",
                                fail_registry_close,
                            )
                            if path == "composer_close":
                                callback = composer.close
                                expected_method = "close"
                            elif path == "composer_exit":
                                exit_error = active_error

                                def exit_callback(
                                    selected_composer=composer,
                                    selected_error=active_error,
                                ):
                                    with selected_composer:
                                        raise selected_error

                                callback = exit_callback
                                expected_method = "__exit__"
                            else:
                                callback = registry.close
                                expected_method = "close"

                    active_error.composer = composer
                    active_error.registry = registry
                    active_error.uninitialized = uninitialized
                    with boundary_patch:
                        if path == "composer_exit":
                            signal = self.capture_control_signal(signal_type, callback)
                        else:
                            try:
                                raise active_error
                            except RuntimeError:
                                signal = self.capture_control_signal(signal_type, callback)
                    if path == "composer_exit":
                        exit_trace = active_error.__traceback__

                    self.assertIsNot(signal, original)
                    self.assertEqual(signal.args, (17,) if signal_type is SystemExit else ())
                    self.assertNotIn(canary, " ".join((str(signal), repr(signal))))
                    forbidden = tuple(
                        value
                        for value in (
                            composer,
                            registry,
                            uninitialized,
                            self.issuer,
                            self.provider,
                            self.authorizer,
                            self.verifier,
                            self.key_ring,
                            self.clock,
                            self.context,
                            self.request,
                            original,
                            active_error,
                            exit_error,
                            exit_trace,
                            b"signing-secret-canary-1234567890",
                        )
                        if value is not None
                    )
                    self.assert_detached_control_traceback(
                        signal,
                        expected_method,
                        canary,
                        forbidden=forbidden,
                    )
                    self.assert_original_control_traceback_is_scrubbed(original, canary)

    def test_exact_control_signals_survive_consume_and_retire_boundaries(self):
        signal_types = (KeyboardInterrupt, SystemExit, GeneratorExit, asyncio.CancelledError)
        for method_name in ("consume", "retire"):
            for signal_type in signal_types:
                with self.subTest(method=method_name, signal=signal_type.__name__):
                    operation = self.composer.authorize(self.context, self.request)
                    canary = f"{method_name}-{signal_type.__name__}-control-secret-canary"
                    original = signal_type(canary)
                    if method_name == "consume":
                        self.provider.failure = original
                        signal = self.capture_control_signal(
                            signal_type,
                            lambda selected=operation: self.composer.consume(
                                selected,
                                self.context,
                                self.request,
                            ),
                        )
                        self.provider.failure = None
                        self.composer.consume(operation, self.context, self.request)
                    else:

                        def hostile_retire(*args, selected=original, **kwargs):
                            raise selected

                        with patch.object(
                            _AuthorizedOperationRegistry,
                            "retire",
                            hostile_retire,
                        ):
                            signal = self.capture_control_signal(
                                signal_type,
                                lambda selected=operation: self.composer.retire(selected),
                            )
                        self.composer.retire(operation)

                    self.assertIsNot(signal, original)
                    self.assertEqual(signal.args, (1,) if signal_type is SystemExit else ())
                    self.assertNotIn(canary, " ".join((str(signal), repr(signal))))
                    self.assert_detached_control_traceback(signal, method_name, canary)
                    self.assert_original_control_traceback_is_scrubbed(original, canary)

    def test_control_signal_subclasses_fail_closed_as_dependency_errors(self):
        class KeyboardInterruptSubclass(KeyboardInterrupt):
            pass

        class SystemExitSubclass(SystemExit):
            pass

        class GeneratorExitSubclass(GeneratorExit):
            pass

        class CancelledErrorSubclass(asyncio.CancelledError):
            pass

        for signal_type in (
            KeyboardInterruptSubclass,
            SystemExitSubclass,
            GeneratorExitSubclass,
            CancelledErrorSubclass,
        ):
            with self.subTest(signal=signal_type.__name__):
                canary = f"{signal_type.__name__}-subclass-secret-canary"
                original = signal_type(canary)
                provider = FakeCurrentStateProvider(self.current_state, failure=original)
                composer = self.make_composer(provider=provider)

                error = self.capture_error(
                    "protected_operation_state_unavailable",
                    lambda selected=composer: selected.authorize(self.context, self.request),
                )

                self.assertIs(type(error), OperationAuthorizationError)
                self.assertNotIn(canary, " ".join((str(error), repr(error))))
                self.assert_detached_traceback(error, canary)
                self.assert_original_control_traceback_is_scrubbed(original, canary)

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

    def receive_process_payload(self, process, connection, *, timeout=5):
        payload = None
        try:
            if connection.poll(timeout):
                try:
                    payload = connection.recv()
                except EOFError:
                    payload = None
        finally:
            connection.close()
            process.join(1)
            if process.is_alive():
                process.terminate()
                process.join(2)
        self.assertIsNotNone(payload, "fork child did not respond before the deadlock bound")
        self.assertEqual(process.exitcode, 0)
        return payload

    @unittest.skipUnless("fork" in multiprocessing.get_all_start_methods(), "fork unavailable")
    def test_real_fork_cannot_duplicate_one_time_consume(self):
        operation = self.composer.authorize(self.context, self.request)
        fork_context = multiprocessing.get_context("fork")
        parent_connection, child_connection = fork_context.Pipe(duplex=False)
        process = fork_context.Process(
            target=_fork_consume_probe,
            args=(
                child_connection,
                self.composer,
                operation,
                self.context,
                self.request,
            ),
        )

        process.start()
        child_connection.close()
        self.composer.consume(operation, self.context, self.request)
        child_result = self.receive_process_payload(process, parent_connection)

        self.assertEqual(
            child_result,
            ("operation_error", "protected_operation_process_mismatch", True, True),
        )
        self.assert_code(
            "protected_operation_untrusted",
            lambda: self.composer.consume(operation, self.context, self.request),
        )

    @unittest.skipUnless("fork" in multiprocessing.get_all_start_methods(), "fork unavailable")
    def test_real_fork_cannot_build_a_fresh_composer_from_an_inherited_issuer(self):
        fork_context = multiprocessing.get_context("fork")
        parent_connection, child_connection = fork_context.Pipe(duplex=False)
        process = fork_context.Process(
            target=_fork_inherited_issuer_composer_probe,
            args=(
                child_connection,
                self.issuer,
                self.provider,
                self.authorizer,
                self.clock,
            ),
        )

        process.start()
        child_connection.close()
        payload = self.receive_process_payload(process, parent_connection)

        self.assertEqual(
            payload,
            ("operation_error", "protected_operation_process_mismatch", True, True),
        )
        replacement = self.make_composer()
        self.assertIsInstance(
            replacement.authorize(self.context, self.request),
            AuthorizedOperation,
        )

    @unittest.skipUnless("fork" in multiprocessing.get_all_start_methods(), "fork unavailable")
    def test_real_fork_rejects_every_inherited_composer_and_registry_path(self):
        operation = self.composer.authorize(self.context, self.request)
        registry = object.__getattribute__(
            self.composer,
            "_ProtectedOperationComposer__registry",
        )
        fork_context = multiprocessing.get_context("fork")
        parent_connection, child_connection = fork_context.Pipe(duplex=False)
        process = fork_context.Process(
            target=_fork_public_path_probe,
            args=(
                child_connection,
                self.composer,
                registry,
                operation,
                object(),
                object(),
                self.context,
                self.request,
            ),
        )

        process.start()
        child_connection.close()
        payload = self.receive_process_payload(process, parent_connection)

        self.assertTrue(payload["identityChanged"])
        self.assertEqual(
            set(payload["results"]),
            {
                "composer_authorize",
                "composer_consume",
                "composer_retire",
                "composer_close",
                "composer_enter",
                "composer_exit",
                "registry_issue",
                "registry_observe_now",
                "registry_verify",
                "registry_check_request",
                "registry_consume_request",
                "registry_retire",
                "registry_close",
            },
        )
        for name, result in payload["results"].items():
            with self.subTest(path=name):
                self.assertEqual(
                    result,
                    (
                        "operation_error",
                        "protected_operation_process_mismatch",
                        True,
                        True,
                    ),
                )
        self.composer.consume(operation, self.context, self.request)

    @unittest.skipUnless("fork" in multiprocessing.get_all_start_methods(), "fork unavailable")
    def test_fork_while_composer_and_registry_locks_are_held_never_waits_on_them(self):
        operation = self.composer.authorize(self.context, self.request)
        registry = object.__getattribute__(
            self.composer,
            "_ProtectedOperationComposer__registry",
        )
        composer_lock = object.__getattribute__(
            self.composer,
            "_ProtectedOperationComposer__lock",
        )
        registry_lock = object.__getattribute__(
            registry,
            "_AuthorizedOperationRegistry__lock",
        )
        locks_held = threading.Event()
        release_locks = threading.Event()

        def hold_inherited_locks():
            with composer_lock:
                with registry_lock:
                    locks_held.set()
                    release_locks.wait(10)

        holder = threading.Thread(target=hold_inherited_locks)
        holder.start()
        self.assertTrue(locks_held.wait(2))
        fork_context = multiprocessing.get_context("fork")
        parent_connection, child_connection = fork_context.Pipe(duplex=False)
        process = fork_context.Process(
            target=_fork_public_path_probe,
            args=(
                child_connection,
                self.composer,
                registry,
                operation,
                object(),
                object(),
                self.context,
                self.request,
            ),
        )

        try:
            process.start()
            child_connection.close()
            payload = self.receive_process_payload(process, parent_connection)
        finally:
            release_locks.set()
            holder.join(2)
        self.assertFalse(holder.is_alive())
        self.assertTrue(payload["identityChanged"])
        self.assertTrue(
            all(
                result
                == (
                    "operation_error",
                    "protected_operation_process_mismatch",
                    True,
                    True,
                )
                for result in payload["results"].values()
            )
        )
        self.composer.consume(operation, self.context, self.request)

    def test_spawn_and_forkserver_refuse_handle_composer_and_registry_transfer(self):
        operation = self.composer.authorize(self.context, self.request)
        registry = object.__getattribute__(
            self.composer,
            "_ProtectedOperationComposer__registry",
        )
        available = set(multiprocessing.get_all_start_methods())
        for start_method in ("spawn", "forkserver"):
            if start_method not in available:
                continue
            process_context = multiprocessing.get_context(start_method)
            for label, value, expected_error in (
                ("handle", operation, "AuthorizedOperation cannot be serialized"),
                (
                    "composer",
                    self.composer,
                    "ProtectedOperationComposer cannot be serialized",
                ),
                (
                    "registry",
                    registry,
                    "AuthorizedOperationRegistry cannot be serialized",
                ),
            ):
                with self.subTest(start_method=start_method, value=label):
                    process = process_context.Process(
                        target=_multiprocessing_transfer_probe,
                        args=(value,),
                    )
                    try:
                        process.start()
                    except TypeError as error:
                        self.assertEqual(str(error), expected_error)
                        continue
                    process.join(2)
                    if process.is_alive():
                        process.terminate()
                        process.join(2)
                    self.fail(f"{start_method} transferred non-serializable {label}")

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
