from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from quantum_entanglement.native_im import (
    IMAcceptanceLookupCapabilityV1,
    IMAcceptanceQueryV1,
    IMActionCommandV1,
    IMActionIntentV1,
    IMActionReceiptV1,
    IMAttachmentRefV1,
    IMCapabilityRequestV1,
    IMCapabilitySnapshotV1,
    IMConversationRefV1,
    IMDispatchRequestV1,
    IMDispatchUnknownObservationV1,
    IMInboundPageV1,
    IMInboundReadRequestV1,
    IMMembershipChangeV1,
    IMMessageContentV1,
    IMMessageRefV1,
    IMMessageSegmentV1,
    IMOperationCapabilityV1,
    IMParticipantRefV1,
    IMReactionRefV1,
    IMVerifiedInboundEnvelopeV1,
    InboundIMEventV1,
    derive_im_idempotency_key_v1,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIRECTORY = REPOSITORY_ROOT / "tests" / "fixtures" / "native_im" / "v1"
MANIFEST_PATH = FIXTURE_DIRECTORY / "manifest.json"

EXPECTED_VECTORS: tuple[tuple[str, type[Any]], ...] = (
    ("conversation_ref.json", IMConversationRefV1),
    ("participant_ref.json", IMParticipantRefV1),
    ("attachment_ref.json", IMAttachmentRefV1),
    ("message_segment_text.json", IMMessageSegmentV1),
    ("message_segment_mention.json", IMMessageSegmentV1),
    ("message_content.json", IMMessageContentV1),
    ("message_ref.json", IMMessageRefV1),
    ("reaction_ref.json", IMReactionRefV1),
    ("membership_change.json", IMMembershipChangeV1),
    ("inbound_event.json", InboundIMEventV1),
    ("verified_inbound_envelope.json", IMVerifiedInboundEnvelopeV1),
    ("capability_request.json", IMCapabilityRequestV1),
    ("acceptance_lookup_capability.json", IMAcceptanceLookupCapabilityV1),
    ("operation_capability.json", IMOperationCapabilityV1),
    ("capability_snapshot.json", IMCapabilitySnapshotV1),
    ("inbound_read_request.json", IMInboundReadRequestV1),
    ("inbound_page.json", IMInboundPageV1),
    ("action_intent.json", IMActionIntentV1),
    ("action_command.json", IMActionCommandV1),
    ("dispatch_request.json", IMDispatchRequestV1),
    ("action_receipt.json", IMActionReceiptV1),
    ("dispatch_unknown_observation.json", IMDispatchUnknownObservationV1),
    ("acceptance_query.json", IMAcceptanceQueryV1),
)
MODEL_BY_FILENAME = dict(EXPECTED_VECTORS)


def load_manifest() -> dict[str, Any]:
    loaded = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert type(loaded) is dict
    return loaded


def test_committed_golden_inventory_covers_every_public_wire_model() -> None:
    manifest = load_manifest()
    assert manifest["schemaVersion"] == 1
    vectors = manifest["vectors"]
    assert type(vectors) is list
    assert tuple((entry["filename"], entry["model"]) for entry in vectors) == tuple(
        (filename, model.__name__) for filename, model in EXPECTED_VECTORS
    )
    expected_files = {entry["filename"] for entry in vectors} | {"manifest.json"}
    assert {path.name for path in FIXTURE_DIRECTORY.glob("*.json")} == expected_files


def test_committed_model_bytes_and_domain_digests_are_exact() -> None:
    for entry in load_manifest()["vectors"]:
        model = MODEL_BY_FILENAME[entry["filename"]]
        raw = (FIXTURE_DIRECTORY / entry["filename"]).read_bytes()
        assert len(raw) == entry["byteLength"]
        independently_decoded = json.loads(raw)
        independently_encoded = json.dumps(
            independently_decoded,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        assert independently_encoded == raw
        decoded = model.from_json_bytes(raw)
        assert decoded.canonical_bytes() == raw
        independent_digest = hashlib.sha256(
            f"quantum-entanglement.native-im/{entry['model']}/1\n".encode() + raw
        ).hexdigest()
        assert entry["digest"] == independent_digest
        assert decoded.canonical_digest() == independent_digest


def test_committed_idempotency_key_uses_only_the_frozen_exact_body() -> None:
    manifest = load_manifest()
    idempotency = manifest["idempotencyKey"]
    raw = (FIXTURE_DIRECTORY / idempotency["intentFilename"]).read_bytes()
    intent_document = json.loads(raw)
    body = {
        "actionId": intent_document["actionId"],
        "channelId": intent_document["conversation"]["channelId"],
        "provider": intent_document["conversation"]["provider"],
        "tenantId": intent_document["tenantId"],
        "workspaceId": intent_document["workspaceId"],
    }
    canonical_body = json.dumps(
        body,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    independent_key = hashlib.sha256(
        b"quantum-entanglement.native-im/idempotency-key/1\n" + canonical_body
    ).hexdigest()
    assert idempotency["value"] == independent_key
    intent = IMActionIntentV1.from_json_bytes(raw)
    assert derive_im_idempotency_key_v1(intent) == independent_key
    assert independent_key != intent.canonical_digest()


def test_golden_verifier_is_read_only() -> None:
    before = {path.name: path.read_bytes() for path in sorted(FIXTURE_DIRECTORY.iterdir())}
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = "src"
    completed = subprocess.run(
        [sys.executable, "scripts/verify_native_im_v1_golden.py"],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "native IM V1 golden vectors verified: 23 vectors"
    after = {path.name: path.read_bytes() for path in sorted(FIXTURE_DIRECTORY.iterdir())}
    assert after == before

    rejected_write = subprocess.run(
        [sys.executable, "scripts/verify_native_im_v1_golden.py", "--write"],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert rejected_write.returncode != 0
    assert "--write" not in rejected_write.stdout
    assert {path.name: path.read_bytes() for path in sorted(FIXTURE_DIRECTORY.iterdir())} == before
