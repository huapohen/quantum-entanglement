import copy
import gc
import multiprocessing
import os
import pickle
import threading
import unittest
from collections import UserDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import quantum_entanglement
from quantum_entanglement import request_context as request_context_module
from quantum_entanglement.request_context import (
    AuthenticatedRequestBinding,
    CallerRequestContext,
    ReauthorizationBasis,
    RequestContext,
    RequestContextError,
    RequestContextIssuer,
)
from quantum_entanglement.service.secrets import SecretMaterial
from quantum_entanglement.tenancy import AccessRequest, ResourceRef, TenantId, WorkspaceId

NOW = datetime(2026, 8, 20, 6, 0, 0, tzinfo=timezone.utc)
EVIDENCE = "ab" * 32


class FixedClock:
    def __init__(self, current=NOW):
        self.current = current

    def now(self):
        return self.current

    def advance(self, delta):
        self.current += delta


class FakeAuthenticator:
    def __init__(self, make_binding, *, failure=None):
        self.make_binding = make_binding
        self.failure = failure
        self.calls = 0
        self.credential_matches = False
        self.retained_view = None

    def authenticate(self, claims, credential, *, audience, at):
        self.calls += 1
        self.credential_matches = bytes(credential) == b"bounded-credential-canary"
        self.retained_view = credential
        if self.failure is not None:
            raise self.failure
        return self.make_binding(claims=claims, audience=audience, at=at)


def _capture_issuer_process_call(callback):
    try:
        callback()
    except RequestContextError as error:
        return (
            "request_context_error",
            error.code,
            error.__cause__ is None,
            error.__context__ is None,
        )
    except BaseException as error:
        return ("unexpected_error", type(error).__name__)
    return ("unexpected_success",)


def _fork_issuer_public_path_probe(connection, issuer, context, claims, request):
    owner_pid = object.__getattribute__(issuer, "_RequestContextIssuer__owner_pid")
    owner_epoch = object.__getattribute__(issuer, "_RequestContextIssuer__owner_epoch")
    current_pid, current_epoch = request_context_module._current_process_identity()
    credential = SecretMaterial(b"fork-child-credential-canary")
    calls = (
        ("issue", lambda: issuer.issue(claims, credential)),
        ("prepare_reauthorization", lambda: issuer.prepare_reauthorization(context, request)),
        ("retire", lambda: issuer.retire(context)),
        ("close", issuer.close),
        ("enter", issuer.__enter__),
    )
    try:
        connection.send(
            {
                "identityChanged": (
                    current_pid != owner_pid and current_epoch is not owner_epoch
                ),
                "results": {name: _capture_issuer_process_call(call) for name, call in calls},
                "credentialClosed": credential.closed,
            }
        )
    finally:
        connection.close()


class RequestContextValueTests(unittest.TestCase):
    def claims(self):
        return CallerRequestContext(
            request_id="request-1",
            subject_id="subject-1",
            tenant_id=TenantId("tenant-a"),
            workspace_id=WorkspaceId("workspace-a"),
        )

    def binding(self, **changes):
        values = {
            "authenticator_id": "fake-authenticator",
            "audience": "qe-runtime",
            "request_id": "request-1",
            "principal_id": "principal-1",
            "subject_id": "subject-1",
            "tenant_id": TenantId("tenant-a"),
            "workspace_id": WorkspaceId("workspace-a"),
            "identity_revision": "identity-7",
            "scope_revision": "membership-11",
            "evidence_fingerprint": EVIDENCE,
            "authenticated_at": NOW,
            "expires_at": NOW + timedelta(minutes=5),
        }
        values.update(changes)
        return AuthenticatedRequestBinding(**values)

    def test_caller_claims_round_trip_but_remain_an_explicit_untrusted_type(self):
        claims = self.claims()

        self.assertEqual(CallerRequestContext.from_dict(claims.to_dict()), claims)
        self.assertNotIsInstance(claims, AuthenticatedRequestBinding)

    def test_request_context_public_api_is_exported_from_package_root(self):
        for name in request_context_module.__all__:
            with self.subTest(name=name):
                self.assertIn(name, quantum_entanglement.__all__)
                self.assertIs(
                    getattr(quantum_entanglement, name),
                    getattr(request_context_module, name),
                )

    def test_caller_claim_parser_rejects_unknown_missing_and_custom_mapping_fields(self):
        valid = self.claims().to_dict()
        unknown = {**valid, "role": "owner"}
        missing = dict(valid)
        del missing["tenantId"]

        for payload in (unknown, missing):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                CallerRequestContext.from_dict(payload)
        with self.assertRaises(TypeError):
            CallerRequestContext.from_dict(UserDict(valid))

    def test_caller_claim_parser_rejects_type_coercion_and_noncanonical_ids(self):
        valid = self.claims().to_dict()
        invalid = (
            {**valid, "requestId": 7},
            {**valid, "subjectId": " subject-1"},
            {**valid, "tenantId": True},
            {**valid, "workspaceId": 1},
        )

        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises((TypeError, ValueError)):
                CallerRequestContext.from_dict(payload)

    def test_binding_rejects_unsafe_identity_revision_and_evidence_values(self):
        invalid = (
            {"principal_id": "principal\nforged"},
            {"identity_revision": "identity/revision"},
            {"scope_revision": " membership-11"},
            {"evidence_fingerprint": "AB" * 32},
            {"evidence_fingerprint": "ab" * 31},
            {"audience": "qe-runtime\nforged"},
        )

        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.binding(**changes)

    def test_binding_requires_exact_scope_types_and_aware_forward_time(self):
        with self.assertRaises(TypeError):
            self.binding(tenant_id="tenant-a")
        with self.assertRaises(TypeError):
            self.binding(workspace_id="workspace-a")
        with self.assertRaises(ValueError):
            self.binding(authenticated_at=NOW.replace(tzinfo=None))
        with self.assertRaises(ValueError):
            self.binding(expires_at=NOW)

    def test_binding_normalizes_aware_timestamps_to_utc(self):
        offset = timezone(timedelta(hours=8))
        binding = self.binding(
            authenticated_at=NOW.astimezone(offset),
            expires_at=(NOW + timedelta(minutes=5)).astimezone(offset),
        )

        self.assertEqual(binding.authenticated_at, NOW)
        self.assertEqual(binding.expires_at, NOW + timedelta(minutes=5))
        self.assertIs(binding.authenticated_at.tzinfo, timezone.utc)

    def test_identity_value_representations_do_not_render_scope_or_evidence(self):
        claims = self.claims()
        binding = self.binding()
        rendered = " ".join((str(claims), repr(claims), str(binding), repr(binding)))

        for canary in (
            "request-1",
            "subject-1",
            "tenant-a",
            "workspace-a",
            "principal-1",
            EVIDENCE,
        ):
            self.assertNotIn(canary, rendered)


