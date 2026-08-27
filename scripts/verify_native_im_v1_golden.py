#!/usr/bin/env python3
"""Read-only verification for the committed provider-neutral native-IM V1 oracle."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from quantum_entanglement.native_im import (  # noqa: E402
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


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _load_json(raw: bytes, *, filename: str) -> dict[str, Any]:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number: {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"invalid golden JSON in {filename}: {error}") from error
    if type(value) is not dict:
        raise ValueError(f"golden JSON root must be an object: {filename}")
    return value


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _digest(model_name: str, raw: bytes) -> str:
    domain = f"quantum-entanglement.native-im/{model_name}/1\n".encode()
    return hashlib.sha256(domain + raw).hexdigest()


def _assert_equal(actual: object, expected: object, message: str) -> None:
    if actual != expected:
        raise ValueError(message)


def _require_exact_type(value: object, expected: type[object], message: str) -> None:
    if type(value) is not expected:
        raise ValueError(message)


def _verify_bindings(
    documents: dict[str, dict[str, Any]],
    digests: dict[str, str],
    decoded_models: dict[str, Any],
) -> None:
    capability = documents["capability_snapshot.json"]
    read_request = documents["inbound_read_request.json"]
    page = documents["inbound_page.json"]
    envelope = documents["verified_inbound_envelope.json"]
    intent = documents["action_intent.json"]
    command = documents["action_command.json"]
    dispatch = documents["dispatch_request.json"]
    receipt = documents["action_receipt.json"]
    observation = documents["dispatch_unknown_observation.json"]
    query = documents["acceptance_query.json"]

    conversation = documents["conversation_ref.json"]
    participant = documents["participant_ref.json"]
    attachment = documents["attachment_ref.json"]
    text_segment = documents["message_segment_text.json"]
    mention_segment = documents["message_segment_mention.json"]
    content = documents["message_content.json"]
    message = documents["message_ref.json"]
    reaction = documents["reaction_ref.json"]
    membership = documents["membership_change.json"]
    event = documents["inbound_event.json"]
    operation = documents["operation_capability.json"]
    lookup = documents["acceptance_lookup_capability.json"]

    _assert_equal(message["conversation"], conversation, "message conversation drift")
    for field in ("tenantId", "workspaceId", "provider", "channelId"):
        _assert_equal(reaction[field], conversation[field], f"reaction {field} scope drift")
    _assert_equal(membership["subject"], participant, "membership participant drift")
    _assert_equal(content["attachments"], [attachment], "content attachment drift")
    _assert_equal(content["segments"][0], text_segment, "content text segment drift")
    _assert_equal(content["segments"][1], mention_segment, "content mention segment drift")
    _assert_equal(event["conversation"], conversation, "event conversation drift")
    _assert_equal(event["sender"], participant, "event participant drift")
    _assert_equal(event["content"], content, "event content drift")
    _assert_equal(event["message"], message, "event message drift")
    _assert_equal(envelope["event"], event, "envelope event drift")
    _assert_equal(
        envelope["eventDigest"], digests["inbound_event.json"], "envelope event digest drift"
    )
    _assert_equal(operation["acceptanceLookups"][0], lookup, "operation lookup drift")
    _assert_equal(capability["operations"], [operation], "capability operation drift")
    _assert_equal(intent["conversation"], conversation, "intent conversation drift")
    _assert_equal(intent["content"], content, "intent content drift")

    _assert_equal(
        page["readRequestDigest"], digests["inbound_read_request.json"], "page request digest drift"
    )
    _assert_equal(page["readRequestId"], read_request["readRequestId"], "page request ID drift")
    _assert_equal(
        page["capabilityDigest"],
        digests["capability_snapshot.json"],
        "page capability digest drift",
    )
    _assert_equal(
        page["capabilityRevision"], capability["revision"], "page capability revision drift"
    )
    _assert_equal(page["envelopes"], [envelope], "page envelope drift")

    _assert_equal(command["intent"], intent, "command intent drift")
    _assert_equal(
        command["intentDigest"], digests["action_intent.json"], "command intent digest drift"
    )
    _assert_equal(dispatch["command"], command, "dispatch command drift")
    _assert_equal(
        dispatch["commandDigest"], digests["action_command.json"], "dispatch command digest drift"
    )
    _assert_equal(observation["dispatchRequest"], dispatch, "unknown observation request drift")
    _assert_equal(
        observation["dispatchRequestDigest"],
        digests["dispatch_request.json"],
        "unknown observation request digest drift",
    )

    expected_receipt_bindings = {
        "actionId": intent["actionId"],
        "commandDigest": digests["action_command.json"],
        "commandId": command["commandId"],
        "dispatchAttemptId": dispatch["dispatchAttemptId"],
        "dispatchRequestDigest": digests["dispatch_request.json"],
        "idempotencyKey": command["idempotencyKey"],
        "intentDigest": digests["action_intent.json"],
    }
    for field, expected in expected_receipt_bindings.items():
        _assert_equal(receipt[field], expected, f"receipt {field} binding drift")
        _assert_equal(query[field], expected, f"acceptance query {field} binding drift")
    _assert_equal(query["unknownSourceId"], receipt["receiptId"], "query source receipt drift")
    _assert_equal(
        query["providerOperationId"],
        receipt["providerOperationId"],
        "query provider operation drift",
    )

    scope_fields = ("tenantId", "workspaceId", "provider", "channelId")
    for field in scope_fields:
        expected = capability[field]
        for filename, document in (
            ("inbound_read_request.json", read_request),
            ("inbound_page.json", page),
            ("action_receipt.json", receipt),
            ("acceptance_query.json", query),
        ):
            _assert_equal(document[field], expected, f"{filename} {field} scope drift")

    decoded_page = decoded_models["inbound_page.json"]
    decoded_request = decoded_models["inbound_read_request.json"]
    decoded_capability = decoded_models["capability_snapshot.json"]
    decoded_command = decoded_models["action_command.json"]
    decoded_dispatch = decoded_models["dispatch_request.json"]
    decoded_receipt = decoded_models["action_receipt.json"]
    decoded_query = decoded_models["acceptance_query.json"]
    decoded_page.validate_request_binding(decoded_request)
    decoded_page.validate_capability_binding(decoded_capability)
    decoded_command.validate_capability_binding(decoded_capability)
    decoded_receipt.validate_dispatch_binding(decoded_dispatch)
    decoded_query.validate_request_binding(decoded_dispatch)
    decoded_query.validate_receipt_source_binding(decoded_receipt, decoded_dispatch)
    decoded_query.validate_capability_binding(decoded_capability, decoded_dispatch)


def verify() -> int:
    errors: list[str] = []
    try:
        manifest = _load_json(MANIFEST_PATH.read_bytes(), filename="manifest.json")
        _require_exact_type(
            manifest.get("schemaVersion"), int, "manifest schemaVersion must be an integer"
        )
        _assert_equal(manifest["schemaVersion"], 1, "manifest schema version drift")
        entries = manifest.get("vectors")
        if type(entries) is not list:
            raise ValueError("manifest vectors must be a list")

        expected_inventory = tuple(
            (filename, model.__name__) for filename, model in EXPECTED_VECTORS
        )
        actual_inventory = tuple(
            (entry.get("filename"), entry.get("model")) for entry in entries if type(entry) is dict
        )
        _assert_equal(actual_inventory, expected_inventory, "golden vector inventory drift")
        _assert_equal(len(actual_inventory), len(entries), "invalid manifest vector entry")
        expected_files = {filename for filename, _ in EXPECTED_VECTORS} | {"manifest.json"}
        actual_paths = tuple(FIXTURE_DIRECTORY.iterdir())
        actual_files = {path.name for path in actual_paths}
        _assert_equal(actual_files, expected_files, "golden fixture file inventory drift")
        for path in actual_paths:
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"golden fixture must be a regular non-symlink file: {path.name}")

        documents: dict[str, dict[str, Any]] = {}
        digests: dict[str, str] = {}
        decoded_models: dict[str, Any] = {}
        for entry, (filename, model) in zip(entries, EXPECTED_VECTORS):
            _assert_equal(
                set(entry),
                {"byteLength", "digest", "filename", "model"},
                f"manifest entry fields drift: {filename}",
            )
            _require_exact_type(
                entry["byteLength"], int, f"byteLength must be an integer: {filename}"
            )
            if entry["byteLength"] < 1:
                raise ValueError(f"byteLength must be positive: {filename}")
            _require_exact_type(entry["digest"], str, f"digest must be a string: {filename}")
            if re.fullmatch(r"[0-9a-f]{64}", entry["digest"]) is None:
                raise ValueError(f"digest must be 64 lowercase hexadecimal characters: {filename}")
            raw = (FIXTURE_DIRECTORY / filename).read_bytes()
            document = _load_json(raw, filename=filename)
            _assert_equal(_canonical_json(document), raw, f"non-canonical golden JSON: {filename}")
            _assert_equal(len(raw), entry.get("byteLength"), f"byte length drift: {filename}")
            digest = _digest(model.__name__, raw)
            _assert_equal(digest, entry.get("digest"), f"domain digest drift: {filename}")
            decoded = model.from_json_bytes(raw)
            _assert_equal(
                decoded.canonical_bytes(), raw, f"production codec byte drift: {filename}"
            )
            _assert_equal(
                decoded.canonical_digest(), digest, f"production codec digest drift: {filename}"
            )
            documents[filename] = document
            digests[filename] = digest
            decoded_models[filename] = decoded

        idempotency = manifest.get("idempotencyKey")
        if type(idempotency) is not dict:
            raise ValueError("manifest idempotencyKey must be an object")
        _assert_equal(
            idempotency.get("intentFilename"), "action_intent.json", "idempotency intent drift"
        )
        intent_document = documents["action_intent.json"]
        body = {
            "actionId": intent_document["actionId"],
            "channelId": intent_document["conversation"]["channelId"],
            "provider": intent_document["conversation"]["provider"],
            "tenantId": intent_document["tenantId"],
            "workspaceId": intent_document["workspaceId"],
        }
        independent_key = hashlib.sha256(
            b"quantum-entanglement.native-im/idempotency-key/1\n" + _canonical_json(body)
        ).hexdigest()
        _assert_equal(independent_key, idempotency.get("value"), "idempotency key drift")
        decoded_intent = IMActionIntentV1.from_json_bytes(
            (FIXTURE_DIRECTORY / "action_intent.json").read_bytes()
        )
        _assert_equal(
            derive_im_idempotency_key_v1(decoded_intent),
            independent_key,
            "production idempotency derivation drift",
        )
        _verify_bindings(documents, digests, decoded_models)
    except (KeyError, OSError, TypeError, ValueError) as failure:
        errors.append(str(failure))

    for message in errors:
        print(message, file=sys.stderr)
    if errors:
        return 1
    print(f"native IM V1 golden vectors verified: {len(EXPECTED_VECTORS)} vectors")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 1:
        print("usage: verify_native_im_v1_golden.py", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(verify())
