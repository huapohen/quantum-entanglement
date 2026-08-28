from __future__ import annotations

import copy
import inspect
import json
import pickle
from dataclasses import replace
from pathlib import Path

import pytest

from quantum_entanglement.native_im_sandbox import (
    NativeIMInboundOnlySandboxAdapter,
    NativeIMSandboxDisabledError,
    compose_default_native_im_sandbox_v1,
)
from quantum_entanglement.native_im_sandbox_approval_store import (
    SQLiteNativeIMSandboxApprovalHighWaterV1,
)
from quantum_entanglement.native_im_sandbox_composition import (
    NativeIMProviderSandboxManifestV1,
    NativeIMProviderSandboxRegistrationV1,
    NativeIMSandboxCompositionError,
    compose_approved_native_im_sandbox_v1,
)
from tests.test_native_im_sandbox_authority import approved_authority_for
from tests.test_native_im_sandbox_config import bound_configuration
from tests.test_native_im_sandbox_inbound_adapter import (
    adapter_inputs,
    fixture_health_evidence,
)


def manifest_for(profile, **changes: object) -> NativeIMProviderSandboxManifestV1:
    values: dict[str, object] = {
        "schema_version": 1,
        "registration_id": "test-native-im-provider-registration-v1",
        "provider": profile.provider,
        "profile_id": profile.profile_id,
        "profile_revision": profile.revision,
        "profile_digest": profile.canonical_digest(),
        "transport_contract_id": "test-native-im-transport-v1",
        "transport_contract_digest": "2" * 64,
        "mapper_contract_id": "test-native-im-mapper-v1",
        "mapper_contract_digest": "3" * 64,
        "source_evidence_digest": "9" * 64,
    }
    values.update(changes)
    return NativeIMProviderSandboxManifestV1(**values)  # type: ignore[arg-type]


def composition_inputs(tmp_path: Path):
    _, request, configuration, profile, transport, mapper, secrets, replay_guard = adapter_inputs()
    high_water = SQLiteNativeIMSandboxApprovalHighWaterV1(
        str((tmp_path / "approved-composition.sqlite3").resolve())
    )
    configuration, authority, _, approval, _ = approved_authority_for(
        configuration,
        profile,
        high_water=high_water,
    )
    manifest = manifest_for(profile)
    transport.health_evidence = fixture_health_evidence(
        configuration,
        profile,
        provider_manifest_digest=manifest.canonical_digest(),
    )
    registration = NativeIMProviderSandboxRegistrationV1(
        manifest,
        transport=transport,
        mapper=mapper,
        secret_provider=secrets,
        replay_guard=replay_guard,
    )
    return (
        configuration,
        profile,
        authority,
        registration,
        approval,
        high_water,
        request,
        transport,
        mapper,
        secrets,
    )


