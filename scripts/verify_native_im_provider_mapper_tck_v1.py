#!/usr/bin/env python3
"""Read-only fresh-process verifier for the synthetic provider Mapper TCK candidate."""

from __future__ import annotations

import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
for path in (SOURCE_ROOT, REPOSITORY_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from tests.native_im_mapper_tck import assert_native_im_mapper_tck_v1  # noqa: E402
from tests.native_im_synthetic_provider_mapper import (  # noqa: E402
    MAPPER_CONTRACT_DIGEST,
    MAPPER_CONTRACT_ID,
    SyntheticSemanticProviderMapperV1,
)
from tests.test_native_im_synthetic_provider_mapper import _vectors  # noqa: E402

EXPECTED_SUITE_DIGEST = "e569232b71e0989d4577604e0452b4ccb058c6b80ca981eac8334da6b34f5d51"


def verify() -> None:
    accepted, rejected = _vectors()
    report = assert_native_im_mapper_tck_v1(
        SyntheticSemanticProviderMapperV1,
        SyntheticSemanticProviderMapperV1,
        mapper_contract_id=MAPPER_CONTRACT_ID,
        mapper_contract_digest=MAPPER_CONTRACT_DIGEST,
        accepted=accepted,
        rejected=rejected,
    )
    if (
        report.accepted_vector_ids
        != (
            "test-provider.accepted.initial",
            "test-provider.accepted.replay",
            "test-provider.accepted.empty-continuation",
        )
        or report.rejected_vector_ids
        != (
            "test-provider.rejected.duplicate-key",
            "test-provider.rejected.unsupported-event",
            "test-provider.rejected.scope",
            "test-provider.rejected.request-correlation",
            "test-provider.rejected.conversation",
            "test-provider.rejected.limit",
        )
        or report.suite_digest != EXPECTED_SUITE_DIGEST
    ):
        raise ValueError("native IM provider mapper TCK report differs")


def main() -> int:
    try:
        verify()
    except (AssertionError, OSError, TypeError, ValueError):
        print("native IM provider mapper TCK verification failed", file=sys.stderr)
        return 1
    print(
        f"native IM provider mapper TCK verified: 3 accepted, 6 rejected, {EXPECTED_SUITE_DIGEST}"
    )
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 1:
        print("usage: verify_native_im_provider_mapper_tck_v1.py", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main())
