from __future__ import annotations

import copy
import importlib
import importlib.util
import multiprocessing
import os
import pickle
import select
import signal
import threading
import time
import unittest
from collections.abc import Callable
from unittest import mock

from quantum_entanglement import process_identity


class _ProcessMismatchError(RuntimeError):
    pass


def _mismatch_error() -> BaseException:
    return _ProcessMismatchError("process_owner_mismatch")


def _owner_outcome(owner: object) -> bytes:
    try:
        process_identity.require_current_process(owner, _mismatch_error)
    except _ProcessMismatchError as exc:
        if (
            exc.args == ("process_owner_mismatch",)
            and exc.__cause__ is None
            and exc.__context__ is None
        ):
            return b"rejected"
        return b"unsafe-error"
    except BaseException:
        return b"wrong-error"
    return b"accepted"


def _wait_for_child(pid: int, timeout: float) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        waited_pid, status = os.waitpid(pid, os.WNOHANG)
        if waited_pid == pid:
            return status
        time.sleep(0.01)
    os.kill(pid, signal.SIGKILL)
    _, status = os.waitpid(pid, 0)
    raise AssertionError(f"fork child {pid} exceeded {timeout:.1f}s (status={status})")


def _run_fork_child(callback: Callable[[], bytes], timeout: float = 3.0) -> bytes:
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        try:
            payload = callback()
            if type(payload) is not bytes:
                payload = b"invalid-payload"
        except BaseException:
            payload = b"child-error"
        try:
            os.write(write_fd, payload[:4096])
        finally:
            os.close(write_fd)
        os._exit(0)

    os.close(write_fd)
    try:
        ready, _, _ = select.select((read_fd,), (), (), timeout)
        if not ready:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            raise AssertionError(f"fork child {pid} produced no result within {timeout:.1f}s")
        payload = os.read(read_fd, 4096)
        status = _wait_for_child(pid, timeout)
    finally:
        os.close(read_fd)
    if not os.WIFEXITED(status) or os.WEXITSTATUS(status) != 0:
        raise AssertionError(f"fork child {pid} failed with status {status}")
    return payload


def _fresh_process_probe(connection: multiprocessing.connection.Connection) -> None:
    try:
        owner = process_identity.capture_process_owner()
        process_identity.require_current_process(owner, _mismatch_error)
        pid, _ = process_identity.current_process_identity()
        connection.send(("ok", pid))
    except BaseException:
        connection.send(("error", 0))
    finally:
        connection.close()


