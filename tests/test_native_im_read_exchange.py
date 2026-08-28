from __future__ import annotations

from dataclasses import replace

import pytest

from quantum_entanglement.native_im_read_exchange import (
    NativeIMInboundReadExchangeEvidenceV1,
    derive_native_im_read_exchange_evidence_digest_v1,
)
from tests.test_native_im_contract import inbound_read_request

NOW = "2026-08-28T12:00:00.000001Z"


def evidence(**changes: object) -> NativeIMInboundReadExchangeEvidenceV1:
    request = inbound_read_request(
        after_cursor="cursor-1",
        after_sequence=1,
        snapshot_token="snapshot-1",
        read_request_id="read-exchange-1",
    )
    values: dict[str, object] = {
        "read_request_id": request.read_request_id,
        "read_request_digest": request.canonical_digest(),
        "after_cursor": request.after_cursor,
        "after_sequence": request.after_sequence,
        "snapshot_token": request.snapshot_token,
        "received_at": NOW,
        "request_intent_digest": "a" * 64,
        "exchange_security_evidence_digest": "b" * 64,
        "event_source_evidence_digest": "c" * 64,
    }
    values.update(changes)
    digest = derive_native_im_read_exchange_evidence_digest_v1(
        read_request_id=values["read_request_id"],  # type: ignore[arg-type]
        read_request_digest=values["read_request_digest"],  # type: ignore[arg-type]
        after_cursor=values["after_cursor"],  # type: ignore[arg-type]
        after_sequence=values["after_sequence"],  # type: ignore[arg-type]
        snapshot_token=values["snapshot_token"],  # type: ignore[arg-type]
        received_at=values["received_at"],  # type: ignore[arg-type]
        request_intent_digest=values["request_intent_digest"],  # type: ignore[arg-type]
        exchange_security_evidence_digest=values[  # type: ignore[arg-type]
            "exchange_security_evidence_digest"
        ],
        event_source_evidence_digest=values[  # type: ignore[arg-type]
            "event_source_evidence_digest"
        ],
    )
    return NativeIMInboundReadExchangeEvidenceV1(
        schema_version=1,
        **values,  # type: ignore[arg-type]
        evidence_digest=digest,
    )


def test_read_exchange_evidence_is_canonical_and_request_bound() -> None:
    value = evidence()
    request = inbound_read_request(
        after_cursor="cursor-1",
        after_sequence=1,
        snapshot_token="snapshot-1",
        read_request_id="read-exchange-1",
    )

    value.validate_request_binding(request)
    assert NativeIMInboundReadExchangeEvidenceV1.from_dict(value.to_dict()) == value
    assert NativeIMInboundReadExchangeEvidenceV1.from_json_bytes(value.canonical_bytes()) == value
    assert value.canonical_bytes() == value.canonical_bytes()
    assert len(value.canonical_digest()) == 64
    assert "b" * 64 not in repr(value)


@pytest.mark.parametrize(
    "field,replacement",
    (
        ("read_request_id", "other-read"),
        ("read_request_digest", "d" * 64),
        ("after_cursor", "other-cursor"),
        ("after_sequence", 2),
        ("snapshot_token", "other-snapshot"),
    ),
)
def test_read_exchange_evidence_rejects_cross_request_binding(
    field: str,
    replacement: object,
) -> None:
    value = evidence()
    request = inbound_read_request(
        after_cursor="cursor-1",
        after_sequence=1,
        snapshot_token="snapshot-1",
        read_request_id="read-exchange-1",
    )
    changed = evidence(**{field: replacement})
    with pytest.raises(ValueError, match="does not bind"):
        changed.validate_request_binding(request)

    assert changed.evidence_digest != value.evidence_digest


def test_read_exchange_evidence_digest_binds_every_exchange_axis() -> None:
    baseline = evidence()
    changes = (
        {"received_at": "2026-08-28T12:00:01.000001Z"},
        {"request_intent_digest": "d" * 64},
        {"exchange_security_evidence_digest": "e" * 64},
        {"event_source_evidence_digest": "f" * 64},
    )
    for change in changes:
        assert evidence(**change).evidence_digest != baseline.evidence_digest


def test_read_exchange_evidence_keeps_stable_event_source_separate_from_exchange() -> None:
    first = evidence(
        received_at="2026-08-28T12:00:00.000001Z",
        exchange_security_evidence_digest="1" * 64,
    )
    second = evidence(
        received_at="2026-08-28T12:00:01.000001Z",
        exchange_security_evidence_digest="2" * 64,
    )

    assert first.event_source_evidence_digest == second.event_source_evidence_digest
    assert first.evidence_digest != second.evidence_digest


def test_read_exchange_evidence_rejects_digest_self_report_and_unknown_fields() -> None:
    value = evidence()
    with pytest.raises(ValueError, match="does not bind"):
        replace(value, evidence_digest="f" * 64)
    with pytest.raises(ValueError, match="fields do not match"):
        NativeIMInboundReadExchangeEvidenceV1.from_dict({**value.to_dict(), "future": 1})


def test_read_exchange_evidence_requires_paired_continuation_state() -> None:
    with pytest.raises(ValueError, match="must be paired"):
        derive_native_im_read_exchange_evidence_digest_v1(
            read_request_id="read-1",
            read_request_digest="a" * 64,
            after_cursor="cursor-1",
            after_sequence=None,
            snapshot_token=None,
            received_at=NOW,
            request_intent_digest="b" * 64,
            exchange_security_evidence_digest="c" * 64,
            event_source_evidence_digest="d" * 64,
        )
