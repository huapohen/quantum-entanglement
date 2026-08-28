from __future__ import annotations

import asyncio
import hashlib
import socket
import sqlite3
import subprocess
import webbrowser
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest

from quantum_entanglement.native_im import (
    IMCapabilitySnapshotV1,
    IMInboundReadRequestV1,
    IMMembershipChangeV1,
    IMVerifiedInboundEnvelopeV1,
)
from quantum_entanglement.native_im_auth import NativeIMRawVerificationResultV1
from quantum_entanglement.native_im_inbox import (
    NativeIMInboundCheckpointConflictError,
    NativeIMInboundConflictError,
)
from quantum_entanglement.native_im_nonce_store import SQLiteNativeIMInboxStore
from quantum_entanglement.native_im_provider_profile import IMProviderProfileV1
from quantum_entanglement.native_im_sandbox import (
    _APPROVED_COMPOSITION_TOKEN,
    NativeIMHealthEvidenceV1,
    NativeIMInboundOnlySandboxAdapter,
    NativeIMInboundRawResponseV1,
    NativeIMMappedPageV1,
    NativeIMTransportContractError,
    derive_native_im_mapping_evidence_digest_v1,
)
from quantum_entanglement.native_im_sandbox_lifecycle import (
    NativeIMSandboxKillSwitchV1,
    NativeIMSandboxLifecycleV1,
)
from quantum_entanglement.native_im_sandbox_observability import (
    NativeIMSandboxMetricsV1,
)
from quantum_entanglement.service.native_im_config import NativeIMInboundOnlyConfigV1
from quantum_entanglement.service.secrets import SecretMaterial
from tests.test_native_im_auth import (
    NOW,
    SIGNED_UNIX_SECONDS,
    configuration_for,
    metadata_for,
    signature_for,
)
from tests.test_native_im_contract import (
    conversation,
    inbound_event,
    inbound_page,
    inbound_read_request,
    participant,
)
from tests.test_native_im_sandbox_authority import approved_authority_for
from tests.test_native_im_sandbox_inbound_adapter import (
    READ_CREDENTIAL,
    RecordingSecretProvider,
    fixture_health_evidence,
    provider_profile,
)
from tests.test_native_im_sandbox_lifecycle import CapturingHandler, sandbox_observer

DISCONNECT_CANARY = "recorded-disconnect-provider-body-canary"


@dataclass(frozen=True)
class RecordedPageSpec:
    body: bytes
    event_id: str
    cursor: str
    sequence_number: int
    verification_id: str
    snapshot_token: str
    has_more: bool


class RecordedTransport:
    """In-memory recorded outcomes; it has no endpoint, socket, or outbound method."""

    def __init__(
        self,
        plans: dict[str, tuple[NativeIMInboundRawResponseV1 | str, ...]],
        health_evidence: NativeIMHealthEvidenceV1,
    ) -> None:
        self.plans = {key: tuple(value) for key, value in plans.items()}
        self.health_evidence = health_evidence
        self.positions = {key: 0 for key in plans}
        self.health_calls = 0
        self.read_calls = 0
        self.close_calls = 0

    async def probe_health(self, credential: SecretMaterial) -> NativeIMHealthEvidenceV1:
        self.health_calls += 1
        assert credential.view().tobytes() == READ_CREDENTIAL
        return self.health_evidence

    async def read_inbound(
        self,
        request: IMInboundReadRequestV1,
        credential: SecretMaterial,
    ) -> NativeIMInboundRawResponseV1:
        self.read_calls += 1
        assert credential.view().tobytes() == READ_CREDENTIAL
        plan = self.plans[request.read_request_id]
        position = self.positions[request.read_request_id]
        outcome = plan[min(position, len(plan) - 1)]
        self.positions[request.read_request_id] = position + 1
        if outcome == "disconnect":
            raise ConnectionError(DISCONNECT_CANARY)
        if type(outcome) is not NativeIMInboundRawResponseV1:
            raise TypeError("recorded transport outcome is invalid")
        return outcome

    async def aclose(self) -> None:
        self.close_calls += 1


