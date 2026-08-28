from __future__ import annotations

import copy
import os
import pickle
import threading
from dataclasses import replace

import pytest

from quantum_entanglement import process_identity
from quantum_entanglement.native_im_sandbox_approval import (
    NativeIMSandboxApprovalV1,
    native_im_secret_reference_binding_digest,
)
from quantum_entanglement.native_im_sandbox_approval_store import (
    NativeIMSandboxApprovalAuthorityStateV1,
    SQLiteNativeIMSandboxApprovalHighWaterV1,
)
from quantum_entanglement.native_im_sandbox_authority import (
    InMemoryNativeIMSandboxApprovalAuthorityV1,
    NativeIMSandboxApprovalAuthorityError,
    NativeIMSandboxApprovalPermitV1,
)
from tests.test_native_im_provider_profile import profile
from tests.test_native_im_sandbox_approval import sandbox_approval
from tests.test_native_im_sandbox_config import bound_configuration

NOW = "2026-08-28T12:00:00.000001Z"
EXPIRES = "2026-08-29T00:00:00.000001Z"


class MutableClock:
    def __init__(self, value: object = NOW) -> None:
        self.value = value

    def __call__(self) -> str:
        if isinstance(self.value, BaseException):
            raise self.value
        return self.value  # type: ignore[return-value]


def authority_inputs(
    *,
    now: object = NOW,
    expires_at: str = EXPIRES,
    approval_changes: dict[str, object] | None = None,
    high_water: SQLiteNativeIMSandboxApprovalHighWaterV1 | None = None,
):
    configuration = bound_configuration(
        QE_NATIVE_IM_APPROVAL_EXPIRES_AT=expires_at,
        QE_NATIVE_IM_APPROVAL_DIGEST="a" * 64,
    )
    changes = {
        "expires_at": expires_at,
        "configuration_binding_digest": configuration.approval_binding_digest,
    }
    if approval_changes:
        changes.update(approval_changes)
    approval = sandbox_approval(**changes)
    configuration = replace(
        configuration,
        approval_digest=approval.canonical_digest(),
    )
    clock = MutableClock(now)
    authority = InMemoryNativeIMSandboxApprovalAuthorityV1(
        approval,
        trusted_record_digest=approval.canonical_digest(),
        high_water=(
            SQLiteNativeIMSandboxApprovalHighWaterV1(":memory:")
            if high_water is None
            else high_water
        ),
        clock=clock,
    )
    return authority, configuration, profile(), approval, clock


def active_inputs(**kwargs: object):
    authority, configuration, provider_profile, approval, clock = authority_inputs(**kwargs)
    permit = authority.activate(configuration, provider_profile)
    return authority, permit, configuration, provider_profile, approval, clock