def test_manifest_round_trip_and_domain_separated_digest_are_stable() -> None:
    profile = adapter_inputs()[3]
    manifest = manifest_for(profile)
    encoded = manifest.canonical_bytes()

    assert NativeIMProviderSandboxManifestV1.from_dict(manifest.to_dict()) == manifest
    assert NativeIMProviderSandboxManifestV1.from_json_bytes(encoded) == manifest
    assert (
        encoded
        == json.dumps(
            manifest.to_dict(),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    )
    assert manifest.canonical_digest() == (
        "fa12837be989e6571f33ecdaaa0859c1107990c153c95ee04f90499181558fbe"
    )


def test_explicit_composition_is_side_effect_free_until_adapter_operation(
    tmp_path: Path,
) -> None:
    (
        configuration,
        profile,
        authority,
        registration,
        _,
        high_water,
        _,
        transport,
        mapper,
        secrets,
    ) = composition_inputs(tmp_path)
    try:
        adapter = compose_approved_native_im_sandbox_v1(
            configuration,
            profile,
            authority,
            registration,
            clock=lambda: "2026-08-28T12:00:00.000000Z",
        )
        assert type(adapter) is NativeIMInboundOnlySandboxAdapter
        assert transport.health_calls == 0
        assert transport.read_calls == 0
        assert mapper.calls == 0
        assert secrets.references == []
    finally:
        high_water.close()


@pytest.mark.asyncio
async def test_composed_fixture_adapter_can_probe_only_after_all_approval_gates(
    tmp_path: Path,
) -> None:
    (
        configuration,
        profile,
        authority,
        registration,
        _,
        high_water,
        _,
        transport,
        _,
        secrets,
    ) = composition_inputs(tmp_path)
    try:
        adapter = compose_approved_native_im_sandbox_v1(
            configuration,
            profile,
            authority,
            registration,
            clock=lambda: "2026-08-28T12:00:00.000000Z",
        )
        health = await adapter.probe_health()
        assert health.healthy is True
        assert transport.health_calls == 1
        assert secrets.references == [configuration.credential_ref]
        await adapter.aclose()
    finally:
        high_water.close()


def test_default_composition_remains_permanently_closed_and_has_no_provider_inputs() -> None:
    signature = inspect.signature(compose_default_native_im_sandbox_v1)
    assert tuple(signature.parameters) == ("configuration",)
    with pytest.raises(NativeIMSandboxDisabledError):
        compose_default_native_im_sandbox_v1(bound_configuration())


def test_explicit_composition_rejects_non_durable_authority(tmp_path: Path) -> None:
    _, _, configuration, profile, transport, mapper, secrets, replay_guard = adapter_inputs()
    configuration, authority, _, _, memory_store = approved_authority_for(
        configuration,
        profile,
    )
    registration = NativeIMProviderSandboxRegistrationV1(
        manifest_for(profile),
        transport=transport,
        mapper=mapper,
        secret_provider=secrets,
        replay_guard=replay_guard,
    )
    with pytest.raises(NativeIMSandboxCompositionError) as raised:
        compose_approved_native_im_sandbox_v1(
            configuration,
            profile,
            authority,
            registration,
            clock=lambda: "2026-08-28T12:00:00.000000Z",
        )
    assert raised.value.code == "native_im_sandbox_durable_authority_required"
    memory_store.close()


@pytest.mark.parametrize(
    ("field_name", "changed"),
    (
        ("provider", "other-provider"),
        ("profile_id", "other-profile"),
        ("profile_revision", "other-revision"),
        ("profile_digest", "4" * 64),
        ("transport_contract_id", "other-transport"),
        ("transport_contract_digest", "5" * 64),
        ("mapper_contract_id", "other-mapper"),
        ("mapper_contract_digest", "6" * 64),
    ),
)
def test_manifest_drift_rejects_before_touching_registered_components(
    tmp_path: Path,
    field_name: str,
    changed: object,
) -> None:
    (
        configuration,
        profile,
        authority,
        registration,
        _,
        high_water,
        _,
        transport,
        mapper,
        secrets,
    ) = composition_inputs(tmp_path)
    drifted = NativeIMProviderSandboxRegistrationV1(
        replace(manifest_for(profile), **{field_name: changed}),
        transport=transport,
        mapper=mapper,
        secret_provider=secrets,
        replay_guard=object(),
    )
    try:
        with pytest.raises(NativeIMSandboxCompositionError) as raised:
            compose_approved_native_im_sandbox_v1(
                configuration,
                profile,
                authority,
                drifted,
                clock=lambda: "2026-08-28T12:00:00.000000Z",
            )
        assert raised.value.code == "native_im_sandbox_provider_manifest_mismatch"
        assert transport.health_calls == transport.read_calls == 0
        assert mapper.calls == 0
        assert secrets.references == []
        assert registration is not drifted
    finally:
        high_water.close()


def test_invalid_registered_component_fails_after_approval_without_calling_others(
    tmp_path: Path,
) -> None:
    (
        configuration,
        profile,
        authority,
        _,
        _,
        high_water,
        _,
        _,
        mapper,
        secrets,
    ) = composition_inputs(tmp_path)
    registration = NativeIMProviderSandboxRegistrationV1(
        manifest_for(profile),
        transport=object(),
        mapper=mapper,
        secret_provider=secrets,
        replay_guard=object(),
    )
    try:
        with pytest.raises(NativeIMSandboxCompositionError) as raised:
            compose_approved_native_im_sandbox_v1(
                configuration,
                profile,
                authority,
                registration,
                clock=lambda: "2026-08-28T12:00:00.000000Z",
            )
        assert raised.value.code == "native_im_sandbox_registered_transport_invalid"
        assert mapper.calls == 0
        assert secrets.references == []
    finally:
        high_water.close()


def test_registration_detects_component_type_swap_and_cannot_be_copied_or_pickled(
    tmp_path: Path,
) -> None:
    (
        configuration,
        profile,
        authority,
        registration,
        _,
        high_water,
        _,
        _,
        _,
        _,
    ) = composition_inputs(tmp_path)
    for operation in (copy.copy, copy.deepcopy, pickle.dumps):
        with pytest.raises(TypeError):
            operation(registration)
    object.__setattr__(
        registration,
        "_NativeIMProviderSandboxRegistrationV1__transport",
        object(),
    )
    try:
        with pytest.raises(NativeIMSandboxCompositionError) as raised:
            compose_approved_native_im_sandbox_v1(
                configuration,
                profile,
                authority,
                registration,
                clock=lambda: "2026-08-28T12:00:00.000000Z",
            )
        assert raised.value.code == "native_im_sandbox_registration_component_drift"
    finally:
        high_water.close()


def test_direct_adapter_construction_is_rejected_before_component_or_config_use() -> None:
    with pytest.raises(TypeError, match="approved composition"):
        NativeIMInboundOnlySandboxAdapter(  # type: ignore[call-arg]
            object(),
            object(),
            object(),
            object(),
            object(),
            object(),
            object(),
            object(),
            object(),
            clock=lambda: "2026-08-28T12:00:00.000000Z",
            _composition_token=object(),
        )


def test_manifest_and_registration_repr_redact_scope_and_build_digests(tmp_path: Path) -> None:
    (
        _,
        profile,
        _,
        registration,
        approval,
        high_water,
        _,
        _,
        _,
        _,
    ) = composition_inputs(tmp_path)
    manifest = manifest_for(profile)
    rendered = f"{manifest!r} {registration!r}"
    try:
        for hidden in (
            manifest.provider,
            manifest.profile_digest,
            manifest.transport_contract_digest,
            manifest.mapper_contract_digest,
            approval.canonical_digest(),
        ):
            assert hidden not in rendered
    finally:
        high_water.close()
