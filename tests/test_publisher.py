import asyncio
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from quantum_entanglement.delivery import OutboxMessage, OutboxStatus
from quantum_entanglement.events import DomainEvent
from quantum_entanglement.publisher import (
    OutboxPublisher,
    PublisherClosedError,
    PublishReceipt,
    PublishRequest,
    PublishResult,
)
from quantum_entanglement.store import SQLiteEventStore

READY = datetime(2026, 8, 20, tzinfo=timezone.utc)


async def wait_thread_signal(event):
    # A threading.Event deliberately coordinates independent connector loops here.
    while not event.is_set():  # noqa: ASYNC110
        await asyncio.sleep(0.001)


class ManualClock:
    def __init__(self, value=READY):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)

    def timestamp(self):
        return self.value.isoformat().replace("+00:00", "Z")


class RecordingStore:
    def __init__(self, delegate):
        self.delegate = delegate
        self.calls = []

    def claim_outbox(self, *args, **kwargs):
        self.calls.append(("claim", threading.get_ident()))
        return self.delegate.claim_outbox(*args, **kwargs)

    def acknowledge_outbox(self, *args, **kwargs):
        self.calls.append(("ack", threading.get_ident()))
        return self.delegate.acknowledge_outbox(*args, **kwargs)

    def reject_outbox(self, *args, **kwargs):
        self.calls.append(("reject", threading.get_ident()))
        return self.delegate.reject_outbox(*args, **kwargs)


class OutboxPublisherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        path = str(Path(self.tempdir.name) / "publisher.sqlite3")
        self.clock = ManualClock()
        self.store = SQLiteEventStore(path, clock=self.clock.timestamp)
        self.sequence = 0

    async def asyncTearDown(self):
        self.store.close()
        self.tempdir.cleanup()

    def enqueue(self, message_id):
        timestamp = self.clock().isoformat().replace("+00:00", "Z")
        outgoing = OutboxMessage(
            destination="test-broker",
            payload={"message": message_id},
            headers={"traceparent": f"trace-{message_id}"},
            message_id=message_id,
            idempotency_key=f"publish:{message_id}",
            available_at=timestamp,
            created_at=timestamp,
        )
        event = DomainEvent(
            stream_id="session:publisher",
            event_type="message.queued",
            payload={"messageId": message_id},
            actor_id="publisher-test",
            idempotency_key=f"queue:{message_id}",
        )
        self.store.append_with_outbox(event, (outgoing,), expected_version=self.sequence)
        self.sequence += 1

    def publisher(self, callback, **overrides):
        store = overrides.pop("store", self.store)
        options = {
            "worker_id": "publisher-1",
            "batch_size": 8,
            "lease_seconds": 1.0,
            "publish_timeout": 0.25,
            "poll_interval": 0.01,
            "max_attempts": 3,
            "base_retry_delay": 2.0,
            "max_retry_delay": 60.0,
            "jitter": lambda delay: delay,
            "clock": self.clock,
        }
        options.update(overrides)
        return OutboxPublisher(store, callback, **options)

    async def wait_until(self, predicate, timeout=0.5):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not predicate():
            if loop.time() >= deadline:
                self.fail("condition was not satisfied before timeout")
            await asyncio.sleep(0)

    async def wait_thread_event(self, event, timeout=0.5):
        self.assertTrue(await asyncio.to_thread(event.wait, timeout))

    async def test_claims_a_batch_and_publishes_it_concurrently(self):
        for index in range(3):
            self.enqueue(f"message-{index}")
        all_started = threading.Event()
        release = threading.Event()
        started = set()

        async def publish(request):
            started.add(request.message_id)
            if len(started) == 3:
                all_started.set()
            await wait_thread_signal(release)
            return PublishReceipt.accepted(f"receipt:{request.message_id}")

        publisher = self.publisher(publish, batch_size=3)
        running = asyncio.create_task(publisher.run_once())
        await self.wait_thread_event(all_started)
        self.assertFalse(running.done())
        release.set()
        batch = await running

        self.assertEqual(batch.claimed, 3)
        self.assertEqual(batch.published, 3)
        self.assertEqual(len(self.store.read_outbox(status=OutboxStatus.PUBLISHED)), 3)

    async def test_retry_exposes_the_same_stable_idempotency_key(self):
        self.enqueue("message-stable")
        requests = []
        downstream_seen = set()
        external_effects = []

        async def publish(request):
            requests.append(request)
            if request.idempotency_key not in downstream_seen:
                downstream_seen.add(request.idempotency_key)
                external_effects.append(request.message_id)
            if len(requests) == 1:
                # The broker accepted the first request, but its response was lost.
                raise ConnectionError("transport response lost")
            return PublishReceipt.accepted("receipt:stable")

        publisher = self.publisher(publish, base_retry_delay=5, max_retry_delay=5)
        first = await publisher.run_once()
        self.clock.advance(5)
        second = await publisher.run_once()

        self.assertEqual(first.retried, 1)
        self.assertEqual(second.published, 1)
        self.assertEqual(
            [request.idempotency_key for request in requests],
            ["publish:message-stable", "publish:message-stable"],
        )
        self.assertEqual([request.attempt_count for request in requests], [1, 2])
        self.assertEqual(external_effects, ["message-stable"])

    async def test_exponential_backoff_uses_injected_jitter(self):
        self.enqueue("message-backoff")
        base_delays = []

        async def fail(_request):
            raise OSError("offline")

        def half_jitter(delay):
            base_delays.append(delay)
            return delay / 2

        publisher = self.publisher(
            fail,
            base_retry_delay=8,
            max_retry_delay=30,
            max_attempts=4,
            jitter=half_jitter,
        )
        await publisher.run_once()
        first = self.store.get_outbox("message-backoff")
        self.assertEqual(first.message.available_at, "2026-08-20T00:00:04Z")

        self.clock.advance(4)
        await publisher.run_once()
        second = self.store.get_outbox("message-backoff")

        self.assertEqual(base_delays, [8.0, 16.0])
        self.assertEqual(second.message.available_at, "2026-08-20T00:00:12Z")
        self.assertEqual(second.attempt_count, 2)

    async def test_failure_at_max_attempts_moves_message_to_dead_letter(self):
        self.enqueue("message-dead")

        async def fail(_request):
            raise RuntimeError("broker rejected message")

        publisher = self.publisher(
            fail,
            max_attempts=2,
            base_retry_delay=3,
            max_retry_delay=3,
        )
        first = await publisher.run_once()
        self.clock.advance(3)
        second = await publisher.run_once()
        stored = self.store.get_outbox("message-dead")

        self.assertEqual(first.retried, 1)
        self.assertEqual(second.dead_lettered, 1)
        self.assertEqual(stored.status, OutboxStatus.DEAD_LETTER)
        self.assertEqual(stored.attempt_count, 2)
        self.assertEqual(stored.last_error, "connector_failure")

    async def test_publish_timeout_is_unconfirmed_and_not_blindly_nacked(self):
        self.enqueue("message-timeout")
        release = threading.Event()

        async def hang(_request):
            await wait_thread_signal(release)
            return PublishReceipt.accepted("receipt:too-late")

        publisher = self.publisher(hang, publish_timeout=0.01, max_attempts=1)
        batch = await publisher.run_once()
        stored = self.store.get_outbox("message-timeout")

        self.assertEqual(batch.publish_failures, 0)
        self.assertEqual(batch.timed_out, 1)
        self.assertEqual(batch.accepted_unconfirmed, 1)
        self.assertEqual(batch.abandoned_callbacks, 1)
        self.assertEqual(stored.status, OutboxStatus.IN_FLIGHT)
        self.assertIsNone(stored.last_error)
        self.assertEqual(len(self.store.read_outbox_ambiguities()), 1)
        release.set()
        await self.wait_until(lambda: not publisher.abandoned)

    async def test_cancellation_resistant_callback_has_a_hard_caller_deadline(self):
        self.enqueue("message-stubborn")
        release_late_callback = threading.Event()

        async def ignore_first_cancellation(_request):
            await wait_thread_signal(release_late_callback)
            return PublishReceipt.accepted("receipt:late")

        publisher = self.publisher(ignore_first_cancellation, publish_timeout=0.01)
        batch = await asyncio.wait_for(publisher.run_once(), timeout=0.15)

        self.assertEqual(batch.accepted_unconfirmed, 1)
        self.assertEqual(batch.abandoned_callbacks, 1)
        self.assertEqual(len(publisher.abandoned), 1)
        self.assertEqual(publisher.abandoned[0].message_id, "message-stubborn")
        self.assertEqual(publisher.stats.leaked_callbacks, 1)

        closed = await asyncio.wait_for(publisher.close(), timeout=0.15)
        self.assertFalse(closed.shutdown_clean)
        self.assertEqual(closed.lifecycle_state, "closed_with_abandoned_callbacks")

        release_late_callback.set()
        await self.wait_until(lambda: not publisher.abandoned)
        self.assertEqual(publisher.stats.lifecycle_state, "closed_unconfirmed")
        self.assertEqual(
            self.store.get_outbox("message-stubborn").status,
            OutboxStatus.IN_FLIGHT,
        )

    async def test_stop_has_a_hard_deadline_with_a_stubborn_service_callback(self):
        self.enqueue("message-stubborn-service")
        started = threading.Event()
        release_late_callback = threading.Event()

        async def ignore_first_cancellation(_request):
            started.set()
            await wait_thread_signal(release_late_callback)
            return PublishReceipt.accepted("receipt:late-service")

        publisher = self.publisher(ignore_first_cancellation, publish_timeout=0.01)
        service = publisher.start()
        await self.wait_thread_event(started, timeout=0.15)
        stopped = await asyncio.wait_for(publisher.stop(), timeout=0.15)
        service_result = await asyncio.wait_for(service, timeout=0.15)

        self.assertEqual(stopped.lifecycle_state, "closed_with_abandoned_callbacks")
        self.assertEqual(service_result.leaked_callbacks, 1)
        self.assertFalse(stopped.shutdown_clean)

        release_late_callback.set()
        await self.wait_until(lambda: not publisher.abandoned)

    async def test_blocking_async_connector_body_is_isolated_from_caller_loop(self):
        self.enqueue("message-blocking-body")

        async def blocks_its_own_loop(_request):
            time.sleep(0.15)  # noqa: ASYNC251 - models a broken async connector.
            return PublishReceipt.accepted("receipt:blocking")

        publisher = self.publisher(blocks_its_own_loop, publish_timeout=0.01)
        started = asyncio.get_running_loop().time()
        batch = await asyncio.wait_for(publisher.run_once(), timeout=0.1)
        elapsed = asyncio.get_running_loop().time() - started

        self.assertLess(elapsed, 0.1)
        self.assertEqual(batch.timed_out, 1)
        self.assertEqual(batch.ambiguity_persisted, 1)
        await self.wait_until(lambda: not publisher.abandoned, timeout=0.5)

    async def test_external_cycle_cancel_is_durably_ambiguous_before_propagation(self):
        self.enqueue("message-caller-cancelled")
        started = threading.Event()
        release = threading.Event()

        async def publish(_request):
            started.set()
            await wait_thread_signal(release)
            return PublishReceipt.accepted("receipt:caller-cancelled")

        publisher = self.publisher(publish, publish_timeout=0.5)
        cycle = asyncio.create_task(publisher.run_once())
        await self.wait_thread_event(started)
        cycle.cancel()
        cycle.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cycle

        ambiguities = self.store.read_outbox_ambiguities()
        self.assertEqual(len(ambiguities), 1)
        self.assertEqual(ambiguities[0].reason_code, "caller_cancelled")
        self.assertEqual(publisher.stats.cancelled_cycles, 1)
        self.assertGreaterEqual(publisher.stats.accepted_unconfirmed, 1)

        stopped = await publisher.stop()
        self.assertFalse(stopped.shutdown_clean)
        self.assertEqual(stopped.lifecycle_state, "closed_with_abandoned_callbacks")
        release.set()
        await self.wait_until(lambda: not publisher.abandoned)

    async def test_bounded_admission_stops_unlimited_abandoned_connectors(self):
        self.enqueue("message-admission-1")
        self.enqueue("message-admission-2")
        release = threading.Event()

        async def publish(request):
            await wait_thread_signal(release)
            return PublishReceipt.accepted(f"receipt:{request.message_id}")

        publisher = self.publisher(
            publish,
            batch_size=2,
            max_callback_tasks=1,
            publish_timeout=0.01,
        )
        first = await publisher.run_once()
        second = await publisher.run_once()

        self.assertEqual(first.claimed, 1)
        self.assertEqual(first.timed_out, 1)
        self.assertEqual(second.claimed, 0)
        self.assertEqual(second.admission_rejected, 1)
        self.assertEqual(publisher.stats.leaked_callbacks, 1)
        self.assertEqual(
            self.store.get_outbox("message-admission-2").status,
            OutboxStatus.PENDING,
        )

        release.set()
        await self.wait_until(lambda: not publisher.abandoned)
        third = await publisher.run_once()
        self.assertEqual(third.published, 1)

    async def test_ambiguous_message_requires_operator_resolution_before_retry(self):
        self.enqueue("message-operator-reconcile")
        release = threading.Event()

        async def publish(_request):
            await wait_thread_signal(release)
            return PublishReceipt.accepted("receipt:operator-reconcile")

        publisher = self.publisher(
            publish,
            max_attempts=1,
            publish_timeout=0.01,
        )
        first = await publisher.run_once()
        blocked = await publisher.run_once()
        ambiguity = self.store.read_outbox_ambiguities()[0]

        self.assertEqual(first.timed_out, 1)
        self.assertEqual(blocked.claimed, 0)
        self.assertEqual(
            self.store.get_outbox("message-operator-reconcile").attempt_count,
            1,
        )

        release.set()
        await self.wait_until(lambda: not publisher.abandoned)
        self.assertTrue(
            self.store.resolve_outbox_ambiguity(
                ambiguity.message_id,
                ambiguity.lease_token_digest,
                "retry",
                resolved_at="2026-08-20T00:00:01Z",
                retry_at="2026-08-20T00:00:01Z",
            )
        )
        self.clock.advance(1)
        retried = await publisher.run_once()

        self.assertEqual(retried.published, 1)
        self.assertEqual(
            self.store.get_outbox("message-operator-reconcile").attempt_count,
            2,
        )
        self.assertEqual(self.store.read_outbox_ambiguities(), ())

    async def test_expired_lease_is_unconfirmed_and_never_blindly_acked(self):
        self.enqueue("message-expired-lease")
        requests = []

        async def publish(request):
            requests.append(request)
            self.clock.advance(2)
            return PublishReceipt.accepted("receipt:expired")

        publisher = self.publisher(
            publish,
            lease_seconds=1,
            publish_timeout=0.25,
        )
        batch = await publisher.run_once()
        stored = self.store.get_outbox("message-expired-lease")

        self.assertEqual(requests[0].lease_deadline, "2026-08-20T00:00:01Z")
        self.assertEqual(batch.published, 0)
        self.assertEqual(batch.accepted_unconfirmed, 1)
        self.assertEqual(batch.lease_expired, 1)
        self.assertEqual(batch.ack_failed, 0)
        self.assertEqual(stored.status, OutboxStatus.IN_FLIGHT)

    async def test_connector_request_never_exposes_internal_fencing_token(self):
        self.enqueue("message-no-fencing-token")
        observed = {}

        async def publish(request):
            observed.update(request.to_dict())
            self.assertFalse(hasattr(request, "lease_token"))
            return PublishReceipt.accepted("receipt:no-fencing-token")

        publisher = self.publisher(publish)
        batch = await publisher.run_once()

        self.assertEqual(batch.published, 1)
        self.assertNotIn("leaseToken", observed)
        self.assertNotIn("lease_token", observed)
        self.assertIsNone(self.store.get_outbox("message-no-fencing-token").lease_token)

    async def test_claim_that_is_already_expired_never_invokes_connector(self):
        self.enqueue("message-expired-at-admission")
        calls = 0

        class ExpiringClaimStore:
            def claim_outbox(inner_self, *args, **kwargs):
                claimed = self.store.claim_outbox(*args, **kwargs)
                self.clock.advance(2)
                return claimed

            def __getattr__(inner_self, name):
                return getattr(self.store, name)

        async def publish(_request):
            nonlocal calls
            calls += 1
            return PublishReceipt.accepted("receipt:must-not-run")

        publisher = self.publisher(
            publish,
            store=ExpiringClaimStore(),
            lease_seconds=1,
            publish_timeout=0.25,
        )
        batch = await publisher.run_once()

        self.assertEqual(calls, 0)
        self.assertEqual(batch.lease_budget_rejected, 1)
        self.assertEqual(batch.lease_conflicts, 1)
        self.assertEqual(
            self.store.get_outbox("message-expired-at-admission").status,
            OutboxStatus.IN_FLIGHT,
        )

    async def test_connector_budget_must_fit_inside_remaining_lease(self):
        self.enqueue("message-short-budget")
        calls = 0

        class ShortBudgetStore:
            def claim_outbox(inner_self, *args, **kwargs):
                claimed = self.store.claim_outbox(*args, **kwargs)
                return tuple(
                    replace(
                        item,
                        lease_expires_at="2026-08-20T00:00:00.100000Z",
                    )
                    for item in claimed
                )

            def __getattr__(inner_self, name):
                return getattr(self.store, name)

        async def publish(_request):
            nonlocal calls
            calls += 1
            return PublishReceipt.accepted("receipt:must-not-run")

        publisher = self.publisher(
            publish,
            store=ShortBudgetStore(),
            publish_timeout=0.25,
        )
        batch = await publisher.run_once()

        self.assertEqual(calls, 0)
        self.assertEqual(batch.lease_budget_rejected, 1)
        self.assertEqual(batch.retried, 1)

    async def test_expired_nack_loses_atomic_store_cas(self):
        self.enqueue("message-expired-nack")

        async def fail_after_expiry(_request):
            self.clock.advance(2)
            raise ConnectionError("offline")

        publisher = self.publisher(
            fail_after_expiry,
            lease_seconds=1,
            publish_timeout=0.25,
        )
        batch = await publisher.run_once()

        self.assertEqual(batch.publish_failures, 1)
        self.assertEqual(batch.retried, 0)
        self.assertEqual(batch.lease_conflicts, 1)
        self.assertEqual(
            self.store.get_outbox("message-expired-nack").status,
            OutboxStatus.IN_FLIGHT,
        )

    async def test_failed_ack_is_separate_from_callback_failure(self):
        self.enqueue("message-ack-failed")

        async def publish(_request):
            return PublishReceipt.accepted("receipt:ack-failed")

        original_ack = self.store.acknowledge_outbox
        self.store.acknowledge_outbox = lambda *_args, **_kwargs: False
        try:
            publisher = self.publisher(publish)
            batch = await publisher.run_once()
        finally:
            self.store.acknowledge_outbox = original_ack

        self.assertEqual(batch.publish_failures, 0)
        self.assertEqual(batch.accepted_unconfirmed, 1)
        self.assertEqual(batch.ack_failed, 1)
        self.assertEqual(batch.lease_conflicts, 1)
        self.assertEqual(
            self.store.get_outbox("message-ack-failed").status,
            OutboxStatus.IN_FLIGHT,
        )

    async def test_ack_exception_is_ambiguous_and_durably_quarantined(self):
        self.enqueue("message-ack-exception")
        canary = "ack-database-secret-canary"

        async def publish(_request):
            return PublishReceipt.accepted("receipt:ack-exception")

        original_ack = self.store.acknowledge_outbox

        def fail_ack(*_args, **_kwargs):
            raise RuntimeError(canary)

        self.store.acknowledge_outbox = fail_ack
        try:
            publisher = self.publisher(publish)
            with self.assertLogs("quantum_entanglement.publisher", level="ERROR") as captured:
                batch = await publisher.run_once()
        finally:
            self.store.acknowledge_outbox = original_ack

        self.assertEqual(batch.ack_failed, 1)
        self.assertEqual(batch.store_errors, 1)
        self.assertEqual(batch.accepted_unconfirmed, 1)
        self.assertEqual(batch.ambiguity_persisted, 1)
        self.assertEqual(len(self.store.read_outbox_ambiguities()), 1)
        rendered = "\n".join(captured.output)
        self.assertIn("qe.publisher.ack_failed", rendered)
        self.assertNotIn(canary, rendered)
        self.assertNotIn("message-ack-exception", rendered)
        self.assertNotIn("Traceback", rendered)

    async def test_nack_exception_is_reported_without_ambiguous_external_success(self):
        self.enqueue("message-nack-exception")

        async def fail(_request):
            raise ConnectionError("broker unavailable")

        original_reject = self.store.reject_outbox

        def fail_nack(*_args, **_kwargs):
            raise RuntimeError("database write failed")

        self.store.reject_outbox = fail_nack
        try:
            publisher = self.publisher(fail)
            with self.assertLogs("quantum_entanglement.publisher", level="ERROR"):
                batch = await publisher.run_once()
        finally:
            self.store.reject_outbox = original_reject

        self.assertEqual(batch.publish_failures, 1)
        self.assertEqual(batch.store_errors, 1)
        self.assertEqual(batch.accepted_unconfirmed, 0)
        self.assertEqual(self.store.read_outbox_ambiguities(), ())

    async def test_false_connector_result_is_never_acknowledged(self):
        self.enqueue("message-false-result")

        async def publish(_request):
            return False

        publisher = self.publisher(publish, max_attempts=1)
        batch = await publisher.run_once()
        stored = self.store.get_outbox("message-false-result")

        self.assertEqual(batch.published, 0)
        self.assertEqual(batch.dead_lettered, 1)
        self.assertEqual(stored.status, OutboxStatus.DEAD_LETTER)
        self.assertEqual(stored.last_error, "invalid_publish_receipt")

    async def test_stringly_typed_receipt_result_is_never_acknowledged(self):
        self.enqueue("message-string-result")

        async def publish(_request):
            return PublishReceipt("accepted", receipt_id="receipt:not-enough")

        publisher = self.publisher(publish, max_attempts=1)
        batch = await publisher.run_once()
        stored = self.store.get_outbox("message-string-result")

        self.assertEqual(batch.published, 0)
        self.assertEqual(batch.dead_lettered, 1)
        self.assertEqual(stored.status, OutboxStatus.DEAD_LETTER)
        self.assertEqual(stored.last_error, "connector_input_rejected")

    async def test_forged_or_mutated_receipts_fail_closed_at_return_boundary(self):
        self.enqueue("message-forged-receipt")
        self.enqueue("message-mutated-receipt")

        forged = object.__new__(PublishReceipt)
        object.__setattr__(forged, "result", "accepted")
        object.__setattr__(forged, "receipt_id", "receipt:forged")
        object.__setattr__(forged, "reason_code", None)
        mutated = object.__new__(PublishReceipt)
        object.__setattr__(mutated, "result", PublishResult.ACCEPTED)
        object.__setattr__(mutated, "receipt_id", "receipt:mutated")
        object.__setattr__(mutated, "reason_code", "unexpected-field")

        async def publish(request):
            if request.message_id == "message-forged-receipt":
                return forged
            return mutated

        publisher = self.publisher(publish, max_attempts=1)
        batch = await publisher.run_once()

        self.assertEqual(batch.published, 0)
        self.assertEqual(batch.dead_lettered, 2)
        for message_id in ("message-forged-receipt", "message-mutated-receipt"):
            stored = self.store.get_outbox(message_id)
            self.assertEqual(stored.status, OutboxStatus.DEAD_LETTER)
            self.assertEqual(stored.last_error, "invalid_publish_receipt")

    async def test_connector_thread_start_failure_releases_admission_capacity(self):
        self.enqueue("message-thread-start-failed")

        async def publish(_request):
            return PublishReceipt.accepted("receipt:must-not-run")

        original_start = threading.Thread.start

        def fail_connector_start(thread):
            if thread.name.startswith("outbox-connector:"):
                raise RuntimeError("connector thread capacity exhausted")
            return original_start(thread)

        publisher = self.publisher(publish, max_attempts=1, max_callback_tasks=1)
        with mock.patch.object(threading.Thread, "start", fail_connector_start):
            batch = await publisher.run_once()
        await asyncio.sleep(0)
        stored = self.store.get_outbox("message-thread-start-failed")

        self.assertEqual(batch.published, 0)
        self.assertEqual(batch.dead_lettered, 1)
        self.assertEqual(stored.status, OutboxStatus.DEAD_LETTER)
        self.assertEqual(publisher.stats.active_callbacks, 0)
        self.assertEqual(publisher.abandoned, ())

    async def test_default_error_classification_never_persists_exception_canary(self):
        self.enqueue("message-canary")
        canary = "CANARY_MUST_NOT_BE_PERSISTED"

        async def fail(_request):
            raise RuntimeError(canary)

        publisher = self.publisher(fail, max_attempts=1)
        await publisher.run_once()
        stored = self.store.get_outbox("message-canary")

        self.assertEqual(stored.last_error, "connector_failure")
        self.assertNotIn(canary, stored.last_error)

    async def test_custom_error_classifier_must_return_allowlisted_fixed_code(self):
        self.enqueue("message-classifier-rejected")
        canary = "connector-secret-canary"

        async def fail(_request):
            raise RuntimeError("exception-message-secret")

        publisher = self.publisher(
            fail,
            max_attempts=1,
            error_formatter=lambda _error: canary,
        )
        with self.assertLogs("quantum_entanglement.publisher", level="ERROR") as captured:
            await publisher.run_once()

        stored = self.store.get_outbox("message-classifier-rejected")
        rendered = "\n".join(captured.output)
        self.assertEqual(stored.last_error, "connector_failure")
        self.assertIn("qe.publisher.error_classifier_rejected", rendered)
        self.assertNotIn(canary, rendered)
        self.assertNotIn("exception-message-secret", rendered)

    async def test_custom_error_classifier_accepts_only_explicit_code(self):
        self.enqueue("message-classifier-allowed")

        async def fail(_request):
            raise RuntimeError("not-persisted")

        publisher = self.publisher(
            fail,
            max_attempts=1,
            error_formatter=lambda _error: "provider_quota",
            error_code_allowlist=("provider_quota",),
        )
        await publisher.run_once()

        self.assertEqual(
            self.store.get_outbox("message-classifier-allowed").last_error,
            "provider_quota",
        )

    async def test_error_classifier_result_is_never_coerced_or_rendered(self):
        self.enqueue("message-classifier-object")

        class ExplosiveCode:
            def __str__(self):
                raise AssertionError("classifier result must not be stringified")

            def __repr__(self):
                raise AssertionError("classifier result must not be rendered")

        async def fail(_request):
            raise RuntimeError("not-persisted")

        publisher = self.publisher(
            fail,
            max_attempts=1,
            error_formatter=lambda _error: ExplosiveCode(),
        )
        with self.assertLogs("quantum_entanglement.publisher", level="ERROR"):
            await publisher.run_once()

        self.assertEqual(
            self.store.get_outbox("message-classifier-object").last_error,
            "connector_failure",
        )

    async def test_cancellation_style_classifier_failure_cannot_strand_lease(self):
        self.enqueue("message-classifier-cancelled")

        async def fail(_request):
            raise RuntimeError("connector-secret-canary")

        def cancelled_classifier(_error):
            raise asyncio.CancelledError("classifier-secret-canary")

        publisher = self.publisher(
            fail,
            max_attempts=1,
            error_formatter=cancelled_classifier,
        )
        with self.assertLogs("quantum_entanglement.publisher", level="ERROR") as captured:
            batch = await publisher.run_once()

        stored = self.store.get_outbox("message-classifier-cancelled")
        rendered = "\n".join(captured.output)
        self.assertEqual(batch.dead_lettered, 1)
        self.assertEqual(stored.status, OutboxStatus.DEAD_LETTER)
        self.assertEqual(stored.last_error, "connector_failure")
        self.assertIn("qe.publisher.error_classifier_failed", rendered)
        self.assertNotIn("connector-secret-canary", rendered)
        self.assertNotIn("classifier-secret-canary", rendered)

    def test_error_code_allowlist_is_bounded_and_canonical(self):
        async def publish(_request):
            return PublishReceipt.accepted("receipt:unused")

        for invalid in (("Bad-Code",), ("bad\ncode",), ("duplicate", "duplicate")):
            with self.subTest(invalid=invalid), self.assertRaises((TypeError, ValueError)):
                self.publisher(publish, error_code_allowlist=invalid)

    async def test_stop_is_graceful_and_does_not_claim_another_batch(self):
        self.enqueue("message-in-flight")
        self.enqueue("message-remains")
        started = threading.Event()
        release = threading.Event()

        async def publish(_request):
            started.set()
            await wait_thread_signal(release)
            return PublishReceipt.accepted("receipt:graceful")

        publisher = self.publisher(publish, batch_size=1)
        service_task = publisher.start()
        await self.wait_thread_event(started)
        stopping = asyncio.create_task(publisher.stop())
        await asyncio.sleep(0)
        self.assertFalse(stopping.done())

        release.set()
        stopped_stats = await asyncio.wait_for(stopping, timeout=0.5)
        await service_task

        self.assertFalse(stopped_stats.running)
        self.assertEqual(stopped_stats.claimed, 1)
        self.assertEqual(stopped_stats.published, 1)
        self.assertEqual(
            self.store.get_outbox("message-in-flight").status,
            OutboxStatus.PUBLISHED,
        )
        self.assertEqual(
            self.store.get_outbox("message-remains").status,
            OutboxStatus.PENDING,
        )

    async def test_stop_drains_public_cycles_accepted_while_queued(self):
        self.enqueue("message-cycle-1")
        self.enqueue("message-cycle-2")
        first_started = threading.Event()
        second_started = threading.Event()
        release_first = threading.Event()
        release_second = threading.Event()

        async def publish(request):
            if request.message_id == "message-cycle-1":
                first_started.set()
                await wait_thread_signal(release_first)
            else:
                second_started.set()
                await wait_thread_signal(release_second)
            return PublishReceipt.accepted(f"receipt:{request.message_id}")

        publisher = self.publisher(publish, batch_size=1)
        first_cycle = asyncio.create_task(publisher.run_once())
        await self.wait_thread_event(first_started)
        second_cycle = asyncio.create_task(publisher.run_once())
        await self.wait_until(lambda: publisher.stats.active_cycles == 2)

        stopping = asyncio.create_task(publisher.stop())
        await asyncio.sleep(0)
        with self.assertRaises(PublisherClosedError):
            await publisher.run_once()
        self.assertFalse(stopping.done())

        release_first.set()
        await self.wait_thread_event(second_started)
        self.assertFalse(stopping.done())
        release_second.set()

        first_batch, second_batch = await asyncio.gather(first_cycle, second_cycle)
        stopped = await asyncio.wait_for(stopping, timeout=0.5)
        self.assertEqual(first_batch.published, 1)
        self.assertEqual(second_batch.published, 1)
        self.assertEqual(stopped.cycles, 2)
        self.assertEqual(stopped.lifecycle_state, "closed")
        self.assertTrue(stopped.shutdown_clean)

    async def test_immediate_stop_after_start_closes_cleanly(self):
        async def publish(_request):
            return PublishReceipt.accepted("receipt:unused")

        publisher = self.publisher(publish)
        service = publisher.start()
        stopped = await asyncio.wait_for(publisher.stop(), timeout=0.2)
        service_result = await service

        self.assertTrue(stopped.shutdown_clean)
        self.assertEqual(stopped.lifecycle_state, "closed")
        self.assertEqual(service_result.lifecycle_state, "closed")

    async def test_structured_statistics_include_empty_polls_and_failures(self):
        calls = 0

        async def publish(request: PublishRequest):
            nonlocal calls
            calls += 1
            raise ValueError(f"invalid payload for {request.message_id}")

        publisher = self.publisher(publish, max_attempts=1)
        empty = await publisher.run_once()
        self.enqueue("message-stats")
        failed = await publisher.run_once()
        stats = publisher.stats

        self.assertEqual(empty.claimed, 0)
        self.assertEqual(failed.dead_lettered, 1)
        self.assertEqual(calls, 1)
        self.assertEqual(stats.cycles, 2)
        self.assertEqual(stats.empty_polls, 1)
        self.assertEqual(stats.claimed, 1)
        self.assertEqual(stats.publish_failures, 1)
        self.assertEqual(stats.dead_lettered, 1)
        self.assertEqual(stats.to_dict()["workerId"], "publisher-1")

    async def test_all_sqlite_calls_are_offloaded_from_the_event_loop(self):
        self.enqueue("message-thread-ack")
        self.enqueue("message-thread-reject")
        recording = RecordingStore(self.store)
        loop_thread = threading.get_ident()

        async def publish(request):
            if request.message_id == "message-thread-reject":
                raise ConnectionError("offline")
            return PublishReceipt.accepted("receipt:thread-ack")

        publisher = self.publisher(publish, store=recording)
        batch = await publisher.run_once()

        self.assertEqual(batch.published, 1)
        self.assertEqual(batch.retried, 1)
        self.assertEqual({name for name, _thread in recording.calls}, {"claim", "ack", "reject"})
        self.assertTrue(all(thread != loop_thread for _name, thread in recording.calls))

    async def test_repeated_cancel_tracks_database_task_until_it_really_finishes(self):
        self.enqueue("message-slow-claim")
        entered = threading.Event()
        release = threading.Event()

        class SlowClaimStore:
            def claim_outbox(inner_self, *args, **kwargs):
                entered.set()
                release.wait()
                return self.store.claim_outbox(*args, **kwargs)

            def __getattr__(inner_self, name):
                return getattr(self.store, name)

        async def publish(_request):
            return PublishReceipt.accepted("receipt:must-not-run")

        publisher = self.publisher(publish, store=SlowClaimStore())
        cycle = asyncio.create_task(publisher.run_once())
        await self.wait_thread_event(entered)
        cycle.cancel()
        cycle.cancel()
        stopping = asyncio.create_task(publisher.stop())
        await asyncio.sleep(0.01)

        self.assertFalse(stopping.done())
        self.assertEqual(publisher.stats.active_db_tasks, 1)
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await cycle
        stopped = await asyncio.wait_for(stopping, timeout=0.5)

        self.assertEqual(stopped.active_db_tasks, 0)
        self.assertEqual(stopped.cancelled_cycles, 1)
        self.assertFalse(stopped.shutdown_clean)

    async def test_jitter_is_bounded_and_exception_falls_back_to_capped_delay(self):
        self.enqueue("message-jitter-large")
        self.enqueue("message-jitter-error")
        calls = 0

        async def fail(_request):
            raise OSError("offline")

        def adversarial_jitter(_delay):
            nonlocal calls
            calls += 1
            if calls == 1:
                return 10_000
            raise RuntimeError("jitter-secret-canary")

        publisher = self.publisher(
            fail,
            base_retry_delay=8,
            max_retry_delay=60,
            jitter=adversarial_jitter,
        )
        with self.assertLogs("quantum_entanglement.publisher", level="ERROR") as captured:
            batch = await publisher.run_once()

        self.assertEqual(batch.retried, 2)
        self.assertEqual(
            self.store.get_outbox("message-jitter-large").message.available_at,
            "2026-08-20T00:00:08Z",
        )
        self.assertEqual(
            self.store.get_outbox("message-jitter-error").message.available_at,
            "2026-08-20T00:00:08Z",
        )
        self.assertNotIn("jitter-secret-canary", "\n".join(captured.output))

    async def test_retry_clock_exception_does_not_strand_the_lease(self):
        self.enqueue("message-clock-error")

        class FailingRetryClock:
            def __init__(self):
                self.calls = 0

            def __call__(self):
                self.calls += 1
                if self.calls == 2:
                    raise RuntimeError("clock failed")
                return READY

        async def fail(_request):
            raise ConnectionError("offline")

        publisher = self.publisher(fail, clock=FailingRetryClock())
        with self.assertLogs("quantum_entanglement.publisher", level="ERROR"):
            batch = await publisher.run_once()

        self.assertEqual(batch.retried, 1)
        self.assertEqual(
            self.store.get_outbox("message-clock-error").status,
            OutboxStatus.PENDING,
        )

    async def test_sync_callback_is_rejected_at_construction(self):
        def publish(_request):
            return None

        with self.assertRaisesRegex(TypeError, "native async callable"):
            self.publisher(publish)

    async def test_store_claim_error_is_contained_and_reported(self):
        async def publish(_request):
            self.fail("callback must not run when claiming fails")

        self.store.close()
        publisher = self.publisher(publish)
        with self.assertLogs("quantum_entanglement.publisher", level="ERROR") as captured:
            batch = await publisher.run_once()

        self.assertEqual(batch.claimed, 0)
        self.assertEqual(batch.store_errors, 1)
        self.assertEqual(publisher.stats.store_errors, 1)
        rendered = "\n".join(captured.output)
        self.assertIn("qe.publisher.claim_failed", rendered)
        self.assertNotIn("publisher-1", rendered)

    def test_rejects_a_lease_shorter_than_callback_timeout(self):
        async def publish(_request):
            return PublishReceipt.accepted("receipt:unused")

        with self.assertRaisesRegex(ValueError, "greater than publish_timeout"):
            self.publisher(publish, lease_seconds=0.1, publish_timeout=0.1)


