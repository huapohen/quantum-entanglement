from __future__ import annotations

import copy
import json
import os
import pickle
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from quantum_entanglement._native_im_codec import NativeIMCodecTooLargeError
from quantum_entanglement.native_im_sandbox_approval_store import (
    NativeIMSandboxApprovalAuthorityStateV1,
    NativeIMSandboxApprovalStoreError,
    NativeIMSandboxApprovalStoreIntegrityError,
    SQLiteNativeIMSandboxApprovalHighWaterV1,
)

TIME = "2026-08-28T12:00:00.000001Z"
LATER = "2026-08-28T12:00:01.000001Z"
DIGEST = "a" * 64


def authority_state(**changes: object) -> NativeIMSandboxApprovalAuthorityStateV1:
    values: dict[str, object] = {
        "schema_version": 1,
        "approval_id": "test-native-im-approval",
        "approval_digest": DIGEST,
        "authority_revision": 7,
        "state": "approved",
        "observed_at": TIME,
    }
    values.update(changes)
    return NativeIMSandboxApprovalAuthorityStateV1(**values)  # type: ignore[arg-type]


def store_path(tmp_path: Path) -> str:
    return str((tmp_path / "native-im-approval-high-water.sqlite3").resolve())


def test_authority_state_round_trip_and_domain_separated_digests_are_stable() -> None:
    value = authority_state()
    wire = value.to_dict()
    encoded = value.canonical_bytes()

    assert NativeIMSandboxApprovalAuthorityStateV1.from_dict(wire) == value
    assert NativeIMSandboxApprovalAuthorityStateV1.from_json_bytes(encoded) == value
    assert encoded == json.dumps(
        wire,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    assert value.canonical_digest() == (
        "73df52ec6ba8fedcac2bf821c1cb9d8a009a13de0c5d38c94021d32490c340d0"
    )
    assert value.state_binding_digest() == (
        "0756ae7a544954de17aeeefc77185fd0f14a8e3d3b21a07d7a9282feb404765f"
    )
    assert replace(value, observed_at=LATER).canonical_digest() != value.canonical_digest()
    assert replace(value, observed_at=LATER).state_binding_digest() == (
        value.state_binding_digest()
    )


def test_authority_state_decoder_rejects_aliases_and_hostile_shapes() -> None:
    value = authority_state()
    missing = value.to_dict()
    del missing["approvalId"]
    with pytest.raises(ValueError):
        NativeIMSandboxApprovalAuthorityStateV1.from_dict(missing)
    with pytest.raises(ValueError):
        NativeIMSandboxApprovalAuthorityStateV1.from_dict(
            {**value.to_dict(), "future": "forbidden"}
        )
    duplicate = b'{"approvalId":"duplicate",' + value.canonical_bytes()[1:]
    with pytest.raises(ValueError):
        NativeIMSandboxApprovalAuthorityStateV1.from_json_bytes(duplicate)
    with pytest.raises(NativeIMCodecTooLargeError):
        NativeIMSandboxApprovalAuthorityStateV1.from_json_bytes(b" " * (16 * 1_024 + 1))
    with pytest.raises(TypeError):
        authority_state(authority_revision=True)
    with pytest.raises(ValueError):
        authority_state(authority_revision=0)
    with pytest.raises(ValueError):
        authority_state(state="superseded")


def test_store_persists_exact_high_water_across_restart_with_private_permissions(
    tmp_path: Path,
) -> None:
    path = store_path(tmp_path)
    first = authority_state()
    with SQLiteNativeIMSandboxApprovalHighWaterV1(path) as store:
        assert store.current(first.approval_id) is None
        assert store.observe(first) == first
        assert store.current(first.approval_id) == first
        assert os.stat(path).st_mode & 0o777 == 0o600

    with SQLiteNativeIMSandboxApprovalHighWaterV1(path) as reopened:
        assert reopened.current(first.approval_id) == first


def test_same_state_can_advance_observed_time_but_clock_rollback_fails_closed(
    tmp_path: Path,
) -> None:
    path = store_path(tmp_path)
    first = authority_state()
    later = replace(first, observed_at=LATER)
    with SQLiteNativeIMSandboxApprovalHighWaterV1(path) as store:
        store.observe(first)
        assert store.observe(later) == later
        assert store.current(first.approval_id) == later
        with pytest.raises(NativeIMSandboxApprovalStoreError) as raised:
            store.observe(first)
        assert raised.value.code == "native_im_sandbox_approval_store_clock_rollback"


@pytest.mark.parametrize(
    ("changed", "expected_code"),
    (
        (
            {"authority_revision": 6, "observed_at": LATER},
            "native_im_sandbox_approval_store_revision_rollback",
        ),
        (
            {"approval_digest": "b" * 64, "observed_at": LATER},
            "native_im_sandbox_approval_store_equivocation",
        ),
        (
            {"state": "revoked", "observed_at": LATER},
            "native_im_sandbox_approval_store_equivocation",
        ),
        (
            {"authority_revision": 8, "observed_at": LATER},
            "native_im_sandbox_approval_store_renewal_requires_new_id",
        ),
        (
            {
                "authority_revision": 8,
                "approval_digest": "b" * 64,
                "observed_at": LATER,
            },
            "native_im_sandbox_approval_store_record_changed",
        ),
    ),
)
def test_store_rejects_revision_rollback_equivocation_and_same_id_renewal(
    tmp_path: Path,
    changed: dict[str, object],
    expected_code: str,
) -> None:
    path = store_path(tmp_path)
    first = authority_state()
    with SQLiteNativeIMSandboxApprovalHighWaterV1(path) as store:
        store.observe(first)
        with pytest.raises(NativeIMSandboxApprovalStoreError) as raised:
            store.observe(replace(first, **changed))
        assert raised.value.code == expected_code


def test_higher_revocation_is_terminal_and_cannot_be_reactivated_or_replaced(
    tmp_path: Path,
) -> None:
    path = store_path(tmp_path)
    approved = authority_state()
    revoked = replace(
        approved,
        authority_revision=8,
        state="revoked",
        observed_at=LATER,
    )
    with SQLiteNativeIMSandboxApprovalHighWaterV1(path) as store:
        store.observe(approved)
        assert store.observe(revoked) == revoked
        assert store.observe(revoked) == revoked
        for attempted in (
            replace(revoked, authority_revision=9, observed_at="2026-08-28T12:00:02.000001Z"),
            replace(revoked, state="approved", observed_at="2026-08-28T12:00:02.000001Z"),
            replace(
                revoked,
                approval_digest="b" * 64,
                observed_at="2026-08-28T12:00:02.000001Z",
            ),
        ):
            with pytest.raises(NativeIMSandboxApprovalStoreError) as raised:
                store.observe(attempted)
            assert raised.value.code == "native_im_sandbox_approval_store_terminal_revoked"

    with SQLiteNativeIMSandboxApprovalHighWaterV1(path) as reopened:
        with pytest.raises(NativeIMSandboxApprovalStoreError) as raised:
            reopened.observe(
                replace(
                    revoked,
                    authority_revision=9,
                    observed_at="2026-08-28T12:00:03.000001Z",
                )
            )
        assert raised.value.code == "native_im_sandbox_approval_store_terminal_revoked"


def test_renewal_with_a_new_approval_id_has_an_independent_high_water(tmp_path: Path) -> None:
    path = store_path(tmp_path)
    first = authority_state()
    renewed = replace(
        first,
        approval_id="test-native-im-approval-renewed",
        approval_digest="b" * 64,
        authority_revision=1,
        observed_at=LATER,
    )
    with SQLiteNativeIMSandboxApprovalHighWaterV1(path) as store:
        store.observe(first)
        store.observe(renewed)
        assert store.current(first.approval_id) == first
        assert store.current(renewed.approval_id) == renewed


def test_two_connections_serialize_same_exact_observation(tmp_path: Path) -> None:
    path = store_path(tmp_path)
    first = SQLiteNativeIMSandboxApprovalHighWaterV1(path)
    second = SQLiteNativeIMSandboxApprovalHighWaterV1(path)
    barrier = threading.Barrier(2)
    value = authority_state()

    def observe(store: SQLiteNativeIMSandboxApprovalHighWaterV1) -> object:
        barrier.wait(timeout=2)
        return store.observe(value)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(executor.map(observe, (first, second)))
        assert results == (value, value)
        assert first.current(value.approval_id) == value
        assert second.current(value.approval_id) == value
    finally:
        first.close()
        second.close()


def test_admission_guard_holds_durable_lock_against_cross_connection_revocation(
    tmp_path: Path,
) -> None:
    path = store_path(tmp_path)
    admission_store = SQLiteNativeIMSandboxApprovalHighWaterV1(path)
    revocation_store = SQLiteNativeIMSandboxApprovalHighWaterV1(path)
    approved = authority_state()
    revoked = replace(
        approved,
        authority_revision=8,
        state="revoked",
        observed_at=LATER,
    )
    started = threading.Event()
    completed = threading.Event()

    def revoke() -> None:
        started.set()
        revocation_store.observe(revoked)
        completed.set()

    try:
        with admission_store.admission_guard(approved) as guarded:
            thread = threading.Thread(target=revoke)
            thread.start()
            assert started.wait(timeout=2)
            assert completed.wait(timeout=0.05) is False
            assert guarded == approved
        thread.join(timeout=2)
        assert completed.is_set()
        assert admission_store.current(approved.approval_id) == revoked
    finally:
        admission_store.close()
        revocation_store.close()


def test_admission_guard_rejects_revoked_input_and_rolls_back_failed_section(
    tmp_path: Path,
) -> None:
    path = store_path(tmp_path)
    approved = authority_state()
    revoked = replace(approved, state="revoked")
    with SQLiteNativeIMSandboxApprovalHighWaterV1(path) as store:
        with pytest.raises(NativeIMSandboxApprovalStoreError) as state_error:
            with store.admission_guard(revoked):
                pass
        assert (
            state_error.value.code
            == "native_im_sandbox_approval_store_admission_state_forbidden"
        )

        with pytest.raises(RuntimeError, match="admission-body-failed"):
            with store.admission_guard(approved):
                raise RuntimeError("admission-body-failed")
        assert store.current(approved.approval_id) is None


def test_store_rejects_weak_permissions_symlink_schema_trigger_and_tampered_row(
    tmp_path: Path,
) -> None:
    broad = tmp_path / "broad.sqlite3"
    broad.touch(mode=0o600)
    broad.chmod(0o644)
    with pytest.raises(NativeIMSandboxApprovalStoreIntegrityError) as permissions:
        SQLiteNativeIMSandboxApprovalHighWaterV1(str(broad.resolve()))
    assert permissions.value.code == "native_im_sandbox_approval_store_permissions_too_broad"

    target = tmp_path / "target.sqlite3"
    target.touch(mode=0o600)
    link = tmp_path / "link.sqlite3"
    link.symlink_to(target)
    with pytest.raises(NativeIMSandboxApprovalStoreIntegrityError) as symlink:
        SQLiteNativeIMSandboxApprovalHighWaterV1(str(link.absolute()))
    assert symlink.value.code == "native_im_sandbox_approval_store_symlink_forbidden"

    weak = tmp_path / "weak.sqlite3"
    connection = sqlite3.connect(weak)
    connection.execute(
        "CREATE TABLE qe_native_im_sandbox_approval_high_water (approval_id TEXT)"
    )
    connection.commit()
    connection.close()
    weak.chmod(0o600)
    with pytest.raises(NativeIMSandboxApprovalStoreIntegrityError) as schema:
        SQLiteNativeIMSandboxApprovalHighWaterV1(str(weak.resolve()))
    assert schema.value.code == "native_im_sandbox_approval_store_schema_mismatch"

    path = store_path(tmp_path)
    store = SQLiteNativeIMSandboxApprovalHighWaterV1(path)
    store.observe(authority_state())
    attacker = sqlite3.connect(path)
    attacker.execute("PRAGMA ignore_check_constraints=ON")
    attacker.execute(
        """
        UPDATE qe_native_im_sandbox_approval_high_water
        SET state_digest = 'ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff'
        """
    )
    attacker.commit()
    attacker.close()
    with pytest.raises(NativeIMSandboxApprovalStoreIntegrityError) as row:
        store.current("test-native-im-approval")
    assert row.value.code == "native_im_sandbox_approval_store_row_integrity_failed"
    store.close()

    clean = str((tmp_path / "trigger.sqlite3").resolve())
    trigger_store = SQLiteNativeIMSandboxApprovalHighWaterV1(clean)
    trigger_attacker = sqlite3.connect(clean)
    trigger_attacker.execute(
        """
        CREATE TRIGGER qe_native_im_approval_attack
        AFTER INSERT ON qe_native_im_sandbox_approval_high_water
        BEGIN
            DELETE FROM qe_native_im_sandbox_approval_high_water;
        END
        """
    )
    trigger_attacker.commit()
    trigger_attacker.close()
    with pytest.raises(NativeIMSandboxApprovalStoreIntegrityError) as trigger:
        trigger_store.observe(authority_state())
    assert trigger.value.code == "native_im_sandbox_approval_store_schema_extension_forbidden"
    trigger_store.close()


def test_store_is_process_bound_noncopyable_nonserializable_and_close_is_idempotent(
    tmp_path: Path,
) -> None:
    store = SQLiteNativeIMSandboxApprovalHighWaterV1(store_path(tmp_path))
    for operation in (copy.copy, copy.deepcopy, pickle.dumps):
        with pytest.raises(TypeError):
            operation(store)
    store.close()
    store.close()
    assert store.closed is True
    with pytest.raises(NativeIMSandboxApprovalStoreError) as raised:
        store.current("test-native-im-approval")
    assert raised.value.code == "native_im_sandbox_approval_store_closed"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_actual_fork_rejects_inherited_store_before_touching_sqlite(tmp_path: Path) -> None:
    store = SQLiteNativeIMSandboxApprovalHighWaterV1(store_path(tmp_path))
    store.observe(authority_state())
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - exercised in child process
        os.close(read_fd)
        try:
            store.current("test-native-im-approval")
        except NativeIMSandboxApprovalStoreError as error:
            payload = error.code.encode()
        except BaseException:
            payload = b"unexpected"
        else:
            payload = b"allowed"
        os.write(write_fd, payload)
        os.close(write_fd)
        os._exit(0)

    os.close(write_fd)
    try:
        payload = os.read(read_fd, 256)
    finally:
        os.close(read_fd)
        _, status = os.waitpid(pid, 0)
    assert os.WIFEXITED(status)
    assert payload == b"native_im_sandbox_approval_store_process_mismatch"
    assert store.current("test-native-im-approval") == authority_state()
    store.close()


def test_repr_and_failures_do_not_render_path_approval_id_or_digest(tmp_path: Path) -> None:
    path = store_path(tmp_path)
    store = SQLiteNativeIMSandboxApprovalHighWaterV1(path)
    rendered = repr(store)
    assert path not in rendered
    assert "test-native-im-approval" not in rendered
    assert DIGEST not in rendered

    canary = "approval-id-secret-canary"
    with pytest.raises(NativeIMSandboxApprovalStoreError) as raised:
        store.current(canary + "\n")
    assert canary not in f"{raised.value!r} {raised.value}"
    store.close()
