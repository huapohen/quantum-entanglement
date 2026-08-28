from __future__ import annotations

import asyncio
import json
import logging
import os
import pickle
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from quantum_entanglement.native_im_inbox import (
    NativeIMInboundCheckpointConflictError,
    NativeIMInboundPageAdmissionResultV1,
)
from quantum_entanglement.native_im_nonce_store import SQLiteNativeIMInboxStore
from quantum_entanglement.native_im_sandbox import (
    _APPROVED_COMPOSITION_TOKEN,
    NativeIMInboundOnlySandboxAdapter,
)
from quantum_entanglement.native_im_sandbox_lifecycle import (
    NativeIMKillSwitchSnapshotV1,
    NativeIMKillSwitchTrippedError,
    NativeIMSandboxKillSwitchV1,
    NativeIMSandboxLifecycleError,
    NativeIMSandboxLifecycleV1,
    NativeIMSandboxProcessMismatchError,
)
from quantum_entanglement.native_im_sandbox_observability import (
    NativeIMSandboxMetricsV1,
    NativeIMSandboxObserverV1,
    native_im_sandbox_log_catalog_v1,
)
from quantum_entanglement.service.logging import SafeLogger
from tests.test_native_im_auth import NOW
from tests.test_native_im_sandbox_authority import approved_authority_for
from tests.test_native_im_sandbox_inbound_adapter import (
    FixtureTransport,
    adapter_inputs,
    provider_profile,
)


class BlockingTransport(FixtureTransport):
    def __init__(self, response, *, health_evidence) -> None:
        super().__init__(response, health_evidence=health_evidence)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def read_inbound(self, request, credential):
        self.read_calls += 1
        self.entered.set()
        await self.release.wait()
        assert credential.view().tobytes()
        return self.response


class BlockingCloseTransport(FixtureTransport):
    def __init__(self, response, *, health_evidence) -> None:
        super().__init__(response, health_evidence=health_evidence)
        self.close_entered = asyncio.Event()
        self.close_release = asyncio.Event()

    async def aclose(self) -> None:
        self.close_calls += 1
        self.close_entered.set()
        await self.close_release.wait()


class CapturingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


class FailingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        raise RuntimeError("logging-backend-message-secret-canary")


def sandbox_observer(
    handler: logging.Handler | None = None,
) -> tuple[NativeIMSandboxObserverV1, NativeIMSandboxMetricsV1]:
    logger = logging.Logger("qe-native-im-lifecycle-test", level=logging.DEBUG)
    logger.propagate = False
    logger.addHandler(logging.NullHandler() if handler is None else handler)
    metrics = NativeIMSandboxMetricsV1()
    return (
        NativeIMSandboxObserverV1(
            SafeLogger(logger, native_im_sandbox_log_catalog_v1()),
            metrics,
        ),
        metrics,
    )


def lifecycle_inputs(
    tmp_path: Path,
    *,
    blocking: bool = False,
    blocking_close: bool = False,
    observer: NativeIMSandboxObserverV1 | None = None,
):
    profile = provider_profile()
    store = SQLiteNativeIMInboxStore(
        str(tmp_path / "native-im-lifecycle.sqlite3"),
        profile_revision=profile.revision,
        profile_digest=profile.canonical_digest(),
        clock=lambda: NOW,
    )
    adapter, request, configuration, profile, transport, mapper, secrets, _ = adapter_inputs(
        replay_guard=store
    )
    if blocking or blocking_close:
        blocked = (
            BlockingTransport(
                transport.response,
                health_evidence=transport.health_evidence,
            )
            if blocking
            else BlockingCloseTransport(
                transport.response,
                health_evidence=transport.health_evidence,
            )
        )
        configuration, approval_authority, approval_permit, _, _ = approved_authority_for(
            configuration,
            profile,
        )
        adapter = NativeIMInboundOnlySandboxAdapter(
            configuration,
            profile,
            approval_authority,
            approval_permit,
            "9" * 64,
            blocked,
            mapper,
            secrets,
            store,
            clock=lambda: NOW,
            _composition_token=_APPROVED_COMPOSITION_TOKEN,
        )
        transport = blocked
    kill_switch = NativeIMSandboxKillSwitchV1()
    observer = sandbox_observer()[0] if observer is None else observer
    lifecycle = NativeIMSandboxLifecycleV1(adapter, store, kill_switch, observer)
    return lifecycle, adapter, store, kill_switch, request, transport