def approved_authority_for(
    configuration,
    provider_profile,
    *,
    high_water: SQLiteNativeIMSandboxApprovalHighWaterV1 | None = None,
    now: str = NOW,
):
    """Build one exact offline approved test authority for arbitrary fixture scope."""

    configuration = replace(
        configuration,
        approval_expires_at=EXPIRES,
        authority_revision=7,
        deployment_subject_digest="1" * 64,
        approval_digest="a" * 64,
    )
    approval = sandbox_approval(
        approval_id=configuration.approval_id,
        authority_revision=configuration.authority_revision,
        expires_at=configuration.approval_expires_at,
        provider=configuration.provider,
        tenant_id=configuration.tenant_id,
        workspace_id=configuration.workspace_id,
        channel_id=configuration.channel_id,
        allowed_conversation_ids=provider_profile.allowed_conversation_ids,
        profile_id=provider_profile.profile_id,
        profile_revision=provider_profile.revision,
        profile_digest=provider_profile.canonical_digest(),
        configuration_binding_digest=configuration.approval_binding_digest,
        deployment_subject_digest=configuration.deployment_subject_digest,
        origin=configuration.origin.canonical,
        approved_addresses=tuple(
            address.compressed for address in configuration.approved_addresses
        ),
        health_path=configuration.health_path.canonical,
        read_path=configuration.read_path.canonical,
        credential_ref_binding_digest=native_im_secret_reference_binding_digest(
            configuration.credential_ref,
            purpose="read_credential",
        ),
        verification_secret_ref_binding_digest=(
            native_im_secret_reference_binding_digest(
                configuration.verification_secret_ref,
                purpose="verification_secret",
            )
        ),
        verification_key_id=configuration.verification_key_id,
        page_limit=configuration.page_limit,
        max_response_bytes=configuration.max_response_bytes,
        connect_timeout_ms=configuration.connect_timeout_ms,
        read_timeout_ms=configuration.read_timeout_ms,
        requests_per_window=provider_profile.limits.requests_per_window,
        rate_limit_window_seconds=provider_profile.limits.rate_limit_window_seconds,
    )
    configuration = replace(configuration, approval_digest=approval.canonical_digest())
    store = (
        SQLiteNativeIMSandboxApprovalHighWaterV1(":memory:") if high_water is None else high_water
    )
    authority = InMemoryNativeIMSandboxApprovalAuthorityV1(
        approval,
        trusted_record_digest=approval.canonical_digest(),
        high_water=store,
        clock=lambda: now,
    )
    permit = authority.activate(configuration, provider_profile)
    return configuration, authority, permit, approval, store


def test_authority_activates_exact_record_and_rechecks_each_allowed_operation() -> None:
    authority, permit, _, _, approval, _ = active_inputs()

    health = authority.require_current(permit, operation="health")
    read = authority.require_current(permit, operation="read")

    assert health == approval
    assert read == approval
    assert health is not approval
    assert permit.approval_digest == approval.canonical_digest()
    assert permit.authority_revision == approval.authority_revision
    assert authority.revoked is False
    assert authority.generation == 0


def test_inert_record_cannot_be_used_or_forged_as_a_live_permit() -> None:
    authority, permit, _, _, approval, _ = active_inputs()

    with pytest.raises(TypeError):
        authority.require_current(approval, operation="read")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        NativeIMSandboxApprovalPermitV1(
            approval_id=permit.approval_id,
            authority_revision=permit.authority_revision,
            approval_digest=permit.approval_digest,
            generation=permit.generation,
            _authority_token=object(),
            _process_owner=process_identity.capture_process_owner(),
            _activation_token=object(),
        )


def test_authority_requires_an_independent_exact_record_digest() -> None:
    approval = sandbox_approval()
    with pytest.raises(NativeIMSandboxApprovalAuthorityError) as raised:
        InMemoryNativeIMSandboxApprovalAuthorityV1(
            approval,
            trusted_record_digest="f" * 64,
            high_water=SQLiteNativeIMSandboxApprovalHighWaterV1(":memory:"),
            clock=lambda: NOW,
        )
    assert raised.value.code == "native_im_sandbox_approval_trust_anchor_mismatch"
    assert approval.canonical_digest() not in str(raised.value)


@pytest.mark.parametrize(
    "configuration_change",
    (
        {"approval_digest": "f" * 64},
        {"authority_revision": 8},
        {"deployment_subject_digest": "9" * 64},
        {"tenant_id": "other-tenant"},
        {"page_limit": 99},
    ),
)
def test_activation_rejects_self_reported_config_drift(
    configuration_change: dict[str, object],
) -> None:
    authority, configuration, provider_profile, _, _ = authority_inputs()
    changed = replace(configuration, **configuration_change)

    with pytest.raises(NativeIMSandboxApprovalAuthorityError) as raised:
        authority.activate(changed, provider_profile)
    assert raised.value.code == "native_im_sandbox_approval_binding_mismatch"


def test_activation_rejects_profile_drift_before_issuing_a_permit() -> None:
    authority, configuration, provider_profile, _, _ = authority_inputs()
    changed_profile = replace(provider_profile, revision="other-revision")

    with pytest.raises(NativeIMSandboxApprovalAuthorityError) as raised:
        authority.activate(configuration, changed_profile)
    assert raised.value.code == "native_im_sandbox_approval_binding_mismatch"


