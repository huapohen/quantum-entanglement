from __future__ import annotations

import hashlib
import json
import math
import unittest
from dataclasses import replace

import quantum_entanglement
import quantum_entanglement.invocation_worker as invocation_worker_module
from quantum_entanglement.attempts import InvocationLease
from quantum_entanglement.invocation_execution import (
    EffectClass,
    InvocationExecutionManifest,
    InvocationStartClaimed,
    InvocationStartEvidenceV2,
    InvocationStartObserved,
    InvocationStartReceipt,
)
from quantum_entanglement.invocation_worker import (
    HeartbeatPureWorkerGate,
    InvocationWorkerAdmission,
    InvocationWorkerConfiguration,
    InvocationWorkerDisabledError,
)

LEASE_TOKEN = "worker-gate-secret-lease-canary"
CLAIMED_AT = "2026-08-27T08:00:00.000001Z"
EXPIRES_AT = "2026-08-27T08:01:00.000001Z"
RUNTIME_REVISION = "runtime:sha256:" + ("d" * 64)


def valid_manifest() -> InvocationExecutionManifest:
    return InvocationExecutionManifest.from_dict(
        {
            "schemaVersion": 1,
            "invocationId": "invocation-worker-1",
            "sessionId": "session-worker-1",
            "planId": "plan-worker-1",
            "taskId": "task-worker-1",
            "agentId": "agent-worker-1",
            "jobIdempotencyKey": "invoke:task-worker-1",
            "taskRevision": 7,
            "correlationId": "correlation-worker-1",
            "causationId": "task-worker-1",
            "envelopeDigest": "a" * 64,
            "contextDigest": "b" * 64,
            "authorizationDigest": "c" * 64,
            "runtimeRevision": RUNTIME_REVISION,
            "effectClass": "pure",
            "retryClass": "never",
        }
    )


def valid_claim(manifest: InvocationExecutionManifest | None = None) -> InvocationStartClaimed:
    selected = manifest or valid_manifest()
    evidence = InvocationStartEvidenceV2(
        schema_version=2,
        invocation_id=selected.invocation_id,
        session_id=selected.session_id,
        plan_id=selected.plan_id,
        task_id=selected.task_id,
        agent_id=selected.agent_id,
        job_idempotency_key=selected.job_idempotency_key,
        attempt_id="attempt-worker-1",
        attempt_number=1,
        lease_epoch=1,
        worker_id="worker-1",
        lease_token_digest=hashlib.sha256(LEASE_TOKEN.encode("utf-8")).hexdigest(),
        claimed_at=CLAIMED_AT,
        lease_expires_at=EXPIRES_AT,
        manifest_digest=selected.canonical_digest(),
        envelope_digest=selected.envelope_digest,
        context_digest=selected.context_digest,
        authorization_digest=selected.authorization_digest,
        runtime_revision=selected.runtime_revision,
        correlation_id=selected.correlation_id,
        causation_id=selected.causation_id,
    )
    receipt = InvocationStartReceipt(
        event_id="event-invocation-worker-start-1",
        stream_id="session:" + selected.session_id,
        sequence=3,
        global_position=9,
        evidence=evidence,
    )
    lease = InvocationLease(
        invocation_id=selected.invocation_id,
        session_id=selected.session_id,
        plan_id=selected.plan_id,
        task_id=selected.task_id,
        agent_id=selected.agent_id,
        idempotency_key=selected.job_idempotency_key,
        payload_digest=selected.canonical_digest(),
        attempt_id=evidence.attempt_id,
        attempt_number=evidence.attempt_number,
        max_attempts=1,
        lease_epoch=evidence.lease_epoch,
        worker_id=evidence.worker_id,
        lease_token=LEASE_TOKEN,
        claimed_at=evidence.claimed_at,
        lease_expires_at=evidence.lease_expires_at,
    )
    return InvocationStartClaimed(receipt, lease)