class RecordedMapper:
    """Pure mapper from immutable recorded bodies to canonical provider-neutral pages."""

    def __init__(self, specs: tuple[RecordedPageSpec, ...]) -> None:
        self.specs = {spec.body: spec for spec in specs}
        self.calls = 0

    def map_inbound(
        self,
        response: NativeIMInboundRawResponseV1,
        request: IMInboundReadRequestV1,
        capability: IMCapabilitySnapshotV1,
        raw_verification: NativeIMRawVerificationResultV1,
        profile: IMProviderProfileV1,
    ) -> NativeIMMappedPageV1:
        self.calls += 1
        spec = self.specs[response.raw_body]
        member = participant(provider=profile.provider)
        membership = IMMembershipChangeV1(
            schema_version=1,
            subject=member,
            change_kind="joined",
            previous_membership_revision=None,
        )
        event = inbound_event(
            event_id=spec.event_id,
            event_type="membership.changed",
            cursor=spec.cursor,
            sequence_number=spec.sequence_number,
            conversation=conversation(provider=profile.provider),
            message=None,
            sender=None,
            content=None,
            reaction=None,
            membership_change=membership,
            ingress_request_id=f"recorded-ingress-{spec.sequence_number}",
            correlation_id=f"recorded-correlation-{spec.sequence_number}",
            transport_evidence_digest=response.transport_evidence_digest,
        )
        envelope = IMVerifiedInboundEnvelopeV1(
            schema_version=1,
            event=event,
            event_digest=event.canonical_digest(),
            verification_id=spec.verification_id,
            verifier_id=raw_verification.verifier_id,
            authentication_evidence_digest=(raw_verification.authentication_evidence_digest),
            tenant_mapping_revision=profile.tenant_mapping_revision,
            verified_at=raw_verification.verified_at,
            traceparent=None,
        )
        page = inbound_page(
            request=request,
            capability=capability,
            envelopes=(envelope,),
            snapshot_token=spec.snapshot_token,
            has_more=spec.has_more,
        )
        canonical = page.canonical_bytes()
        return NativeIMMappedPageV1(
            schema_version=1,
            source_body_digest=raw_verification.body_digest,
            canonical_page_body=canonical,
            mapping_evidence_digest=derive_native_im_mapping_evidence_digest_v1(
                mapper_contract_id="test-native-im-mapper-v1",
                mapper_contract_digest="3" * 64,
                profile_digest=profile.canonical_digest(),
                read_request_digest=request.canonical_digest(),
                capability_digest=capability.canonical_digest(),
                source_body_digest=raw_verification.body_digest,
                page_digest=page.canonical_digest(),
            ),
        )


@dataclass(frozen=True)
class RecordedProbeRig:
    lifecycle: NativeIMSandboxLifecycleV1
    store: SQLiteNativeIMInboxStore
    database_path: Path
    transport: RecordedTransport
    mapper: RecordedMapper
    metrics: NativeIMSandboxMetricsV1
    handler: CapturingHandler


def recorded_response(
    configuration: NativeIMInboundOnlyConfigV1,
    request: IMInboundReadRequestV1,
    spec: RecordedPageSpec,
    *,
    nonce: str,
) -> NativeIMInboundRawResponseV1:
    return NativeIMInboundRawResponseV1(
        schema_version=1,
        read_request_id=request.read_request_id,
        status_code=200,
        metadata=metadata_for(
            configuration,
            timestamp=SIGNED_UNIX_SECONDS,
            nonce=nonce,
            signature=signature_for(
                configuration,
                body=spec.body,
                timestamp=SIGNED_UNIX_SECONDS,
                nonce=nonce,
            ),
        ),
        raw_body=spec.body,
        received_at=NOW,
        transport_evidence_digest=hashlib.sha256(
            b"recorded-transport-v1\n" + spec.body
        ).hexdigest(),
    )


def make_probe_rig(
    tmp_path: Path,
    profile: IMProviderProfileV1,
    configuration: NativeIMInboundOnlyConfigV1,
    plans: dict[str, tuple[NativeIMInboundRawResponseV1 | str, ...]],
    specs: tuple[RecordedPageSpec, ...],
) -> RecordedProbeRig:
    database_path = tmp_path / "native-im-recorded-probe.sqlite3"
    store = SQLiteNativeIMInboxStore(
        str(database_path),
        profile_revision=profile.revision,
        profile_digest=profile.canonical_digest(),
        clock=lambda: NOW,
    )
    mapper = RecordedMapper(specs)
    configuration, approval_authority, approval_permit, _, _ = approved_authority_for(
        configuration,
        profile,
    )
    transport = RecordedTransport(
        plans,
        fixture_health_evidence(configuration, profile),
    )
    adapter = NativeIMInboundOnlySandboxAdapter(
        configuration,
        profile,
        approval_authority,
        approval_permit,
        "9" * 64,
        transport,
        mapper,
        RecordingSecretProvider(configuration),
        store,
        clock=lambda: NOW,
        _composition_token=_APPROVED_COMPOSITION_TOKEN,
    )
    handler = CapturingHandler()
    observer, metrics = sandbox_observer(handler)
    return RecordedProbeRig(
        lifecycle=NativeIMSandboxLifecycleV1(
            adapter,
            store,
            NativeIMSandboxKillSwitchV1(),
            observer,
        ),
        store=store,
        database_path=database_path,
        transport=transport,
        mapper=mapper,
        metrics=metrics,
        handler=handler,
    )