class SynchronousConstructionTests(unittest.TestCase):
    def test_construct_without_loop_then_run_with_asyncio_run(self):
        with tempfile.TemporaryDirectory() as tempdir:
            timestamp = READY.isoformat().replace("+00:00", "Z")
            store = SQLiteEventStore(
                str(Path(tempdir) / "sync-construction.sqlite3"),
                clock=lambda: timestamp,
            )
            event = DomainEvent(
                "session:sync-construction",
                "message.queued",
                {"messageId": "message-sync-construction"},
                "publisher-test",
            )
            outgoing = OutboxMessage(
                "test-broker",
                {"message": "message-sync-construction"},
                message_id="message-sync-construction",
                available_at=timestamp,
                created_at=timestamp,
            )
            store.append_with_outbox(event, (outgoing,))

            async def publish(_request):
                return PublishReceipt.accepted("receipt:sync-construction")

            # This constructor executes with no running event loop on Python 3.9.
            publisher = OutboxPublisher(
                store,
                publish,
                worker_id="sync-construction",
                lease_seconds=1,
                publish_timeout=0.25,
                clock=lambda: READY,
                jitter=lambda delay: delay,
            )

            async def scenario():
                batch = await publisher.run_once()
                stopped = await publisher.stop()
                return batch, stopped

            try:
                batch, stopped = asyncio.run(scenario())
            finally:
                store.close()

            self.assertEqual(batch.published, 1)
            self.assertTrue(stopped.shutdown_clean)


if __name__ == "__main__":
    unittest.main()
