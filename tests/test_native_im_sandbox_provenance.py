from __future__ import annotations

import json
from dataclasses import replace

import pytest

from quantum_entanglement.native_im_sandbox_provenance import (
    NativeIMSandboxAdmissionProvenanceV1,
    NativeIMSandboxExchangeAdmissionProvenanceV1,
    decode_native_im_sandbox_admission_provenance_v1,
)
from tests.test_native_im_read_exchange import evidence as exchange_evidence


def provenance(**changes: object) -> NativeIMSandboxAdmissionProvenanceV1:
    values: dict[str, object] = {
        "schema_version": 1,
        "approval_id": "test-approval",
        "authority_revision": 7,
        "approval_digest": "a" * 64,
        "configuration_binding_digest": "b" * 64,
        "profile_id": "test-profile",
        "profile_revision": "test-profile-revision",
        "profile_digest": "c" * 64,
        "provider_manifest_digest": "d" * 64,
        "transport_contract_id": "test-transport-v1",
        "transport_contract_digest": "e" * 64,
        "mapper_contract_id": "test-mapper-v1",
        "mapper_contract_digest": "f" * 64,
        "read_request_digest": "1" * 64,
        "page_digest": "2" * 64,
        "transport_evidence_digest": "3" * 64,
        "mapping_evidence_digest": "4" * 64,
    }
    values.update(changes)
    return NativeIMSandboxAdmissionProvenanceV1(**values)  # type: ignore[arg-type]


def test_provenance_round_trip_and_domain_digest_are_stable() -> None:
    value = provenance()
    encoded = value.canonical_bytes()

    assert NativeIMSandboxAdmissionProvenanceV1.from_dict(value.to_dict()) == value
    assert NativeIMSandboxAdmissionProvenanceV1.from_json_bytes(encoded) == value
    assert (
        encoded
        == json.dumps(
            value.to_dict(),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    )
    assert value.canonical_digest() == (
        "68b90e956bca451d8411177059c7ffbc15ea24207a58750220eb0e1466356094"
    )


@pytest.mark.parametrize(
    ("field_name", "changed"),
    (
        ("approval_id", "other-approval"),
        ("authority_revision", 8),
        ("approval_digest", "5" * 64),
        ("configuration_binding_digest", "6" * 64),
        ("profile_id", "other-profile"),
        ("profile_revision", "other-revision"),
        ("profile_digest", "7" * 64),
        ("provider_manifest_digest", "8" * 64),
        ("transport_contract_id", "other-transport"),
        ("transport_contract_digest", "9" * 64),
        ("mapper_contract_id", "other-mapper"),
        ("mapper_contract_digest", "0" * 64),
        ("read_request_digest", "a" * 64),
        ("page_digest", "b" * 64),
        ("transport_evidence_digest", "c" * 64),
        ("mapping_evidence_digest", "d" * 64),
    ),
)
def test_provenance_digest_binds_every_axis(field_name: str, changed: object) -> None:
    value = provenance()
    assert replace(value, **{field_name: changed}).canonical_digest() != value.canonical_digest()


def test_provenance_decoder_rejects_unknown_missing_duplicate_and_subclass_values() -> None:
    value = provenance()
    missing = value.to_dict()
    del missing["approvalId"]
    with pytest.raises(ValueError):
        NativeIMSandboxAdmissionProvenanceV1.from_dict(missing)
    with pytest.raises(ValueError):
        NativeIMSandboxAdmissionProvenanceV1.from_dict({**value.to_dict(), "future": 1})
    duplicate = b'{"approvalId":"duplicate",' + value.canonical_bytes()[1:]
    with pytest.raises(ValueError):
        NativeIMSandboxAdmissionProvenanceV1.from_json_bytes(duplicate)

    class ProvenanceSubclass(NativeIMSandboxAdmissionProvenanceV1):
        pass

    with pytest.raises(TypeError):
        ProvenanceSubclass(**value.__dict__)
    with pytest.raises(TypeError):
        ProvenanceSubclass.from_dict(value.to_dict())


def test_provenance_repr_hides_all_identity_and_evidence_values() -> None:
    value = provenance()
    rendered = repr(value)
    for hidden in (
        value.approval_id,
        value.approval_digest,
        value.profile_id,
        value.profile_digest,
        value.provider_manifest_digest,
        value.transport_evidence_digest,
        value.mapping_evidence_digest,
    ):
        assert hidden not in rendered


def exchange_provenance(
    **changes: object,
) -> NativeIMSandboxExchangeAdmissionProvenanceV1:
    legacy = provenance(
        read_request_digest=exchange_evidence().read_request_digest,
        transport_evidence_digest=exchange_evidence().event_source_evidence_digest,
    )
    values = {
        **legacy.__dict__,
        "read_exchange_evidence": exchange_evidence(),
    }
    values.update(changes)
    return NativeIMSandboxExchangeAdmissionProvenanceV1(
        **values  # type: ignore[arg-type]
    )


def test_exchange_provenance_round_trip_binds_nested_evidence() -> None:
    value = exchange_provenance()

    assert NativeIMSandboxExchangeAdmissionProvenanceV1.from_dict(value.to_dict()) == value
    assert (
        NativeIMSandboxExchangeAdmissionProvenanceV1.from_json_bytes(value.canonical_bytes())
        == value
    )
    assert decode_native_im_sandbox_admission_provenance_v1(value.canonical_bytes()) == value
    assert (
        decode_native_im_sandbox_admission_provenance_v1(provenance().canonical_bytes())
        == provenance()
    )
    assert value.canonical_digest() != provenance().canonical_digest()


def test_exchange_provenance_rejects_request_or_event_source_mismatch() -> None:
    with pytest.raises(ValueError, match="does not bind"):
        exchange_provenance(read_request_digest="f" * 64)
    with pytest.raises(ValueError, match="does not bind"):
        exchange_provenance(transport_evidence_digest="f" * 64)


def test_exchange_provenance_repr_hides_nested_exchange_values() -> None:
    value = exchange_provenance()
    rendered = repr(value)

    assert value.read_exchange_evidence.read_request_id not in rendered
    assert value.read_exchange_evidence.evidence_digest not in rendered
    assert value.read_exchange_evidence.exchange_security_evidence_digest not in rendered
