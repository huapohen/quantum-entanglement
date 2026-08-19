import copy
import pickle
import sqlite3
import tempfile
import threading
import unittest
from collections import UserDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from quantum_entanglement.tenancy import (
    CAPABILITY_PROTOCOL_VERSION,
    AccessRequest,
    AuthorizationDecision,
    AuthorizationOutcome,
    CapabilityClaims,
    CapabilityEnvelope,
    CapabilityNonce,
    CapabilityProof,
    CapabilitySigningKey,
    CapabilityVerificationError,
    CapabilityVerifier,
    DecisionCode,
    DelegationError,
    InMemoryRevocationRevisionGuard,
    KeyStatus,
    KeyUsage,
    Member,
    MemberStatus,
    ResourceRef,
    ResourceScope,
    RevocationGuardIntegrityError,
    RevocationId,
    RevocationSnapshot,
    Role,
    RoleBinding,
    RotatingHMACKeyRing,
    SQLiteRevocationRevisionGuard,
    TenantAuthorizer,
    TenantId,
    VerifiedCapability,
    WorkspaceId,
    validate_delegation,
)

NOW = datetime(2026, 8, 20, 1, 2, 3, tzinfo=timezone.utc)
AUDIENCE = "qe-runtime"
NONCE_A = "01" * 16
NONCE_B = "02" * 16
NONCE_C = "03" * 16


class FixedClock:
    def __init__(self, current=NOW):
        self.current = current

    def now(self):
        return self.current

    def advance(self, delta):
        self.current += delta


