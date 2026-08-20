# ruff: noqa: UP007 -- PEP 604 syntax is not parseable on supported Python 3.9.
"""Lock-free ownership identity for process-bound runtime objects.

The helpers in this module only make inherited instances fail closed. They do not make
SQLite connections, locks, event loops, providers, or secret material safe to inherit.
Process-bound components must capture their owner during construction and run the guard
before touching any inherited dependency.
"""

from __future__ import annotations

import os
from typing import Callable, NoReturn, SupportsIndex

__all__ = (
    "capture_process_owner",
    "current_process_identity",
    "require_current_process",
)


_CONSTRUCTION_TOKEN = object()


class _ProcessEpoch:
    """Opaque, identity-compared generation marker for one process lifetime."""

    __slots__ = ()

    def __init__(self, token: object) -> None:
        if token is not _CONSTRUCTION_TOKEN:
            raise TypeError("process epochs are created internally")

    def __str__(self) -> str:
        return "ProcessEpoch<opaque>"

    def __repr__(self) -> str:
        return "ProcessEpoch(<opaque>)"

    def __copy__(self) -> NoReturn:
        raise TypeError("process epochs cannot be copied")

    def __deepcopy__(self, memo: object) -> NoReturn:
        raise TypeError("process epochs cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("process epochs cannot be serialized")

    def __reduce_ex__(self, protocol: SupportsIndex) -> NoReturn:
        raise TypeError("process epochs cannot be serialized")


class _ProcessOwner:
    """Opaque descriptor captured by a process-bound instance constructor."""

    __slots__ = ("__epoch", "__pid")

    def __init__(self, pid: int, epoch: _ProcessEpoch, token: object) -> None:
        if (
            token is not _CONSTRUCTION_TOKEN
            or type(pid) is not int
            or type(epoch) is not _ProcessEpoch
        ):
            raise TypeError("process owners are captured internally")
        self.__pid = pid
        self.__epoch = epoch

    def __str__(self) -> str:
        return "ProcessOwner<opaque>"

    def __repr__(self) -> str:
        return "ProcessOwner(<opaque>)"

    def __copy__(self) -> NoReturn:
        raise TypeError("process owners cannot be copied")

    def __deepcopy__(self, memo: object) -> NoReturn:
        raise TypeError("process owners cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("process owners cannot be serialized")

    def __reduce_ex__(self, protocol: SupportsIndex) -> NoReturn:
        raise TypeError("process owners cannot be serialized")


def _read_current_pid() -> int:
    pid = os.getpid()
    if type(pid) is not int or pid <= 0:
        raise RuntimeError("process_identity_unavailable")
    return pid


def _new_epoch() -> _ProcessEpoch:
    return _ProcessEpoch(_CONSTRUCTION_TOKEN)


_PROCESS_IDENTITY = (_read_current_pid(), _new_epoch())


def _after_fork_in_child() -> None:
    """Rotate the child identity without traversing instances or acquiring a lock."""

    global _PROCESS_IDENTITY
    _PROCESS_IDENTITY = (_read_current_pid(), _new_epoch())


_previous_at_fork_registration = globals().get("_AT_FORK_REGISTERED") is True
_AT_FORK_REGISTERED = _previous_at_fork_registration
if not _AT_FORK_REGISTERED:
    _register_at_fork = getattr(os, "register_at_fork", None)
    if _register_at_fork is not None:
        try:
            _register_at_fork(after_in_child=_after_fork_in_child)
        except Exception:
            # PID drift remains an independent fail-closed fallback when registration is
            # unavailable or rejected by the interpreter/platform.
            pass
        else:
            _AT_FORK_REGISTERED = True


def current_process_identity() -> tuple[int, _ProcessEpoch]:
    """Return the current PID and opaque epoch, rotating on unannounced PID drift."""

    global _PROCESS_IDENTITY
    pid = _read_current_pid()
    identity = _PROCESS_IDENTITY
    if pid != identity[0]:
        identity = (pid, _new_epoch())
        _PROCESS_IDENTITY = identity
    return identity


def capture_process_owner() -> _ProcessOwner:
    """Capture an opaque descriptor for a newly constructed process-bound instance."""

    pid, epoch = current_process_identity()
    return _ProcessOwner(pid, epoch, _CONSTRUCTION_TOKEN)


def _raise_process_mismatch(error_factory: Callable[[], BaseException]) -> NoReturn:
    error = error_factory()
    if not isinstance(error, BaseException):
        raise TypeError("process mismatch error factory must return an exception")
    raise error from None


def require_current_process(
    owner: object,
    error_factory: Callable[[], BaseException],
) -> None:
    """Reject an owner captured outside the exact current PID and process epoch.

    The factory normally creates a module-private signal. A component that can call this
    helper while another exception is active must cleanly translate that signal after
    leaving its ``except`` block; ``raise ... from None`` suppresses display but cannot
    erase Python's active exception context.
    """

    owner_pid: object = None
    owner_epoch: object = None
    if type(owner) is _ProcessOwner:
        try:
            owner_pid = object.__getattribute__(owner, "_ProcessOwner__pid")
            owner_epoch = object.__getattribute__(owner, "_ProcessOwner__epoch")
        except AttributeError:
            # A descriptor bypassing the private constructor is invalid. Leave the
            # exception handler before raising the caller's stable public error so it
            # cannot become that error's implicit context.
            owner_pid = None
            owner_epoch = None

    pid, epoch = current_process_identity()
    if type(owner_pid) is not int or owner_pid != pid or owner_epoch is not epoch:
        _raise_process_mismatch(error_factory)
