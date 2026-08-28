from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from quantum_entanglement.native_im_auth import NativeIMRawVerificationResultV1
from quantum_entanglement.native_im_inbox import NativeIMInboundPageAdmissionResultV1
from quantum_entanglement.native_im_nonce_store import SQLiteNativeIMInboxStore
from tests.test_native_im_contract import capability, inbound_page, inbound_read_request

PREPARED_AT = "2026-08-28T01:02:03.000000Z"
ADMITTED_AT = "2026-08-28T01:02:04.000000Z"
EXPIRES_AT = "2026-08-28T00:05:00.000001Z"
PROFILE_REVISION = "profile-revision-atomic-page-1"
PROFILE_DIGEST = "1" * 64


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
        self.store = SQLiteNativeIMInboxStore(
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


if __name__ == "__main__":
    unittest.main()
