from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from quantum_entanglement.native_im_auth import NativeIMRawVerificationResultV1
from quantum_entanglement.native_im_inbox import (
    NativeIMInboundCommitAmbiguityError,
    NativeIMInboundConflictError,
    NativeIMInboundPageAdmissionResultV1,
    NativeIMInboundTransactionError,
)
from quantum_entanglement.native_im_nonce_store import (
    NativeIMInboxStoreIntegrityError,
    NativeIMNonceStorePoisonedError,
    SQLiteNativeIMInboxStore,
)
from tests.test_native_im_contract import (
    capability,
    inbound_event,
    inbound_page,
    inbound_read_request,
    verified_envelope,
)

PREPARED_AT = "2026-08-28T01:02:03.000000Z"
ADMITTED_AT = "2026-08-28T01:02:04.000000Z"
EXPIRES_AT = "2026-08-28T00:05:00.000001Z"
PROFILE_REVISION = "profile-revision-atomic-page-1"
PROFILE_DIGEST = "1" * 64
TAMPER_DIGEST = "0" * 64
OBSERVATION_TABLES = (
    "native_im_auth_nonces",
    "native_im_inbox_events",
    "native_im_inbox_verifications",
    "native_im_inbound_read_events",
    "native_im_inbound_checkpoints",
)


class MutableClock:
    def __init__(self, value: object = PREPARED_AT) -> None:
        self.value = value
        self.calls = 0

    def __call__(self) -> object:
        self.calls += 1
        return self.value


def raw_verification(**changes: object) -> NativeIMRawVerificationResultV1:
    values: dict[str, object] = {
        "schema_version": 1,
        "verifier_id": "test-verifier-1",
        "key_id": "test-key-1",
        "signed_at": "2026-08-28T00:00:00.000001Z",
        "expires_at": EXPIRES_AT,
        "verified_at": "2026-08-28T00:00:00.000001Z",
        "body_digest": "d" * 64,
        "nonce_digest": "e" * 64,
        "authentication_evidence_digest": "c" * 64,
    }
    values.update(changes)
    return NativeIMRawVerificationResultV1(**values)  # type: ignore[arg-type]


class SQLiteNativeIMPageAdmissionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tempdir.name) / "native-im-page.sqlite3")
        self.clock = MutableClock()
        self.store = self._open()

    def _open(self) -> SQLiteNativeIMInboxStore:
        return SQLiteNativeIMInboxStore(
            self.path,
            profile_revision=PROFILE_REVISION,
            profile_digest=PROFILE_DIGEST,
            clock=self.clock,
        )

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def _rows(self, table: str) -> tuple[sqlite3.Row, ...]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            return tuple(connection.execute(f"SELECT * FROM {table}").fetchall())
        finally:
            connection.close()

    def _execute_mutations(
        self,
        *mutations: tuple[str, tuple[object, ...]],
    ) -> None:
        connection = sqlite3.connect(self.path)
        try:
            for statement, parameters in mutations:
                cursor = connection.execute(statement, parameters)
                self.assertEqual(cursor.rowcount, 1)
            connection.commit()
        finally:
            connection.close()

    def _admit(self, request=None, snapshot=None, page=None, verification=None, store=None):
        request = request or inbound_read_request()
        snapshot = snapshot or capability()
        page = page or inbound_page(request=request, capability=snapshot)
        return (store or self.store).admit_native_im_inbound_page(
            request,
            snapshot,
            page,
            verification or raw_verification(),
        )

    def _assert_no_admitted_observation_rows(self) -> None:
        self.assertEqual(
            {table: len(self._rows(table)) for table in OBSERVATION_TABLES},
            {table: 0 for table in OBSERVATION_TABLES},
        )
        reads = self._rows("native_im_inbound_reads")
        self.assertEqual(len(reads), 1)
        self.assertEqual(reads[0]["status"], "prepared")

    def _assert_exact_replay_rejects_tamper(
        self,
        *mutations: tuple[str, tuple[object, ...]],
    ) -> None:
        request = inbound_read_request()
        snapshot = capability()
        page = inbound_page(request=request, capability=snapshot)
        verification = raw_verification()
        self.store.prepare_native_im_inbound_read(request)
        self.clock.value = ADMITTED_AT
        self._admit(request, snapshot, page, verification)
        self._execute_mutations(*mutations)

        with self.assertRaises(NativeIMInboxStoreIntegrityError) as raised:
            self._admit(request, snapshot, page, verification)

        self.assertIs(type(raised.exception), NativeIMInboxStoreIntegrityError)

    def test_fresh_page_atomically_claims_nonce_events_read_and_checkpoint(self) -> None:
        request = inbound_read_request()
        snapshot = capability()
        page = inbound_page(request=request, capability=snapshot)
        self.store.prepare_native_im_inbound_read(request)
        self.clock.value = ADMITTED_AT
        self.clock.calls = 0

        result = self.store.admit_native_im_inbound_page(
            request,
            snapshot,
            page,
            raw_verification(),
        )

        self.assertIs(type(result), NativeIMInboundPageAdmissionResultV1)
        self.assertEqual(result.disposition, "fresh_observation")
        self.assertEqual(result.read_request_digest, request.canonical_digest())
        self.assertEqual(result.page_digest, page.canonical_digest())
        self.assertEqual(result.admitted_at, ADMITTED_AT)
        self.assertEqual(result.checkpoint.checkpoint_revision, 1)
        self.assertEqual(result.checkpoint.after_cursor, page.next_cursor)
        self.assertEqual(result.checkpoint.after_sequence, page.next_sequence)
        self.assertEqual(result.checkpoint.continuation_snapshot_token, page.snapshot_token)
        self.assertEqual(len(result.event_receipts), 1)
        self.assertEqual(result.event_receipts[0].event_id, page.envelopes[0].event.event_id)
        self.assertEqual(result.event_receipts[0].admitted_at, ADMITTED_AT)
        self.assertEqual(self.clock.calls, 2)

        expected_counts = {
            "native_im_auth_nonces": 1,
            "native_im_inbox_events": 1,
            "native_im_inbox_verifications": 1,
            "native_im_inbound_reads": 1,
            "native_im_inbound_read_events": 1,
            "native_im_inbound_checkpoints": 1,
        }
        self.assertEqual(
            {table: len(self._rows(table)) for table in expected_counts},
            expected_counts,
        )
        read = self._rows("native_im_inbound_reads")[0]
        self.assertEqual(read["status"], "admitted")
        self.assertEqual(read["page_digest"], page.canonical_digest())
        self.assertEqual(read["admitted_checkpoint_revision"], 1)
        self.assertEqual(read["admitted_at"], ADMITTED_AT)

    def test_exact_page_and_nonce_replay_returns_original_graph_without_clock(self) -> None:
        request = inbound_read_request()
        snapshot = capability()
        page = inbound_page(request=request, capability=snapshot)
        verification = raw_verification()
        self.store.prepare_native_im_inbound_read(request)
        self.clock.value = ADMITTED_AT
        fresh = self.store.admit_native_im_inbound_page(
            request,
            snapshot,
            page,
            verification,
        )
        before = {
            table: tuple(dict(row) for row in self._rows(table))
            for table in (
                "native_im_auth_nonces",
                "native_im_inbox_events",
                "native_im_inbox_verifications",
                "native_im_inbound_reads",
                "native_im_inbound_read_events",
                "native_im_inbound_checkpoints",
            )
        }
        self.clock.value = True
        self.clock.calls = 0

        replay = self.store.admit_native_im_inbound_page(
            request,
            snapshot,
            page,
            verification,
        )

        self.assertEqual(replay.disposition, "observed_replay")
        self.assertEqual(replay.checkpoint, fresh.checkpoint)
        self.assertEqual(replay.event_receipts, fresh.event_receipts)
        self.assertEqual(replay.admitted_at, fresh.admitted_at)
        self.assertEqual(self.clock.calls, 0)
        self.assertEqual(
            {table: tuple(dict(row) for row in self._rows(table)) for table in before},
            before,
        )

    def test_empty_terminal_page_advances_checkpoint_without_event_rows(self) -> None:
        request = inbound_read_request()
        snapshot = capability()
        page = inbound_page(
            request=request,
            capability=snapshot,
            envelopes=(),
        )
        verification = raw_verification()
        self.store.prepare_native_im_inbound_read(request)
        self.clock.value = ADMITTED_AT

        fresh = self._admit(request, snapshot, page, verification)

        self.assertEqual(fresh.disposition, "fresh_observation")
        self.assertEqual(fresh.event_receipts, ())
        self.assertEqual(fresh.checkpoint.checkpoint_revision, 1)
        self.assertIsNone(fresh.checkpoint.after_cursor)
        self.assertIsNone(fresh.checkpoint.after_sequence)
        self.assertIsNone(fresh.checkpoint.continuation_snapshot_token)
        self.assertEqual(self._rows("native_im_inbox_events"), ())
        self.assertEqual(self._rows("native_im_inbox_verifications"), ())
        self.assertEqual(self._rows("native_im_inbound_read_events"), ())
        self.assertEqual(len(self._rows("native_im_auth_nonces")), 1)
        self.assertEqual(len(self._rows("native_im_inbound_checkpoints")), 1)

        self.clock.value = True
        self.clock.calls = 0
        replay = self._admit(request, snapshot, page, verification)
        self.assertEqual(replay.disposition, "observed_replay")
        self.assertEqual(replay.checkpoint, fresh.checkpoint)
        self.assertEqual(replay.event_receipts, ())
        self.assertEqual(self.clock.calls, 0)

    def test_body_sqlite_failure_rolls_back_nonce_page_events_read_and_checkpoint(self) -> None:
        request = inbound_read_request()
        snapshot = capability()
        page = inbound_page(request=request, capability=snapshot)
        self.store.prepare_native_im_inbound_read(request)
        self.clock.value = ADMITTED_AT

        with patch.object(
            self.store,
            "_advance_inbound_checkpoint",
            side_effect=sqlite3.OperationalError("page-body-secret-canary"),
        ):
            with self.assertRaises(NativeIMInboundTransactionError) as raised:
                self._admit(request, snapshot, page)

        self.assertIs(type(raised.exception), NativeIMInboundTransactionError)
        self.assertNotIn("page-body-secret-canary", repr(raised.exception))
        self._assert_no_admitted_observation_rows()

    def test_body_integrity_failure_rolls_back_every_atomic_admission_row(self) -> None:
        request = inbound_read_request()
        snapshot = capability()
        page = inbound_page(request=request, capability=snapshot)
        self.store.prepare_native_im_inbound_read(request)
        self.clock.value = ADMITTED_AT

        with patch.object(
            self.store,
            "_advance_inbound_checkpoint",
            side_effect=sqlite3.IntegrityError("page-integrity-secret-canary"),
        ):
            with self.assertRaises(NativeIMInboxStoreIntegrityError) as raised:
                self._admit(request, snapshot, page)

        self.assertIs(type(raised.exception), NativeIMInboxStoreIntegrityError)
        self.assertNotIn("page-integrity-secret-canary", repr(raised.exception))
        self._assert_no_admitted_observation_rows()

    def test_commit_rejection_is_confirmed_noncommit_and_store_remains_usable(self) -> None:
        request = inbound_read_request()
        snapshot = capability()
        page = inbound_page(request=request, capability=snapshot)
        verification = raw_verification()
        self.store.prepare_native_im_inbound_read(request)
        self.clock.value = ADMITTED_AT

        with patch.object(
            self.store,
            "_commit_write_transaction",
            side_effect=sqlite3.OperationalError("page-commit-secret-canary"),
        ):
            with self.assertRaises(NativeIMInboundTransactionError) as raised:
                self._admit(request, snapshot, page, verification)

        self.assertIs(type(raised.exception), NativeIMInboundTransactionError)
        self._assert_no_admitted_observation_rows()
        admitted = self._admit(request, snapshot, page, verification)
        self.assertEqual(admitted.disposition, "fresh_observation")

    def test_commit_ack_loss_poison_reopen_and_exact_reconciliation(self) -> None:
        request = inbound_read_request()
        snapshot = capability()
        page = inbound_page(request=request, capability=snapshot)
        verification = raw_verification()
        self.store.prepare_native_im_inbound_read(request)
        self.clock.value = ADMITTED_AT
        real_commit = self.store._commit_write_transaction

        def commit_then_raise(connection):
            real_commit(connection)
            raise RuntimeError("page-commit-ack-secret-canary")

        with patch.object(
            self.store,
            "_commit_write_transaction",
            side_effect=commit_then_raise,
        ):
            with self.assertRaises(NativeIMInboundCommitAmbiguityError) as raised:
                self._admit(request, snapshot, page, verification)

        self.assertIs(type(raised.exception), NativeIMInboundCommitAmbiguityError)
        self.assertNotIn("page-commit-ack-secret-canary", repr(raised.exception))
        with self.assertRaises(NativeIMNonceStorePoisonedError):
            self._admit(request, snapshot, page, verification)

        self.store.close()
        self.store = self._open()
        self.clock.value = True
        self.clock.calls = 0
        replay = self._admit(request, snapshot, page, verification)
        self.assertEqual(replay.disposition, "observed_replay")
        self.assertEqual(replay.admitted_at, ADMITTED_AT)
        self.assertEqual(self.clock.calls, 0)

    def test_rollback_failure_is_ambiguous_poisoned_and_recoverable_after_reopen(self) -> None:
        request = inbound_read_request()
        snapshot = capability()
        page = inbound_page(request=request, capability=snapshot)
        verification = raw_verification()
        self.store.prepare_native_im_inbound_read(request)
        self.clock.value = ADMITTED_AT

        with (
            patch.object(
                self.store,
                "_advance_inbound_checkpoint",
                side_effect=sqlite3.OperationalError("page-body-rollback-canary"),
            ),
            patch.object(
                self.store,
                "_rollback_write_transaction",
                side_effect=RuntimeError("page-rollback-failure-canary"),
            ),
        ):
            with self.assertRaises(NativeIMInboundCommitAmbiguityError) as raised:
                self._admit(request, snapshot, page, verification)

        self.assertIs(type(raised.exception), NativeIMInboundCommitAmbiguityError)
        self.assertNotIn("page-body-rollback-canary", repr(raised.exception))
        self.assertNotIn("page-rollback-failure-canary", repr(raised.exception))
        with self.assertRaises(NativeIMNonceStorePoisonedError) as poisoned:
            self._admit(request, snapshot, page, verification)
        self.assertIs(type(poisoned.exception), NativeIMNonceStorePoisonedError)

        self.store.close()
        self.store = self._open()
        self._assert_no_admitted_observation_rows()
        admitted = self._admit(request, snapshot, page, verification)
        self.assertEqual(admitted.disposition, "fresh_observation")

    def test_waiter_blocked_behind_ambiguous_admission_rechecks_poison(self) -> None:
        request = inbound_read_request()
        snapshot = capability()
        page = inbound_page(request=request, capability=snapshot)
        verification = raw_verification()
        self.store.prepare_native_im_inbound_read(request)
        self.clock.value = ADMITTED_AT
        real_commit = self.store._commit_write_transaction
        commit_entered = threading.Event()
        release_commit = threading.Event()
        waiter_started = threading.Event()

        def commit_then_raise(connection):
            commit_entered.set()
            self.assertTrue(release_commit.wait(timeout=10))
            real_commit(connection)
            raise RuntimeError("page-blocked-waiter-canary")

        def first_admission():
            try:
                self._admit(request, snapshot, page, verification)
            except BaseException as error:
                return type(error)
            return "accepted"

        def waiting_admission():
            waiter_started.set()
            try:
                self._admit(request, snapshot, page, verification)
            except BaseException as error:
                return type(error)
            return "accepted"

        with patch.object(
            self.store,
            "_commit_write_transaction",
            side_effect=commit_then_raise,
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(first_admission)
                self.assertTrue(commit_entered.wait(timeout=10))
                waiter = executor.submit(waiting_admission)
                self.assertTrue(waiter_started.wait(timeout=10))
                release_commit.set()
                outcomes = (first.result(timeout=10), waiter.result(timeout=10))

        self.assertEqual(
            outcomes,
            (NativeIMInboundCommitAmbiguityError, NativeIMNonceStorePoisonedError),
        )
        self.store.close()
        self.store = self._open()
        replay = self._admit(request, snapshot, page, verification)
        self.assertEqual(replay.disposition, "observed_replay")

    def test_preclaimed_nonce_cannot_complete_a_split_prepared_page(self) -> None:
        request = inbound_read_request()
        snapshot = capability()
        page = inbound_page(request=request, capability=snapshot)
        verification = raw_verification()
        self.store.prepare_native_im_inbound_read(request)
        self.store.claim(
            scope=(request.tenant_id, request.workspace_id, request.provider, request.channel_id),
            key_id=verification.key_id,
            nonce_digest=verification.nonce_digest,
            signed_at=verification.signed_at,
            expires_at=verification.expires_at,
            authentication_evidence_digest=verification.authentication_evidence_digest,
        )

        with self.assertRaises(NativeIMInboundConflictError) as raised:
            self._admit(request, snapshot, page, verification)

        self.assertIs(type(raised.exception), NativeIMInboundConflictError)
        self.assertEqual(len(self._rows("native_im_auth_nonces")), 1)
        for table in OBSERVATION_TABLES[1:]:
            self.assertEqual(self._rows(table), ())
        self.assertEqual(self._rows("native_im_inbound_reads")[0]["status"], "prepared")

    def test_changed_page_or_raw_evidence_cannot_rebind_an_admitted_request(self) -> None:
        request = inbound_read_request()
        snapshot = capability()
        page = inbound_page(request=request, capability=snapshot)
        verification = raw_verification()
        self.store.prepare_native_im_inbound_read(request)
        self.clock.value = ADMITTED_AT
        self._admit(request, snapshot, page, verification)
        changed_page = inbound_page(
            request=request,
            capability=snapshot,
            snapshot_token="different-response-snapshot",
        )
        cases = (
            (changed_page, verification),
            (page, raw_verification(nonce_digest="f" * 64, body_digest="a" * 64)),
        )

        for candidate_page, candidate_verification in cases:
            with self.subTest(page_digest=candidate_page.canonical_digest()):
                with self.assertRaises(NativeIMInboundConflictError) as raised:
                    self._admit(
                        request,
                        snapshot,
                        candidate_page,
                        candidate_verification,
                    )
                self.assertIs(type(raised.exception), NativeIMInboundConflictError)
        self.assertEqual(len(self._rows("native_im_auth_nonces")), 1)

    def test_replay_rejects_noncanonical_persisted_event_json(self) -> None:
        self._assert_exact_replay_rejects_tamper(
            ("UPDATE native_im_inbox_events SET event_json = event_json || ' '", ()),
        )

    def test_replay_rejects_event_digest_tamper(self) -> None:
        self._assert_exact_replay_rejects_tamper(
            ("UPDATE native_im_inbox_events SET event_digest = ?", (TAMPER_DIGEST,)),
        )

    def test_replay_rejects_verification_envelope_digest_tamper(self) -> None:
        self._assert_exact_replay_rejects_tamper(
            (
                "UPDATE native_im_inbox_verifications SET envelope_digest = ?",
                (TAMPER_DIGEST,),
            ),
        )

    def test_replay_rejects_verification_event_binding_tamper(self) -> None:
        self._assert_exact_replay_rejects_tamper(
            (
                "UPDATE native_im_inbox_verifications SET event_id = ?",
                ("tampered-event",),
            ),
        )

    def test_replay_rejects_invalid_persisted_verification_traceparent(self) -> None:
        invalid_traceparent = f"00-{'0' * 32}-{'0' * 16}-01"
        self._assert_exact_replay_rejects_tamper(
            (
                "UPDATE native_im_inbox_verifications SET traceparent = ?",
                (invalid_traceparent,),
            ),
        )

    def test_replay_rejects_read_event_ordinal_tamper(self) -> None:
        self._assert_exact_replay_rejects_tamper(
            ("UPDATE native_im_inbound_read_events SET ordinal = 1", ()),
        )

    def test_replay_rejects_read_event_identity_tamper(self) -> None:
        self._assert_exact_replay_rejects_tamper(
            (
                "UPDATE native_im_inbound_read_events SET event_id = ?",
                ("tampered-event",),
            ),
        )

    def test_replay_rejects_coordinated_envelope_digest_tamper(self) -> None:
        self._assert_exact_replay_rejects_tamper(
            (
                "UPDATE native_im_inbox_verifications SET envelope_digest = ?",
                (TAMPER_DIGEST,),
            ),
            (
                "UPDATE native_im_inbound_read_events SET envelope_digest = ?",
                (TAMPER_DIGEST,),
            ),
        )

    def test_replay_rejects_coordinated_read_and_checkpoint_page_digest_tamper(self) -> None:
        self._assert_exact_replay_rejects_tamper(
            (
                "UPDATE native_im_inbound_reads SET page_digest = ?",
                (TAMPER_DIGEST,),
            ),
            (
                "UPDATE native_im_inbound_checkpoints SET last_page_digest = ?",
                (TAMPER_DIGEST,),
            ),
        )

    def test_replay_rejects_cross_bound_read_event_scope(self) -> None:
        self._assert_exact_replay_rejects_tamper(
            (
                "UPDATE native_im_inbound_read_events SET workspace_id = ?",
                ("tampered-workspace",),
            ),
        )

    def test_two_connections_racing_exact_page_yield_one_fresh_and_one_replay(self) -> None:
        request = inbound_read_request()
        snapshot = capability()
        page = inbound_page(request=request, capability=snapshot)
        verification = raw_verification()
        self.store.prepare_native_im_inbound_read(request)
        second = self._open()
        self.clock.value = ADMITTED_AT
        barrier = threading.Barrier(2)

        def admit(store):
            barrier.wait()
            return self._admit(request, snapshot, page, verification, store).disposition

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = tuple(executor.map(admit, (self.store, second)))
        finally:
            second.close()
        self.assertCountEqual(results, ("fresh_observation", "observed_replay"))
        self.assertEqual(len(self._rows("native_im_auth_nonces")), 1)
        self.assertEqual(len(self._rows("native_im_inbox_events")), 1)
        self.assertEqual(len(self._rows("native_im_inbound_checkpoints")), 1)

    def test_two_connections_racing_different_pages_roll_back_loser_nonce(self) -> None:
        request = inbound_read_request()
        snapshot = capability()
        first_page = inbound_page(request=request, capability=snapshot)
        first_verification = raw_verification()
        second_evidence_digest = "a" * 64
        second_envelope = verified_envelope(
            authentication_evidence_digest=second_evidence_digest,
        )
        second_page = inbound_page(
            request=request,
            capability=snapshot,
            envelopes=(second_envelope,),
            snapshot_token="different-response-snapshot",
        )
        second_verification = raw_verification(
            nonce_digest="f" * 64,
            body_digest="a" * 64,
            authentication_evidence_digest=second_evidence_digest,
        )
        candidates = (
            ("first", first_page, first_verification),
            ("second", second_page, second_verification),
        )
        self.store.prepare_native_im_inbound_read(request)
        second_store = self._open()
        self.clock.value = ADMITTED_AT
        barrier = threading.Barrier(2)

        def admit(candidate, store):
            label, page, verification = candidate
            barrier.wait()
            try:
                result = self._admit(request, snapshot, page, verification, store)
            except BaseException as error:
                return label, type(error), None
            return label, type(result), result.disposition

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = tuple(
                    executor.submit(admit, candidate, store)
                    for candidate, store in zip(candidates, (self.store, second_store), strict=True)
                )
                outcomes = tuple(future.result(timeout=10) for future in futures)
        finally:
            second_store.close()

        accepted = tuple(outcome for outcome in outcomes if outcome[2] == "fresh_observation")
        rejected = tuple(outcome for outcome in outcomes if outcome[2] is None)
        self.assertEqual(len(accepted), 1)
        self.assertEqual(len(rejected), 1)
        self.assertIs(accepted[0][1], NativeIMInboundPageAdmissionResultV1)
        self.assertIs(rejected[0][1], NativeIMInboundConflictError)
        winning_label = accepted[0][0]
        expected_nonce = first_verification.nonce_digest if winning_label == "first" else "f" * 64
        nonce_rows = self._rows("native_im_auth_nonces")
        self.assertEqual(len(nonce_rows), 1)
        self.assertEqual(nonce_rows[0]["nonce_digest"], expected_nonce)
        read = self._rows("native_im_inbound_reads")[0]
        winning_page = first_page if winning_label == "first" else second_page
        self.assertEqual(read["page_digest"], winning_page.canonical_digest())

    def test_historical_exact_replay_remains_valid_after_checkpoint_advances(self) -> None:
        first_request = inbound_read_request()
        snapshot = capability()
        first_page = inbound_page(request=first_request, capability=snapshot)
        first_verification = raw_verification()
        self.store.prepare_native_im_inbound_read(first_request)
        self.clock.value = ADMITTED_AT
        first = self._admit(first_request, snapshot, first_page, first_verification)

        second_event = inbound_event(
            event_id="test-event-2",
            cursor="test-cursor-2",
            sequence_number=2,
            ingress_request_id="test-ingress-request-2",
            correlation_id="test-correlation-2",
        )
        second_envelope = verified_envelope(
            event=second_event,
            verification_id="test-verification-2",
        )
        second_request = inbound_read_request(
            after_cursor=first_page.next_cursor,
            after_sequence=first_page.next_sequence,
            snapshot_token=first_page.snapshot_token,
            read_request_id="test-read-request-2",
        )
        second_page = inbound_page(
            request=second_request,
            capability=snapshot,
            envelopes=(second_envelope,),
            has_more=False,
        )
        second_verification = raw_verification(
            nonce_digest="f" * 64,
            body_digest="a" * 64,
        )
        self.store.prepare_native_im_inbound_read(second_request)
        self.clock.value = "2026-08-28T01:02:05.000000Z"
        second = self._admit(
            second_request,
            snapshot,
            second_page,
            second_verification,
        )
        self.assertEqual(second.checkpoint.checkpoint_revision, 2)
        self.assertEqual(second.checkpoint.after_cursor, second_page.next_cursor)
        self.assertEqual(second.checkpoint.after_sequence, second_page.next_sequence)
        self.assertIsNone(second.checkpoint.continuation_snapshot_token)
        checkpoint_row = self._rows("native_im_inbound_checkpoints")[0]
        self.assertEqual(checkpoint_row["checkpoint_revision"], 2)
        self.assertEqual(checkpoint_row["after_cursor"], second_page.next_cursor)
        self.assertEqual(checkpoint_row["after_sequence"], second_page.next_sequence)
        self.assertIsNone(checkpoint_row["continuation_snapshot_token"])
        self.assertEqual(
            checkpoint_row["last_read_request_digest"],
            second_request.canonical_digest(),
        )
        self.assertEqual(checkpoint_row["last_page_digest"], second_page.canonical_digest())
        admitted_reads = self._rows("native_im_inbound_reads")
        self.assertEqual(
            tuple(row["admitted_checkpoint_revision"] for row in admitted_reads),
            (1, 2),
        )

        self.clock.value = True
        self.clock.calls = 0
        replay = self._admit(first_request, snapshot, first_page, first_verification)
        self.assertEqual(replay.disposition, "observed_replay")
        self.assertEqual(replay.checkpoint, first.checkpoint)
        self.assertEqual(replay.event_receipts, first.event_receipts)
        self.assertEqual(self.clock.calls, 0)


if __name__ == "__main__":
    unittest.main()
