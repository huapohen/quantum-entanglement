from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from quantum_entanglement.native_im import IMInboundReadRequestV1
from quantum_entanglement.native_im_nonce_store import SQLiteNativeIMInboxStore
from quantum_entanglement.native_im_sandbox import (
    NativeIMHealthEvidenceV1,
    NativeIMInboundOnlySandboxAdapter,
    NativeIMInboundRawResponseV1,
)
from quantum_entanglement.native_im_sandbox_lifecycle import (
    NativeIMSandboxKillSwitchV1,
    NativeIMSandboxLifecycleError,
    NativeIMSandboxLifecycleV1,
)
from quantum_entanglement.service.native_im_config import NativeIMInboundOnlyConfigV1
from quantum_entanglement.service.secrets import SecretMaterial, SecretRef
from tests.test_native_im_auth import (
    NOW,
    SIGNED_UNIX_SECONDS,
    configuration_for,
    metadata_for,
    signature_for,
)
from tests.test_native_im_contract import inbound_read_request
from tests.test_native_im_sandbox_inbound_adapter import (
    MAPPING_EVIDENCE,
    TRANSPORT_EVIDENCE,
    FixtureMapper,
    provider_profile,
)
from tests.test_native_im_sandbox_lifecycle import CapturingHandler, sandbox_observer

MESSAGE_BODY_CANARY = "native-im-private-message-body-canary-5ef95c78"
TRACE_CANARY = "00-11111111111111111111111111111111-2222222222222222-01"
READ_SECRET_CANARY = b"native-im-read-secret-canary-89c2a4b3"
VERIFY_SECRET_CANARY = b"native-im-verify-secret-canary-3d753d2a"
RAW_CANARY_BODY = (
    '{"message":"' + MESSAGE_BODY_CANARY + '","traceparent":"' + TRACE_CANARY + '"}'
).encode()


class CanarySecretProvider:
    def __init__(self, configuration: NativeIMInboundOnlyConfigV1) -> None:
        self.configuration = configuration
        self.materials: list[SecretMaterial] = []
        self.retained_views: list[memoryview] = []

    def resolve(self, reference: SecretRef) -> SecretMaterial:
        value = (
            VERIFY_SECRET_CANARY
            if reference == self.configuration.verification_secret_ref
            else READ_SECRET_CANARY
        )
        material = SecretMaterial(value)
        self.materials.append(material)
        self.retained_views.append(material.view())
        return material


class CanaryFixtureTransport:
    def __init__(self, response: NativeIMInboundRawResponseV1) -> None:
        self.response = response
        self.health_calls = 0
        self.read_calls = 0
        self.close_calls = 0

    async def probe_health(self, credential: SecretMaterial) -> NativeIMHealthEvidenceV1:
        self.health_calls += 1
        assert credential.view().tobytes() == READ_SECRET_CANARY
        return NativeIMHealthEvidenceV1(
            schema_version=1,
            healthy=True,
            observed_at=NOW,
            evidence_digest="a" * 64,
        )

    async def read_inbound(
        self,
        request: IMInboundReadRequestV1,
        credential: SecretMaterial,
    ) -> NativeIMInboundRawResponseV1:
        self.read_calls += 1
        assert credential.view().tobytes() == READ_SECRET_CANARY
        assert request.read_request_id == self.response.read_request_id
        return self.response

    async def aclose(self) -> None:
        self.close_calls += 1


def _persisted_bytes(database_path: Path) -> bytes:
    logical_values = bytearray()
    with sqlite3.connect(database_path) as connection:
        table_names = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
        for (table_name,) in table_names:
            quoted_name = '"' + str(table_name).replace('"', '""') + '"'
            for row in connection.execute(f"SELECT * FROM {quoted_name}"):
                for value in row:
                    if isinstance(value, bytes):
                        logical_values.extend(value)
                    elif value is not None:
                        logical_values.extend(str(value).encode())
                    logical_values.append(0)
    for path in sorted(database_path.parent.glob(database_path.name + "*")):
        logical_values.extend(path.read_bytes())
    return bytes(logical_values)


@pytest.mark.asyncio
async def test_message_secret_and_trace_canaries_are_contained_end_to_end(
    tmp_path: Path,
) -> None:
    profile = provider_profile()
    configuration = configuration_for(profile)
    request = inbound_read_request(provider=profile.provider)
    metadata = metadata_for(
        configuration,
        timestamp=SIGNED_UNIX_SECONDS,
        signature=signature_for(
            configuration,
            body=RAW_CANARY_BODY,
            timestamp=SIGNED_UNIX_SECONDS,
            key=VERIFY_SECRET_CANARY,
        ),
    )
    response = NativeIMInboundRawResponseV1(
        schema_version=1,
        read_request_id=request.read_request_id,
        status_code=200,
        metadata=metadata,
        raw_body=RAW_CANARY_BODY,
        received_at=NOW,
        transport_evidence_digest=TRANSPORT_EVIDENCE,
    )
    transport = CanaryFixtureTransport(response)
    mapper = FixtureMapper()
    secrets = CanarySecretProvider(configuration)
    database_path = tmp_path / "native-im-canary.sqlite3"
    store = SQLiteNativeIMInboxStore(
        str(database_path),
        profile_revision=profile.revision,
        profile_digest=profile.canonical_digest(),
        clock=lambda: NOW,
    )
    adapter = NativeIMInboundOnlySandboxAdapter(
        configuration,
        profile,
        transport,
        mapper,
        secrets,
        store,
        clock=lambda: NOW,
    )
    handler = CapturingHandler()
    observer, metrics = sandbox_observer(handler)
    lifecycle = NativeIMSandboxLifecycleV1(
        adapter,
        store,
        NativeIMSandboxKillSwitchV1(),
        observer,
    )

    try:
        health = await lifecycle.start()
        result = await lifecycle.admit_once(request)
        assert result.disposition == "fresh_observation"
        assert len(result.event_receipts) == 1
    finally:
        await lifecycle.aclose()
        store.close()

    with pytest.raises(NativeIMSandboxLifecycleError) as raised:
        await lifecycle.admit_once(request)

    canaries = (
        MESSAGE_BODY_CANARY.encode(),
        TRACE_CANARY.encode(),
        READ_SECRET_CANARY,
        VERIFY_SECRET_CANARY,
        metadata.nonce.encode(),
        metadata.signature.encode(),
    )
    captured_logs = "\n".join(handler.messages).encode()
    rendered_evidence = " ".join(
        (
            repr(response),
            repr(health),
            repr(result),
            repr(adapter),
            repr(lifecycle),
            repr(observer),
            repr(metrics),
            repr(metrics.snapshot()),
            repr(raised.value),
            str(raised.value),
            repr(secrets.materials),
        )
    ).encode()
    durable_evidence = _persisted_bytes(database_path)
    for canary in canaries:
        assert canary not in captured_logs
        assert canary not in rendered_evidence
        assert canary not in durable_evidence

    assert mapper.calls == 1
    assert transport.health_calls == transport.read_calls == transport.close_calls == 1
    assert all(material.closed for material in secrets.materials)
    assert all(view.tobytes() == bytes(len(view)) for view in secrets.retained_views)
    assert MAPPING_EVIDENCE not in captured_logs.decode()
    assert all(
        '"trace_present":false' in message
        for message in handler.messages
        if '"event":"qe.native_im.read"' in message
    )
    snapshot = metrics.snapshot()
    assert snapshot.health_success_count == 1
    assert snapshot.read_fresh_count == 1
    assert snapshot.events_admitted_count == 1
    assert snapshot.read_rejected_count == 1