class RequestContextIssuanceTests(unittest.TestCase):
    def setUp(self):
        self.clock = FixedClock()
        self.claims = CallerRequestContext(
            request_id="request-1",
            subject_id="subject-1",
            tenant_id=TenantId("tenant-a"),
            workspace_id=WorkspaceId("workspace-a"),
        )

    def binding(self, *, claims=None, audience="qe-runtime", at=NOW, **changes):
        selected = claims or self.claims
        values = {
            "authenticator_id": "fake-authenticator",
            "audience": audience,
            "request_id": selected.request_id,
            "principal_id": "principal-1",
            "subject_id": selected.subject_id,
            "tenant_id": selected.tenant_id,
            "workspace_id": selected.workspace_id,
            "identity_revision": "identity-7",
            "scope_revision": "membership-11",
            "evidence_fingerprint": EVIDENCE,
            "authenticated_at": at,
            "expires_at": at + timedelta(minutes=5),
        }
        values.update(changes)
        return AuthenticatedRequestBinding(**values)

    def make_issuer(self, make_binding=None, **changes):
        factory = make_binding or self.binding
        authenticator = FakeAuthenticator(factory)
        values = {
            "authenticator": authenticator,
            "authenticator_id": "fake-authenticator",
            "audience": "qe-runtime",
            "clock": self.clock,
        }
        values.update(changes)
        return RequestContextIssuer(**values), authenticator

    @staticmethod
    def credential():
        return SecretMaterial(b"bounded-credential-canary")

    def assert_code(self, code, callback):
        with self.assertRaises(RequestContextError) as raised:
            callback()
        self.assertEqual(raised.exception.code, code)
        self.assertEqual(str(raised.exception), code)
        self.assertIsNone(raised.exception.__cause__)
        self.assertIsNone(raised.exception.__context__)

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

    def assert_detached_traceback(self, error, *canaries):
        frames = []
        traceback = error.__traceback__
        while traceback is not None:
            frames.append(traceback.tb_frame)
            traceback = traceback.tb_next

        library_frames = [
            frame for frame in frames if frame.f_code.co_filename.endswith("/request_context.py")
        ]
        self.assertEqual([frame.f_code.co_name for frame in library_frames], ["issue"])
        for frame in frames:
            for value in list(frame.f_locals.values()):
                self.assertNotIsInstance(value, (SecretMaterial, memoryview))
                if isinstance(value, BaseException):
                    self.assertIs(value, error)
                if isinstance(value, str):
                    for canary in canaries:
                        self.assertNotIn(canary, value)
                if isinstance(value, (bytes, bytearray)):
                    for canary in canaries:
                        self.assertNotIn(canary.encode("utf-8"), value)

    def access_request(
        self,
        *,
        request_id="request-1",
        subject_id="subject-1",
        tenant_id=None,
        resource_tenant=None,
        workspace_id="default",
    ):
        selected_tenant = tenant_id or TenantId("tenant-a")
        selected_resource_tenant = resource_tenant or selected_tenant
        selected_workspace = (
            WorkspaceId("workspace-a") if workspace_id == "default" else workspace_id
        )
        return AccessRequest(
            request_id=request_id,
            subject_id=subject_id,
            tenant_id=selected_tenant,
            action="resource.read",
            resource=ResourceRef(
                tenant_id=selected_resource_tenant,
                workspace_id=selected_workspace,
                resource_type="document",
                resource_id="document-1",
            ),
        )

    def test_issuer_calls_authenticator_and_consumes_credential_lease(self):
        issuer, authenticator = self.make_issuer()
        credential = self.credential()

        context = issuer.issue(self.claims, credential)

        self.assertIsInstance(context, RequestContext)
        self.assertTrue(authenticator.credential_matches)
        self.assertTrue(credential.closed)
        self.assertEqual(
            bytes(authenticator.retained_view),
            bytes(len(b"bounded-credential-canary")),
        )
        self.assertEqual(context.principal_id, "principal-1")
        self.assertEqual(context.subject_id, "subject-1")
        self.assertEqual(context.tenant_id, TenantId("tenant-a"))
        self.assertEqual(context.workspace_id, WorkspaceId("workspace-a"))
        self.assertEqual(context.identity_revision, "identity-7")
        self.assertEqual(context.scope_revision, "membership-11")
        self.assertEqual(context.evidence_fingerprint, EVIDENCE)
        self.assertEqual(context.issued_at, NOW)
        self.assertEqual(context.expires_at, NOW + timedelta(minutes=5))
        self.assertNotIn("tenant-a", repr(context))
        self.assertNotIn("subject-1", str(context))

    def test_explicit_falsy_service_clock_is_not_replaced(self):
        class FalsyClock(FixedClock):
            def __init__(self):
                super().__init__()
                self.calls = 0

            def __bool__(self):
                return False

            def now(self):
                self.calls += 1
                return super().now()

        clock = FalsyClock()
        issuer, _ = self.make_issuer(clock=clock)

        context = issuer.issue(self.claims, self.credential())

        self.assertEqual(clock.calls, 3)
        self.assertEqual(context.issued_at, NOW)

    def test_authentication_completion_resamples_time_and_rejects_stale_result(self):
        def slow_factory(*, claims, audience, at):
            binding = self.binding(claims=claims, audience=audience, at=at)
            self.clock.advance(timedelta(minutes=6))
            return binding

        issuer, _ = self.make_issuer(slow_factory)
        credential = self.credential()

        self.assert_code(
            "request_authentication_expired",
            lambda: issuer.issue(self.claims, credential),
        )
        self.assertTrue(credential.closed)

    def test_authentication_completion_clock_failure_is_redacted_and_releases_capacity(self):
        class SecondReadFailsClock(FixedClock):
            def __init__(self):
                super().__init__()
                self.calls = 0

            def now(self):
                self.calls += 1
                if self.calls == 2:
                    raise RuntimeError("completion clock secret")
                return super().now()

        clock = SecondReadFailsClock()
        issuer, authenticator = self.make_issuer(clock=clock, max_active_contexts=1)
        credential = self.credential()

        self.assert_code(
            "request_context_clock_unavailable",
            lambda: issuer.issue(self.claims, credential),
        )
        self.assertEqual(authenticator.calls, 1)
        self.assertTrue(credential.closed)

        clock.calls = 0
        clock.current = NOW
        with self.assertRaises(RequestContextError):
            issuer.issue(self.claims, self.credential())
        self.assertEqual(authenticator.calls, 2)

    def test_authentication_completion_rejects_clock_regression(self):
        def regressing_factory(*, claims, audience, at):
            binding = self.binding(claims=claims, audience=audience, at=at)
            self.clock.current = at - timedelta(seconds=31)
            return binding

        issuer, _ = self.make_issuer(regressing_factory)

        self.assert_code(
            "request_context_time_regressed",
            lambda: issuer.issue(self.claims, self.credential()),
        )

    def test_credential_is_closed_when_claims_clock_or_authenticator_fails(self):
        class FailingClock:
            def now(self):
                raise RuntimeError("clock credential canary")

        failure_cases = []
        bad_claims = CallerRequestContext.from_dict(self.claims.to_dict())
        object.__setattr__(bad_claims, "subject_id", " subject-1")
        issuer, _ = self.make_issuer()
        failure_cases.append((issuer, bad_claims, (TypeError,)))
        issuer, _ = self.make_issuer(clock=FailingClock())
        failure_cases.append((issuer, self.claims, (RequestContextError,)))
        authenticator = FakeAuthenticator(self.binding, failure=RuntimeError("adapter secret"))
        issuer = RequestContextIssuer(
            authenticator=authenticator,
            authenticator_id="fake-authenticator",
            audience="qe-runtime",
            clock=self.clock,
        )
        failure_cases.append((issuer, self.claims, (RequestContextError,)))

        for selected_issuer, selected_claims, error_types in failure_cases:
            credential = self.credential()
            with self.subTest(error_types=error_types), self.assertRaises(error_types):
                selected_issuer.issue(selected_claims, credential)
            self.assertTrue(credential.closed)

    def test_authenticator_failure_is_redacted_and_does_not_echo_credential(self):
        canary = "bounded-credential-canary"
        authenticator = FakeAuthenticator(
            self.binding,
            failure=RuntimeError(f"provider echoed {canary}"),
        )
        issuer = RequestContextIssuer(
            authenticator=authenticator,
            authenticator_id="fake-authenticator",
            audience="qe-runtime",
            clock=self.clock,
        )

        with self.assertRaises(RequestContextError) as raised:
            issuer.issue(self.claims, self.credential())

        self.assertEqual(raised.exception.code, "request_authentication_failed")
        self.assertNotIn(canary, str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertIsNone(raised.exception.__context__)

    def test_closed_credential_failure_detaches_the_raw_exception_context(self):
        issuer, _ = self.make_issuer()
        credential = self.credential()
        credential.close()

        self.assert_code(
            "request_context_credential_unavailable",
            lambda: issuer.issue(self.claims, credential),
        )

    def test_credential_wipe_failure_is_redacted_and_never_returns_a_context(self):
        canary = "wipe-failure-secret-canary"

        class FailingWipeBuffer(bytearray):
            def __setitem__(self, key, value):
                raise RuntimeError(canary)

        issuer, _ = self.make_issuer()
        credential = self.credential()
        object.__setattr__(
            credential,
            "_SecretMaterial__buffer",
            FailingWipeBuffer(b"bounded-credential-canary"),
        )

        with self.assertRaises(RequestContextError) as raised:
            issuer.issue(self.claims, credential)

        self.assertEqual(raised.exception.code, "request_context_credential_close_failed")
        self.assertNotIn(canary, str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertIsNone(raised.exception.__context__)

    def test_wipe_failure_traceback_does_not_retain_internal_secret_frames(self):
        class FailingWipeBuffer(bytearray):
            def __setitem__(self, key, value):
                raise RuntimeError("traceback-secret-canary")

        issuer, _ = self.make_issuer()
        credential = self.credential()
        object.__setattr__(
            credential,
            "_SecretMaterial__buffer",
            FailingWipeBuffer(b"bounded-credential-canary"),
        )

        captured_error = None
        try:
            issuer.issue(self.claims, credential)
        except RequestContextError as error:
            captured_error = error
        else:
            self.fail("wipe failure unexpectedly returned a context")

        del credential
        self.assertIsNotNone(captured_error)
        self.assert_detached_traceback(
            captured_error,
            "bounded-credential-canary",
            "traceback-secret-canary",
        )

    def test_compound_authentication_and_wipe_failure_detaches_both_exceptions(self):
        class FailingWipeBuffer(bytearray):
            def __setitem__(self, key, value):
                raise RuntimeError("compound-failure-secret-canary")

        authenticator = FakeAuthenticator(
            self.binding,
            failure=RuntimeError("compound-failure-secret-canary"),
        )
        issuer = RequestContextIssuer(
            authenticator=authenticator,
            authenticator_id="fake-authenticator",
            audience="qe-runtime",
            clock=self.clock,
        )
        credential = self.credential()
        object.__setattr__(
            credential,
            "_SecretMaterial__buffer",
            FailingWipeBuffer(b"bounded-credential-canary"),
        )

        captured_error = None
        try:
            issuer.issue(self.claims, credential)
        except RequestContextError as error:
            captured_error = error
        else:
            self.fail("compound failure unexpectedly returned a context")

        del credential
        self.assertIsNotNone(captured_error)
        self.assertEqual(captured_error.code, "request_context_credential_close_failed")
        self.assertNotIn("compound-failure-secret-canary", str(captured_error))
        self.assertIsNone(captured_error.__cause__)
        self.assertIsNone(captured_error.__context__)
        self.assert_detached_traceback(
            captured_error,
            "bounded-credential-canary",
            "compound-failure-secret-canary",
        )

    def test_reflectively_corrupted_binding_failure_is_redacted_and_detached(self):
        canary = "binding-validation-secret-canary"

        class ExplosiveScope:
            def __str__(self):
                raise RuntimeError(canary)

        def factory(*, claims, audience, at):
            binding = self.binding(claims=claims, audience=audience, at=at)
            object.__setattr__(binding, "tenant_id", ExplosiveScope())
            return binding

        issuer, _ = self.make_issuer(factory)

        self.assert_code(
            "request_authentication_result_invalid",
            lambda: issuer.issue(self.claims, self.credential()),
        )

    def test_every_caller_scope_and_configured_binding_mismatch_fails_closed(self):
        mismatches = (
            {"authenticator_id": "other-authenticator"},
            {"audience": "other-service"},
            {"request_id": "request-2"},
            {"subject_id": "subject-2"},
            {"tenant_id": TenantId("tenant-b")},
            {"workspace_id": WorkspaceId("workspace-b")},
        )

        for changes in mismatches:

            def factory(*, claims, audience, at, selected=changes):
                selected_audience = selected.get("audience", audience)
                remaining = {key: value for key, value in selected.items() if key != "audience"}
                return self.binding(
                    claims=claims,
                    audience=selected_audience,
                    at=at,
                    **remaining,
                )

            issuer, _ = self.make_issuer(factory)
            with self.subTest(changes=changes):
                self.assert_code(
                    "request_authentication_binding_mismatch",
                    lambda selected_issuer=issuer: selected_issuer.issue(
                        self.claims, self.credential()
                    ),
                )

    def test_adapter_owned_scope_objects_cannot_change_binding_after_validation(self):
        returned = None

        def factory(*, claims, audience, at):
            nonlocal returned
            returned = self.binding(claims=claims, audience=audience, at=at)
            return returned

        issuer, _ = self.make_issuer(factory)
        register = RequestContextIssuer._register

        def mutate_then_register(selected_issuer, binding, expected):
            object.__setattr__(returned.tenant_id, "value", "tenant-b")
            object.__setattr__(returned.workspace_id, "value", "workspace-b")
            return register(selected_issuer, binding, expected)

        with patch.object(RequestContextIssuer, "_register", mutate_then_register):
            context = issuer.issue(self.claims, self.credential())

        self.assertEqual(context.tenant_id, TenantId("tenant-a"))
        self.assertEqual(context.workspace_id, WorkspaceId("workspace-a"))

    def test_unexpected_result_future_time_expiry_and_excessive_ttl_fail_closed(self):
        cases = (
            (
                lambda **_: object(),
                "request_authentication_result_invalid",
            ),
            (
                lambda *, claims, audience, at: self.binding(
                    claims=claims,
                    audience=audience,
                    at=at + timedelta(seconds=31),
                    expires_at=at + timedelta(minutes=5),
                ),
                "request_authentication_time_invalid",
            ),
            (
                lambda *, claims, audience, at: self.binding(
                    claims=claims,
                    audience=audience,
                    at=at - timedelta(minutes=5),
                    expires_at=at,
                ),
                "request_authentication_expired",
            ),
            (
                lambda *, claims, audience, at: self.binding(
                    claims=claims,
                    audience=audience,
                    at=at,
                    expires_at=at + timedelta(minutes=6),
                ),
                "request_authentication_ttl_exceeded",
            ),
        )

        for factory, code in cases:
            issuer, _ = self.make_issuer(factory)
            with self.subTest(code=code):
                self.assert_code(
                    code,
                    lambda selected_issuer=issuer: selected_issuer.issue(
                        self.claims, self.credential()
                    ),
                )

    def test_context_cannot_be_directly_constructed_copied_or_serialized(self):
        issuer, _ = self.make_issuer()
        context = issuer.issue(self.claims, self.credential())

        with self.assertRaises(TypeError):
            RequestContext(None, None)
        with self.assertRaises(TypeError):
            copy.copy(context)
        with self.assertRaises(TypeError):
            copy.deepcopy(context)
        with self.assertRaises(TypeError):
            pickle.dumps(context)

    def test_issuer_registry_cannot_be_duplicated_or_serialized(self):
        issuer, _ = self.make_issuer()
        issuer.issue(self.claims, self.credential())

        with self.assertRaises(TypeError):
            copy.copy(issuer)
        with self.assertRaises(TypeError):
            copy.deepcopy(issuer)
        with self.assertRaises(TypeError):
            pickle.dumps(issuer)

    def test_pid_drift_lazily_refreshes_epoch_without_at_fork_hook(self):
        stale_epoch = object()
        with patch.object(request_context_module, "_PROCESS_PID", os.getpid() + 1):
            with patch.object(request_context_module, "_PROCESS_EPOCH", stale_epoch):
                process_pid, process_epoch = request_context_module._current_process_identity()

        self.assertEqual(process_pid, os.getpid())
        self.assertIsNot(process_epoch, stale_epoch)

    @unittest.skipUnless("fork" in multiprocessing.get_all_start_methods(), "fork unavailable")
    def test_real_fork_rejects_every_inherited_issuer_path_and_parent_remains_usable(self):
        issuer, _ = self.make_issuer()
        context = issuer.issue(self.claims, self.credential())
        request = self.access_request()
        fork_context = multiprocessing.get_context("fork")
        parent_connection, child_connection = fork_context.Pipe(duplex=False)
        process = fork_context.Process(
            target=_fork_issuer_public_path_probe,
            args=(child_connection, issuer, context, self.claims, request),
        )

        process.start()
        child_connection.close()
        payload = self.receive_process_payload(process, parent_connection)

        self.assertTrue(payload["identityChanged"])
        self.assertTrue(payload["credentialClosed"])
        self.assertEqual(
            set(payload["results"]),
            {"issue", "prepare_reauthorization", "retire", "close", "enter"},
        )
        for name, result in payload["results"].items():
            with self.subTest(path=name):
                self.assertEqual(
                    result,
                    (
                        "request_context_error",
                        "request_context_process_mismatch",
                        True,
                        True,
                    ),
                )

        basis = issuer.prepare_reauthorization(context, request)
        self.assertEqual(basis.context_id, context.context_id)
        replacement = issuer.issue(self.claims, self.credential())
        self.assertIsInstance(replacement, RequestContext)

    @unittest.skipUnless("fork" in multiprocessing.get_all_start_methods(), "fork unavailable")
    def test_fork_while_issuer_lock_is_held_never_waits_on_the_inherited_lock(self):
        issuer, _ = self.make_issuer()
        context = issuer.issue(self.claims, self.credential())
        request = self.access_request()
        issuer_lock = object.__getattribute__(issuer, "_RequestContextIssuer__lock")
        lock_held = threading.Event()
        release_lock = threading.Event()

        def hold_inherited_lock():
            with issuer_lock:
                lock_held.set()
                release_lock.wait(10)

        holder = threading.Thread(target=hold_inherited_lock)
        holder.start()
        self.assertTrue(lock_held.wait(2))
        fork_context = multiprocessing.get_context("fork")
        parent_connection, child_connection = fork_context.Pipe(duplex=False)
        process = fork_context.Process(
            target=_fork_issuer_public_path_probe,
            args=(child_connection, issuer, context, self.claims, request),
        )

        try:
            process.start()
            child_connection.close()
            payload = self.receive_process_payload(process, parent_connection)
        finally:
            release_lock.set()
            holder.join(2)
        self.assertFalse(holder.is_alive())
        self.assertTrue(payload["identityChanged"])
        self.assertTrue(
            all(
                result
                == (
                    "request_context_error",
                    "request_context_process_mismatch",
                    True,
                    True,
                )
                for result in payload["results"].values()
            )
        )
        self.assertEqual(
            issuer.prepare_reauthorization(context, request).context_id,
            context.context_id,
        )

    def test_active_context_capacity_is_bounded_and_dead_handles_are_pruned(self):
        issuer, authenticator = self.make_issuer(max_active_contexts=1)
        first = issuer.issue(self.claims, self.credential())

        self.assert_code(
            "request_context_capacity_exceeded",
            lambda: issuer.issue(self.claims, self.credential()),
        )
        self.assertEqual(authenticator.calls, 1)
        del first
        gc.collect()
        replacement = issuer.issue(self.claims, self.credential())
        self.assertIsInstance(replacement, RequestContext)

    def test_pending_authentication_reserves_capacity_before_adapter_call(self):
        entered = threading.Event()
        release = threading.Event()

        class BlockingAuthenticator(FakeAuthenticator):
            def authenticate(self, claims, credential, *, audience, at):
                self.calls += 1
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test authentication timeout")
                return self.make_binding(claims=claims, audience=audience, at=at)

        authenticator = BlockingAuthenticator(self.binding)
        issuer = RequestContextIssuer(
            authenticator=authenticator,
            authenticator_id="fake-authenticator",
            audience="qe-runtime",
            clock=self.clock,
            max_active_contexts=1,
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(issuer.issue, self.claims, self.credential())
            self.assertTrue(entered.wait(2))
            second = self.credential()
            try:
                self.assert_code(
                    "request_context_capacity_exceeded",
                    lambda: issuer.issue(self.claims, second),
                )
                self.assertTrue(second.closed)
                self.assertEqual(authenticator.calls, 1)
            finally:
                release.set()
            self.assertIsInstance(pending.result(timeout=2), RequestContext)

    def test_close_invalidates_issuer_and_still_consumes_new_credentials(self):
        issuer, _ = self.make_issuer()
        issuer.close()
        issuer.close()
        credential = self.credential()

        self.assert_code(
            "request_context_issuer_closed",
            lambda: issuer.issue(self.claims, credential),
        )
        self.assertTrue(credential.closed)

    def test_same_issuer_prepares_complete_non_authorizing_reauthentication_basis(self):
        issuer, _ = self.make_issuer()
        context = issuer.issue(self.claims, self.credential())

        basis = issuer.prepare_reauthorization(context, self.access_request())

        self.assertIsInstance(basis, ReauthorizationBasis)
        self.assertEqual(basis.context_id, context.context_id)
        self.assertEqual(basis.authenticator_id, "fake-authenticator")
        self.assertEqual(basis.audience, "qe-runtime")
        self.assertEqual(basis.principal_id, "principal-1")
        self.assertEqual(basis.subject_id, "subject-1")
        self.assertEqual(basis.tenant_id, TenantId("tenant-a"))
        self.assertEqual(basis.workspace_id, WorkspaceId("workspace-a"))
        self.assertEqual(basis.identity_revision, "identity-7")
        self.assertEqual(basis.scope_revision, "membership-11")
        self.assertEqual(basis.evidence_fingerprint, EVIDENCE)
        self.assertEqual(basis.authenticated_at, NOW)
        self.assertEqual(basis.context_issued_at, NOW)
        self.assertEqual(basis.context_expires_at, NOW + timedelta(minutes=5))
        self.assertEqual(basis.prepared_at, NOW)
        self.assertIn("non-authorizing", repr(basis))
        self.assertNotIn("tenant-a", repr(basis))

    def test_context_cannot_cross_request_subject_tenant_or_workspace_scope(self):
        issuer, _ = self.make_issuer()
        context = issuer.issue(self.claims, self.credential())
        mismatches = (
            self.access_request(request_id="request-2"),
            self.access_request(subject_id="subject-2"),
            self.access_request(tenant_id=TenantId("tenant-b")),
            self.access_request(resource_tenant=TenantId("tenant-b")),
            self.access_request(workspace_id=WorkspaceId("workspace-b")),
            self.access_request(workspace_id=None),
        )

        for request in mismatches:
            with self.subTest(request=request.to_dict()):
                self.assert_code(
                    "request_context_scope_mismatch",
                    lambda selected=request: issuer.prepare_reauthorization(context, selected),
                )

    def test_tenant_wide_context_is_not_a_workspace_wildcard(self):
        tenant_claims = CallerRequestContext(
            request_id="request-1",
            subject_id="subject-1",
            tenant_id=TenantId("tenant-a"),
            workspace_id=None,
        )
        issuer, _ = self.make_issuer()
        context = issuer.issue(tenant_claims, self.credential())

        basis = issuer.prepare_reauthorization(
            context,
            self.access_request(workspace_id=None),
        )
        self.assertIsNone(basis.workspace_id)
        self.assert_code(
            "request_context_scope_mismatch",
            lambda: issuer.prepare_reauthorization(context, self.access_request()),
        )

    def test_context_from_another_issuer_and_uninitialized_forgery_are_rejected(self):
        issuer, _ = self.make_issuer()
        other_issuer, _ = self.make_issuer()
        context = issuer.issue(self.claims, self.credential())
        forged = object.__new__(RequestContext)

        self.assert_code(
            "request_context_untrusted",
            lambda: other_issuer.prepare_reauthorization(context, self.access_request()),
        )
        self.assert_code(
            "request_context_untrusted",
            lambda: issuer.prepare_reauthorization(forged, self.access_request()),
        )

    def test_reflective_context_mutation_is_detected_and_quarantined(self):
        issuer, _ = self.make_issuer()
        context = issuer.issue(self.claims, self.credential())
        object.__setattr__(context, "_RequestContext__subject_id", "subject-2")

        self.assert_code(
            "request_context_tampered",
            lambda: issuer.prepare_reauthorization(context, self.access_request()),
        )
        self.assert_code(
            "request_context_untrusted",
            lambda: issuer.prepare_reauthorization(context, self.access_request()),
        )

    def test_context_expiry_and_service_clock_regression_fail_closed(self):
        issuer, _ = self.make_issuer()
        expired = issuer.issue(self.claims, self.credential())
        self.clock.advance(timedelta(minutes=5))
        self.assert_code(
            "request_context_expired",
            lambda: issuer.prepare_reauthorization(expired, self.access_request()),
        )

        self.clock.current = NOW
        credential = self.credential()
        self.assert_code(
            "request_context_time_regressed",
            lambda: issuer.issue(self.claims, credential),
        )
        self.assertTrue(credential.closed)

    def test_clock_high_water_prevents_context_expiry_revival(self):
        issuer, _ = self.make_issuer()
        context = issuer.issue(self.claims, self.credential())
        self.clock.current = NOW + timedelta(minutes=4, seconds=59)
        near_expiry = issuer.prepare_reauthorization(context, self.access_request())
        self.assertEqual(near_expiry.prepared_at, self.clock.current)

        self.clock.current = NOW
        self.assert_code(
            "request_context_time_regressed",
            lambda: issuer.prepare_reauthorization(context, self.access_request()),
        )

        self.clock.current = NOW + timedelta(minutes=5)
        self.assert_code(
            "request_context_expired",
            lambda: issuer.prepare_reauthorization(context, self.access_request()),
        )
        self.clock.current -= timedelta(seconds=1)
        self.assert_code(
            "request_context_untrusted",
            lambda: issuer.prepare_reauthorization(context, self.access_request()),
        )

    def test_in_skew_rollback_freezes_the_logical_snapshot_time(self):
        issuer, _ = self.make_issuer()
        context = issuer.issue(self.claims, self.credential())
        self.clock.current = NOW + timedelta(seconds=20)
        forward = issuer.prepare_reauthorization(context, self.access_request())

        self.clock.current = NOW - timedelta(seconds=10)
        frozen = issuer.prepare_reauthorization(context, self.access_request())

        self.assertEqual(forward.prepared_at, NOW + timedelta(seconds=20))
        self.assertEqual(frozen.prepared_at, forward.prepared_at)

    def test_completion_high_water_is_the_issued_snapshot_time(self):
        class SequenceClock:
            def __init__(self):
                self.values = iter(
                    (
                        NOW,
                        NOW + timedelta(seconds=20),
                        NOW - timedelta(seconds=10),
                    )
                )

            def now(self):
                return next(self.values)

        issuer, _ = self.make_issuer(clock=SequenceClock())

        context = issuer.issue(self.claims, self.credential())

        self.assertEqual(context.authenticated_at, NOW)
        self.assertEqual(context.issued_at, NOW + timedelta(seconds=20))

    def test_utc_upper_bound_validation_does_not_overflow(self):
        upper = datetime.max.replace(tzinfo=timezone.utc)
        clock = FixedClock(upper - timedelta(microseconds=2))

        def upper_binding(*, claims, audience, at):
            return self.binding(
                claims=claims,
                audience=audience,
                at=upper - timedelta(minutes=5),
            )

        issuer, _ = self.make_issuer(
            upper_binding,
            clock=clock,
        )
        context = issuer.issue(self.claims, self.credential())
        self.assertEqual(context.expires_at, upper)

        issuer.prepare_reauthorization(context, self.access_request())
        clock.current = upper
        self.assert_code(
            "request_context_expired",
            lambda: issuer.prepare_reauthorization(context, self.access_request()),
        )

    def test_clock_failure_does_not_lower_or_corrupt_the_high_water(self):
        class SwitchableClock(FixedClock):
            def __init__(self):
                super().__init__()
                self.failure = False

            def now(self):
                if self.failure:
                    raise RuntimeError("clock-state-secret-canary")
                return super().now()

        clock = SwitchableClock()
        issuer, _ = self.make_issuer(clock=clock)
        context = issuer.issue(self.claims, self.credential())
        clock.current = NOW + timedelta(seconds=20)
        forward = issuer.prepare_reauthorization(context, self.access_request())

        clock.failure = True
        self.assert_code(
            "request_context_clock_unavailable",
            lambda: issuer.prepare_reauthorization(context, self.access_request()),
        )
        clock.failure = False
        clock.current = NOW
        frozen = issuer.prepare_reauthorization(context, self.access_request())
        self.assertEqual(frozen.prepared_at, forward.prepared_at)

    def test_same_issuer_serializes_concurrent_clock_samples(self):
        entered = threading.Event()
        release = threading.Event()

        class OverlapDetectingClock:
            def __init__(self):
                self.guard = threading.Lock()
                self.calls = 0
                self.active = 0
                self.overlapped = False

            def now(self):
                with self.guard:
                    self.calls += 1
                    selected_call = self.calls
                    self.active += 1
                    self.overlapped = self.overlapped or self.active > 1
                try:
                    if selected_call == 1:
                        entered.set()
                        if not release.wait(5):
                            raise RuntimeError("test clock timeout")
                    return NOW
                finally:
                    with self.guard:
                        self.active -= 1

        clock = OverlapDetectingClock()
        issuer, _ = self.make_issuer(clock=clock, max_active_contexts=2)
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(issuer.issue, self.claims, self.credential())
            self.assertTrue(entered.wait(2))
            second = executor.submit(issuer.issue, self.claims, self.credential())
            release.set()
            contexts = (first.result(timeout=2), second.result(timeout=2))

        self.assertTrue(all(isinstance(context, RequestContext) for context in contexts))
        self.assertFalse(clock.overlapped)

    def test_close_is_serialized_with_completion_clock_and_fences_registration(self):
        entered = threading.Event()
        release = threading.Event()

        class BlockingCompletionClock(FixedClock):
            def __init__(self):
                super().__init__()
                self.calls = 0

            def now(self):
                self.calls += 1
                if self.calls == 2:
                    entered.set()
                    if not release.wait(5):
                        raise RuntimeError("test completion clock timeout")
                return super().now()

        clock = BlockingCompletionClock()
        issuer, _ = self.make_issuer(clock=clock)
        credential = self.credential()
        context = None
        with ThreadPoolExecutor(max_workers=2) as executor:
            pending = executor.submit(issuer.issue, self.claims, credential)
            self.assertTrue(entered.wait(2))
            closing = executor.submit(issuer.close)
            self.assertFalse(closing.done())
            release.set()
            try:
                context = pending.result(timeout=2)
            except RequestContextError as error:
                self.assertEqual(error.code, "request_context_issuer_closed")
                self.assertIsNone(error.__context__)
            closing.result(timeout=2)

        self.assertTrue(credential.closed)
        if context is not None:
            self.assert_code(
                "request_context_issuer_closed",
                lambda: issuer.prepare_reauthorization(context, self.access_request()),
            )

    def test_retire_is_serialized_after_an_in_flight_snapshot(self):
        issuer, _ = self.make_issuer()
        context = issuer.issue(self.claims, self.credential())
        entered = threading.Event()
        release = threading.Event()
        trusted_snapshot = RequestContextIssuer._trusted_snapshot

        def blocking_snapshot(selected_issuer, selected_context):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test snapshot timeout")
            return trusted_snapshot(selected_issuer, selected_context)

        with patch.object(RequestContextIssuer, "_trusted_snapshot", blocking_snapshot):
            with ThreadPoolExecutor(max_workers=2) as executor:
                preparing = executor.submit(
                    issuer.prepare_reauthorization,
                    context,
                    self.access_request(),
                )
                self.assertTrue(entered.wait(2))
                retiring = executor.submit(issuer.retire, context)
                self.assertFalse(retiring.done())
                release.set()
                basis = preparing.result(timeout=2)
                retiring.result(timeout=2)

        self.assertEqual(basis.context_id, context.context_id)
        self.assert_code(
            "request_context_untrusted",
            lambda: issuer.prepare_reauthorization(context, self.access_request()),
        )

    def test_retired_context_cannot_be_reused(self):
        issuer, _ = self.make_issuer()
        context = issuer.issue(self.claims, self.credential())

        issuer.retire(context)

        self.assert_code(
            "request_context_untrusted",
            lambda: issuer.prepare_reauthorization(context, self.access_request()),
        )


if __name__ == "__main__":
    unittest.main()