@unittest.skipUnless(hasattr(os, "fork"), "requires POSIX fork")
class ProcessIdentityForkTests(unittest.TestCase):
    def test_pid_fallback_survives_at_fork_registration_failure(self) -> None:
        with mock.patch.object(
            os,
            "register_at_fork",
            side_effect=RuntimeError("synthetic registration failure"),
        ):
            spec = importlib.util.spec_from_file_location(
                "quantum_entanglement._process_identity_registration_failure_probe",
                process_identity.__file__,
            )
            if spec is None or spec.loader is None:
                self.fail("process identity module spec is unavailable")
            fallback_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(fallback_module)

        owner = fallback_module.capture_process_owner()

        def child_probe() -> bytes:
            try:
                fallback_module.require_current_process(owner, _mismatch_error)
            except _ProcessMismatchError as exc:
                if exc.__cause__ is None and exc.__context__ is None:
                    return b"rejected"
                return b"unsafe-error"
            except BaseException:
                return b"wrong-error"
            return b"accepted"

        self.assertFalse(fallback_module._AT_FORK_REGISTERED)
        self.assertEqual(_run_fork_child(child_probe), b"rejected")
        fallback_module.require_current_process(owner, _mismatch_error)

    def test_registered_child_hook_rotates_identity_before_first_guard(self) -> None:
        if not process_identity._AT_FORK_REGISTERED:
            self.skipTest("os.register_at_fork is unavailable or registration failed")
        parent_pid, parent_epoch = process_identity.current_process_identity()

        def child_probe() -> bytes:
            child_pid, child_epoch = process_identity._PROCESS_IDENTITY
            if (
                child_pid == os.getpid()
                and child_pid != parent_pid
                and child_epoch is not parent_epoch
            ):
                return b"rotated"
            return b"not-rotated"

        self.assertEqual(_run_fork_child(child_probe), b"rotated")

    def test_inherited_owner_is_rejected_and_parent_remains_current(self) -> None:
        owner = process_identity.capture_process_owner()

        self.assertEqual(_run_fork_child(lambda: _owner_outcome(owner)), b"rejected")
        process_identity.require_current_process(owner, _mismatch_error)

    def test_child_can_capture_a_fresh_owner_after_rejecting_parent_owner(self) -> None:
        parent_owner = process_identity.capture_process_owner()

        def child_probe() -> bytes:
            inherited = _owner_outcome(parent_owner)
            child_owner = process_identity.capture_process_owner()
            fresh = _owner_outcome(child_owner)
            return inherited + b":" + fresh

        self.assertEqual(_run_fork_child(child_probe), b"rejected:accepted")
        process_identity.require_current_process(parent_owner, _mismatch_error)

    def test_guard_does_not_wait_for_a_lock_held_by_a_vanished_thread(self) -> None:
        owner = process_identity.capture_process_owner()
        unrelated_lock = threading.Lock()
        lock_held = threading.Event()
        release_lock = threading.Event()

        def lock_holder() -> None:
            with unrelated_lock:
                lock_held.set()
                release_lock.wait(5.0)

        thread = threading.Thread(target=lock_holder)
        thread.start()
        self.assertTrue(lock_held.wait(1.0))
        try:
            self.assertEqual(
                _run_fork_child(lambda: _owner_outcome(owner), timeout=2.0),
                b"rejected",
            )
        finally:
            release_lock.set()
            thread.join(2.0)
        self.assertFalse(thread.is_alive())
        process_identity.require_current_process(owner, _mismatch_error)

    def test_every_fork_generation_gets_a_distinct_epoch(self) -> None:
        parent_owner = process_identity.capture_process_owner()

        def child_probe() -> bytes:
            if _owner_outcome(parent_owner) != b"rejected":
                return b"parent-owner-not-rejected-by-child"
            child_owner = process_identity.capture_process_owner()
            if _owner_outcome(child_owner) != b"accepted":
                return b"child-owner-not-current"

            def grandchild_probe() -> bytes:
                parent_result = _owner_outcome(parent_owner)
                child_result = _owner_outcome(child_owner)
                grandchild_owner = process_identity.capture_process_owner()
                grandchild_result = _owner_outcome(grandchild_owner)
                return b":".join((parent_result, child_result, grandchild_result))

            return _run_fork_child(grandchild_probe)

        self.assertEqual(
            _run_fork_child(child_probe, timeout=5.0),
            b"rejected:rejected:accepted",
        )
        process_identity.require_current_process(parent_owner, _mismatch_error)


