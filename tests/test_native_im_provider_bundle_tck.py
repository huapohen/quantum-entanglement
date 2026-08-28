from __future__ import annotations

import sqlite3

import pytest

from quantum_entanglement.native_im_nonce_store import SQLiteNativeIMInboxStore
from quantum_entanglement.native_im_provider_exchange import NativeIMProviderWireResponseV1
from quantum_entanglement.native_im_sandbox import (
    NativeIMVerifiedInboundReadV1,
)
from quantum_entanglement.native_im_sandbox_approval_store import (
    SQLiteNativeIMSandboxApprovalHighWaterV1,
)
from quantum_entanglement.native_im_sandbox_composition import (
    NativeIMProviderSandboxRegistrationV1,
    compose_approved_native_im_sandbox_v1,
)
from quantum_entanglement.native_im_sandbox_provenance import (
    NativeIMSandboxExchangeAdmissionProvenanceV1,
    decode_native_im_sandbox_admission_provenance_v1,
)
from tests.native_im_mapper_tck import native_im_mapper_zero_effect_fence_v1
from tests.native_im_synthetic_provider_mapper import SyntheticSemanticProviderMapperV1
from tests.native_im_synthetic_provider_transport import (
    ScriptedNativeIMExchangeStepV1,
    ScriptedNativeIMProviderExchangeV1,
    SyntheticSemanticProviderTransportV1,
)
from tests.test_native_im_auth import (
    KEY,
    NOW,
    SIGNED_UNIX_SECONDS,
    authentication_profile,
    configuration_for,
    signature_for,
)
from tests.test_native_im_contract import inbound_read_request
from tests.test_native_im_sandbox_authority import approved_authority_for
from tests.test_native_im_sandbox_composition import manifest_for
from tests.test_native_im_sandbox_inbound_adapter import RecordingSecretProvider
from tests.test_native_im_synthetic_provider_mapper import (
    TRANSPORT_EVIDENCE,
    _payload,
    _raw,
)

EXCHANGE_SECURITY_EVIDENCE = "8" * 64


class PlanningExchange:
    async def exchange(self, intent, credential):
        raise AssertionError("planning exchange cannot execute")

    async def aclose(self):
        return None


@pytest.mark.asyncio
async def test_provider_bundle_tck_closes_verified_exchange_to_durable_admission(
    tmp_path,
) -> None:
    provider_profile = authentication_profile()
    configuration = configuration_for(provider_profile)
    high_water = SQLiteNativeIMSandboxApprovalHighWaterV1(
        str((tmp_path / "provider-bundle-approval.sqlite3").resolve())
    )
    inbox_path = str((tmp_path / "provider-bundle-inbox.sqlite3").resolve())
    inbox = SQLiteNativeIMInboxStore(
        inbox_path,
        profile_revision=provider_profile.revision,
        profile_digest=provider_profile.canonical_digest(),
        clock=lambda: NOW,
    )
    adapter = None
    try:
        configuration, authority, _, _, _ = approved_authority_for(
            configuration,
            provider_profile,
            high_water=high_water,
            now=NOW,
        )
        manifest = manifest_for(provider_profile)
        request = inbound_read_request(provider=provider_profile.provider)
        raw_body = _raw(_payload(request))
        nonce = f"nonce-{request.read_request_id}"
        planning_transport = SyntheticSemanticProviderTransportV1(
            configuration,
            provider_profile,
            manifest.canonical_digest(),
            ScriptedNativeIMProviderExchangeV1(
                (
                    ScriptedNativeIMExchangeStepV1(
                        SyntheticSemanticProviderTransportV1(
                            configuration,
                            provider_profile,
                            manifest.canonical_digest(),
                            PlanningExchange(),
                            clock=lambda: NOW,
                        ).read_intent(request),
                        response=NativeIMProviderWireResponseV1(
                            schema_version=1,
                            status_code=200,
                            headers=tuple(
                                sorted(
                                    {
                                        "x-native-im-event-source-digest": TRANSPORT_EVIDENCE,
                                        "x-native-im-key-id": configuration.verification_key_id,
                                        "x-native-im-nonce": nonce,
                                        "x-native-im-read-request-id": request.read_request_id,
                                        "x-native-im-signature": signature_for(
                                            configuration,
                                            body=raw_body,
                                            timestamp=SIGNED_UNIX_SECONDS,
                                            nonce=nonce,
                                            key=KEY,
                                        ),
                                        "x-native-im-timestamp": SIGNED_UNIX_SECONDS,
                                    }.items()
                                )
                            ),
                            raw_body=raw_body,
                            received_at=NOW,
                            exchange_security_evidence_digest=EXCHANGE_SECURITY_EVIDENCE,
                        ),
                    ),
                )
            ),
            clock=lambda: NOW,
        )
        registration = NativeIMProviderSandboxRegistrationV1(
            manifest,
            transport=planning_transport,
            mapper=SyntheticSemanticProviderMapperV1(),
            secret_provider=RecordingSecretProvider(configuration),
            replay_guard=inbox,
        )
        adapter = compose_approved_native_im_sandbox_v1(
            configuration,
            provider_profile,
            authority,
            registration,
            clock=lambda: NOW,
        )
        inbox.prepare_native_im_inbound_read(request)

        with native_im_mapper_zero_effect_fence_v1():
            verified = await adapter.read_verified_inbound(request)
        with adapter.approval_admission_guard():
            admitted = inbox.admit_native_im_inbound_page(
                verified.request,
                verified.capability,
                verified.page,
                verified.raw_verification,
                verified.provenance,
            )

        assert type(verified) is NativeIMVerifiedInboundReadV1
        assert type(verified.provenance) is NativeIMSandboxExchangeAdmissionProvenanceV1
        assert verified.provenance.read_exchange_evidence.event_source_evidence_digest == (
            verified.page.envelopes[0].event.transport_evidence_digest
        )
        assert (
            verified.provenance.read_exchange_evidence.exchange_security_evidence_digest
            == EXCHANGE_SECURITY_EVIDENCE
        )
        assert admitted.disposition == "fresh_observation"
        assert admitted.page_digest == verified.page.canonical_digest()

        connection = sqlite3.connect(inbox_path)
        try:
            row = connection.execute(
                "SELECT provenance_json, provenance_digest FROM native_im_inbound_provenance"
            ).fetchone()
        finally:
            connection.close()
        assert row is not None
        persisted = decode_native_im_sandbox_admission_provenance_v1(row[0].encode("utf-8"))
        assert persisted == verified.provenance
        assert row[1] == verified.provenance.canonical_digest()
    finally:
        if adapter is not None:
            await adapter.aclose()
        inbox.close()
        high_water.close()