@pytest.mark.asyncio
async def test_lifecycle_starts_health_then_atomically_admits_and_replays_page(
    tmp_path: Path,
) -> None:
    lifecycle, _, store, _, request, transport = lifecycle_inputs(tmp_path)
    try:
        status = lifecycle.status()
        assert status.state == "stopped"
        assert status.ready is False
        assert status.approval_current is False

        health = await lifecycle.start()
        assert health.healthy is True
        assert lifecycle.status().ready is True
        assert lifecycle.status().approval_current is True

        fresh = await lifecycle.admit_once(request)
        replay = await lifecycle.admit_once(request)

        assert type(fresh) is NativeIMInboundPageAdmissionResultV1
        assert fresh.disposition == "fresh_observation"
        assert replay.disposition == "observed_replay"
        assert fresh.page_digest == replay.page_digest
        assert fresh.checkpoint == replay.checkpoint
        assert transport.health_calls == 1
        assert transport.read_calls == 2
    finally:
        await lifecycle.aclose()
        store.close()


@pytest.mark.asyncio
async def test_ready_status_fails_closed_immediately_after_approval_revocation(
    tmp_path: Path,
) -> None:
    lifecycle, adapter, store, _, _, _ = lifecycle_inputs(tmp_path)
    authority = object.__getattribute__(
        adapter,
        "_NativeIMInboundOnlySandboxAdapter__approval_authority",
    )
    try:
        await lifecycle.start()
        assert lifecycle.status().ready is True
        authority.revoke()

        status = lifecycle.status()
        assert status.state == "ready"
        assert status.approval_current is False
        assert status.ready is False
    finally:
        await lifecycle.aclose()
        store.close()


@pytest.mark.asyncio
async def test_lifecycle_emits_typed_state_read_and_kill_switch_observations(
    tmp_path: Path,
) -> None:
    handler = CapturingHandler()
    observer, metrics = sandbox_observer(handler)
    lifecycle, _, store, _, request, _ = lifecycle_inputs(tmp_path, observer=observer)
    try:
        await lifecycle.start()
        fresh = await lifecycle.admit_once(request)
        replay = await lifecycle.admit_once(request)
        assert lifecycle.trip("manual") is True
        with pytest.raises(NativeIMKillSwitchTrippedError):
            await lifecycle.admit_once(request)
    finally:
        await lifecycle.aclose()
        store.close()

    decoded = [json.loads(message) for message in handler.messages]
    assert [(item["event"], item["fields"]) for item in decoded] == [
        (
            "qe.native_im.lifecycle",
            {"kill_switch_tripped": False, "ready": False, "state": "starting"},
        ),
        ("qe.native_im.health", {"outcome": "success"}),
        (
            "qe.native_im.lifecycle",
            {"kill_switch_tripped": False, "ready": True, "state": "ready"},
        ),
        (
            "qe.native_im.read",
            {
                "event_count": len(fresh.event_receipts),
                "outcome": "fresh_observation",
                "trace_present": False,
            },
        ),
        (
            "qe.native_im.read",
            {
                "event_count": len(replay.event_receipts),
                "outcome": "observed_replay",
                "trace_present": False,
            },
        ),
        ("qe.native_im.kill_switch", {"reason": "manual"}),
        (
            "qe.native_im.read",
            {"event_count": 0, "outcome": "kill_switch", "trace_present": False},
        ),
        (
            "qe.native_im.lifecycle",
            {"kill_switch_tripped": True, "ready": False, "state": "draining"},
        ),
        (
            "qe.native_im.lifecycle",
            {"kill_switch_tripped": True, "ready": False, "state": "closed"},
        ),
    ]
    snapshot = metrics.snapshot()
    assert snapshot.health_success_count == 1
    assert snapshot.read_fresh_count == 1
    assert snapshot.read_replay_count == 1
    assert snapshot.read_rejected_count == 1
    assert snapshot.events_admitted_count == len(fresh.event_receipts)
    assert snapshot.kill_switch_trip_count == 1


