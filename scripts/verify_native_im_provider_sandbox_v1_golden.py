#!/usr/bin/env python3
"""Read-only verification for native-IM provider-sandbox approval provenance."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from quantum_entanglement.native_im import (  # noqa: E402
    IMInboundPageV1,
    IMInboundReadRequestV1,
)
from quantum_entanglement.native_im_sandbox_approval import (  # noqa: E402
    NativeIMSandboxApprovalV1,
)
from quantum_entanglement.native_im_sandbox_provenance import (  # noqa: E402
    NativeIMSandboxAdmissionProvenanceV1,
)

FIXTURE_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "native_im" / "provider_sandbox" / "v1"
EXPECTED_VECTORS: tuple[tuple[str, type[Any]], ...] = (
    ("approval.json", NativeIMSandboxApprovalV1),
    ("provenance.json", NativeIMSandboxAdmissionProvenanceV1),
    ("inbound_read_request.json", IMInboundReadRequestV1),
    ("inbound_page.json", IMInboundPageV1),
)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("provider sandbox golden JSON contains duplicate keys")
        result[key] = value
    return result


def _decode_json(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("provider sandbox golden JSON contains a non-finite number")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"provider sandbox golden JSON is invalid: {label}") from error
    if type(value) is not dict:
        raise ValueError(f"provider sandbox golden JSON root is invalid: {label}")
    return value


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _domain_digest(model_name: str, raw: bytes) -> str:
    domain = f"quantum-entanglement.native-im/{model_name}/1\n".encode()
    return hashlib.sha256(domain + raw).hexdigest()


def verify() -> None:
    manifest_raw = (FIXTURE_ROOT / "manifest.json").read_bytes()
    manifest = _decode_json(manifest_raw, "manifest.json")
    if _canonical_json(manifest) != manifest_raw:
        raise ValueError("provider sandbox golden manifest is not canonical")
    vectors = manifest.get("vectors")
    if manifest.get("schemaVersion") != 1 or type(vectors) is not list:
        raise ValueError("provider sandbox golden manifest shape differs")
    expected_inventory = tuple((filename, model.__name__) for filename, model in EXPECTED_VECTORS)
    actual_inventory = tuple(
        (entry.get("filename"), entry.get("model")) for entry in vectors if type(entry) is dict
    )
    if actual_inventory != expected_inventory or len(vectors) != len(EXPECTED_VECTORS):
        raise ValueError("provider sandbox golden vector inventory differs")
    expected_files = {"manifest.json", *(filename for filename, _ in EXPECTED_VECTORS)}
    paths = tuple(FIXTURE_ROOT.iterdir())
    if {path.name for path in paths} != expected_files:
        raise ValueError("provider sandbox golden file inventory differs")
    if any(not path.is_file() or path.is_symlink() for path in paths):
        raise ValueError("provider sandbox golden fixture must be a regular file")

    decoded: dict[str, Any] = {}
    for index, (filename, model) in enumerate(EXPECTED_VECTORS):
        entry = vectors[index]
        if type(entry) is not dict or set(entry) != {
            "byteLength",
            "digest",
            "filename",
            "model",
        }:
            raise ValueError("provider sandbox golden manifest vector shape differs")
        raw = (FIXTURE_ROOT / filename).read_bytes()
        if _canonical_json(_decode_json(raw, filename)) != raw:
            raise ValueError(f"provider sandbox golden vector is not canonical: {filename}")
        value = model.from_json_bytes(raw)
        if value.canonical_bytes() != raw:
            raise ValueError(f"provider sandbox golden model bytes differ: {filename}")
        digest = _domain_digest(model.__name__, raw)
        if (
            entry
            != {
                "byteLength": len(raw),
                "digest": digest,
                "filename": filename,
                "model": model.__name__,
            }
            or value.canonical_digest() != digest
        ):
            raise ValueError(f"provider sandbox golden digest differs: {filename}")
        decoded[filename] = value

    approval = decoded["approval.json"]
    provenance = decoded["provenance.json"]
    if type(approval) is not NativeIMSandboxApprovalV1:
        raise ValueError("provider sandbox approval decoder returned a different type")
    if type(provenance) is not NativeIMSandboxAdmissionProvenanceV1:
        raise ValueError("provider sandbox provenance decoder returned a different type")
    approval_binding = (
        approval.approval_id,
        approval.authority_revision,
        approval.canonical_digest(),
        approval.configuration_binding_digest,
        approval.profile_id,
        approval.profile_revision,
        approval.profile_digest,
        approval.transport_contract_id,
        approval.transport_contract_digest,
        approval.mapper_contract_id,
        approval.mapper_contract_digest,
    )
    provenance_binding = (
        provenance.approval_id,
        provenance.authority_revision,
        provenance.approval_digest,
        provenance.configuration_binding_digest,
        provenance.profile_id,
        provenance.profile_revision,
        provenance.profile_digest,
        provenance.transport_contract_id,
        provenance.transport_contract_digest,
        provenance.mapper_contract_id,
        provenance.mapper_contract_digest,
    )
    if provenance_binding != approval_binding:
        raise ValueError("provider sandbox provenance is not bound to its approval")
    request = decoded["inbound_read_request.json"]
    page = decoded["inbound_page.json"]
    if type(request) is not IMInboundReadRequestV1:
        raise ValueError("provider sandbox request decoder returned a different type")
    if type(page) is not IMInboundPageV1:
        raise ValueError("provider sandbox page decoder returned a different type")
    page.validate_request_binding(request)
    approved_scope = (
        approval.tenant_id,
        approval.workspace_id,
        approval.provider,
        approval.channel_id,
    )
    if (
        (request.tenant_id, request.workspace_id, request.provider, request.channel_id)
        != approved_scope
        or (page.tenant_id, page.workspace_id, page.provider, page.channel_id) != approved_scope
        or any(
            envelope.event.conversation.conversation_id not in approval.allowed_conversation_ids
            for envelope in page.envelopes
        )
    ):
        raise ValueError("provider sandbox request/page scope differs from approval")
    if (
        provenance.read_request_digest != request.canonical_digest()
        or provenance.page_digest != page.canonical_digest()
        or any(
            envelope.event.transport_evidence_digest != provenance.transport_evidence_digest
            for envelope in page.envelopes
        )
    ):
        raise ValueError("provider sandbox provenance is not bound to request/page evidence")


def main() -> int:
    try:
        verify()
    except (OSError, TypeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print("native IM provider sandbox V1 golden vectors verified: 4 vectors")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 1:
        print("usage: verify_native_im_provider_sandbox_v1_golden.py", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main())