class TenancyTests(unittest.TestCase):
    def setUp(self):
        self.clock = FixedClock()
        self.tenant = TenantId("tenant-a")
        self.other_tenant = TenantId("tenant-b")
        self.workspace = WorkspaceId("workspace-1")
        self.other_workspace = WorkspaceId("workspace-2")
        self.key_ring = self.make_key_ring()
        self.verifier = self.make_verifier(self.key_ring)
        self.revision_guard = InMemoryRevocationRevisionGuard()
        self.authorizer = self.make_authorizer()

    def signing_key(
        self,
        principal,
        *,
        kid=None,
        secret_byte=1,
        status=KeyStatus.ACTIVE,
        usages=None,
        root_tenants=None,
    ):
        selected_usages = (
            frozenset((KeyUsage.ROOT,))
            if usages is None and principal in {"issuer-1", "issuer-2", "owner-1"}
            else frozenset((KeyUsage.DELEGATION,))
            if usages is None
            else frozenset(usages)
        )
        selected_root_tenants = (
            frozenset((self.tenant,))
            if root_tenants is None and KeyUsage.ROOT in selected_usages
            else frozenset()
            if root_tenants is None
            else frozenset(root_tenants)
        )
        return CapabilitySigningKey(
            kid=kid or f"{principal}-key",
            principal_id=principal,
            secret=bytes((secret_byte,)) * 32,
            not_before=NOW - timedelta(days=1),
            expires_at=NOW + timedelta(days=1),
            status=status,
            usages=selected_usages,
            root_tenants=selected_root_tenants,
        )

    def make_key_ring(self, *, domain="production", policy="policy-v1", secret_offset=0):
        principals = ("issuer-1", "issuer-2", "owner-1", "agent-1", "agent-2")
        return RotatingHMACKeyRing(
            trust_domain=domain,
            policy_version=policy,
            keys=tuple(
                self.signing_key(principal, secret_byte=index + 1 + secret_offset)
                for index, principal in enumerate(principals)
            ),
        )

    def make_verifier(self, key_ring, *, max_ttl=timedelta(hours=4)):
        return CapabilityVerifier(
            proof_verifier=key_ring,
            trust_domain=key_ring.trust_domain,
            policy_version=key_ring.policy_version,
            audience=AUDIENCE,
            clock=self.clock,
            max_ttl=max_ttl,
        )

    def make_authorizer(self, *, verifier=None, revision_guard=None):
        selected_verifier = verifier or self.verifier
        return TenantAuthorizer(
            capability_verifier=selected_verifier,
            trust_domain=selected_verifier.trust_domain,
            policy_version=selected_verifier.policy_version,
            revision_guard=revision_guard or self.revision_guard,
            audience=AUDIENCE,
            clock=self.clock,
        )

    def resource(
        self,
        *,
        tenant=None,
        workspace="default",
        resource_type="document",
        resource_id="doc-1",
    ):
        selected_workspace = self.workspace if workspace == "default" else workspace
        return ResourceRef(
            tenant_id=tenant or self.tenant,
            workspace_id=selected_workspace,
            resource_type=resource_type,
            resource_id=resource_id,
        )

    def scope(
        self,
        *,
        tenant=None,
        workspace="default",
        resource_type="document",
        resource_id="*",
    ):
        selected_workspace = self.workspace if workspace == "default" else workspace
        return ResourceScope(
            tenant_id=tenant or self.tenant,
            workspace_id=selected_workspace,
            resource_type=resource_type,
            resource_id=resource_id,
        )

    def request(self, *, action="resource.read", resource=None, tenant=None, subject="member-1"):
        return AccessRequest(
            request_id="request-1",
            subject_id=subject,
            tenant_id=tenant or self.tenant,
            action=action,
            resource=resource or self.resource(),
        )

    def member(self, *bindings, status=MemberStatus.ACTIVE, tenant=None, member_id="member-1"):
        return Member(
            member_id=member_id,
            tenant_id=tenant or self.tenant,
            role_bindings=bindings,
            status=status,
        )

    def binding(self, role=Role.VIEWER, **scope_values):
        return RoleBinding(role, self.scope(**scope_values))

    def claims(
        self,
        *,
        issuer="issuer-1",
        subject="member-1",
        action="resource.read",
        resource=None,
        issued_at=None,
        not_before=None,
        expires_at=None,
        audience=AUDIENCE,
        nonce=NONCE_A,
        parent_fingerprint=None,
    ):
        issued = issued_at or NOW - timedelta(minutes=1)
        return CapabilityClaims(
            issuer_id=issuer,
            subject_id=subject,
            action=action,
            resource=resource or self.scope(),
            issued_at=issued,
            not_before=not_before or issued,
            expires_at=expires_at or NOW + timedelta(hours=1),
            audience=audience,
            nonce=CapabilityNonce(nonce),
            parent_fingerprint=parent_fingerprint,
        )

    def envelope(
        self,
        chain,
        *,
        key_ring=None,
        protocol_version=CAPABILITY_PROTOCOL_VERSION,
    ):
        signer = key_ring or self.key_ring
        root_kid = f"{chain[0].issuer_id}-key"
        edge_kids = tuple(f"{child.issuer_id}-key" for child in chain[1:])
        return CapabilityEnvelope.signed(
            chain,
            signer=signer,
            root_kid=root_kid,
            delegation_kids=edge_kids,
            clock=self.clock,
            protocol_version=protocol_version,
        )

    def verify(self, chain, *, max_ttl=timedelta(hours=4), key_ring=None, verifier=None):
        signer = key_ring or self.key_ring
        envelope = self.envelope(chain, key_ring=signer)
        selected_verifier = verifier or self.make_verifier(signer, max_ttl=max_ttl)
        return selected_verifier.verify(envelope)

    def snapshot(self, *revoked, tenant=None, captured_at=None, revision=1):
        return RevocationSnapshot(
            tenant_id=tenant or self.tenant,
            revision=revision,
            captured_at=captured_at or self.clock.now(),
            revoked_ids=frozenset(revoked),
        )

    def test_request_has_no_client_time_and_wildcards_are_rejected(self):
        payload = self.request().to_dict()
        payload["at"] = "1900-01-01T00:00:00Z"
        with self.assertRaises(ValueError):
            AccessRequest.from_dict(payload)
        for action in ("*", "resource.*", "resource.document.*"):
            with self.subTest(action=action), self.assertRaises(ValueError):
                self.request(action=action)

    def test_authorization_expiry_uses_service_clock(self):
        claims = self.claims(expires_at=NOW + timedelta(minutes=1))
        verified = self.verify((claims,))
        member = self.member()

        before = self.authorizer.evaluate(self.request(), member, self.snapshot(), (verified,))
        self.clock.advance(timedelta(minutes=1))
        after = self.authorizer.evaluate(self.request(), member, self.snapshot(), (verified,))

        self.assertEqual(before.code, DecisionCode.ALLOW_CAPABILITY)
        self.assertEqual(after.code, DecisionCode.CAPABILITY_EXPIRED)
        self.assertEqual(after.evaluated_at, self.clock.now())

    def test_claims_cannot_cross_verified_capability_boundary(self):
        claims = self.claims()
        with self.assertRaises(TypeError):
            self.authorizer.evaluate(self.request(), self.member(), self.snapshot(), (claims,))

        envelope = self.envelope((claims,))
        fabricated_stamp = VerifiedCapability(
            envelope=envelope,
            trust_domain="untrusted-stamp",
            policy_version="made-up",
            verified_at=NOW,
        )
        decision = self.authorizer.evaluate(
            self.request(), self.member(), self.snapshot(), (fabricated_stamp,)
        )
        self.assertEqual(decision.code, DecisionCode.ALLOW_CAPABILITY)

    def test_verifier_requires_trusted_root_and_delegation_proof(self):
        root = self.claims(subject="agent-1", action="resource.*")
        child = root.delegate(
            subject_id="member-1",
            action="resource.read",
            resource=self.scope(resource_id="doc-1"),
            issued_at=NOW,
            not_before=NOW,
            expires_at=NOW + timedelta(minutes=30),
            nonce=CapabilityNonce(NONCE_B),
        )
        envelope = self.envelope((root, child))
        root_tampered = envelope.to_dict()
        root_tampered["rootProof"]["signature"] = "A" * 43
        edge_tampered = envelope.to_dict()
        edge_tampered["delegationProofs"][0]["signature"] = "A" * 43

        for payload in (root_tampered, edge_tampered):
            with self.subTest(payload=payload), self.assertRaises(CapabilityVerificationError):
                self.verifier.verify(CapabilityEnvelope.from_dict(payload))

    def test_root_authority_is_key_usage_and_tenant_scoped(self):
        foreign_root = self.claims(
            resource=self.scope(tenant=self.other_tenant),
        )
        with self.assertRaisesRegex(
            CapabilityVerificationError, "not authorized for this proof usage"
        ):
            self.envelope((foreign_root,))

        delegation_only_root = self.claims(issuer="agent-1")
        with self.assertRaisesRegex(
            CapabilityVerificationError, "not authorized for this proof usage"
        ):
            self.envelope((delegation_only_root,))

        root_only_delegate = self.claims(subject="issuer-2", action="resource.*")
        child = root_only_delegate.delegate(
            subject_id="member-1",
            action="resource.read",
            resource=self.scope(resource_id="doc-1"),
            issued_at=NOW,
            not_before=NOW,
            expires_at=NOW + timedelta(minutes=30),
            nonce=CapabilityNonce(NONCE_B),
        )
        with self.assertRaisesRegex(
            CapabilityVerificationError, "not authorized for this proof usage"
        ):
            self.envelope((root_only_delegate, child))

        valid_envelope = self.envelope((self.claims(),))

        class DenyRootAuthority:
            trust_domain = "production"
            policy_version = "policy-v1"

            @staticmethod
            def verify(_proof, _signer_id, _payload, _at):
                return True

            @staticmethod
            def authorize_root(_proof, _claims, _at):
                return False

            @staticmethod
            def authorize_delegation(_proof, _parent, _child, _at):
                return True

        verifier = CapabilityVerifier(
            proof_verifier=DenyRootAuthority(),
            trust_domain="production",
            policy_version="policy-v1",
            audience=AUDIENCE,
            clock=self.clock,
            max_ttl=timedelta(hours=4),
        )
        with self.assertRaisesRegex(CapabilityVerificationError, "root signer is not authorized"):
            verifier.verify(valid_envelope)

    def test_authorizer_rejects_arbitrary_verifier_and_trust_domain_outputs(self):
        claims = self.claims()
        attacker_ring = self.make_key_ring(domain="attacker", secret_offset=20)
        attacker_verifier = self.make_verifier(attacker_ring)
        attacker_verified = self.verify(
            (claims,), key_ring=attacker_ring, verifier=attacker_verifier
        )

        same_domain_wrong_keys = self.make_key_ring(secret_offset=40)
        arbitrary_verifier = self.make_verifier(same_domain_wrong_keys)
        arbitrary_verified = self.verify(
            (claims,),
            key_ring=same_domain_wrong_keys,
            verifier=arbitrary_verifier,
        )

        old_policy_ring = self.make_key_ring(policy="policy-v0", secret_offset=60)
        old_policy_verifier = self.make_verifier(old_policy_ring)
        old_policy_verified = self.verify(
            (claims,), key_ring=old_policy_ring, verifier=old_policy_verifier
        )

        for verified in (attacker_verified, arbitrary_verified, old_policy_verified):
            with self.subTest(verified=verified):
                decision = self.authorizer.evaluate(
                    self.request(), self.member(), self.snapshot(), (verified,)
                )
                self.assertEqual(decision.code, DecisionCode.CAPABILITY_INVALID)

    def test_envelope_protocol_kid_algorithm_and_each_edge_proof_are_enforced(self):
        root = self.claims(subject="agent-1", action="resource.*")
        child = root.delegate(
            subject_id="member-1",
            action="resource.read",
            resource=self.scope(resource_id="doc-1"),
            issued_at=NOW,
            not_before=NOW,
            expires_at=NOW + timedelta(minutes=30),
            nonce=CapabilityNonce(NONCE_B),
        )
        envelope = self.envelope((root, child))

        mutations = (
            ("unknown kid", lambda value: value["rootProof"].update(kid="missing-key")),
            ("wrong alg", lambda value: value["rootProof"].update(alg="HS512")),
            (
                "wrong signature",
                lambda value: value["rootProof"].update(signature="A" * 43),
            ),
            (
                "unsupported protocol",
                lambda value: value.update(protocolVersion="qe-capability/2"),
            ),
        )
        for name, mutate in mutations:
            payload = envelope.to_dict()
            mutate(payload)
            fabricated = VerifiedCapability(
                envelope=CapabilityEnvelope.from_dict(payload),
                trust_domain="production",
                policy_version="policy-v1",
                verified_at=NOW,
            )
            with self.subTest(name=name):
                decision = self.authorizer.evaluate(
                    self.request(), self.member(), self.snapshot(), (fabricated,)
                )
                self.assertEqual(decision.code, DecisionCode.CAPABILITY_INVALID)

        missing_edge = envelope.to_dict()
        missing_edge["delegationProofs"] = []
        with self.assertRaises(ValueError):
            CapabilityEnvelope.from_dict(missing_edge)
        unknown_field = envelope.to_dict()
        unknown_field["proof"] = "ambiguous"
        with self.assertRaises(ValueError):
            CapabilityEnvelope.from_dict(unknown_field)
        with self.assertRaises(ValueError):
            CapabilityProof("issuer-1-key", "none", "A" * 43)

    def test_key_rotation_supports_verify_only_and_revocation(self):
        old_active = self.signing_key(
            "issuer-1", kid="old-key", secret_byte=80, status=KeyStatus.ACTIVE
        )
        new_active = self.signing_key(
            "issuer-1", kid="new-key", secret_byte=81, status=KeyStatus.ACTIVE
        )
        ring = RotatingHMACKeyRing(
            trust_domain="production",
            policy_version="policy-v1",
            keys=(old_active,),
        )
        verifier = self.make_verifier(ring)
        claims = self.claims()
        old_envelope = CapabilityEnvelope.signed(
            (claims,), signer=ring, root_kid="old-key", clock=self.clock
        )
        verifier.verify(old_envelope)

        old_verify_only = self.signing_key(
            "issuer-1",
            kid="old-key",
            secret_byte=80,
            status=KeyStatus.VERIFY_ONLY,
        )
        ring.replace_keys((old_verify_only, new_active))
        verifier.verify(old_envelope)
        with self.assertRaises(CapabilityVerificationError):
            CapabilityEnvelope.signed((claims,), signer=ring, root_kid="old-key", clock=self.clock)
        new_envelope = CapabilityEnvelope.signed(
            (claims,), signer=ring, root_kid="new-key", clock=self.clock
        )
        verifier.verify(new_envelope)

        old_revoked = self.signing_key(
            "issuer-1",
            kid="old-key",
            secret_byte=80,
            status=KeyStatus.REVOKED,
        )
        ring.replace_keys((old_revoked, new_active))
        with self.assertRaises(CapabilityVerificationError):
            verifier.verify(old_envelope)

    def test_key_rotation_rejects_status_rollback_identity_swap_and_kid_reuse(self):
        old_active = self.signing_key(
            "issuer-1", kid="old-key", secret_byte=80, status=KeyStatus.ACTIVE
        )
        replacement = self.signing_key(
            "issuer-1", kid="new-key", secret_byte=81, status=KeyStatus.ACTIVE
        )
        ring = RotatingHMACKeyRing(
            trust_domain="production",
            policy_version="policy-v1",
            keys=(old_active, replacement),
        )
        claims = self.claims()
        old_envelope = CapabilityEnvelope.signed(
            (claims,), signer=ring, root_kid="old-key", clock=self.clock
        )
        object.__setattr__(old_active, "secret", b"Z" * 32)
        after_external_mutation = CapabilityEnvelope.signed(
            (claims,), signer=ring, root_kid="old-key", clock=self.clock
        )
        self.assertEqual(
            after_external_mutation.root_proof.signature,
            old_envelope.root_proof.signature,
        )
        old_verify_only = self.signing_key(
            "issuer-1",
            kid="old-key",
            secret_byte=80,
            status=KeyStatus.VERIFY_ONLY,
        )
        ring.replace_keys((old_verify_only, replacement))

        reactivated = self.signing_key(
            "issuer-1", kid="old-key", secret_byte=80, status=KeyStatus.ACTIVE
        )
        with self.assertRaisesRegex(ValueError, "status cannot move backwards"):
            ring.replace_keys((reactivated, replacement))
        swapped_secret = self.signing_key(
            "issuer-1",
            kid="old-key",
            secret_byte=99,
            status=KeyStatus.VERIFY_ONLY,
        )
        with self.assertRaisesRegex(ValueError, "identity is immutable"):
            ring.replace_keys((swapped_secret, replacement))

        # Rejected replacements are atomic; the verify-only key still validates
        # proofs created before rotation.
        self.make_verifier(ring).verify(old_envelope)
        ring.replace_keys((replacement,))
        with self.assertRaisesRegex(ValueError, "removed.*cannot be reused"):
            ring.replace_keys((old_verify_only, replacement))

    def test_reflectively_removed_delegation_proof_fails_closed(self):
        root = self.claims(subject="agent-1", action="resource.*")
        child = root.delegate(
            subject_id="member-1",
            action="resource.read",
            resource=self.scope(resource_id="doc-1"),
            issued_at=NOW,
            not_before=NOW,
            expires_at=NOW + timedelta(minutes=30),
            nonce=CapabilityNonce(NONCE_B),
        )
        verified = self.verify((root, child))
        object.__setattr__(verified.envelope, "delegation_proofs", ())

        with self.assertRaisesRegex(
            CapabilityVerificationError, "exactly one proof per delegation edge"
        ):
            self.verifier.verify(verified.envelope)
        decision = self.authorizer.evaluate(
            self.request(), self.member(), self.snapshot(), (verified,)
        )
        self.assertEqual(decision.code, DecisionCode.CAPABILITY_INVALID)

    def test_verified_capability_copy_pickle_and_tamper_are_reverified(self):
        verified = self.verify((self.claims(),))
        variants = (
            copy.copy(verified),
            copy.deepcopy(verified),
            pickle.loads(pickle.dumps(verified)),
        )
        for variant in variants:
            with self.subTest(variant=variant):
                decision = self.authorizer.evaluate(
                    self.request(), self.member(), self.snapshot(), (variant,)
                )
                self.assertEqual(decision.code, DecisionCode.ALLOW_CAPABILITY)

        payload = verified.envelope.to_dict()
        payload["rootProof"]["signature"] = "A" * 43
        tampered = VerifiedCapability(
            envelope=CapabilityEnvelope.from_dict(payload),
            trust_domain="production",
            policy_version="policy-v1",
            verified_at=NOW,
        )
        decision = self.authorizer.evaluate(
            self.request(), self.member(), self.snapshot(), (tampered,)
        )
        self.assertEqual(decision.code, DecisionCode.CAPABILITY_INVALID)

    def test_revocation_revision_rollback_fails_closed_across_authorizers(self):
        owner = self.member(self.binding(Role.OWNER))
        accepted = self.authorizer.evaluate(self.request(), owner, self.snapshot(revision=5))
        rollback = self.authorizer.evaluate(self.request(), owner, self.snapshot(revision=4))
        restarted = self.make_authorizer(revision_guard=self.revision_guard)
        persisted_rollback = restarted.evaluate(self.request(), owner, self.snapshot(revision=3))
        equal_revision = restarted.evaluate(self.request(), owner, self.snapshot(revision=5))

        self.assertEqual(accepted.code, DecisionCode.ALLOW_RBAC)
        self.assertEqual(rollback.code, DecisionCode.REVOCATION_REVISION_ROLLBACK)
        self.assertEqual(persisted_rollback.code, DecisionCode.REVOCATION_REVISION_ROLLBACK)
        self.assertEqual(equal_revision.code, DecisionCode.ALLOW_RBAC)

    def test_same_revocation_revision_cannot_change_canonical_state(self):
        owner = self.member(self.binding(Role.OWNER))
        capability = self.claims()
        accepted_snapshot = self.snapshot(revision=5)
        accepted = self.authorizer.evaluate(self.request(), owner, accepted_snapshot)
        conflicting = self.authorizer.evaluate(
            self.request(),
            owner,
            self.snapshot(capability.revocation_id, revision=5),
        )
        refreshed_same_state = self.authorizer.evaluate(
            self.request(),
            owner,
            self.snapshot(captured_at=NOW + timedelta(seconds=1), revision=5),
        )

        self.assertEqual(accepted.code, DecisionCode.ALLOW_RBAC)
        self.assertEqual(conflicting.code, DecisionCode.REVOCATION_REVISION_ROLLBACK)
        self.assertEqual(refreshed_same_state.code, DecisionCode.ALLOW_RBAC)
        self.assertEqual(
            self.revision_guard.state_digest(self.tenant),
            accepted_snapshot.state_digest,
        )

    def test_authorizer_uses_revocation_snapshot_copy_across_guard_callback(self):
        claims = self.claims()
        verified = self.verify((claims,))
        source = self.snapshot(claims.revocation_id, revision=7)

        class MutatingGuard:
            def check_and_advance(_self, _tenant_id, _revision, _state_digest):
                object.__setattr__(source, "revoked_ids", frozenset())
                return True

        authorizer = self.make_authorizer(revision_guard=MutatingGuard())
        decision = authorizer.evaluate(self.request(), self.member(), source, (verified,))

        self.assertEqual(decision.code, DecisionCode.CAPABILITY_REVOKED)

    def test_sqlite_revision_guard_persists_revision_and_state_across_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "revocations.sqlite3")
            first_state = self.snapshot(revision=5)
            conflicting_state = self.snapshot(
                self.claims().revocation_id,
                revision=5,
            )
            next_state = self.snapshot(
                self.claims().revocation_id,
                revision=6,
            )
            with SQLiteRevocationRevisionGuard(path) as guard:
                self.assertTrue(
                    guard.check_and_advance(
                        self.tenant,
                        first_state.revision,
                        first_state.state_digest,
                    )
                )
                self.assertEqual(Path(path).stat().st_mode & 0o777, 0o600)

            with SQLiteRevocationRevisionGuard(path) as reopened:
                self.assertFalse(
                    reopened.check_and_advance(
                        self.tenant,
                        4,
                        first_state.state_digest,
                    )
                )
                self.assertFalse(
                    reopened.check_and_advance(
                        self.tenant,
                        conflicting_state.revision,
                        conflicting_state.state_digest,
                    )
                )
                self.assertTrue(
                    reopened.check_and_advance(
                        self.tenant,
                        first_state.revision,
                        first_state.state_digest,
                    )
                )
                self.assertTrue(
                    reopened.check_and_advance(
                        self.tenant,
                        next_state.revision,
                        next_state.state_digest,
                    )
                )
                self.assertEqual(reopened.high_water(self.tenant), 6)
                self.assertEqual(
                    reopened.state_digest(self.tenant),
                    next_state.state_digest,
                )

    def test_sqlite_revision_guard_serializes_independent_connections(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "concurrent-revocations.sqlite3")
            first = SQLiteRevocationRevisionGuard(path)
            second = SQLiteRevocationRevisionGuard(path)
            barrier = threading.Barrier(2)
            snapshot = self.snapshot(revision=12)

            def advance(guard):
                barrier.wait()
                return guard.check_and_advance(
                    self.tenant,
                    snapshot.revision,
                    snapshot.state_digest,
                )

            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    results = tuple(executor.map(advance, (first, second)))
                self.assertEqual(results, (True, True))
                self.assertEqual(first.high_water(self.tenant), 12)
                self.assertEqual(second.high_water(self.tenant), 12)
            finally:
                first.close()
                second.close()

    def test_sqlite_revision_guard_rejects_weak_schema_trigger_and_bad_row(self):
        with tempfile.TemporaryDirectory() as directory:
            weak_path = str(Path(directory) / "weak.sqlite3")
            connection = sqlite3.connect(weak_path)
            connection.execute(
                """
                CREATE TABLE qe_revocation_high_water (
                    tenant_id TEXT PRIMARY KEY,
                    revision INTEGER,
                    state_digest TEXT
                )
                """
            )
            connection.commit()
            connection.close()
            with self.assertRaisesRegex(RevocationGuardIntegrityError, "schema does not match"):
                SQLiteRevocationRevisionGuard(weak_path)

            path = str(Path(directory) / "tampered.sqlite3")
            snapshot = self.snapshot(revision=2)
            guard = SQLiteRevocationRevisionGuard(path)
            try:
                guard.check_and_advance(
                    self.tenant,
                    snapshot.revision,
                    snapshot.state_digest,
                )
                attacker = sqlite3.connect(path)
                attacker.execute("PRAGMA ignore_check_constraints=ON")
                attacker.execute(
                    """
                    UPDATE qe_revocation_high_water
                    SET state_digest = 'not-a-digest' WHERE tenant_id = ?
                    """,
                    (str(self.tenant),),
                )
                attacker.commit()
                attacker.close()
                with self.assertRaisesRegex(RevocationGuardIntegrityError, "invalid state digest"):
                    guard.high_water(self.tenant)
            finally:
                guard.close()

            clean_path = str(Path(directory) / "trigger.sqlite3")
            live_guard = SQLiteRevocationRevisionGuard(clean_path)
            connection = sqlite3.connect(clean_path)
            connection.execute(
                """
                CREATE TRIGGER qe_revision_attack
                AFTER UPDATE ON qe_revocation_high_water
                BEGIN
                    DELETE FROM qe_revocation_high_water;
                END
                """
            )
            connection.commit()
            connection.close()
            with self.assertRaisesRegex(
                RevocationGuardIntegrityError, "custom indexes or triggers"
            ):
                live_guard.check_and_advance(
                    self.tenant,
                    snapshot.revision,
                    snapshot.state_digest,
                )
            live_guard.close()
            with self.assertRaisesRegex(
                RevocationGuardIntegrityError, "custom indexes or triggers"
            ):
                SQLiteRevocationRevisionGuard(clean_path)

    def test_authorizer_rejects_rollback_after_sqlite_guard_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "authorization-revisions.sqlite3")
            owner = self.member(self.binding(Role.OWNER))
            with SQLiteRevocationRevisionGuard(path) as guard:
                authorizer = self.make_authorizer(revision_guard=guard)
                accepted = authorizer.evaluate(self.request(), owner, self.snapshot(revision=9))
            with SQLiteRevocationRevisionGuard(path) as reopened:
                restarted = self.make_authorizer(revision_guard=reopened)
                rollback = restarted.evaluate(self.request(), owner, self.snapshot(revision=8))

            self.assertEqual(accepted.code, DecisionCode.ALLOW_RBAC)
            self.assertEqual(
                rollback.code,
                DecisionCode.REVOCATION_REVISION_ROLLBACK,
            )

    def test_malformed_verified_capability_fails_closed_without_attribute_error(self):
        malformed = object.__new__(VerifiedCapability)
        decision = self.authorizer.evaluate(
            self.request(), self.member(), self.snapshot(), (malformed,)
        )
        self.assertEqual(decision.code, DecisionCode.CAPABILITY_INVALID)

        typed_but_bad = self.verify((self.claims(),))
        object.__setattr__(typed_but_bad, "envelope", "not-an-envelope")
        second = self.authorizer.evaluate(
            self.request(), self.member(), self.snapshot(), (typed_but_bad,)
        )
        self.assertEqual(second.code, DecisionCode.CAPABILITY_INVALID)

    def test_full_delegation_chain_and_ancestor_revocation(self):
        root = self.claims(
            issuer="owner-1",
            subject="agent-1",
            action="resource.*",
            resource=self.scope(workspace=None, resource_type="*"),
            nonce=NONCE_A,
            expires_at=NOW + timedelta(hours=2),
        )
        middle = root.delegate(
            subject_id="agent-2",
            action="resource.read",
            resource=self.scope(),
            issued_at=NOW - timedelta(seconds=30),
            not_before=NOW - timedelta(seconds=30),
            expires_at=NOW + timedelta(hours=1),
            nonce=CapabilityNonce(NONCE_B),
        )
        leaf = middle.delegate(
            subject_id="member-1",
            action="resource.read",
            resource=self.scope(resource_id="doc-1"),
            issued_at=NOW,
            not_before=NOW,
            expires_at=NOW + timedelta(minutes=30),
            nonce=CapabilityNonce(NONCE_C),
        )
        verified = self.verify((root, middle, leaf))

        allowed = self.authorizer.evaluate(
            self.request(), self.member(), self.snapshot(), (verified,)
        )
        revoked = self.authorizer.evaluate(
            self.request(),
            self.member(),
            self.snapshot(root.revocation_id, revision=2),
            (verified,),
        )

        self.assertEqual(allowed.code, DecisionCode.ALLOW_CAPABILITY)
        self.assertEqual(revoked.code, DecisionCode.CAPABILITY_REVOKED)
        self.assertIn("ancestor", revoked.reason)

    def test_authorizer_revalidates_envelope_instead_of_in_process_object(self):
        root = self.claims(subject="member-1")
        verified = self.verify((root,))
        forged_root = self.claims(
            subject="member-1",
            nonce=NONCE_B,
        )
        object.__setattr__(verified.envelope, "claims", (forged_root,))

        decision = self.authorizer.evaluate(
            self.request(), self.member(), self.snapshot(), (verified,)
        )
        self.assertEqual(decision.code, DecisionCode.CAPABILITY_INVALID)

        empty = self.verify((root,))
        object.__setattr__(empty.envelope, "claims", ())
        empty_decision = self.authorizer.evaluate(
            self.request(), self.member(), self.snapshot(), (empty,)
        )
        self.assertEqual(empty_decision.code, DecisionCode.CAPABILITY_INVALID)

    def test_revocation_inputs_are_strict_typed_objects(self):
        with self.assertRaises(TypeError):
            RevocationSnapshot(self.tenant, 1, NOW, "not-a-set")
        with self.assertRaises(TypeError):
            RevocationSnapshot(self.tenant, 1, NOW, b"not-a-set")
        with self.assertRaises(TypeError):
            self.authorizer.evaluate(self.request(), self.member(), "revoked")
        with self.assertRaises(TypeError):
            RevocationId(self.tenant, "issuer-1", NONCE_A)

    def test_revocation_id_is_scoped_by_tenant_and_issuer(self):
        first = self.claims(issuer="issuer-1", nonce=NONCE_A)
        second = self.claims(issuer="issuer-2", nonce=NONCE_A)
        first_verified = self.verify((first,))
        second_verified = self.verify((second,))
        snapshot = self.snapshot(first.revocation_id)

        first_only = self.authorizer.evaluate(
            self.request(), self.member(), snapshot, (first_verified,)
        )
        with_second = self.authorizer.evaluate(
            self.request(), self.member(), snapshot, (first_verified, second_verified)
        )

        self.assertNotEqual(first.revocation_id, second.revocation_id)
        self.assertEqual(first_only.code, DecisionCode.CAPABILITY_REVOKED)
        self.assertEqual(with_second.code, DecisionCode.ALLOW_CAPABILITY)

    def test_revoked_unrelated_capability_does_not_change_default_deny(self):
        unrelated = self.claims(action="resource.update")
        verified = self.verify((unrelated,))
        decision = self.authorizer.evaluate(
            self.request(),
            self.member(),
            self.snapshot(unrelated.revocation_id),
            (verified,),
        )

        self.assertEqual(decision.code, DecisionCode.DEFAULT_DENY)

    def test_nonce_generation_uses_at_least_128_bits(self):
        first = CapabilityNonce.generate(16)
        second = CapabilityNonce.generate()
        self.assertEqual(len(str(first)), 32)
        self.assertEqual(len(str(second)), 64)
        self.assertNotEqual(first, second)
        for invalid in ("00" * 15, "GG" * 16, "0" * 33):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                CapabilityNonce(invalid)

    def test_issued_not_before_audience_and_max_ttl_are_enforced(self):
        with self.assertRaises(ValueError):
            self.claims(not_before=NOW - timedelta(minutes=2))

        wrong_audience = self.claims(audience="another-service")
        long_lived = self.claims(expires_at=NOW + timedelta(days=1))
        future_issued = self.claims(
            issued_at=NOW + timedelta(minutes=1),
            not_before=NOW + timedelta(minutes=1),
            expires_at=NOW + timedelta(hours=1),
        )
        for claims in (wrong_audience, long_lived, future_issued):
            with self.subTest(claims=claims), self.assertRaises(CapabilityVerificationError):
                self.verify((claims,), max_ttl=timedelta(hours=4))

        future_active = self.claims(
            issued_at=NOW,
            not_before=NOW + timedelta(minutes=2),
            expires_at=NOW + timedelta(hours=1),
            nonce=NONCE_B,
        )
        verified = self.verify((future_active,))
        decision = self.authorizer.evaluate(
            self.request(), self.member(), self.snapshot(), (verified,)
        )
        self.assertEqual(decision.code, DecisionCode.CAPABILITY_NOT_YET_VALID)

    def test_revocation_snapshot_must_be_current_and_same_tenant(self):
        verified = self.verify((self.claims(),))
        foreign = self.snapshot(tenant=self.other_tenant)
        stale = self.snapshot(captured_at=NOW - timedelta(minutes=6))

        foreign_decision = self.authorizer.evaluate(
            self.request(), self.member(), foreign, (verified,)
        )
        stale_decision = self.authorizer.evaluate(self.request(), self.member(), stale, (verified,))

        self.assertEqual(foreign_decision.code, DecisionCode.REVOCATION_STATE_INVALID)
        self.assertEqual(stale_decision.code, DecisionCode.REVOCATION_STATE_STALE)

        stale_role = self.authorizer.evaluate(
            self.request(),
            self.member(self.binding(Role.OWNER)),
            self.snapshot(captured_at=NOW - timedelta(minutes=6)),
        )
        self.assertEqual(stale_role.code, DecisionCode.REVOCATION_STATE_STALE)

        role_decision = self.authorizer.evaluate(
            self.request(), self.member(self.binding(Role.OWNER)), foreign
        )
        self.assertEqual(role_decision.code, DecisionCode.REVOCATION_STATE_INVALID)

    def test_tenant_wide_role_covers_workspaces_but_never_another_tenant(self):
        tenant_wide = self.member(self.binding(Role.VIEWER, workspace=None, resource_type="*"))
        first = self.authorizer.evaluate(
            self.request(resource=self.resource(workspace=self.workspace)),
            tenant_wide,
            self.snapshot(),
        )
        second = self.authorizer.evaluate(
            self.request(resource=self.resource(workspace=self.other_workspace)),
            tenant_wide,
            self.snapshot(),
        )
        foreign = self.authorizer.evaluate(
            self.request(resource=self.resource(tenant=self.other_tenant)),
            tenant_wide,
            self.snapshot(),
        )

        self.assertEqual(first.code, DecisionCode.ALLOW_RBAC)
        self.assertEqual(second.code, DecisionCode.ALLOW_RBAC)
        self.assertEqual(foreign.code, DecisionCode.CROSS_TENANT)

    def test_workspace_role_and_default_deny(self):
        editor = self.member(self.binding(Role.EDITOR))
        allowed = self.authorizer.evaluate(
            self.request(action="resource.update"), editor, self.snapshot()
        )
        outside = self.authorizer.evaluate(
            self.request(
                action="resource.update",
                resource=self.resource(workspace=self.other_workspace),
            ),
            editor,
            self.snapshot(),
        )
        denied = self.authorizer.evaluate(
            self.request(action="resource.delete"), editor, self.snapshot()
        )

        self.assertEqual(allowed.code, DecisionCode.ALLOW_RBAC)
        self.assertEqual(outside.code, DecisionCode.OUTSIDE_SCOPE)
        self.assertEqual(denied.code, DecisionCode.DEFAULT_DENY)

    def test_scope_attenuation_matrix(self):
        parent = self.scope(workspace=None, resource_type="*", resource_id="*")
        workspace = self.scope(resource_type="*", resource_id="*")
        typed = self.scope(resource_type="document", resource_id="*")
        exact = self.scope(resource_type="document", resource_id="doc-1")
        foreign_workspace = self.scope(
            workspace=self.other_workspace, resource_type="document", resource_id="*"
        )
        foreign_tenant = self.scope(
            tenant=self.other_tenant, resource_type="document", resource_id="*"
        )

        matrix = (
            (parent, workspace, True),
            (workspace, parent, False),
            (workspace, typed, True),
            (typed, workspace, False),
            (typed, exact, True),
            (exact, typed, False),
            (workspace, foreign_workspace, False),
            (parent, foreign_tenant, False),
        )
        for outer, inner, expected in matrix:
            with self.subTest(outer=outer, inner=inner):
                self.assertEqual(outer.contains(inner), expected)

    def test_delegation_rejects_every_privilege_amplification_axis(self):
        parent = self.claims(
            issuer="owner-1",
            subject="agent-1",
            action="resource.*",
            resource=self.scope(),
            expires_at=NOW + timedelta(hours=2),
        )
        base = dict(
            issuer_id="agent-1",
            subject_id="member-1",
            action="resource.read",
            resource=self.scope(resource_id="doc-1"),
            issued_at=NOW,
            not_before=NOW,
            expires_at=NOW + timedelta(hours=1),
            audience=AUDIENCE,
            nonce=CapabilityNonce(NONCE_B),
            parent_fingerprint=parent.fingerprint,
        )
        valid = CapabilityClaims(**base)
        validate_delegation(parent, valid)

        invalid_values = (
            {"action": "member.read"},
            {"resource": self.scope(workspace=self.other_workspace)},
            {"expires_at": parent.expires_at + timedelta(seconds=1)},
            {
                "issued_at": parent.issued_at - timedelta(seconds=1),
                "not_before": parent.not_before,
            },
            {"audience": "other-service"},
            {"issuer_id": "attacker-1"},
            {"parent_fingerprint": "f" * 64},
        )
        for changes in invalid_values:
            values = dict(base)
            values.update(changes)
            child = CapabilityClaims(**values)
            with self.subTest(changes=changes), self.assertRaises(DelegationError):
                validate_delegation(parent, child)

    def test_strict_parsers_reject_coercion_unknown_fields_and_bad_rfc3339(self):
        with self.assertRaises(TypeError):
            AccessRequest.from_dict(UserDict(self.request().to_dict()))

        request_payload = self.request().to_dict()
        request_payload["tenantId"] = 123
        with self.assertRaises(TypeError):
            AccessRequest.from_dict(request_payload)

        member_payload = self.member().to_dict()
        member_payload["unexpected"] = True
        with self.assertRaises(ValueError):
            Member.from_dict(member_payload)
        member_payload.pop("unexpected")
        member_payload[7] = "not-a-json-key"
        with self.assertRaises(TypeError):
            Member.from_dict(member_payload)

        claims_payload = self.claims().to_dict()
        claims_payload["issuerId"] = 7
        with self.assertRaises(TypeError):
            CapabilityClaims.from_dict(claims_payload)

        for timestamp in (
            "2026-08-20 01:02:03Z",
            "2026-08-20T01:02Z",
            "2026-08-20T01:02:03z",
            "2026-08-20T01:02:03.1234567Z",
            "2026-08-20T01:02:03",
            "2026-08-20T01:02:03-00:00",
        ):
            payload = self.claims().to_dict()
            payload["issuedAt"] = timestamp
            with self.subTest(timestamp=timestamp), self.assertRaises(ValueError):
                CapabilityClaims.from_dict(payload)

        snapshot_payload = self.snapshot().to_dict()
        snapshot_payload["revision"] = True
        with self.assertRaises(TypeError):
            RevocationSnapshot.from_dict(snapshot_payload)

        duplicate_snapshot = self.snapshot(self.claims().revocation_id).to_dict()
        duplicate_snapshot["revokedIds"].append(copy.deepcopy(duplicate_snapshot["revokedIds"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate revoked ids"):
            RevocationSnapshot.from_dict(duplicate_snapshot)

    def test_round_trip_and_tamper_detection(self):
        claims = self.claims()
        envelope = self.envelope((claims,))
        request = self.request()
        member = self.member(self.binding(Role.AGENT))
        snapshot = self.snapshot(claims.revocation_id)
        decision = self.authorizer.evaluate(request, member, snapshot)

        self.assertEqual(CapabilityClaims.from_dict(claims.to_dict()), claims)
        self.assertEqual(CapabilityEnvelope.from_dict(envelope.to_dict()), envelope)
        self.assertEqual(AccessRequest.from_dict(request.to_dict()), request)
        self.assertEqual(Member.from_dict(member.to_dict()), member)
        self.assertEqual(RevocationSnapshot.from_dict(snapshot.to_dict()), snapshot)
        self.assertEqual(AuthorizationDecision.from_dict(decision.to_dict()), decision)

        tampered = claims.to_dict()
        tampered["subjectId"] = "attacker-1"
        with self.assertRaises(ValueError):
            CapabilityClaims.from_dict(tampered)

    def test_audit_decision_is_deterministic_and_order_independent(self):
        viewer = self.binding(Role.VIEWER)
        owner = self.binding(Role.OWNER)
        first = self.authorizer.evaluate(
            self.request(), self.member(viewer, owner), self.snapshot(revision=9)
        )
        second = self.authorizer.evaluate(
            self.request(), self.member(owner, viewer), self.snapshot(revision=9)
        )

        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(first.decision_id, second.decision_id)
        self.assertNotIn(NONCE_A, str(first.to_dict()))

    def test_audit_decision_rejects_outcome_code_mismatch(self):
        values = {
            "reason": "canonical audit decision",
            "request": self.request(),
            "evaluated_at": NOW,
        }
        with self.assertRaisesRegex(ValueError, "outcome and decision code"):
            AuthorizationDecision(
                outcome=AuthorizationOutcome.ALLOW,
                code=DecisionCode.DEFAULT_DENY,
                **values,
            )
        with self.assertRaisesRegex(ValueError, "outcome and decision code"):
            AuthorizationDecision(
                outcome=AuthorizationOutcome.DENY,
                code=DecisionCode.ALLOW_RBAC,
                **values,
            )

    def test_cross_tenant_inactive_and_subject_mismatch_denials(self):
        owner = self.member(self.binding(Role.OWNER, workspace=None, resource_type="*"))
        cross_resource = self.authorizer.evaluate(
            self.request(resource=self.resource(tenant=self.other_tenant)),
            owner,
            self.snapshot(),
        )
        cross_member = self.authorizer.evaluate(
            self.request(
                tenant=self.other_tenant,
                resource=self.resource(tenant=self.other_tenant),
            ),
            owner,
            self.snapshot(),
        )
        inactive = self.authorizer.evaluate(
            self.request(),
            self.member(self.binding(Role.OWNER), status=MemberStatus.SUSPENDED),
            self.snapshot(),
        )
        mismatch = self.authorizer.evaluate(
            self.request(),
            self.member(self.binding(Role.OWNER), member_id="member-2"),
            self.snapshot(),
        )

        self.assertEqual(cross_resource.code, DecisionCode.CROSS_TENANT)
        self.assertEqual(cross_member.code, DecisionCode.CROSS_TENANT)
        self.assertEqual(inactive.code, DecisionCode.MEMBER_INACTIVE)
        self.assertEqual(mismatch.code, DecisionCode.SUBJECT_MISMATCH)


if __name__ == "__main__":
    unittest.main()
