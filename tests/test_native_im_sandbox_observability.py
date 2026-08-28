from __future__ import annotations

import json
import logging
import pickle
from collections.abc import Callable
from typing import cast
from unittest.mock import patch

import pytest

from quantum_entanglement.native_im_sandbox_observability import (
    NativeIMSandboxMetricsV1,
    NativeIMSandboxObservationProcessMismatchError,
    NativeIMSandboxObserverV1,
    native_im_sandbox_log_catalog_v1,
)
from quantum_entanglement.service.logging import SafeLogger


class CapturingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


class FailingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        raise RuntimeError("handler-message-body-secret-canary")


def observer(
    handler: logging.Handler,
) -> tuple[NativeIMSandboxObserverV1, NativeIMSandboxMetricsV1]:
    logger = logging.Logger("qe-native-im-sandbox-test", level=logging.DEBUG)
    logger.propagate = False
    logger.addHandler(handler)
    metrics = NativeIMSandboxMetricsV1()
    value = NativeIMSandboxObserverV1(
        SafeLogger(logger, native_im_sandbox_log_catalog_v1()),
        metrics,
    )
    return value, metrics


def test_observer_emits_only_fixed_schema_and_boolean_trace_presence() -> None:
    handler = CapturingHandler()
    value, metrics = observer(handler)
    raw_trace = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"

    assert value.lifecycle("starting", ready=False, kill_switch_tripped=False) is True
    assert value.health("success") is True
    assert value.lifecycle("ready", ready=True, kill_switch_tripped=False) is True
    assert value.read("fresh_observation", event_count=3, trace_present=True) is True
    assert value.read("observed_replay", event_count=3, trace_present=False) is True
    assert value.kill_switch("manual") is True

    decoded = [json.loads(message) for message in handler.messages]
    assert [item["event"] for item in decoded] == [
        "qe.native_im.lifecycle",
        "qe.native_im.health",
        "qe.native_im.lifecycle",
        "qe.native_im.read",
        "qe.native_im.read",
        "qe.native_im.kill_switch",
    ]
    assert all(raw_trace not in message for message in handler.messages)
    assert decoded[3]["fields"] == {
        "event_count": 3,
        "outcome": "fresh_observation",
        "trace_present": True,
    }
    snapshot = metrics.snapshot()
    assert snapshot.health_success_count == 1
    assert snapshot.health_failure_count == 0
    assert snapshot.read_fresh_count == 1
    assert snapshot.read_replay_count == 1
    assert snapshot.read_rejected_count == 0
    assert snapshot.events_admitted_count == 3
    assert snapshot.kill_switch_trip_count == 1


def test_canaries_cannot_enter_event_codes_fields_metrics_or_repr() -> None:
    handler = CapturingHandler()
    value, _ = observer(handler)
    canary = "message-body-secret-authorization-bearer-canary"

    calls: tuple[Callable[[], object], ...] = (
        lambda: value.lifecycle(canary, ready=False, kill_switch_tripped=False),
        lambda: value.health(canary),
        lambda: value.read(canary, event_count=0, trace_present=False),
        lambda: value.kill_switch(canary),
    )
    for call in calls:
        with pytest.raises(ValueError):
            call()

    assert handler.messages == []
    assert canary not in repr(value)


def test_wrong_types_and_unbounded_counts_fail_before_logging() -> None:
    handler = CapturingHandler()
    value, _ = observer(handler)
    calls: tuple[Callable[[], object], ...] = (
        lambda: value.lifecycle("ready", ready=cast(bool, 1), kill_switch_tripped=False),
        lambda: value.read("fresh_observation", event_count=True, trace_present=False),
        lambda: value.read("fresh_observation", event_count=1_001, trace_present=False),
        lambda: value.read("fresh_observation", event_count=0, trace_present=cast(bool, 1)),
    )
    for call in calls:
        with pytest.raises((TypeError, ValueError)):
            call()
    assert handler.messages == []


def test_logging_backend_failure_does_not_rollback_bounded_metrics() -> None:
    value, metrics = observer(FailingHandler())
    assert value.health("failure") is False
    assert value.read("contract_failure", event_count=0, trace_present=False) is False
    snapshot = metrics.snapshot()
    assert snapshot.health_failure_count == 1
    assert snapshot.read_rejected_count == 1


def test_observer_and_metrics_are_process_local_and_metrics_are_not_serializable() -> None:
    value, metrics = observer(CapturingHandler())
    with patch(
        "quantum_entanglement.native_im_sandbox_observability.os.getpid",
        return_value=1,
    ):
        with pytest.raises(NativeIMSandboxObservationProcessMismatchError):
            value.health("success")
        with pytest.raises(NativeIMSandboxObservationProcessMismatchError):
            metrics.snapshot()
    with pytest.raises(TypeError):
        pickle.dumps(metrics)
