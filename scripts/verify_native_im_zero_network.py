#!/usr/bin/env python3
"""Verify native-IM fake and sandbox imports/runtime without network capability."""

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

SOURCE_IMPORT_ALLOWLISTS = {
    SOURCE_ROOT / "quantum_entanglement" / "native_im_fake.py": {
        "__future__",
        "dataclasses",
        "hashlib",
        "os",
        "typing",
    },
    SOURCE_ROOT / "quantum_entanglement" / "native_im_sandbox.py": {
        "__future__",
        "asyncio",
        "collections",
        "contextlib",
        "dataclasses",
        "hashlib",
        "ipaddress",
        "os",
        "typing",
    },
    SOURCE_ROOT / "quantum_entanglement" / "native_im_sandbox_approval.py": {
        "__future__",
        "dataclasses",
        "typing",
    },
    SOURCE_ROOT / "quantum_entanglement" / "native_im_sandbox_approval_store.py": {
        "__future__",
        "collections",
        "contextlib",
        "dataclasses",
        "os",
        "sqlite3",
        "threading",
        "typing",
    },
    SOURCE_ROOT / "quantum_entanglement" / "native_im_sandbox_authority.py": {
        "__future__",
        "collections",
        "contextlib",
        "dataclasses",
        "datetime",
        "threading",
        "typing",
    },
    SOURCE_ROOT / "quantum_entanglement" / "native_im_sandbox_composition.py": {
        "__future__",
        "dataclasses",
        "typing",
    },
    SOURCE_ROOT / "quantum_entanglement" / "native_im_sandbox_lifecycle.py": {
        "__future__",
        "asyncio",
        "collections",
        "contextlib",
        "dataclasses",
        "os",
        "threading",
        "typing",
    },
    SOURCE_ROOT / "quantum_entanglement" / "native_im_sandbox_observability.py": {
        "__future__",
        "dataclasses",
        "logging",
        "os",
        "threading",
        "typing",
    },
    SOURCE_ROOT / "quantum_entanglement" / "native_im_sandbox_provenance.py": {
        "__future__",
        "dataclasses",
        "typing",
    },
}
FORBIDDEN_NETWORK_IMPORTS = {
    "aiohttp",
    "ftplib",
    "grpc",
    "http.client",
    "httpx",
    "requests",
    "smtplib",
    "urllib.request",
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
        if any(
            fullname == forbidden or fullname.startswith(forbidden + ".")
            for forbidden in FORBIDDEN_NETWORK_IMPORTS
        ):
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
    import logging

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
    from quantum_entanglement.native_im_sandbox import (
        NativeIMSandboxDisabledError,
        compose_default_native_im_sandbox_v1,
    )
    from quantum_entanglement.native_im_sandbox_lifecycle import (
        NativeIMSandboxLifecycleV1,
    )
    from quantum_entanglement.native_im_sandbox_observability import (
        NativeIMSandboxMetricsV1,
        NativeIMSandboxObserverV1,
        native_im_sandbox_log_catalog_v1,
    )
    from quantum_entanglement.native_im_sandbox_provenance import (
        NativeIMSandboxAdmissionProvenanceV1,
    )
    from quantum_entanglement.service.logging import SafeLogger
    from quantum_entanglement.service.native_im_config import NativeIMDisabledConfigV1

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

    disabled = compose_default_native_im_sandbox_v1(
        NativeIMDisabledConfigV1(schema_version=1, enabled=False)
    )
    try:
        await disabled.read_inbound(read_request)
    except NativeIMSandboxDisabledError:
        pass
    else:
        raise AssertionError("default sandbox unexpectedly enabled inbound transport")
    await disabled.aclose()
    if not disabled.closed:
        raise AssertionError("disabled sandbox did not close")

    logger = logging.Logger("native-im-zero-network-gate")
    logger.addHandler(logging.NullHandler())
    metrics = NativeIMSandboxMetricsV1()
    observer = NativeIMSandboxObserverV1(
        SafeLogger(logger, native_im_sandbox_log_catalog_v1()),
        metrics,
    )
    observer.lifecycle("stopped", ready=False, kill_switch_tripped=False)
    if metrics.snapshot().events_admitted_count != 0:
        raise AssertionError("observer created an inbound observation")
    if not isinstance(NativeIMSandboxLifecycleV1, type):
        raise AssertionError("sandbox lifecycle import did not produce a type")
    if not isinstance(NativeIMSandboxAdmissionProvenanceV1, type):
        raise AssertionError("sandbox provenance import did not produce a type")

    sandbox_rendered = "\n".join((repr(disabled), repr(observer), repr(metrics)))
    if CREDENTIAL_CANARY in sandbox_rendered:
        raise AssertionError("credential canary escaped native IM sandbox runtime")


def main() -> int:
    for source_path, allowed_imports in SOURCE_IMPORT_ALLOWLISTS.items():
        source = source_path.read_text(encoding="utf-8")
        if _direct_imports(source) != allowed_imports:
            print(
                f"native IM direct import allowlist drift: {source_path.name}",
                file=sys.stderr,
            )
            return 1
        if source_path.name == "native_im_fake.py":
            lowered = source.lower()
            if any(word in lowered for word in FORBIDDEN_CONFIGURATION_WORDS):
                print("native IM fake contains forbidden network configuration", file=sys.stderr)
                return 1
        if "os.environ" in source or "os.getenv" in source:
            print(
                f"native IM source contains environment access: {source_path.name}",
                file=sys.stderr,
            )
            return 1

    for name in CREDENTIAL_ENVIRONMENT_NAMES:
        os.environ[name] = CREDENTIAL_CANARY
    loop = asyncio.new_event_loop()
    try:
        _replace_attribute(socket, "socket", _deny_network)
        _replace_attribute(socket, "create_connection", _deny_network)
        _replace_attribute(socket, "getaddrinfo", _deny_network)
        _replace_attribute(socket, "gethostbyname", _deny_network)
        _replace_attribute(asyncio, "open_connection", _deny_network)
        _replace_attribute(asyncio, "start_server", _deny_network)
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