@pytest.mark.asyncio
async def test_logging_backend_failure_cannot_change_lifecycle_admission(
    tmp_path: Path,
) -> None:
    observer, metrics = sandbox_observer(FailingHandler())
    lifecycle, _, store, _, request, _ = lifecycle_inputs(tmp_path, observer=observer)
    try:
        health = await lifecycle.start()
        result = await lifecycle.admit_once(request)
        assert health.healthy is True
        assert result.disposition == "fresh_observation"
    finally:
        await lifecycle.aclose()
        store.close()
    snapshot = metrics.snapshot()
    assert snapshot.health_success_count == 1
    assert snapshot.read_fresh_count == 1
    assert snapshot.events_admitted_count == len(result.event_receipts)


@pytest.mark.asyncio
async def test_checkpoint_conflict_is_observed_as_rejected_before_transport(
    tmp_path: Path,
) -> None:
    observer, metrics = sandbox_observer()
    lifecycle, _, store, _, request, transport = lifecycle_inputs(
        tmp_path,
        observer=observer,
    )
    conflicting_request = replace(
        request,
        after_cursor="test-out-of-order-cursor",
        after_sequence=99,
        snapshot_token="test-out-of-order-snapshot",
        read_request_id="test-out-of-order-read",
    )
    try:
        await lifecycle.start()
        with pytest.raises(NativeIMInboundCheckpointConflictError):
            await lifecycle.admit_once(conflicting_request)
        assert transport.read_calls == 0
        assert metrics.snapshot().read_rejected_count == 1
    finally:
        await lifecycle.aclose()
        store.close()


@pytest.mark.asyncio
async def test_trip_during_inflight_read_prevents_admission_and_restart_resumes(
    tmp_path: Path,
) -> None:
    lifecycle, _, store, kill_switch, request, transport = lifecycle_inputs(
        tmp_path,
        blocking=True,
    )
    assert type(transport) is BlockingTransport
    await lifecycle.start()
    task = asyncio.create_task(lifecycle.admit_once(request))
    await transport.entered.wait()

    assert lifecycle.trip("manual") is True
    assert lifecycle.trip("manual") is False
    transport.release.set()
    with pytest.raises(NativeIMKillSwitchTrippedError):
        await task

    preparation = store.prepare_native_im_inbound_read(request)
    assert preparation.read_status == "prepared"
    assert preparation.disposition == "observed_replay"
    assert lifecycle.status().ready is False
    assert kill_switch.tripped is True
    await lifecycle.aclose()

    resumed_adapter, _, _, _, _, _, _, _ = adapter_inputs(replay_guard=store)
    resumed = NativeIMSandboxLifecycleV1(
        resumed_adapter,
        store,
        NativeIMSandboxKillSwitchV1(),
        sandbox_observer()[0],
    )
    try:
        await resumed.start()
        result = await resumed.admit_once(request)
        assert result.disposition == "fresh_observation"
    finally:
        await resumed.aclose()
        store.close()


@pytest.mark.asyncio
async def test_cancelled_read_retains_preparation_and_can_resume_without_restart(
    tmp_path: Path,
) -> None:
    observer, metrics = sandbox_observer()
    lifecycle, _, store, _, request, transport = lifecycle_inputs(
        tmp_path,
        blocking=True,
        observer=observer,
    )
    assert type(transport) is BlockingTransport
    try:
        await lifecycle.start()
        task = asyncio.create_task(lifecycle.admit_once(request))
        await transport.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        preparation = store.prepare_native_im_inbound_read(request)
        assert preparation.read_status == "prepared"
        assert lifecycle.status().ready is True
        assert metrics.snapshot().read_rejected_count == 1

        transport.release.set()
        resumed = await lifecycle.admit_once(request)
        assert resumed.disposition == "fresh_observation"
        assert transport.read_calls == 2
        assert metrics.snapshot().read_fresh_count == 1
    finally:
        await lifecycle.aclose()
        store.close()


