"""Typed, body-free operational observation for native-IM E2 sandbox lifecycle."""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import NoReturn, SupportsIndex

from .service.logging import (
    LogEventSchema,
    LogField,
    LogFieldKind,
    SafeLogCatalog,
    SafeLogger,
)

_LIFECYCLE_STATES = ("closed", "draining", "failed", "ready", "starting", "stopped")
_HEALTH_OUTCOMES = ("failure", "success")
_READ_OUTCOMES = (
    "contract_failure",
    "fresh_observation",
    "kill_switch",
    "observed_replay",
    "rejected",
)
_KILL_REASONS = (
    "canary_detected",
    "contract_failure",
    "health_failed",
    "manual",
    "shutdown",
)


class NativeIMSandboxObservationProcessMismatchError(RuntimeError):
    code = "native_im_sandbox_observation_process_mismatch"

    def __init__(self) -> None:
        super().__init__(self.code)


@dataclass(frozen=True)
class NativeIMSandboxMetricsSnapshotV1:
    health_success_count: int
    health_failure_count: int
    read_fresh_count: int
    read_replay_count: int
    read_rejected_count: int
    events_admitted_count: int
    kill_switch_trip_count: int

    def __post_init__(self) -> None:
        if type(self) is not NativeIMSandboxMetricsSnapshotV1:
            raise TypeError("native IM metrics snapshot requires the exact V1 class")
        for value in (
            self.health_success_count,
            self.health_failure_count,
            self.read_fresh_count,
            self.read_replay_count,
            self.read_rejected_count,
            self.events_admitted_count,
            self.kill_switch_trip_count,
        ):
            if type(value) is not int or value < 0 or value > 2**63 - 1:
                raise TypeError("native IM metric must be a non-negative signed-64 integer")


class NativeIMSandboxMetricsV1:
    """Process-local counters with no caller-provided labels or identifiers."""

    __slots__ = (
        "__events_admitted_count",
        "__health_failure_count",
        "__health_success_count",
        "__kill_switch_trip_count",
        "__lock",
        "__process_id",
        "__read_fresh_count",
        "__read_rejected_count",
        "__read_replay_count",
    )

    def __init__(self) -> None:
        self.__process_id = os.getpid()
        self.__lock = threading.Lock()
        self.__health_success_count = 0
        self.__health_failure_count = 0
        self.__read_fresh_count = 0
        self.__read_replay_count = 0
        self.__read_rejected_count = 0
        self.__events_admitted_count = 0
        self.__kill_switch_trip_count = 0

    def _require_current_process(self) -> None:
        if os.getpid() != self.__process_id:
            raise NativeIMSandboxObservationProcessMismatchError() from None

    def record_health(self, outcome: str) -> None:
        self._require_current_process()
        if type(outcome) is not str or outcome not in _HEALTH_OUTCOMES:
            raise ValueError("native IM health outcome is invalid")
        with self.__lock:
            if outcome == "success":
                self.__health_success_count += 1
            else:
                self.__health_failure_count += 1

    def record_read(self, outcome: str, event_count: int) -> None:
        self._require_current_process()
        if type(outcome) is not str or outcome not in _READ_OUTCOMES:
            raise ValueError("native IM read outcome is invalid")
        if type(event_count) is not int or not 0 <= event_count <= 1_000:
            raise ValueError("native IM read event count is invalid")
        with self.__lock:
            if outcome == "fresh_observation":
                self.__read_fresh_count += 1
                self.__events_admitted_count += event_count
            elif outcome == "observed_replay":
                self.__read_replay_count += 1
            else:
                self.__read_rejected_count += 1

    def record_kill_switch_trip(self) -> None:
        self._require_current_process()
        with self.__lock:
            self.__kill_switch_trip_count += 1

    def snapshot(self) -> NativeIMSandboxMetricsSnapshotV1:
        self._require_current_process()
        with self.__lock:
            return NativeIMSandboxMetricsSnapshotV1(
                health_success_count=self.__health_success_count,
                health_failure_count=self.__health_failure_count,
                read_fresh_count=self.__read_fresh_count,
                read_replay_count=self.__read_replay_count,
                read_rejected_count=self.__read_rejected_count,
                events_admitted_count=self.__events_admitted_count,
                kill_switch_trip_count=self.__kill_switch_trip_count,
            )

    def __repr__(self) -> str:
        return "NativeIMSandboxMetricsV1(labels=0)"

    def __reduce_ex__(self, protocol: SupportsIndex) -> NoReturn:
        raise TypeError("native IM sandbox metrics cannot be serialized")