@pytest.mark.parametrize(
    ("now", "expires_at", "approval_changes", "expected_code"),
    (
        (
            "2026-08-28T08:04:59.999999Z",
            EXPIRES,
            None,
            "native_im_sandbox_approval_not_yet_valid",
        ),
        (
            EXPIRES,
            EXPIRES,
            None,
            "native_im_sandbox_approval_expired",
        ),
        (
            NOW,
            "2026-09-28T00:00:00.000001Z",
            None,
            "native_im_sandbox_approval_ttl_exceeded",
        ),
        (
            "2026-08-28T07:59:59.999999Z",
            EXPIRES,
            None,
            "native_im_sandbox_approval_not_yet_valid",
        ),
    ),
)
def test_activation_enforces_not_before_expiry_issued_time_and_maximum_ttl(
    now: str,
    expires_at: str,
    approval_changes: dict[str, object] | None,
    expected_code: str,
) -> None:
    authority, configuration, provider_profile, _, _ = authority_inputs(
        now=now,
        expires_at=expires_at,
        approval_changes=approval_changes,
    )
    with pytest.raises(NativeIMSandboxApprovalAuthorityError) as raised:
        authority.activate(configuration, provider_profile)
    assert raised.value.code == expected_code


def test_action_time_recheck_rejects_expiry_after_successful_activation() -> None:
    authority, permit, _, _, _, clock = active_inputs()
    clock.value = EXPIRES

    with pytest.raises(NativeIMSandboxApprovalAuthorityError) as raised:
        authority.require_current(permit, operation="read")
    assert raised.value.code == "native_im_sandbox_approval_expired"


@pytest.mark.parametrize("invalid", (None, 7, "2026-08-28T12:00:00Z"))
def test_clock_values_fail_closed_without_leaking_the_value(invalid: object) -> None:
    authority, configuration, provider_profile, _, _ = authority_inputs(now=invalid)
    with pytest.raises(NativeIMSandboxApprovalAuthorityError) as raised:
        authority.activate(configuration, provider_profile)
    assert raised.value.code == "native_im_sandbox_approval_clock_invalid"
    assert str(invalid) not in str(raised.value)


def test_clock_exception_is_detached_and_redacted() -> None:
    canary = "authority-clock-secret-canary"
    authority, configuration, provider_profile, _, _ = authority_inputs(now=RuntimeError(canary))
    with pytest.raises(NativeIMSandboxApprovalAuthorityError) as raised:
        authority.activate(configuration, provider_profile)
    assert raised.value.code == "native_im_sandbox_approval_clock_invalid"
    assert raised.value.__context__ is None
    assert canary not in f"{raised.value!r} {raised.value}"


def test_revocation_is_one_way_and_invalidates_existing_and_future_permits() -> None:
    authority, permit, configuration, provider_profile, _, _ = active_inputs()

    assert authority.revoke() is True
    assert authority.revoke() is False
    assert authority.revoked is True
    assert authority.generation == 1

    with pytest.raises(NativeIMSandboxApprovalAuthorityError) as current:
        authority.require_current(permit, operation="read")
    assert current.value.code == "native_im_sandbox_approval_revoked"
    with pytest.raises(NativeIMSandboxApprovalAuthorityError) as future:
        authority.activate(configuration, provider_profile)
    assert future.value.code == "native_im_sandbox_approval_revoked"


def test_unknown_operation_is_rejected_without_granting_a_wider_surface() -> None:
    authority, permit, _, _, _, _ = active_inputs()
    for operation in ("dispatch", "query_acceptance", "send_message", ""):
        with pytest.raises(NativeIMSandboxApprovalAuthorityError) as raised:
            authority.require_current(permit, operation=operation)
        assert raised.value.code == "native_im_sandbox_approval_operation_forbidden"