@contextmanager
def zero_effect_fence() -> Iterator[None]:
    with ExitStack() as stack:
        stack.enter_context(patch.object(socket, "socket", side_effect=AssertionError))
        stack.enter_context(patch.object(socket, "getaddrinfo", side_effect=AssertionError))
        stack.enter_context(patch.object(subprocess, "Popen", side_effect=AssertionError))
        stack.enter_context(
            patch.object(asyncio, "create_subprocess_exec", side_effect=AssertionError)
        )
        stack.enter_context(patch.object(webbrowser, "open", side_effect=AssertionError))
        yield


def page_spec(
    name: str,
    sequence_number: int,
    *,
    snapshot_token: str = "recorded-snapshot-1",
    has_more: bool = False,
) -> RecordedPageSpec:
    return RecordedPageSpec(
        body=(f'{{"record":"{name}"}}').encode(),
        event_id=f"recorded-event-{name}",
        cursor=f"recorded-cursor-{sequence_number}",
        sequence_number=sequence_number,
        verification_id=f"recorded-verification-{name}",
        snapshot_token=snapshot_token,
        has_more=has_more,
    )


@pytest.mark.asyncio
async def test_recorded_disconnect_resumes_only_through_atomic_admission(
    tmp_path: Path,
) -> None:
    profile = provider_profile()
    configuration = configuration_for(profile)
    request = inbound_read_request(provider=profile.provider)
    spec = page_spec("resume", 1)
    response = recorded_response(
        configuration,
        request,
        spec,
        nonce="recorded-nonce-resume",
    )
    rig = make_probe_rig(
        tmp_path,
        profile,
        configuration,
        {request.read_request_id: ("disconnect", response)},
        (spec,),
    )
    try:
        with zero_effect_fence():
            await rig.lifecycle.start()
            with pytest.raises(NativeIMTransportContractError) as raised:
                await rig.lifecycle.admit_once(request)
            preparation = rig.store.prepare_native_im_inbound_read(request)
            resumed = await rig.lifecycle.admit_once(request)
        assert preparation.read_status == "prepared"
        assert preparation.disposition == "observed_replay"
        assert resumed.disposition == "fresh_observation"
        assert rig.transport.read_calls == 2
        assert rig.mapper.calls == 1
        assert DISCONNECT_CANARY not in f"{raised.value!r} {raised.value}"
        assert raised.value.__cause__ is None
        assert raised.value.__context__ is None
        snapshot = rig.metrics.snapshot()
        assert snapshot.read_rejected_count == 1
        assert snapshot.read_fresh_count == 1
    finally:
        with zero_effect_fence():
            await rig.lifecycle.aclose()
        rig.store.close()


@pytest.mark.asyncio
async def test_recorded_duplicate_page_is_one_fresh_observation_then_replay(
    tmp_path: Path,
) -> None:
    profile = provider_profile()
    configuration = configuration_for(profile)
    request = inbound_read_request(provider=profile.provider)
    spec = page_spec("duplicate", 1)
    response = recorded_response(
        configuration,
        request,
        spec,
        nonce="recorded-nonce-duplicate",
    )
    rig = make_probe_rig(
        tmp_path,
        profile,
        configuration,
        {request.read_request_id: (response,)},
        (spec,),
    )
    try:
        with zero_effect_fence():
            await rig.lifecycle.start()
            fresh = await rig.lifecycle.admit_once(request)
            replay = await rig.lifecycle.admit_once(request)
        assert fresh.disposition == "fresh_observation"
        assert replay.disposition == "observed_replay"
        assert replay.page_digest == fresh.page_digest
        assert replay.event_receipts == fresh.event_receipts
        assert rig.transport.read_calls == rig.mapper.calls == 2
        snapshot = rig.metrics.snapshot()
        assert snapshot.read_fresh_count == 1
        assert snapshot.read_replay_count == 1
        assert snapshot.events_admitted_count == 1
    finally:
        with zero_effect_fence():
            await rig.lifecycle.aclose()
        rig.store.close()