def native_im_sandbox_log_catalog_v1() -> SafeLogCatalog:
    """Build the exact source-defined E2 event and field allowlist."""

    return SafeLogCatalog(
        (
            LogEventSchema(
                event_code="qe.native_im.lifecycle",
                level=logging.INFO,
                fields=(
                    LogField(
                        name="state",
                        kind=LogFieldKind.CODE,
                        allowed_codes=_LIFECYCLE_STATES,
                    ),
                    LogField(name="ready", kind=LogFieldKind.BOOLEAN),
                    LogField(name="kill_switch_tripped", kind=LogFieldKind.BOOLEAN),
                ),
            ),
            LogEventSchema(
                event_code="qe.native_im.health",
                level=logging.INFO,
                fields=(
                    LogField(
                        name="outcome",
                        kind=LogFieldKind.CODE,
                        allowed_codes=_HEALTH_OUTCOMES,
                    ),
                ),
            ),
            LogEventSchema(
                event_code="qe.native_im.read",
                level=logging.INFO,
                fields=(
                    LogField(
                        name="outcome",
                        kind=LogFieldKind.CODE,
                        allowed_codes=_READ_OUTCOMES,
                    ),
                    LogField(name="event_count", kind=LogFieldKind.COUNT),
                    LogField(name="trace_present", kind=LogFieldKind.BOOLEAN),
                ),
            ),
            LogEventSchema(
                event_code="qe.native_im.kill_switch",
                level=logging.WARNING,
                fields=(
                    LogField(
                        name="reason",
                        kind=LogFieldKind.CODE,
                        allowed_codes=_KILL_REASONS,
                    ),
                ),
            ),
        )
    )


class NativeIMSandboxObserverV1:
    """The only lifecycle logging/metrics surface; it accepts no free-form content."""

    __slots__ = ("__logger", "__metrics", "__process_id")

    def __init__(self, logger: SafeLogger, metrics: NativeIMSandboxMetricsV1) -> None:
        if type(logger) is not SafeLogger:
            raise TypeError("native IM observer requires the exact safe logger")
        if type(metrics) is not NativeIMSandboxMetricsV1:
            raise TypeError("native IM observer requires the exact metrics registry")
        self.__process_id = os.getpid()
        self.__logger = logger
        self.__metrics = metrics

    def _require_current_process(self) -> None:
        if os.getpid() != self.__process_id:
            raise NativeIMSandboxObservationProcessMismatchError() from None

    def lifecycle(self, state: str, *, ready: bool, kill_switch_tripped: bool) -> bool:
        self._require_current_process()
        if type(state) is not str or state not in _LIFECYCLE_STATES:
            raise ValueError("native IM lifecycle observation state is invalid")
        if type(ready) is not bool or type(kill_switch_tripped) is not bool:
            raise TypeError("native IM lifecycle observation flags must be exact booleans")
        return self.__logger.emit(
            "qe.native_im.lifecycle",
            {
                "state": state,
                "ready": ready,
                "kill_switch_tripped": kill_switch_tripped,
            },
        )

    def health(self, outcome: str) -> bool:
        self._require_current_process()
        self.__metrics.record_health(outcome)
        return self.__logger.emit("qe.native_im.health", {"outcome": outcome})

    def read(self, outcome: str, *, event_count: int, trace_present: bool) -> bool:
        self._require_current_process()
        if type(trace_present) is not bool:
            raise TypeError("native IM trace presence must be an exact boolean")
        self.__metrics.record_read(outcome, event_count)
        return self.__logger.emit(
            "qe.native_im.read",
            {
                "outcome": outcome,
                "event_count": event_count,
                "trace_present": trace_present,
            },
        )

    def kill_switch(self, reason: str) -> bool:
        self._require_current_process()
        if type(reason) is not str or reason not in _KILL_REASONS:
            raise ValueError("native IM kill-switch observation reason is invalid")
        self.__metrics.record_kill_switch_trip()
        return self.__logger.emit("qe.native_im.kill_switch", {"reason": reason})

    def metrics_snapshot(self) -> NativeIMSandboxMetricsSnapshotV1:
        self._require_current_process()
        return self.__metrics.snapshot()

    def __repr__(self) -> str:
        return "NativeIMSandboxObserverV1(typed_events=4, free_form_fields=0)"


__all__ = [
    "NativeIMSandboxMetricsSnapshotV1",
    "NativeIMSandboxMetricsV1",
    "NativeIMSandboxObservationProcessMismatchError",
    "NativeIMSandboxObserverV1",
    "native_im_sandbox_log_catalog_v1",
]
