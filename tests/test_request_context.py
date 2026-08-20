import copy
import gc
import pickle
import unittest
from collections import UserDict
from datetime import datetime, timedelta, timezone

from quantum_entanglement.request_context import (
    AuthenticatedRequestBinding,
    CallerRequestContext,
    RequestContext,
    RequestContextError,
    RequestContextIssuer,
)
from quantum_entanglement.service.secrets import SecretMaterial
from quantum_entanglement.tenancy import TenantId, WorkspaceId

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
        self.credential_matches = False
        self.retained_view = None

    def authenticate(self, claims, credential, *, audience, at):
        self.credential_matches = bytes(credential) == b"bounded-credential-canary"
        self.retained_view = credential
        if self.failure is not None:
            raise self.failure
        return self.make_binding(claims=claims, audience=audience, at=at)


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

    def test_active_context_capacity_is_bounded_and_dead_handles_are_pruned(self):
        issuer, _ = self.make_issuer(max_active_contexts=1)
        first = issuer.issue(self.claims, self.credential())

        self.assert_code(
            "request_context_capacity_exceeded",
            lambda: issuer.issue(self.claims, self.credential()),
        )
        del first
        gc.collect()
        replacement = issuer.issue(self.claims, self.credential())
        self.assertIsInstance(replacement, RequestContext)

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


if __name__ == "__main__":
    unittest.main()
