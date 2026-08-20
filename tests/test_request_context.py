import unittest
from collections import UserDict
from datetime import datetime, timedelta, timezone

from quantum_entanglement.request_context import (
    AuthenticatedRequestBinding,
    CallerRequestContext,
)
from quantum_entanglement.tenancy import TenantId, WorkspaceId

NOW = datetime(2026, 8, 20, 6, 0, 0, tzinfo=timezone.utc)
EVIDENCE = "ab" * 32


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


if __name__ == "__main__":
    unittest.main()