@pytest.mark.asyncio
async def test_recorded_out_of_order_resume_is_rejected_before_transport_then_recovers(
    tmp_path: Path,
) -> None:
    profile = provider_profile()
    configuration = configuration_for(profile)
    first_request = inbound_read_request(provider=profile.provider)
    first_spec = page_spec("ordered-1", 1, has_more=True)
    second_request = inbound_read_request(
        provider=profile.provider,
        after_cursor=first_spec.cursor,
        after_sequence=first_spec.sequence_number,
        snapshot_token=first_spec.snapshot_token,
        read_request_id="test-read-request-2",
    )
    second_spec = page_spec("ordered-2", 2)
    first_response = recorded_response(
        configuration,
        first_request,
        first_spec,
        nonce="recorded-nonce-ordered-1",
    )
    second_response = recorded_response(
        configuration,
        second_request,
        second_spec,
        nonce="recorded-nonce-ordered-2",
    )
    rig = make_probe_rig(
        tmp_path,
        profile,
        configuration,
        {
            first_request.read_request_id: (first_response,),
            second_request.read_request_id: (second_response,),
        },
        (first_spec, second_spec),
    )
    try:
        with zero_effect_fence():
            await rig.lifecycle.start()
            with pytest.raises(NativeIMInboundCheckpointConflictError):
                await rig.lifecycle.admit_once(second_request)
            assert rig.transport.read_calls == 0
            first = await rig.lifecycle.admit_once(first_request)
            second = await rig.lifecycle.admit_once(second_request)
        assert first.checkpoint.checkpoint_revision == 1
        assert second.checkpoint.checkpoint_revision == 2
        assert second.event_receipts[0].sequence_number == 2
        assert rig.transport.read_calls == rig.mapper.calls == 2
        snapshot = rig.metrics.snapshot()
        assert snapshot.read_rejected_count == 1
        assert snapshot.read_fresh_count == 2
        assert snapshot.events_admitted_count == 2
    finally:
        with zero_effect_fence():
            await rig.lifecycle.aclose()
        rig.store.close()


@pytest.mark.asyncio
async def test_recorded_conflicting_replay_rolls_back_second_nonce_and_page(
    tmp_path: Path,
) -> None:
    profile = provider_profile()
    configuration = configuration_for(profile)
    request = inbound_read_request(provider=profile.provider)
    accepted_spec = page_spec("accepted", 1)
    conflicting_spec = page_spec("conflicting", 1)
    accepted_response = recorded_response(
        configuration,
        request,
        accepted_spec,
        nonce="recorded-nonce-accepted",
    )
    conflicting_response = recorded_response(
        configuration,
        request,
        conflicting_spec,
        nonce="recorded-nonce-conflicting",
    )
    rig = make_probe_rig(
        tmp_path,
        profile,
        configuration,
        {request.read_request_id: (accepted_response, conflicting_response)},
        (accepted_spec, conflicting_spec),
    )
    try:
        with zero_effect_fence():
            await rig.lifecycle.start()
            fresh = await rig.lifecycle.admit_once(request)
            with pytest.raises(NativeIMInboundConflictError):
                await rig.lifecycle.admit_once(request)
        assert fresh.disposition == "fresh_observation"
        assert rig.transport.read_calls == rig.mapper.calls == 2
        with sqlite3.connect(rig.database_path) as connection:
            assert (
                connection.execute("SELECT COUNT(*) FROM native_im_auth_nonces").fetchone()[0] == 1
            )
            assert (
                connection.execute("SELECT COUNT(*) FROM native_im_inbox_events").fetchone()[0] == 1
            )
        snapshot = rig.metrics.snapshot()
        assert snapshot.read_fresh_count == 1
        assert snapshot.read_rejected_count == 1
        assert snapshot.events_admitted_count == 1
    finally:
        with zero_effect_fence():
            await rig.lifecycle.aclose()
        rig.store.close()


def test_recorded_probe_transport_exposes_no_endpoint_or_outbound_surface() -> None:
    profile = provider_profile()
    configuration = configuration_for(profile)
    configuration, _, _, _, _ = approved_authority_for(configuration, profile)
    transport = RecordedTransport(
        {"test-read": ("disconnect",)},
        fixture_health_evidence(configuration, profile),
    )
    assert not hasattr(transport, "dispatch")
    assert not hasattr(transport, "query_acceptance")
    assert not hasattr(transport, "endpoint")
    assert set(vars(transport)) == {
        "plans",
        "positions",
        "health_evidence",
        "health_calls",
        "read_calls",
        "close_calls",
    }
