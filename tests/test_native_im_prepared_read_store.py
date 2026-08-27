import os
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from quantum_entanglement.native_im import IMInboundReadRequestV1
from quantum_entanglement.native_im_gateway import IMGatewayPort
from quantum_entanglement.native_im_inbox import (
    NativeIMInboundCheckpointConflictError,
    NativeIMInboundCommitAmbiguityError,
    NativeIMInboundConflictError,
    NativeIMInboundReadPreparationV1,
    NativeIMInboundTransactionError,
    NativeIMScopeV1,
)
from quantum_entanglement.native_im_nonce_store import (
    NativeIMInboxStoreIntegrityError,
    NativeIMNonceCommitAmbiguityError,
    NativeIMNonceStoreClosedError,
    NativeIMNonceStorePoisonedError,
    NativeIMNonceStoreProcessMismatchError,
    SQLiteNativeIMInboxStore,
)
from quantum_entanglement.store import SQLiteEventStore

PREPARED_AT = "2026-08-28T01:02:03.000000Z"
LATER_PREPARED_AT = "2026-08-28T01:02:04.000000Z"
ADMITTED_AT = "2026-08-28T01:01:59.000000Z"
PROFILE_REVISION = "profile-revision-17"
PROFILE_DIGEST = "1" * 64
PAGE_DIGEST = "2" * 64
MANIFEST_DIGEST = "3" * 64
CAPABILITY_DIGEST = "4" * 64
SCOPE = ("tenant-1", "workspace-1", "native-im", "channel-1")


class MutableClock:
    def __init__(self, value=PREPARED_AT):
        self.value = value
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.value


def read_request(**changes):
    values = {
        "schema_version": 1,
        "tenant_id": SCOPE[0],
        "workspace_id": SCOPE[1],
        "provider": SCOPE[2],
        "channel_id": SCOPE[3],
        "after_cursor": None,
        "after_sequence": None,
        "snapshot_token": None,
        "limit": 100,
        "read_request_id": "read-request-1",
    }
    values.update(changes)
    return IMInboundReadRequestV1(**values)


def _exception_graph_text(error):
    pending = [error]
    visited = set()
    details = []
    while pending and len(visited) < 50:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        details.extend((str(current), repr(current), repr(current.args)))
        for related in (current.__cause__, current.__context__):
            if related is not None:
                pending.append(related)
    return " ".join(details)


def _inherited_store_probe(write_fd, store, request):
    try:
        try:
            store.prepare_native_im_inbound_read(request)
        except BaseException as error:
            prepare_result = type(error).__name__
        else:
            prepare_result = "accepted"
        try:
            store.close()
        except BaseException as error:
            close_result = type(error).__name__
        else:
            close_result = "closed"
        os.write(write_fd, f"{prepare_result}|{close_result}".encode("ascii"))
    finally:
        os.close(write_fd)


class SQLiteNativeIMPreparedReadStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tempdir.name) / "native-im.sqlite3")
        self.clock = MutableClock()
        self.store = self._open()

    def tearDown(self):
        try:
            self.store.close()
        except (NativeIMNonceStoreClosedError, NativeIMNonceStoreProcessMismatchError):
            pass
        self.tempdir.cleanup()

    def _open(self, path=None, **changes):
        options = {
            "profile_revision": PROFILE_REVISION,
            "profile_digest": PROFILE_DIGEST,
            "clock": self.clock,
            "busy_timeout_ms": 5_000,
        }
        options.update(changes)
        return SQLiteNativeIMInboxStore(path or self.path, **options)

    def _prepare(self, request=None, store=None):
        return (store or self.store).prepare_native_im_inbound_read(request or read_request())

    def _rows(self, table, path=None):
        connection = sqlite3.connect(path or self.path)
        connection.row_factory = sqlite3.Row
        try:
            return tuple(connection.execute(f"SELECT * FROM {table}").fetchall())
        finally:
            connection.close()

    def _row(self, path=None):
        rows = self._rows("native_im_inbound_reads", path)
        self.assertLessEqual(len(rows), 1)
        return rows[0] if rows else None

    def _seed_checkpoint(self, scope=SCOPE):
        seed = read_request(
            tenant_id=scope[0],
            workspace_id=scope[1],
            provider=scope[2],
            channel_id=scope[3],
            read_request_id="admitted-read-1",
        )
        digest = seed.canonical_digest()
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                """
                INSERT INTO native_im_inbound_reads (
                    tenant_id, workspace_id, provider, channel_id,
                    read_request_id, read_request_digest, request_json,
                    base_checkpoint_revision, after_cursor, after_sequence,
                    request_snapshot_token, status, prepared_at, page_digest,
                    response_snapshot_token, next_cursor, next_sequence,
                    continuation_snapshot_token, has_more, envelope_count,
                    event_manifest_sha256, capability_revision,
                    capability_digest, admitted_checkpoint_revision, admitted_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, NULL, 'admitted', ?, ?,
                    'snapshot-1', 'cursor-10', 10, 'snapshot-1', 1, 1, ?,
                    'capability-revision-1', ?, 1, ?
                )
                """,
                (
                    *scope,
                    seed.read_request_id,
                    digest,
                    seed.canonical_bytes().decode("utf-8"),
                    ADMITTED_AT,
                    PAGE_DIGEST,
                    MANIFEST_DIGEST,
                    CAPABILITY_DIGEST,
                    ADMITTED_AT,
                ),
            )
            connection.execute(
                """
                INSERT INTO native_im_inbound_checkpoints (
                    tenant_id, workspace_id, provider, channel_id,
                    after_cursor, after_sequence, continuation_snapshot_token,
                    checkpoint_revision, last_read_request_digest,
                    last_page_digest, updated_at
                ) VALUES (?, ?, ?, ?, 'cursor-10', 10, 'snapshot-1', 1, ?, ?, ?)
                """,
                (*scope, digest, PAGE_DIGEST, ADMITTED_AT),
            )
            connection.commit()
        finally:
            connection.close()
        return read_request(
            tenant_id=scope[0],
            workspace_id=scope[1],
            provider=scope[2],
            channel_id=scope[3],
            after_cursor="cursor-10",
            after_sequence=10,
            snapshot_token="snapshot-1",
            read_request_id="read-request-after-checkpoint",
        )

    def _advance_checkpoint_to_revision_two(self):
        request = read_request(
            after_cursor="cursor-10",
            after_sequence=10,
            snapshot_token="snapshot-1",
            read_request_id="admitted-read-2",
        )
        digest = request.canonical_digest()
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                """
                INSERT INTO native_im_inbound_reads (
                    tenant_id, workspace_id, provider, channel_id,
                    read_request_id, read_request_digest, request_json,
                    base_checkpoint_revision, after_cursor, after_sequence,
                    request_snapshot_token, status, prepared_at, page_digest,
                    response_snapshot_token, next_cursor, next_sequence,
                    continuation_snapshot_token, has_more, envelope_count,
                    event_manifest_sha256, capability_revision,
                    capability_digest, admitted_checkpoint_revision, admitted_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, 1, 'cursor-10', 10, 'snapshot-1',
                    'admitted', ?, ?, 'snapshot-1', 'cursor-20', 20,
                    'snapshot-1', 1, 1, ?, 'capability-revision-2', ?, 2, ?
                )
                """,
                (
                    *SCOPE,
                    request.read_request_id,
                    digest,
                    request.canonical_bytes().decode("utf-8"),
                    ADMITTED_AT,
                    "9" * 64,
                    "a" * 64,
                    "b" * 64,
                    ADMITTED_AT,
                ),
            )
            connection.execute(
                """
                UPDATE native_im_inbound_checkpoints
                SET after_cursor = 'cursor-20', after_sequence = 20,
                    continuation_snapshot_token = 'snapshot-1',
                    checkpoint_revision = 2, last_read_request_digest = ?,
                    last_page_digest = ?, updated_at = ?
                WHERE tenant_id = ? AND workspace_id = ?
                  AND provider = ? AND channel_id = ?
                """,
                (digest, "9" * 64, ADMITTED_AT, *SCOPE),
            )
            connection.commit()
        finally:
            connection.close()
        return request

    def assertPreparation(
        self,
        value,
        *,
        request,
        revision,
        disposition,
        status="prepared",
        prepared_at=PREPARED_AT,
    ):
        self.assertIs(type(value), NativeIMInboundReadPreparationV1)
        self.assertEqual(
            value,
            NativeIMInboundReadPreparationV1(
                schema_version=1,
                scope=NativeIMScopeV1(
                    schema_version=1,
                    tenant_id=request.tenant_id,
                    workspace_id=request.workspace_id,
                    provider=request.provider,
                    channel_id=request.channel_id,
                ),
                read_request_id=request.read_request_id,
                read_request_digest=request.canonical_digest(),
                base_checkpoint_revision=revision,
                read_status=status,
                disposition=disposition,
                prepared_at=prepared_at,
            ),
        )

    def test_initial_request_is_fresh_and_exact_replay_is_observed_without_refresh(self):
        request = read_request()
        self.clock.calls = 0
        fresh = self._prepare(request)
        self.assertEqual(self.clock.calls, 1)
        row_after_fresh = dict(self._row())

        self.clock.value = LATER_PREPARED_AT
        self.clock.calls = 0
        replay = self._prepare(request)

        self.assertPreparation(
            fresh,
            request=request,
            revision=0,
            disposition="fresh_observation",
        )
        self.assertPreparation(
            replay,
            request=request,
            revision=0,
            disposition="observed_replay",
        )
        self.assertEqual(self.clock.calls, 0)
        self.assertEqual(dict(self._row()), row_after_fresh)
        self.assertEqual(
            row_after_fresh,
            {
                "tenant_id": SCOPE[0],
                "workspace_id": SCOPE[1],
                "provider": SCOPE[2],
                "channel_id": SCOPE[3],
                "read_request_id": request.read_request_id,
                "read_request_digest": request.canonical_digest(),
                "request_json": request.canonical_bytes().decode("utf-8"),
                "base_checkpoint_revision": 0,
                "after_cursor": None,
                "after_sequence": None,
                "request_snapshot_token": None,
                "status": "prepared",
                "prepared_at": PREPARED_AT,
                "page_digest": None,
                "response_snapshot_token": None,
                "next_cursor": None,
                "next_sequence": None,
                "continuation_snapshot_token": None,
                "has_more": None,
                "envelope_count": None,
                "event_manifest_sha256": None,
                "capability_revision": None,
                "capability_digest": None,
                "admitted_checkpoint_revision": None,
                "admitted_at": None,
            },
        )

    def test_exact_checkpoint_resume_prepares_against_its_current_revision(self):
        request = self._seed_checkpoint()
        preparation = self._prepare(request)

        self.assertPreparation(
            preparation,
            request=request,
            revision=1,
            disposition="fresh_observation",
        )
        rows = self._rows("native_im_inbound_reads")
        self.assertEqual(len(rows), 2)
        prepared = next(row for row in rows if row["status"] == "prepared")
        self.assertEqual(prepared["base_checkpoint_revision"], 1)
        self.assertEqual(prepared["after_cursor"], "cursor-10")
        self.assertEqual(prepared["after_sequence"], 10)
        self.assertEqual(prepared["request_snapshot_token"], "snapshot-1")

    def test_admitted_replay_is_stable_even_after_the_scope_advances_to_a_later_checkpoint(self):
        self._seed_checkpoint()
        historical_request = read_request(read_request_id="admitted-read-1")
        self._advance_checkpoint_to_revision_two()
        self.clock.value = True
        self.clock.calls = 0

        replay = self._prepare(historical_request)

        self.assertPreparation(
            replay,
            request=historical_request,
            revision=0,
            status="admitted",
            disposition="observed_replay",
            prepared_at=ADMITTED_AT,
        )
        self.assertEqual(self.clock.calls, 0)
        checkpoint = self._rows("native_im_inbound_checkpoints")[0]
        self.assertEqual(checkpoint["checkpoint_revision"], 2)

    def test_admitted_rows_without_a_checkpoint_fail_as_durable_graph_integrity(self):
        request = self._seed_checkpoint()
        attacker = sqlite3.connect(self.path)
        try:
            attacker.execute("DELETE FROM native_im_inbound_checkpoints")
            attacker.commit()
        finally:
            attacker.close()

        with self.assertRaises(NativeIMInboxStoreIntegrityError) as raised:
            self._prepare(request)

        self.assertIs(type(raised.exception), NativeIMInboxStoreIntegrityError)

    def test_checkpoint_behind_maximum_admitted_revision_fails_as_graph_integrity(self):
        request = self._seed_checkpoint()
        self._advance_checkpoint_to_revision_two()
        attacker = sqlite3.connect(self.path)
        try:
            first_digest = read_request(read_request_id="admitted-read-1").canonical_digest()
            attacker.execute(
                """
                UPDATE native_im_inbound_checkpoints
                SET after_cursor = 'cursor-10', after_sequence = 10,
                    continuation_snapshot_token = 'snapshot-1',
                    checkpoint_revision = 1, last_read_request_digest = ?,
                    last_page_digest = ?, updated_at = ?
                """,
                (first_digest, PAGE_DIGEST, ADMITTED_AT),
            )
            attacker.commit()
        finally:
            attacker.close()

        with self.assertRaises(NativeIMInboxStoreIntegrityError) as raised:
            self._prepare(request)

        self.assertIs(type(raised.exception), NativeIMInboxStoreIntegrityError)

    def test_checkpoint_resume_cross_binding_to_its_parent_read_fails_closed(self):
        request = self._seed_checkpoint()
        attacker = sqlite3.connect(self.path)
        try:
            attacker.execute(
                "UPDATE native_im_inbound_checkpoints SET after_cursor = 'cross-bound-cursor'"
            )
            attacker.commit()
        finally:
            attacker.close()

        with self.assertRaises(NativeIMInboxStoreIntegrityError) as raised:
            self._prepare(request)

        self.assertIs(type(raised.exception), NativeIMInboxStoreIntegrityError)

    def test_each_resume_coordinate_must_exactly_match_the_durable_checkpoint(self):
        request = self._seed_checkpoint()
        mismatches = (
            {"after_cursor": "cursor-11"},
            {"after_sequence": 11},
            {"snapshot_token": "snapshot-2"},
            {
                "after_cursor": None,
                "after_sequence": None,
                "snapshot_token": None,
            },
        )
        for changes in mismatches:
            with self.subTest(changes=changes):
                values = request.to_dict()
                candidate = read_request(
                    tenant_id=values["tenantId"],
                    workspace_id=values["workspaceId"],
                    provider=values["provider"],
                    channel_id=values["channelId"],
                    after_cursor=changes.get("after_cursor", request.after_cursor),
                    after_sequence=changes.get("after_sequence", request.after_sequence),
                    snapshot_token=changes.get("snapshot_token", request.snapshot_token),
                    read_request_id=f"mismatch-{len(str(changes))}",
                )
                with self.assertRaises(NativeIMInboundCheckpointConflictError) as raised:
                    self._prepare(candidate)
                self.assertIs(type(raised.exception), NativeIMInboundCheckpointConflictError)
        self.assertEqual(len(self._rows("native_im_inbound_reads")), 1)

    def test_same_read_request_id_with_a_different_digest_fails_closed(self):
        original = read_request()
        conflict = read_request(limit=99)
        self._prepare(original)

        with self.assertRaises(NativeIMInboundConflictError) as raised:
            self._prepare(conflict)

        self.assertIs(type(raised.exception), NativeIMInboundConflictError)
        self.assertEqual(len(self._rows("native_im_inbound_reads")), 1)

    def test_same_digest_rebound_to_a_different_persisted_identity_fails_closed(self):
        request = read_request()
        self._prepare(request)
        attacker = sqlite3.connect(self.path)
        try:
            attacker.execute(
                "UPDATE native_im_inbound_reads SET read_request_id = 'forked-identity'"
            )
            attacker.commit()
        finally:
            attacker.close()

        with self.assertRaises(NativeIMInboxStoreIntegrityError) as raised:
            self._prepare(request)

        self.assertIs(type(raised.exception), NativeIMInboxStoreIntegrityError)
        self.assertEqual(self._row()["read_request_id"], "forked-identity")

    def test_only_one_distinct_prepared_read_can_exist_per_scope(self):
        first = read_request()
        second = read_request(read_request_id="read-request-2", limit=99)
        self._prepare(first)

        with self.assertRaises(NativeIMInboundConflictError) as raised:
            self._prepare(second)

        self.assertIs(type(raised.exception), NativeIMInboundConflictError)
        self.assertEqual(len(self._rows("native_im_inbound_reads")), 1)

    def test_prepared_read_identity_and_singleton_are_namespaced_by_exact_scope(self):
        first = read_request()
        second = read_request(
            tenant_id="tenant-2",
            workspace_id="workspace-2",
            provider="other-provider",
            channel_id="channel-2",
        )

        results = (self._prepare(first), self._prepare(second))

        for result, request in zip(results, (first, second), strict=True):
            self.assertPreparation(
                result,
                request=request,
                revision=0,
                disposition="fresh_observation",
            )
        self.assertEqual(len(self._rows("native_im_inbound_reads")), 2)

    def test_two_connections_racing_the_same_request_yield_one_fresh_and_one_replay(self):
        second = self._open()
        request = read_request()
        barrier = threading.Barrier(2)

        def prepare(store):
            barrier.wait()
            return store.prepare_native_im_inbound_read(request)

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = tuple(executor.map(prepare, (self.store, second)))
        finally:
            second.close()
        self.assertCountEqual(
            (result.disposition for result in results),
            ("fresh_observation", "observed_replay"),
        )
        self.assertEqual(len(self._rows("native_im_inbound_reads")), 1)

    def test_two_connections_racing_distinct_requests_yield_fresh_and_checkpoint_conflict(self):
        second = self._open()
        requests = (read_request(), read_request(read_request_id="read-request-2", limit=99))
        barrier = threading.Barrier(2)

        def prepare(store, request):
            barrier.wait()
            try:
                return store.prepare_native_im_inbound_read(request).disposition
            except NativeIMInboundConflictError as error:
                self.assertIs(type(error), NativeIMInboundConflictError)
                return type(error)

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = tuple(
                    executor.submit(prepare, store, request)
                    for store, request in zip((self.store, second), requests, strict=True)
                )
                results = tuple(future.result() for future in futures)
        finally:
            second.close()
        self.assertCountEqual(
            results,
            ("fresh_observation", NativeIMInboundConflictError),
        )
        self.assertEqual(len(self._rows("native_im_inbound_reads")), 1)

    def test_two_connections_racing_same_id_with_different_digests_yield_fresh_and_conflict(self):
        second = self._open()
        requests = (read_request(), read_request(limit=99))
        barrier = threading.Barrier(2)

        def prepare(store, request):
            barrier.wait()
            try:
                return store.prepare_native_im_inbound_read(request).disposition
            except NativeIMInboundConflictError as error:
                self.assertIs(type(error), NativeIMInboundConflictError)
                return type(error)

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = tuple(
                    executor.submit(prepare, store, request)
                    for store, request in zip((self.store, second), requests, strict=True)
                )
                results = tuple(future.result() for future in futures)
        finally:
            second.close()
        self.assertCountEqual(
            results,
            ("fresh_observation", NativeIMInboundConflictError),
        )
        self.assertEqual(len(self._rows("native_im_inbound_reads")), 1)

    def test_every_persisted_read_column_is_validated_before_replay(self):
        tamper_cases = (
            ("tenant_id", "tenant-tampered", {"tenant_id": "tenant-tampered"}),
            ("workspace_id", "workspace-tampered", {"workspace_id": "workspace-tampered"}),
            ("provider", "provider-tampered", {"provider": "provider-tampered"}),
            ("channel_id", "channel-tampered", {"channel_id": "channel-tampered"}),
            ("read_request_id", "forked-request", {}),
            ("read_request_digest", "5" * 64, {}),
            ("request_json", "{}", {}),
            ("base_checkpoint_revision", 1, {}),
            ("after_cursor", "cursor-tampered", {}),
            ("after_sequence", 7, {}),
            ("request_snapshot_token", "snapshot-tampered", {}),
            ("status", "admitted", {}),
            ("prepared_at", "invalid-timestamp", {}),
            ("page_digest", "6" * 64, {}),
            ("response_snapshot_token", "response-tampered", {}),
            ("next_cursor", "next-tampered", {}),
            ("next_sequence", 8, {}),
            ("continuation_snapshot_token", "continuation-tampered", {}),
            ("has_more", 1, {}),
            ("envelope_count", 1, {}),
            ("event_manifest_sha256", "7" * 64, {}),
            ("capability_revision", "capability-tampered", {}),
            ("capability_digest", "8" * 64, {}),
            ("admitted_checkpoint_revision", 1, {}),
            ("admitted_at", LATER_PREPARED_AT, {}),
        )
        self.assertEqual(len(tamper_cases), 25)

        for index, (column, value, request_changes) in enumerate(tamper_cases):
            with self.subTest(column=column):
                path = str(Path(self.tempdir.name) / f"tamper-{index}.sqlite3")
                store = self._open(path)
                request = read_request()
                try:
                    store.prepare_native_im_inbound_read(request)
                    attacker = sqlite3.connect(path)
                    try:
                        attacker.execute("PRAGMA ignore_check_constraints=ON")
                        attacker.execute(
                            f"UPDATE native_im_inbound_reads SET {column} = ?",
                            (value,),
                        )
                        attacker.commit()
                    finally:
                        attacker.close()
                    candidate = read_request(**request_changes)
                    with self.assertRaises(NativeIMInboxStoreIntegrityError) as raised:
                        store.prepare_native_im_inbound_read(candidate)
                    self.assertIs(type(raised.exception), NativeIMInboxStoreIntegrityError)
                finally:
                    store.close()

    def test_invalid_clock_fails_before_write_and_exact_replay_never_reads_clock(self):
        for index, (value, error_type) in enumerate(
            (("2026-08-28T01:02:03Z", ValueError), (True, TypeError))
        ):
            with self.subTest(value=value):
                path = str(Path(self.tempdir.name) / f"clock-{index}.sqlite3")
                clock = MutableClock()
                store = self._open(path, clock=clock)
                try:
                    clock.value = value
                    clock.calls = 0
                    with self.assertRaises(error_type):
                        store.prepare_native_im_inbound_read(read_request())
                    self.assertEqual(clock.calls, 1)
                    self.assertEqual(self._rows("native_im_inbound_reads", path), ())
                finally:
                    store.close()

        request = read_request()
        self._prepare(request)
        self.clock.value = True
        self.clock.calls = 0
        replay = self._prepare(request)
        self.assertEqual(replay.disposition, "observed_replay")
        self.assertEqual(self.clock.calls, 0)

    def test_prepare_writes_no_observation_output_domain_or_delivery_rows_and_calls_no_port(self):
        protected_tables = (
            "native_im_inbox_events",
            "native_im_inbox_verifications",
            "native_im_inbound_read_events",
            "native_im_inbound_checkpoints",
            "events",
            "outbox",
            "inbox_receipts",
            "invocation_jobs",
        )
        before = {table: len(self._rows(table)) for table in protected_tables}

        with (
            patch.object(
                IMGatewayPort,
                "read_inbound",
                side_effect=AssertionError("gateway-read-canary"),
            ) as gateway_read,
            patch.object(
                SQLiteEventStore,
                "append_inbox",
                side_effect=AssertionError("append-inbox-canary"),
            ) as append_inbox,
        ):
            preparation = self._prepare()

        self.assertEqual(preparation.disposition, "fresh_observation")
        self.assertEqual(
            {table: len(self._rows(table)) for table in protected_tables},
            before,
        )
        self.assertEqual(len(self._rows("native_im_inbound_reads")), 1)
        gateway_read.assert_not_called()
        append_inbox.assert_not_called()

    def test_begin_failure_is_sanitized_confirmed_noncommit_and_store_remains_usable(self):
        marker = "begin-prepared-read-secret-canary"
        with patch.object(
            self.store,
            "_begin_write_transaction",
            side_effect=sqlite3.DatabaseError(marker),
        ):
            with self.assertRaises(NativeIMInboundTransactionError) as raised:
                self._prepare()

        self.assertIs(type(raised.exception), NativeIMInboundTransactionError)
        self.assertNotIn(marker, _exception_graph_text(raised.exception))
        self.assertEqual(self._rows("native_im_inbound_reads"), ())
        self.assertEqual(self._prepare().disposition, "fresh_observation")

    def test_begin_acknowledgement_loss_rolls_back_and_is_not_poisoning(self):
        real_begin = self.store._begin_write_transaction

        def begin_then_raise(connection):
            real_begin(connection)
            raise RuntimeError("begin-ack-secret-canary")

        with patch.object(
            self.store,
            "_begin_write_transaction",
            side_effect=begin_then_raise,
        ):
            with self.assertRaises(NativeIMInboundTransactionError) as raised:
                self._prepare()

        self.assertIs(type(raised.exception), NativeIMInboundTransactionError)
        self.assertNotIn("begin-ack-secret-canary", _exception_graph_text(raised.exception))
        self.assertEqual(self._rows("native_im_inbound_reads"), ())
        self.assertEqual(self._prepare().disposition, "fresh_observation")

    def test_body_operational_failure_is_sanitized_transaction_error_after_rollback(self):
        marker = "body-operational-secret-canary"
        with patch.object(
            self.store,
            "_load_inbound_checkpoint",
            side_effect=sqlite3.OperationalError(marker),
        ):
            with self.assertRaises(NativeIMInboundTransactionError) as raised:
                self._prepare()

        self.assertIs(type(raised.exception), NativeIMInboundTransactionError)
        self.assertNotIn(marker, _exception_graph_text(raised.exception))
        self.assertEqual(self._rows("native_im_inbound_reads"), ())
        self.assertEqual(self._prepare().disposition, "fresh_observation")

    def test_body_integrity_failure_is_sanitized_store_integrity_error_after_rollback(self):
        marker = "body-integrity-secret-canary"
        with patch.object(
            self.store,
            "_load_inbound_checkpoint",
            side_effect=sqlite3.IntegrityError(marker),
        ):
            with self.assertRaises(NativeIMInboxStoreIntegrityError) as raised:
                self._prepare()

        self.assertIs(type(raised.exception), NativeIMInboxStoreIntegrityError)
        self.assertNotIn(marker, _exception_graph_text(raised.exception))
        self.assertEqual(self._rows("native_im_inbound_reads"), ())
        self.assertEqual(self._prepare().disposition, "fresh_observation")

    def test_commit_rejection_is_confirmed_noncommit_and_store_remains_usable(self):
        marker = "commit-rejected-secret-canary"
        with patch.object(
            self.store,
            "_commit_write_transaction",
            side_effect=sqlite3.OperationalError(marker),
        ):
            with self.assertRaises(NativeIMInboundTransactionError) as raised:
                self._prepare()

        self.assertIs(type(raised.exception), NativeIMInboundTransactionError)
        self.assertNotIn(marker, _exception_graph_text(raised.exception))
        self.assertEqual(self._rows("native_im_inbound_reads"), ())
        self.assertEqual(self._prepare().disposition, "fresh_observation")

    def test_commit_acknowledgement_loss_is_ambiguous_poisoned_and_reconciles_on_reopen(self):
        real_commit = self.store._commit_write_transaction

        def commit_then_raise(connection):
            real_commit(connection)
            raise RuntimeError("commit-ack-secret-canary")

        request = read_request()
        with patch.object(
            self.store,
            "_commit_write_transaction",
            side_effect=commit_then_raise,
        ):
            with self.assertRaises(NativeIMInboundCommitAmbiguityError) as raised:
                self._prepare(request)

        self.assertIs(type(raised.exception), NativeIMInboundCommitAmbiguityError)
        self.assertNotIn("commit-ack-secret-canary", _exception_graph_text(raised.exception))
        with self.assertRaises(NativeIMNonceStorePoisonedError) as poisoned:
            self._prepare(request)
        self.assertIs(type(poisoned.exception), NativeIMNonceStorePoisonedError)

        self.store.close()
        self.store = self._open()
        replay = self._prepare(request)
        self.assertPreparation(
            replay,
            request=request,
            revision=0,
            disposition="observed_replay",
        )

    def test_prepare_ack_loss_poison_blocks_nonce_claim_until_reopen(self):
        real_commit = self.store._commit_write_transaction

        def commit_then_raise(connection):
            real_commit(connection)
            raise RuntimeError("prepare-cross-operation-canary")

        with patch.object(
            self.store,
            "_commit_write_transaction",
            side_effect=commit_then_raise,
        ):
            with self.assertRaises(NativeIMInboundCommitAmbiguityError):
                self._prepare()

        with self.assertRaises(NativeIMNonceStorePoisonedError) as raised:
            self.store.claim(
                scope=SCOPE,
                key_id="verification-key-1",
                nonce_digest="c" * 64,
                signed_at="2026-08-28T01:00:00.000000Z",
                expires_at="2026-08-28T01:05:00.000000Z",
                authentication_evidence_digest="d" * 64,
            )
        self.assertIs(type(raised.exception), NativeIMNonceStorePoisonedError)

    def test_nonce_ack_loss_poison_blocks_prepare_until_reopen(self):
        real_commit = self.store._commit_write_transaction

        def commit_then_raise(connection):
            real_commit(connection)
            raise RuntimeError("nonce-cross-operation-canary")

        with patch.object(
            self.store,
            "_commit_write_transaction",
            side_effect=commit_then_raise,
        ):
            with self.assertRaises(NativeIMNonceCommitAmbiguityError):
                self.store.claim(
                    scope=SCOPE,
                    key_id="verification-key-1",
                    nonce_digest="e" * 64,
                    signed_at="2026-08-28T01:00:00.000000Z",
                    expires_at="2026-08-28T01:05:00.000000Z",
                    authentication_evidence_digest="f" * 64,
                )

        with self.assertRaises(NativeIMNonceStorePoisonedError) as raised:
            self._prepare()
        self.assertIs(type(raised.exception), NativeIMNonceStorePoisonedError)

    def test_waiter_blocked_behind_ambiguous_commit_rechecks_poison_inside_the_lock(self):
        real_commit = self.store._commit_write_transaction
        commit_entered = threading.Event()
        release_commit = threading.Event()
        waiter_started = threading.Event()

        def commit_then_raise(connection):
            commit_entered.set()
            self.assertTrue(release_commit.wait(timeout=10))
            real_commit(connection)
            raise RuntimeError("blocked-waiter-commit-canary")

        def first_prepare():
            try:
                self._prepare(read_request())
            except BaseException as error:
                return type(error)
            return "accepted"

        def waiting_prepare():
            waiter_started.set()
            try:
                self._prepare(read_request(read_request_id="waiting-request", limit=99))
            except BaseException as error:
                return type(error)
            return "accepted"

        with patch.object(
            self.store,
            "_commit_write_transaction",
            side_effect=commit_then_raise,
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(first_prepare)
                self.assertTrue(commit_entered.wait(timeout=10))
                waiter = executor.submit(waiting_prepare)
                self.assertTrue(waiter_started.wait(timeout=10))
                release_commit.set()
                results = (first.result(timeout=10), waiter.result(timeout=10))

        self.assertEqual(
            results,
            (NativeIMInboundCommitAmbiguityError, NativeIMNonceStorePoisonedError),
        )

    def test_close_and_reopen_preserve_replay_while_closed_store_fails_closed(self):
        request = read_request()
        self._prepare(request)
        self.store.close()
        with self.assertRaises(NativeIMNonceStoreClosedError) as raised:
            self._prepare(request)
        self.assertIs(type(raised.exception), NativeIMNonceStoreClosedError)

        self.store = self._open()
        self.assertEqual(self._prepare(request).disposition, "observed_replay")
        with self._open() as managed:
            self.assertEqual(self._prepare(request, managed).disposition, "observed_replay")
        with self.assertRaises(NativeIMNonceStoreClosedError):
            self._prepare(request, managed)

    @unittest.skipUnless(hasattr(os, "fork"), "requires POSIX fork")
    def test_fork_inherited_store_rejects_prepare_and_close_while_parent_remains_usable(self):
        request = read_request()
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            try:
                _inherited_store_probe(write_fd, self.store, request)
            finally:
                os._exit(0)
        os.close(write_fd)
        try:
            payload = os.read(read_fd, 4096).decode("ascii")
        finally:
            os.close(read_fd)
        waited_pid, status = os.waitpid(pid, 0)
        self.assertEqual(waited_pid, pid)
        self.assertTrue(os.WIFEXITED(status))
        self.assertEqual(os.WEXITSTATUS(status), 0)
        self.assertEqual(
            payload,
            "NativeIMNonceStoreProcessMismatchError|NativeIMNonceStoreProcessMismatchError",
        )
        self.assertEqual(self._prepare(request).disposition, "fresh_observation")


if __name__ == "__main__":
    unittest.main()
