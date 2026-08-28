from __future__ import annotations

import asyncio
import os
import pickle
from pathlib import Path
from unittest.mock import patch

import pytest

from quantum_entanglement.native_im_inbox import NativeIMInboundPageAdmissionResultV1
from quantum_entanglement.native_im_nonce_store import SQLiteNativeIMInboxStore
from quantum_entanglement.native_im_sandbox import NativeIMInboundOnlySandboxAdapter
from quantum_entanglement.native_im_sandbox_lifecycle import (
    NativeIMKillSwitchSnapshotV1,
    NativeIMKillSwitchTrippedError,
    NativeIMSandboxKillSwitchV1,
    NativeIMSandboxLifecycleError,
    NativeIMSandboxLifecycleV1,
    NativeIMSandboxProcessMismatchError,
)
from tests.test_native_im_auth import NOW
from tests.test_native_im_sandbox_inbound_adapter import (
    FixtureTransport,
    adapter_inputs,
    provider_profile,
)


class BlockingTransport(FixtureTransport):
    def __init__(self, response) -> None:
        super().__init__(response)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def read_inbound(self, request, credential):
        self.read_calls += 1
        self.entered.set()
        await self.release.wait()
        assert credential.view().tobytes()
        return self.response


def lifecycle_inputs(tmp_path: Path, *, blocking: bool = False):
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
    if blocking:
        blocked = BlockingTransport(transport.response)
        adapter = NativeIMInboundOnlySandboxAdapter(
            configuration,
            profile,
            blocked,
            mapper,
            secrets,
            store,
            clock=lambda: NOW,
        )
        transport = blocked
    kill_switch = NativeIMSandboxKillSwitchV1()
    lifecycle = NativeIMSandboxLifecycleV1(adapter, store, kill_switch)
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

        health = await lifecycle.start()
        assert health.healthy is True
        assert lifecycle.status().ready is True

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
    )
    try:
        await resumed.start()
        result = await resumed.admit_once(request)
        assert result.disposition == "fresh_observation"
    finally:
        await resumed.aclose()
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

    try:
        with pytest.raises(TypeError):
            NativeIMSandboxLifecycleV1(object.__new__(AdapterSubclass), store, kill_switch)
        with pytest.raises(TypeError):
            pickle.dumps(lifecycle)
    finally:
        assert adapter.closed is False
        store.close()