def valid_configuration() -> InvocationWorkerConfiguration:
    return InvocationWorkerConfiguration(
        lease_seconds=60,
        heartbeat_interval_seconds=15,
        handler_timeout_seconds=30,
        drain_timeout_seconds=10,
    )


class InvocationWorkerAdmissionTests(unittest.TestCase):
    def test_worker_contracts_are_exported_from_the_package_surface(self) -> None:
        expected = {
            "HeartbeatPureWorkerGate": HeartbeatPureWorkerGate,
            "InvocationWorkerAdmission": InvocationWorkerAdmission,
            "InvocationWorkerConfiguration": InvocationWorkerConfiguration,
            "InvocationWorkerDisabledError": InvocationWorkerDisabledError,
        }
        self.assertEqual(set(invocation_worker_module.__all__), set(expected))
        for name, value in expected.items():
            with self.subTest(name=name):
                self.assertIn(name, quantum_entanglement.__all__)
                self.assertIs(getattr(quantum_entanglement, name), value)

    def test_prepare_snapshots_exact_claim_manifest_and_configuration(self) -> None:
        manifest = valid_manifest()
        claim = valid_claim(manifest)
        configuration = valid_configuration()

        admission = HeartbeatPureWorkerGate.prepare(
            claim,
            manifest,
            configuration,
            handler_revision=RUNTIME_REVISION,
        )

        self.assertIs(type(admission), InvocationWorkerAdmission)
        self.assertIsNot(admission.claim, claim)
        self.assertIsNot(admission.manifest, manifest)
        self.assertIsNot(admission.configuration, configuration)
        self.assertEqual(admission.claim.receipt, claim.receipt)
        self.assertEqual(admission.manifest, manifest)
        self.assertEqual(admission.configuration, configuration)
        self.assertNotIn(LEASE_TOKEN, repr(admission))
        self.assertNotIn(LEASE_TOKEN, str(admission))
        with self.assertRaises(TypeError):
            json.dumps(admission)

    def test_prepare_rejects_observation_receipt_and_lease_without_authority(self) -> None:
        claim = valid_claim()
        invalid_claims = (
            InvocationStartObserved(claim.receipt),
            claim.receipt,
            claim.lease,
            claim.to_dict() if hasattr(claim, "to_dict") else {"receipt": claim.receipt.to_dict()},
        )
        for invalid in invalid_claims:
            with self.subTest(claim_type=type(invalid).__name__):
                with self.assertRaisesRegex(TypeError, "exact InvocationStartClaimed"):
                    HeartbeatPureWorkerGate.prepare(
                        invalid,  # type: ignore[arg-type]
                        valid_manifest(),
                        valid_configuration(),
                        handler_revision=RUNTIME_REVISION,
                    )

    def test_prepare_rejects_every_manifest_start_binding_drift(self) -> None:
        manifest = valid_manifest()
        claim = valid_claim(manifest)
        mismatches = {
            "invocation_id": {"invocation_id": "invocation-worker-other"},
            "session_id": {"session_id": "session-worker-other"},
            "plan_id": {"plan_id": "plan-worker-other"},
            "task_id": {
                "task_id": "task-worker-other",
                "causation_id": "task-worker-other",
            },
            "agent_id": {"agent_id": "agent-worker-other"},
            "job_idempotency_key": {"job_idempotency_key": "invoke:task-worker-other"},
            "envelope_digest": {"envelope_digest": "1" * 64},
            "context_digest": {"context_digest": "2" * 64},
            "authorization_digest": {"authorization_digest": "3" * 64},
            "runtime_revision": {"runtime_revision": "runtime:sha256:" + ("4" * 64)},
            "correlation_id": {"correlation_id": "correlation-worker-other"},
        }
        for field_name, changes in mismatches.items():
            with self.subTest(field=field_name):
                changed = replace(manifest, **changes)
                with self.assertRaisesRegex(ValueError, "manifest"):
                    HeartbeatPureWorkerGate.prepare(
                        claim,
                        changed,
                        valid_configuration(),
                        handler_revision=changed.runtime_revision,
                    )

    def test_prepare_rejects_non_pure_effect_class(self) -> None:
        manifest = valid_manifest()
        for effect_class in (
            EffectClass.IDEMPOTENT,
            EffectClass.RECEIPT_RECONCILED,
            EffectClass.NON_RETRIABLE,
        ):
            with self.subTest(effect_class=effect_class.value):
                changed = replace(manifest, effect_class=effect_class)
                changed_claim = valid_claim(changed)
                with self.assertRaisesRegex(ValueError, "effectClass=pure"):
                    HeartbeatPureWorkerGate.prepare(
                        changed_claim,
                        changed,
                        valid_configuration(),
                        handler_revision=RUNTIME_REVISION,
                    )

    def test_prepare_rejects_handler_revision_drift(self) -> None:
        with self.assertRaisesRegex(ValueError, "handler revision"):
            HeartbeatPureWorkerGate.prepare(
                valid_claim(),
                valid_manifest(),
                valid_configuration(),
                handler_revision="runtime:sha256:" + ("e" * 64),
            )

    def test_configuration_normalizes_exact_numbers_and_enforces_timing_order(self) -> None:
        configuration = valid_configuration()
        self.assertEqual(
            (
                configuration.lease_seconds,
                configuration.heartbeat_interval_seconds,
                configuration.handler_timeout_seconds,
                configuration.drain_timeout_seconds,
            ),
            (60.0, 15.0, 30.0, 10.0),
        )

        fields = (
            "lease_seconds",
            "heartbeat_interval_seconds",
            "handler_timeout_seconds",
            "drain_timeout_seconds",
        )
        invalid_scalars = (True, 0, -1, math.nan, math.inf, "1")
        baseline = {
            "lease_seconds": 60,
            "heartbeat_interval_seconds": 15,
            "handler_timeout_seconds": 30,
            "drain_timeout_seconds": 10,
        }
        for field_name in fields:
            for value in invalid_scalars:
                with self.subTest(field=field_name, value=value):
                    values = dict(baseline)
                    values[field_name] = value
                    with self.assertRaises((TypeError, ValueError)):
                        InvocationWorkerConfiguration(**values)  # type: ignore[arg-type]

        for values, message in (
            (
                {
                    **baseline,
                    "heartbeat_interval_seconds": 21,
                },
                "one third",
            ),
            (
                {
                    **baseline,
                    "handler_timeout_seconds": 60,
                },
                "shorter than the lease",
            ),
            (
                {
                    **baseline,
                    "handler_timeout_seconds": 50,
                    "drain_timeout_seconds": 11,
                },
                "remaining",
            ),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    InvocationWorkerConfiguration(**values)


class HeartbeatPureWorkerDisabledTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatch_is_disabled_from_an_argument_free_error_frame(self) -> None:
        canary = "disabled-dispatch-private-handler-canary"

        class Hostile:
            def __repr__(self) -> str:
                raise AssertionError(canary)

        gate = HeartbeatPureWorkerGate()
        self.assertFalse(gate.dispatch_enabled)

        try:
            await gate.dispatch(Hostile(), Hostile())
        except InvocationWorkerDisabledError as error:
            caught = error
            frames = []
            traceback = error.__traceback__
            while traceback is not None:
                frames.append(traceback.tb_frame.f_code.co_name)
                traceback = traceback.tb_next
        else:  # pragma: no cover - the gate is deliberately unreachable.
            self.fail("disabled worker dispatch unexpectedly returned")
        self.assertEqual(caught.code, "invocation_worker_disabled")
        self.assertNotIn(canary, str(caught))
        self.assertNotIn("dispatch", frames)
        self.assertIn("_disabled_dispatch", frames)
        self.assertIsNone(caught.__cause__)
        self.assertIsNone(caught.__context__)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