@pytest.mark.asyncio
async def test_cancelled_close_is_retryable_and_only_success_marks_adapter_closed(
    tmp_path: Path,
) -> None:
    lifecycle, adapter, store, kill_switch, _, transport = lifecycle_inputs(
        tmp_path,
        blocking_close=True,
    )
    assert type(transport) is BlockingCloseTransport
    await lifecycle.start()
    close_task = asyncio.create_task(lifecycle.aclose())
    await transport.close_entered.wait()
    close_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await close_task

    assert lifecycle.status().state == "failed"
    assert lifecycle.status().ready is False
    assert kill_switch.tripped is True
    assert adapter.closed is False

    transport.close_release.set()
    await lifecycle.aclose()
    assert lifecycle.status().state == "closed"
    assert adapter.closed is True
    assert transport.close_calls == 2
    store.close()


@pytest.mark.asyncio
async def test_close_trips_gate_is_idempotent_and_prevents_future_reads(tmp_path: Path) -> None:
    lifecycle, _, store, kill_switch, request, transport = lifecycle_inputs(tmp_path)
    await lifecycle.start()
    await lifecycle.aclose()
    await lifecycle.aclose()

    assert lifecycle.status().state == "closed"
    assert lifecycle.status().ready is False
    assert kill_switch.tripped is True
    assert transport.close_calls == 1
    with pytest.raises(NativeIMSandboxLifecycleError):
        await lifecycle.admit_once(request)
    store.close()


def test_kill_switch_snapshot_is_owner_bound_one_way_and_not_serializable() -> None:
    first = NativeIMSandboxKillSwitchV1()
    second = NativeIMSandboxKillSwitchV1()
    snapshot = first.snapshot()
    assert snapshot.generation == 0
    assert snapshot.tripped is False
    first.require_permitted(snapshot)

    with pytest.raises(NativeIMKillSwitchTrippedError):
        second.require_permitted(snapshot)
    assert first.trip("contract_failure") is True
    with pytest.raises(NativeIMKillSwitchTrippedError):
        first.require_permitted(snapshot)
    with pytest.raises(TypeError):
        pickle.dumps(first)

    forged = NativeIMKillSwitchSnapshotV1(
        generation=first.generation,
        tripped=False,
        _owner_token=object(),
        _process_id=os.getpid(),
    )
    with pytest.raises(NativeIMKillSwitchTrippedError):
        first.require_permitted(forged)


def test_lifecycle_and_kill_switch_fail_closed_after_process_identity_change(
    tmp_path: Path,
) -> None:
    lifecycle, _, store, kill_switch, _, _ = lifecycle_inputs(tmp_path)
    try:
        with patch("quantum_entanglement.native_im_sandbox_lifecycle.os.getpid", return_value=1):
            with pytest.raises(NativeIMSandboxProcessMismatchError):
                lifecycle.status()
            with pytest.raises(NativeIMSandboxProcessMismatchError):
                kill_switch.trip("manual")
    finally:
        store.close()


def test_lifecycle_rejects_subclasses_and_serialization(tmp_path: Path) -> None:
    lifecycle, adapter, store, kill_switch, _, _ = lifecycle_inputs(tmp_path)

    class AdapterSubclass(NativeIMInboundOnlySandboxAdapter):
        pass

    class ObserverSubclass(NativeIMSandboxObserverV1):
        pass

    try:
        with pytest.raises(TypeError):
            NativeIMSandboxLifecycleV1(
                object.__new__(AdapterSubclass),
                store,
                kill_switch,
                sandbox_observer()[0],
            )
        with pytest.raises(TypeError):
            NativeIMSandboxLifecycleV1(
                adapter,
                store,
                kill_switch,
                object.__new__(ObserverSubclass),
            )
        with pytest.raises(TypeError):
            pickle.dumps(lifecycle)
    finally:
        assert adapter.closed is False
        store.close()
