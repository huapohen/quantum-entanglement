#!/usr/bin/env python3
"""Read-only verification for the private stored-event envelope V1 golden."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from quantum_entanglement._stored_event_envelope_codec import (  # noqa: E402
    STORED_EVENT_ENVELOPE_DOMAIN,
    STORED_EVENT_ENVELOPE_SCHEMA_VERSION,
    _stored_event_envelope_from_raw_row,
    _stored_event_envelope_from_values,
)

FIXTURE_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "stored_event_envelope" / "v1"
EXPECTED_FILES = {"envelope.json", "manifest.json"}
EXPECTED_BODY_FIELDS = {
    "actorId",
    "causationId",
    "correlationId",
    "eventId",
    "eventType",
    "globalPosition",
    "idempotencyKey",
    "payload",
    "schemaVersion",
    "sequence",
    "streamId",
    "timestamp",
}
MAX_FIXTURE_BYTES = 2_048


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("stored event golden JSON contains duplicate keys")
        result[key] = value
    return result


def _reject_constant(_value: str) -> Any:
    raise ValueError("stored event golden JSON contains a non-finite number")


def _read_regular_file(filename: str) -> bytes:
    path = FIXTURE_ROOT / filename
    if path.is_symlink() or not path.is_file():
        raise ValueError("stored event golden fixture must be a regular file")
    raw = path.read_bytes()
    if not raw or len(raw) > MAX_FIXTURE_BYTES:
        raise ValueError("stored event golden fixture violates its byte limit")
    return raw


def _decode_object(raw: bytes, label: str) -> dict[str, Any]:
    try:
        decoded = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError):
        raise ValueError(f"stored event golden JSON is invalid: {label}") from None
    if type(decoded) is not dict:
        raise ValueError(f"stored event golden JSON root is invalid: {label}")
    return decoded


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _raw_row(body: dict[str, Any], payload_json: str) -> sqlite3.Row:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            """
            SELECT
                ? AS global_position,
                ? AS stream_id,
                ? AS sequence,
                ? AS event_id,
                ? AS event_type,
                ? AS actor_id,
                ? AS timestamp,
                ? AS payload_json,
                ? AS correlation_id,
                ? AS causation_id,
                ? AS idempotency_key
            """,
            (
                body["globalPosition"],
                body["streamId"],
                body["sequence"],
                body["eventId"],
                body["eventType"],
                body["actorId"],
                body["timestamp"],
                payload_json,
                body["correlationId"],
                body["causationId"],
                body["idempotencyKey"],
            ),
        ).fetchone()
        if type(row) is not sqlite3.Row:
            raise ValueError("stored event golden SQLite row type differs")
        return row
    finally:
        connection.close()


def verify() -> None:
    if FIXTURE_ROOT.is_symlink() or not FIXTURE_ROOT.is_dir():
        raise ValueError("stored event golden fixture root is invalid")
    paths = tuple(FIXTURE_ROOT.iterdir())
    if {path.name for path in paths} != EXPECTED_FILES:
        raise ValueError("stored event golden file inventory differs")
    if any(path.is_symlink() or not path.is_file() for path in paths):
        raise ValueError("stored event golden fixture inventory is not regular")

    manifest_raw = _read_regular_file("manifest.json")
    manifest = _decode_object(manifest_raw, "manifest.json")
    if _canonical_json(manifest) != manifest_raw:
        raise ValueError("stored event golden manifest is not canonical")
    if set(manifest) != {"domain", "schemaVersion", "vectors"}:
        raise ValueError("stored event golden manifest shape differs")
    vectors = manifest["vectors"]
    if (
        manifest["schemaVersion"] != STORED_EVENT_ENVELOPE_SCHEMA_VERSION
        or type(manifest["schemaVersion"]) is not int
        or manifest["domain"] != STORED_EVENT_ENVELOPE_DOMAIN
        or type(vectors) is not list
        or len(vectors) != 1
        or type(vectors[0]) is not dict
    ):
        raise ValueError("stored event golden manifest contract differs")
    entry = vectors[0]
    if set(entry) != {"byteLength", "digest", "filename", "model"}:
        raise ValueError("stored event golden vector shape differs")

    raw = _read_regular_file("envelope.json")
    body = _decode_object(raw, "envelope.json")
    if set(body) != EXPECTED_BODY_FIELDS or _canonical_json(body) != raw:
        raise ValueError("stored event golden body differs from the exact schema")
    if (
        body["schemaVersion"] != STORED_EVENT_ENVELOPE_SCHEMA_VERSION
        or type(body["schemaVersion"]) is not int
        or type(body["payload"]) is not dict
    ):
        raise ValueError("stored event golden body contract differs")

    digest = hashlib.sha256(STORED_EVENT_ENVELOPE_DOMAIN.encode("utf-8") + raw).hexdigest()
    expected_entry = {
        "byteLength": len(raw),
        "digest": digest,
        "filename": "envelope.json",
        "model": "StoredEventEnvelopeV1",
    }
    if entry != expected_entry:
        raise ValueError("stored event golden manifest binding differs")

    payload_json = _canonical_json(body["payload"]).decode("utf-8")
    from_values = _stored_event_envelope_from_values(
        event_id=body["eventId"],
        stream_id=body["streamId"],
        event_type=body["eventType"],
        actor_id=body["actorId"],
        timestamp=body["timestamp"],
        correlation_id=body["correlationId"],
        causation_id=body["causationId"],
        idempotency_key=body["idempotencyKey"],
        payload_json=payload_json,
        sequence=body["sequence"],
        global_position=body["globalPosition"],
    )
    from_row = _stored_event_envelope_from_raw_row(_raw_row(body, payload_json))
    if (
        from_values.canonical_bytes() != raw
        or from_row.canonical_bytes() != raw
        or from_values.digest() != digest
        or from_row.digest() != digest
    ):
        raise ValueError("stored event golden codec or raw-row reconstruction differs")


def main() -> int:
    try:
        verify()
    except (OSError, TypeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print("stored event envelope V1 golden vectors verified: 1 vector")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 1:
        print("usage: verify_stored_event_envelope_v1_golden.py", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main())