def test_permit_and_authority_are_non_copyable_and_non_serializable() -> None:
    authority, permit, _, _, _, _ = active_inputs()
    for value in (permit, authority):
        with pytest.raises(TypeError):
            copy.copy(value)
        with pytest.raises(TypeError):
            copy.deepcopy(value)
        with pytest.raises(TypeError):
            pickle.dumps(value)


def test_permit_from_another_authority_is_rejected() -> None:
    first, permit, _, _, _, _ = active_inputs()
    second, _, _, _, _, _ = active_inputs()

    with pytest.raises(NativeIMSandboxApprovalAuthorityError) as raised:
        second.require_current(permit, operation="read")
    assert raised.value.code == "native_im_sandbox_approval_permit_invalid"
    first.require_current(permit, operation="read")


def test_admission_guard_linearizes_revocation_after_the_guarded_section() -> None:
    authority, permit, _, _, _, _ = active_inputs()
    started = threading.Event()
    completed = threading.Event()

    def revoke() -> None:
        started.set()
        authority.revoke()
        completed.set()

    with authority.admission_guard(permit) as approval:
        thread = threading.Thread(target=revoke)
        thread.start()
        assert started.wait(timeout=2)
        assert completed.wait(timeout=0.05) is False
        assert type(approval) is NativeIMSandboxApprovalV1
    thread.join(timeout=2)

    assert completed.is_set()
    assert authority.revoked is True


def test_external_durable_revocation_invalidates_live_permit_after_restart_boundary(
    tmp_path,
) -> None:
    path = str((tmp_path / "approval-authority.sqlite3").resolve())
    local_store = SQLiteNativeIMSandboxApprovalHighWaterV1(path)
    external_store = SQLiteNativeIMSandboxApprovalHighWaterV1(path)
    authority, permit, _, _, approval, _ = active_inputs(high_water=local_store)
    revoked = NativeIMSandboxApprovalAuthorityStateV1(
        schema_version=1,
        approval_id=approval.approval_id,
        approval_digest=approval.canonical_digest(),
        authority_revision=approval.authority_revision + 1,
        state="revoked",
        observed_at="2026-08-28T12:00:01.000001Z",
    )
    try:
        external_store.observe(revoked)
        with pytest.raises(NativeIMSandboxApprovalAuthorityError) as raised:
            authority.require_current(permit, operation="read")
        assert raised.value.code == "native_im_sandbox_approval_revoked"
        assert authority.revoked is True
    finally:
        local_store.close()
        external_store.close()


def test_durable_backend_failure_invalidates_action_time_check(tmp_path) -> None:
    path = str((tmp_path / "closed-authority.sqlite3").resolve())
    store = SQLiteNativeIMSandboxApprovalHighWaterV1(path)
    authority, permit, _, _, _, _ = active_inputs(high_water=store)
    store.close()

    with pytest.raises(NativeIMSandboxApprovalAuthorityError) as raised:
        authority.require_current(permit, operation="read")
    assert raised.value.code == "native_im_sandbox_approval_durable_state_invalid"


