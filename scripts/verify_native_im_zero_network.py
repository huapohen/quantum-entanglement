#!/usr/bin/env python3
"""Verify that the native-IM P0 fake imports and runs without network capability."""

from __future__ import annotations

import ast
import asyncio
import importlib.abc
import os
import socket
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
FIXTURE_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "native_im" / "v1"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

FAKE_SOURCE = SOURCE_ROOT / "quantum_entanglement" / "native_im_fake.py"
ALLOWED_DIRECT_IMPORTS = {
    "__future__",
    "dataclasses",
    "hashlib",
    "os",
    "typing",
}
FORBIDDEN_NETWORK_IMPORTS = {
    "aiohttp",
    "http",
    "httpx",
    "requests",
    "websocket",
    "websockets",
}
FORBIDDEN_CONFIGURATION_WORDS = {
    "authorization",
    "callback",
    "endpoint",
    "http",
    "webhook",
    "websocket",
}
CREDENTIAL_ENVIRONMENT_NAMES = (
    "DEEPSEEK_API_KEY",
    "OPENAI_API_KEY",
    "FEISHU_APP_SECRET",
    "WECOM_CORP_SECRET",
)
CREDENTIAL_CANARY = "test-native-im-credential-canary-do-not-read"


class _NetworkImportBlocker(importlib.abc.MetaPathFinder):
    def find_spec(
        self,
        fullname: str,
        path: object = None,
        target: object = None,
    ) -> None:
        if fullname.split(".", 1)[0] in FORBIDDEN_NETWORK_IMPORTS:
            raise ImportError(f"network import blocked by native IM P0 gate: {fullname}")
        return None


def _direct_imports(source: str) -> set[str]:
    parsed = ast.parse(source)
    imported = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(parsed)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        (node.module or "").split(".", 1)[0]
        for node in ast.walk(parsed)
        if isinstance(node, ast.ImportFrom) and node.level == 0
    )
    return imported


def _load_fixture(model: type[Any], filename: str) -> Any:
    return model.from_json_bytes((FIXTURE_ROOT / filename).read_bytes())


def _deny_network(*args: object, **kwargs: object) -> None:
    raise AssertionError("native IM fake attempted to open network capability")


def _deny_environment(name: str, default: object = None) -> str | None:
    if name in CREDENTIAL_ENVIRONMENT_NAMES:
        raise AssertionError("native IM fake attempted to read credential environment")
    return default if type(default) is str else None


def _replace_attribute(owner: object, name: str, value: object) -> None:
    setattr(owner, name, value)


async def _exercise() -> None:
    from quantum_entanglement.native_im import (
        IMAcceptanceQueryV1,
        IMCapabilityRequestV1,
        IMCapabilitySnapshotV1,
        IMDispatchRequestV1,
        IMInboundReadRequestV1,
        IMVerifiedInboundEnvelopeV1,
    )
    from quantum_entanglement.native_im_fake import (
        FakeIMAdapter,
        FakeIMOutboundDisabledError,
        FakeIMTestOutboundPermit,
    )
    from quantum_entanglement.native_im_gateway import (
        validate_im_acceptance_result_v1,
        validate_im_dispatch_result_v1,
        validate_im_inbound_result_v1,
    )

    capability = _load_fixture(IMCapabilitySnapshotV1, "capability_snapshot.json")
    envelope = _load_fixture(IMVerifiedInboundEnvelopeV1, "verified_inbound_envelope.json")
    capability_request = _load_fixture(IMCapabilityRequestV1, "capability_request.json")
    read_request = _load_fixture(IMInboundReadRequestV1, "inbound_read_request.json")
    dispatch_request = _load_fixture(IMDispatchRequestV1, "dispatch_request.json")
    acceptance_query = _load_fixture(IMAcceptanceQueryV1, "acceptance_query.json")
    acceptance_query = replace(
        acceptance_query,
        lookup_mode="idempotency_key",
        provider_operation_id=None,
    )

    default = FakeIMAdapter(
        tenant_id="test-tenant",
        workspace_id="test-workspace",
        channel_id="test-channel",
        capability=capability,
        envelopes=(envelope,),
    )
    assert await default.capability_snapshot(capability_request) is capability
    page = await default.read_inbound(read_request)
    validate_im_inbound_result_v1(read_request, capability, page)
    try:
        await default.dispatch(dispatch_request)
    except FakeIMOutboundDisabledError:
        pass
    else:
        raise AssertionError("ordinary fake unexpectedly enabled outbound")

    outbound = FakeIMAdapter.for_test(
        tenant_id="test-tenant",
        workspace_id="test-workspace",
        channel_id="test-channel",
        capability=capability,
        envelopes=(envelope,),
        outbound_permit=FakeIMTestOutboundPermit(),
    )
    dispatch_result = await outbound.dispatch(dispatch_request)
    validate_im_dispatch_result_v1(dispatch_request, dispatch_result)
    query_result = await outbound.query_acceptance(acceptance_query)
    validate_im_acceptance_result_v1(
        acceptance_query,
        dispatch_request,
        capability,
        query_result,
    )
    rendered = "\n".join(
        (
            repr(default),
            repr(outbound),
            repr(dispatch_result),
            repr(query_result),
        )
    )
    if CREDENTIAL_CANARY in rendered:
        raise AssertionError("credential canary escaped native IM fake runtime")


def main() -> int:
    source = FAKE_SOURCE.read_text(encoding="utf-8")
    if _direct_imports(source) != ALLOWED_DIRECT_IMPORTS:
        print("native IM fake direct import allowlist drift", file=sys.stderr)
        return 1
    lowered = source.lower()
    if any(word in lowered for word in FORBIDDEN_CONFIGURATION_WORDS):
        print("native IM fake contains forbidden network configuration", file=sys.stderr)
        return 1
    if "os.environ" in source or "os.getenv" in source:
        print("native IM fake contains environment credential access", file=sys.stderr)
        return 1

    for name in CREDENTIAL_ENVIRONMENT_NAMES:
        os.environ[name] = CREDENTIAL_CANARY
    loop = asyncio.new_event_loop()
    try:
        _replace_attribute(socket, "socket", _deny_network)
        _replace_attribute(socket, "create_connection", _deny_network)
        _replace_attribute(socket, "getaddrinfo", _deny_network)
        _replace_attribute(socket, "gethostbyname", _deny_network)
        _replace_attribute(os, "getenv", _deny_environment)
        sys.meta_path.insert(0, _NetworkImportBlocker())
        loop.run_until_complete(_exercise())
    except (AssertionError, ImportError, OSError, TypeError, ValueError) as failure:
        print(str(failure), file=sys.stderr)
        return 1
    finally:
        loop.close()
    print("native IM P0 zero-network gate verified")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 1:
        print("usage: verify_native_im_zero_network.py", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main())