class ProcessIdentityTests(unittest.TestCase):
    def test_identity_is_stable_within_one_process(self) -> None:
        first = process_identity.current_process_identity()
        second = process_identity.current_process_identity()

        self.assertIs(first, second)
        self.assertIs(type(first), tuple)
        self.assertIs(type(first[0]), int)
        self.assertGreater(first[0], 0)
        self.assertIs(first[1], second[1])

    def test_owner_and_epoch_representations_are_opaque(self) -> None:
        owner = process_identity.capture_process_owner()
        pid, epoch = process_identity.current_process_identity()

        self.assertEqual(str(owner), "ProcessOwner<opaque>")
        self.assertEqual(repr(owner), "ProcessOwner(<opaque>)")
        self.assertEqual(str(epoch), "ProcessEpoch<opaque>")
        self.assertEqual(repr(epoch), "ProcessEpoch(<opaque>)")
        self.assertNotIn(str(pid), repr(owner))

    def test_owner_rejects_copy_deepcopy_and_pickle(self) -> None:
        owner = process_identity.capture_process_owner()

        with self.assertRaisesRegex(TypeError, "cannot be copied"):
            copy.copy(owner)
        with self.assertRaisesRegex(TypeError, "cannot be copied"):
            copy.deepcopy(owner)
        with self.assertRaisesRegex(TypeError, "cannot be serialized"):
            pickle.dumps(owner)

    def test_epoch_prevents_identity_serialization(self) -> None:
        identity = process_identity.current_process_identity()

        with self.assertRaisesRegex(TypeError, "cannot be copied"):
            copy.deepcopy(identity)
        with self.assertRaisesRegex(TypeError, "cannot be serialized"):
            pickle.dumps(identity)

    def test_pid_drift_fallback_rotates_epoch_without_at_fork_hook(self) -> None:
        original_pid, original_epoch = process_identity.current_process_identity()
        original_owner = process_identity.capture_process_owner()
        drift_pid = original_pid + 100_000

        with mock.patch.object(process_identity, "_read_current_pid", return_value=drift_pid):
            drift_identity = process_identity.current_process_identity()
            drift_owner = process_identity.capture_process_owner()
            self.assertEqual(drift_identity[0], drift_pid)
            self.assertIsNot(drift_identity[1], original_epoch)
            self.assertEqual(_owner_outcome(original_owner), b"rejected")
            self.assertEqual(_owner_outcome(drift_owner), b"accepted")

        restored_identity = process_identity.current_process_identity()
        self.assertEqual(restored_identity[0], original_pid)
        self.assertIsNot(restored_identity[1], drift_identity[1])
        self.assertEqual(_owner_outcome(original_owner), b"rejected")
        self.assertEqual(_owner_outcome(drift_owner), b"rejected")

    def test_epoch_rotation_rejects_owner_even_when_pid_is_unchanged(self) -> None:
        owner = process_identity.capture_process_owner()
        pid, epoch = process_identity.current_process_identity()

        process_identity._after_fork_in_child()

        next_pid, next_epoch = process_identity.current_process_identity()
        self.assertEqual(next_pid, pid)
        self.assertIsNot(next_epoch, epoch)
        self.assertEqual(_owner_outcome(owner), b"rejected")
        process_identity.require_current_process(
            process_identity.capture_process_owner(),
            _mismatch_error,
        )

    def test_invalid_or_uninitialized_descriptors_fail_with_clean_error(self) -> None:
        uninitialized_owner = process_identity._ProcessOwner.__new__(process_identity._ProcessOwner)
        invalid_owners = (object(), None, uninitialized_owner)

        for owner in invalid_owners:
            with self.subTest(owner_type=type(owner).__name__):
                with self.assertRaises(_ProcessMismatchError) as caught:
                    process_identity.require_current_process(owner, _mismatch_error)
                self.assertEqual(caught.exception.args, ("process_owner_mismatch",))
                self.assertIsNone(caught.exception.__cause__)
                self.assertIsNone(caught.exception.__context__)

    def test_error_factory_is_not_touched_for_current_owner(self) -> None:
        owner = process_identity.capture_process_owner()

        def forbidden_factory() -> BaseException:
            raise AssertionError("current owner must not invoke mismatch factory")

        process_identity.require_current_process(owner, forbidden_factory)

    def test_error_factory_must_return_an_exception(self) -> None:
        with self.assertRaisesRegex(TypeError, "must return an exception"):
            process_identity.require_current_process(object(), lambda: object())  # type: ignore[arg-type]

    def test_reload_rotates_epoch_and_invalidates_pre_reload_owner(self) -> None:
        owner = process_identity.capture_process_owner()
        pid, epoch = process_identity.current_process_identity()

        reloaded = importlib.reload(process_identity)

        next_pid, next_epoch = reloaded.current_process_identity()
        self.assertEqual(next_pid, pid)
        self.assertIsNot(next_epoch, epoch)
        self.assertEqual(_owner_outcome(owner), b"rejected")
        reloaded.require_current_process(reloaded.capture_process_owner(), _mismatch_error)

    def test_spawn_style_processes_construct_fresh_identity(self) -> None:
        available = set(multiprocessing.get_all_start_methods())
        methods = [method for method in ("spawn", "forkserver") if method in available]
        if not methods:
            self.skipTest("spawn and forkserver are unavailable")

        parent_pid = os.getpid()
        for method in methods:
            with self.subTest(method=method):
                context = multiprocessing.get_context(method)
                parent_connection, child_connection = context.Pipe(duplex=False)
                process = context.Process(target=_fresh_process_probe, args=(child_connection,))
                process.start()
                child_connection.close()
                try:
                    self.assertTrue(parent_connection.poll(8.0), f"{method} child timed out")
                    outcome, child_pid = parent_connection.recv()
                finally:
                    parent_connection.close()
                    process.join(8.0)
                    if process.is_alive():
                        process.kill()
                        process.join(2.0)
                self.assertEqual(process.exitcode, 0)
                self.assertEqual(outcome, "ok")
                self.assertNotEqual(child_pid, parent_pid)


if __name__ == "__main__":
    unittest.main()