def test_authority_admission_guard_blocks_external_durable_revocation(tmp_path) -> None:
    path = str((tmp_path / "guarded-authority.sqlite3").resolve())
    local_store = SQLiteNativeIMSandboxApprovalHighWaterV1(path)
    external_store = SQLiteNativeIMSandboxApprovalHighWaterV1(path)
    authority, permit, _, _, approval, _ = active_inputs(high_water=local_store)
    revoked = NativeIMSandboxApprovalAuthorityStateV1(
        schema_version=1,
        approval_id=approval.approval_id,
        approval_digest=approval.canonical_digest(),
        authority_revision=approval.authority_revision + 1,
        state="revoked",
        observed_at="2026-08-28T12:00:01.000001Z",
    )
    started = threading.Event()
    completed = threading.Event()

    def revoke() -> None:
        started.set()
        external_store.observe(revoked)
        completed.set()

    try:
        with authority.admission_guard(permit):
            thread = threading.Thread(target=revoke)
            thread.start()
            assert started.wait(timeout=2)
            assert completed.wait(timeout=0.05) is False
        thread.join(timeout=2)
        assert completed.is_set()
    finally:
        local_store.close()
        external_store.close()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_actual_fork_rejects_inherited_authority_and_permit_before_lock_access() -> None:
    authority, permit, _, _, _, _ = active_inputs()
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - exercised in child process
        os.close(read_fd)
        try:
            authority.require_current(permit, operation="read")
        except NativeIMSandboxApprovalAuthorityError as error:
            payload = error.code.encode()
        except BaseException:
            payload = b"unexpected"
        else:
            payload = b"allowed"
        os.write(write_fd, payload)
        os.close(write_fd)
        os._exit(0)

    os.close(write_fd)
    try:
        payload = os.read(read_fd, 256)
    finally:
        os.close(read_fd)
        _, status = os.waitpid(pid, 0)
    assert os.WIFEXITED(status)
    assert payload == b"native_im_sandbox_approval_process_mismatch"
    authority.require_current(permit, operation="read")


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
def test_fork_while_another_thread_holds_authority_lock_fails_fast_in_child() -> None:
    authority, permit, _, _, _, _ = active_inputs()
    entered = threading.Event()
    release = threading.Event()

    def hold_guard() -> None:
        with authority.admission_guard(permit):
            entered.set()
            release.wait(timeout=5)

    holder = threading.Thread(target=hold_guard)
    holder.start()
    assert entered.wait(timeout=2)
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - exercised in child process
        os.close(read_fd)
        try:
            authority.require_current(permit, operation="read")
        except NativeIMSandboxApprovalAuthorityError as error:
            payload = error.code.encode()
        except BaseException:
            payload = b"unexpected"
        else:
            payload = b"allowed"
        os.write(write_fd, payload)
        os.close(write_fd)
        os._exit(0)

    os.close(write_fd)
    try:
        payload = os.read(read_fd, 256)
    finally:
        os.close(read_fd)
        _, status = os.waitpid(pid, 0)
        release.set()
        holder.join(timeout=2)
    assert os.WIFEXITED(status)
    assert payload == b"native_im_sandbox_approval_process_mismatch"
    assert holder.is_alive() is False


def test_same_pid_epoch_rotation_invalidates_authority_and_permit() -> None:
    authority, permit, _, _, _, _ = active_inputs()
    original = process_identity._PROCESS_IDENTITY
    try:
        process_identity._PROCESS_IDENTITY = (
            original[0],
            process_identity._new_epoch(),
        )
        with pytest.raises(NativeIMSandboxApprovalAuthorityError) as raised:
            authority.require_current(permit, operation="read")
        assert raised.value.code == "native_im_sandbox_approval_process_mismatch"
    finally:
        process_identity._PROCESS_IDENTITY = original
    authority.require_current(permit, operation="read")


def test_hostile_subclasses_are_rejected_before_field_access() -> None:
    authority, permit, configuration, provider_profile, _, _ = active_inputs()

    class PermitSubclass(NativeIMSandboxApprovalPermitV1):
        pass

    class ConfigSubclass(type(configuration)):
        pass

    class ProfileSubclass(type(provider_profile)):
        pass

    with pytest.raises(TypeError):
        authority.require_current(object.__new__(PermitSubclass), operation="read")
    with pytest.raises(TypeError):
        authority.activate(object.__new__(ConfigSubclass), provider_profile)
    with pytest.raises(TypeError):
        authority.activate(configuration, object.__new__(ProfileSubclass))


def test_repr_redacts_record_scope_people_endpoint_and_digest_values() -> None:
    authority, permit, _, _, approval, _ = active_inputs()
    rendered = f"{authority!r} {permit!r}"
    for hidden in (
        approval.approval_id,
        approval.provider,
        approval.tenant_id,
        approval.origin,
        approval.issuer_id,
        approval.canonical_digest(),
    ):
        assert hidden not in rendered
