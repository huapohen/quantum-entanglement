#!/usr/bin/env python3
"""Read-only fresh-process verifier for the synthetic native-IM provider bundle."""

from __future__ import annotations

import asyncio
import hashlib
import sys
import tempfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
for path in (SOURCE_ROOT, REPOSITORY_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from tests.test_native_im_provider_bundle_tck import (  # noqa: E402
    test_provider_bundle_tck_closes_verified_exchange_to_durable_admission,
)

EXPECTED_SUITE_DIGEST = "7fbdec73b0bbe74e18e721c39ae623548f5dfe6dfa97bbf14c4a716bdc50d4e7"
_SUITE_FILES = (
    "src/quantum_entanglement/native_im_read_exchange.py",
    "src/quantum_entanglement/native_im_sandbox_provenance.py",
    "tests/native_im_synthetic_provider_mapper.py",
    "tests/native_im_synthetic_provider_transport.py",
    "tests/test_native_im_provider_bundle_tck.py",
)


def _suite_digest() -> str:
    digest = hashlib.sha256()
    digest.update(b"quantum-entanglement.native-im/ProviderBundleTCKSuiteV1/1\n")
    for relative_path in _SUITE_FILES:
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update((REPOSITORY_ROOT / relative_path).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


async def verify() -> None:
    if _suite_digest() != EXPECTED_SUITE_DIGEST:
        raise ValueError("native IM provider bundle TCK suite differs")
    with tempfile.TemporaryDirectory(prefix="qe-native-im-provider-bundle-") as directory:
        await test_provider_bundle_tck_closes_verified_exchange_to_durable_admission(
            Path(directory)
        )


def main() -> int:
    try:
        asyncio.run(verify())
    except (AssertionError, OSError, RuntimeError, TypeError, ValueError):
        print("native IM provider bundle TCK verification failed", file=sys.stderr)
        return 1
    print(f"native IM provider bundle TCK verified: {EXPECTED_SUITE_DIGEST}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 1:
        print("usage: verify_native_im_provider_bundle_tck_v1.py", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main())
