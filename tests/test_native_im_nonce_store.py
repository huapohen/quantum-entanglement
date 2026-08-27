import os
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from quantum_entanglement.native_im_nonce_store import (
    NativeIMNonceCommitAmbiguityError,
    NativeIMNonceIntegrityError,
    NativeIMNonceStoreClosedError,
    NativeIMNonceStorePoisonedError,
    NativeIMNonceStoreProcessMismatchError,
    NativeIMNonceTransactionError,
    SQLiteNativeIMNonceReplayGuard,
)

SIGNED_AT = "2026-08-20T00:00:00.000000Z"
EXPIRES_AT = "2026-08-20T00:05:00.000000Z"
CLAIMED_AT = "2026-08-20T00:00:01.000000Z"
LATER_CLAIMED_AT = "2026-08-20T00:00:02.000000Z"
PROFILE_REVISION = "profile-revision-17"
PROFILE_DIGEST = "1" * 64
NONCE_DIGEST = "2" * 64
EVIDENCE_DIGEST = "3" * 64
SCOPE = ("tenant-1", "workspace-1", "native-im", "channel-1")
KEY_ID = "verification-key-1"


class MutableClock:
    def __init__(self, value=CLAIMED_AT):
        self.value = value

    def __call__(self):
        return self.value


class HostileText(str):
    def __str__(self):
        raise AssertionError("hostile text was coerced")

    def encode(self, *args, **kwargs):
        raise AssertionError("hostile text was encoded")


class HostileTuple(tuple):
    def __iter__(self):
        raise AssertionError("hostile tuple was traversed")

    def __len__(self):
        raise AssertionError("hostile tuple length was read")


class HostileObject:
    def __getattribute__(self, name):
        if name.startswith("__") and name.endswith("__"):
            return object.__getattribute__(self, name)
        raise AssertionError("hostile object was inspected")

    def __str__(self):
        raise AssertionError("hostile object was coerced")


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


def _inherited_guard_probe(write_fd, guard):
    try:
        try:
            guard.claim(
                scope=SCOPE,
                key_id=KEY_ID,
                nonce_digest="f" * 64,
                signed_at=SIGNED_AT,
                expires_at=EXPIRES_AT,
                authentication_evidence_digest=EVIDENCE_DIGEST,
            )
        except BaseException as error:
            claim_result = type(error).__name__
        else:
            claim_result = "accepted"
        try:
            guard.close()
        except BaseException as error:
            close_result = type(error).__name__
        else:
            close_result = "closed"
        payload = f"{claim_result}|{close_result}".encode("ascii")
        os.write(write_fd, payload)
    finally:
        os.close(write_fd)


class SQLiteNativeIMNonceReplayGuardTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tempdir.name) / "native-im.sqlite3")
        self.clock = MutableClock()
        self.guard = self._open()

    def tearDown(self):
        try:
            self.guard.close()
        except (NativeIMNonceStoreClosedError, NativeIMNonceStoreProcessMismatchError):
            pass
        self.tempdir.cleanup()

    def _open(self, **changes):
        options = {
            "profile_revision": PROFILE_REVISION,
            "profile_digest": PROFILE_DIGEST,
            "clock": self.clock,
            "busy_timeout_ms": 5_000,
        }
        options.update(changes)
        return SQLiteNativeIMNonceReplayGuard(self.path, **options)

    @staticmethod
    def _claim_on(guard, **changes):
        values = {
            "scope": SCOPE,
            "key_id": KEY_ID,
            "nonce_digest": NONCE_DIGEST,
            "signed_at": SIGNED_AT,
            "expires_at": EXPIRES_AT,
            "authentication_evidence_digest": EVIDENCE_DIGEST,
        }
        values.update(changes)
        return guard.claim(**values)

    def _claim(self, **changes):
        return self._claim_on(self.guard, **changes)

    def _row(self, scope=SCOPE, key_id=KEY_ID, nonce_digest=NONCE_DIGEST):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            return connection.execute(
                """
                SELECT * FROM native_im_auth_nonces
                WHERE tenant_id = ? AND workspace_id = ? AND provider = ?
                  AND channel_id = ? AND key_id = ? AND nonce_digest = ?
                """,
                (*scope, key_id, nonce_digest),
            ).fetchone()
        finally:
            connection.close()

    def test_first_claim_is_exact_true_and_exact_replay_is_exact_false(self):
        first = self._claim()
        replay = self._claim()

        self.assertIs(type(first), bool)
        self.assertIs(first, True)
        self.assertIs(type(replay), bool)
        self.assertIs(replay, False)

    def test_claim_persists_every_immutable_binding_and_does_not_refresh_replay_time(self):
        self.assertIs(self._claim(), True)
        row = self._row()
        self.assertIsNotNone(row)
        self.assertEqual(
            dict(row),
            {
                "tenant_id": SCOPE[0],
                "workspace_id": SCOPE[1],
                "provider": SCOPE[2],
                "channel_id": SCOPE[3],
                "key_id": KEY_ID,
                "nonce_digest": NONCE_DIGEST,
                "signed_at": SIGNED_AT,
                "expires_at": EXPIRES_AT,
                "authentication_evidence_digest": EVIDENCE_DIGEST,
                "profile_revision": PROFILE_REVISION,
                "profile_digest": PROFILE_DIGEST,
                "claimed_at": CLAIMED_AT,
            },
        )

        self.clock.value = LATER_CLAIMED_AT
        self.assertIs(self._claim(), False)
        self.assertEqual(self._row()["claimed_at"], CLAIMED_AT)

    def test_each_scope_component_and_key_id_are_independent_nonce_namespaces(self):
        identities = [(SCOPE, KEY_ID, NONCE_DIGEST)]
        for index in range(4):
            changed_scope = list(SCOPE)
            changed_scope[index] += "-other"
            identities.append((tuple(changed_scope), KEY_ID, NONCE_DIGEST))
        identities.append((SCOPE, "verification-key-2", NONCE_DIGEST))
        identities.append((SCOPE, KEY_ID, "6" * 64))

        for scope, key_id, nonce_digest in identities:
            with self.subTest(
                scope=scope,
                key_id=key_id,
                nonce_digest=nonce_digest,
                phase="fresh",
            ):
                self.assertIs(
                    self._claim(
                        scope=scope,
                        key_id=key_id,
                        nonce_digest=nonce_digest,
                    ),
                    True,
                )
        for scope, key_id, nonce_digest in identities:
            with self.subTest(
                scope=scope,
                key_id=key_id,
                nonce_digest=nonce_digest,
                phase="replay",
            ):
                self.assertIs(
                    self._claim(
                        scope=scope,
                        key_id=key_id,
                        nonce_digest=nonce_digest,
                    ),
                    False,
                )

    def test_same_nonce_with_different_signed_expiry_or_evidence_binding_fails_closed(self):
        self.assertIs(self._claim(), True)
        conflicts = (
            {"signed_at": "2026-08-20T00:00:01.000000Z"},
            {"expires_at": "2026-08-20T00:05:01.000000Z"},
            {"authentication_evidence_digest": "4" * 64},
        )
        for changes in conflicts:
            with self.subTest(changes=changes):
                with self.assertRaises(NativeIMNonceIntegrityError) as raised:
                    self._claim(**changes)
                self.assertIs(type(raised.exception), NativeIMNonceIntegrityError)
        self.assertEqual(self._row()["authentication_evidence_digest"], EVIDENCE_DIGEST)

    def test_same_nonce_reopened_under_a_different_profile_binding_fails_closed(self):
        self.assertIs(self._claim(), True)
        for changes in (
            {"profile_revision": "profile-revision-18"},
            {"profile_digest": "5" * 64},
        ):
            with self.subTest(changes=changes):
                contender = self._open(**changes)
                try:
                    with self.assertRaises(NativeIMNonceIntegrityError) as raised:
                        self._claim_on(contender)
                    self.assertIs(type(raised.exception), NativeIMNonceIntegrityError)
                finally:
                    contender.close()

    def test_constructor_rejects_non_exact_or_malformed_profile_clock_and_timeout_inputs(self):
        cases = (
            ({"profile_revision": HostileText(PROFILE_REVISION)}, TypeError),
            ({"profile_revision": ""}, ValueError),
            ({"profile_digest": HostileText(PROFILE_DIGEST)}, TypeError),
            ({"profile_digest": "A" * 64}, ValueError),
            ({"clock": HostileObject()}, TypeError),
            ({"busy_timeout_ms": True}, TypeError),
            ({"busy_timeout_ms": -1}, ValueError),
        )
        for changes, error_type in cases:
            with self.subTest(changes=changes):
                with self.assertRaises(error_type):
                    self._open(**changes)

    def test_claim_rejects_hostile_subclasses_wrong_shapes_and_noncanonical_values(self):
        cases = (
            ({"scope": None}, TypeError),
            ({"scope": list(SCOPE)}, TypeError),
            ({"scope": HostileTuple(SCOPE)}, TypeError),
            ({"scope": SCOPE[:3]}, ValueError),
            ({"scope": (*SCOPE, "extra")}, ValueError),
            ({"scope": (HostileText(SCOPE[0]), *SCOPE[1:])}, TypeError),
            ({"scope": (True, *SCOPE[1:])}, TypeError),
            ({"scope": ("", *SCOPE[1:])}, ValueError),
            ({"key_id": HostileText(KEY_ID)}, TypeError),
            ({"key_id": HostileObject()}, TypeError),
            ({"key_id": True}, TypeError),
            ({"key_id": ""}, ValueError),
            ({"nonce_digest": HostileText(NONCE_DIGEST)}, TypeError),
            ({"nonce_digest": "A" * 64}, ValueError),
            ({"nonce_digest": "2" * 63}, ValueError),
            ({"signed_at": HostileText(SIGNED_AT)}, TypeError),
            ({"signed_at": True}, TypeError),
            ({"signed_at": "2026-08-20T00:00:00Z"}, ValueError),
            ({"expires_at": True}, TypeError),
            ({"expires_at": SIGNED_AT}, ValueError),
            ({"expires_at": "2026-08-20T00:00:00Z"}, ValueError),
            ({"authentication_evidence_digest": HostileText(EVIDENCE_DIGEST)}, TypeError),
            ({"authentication_evidence_digest": True}, TypeError),
            ({"authentication_evidence_digest": "not-a-digest"}, ValueError),
        )
        for changes, error_type in cases:
            with self.subTest(changes=changes):
                with self.assertRaises(error_type):
                    self._claim(**changes)
        self.assertIsNone(self._row())

    def test_invalid_clock_result_fails_before_any_row_is_written(self):
        for value, error_type in (
            (HostileText(CLAIMED_AT), TypeError),
            ("2026-08-20T00:00:01Z", ValueError),
            (True, TypeError),
        ):
            with self.subTest(value=value):
                self.clock.value = value
                with self.assertRaises(error_type):
                    self._claim()
                self.assertIsNone(self._row())

    def test_two_connections_racing_identical_claim_yield_one_fresh_and_one_replay(self):
        second = self._open()
        barrier = threading.Barrier(2)

        def claim(guard):
            barrier.wait()
            return self._claim_on(guard)

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = tuple(executor.map(claim, (self.guard, second)))
        finally:
            second.close()
        self.assertCountEqual(results, (True, False))
        self.assertTrue(all(type(result) is bool for result in results))

    def test_two_connections_racing_conflicting_bindings_yield_fresh_and_typed_integrity(self):
        second = self._open()
        barrier = threading.Barrier(2)

        def claim(guard, evidence):
            barrier.wait()
            try:
                result = self._claim_on(
                    guard,
                    authentication_evidence_digest=evidence,
                )
            except NativeIMNonceIntegrityError as error:
                return type(error)
            return result

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = (
                    executor.submit(claim, self.guard, EVIDENCE_DIGEST),
                    executor.submit(claim, second, "4" * 64),
                )
                results = tuple(future.result() for future in futures)
        finally:
            second.close()
        self.assertCountEqual(results, (True, NativeIMNonceIntegrityError))

    def test_claim_survives_close_reopen_and_context_manager_close_is_fail_closed(self):
        self.assertIs(self._claim(), True)
        self.guard.close()
        with self.assertRaises(NativeIMNonceStoreClosedError) as raised:
            self._claim()
        self.assertIs(type(raised.exception), NativeIMNonceStoreClosedError)

        self.guard = self._open()
        self.assertIs(self._claim(), False)
        with self._open() as managed:
            self.assertIs(self._claim_on(managed), False)
        with self.assertRaises(NativeIMNonceStoreClosedError):
            self._claim_on(managed)

    @unittest.skipUnless(hasattr(os, "fork"), "requires POSIX fork")
    def test_inherited_guard_rejects_claim_and_close_while_parent_remains_usable(self):
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            try:
                _inherited_guard_probe(write_fd, self.guard)
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
        self.assertIs(self._claim(), True)

    def test_fresh_guard_opened_after_process_epoch_rotation_does_not_revive_old_owner(self):
        from quantum_entanglement import process_identity

        stale = self.guard
        process_identity._after_fork_in_child()
        self.guard = self._open()
        with self.assertRaises(NativeIMNonceStoreProcessMismatchError):
            self._claim_on(stale)
        with self.assertRaises(NativeIMNonceStoreProcessMismatchError):
            stale.close()
        self.assertIs(self._claim(), True)

    def test_committed_acknowledgement_loss_is_typed_poisoned_and_durable_after_reopen(self):
        real_commit = self.guard._commit_write_transaction

        def commit_then_raise(connection):
            real_commit(connection)
            raise RuntimeError("commit-ack-secret-canary")

        with patch.object(
            self.guard,
            "_commit_write_transaction",
            side_effect=commit_then_raise,
        ):
            with self.assertRaises(NativeIMNonceCommitAmbiguityError) as raised:
                self._claim()

        self.assertIs(type(raised.exception), NativeIMNonceCommitAmbiguityError)
        self.assertNotIn("commit-ack-secret-canary", _exception_graph_text(raised.exception))
        with self.assertRaises(NativeIMNonceStorePoisonedError) as poisoned:
            self._claim()
        self.assertIs(type(poisoned.exception), NativeIMNonceStorePoisonedError)
        self.guard.close()
        self.guard = self._open()
        self.assertIs(self._claim(), False)

    def test_confirmed_rollback_at_commit_is_typed_nonambiguous_and_store_remains_usable(self):
        marker = "rolled-back-commit-secret-canary"

        def reject_before_commit(_connection):
            raise sqlite3.OperationalError(marker)

        with patch.object(
            self.guard,
            "_commit_write_transaction",
            side_effect=reject_before_commit,
        ):
            with self.assertRaises(NativeIMNonceTransactionError) as raised:
                self._claim()
        self.assertIs(type(raised.exception), NativeIMNonceTransactionError)
        self.assertNotIn(marker, _exception_graph_text(raised.exception))
        self.assertIsNone(self._row())
        self.assertIs(self._claim(), True)

    def test_begin_database_failure_is_typed_sanitized_and_does_not_write(self):
        marker = "begin-secret-canary"
        with patch.object(
            self.guard,
            "_begin_write_transaction",
            side_effect=sqlite3.DatabaseError(marker),
        ):
            with self.assertRaises(NativeIMNonceTransactionError) as raised:
                self._claim()
        self.assertIs(type(raised.exception), NativeIMNonceTransactionError)
        self.assertNotIn(marker, _exception_graph_text(raised.exception))
        self.assertIsNone(self._row())

    def test_tampered_persisted_binding_fails_closed_as_typed_integrity(self):
        self.assertIs(self._claim(), True)
        attacker = sqlite3.connect(self.path)
        try:
            attacker.execute("PRAGMA ignore_check_constraints=ON")
            attacker.execute(
                """
                UPDATE native_im_auth_nonces
                SET authentication_evidence_digest = 'not-a-digest'
                WHERE tenant_id = ? AND workspace_id = ? AND provider = ?
                  AND channel_id = ? AND key_id = ? AND nonce_digest = ?
                """,
                (*SCOPE, KEY_ID, NONCE_DIGEST),
            )
            attacker.commit()
        finally:
            attacker.close()

        with self.assertRaises(NativeIMNonceIntegrityError) as raised:
            self._claim()
        self.assertIs(type(raised.exception), NativeIMNonceIntegrityError)

    def test_missing_owned_table_fails_closed_as_typed_integrity(self):
        attacker = sqlite3.connect(self.path)
        try:
            attacker.execute("DROP TABLE native_im_auth_nonces")
            attacker.commit()
        finally:
            attacker.close()
        with self.assertRaises(NativeIMNonceIntegrityError) as raised:
            self._claim()
        self.assertIs(type(raised.exception), NativeIMNonceIntegrityError)


if __name__ == "__main__":
    unittest.main()
