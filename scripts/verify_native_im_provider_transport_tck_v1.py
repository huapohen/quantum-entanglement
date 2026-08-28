#!/usr/bin/env python3
"""Read-only fresh-process verifier for the scripted provider transport TCK."""

from __future__ import annotations

import asyncio
import hashlib
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
for path in (SOURCE_ROOT, REPOSITORY_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from tests.test_native_im_synthetic_provider_transport import (  # noqa: E402
    test_scripted_exchange_exposes_no_endpoint_or_outbound_surface,
    test_scripted_exchange_step_requires_one_exact_outcome,
    test_transport_intent_changes_with_every_approved_configuration_axis,
    test_transport_tck_close_is_idempotent_and_blocks_new_exchange,
    test_transport_tck_continuation_intent_binds_cursor_sequence_snapshot_and_request,
    test_transport_tck_enhanced_read_binds_transient_exchange_evidence,
    test_transport_tck_health_is_exact_bound_and_zero_effect,
    test_transport_tck_initial_read_builds_exact_intent_without_reading_credential,
    test_transport_tck_redacts_scripted_exchange_faults,
    test_transport_tck_rejects_cross_request_signed_response,
    test_transport_tck_rejects_every_non_200_read_status,
    test_transport_tck_rejects_missing_or_extra_signed_headers,
    test_transport_tck_repeat_event_source_does_not_reuse_exchange_evidence,
)

EXPECTED_SUITE_DIGEST = "173a05e443a1506a41a23cf17ca834c08b667a0d28e46fd1d186b64cd106c1d4"
_SUITE_FILES = (
    "tests/native_im_synthetic_provider_transport.py",
    "tests/test_native_im_synthetic_provider_transport.py",
)
_FAULTS = ("disconnect", "timeout")
_REJECTED_STATUSES = (204, 206, 301, 401, 403, 404, 429, 500)


def _suite_digest() -> str:
    digest = hashlib.sha256()
    digest.update(b"quantum-entanglement.native-im/ProviderTransportTCKSuiteV1/1\n")
    for relative_path in _SUITE_FILES:
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update((REPOSITORY_ROOT / relative_path).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


async def verify() -> None:
    if _suite_digest() != EXPECTED_SUITE_DIGEST:
        raise ValueError("native IM provider transport TCK suite differs")

    await test_transport_tck_health_is_exact_bound_and_zero_effect()
    await test_transport_tck_initial_read_builds_exact_intent_without_reading_credential()
    await test_transport_tck_enhanced_read_binds_transient_exchange_evidence()
    await test_transport_tck_repeat_event_source_does_not_reuse_exchange_evidence()
    test_transport_tck_continuation_intent_binds_cursor_sequence_snapshot_and_request()
    for fault in _FAULTS:
        await test_transport_tck_redacts_scripted_exchange_faults(fault)
    for status in _REJECTED_STATUSES:
        await test_transport_tck_rejects_every_non_200_read_status(status)
    await test_transport_tck_rejects_cross_request_signed_response()
    await test_transport_tck_rejects_missing_or_extra_signed_headers()
    await test_transport_tck_close_is_idempotent_and_blocks_new_exchange()
    test_scripted_exchange_exposes_no_endpoint_or_outbound_surface()
    test_scripted_exchange_step_requires_one_exact_outcome()
    test_transport_intent_changes_with_every_approved_configuration_axis()


def main() -> int:
    try:
        asyncio.run(verify())
    except (AssertionError, OSError, RuntimeError, TypeError, ValueError):
        print("native IM provider transport TCK verification failed", file=sys.stderr)
        return 1
    print(
        "native IM provider transport TCK verified: "
        f"5 accepted, 12 rejected, {EXPECTED_SUITE_DIGEST}"
    )
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 1:
        print("usage: verify_native_im_provider_transport_tck_v1.py", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main())
