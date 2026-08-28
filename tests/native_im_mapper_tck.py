"""Reusable test-only compatibility kit for reviewed native-IM mapper candidates."""

from __future__ import annotations

import asyncio
import builtins
import hashlib
import multiprocessing
import os
import socket
import sqlite3
import subprocess
import threading
import webbrowser
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn
from unittest.mock import patch

from quantum_entanglement.native_im import (
    IMCapabilitySnapshotV1,
    IMInboundReadRequestV1,
)
from quantum_entanglement.native_im_auth import (
    NativeIMDetachedSignatureV1,
    NativeIMRawVerificationResultV1,
)
from quantum_entanglement.native_im_provider_profile import IMProviderProfileV1
from quantum_entanglement.native_im_sandbox import (
    NativeIMInboundMapperPort,
    NativeIMInboundRawResponseV1,
    NativeIMMappedPageV1,
    NativeIMMapperRejectionError,
    derive_native_im_mapping_evidence_digest_v1,
    parse_native_im_inbound_page_v1,
)
from quantum_entanglement.service.native_im_config import NativeIMInboundOnlyConfigV1


class NativeIMMapperTCKFailure(AssertionError):
    """A content-free mapper compatibility failure."""

    __slots__ = ("code", "vector_id")

    def __init__(self, code: str, vector_id: str) -> None:
        self.code = code
        self.vector_id = vector_id
        super().__init__(f"{code}:{vector_id}")


class _NativeIMMapperForbiddenEffect(AssertionError):
    pass


@dataclass(frozen=True, repr=False)
class MapperTCKContextV1:
    configuration: NativeIMInboundOnlyConfigV1 = field(repr=False)
    response: NativeIMInboundRawResponseV1 = field(repr=False)
    request: IMInboundReadRequestV1 = field(repr=False)
    capability: IMCapabilitySnapshotV1 = field(repr=False)
    raw_verification: NativeIMRawVerificationResultV1 = field(repr=False)
    profile: IMProviderProfileV1 = field(repr=False)

    def __post_init__(self) -> None:
        expected = (
            (self.configuration, NativeIMInboundOnlyConfigV1),
            (self.response, NativeIMInboundRawResponseV1),
            (self.request, IMInboundReadRequestV1),
            (self.capability, IMCapabilitySnapshotV1),
            (self.raw_verification, NativeIMRawVerificationResultV1),
            (self.profile, IMProviderProfileV1),
        )
        if type(self) is not MapperTCKContextV1 or any(
            type(value) is not model for value, model in expected
        ):
            raise TypeError("mapper TCK context requires exact V1 input classes")
        if self.response.read_request_id != self.request.read_request_id:
            raise ValueError("mapper TCK response is not bound to its request")
        if hashlib.sha256(self.response.raw_body).hexdigest() != self.raw_verification.body_digest:
            raise ValueError("mapper TCK verification does not bind the response body")


@dataclass(frozen=True, repr=False)
class MapperTCKAcceptedV1:
    vector_id: str
    context: MapperTCKContextV1 = field(repr=False)
    expected: NativeIMMappedPageV1 = field(repr=False)

    def __post_init__(self) -> None:
        if type(self) is not MapperTCKAcceptedV1:
            raise TypeError("accepted mapper TCK vector requires the exact V1 class")
        _vector_id(self.vector_id)
        if type(self.context) is not MapperTCKContextV1:
            raise TypeError("accepted mapper TCK vector requires an exact context")
        if type(self.expected) is not NativeIMMappedPageV1:
            raise TypeError("accepted mapper TCK vector requires an exact mapped page")


@dataclass(frozen=True, repr=False)
class MapperTCKRejectedV1:
    vector_id: str
    context: MapperTCKContextV1 = field(repr=False)
    expected_error_code: str

    def __post_init__(self) -> None:
        if type(self) is not MapperTCKRejectedV1:
            raise TypeError("rejected mapper TCK vector requires the exact V1 class")
        _vector_id(self.vector_id)
        if type(self.context) is not MapperTCKContextV1:
            raise TypeError("rejected mapper TCK vector requires an exact context")
        NativeIMMapperRejectionError(self.expected_error_code)


@dataclass(frozen=True, repr=False)
class MapperTCKReportV1:
    mapper_contract_id: str
    mapper_contract_digest: str = field(repr=False)
    accepted_vector_ids: tuple[str, ...]
    rejected_vector_ids: tuple[str, ...]
    suite_digest: str = field(repr=False)

    def __repr__(self) -> str:
        return (
            "MapperTCKReportV1("
            f"accepted={len(self.accepted_vector_ids)}, "
            f"rejected={len(self.rejected_vector_ids)}, "
            f"digest={self.suite_digest[:12]!r})"
        )


def _vector_id(value: object) -> str:
    if type(value) is not str or not value or len(value.encode("utf-8")) > 128:
        raise ValueError("mapper TCK vector ID is invalid")
    if any(character.isspace() or ord(character) < 0x21 for character in value):
        raise ValueError("mapper TCK vector ID is invalid")
    return value


def _fail(code: str, vector_id: str) -> NoReturn:
    raise NativeIMMapperTCKFailure(code, vector_id) from None


def _metadata_snapshot(value: NativeIMDetachedSignatureV1) -> tuple[object, ...]:
    return (
        value.schema_version,
        value.timestamp,
        value.nonce,
        value.key_id,
        value.signature,
    )


def _verification_snapshot(value: NativeIMRawVerificationResultV1) -> tuple[object, ...]:
    return (
        value.schema_version,
        value.verifier_id,
        value.key_id,
        value.signed_at,
        value.expires_at,
        value.verified_at,
        value.body_digest,
        value.nonce_digest,
        value.authentication_evidence_digest,
    )


def _context_snapshot(value: MapperTCKContextV1) -> tuple[object, ...]:
    response = value.response
    return (
        value.configuration.approval_binding_digest,
        response.schema_version,
        response.read_request_id,
        response.status_code,
        _metadata_snapshot(response.metadata),
        response.raw_body,
        response.received_at,
        response.transport_evidence_digest,
        value.request.canonical_bytes(),
        value.capability.canonical_bytes(),
        _verification_snapshot(value.raw_verification),
        value.profile.canonical_bytes(),
    )


def _clone_context(value: MapperTCKContextV1) -> MapperTCKContextV1:
    metadata = value.response.metadata
    response = NativeIMInboundRawResponseV1(
        schema_version=value.response.schema_version,
        read_request_id=value.response.read_request_id,
        status_code=value.response.status_code,
        metadata=NativeIMDetachedSignatureV1(
            schema_version=metadata.schema_version,
            timestamp=metadata.timestamp,
            nonce=metadata.nonce,
            key_id=metadata.key_id,
            signature=metadata.signature,
        ),
        raw_body=bytes(value.response.raw_body),
        received_at=value.response.received_at,
        transport_evidence_digest=value.response.transport_evidence_digest,
    )
    evidence = value.raw_verification
    return MapperTCKContextV1(
        configuration=value.configuration,
        response=response,
        request=IMInboundReadRequestV1.from_json_bytes(value.request.canonical_bytes()),
        capability=IMCapabilitySnapshotV1.from_json_bytes(value.capability.canonical_bytes()),
        raw_verification=NativeIMRawVerificationResultV1(
            schema_version=evidence.schema_version,
            verifier_id=evidence.verifier_id,
            key_id=evidence.key_id,
            signed_at=evidence.signed_at,
            expires_at=evidence.expires_at,
            verified_at=evidence.verified_at,
            body_digest=evidence.body_digest,
            nonce_digest=evidence.nonce_digest,
            authentication_evidence_digest=evidence.authentication_evidence_digest,
        ),
        profile=IMProviderProfileV1.from_json_bytes(value.profile.canonical_bytes()),
    )


def _blocked_effect(*args: object, **kwargs: object) -> NoReturn:
    raise _NativeIMMapperForbiddenEffect("native_im_mapper_tck_effect_forbidden")


@contextmanager
def native_im_mapper_zero_effect_fence_v1() -> Iterator[None]:
    """Block common ambient I/O/effect APIs while a synchronous mapper executes."""

    environment_type = type(os.environ)
    with ExitStack() as stack:
        for target, attribute in (
            (socket, "socket"),
            (socket, "create_connection"),
            (socket, "getaddrinfo"),
            (socket, "gethostbyname"),
            (subprocess, "Popen"),
            (subprocess, "run"),
            (asyncio, "create_task"),
            (asyncio, "create_subprocess_exec"),
            (asyncio, "create_subprocess_shell"),
            (webbrowser, "open"),
            (sqlite3, "connect"),
            (builtins, "open"),
            (Path, "open"),
            (Path, "read_bytes"),
            (Path, "read_text"),
            (Path, "write_bytes"),
            (Path, "write_text"),
            (os, "getenv"),
            (environment_type, "__getitem__"),
            (environment_type, "get"),
            (threading.Thread, "start"),
            (multiprocessing.Process, "start"),
        ):
            stack.enter_context(patch.object(target, attribute, side_effect=_blocked_effect))
        yield


def _invoke(
    mapper: NativeIMInboundMapperPort,
    context: MapperTCKContextV1,
    *,
    vector_id: str,
) -> NativeIMMappedPageV1:
    cloned = _clone_context(context)
    before = _context_snapshot(cloned)
    result: object = None
    failed = False
    effect_failed = False
    try:
        with native_im_mapper_zero_effect_fence_v1():
            result = mapper.map_inbound(
                cloned.response,
                cloned.request,
                cloned.capability,
                cloned.raw_verification,
                cloned.profile,
            )
    except _NativeIMMapperForbiddenEffect as error:
        effect_failed = True
        error.__traceback__ = None
        error.__cause__ = None
        error.__context__ = None
    except Exception as error:
        failed = True
        error.__traceback__ = None
        error.__cause__ = None
        error.__context__ = None
    if _context_snapshot(cloned) != before:
        _fail("native_im_mapper_tck_input_mutated", vector_id)
    if effect_failed:
        _fail("native_im_mapper_tck_effect_forbidden", vector_id)
    if failed:
        _fail("native_im_mapper_tck_accepted_rejected", vector_id)
    if type(result) is not NativeIMMappedPageV1:
        _fail("native_im_mapper_tck_output_type_invalid", vector_id)
    return result


def _assert_accepted(
    mapper: NativeIMInboundMapperPort,
    vector: MapperTCKAcceptedV1,
    *,
    mapper_contract_id: str,
    mapper_contract_digest: str,
) -> NativeIMMappedPageV1:
    result = _invoke(mapper, vector.context, vector_id=vector.vector_id)
    if result != vector.expected:
        _fail("native_im_mapper_tck_golden_mismatch", vector.vector_id)
    context = vector.context
    page = parse_native_im_inbound_page_v1(
        context.response,
        result,
        context.request,
        context.capability,
        context.raw_verification,
        context.configuration,
        context.profile,
    )
    expected_evidence = derive_native_im_mapping_evidence_digest_v1(
        mapper_contract_id=mapper_contract_id,
        mapper_contract_digest=mapper_contract_digest,
        profile_digest=context.profile.canonical_digest(),
        read_request_digest=context.request.canonical_digest(),
        capability_digest=context.capability.canonical_digest(),
        source_body_digest=context.raw_verification.body_digest,
        page_digest=page.canonical_digest(),
    )
    if result.mapping_evidence_digest != expected_evidence:
        _fail("native_im_mapper_tck_evidence_mismatch", vector.vector_id)
    return result


def _assert_rejected(
    mapper: NativeIMInboundMapperPort,
    vector: MapperTCKRejectedV1,
) -> None:
    cloned = _clone_context(vector.context)
    before = _context_snapshot(cloned)
    error: NativeIMMapperRejectionError | None = None
    effect_failed = False
    try:
        with native_im_mapper_zero_effect_fence_v1():
            mapper.map_inbound(
                cloned.response,
                cloned.request,
                cloned.capability,
                cloned.raw_verification,
                cloned.profile,
            )
    except NativeIMMapperRejectionError as caught:
        error = caught
    except _NativeIMMapperForbiddenEffect as caught:
        effect_failed = True
        caught.__traceback__ = None
        caught.__cause__ = None
        caught.__context__ = None
    except Exception as caught:
        caught.__traceback__ = None
        caught.__cause__ = None
        caught.__context__ = None
    if _context_snapshot(cloned) != before:
        _fail("native_im_mapper_tck_input_mutated", vector.vector_id)
    if effect_failed:
        _fail("native_im_mapper_tck_effect_forbidden", vector.vector_id)
    if type(error) is not NativeIMMapperRejectionError:
        _fail("native_im_mapper_tck_rejection_type_invalid", vector.vector_id)
    if (
        error.code != vector.expected_error_code
        or str(error) != vector.expected_error_code
        or error.args != (vector.expected_error_code,)
        or error.__cause__ is not None
        or error.__context__ is not None
    ):
        _fail("native_im_mapper_tck_rejection_drift", vector.vector_id)


def _suite_digest(
    mapper_contract_id: str,
    mapper_contract_digest: str,
    accepted: tuple[MapperTCKAcceptedV1, ...],
    rejected: tuple[MapperTCKRejectedV1, ...],
) -> str:
    lines = [mapper_contract_id, mapper_contract_digest]
    for accepted_vector in accepted:
        lines.extend(
            (
                "accepted",
                accepted_vector.vector_id,
                hashlib.sha256(accepted_vector.context.response.raw_body).hexdigest(),
                accepted_vector.context.request.canonical_digest(),
                accepted_vector.context.capability.canonical_digest(),
                accepted_vector.context.profile.canonical_digest(),
                hashlib.sha256(accepted_vector.expected.canonical_page_body).hexdigest(),
                accepted_vector.expected.mapping_evidence_digest,
            )
        )
    for rejected_vector in rejected:
        lines.extend(
            (
                "rejected",
                rejected_vector.vector_id,
                hashlib.sha256(rejected_vector.context.response.raw_body).hexdigest(),
                rejected_vector.expected_error_code,
            )
        )
    encoded = "\n".join(lines).encode("utf-8")
    domain = b"quantum-entanglement.native-im/MapperTCKSuiteV1/1\n"
    return hashlib.sha256(domain + encoded).hexdigest()


def assert_native_im_mapper_tck_v1(
    mapper_factory: Callable[[], object],
    mapper_type: type,
    *,
    mapper_contract_id: str,
    mapper_contract_digest: str,
    accepted: tuple[MapperTCKAcceptedV1, ...],
    rejected: tuple[MapperTCKRejectedV1, ...],
) -> MapperTCKReportV1:
    """Run deterministic accepted/rejected vectors against exact fresh mapper instances."""

    derive_native_im_mapping_evidence_digest_v1(
        mapper_contract_id=mapper_contract_id,
        mapper_contract_digest=mapper_contract_digest,
        profile_digest="0" * 64,
        read_request_digest="0" * 64,
        capability_digest="0" * 64,
        source_body_digest="0" * 64,
        page_digest="0" * 64,
    )
    if type(accepted) is not tuple or not accepted:
        raise TypeError("mapper TCK requires accepted vectors as a non-empty tuple")
    if type(rejected) is not tuple or not rejected:
        raise TypeError("mapper TCK requires rejected vectors as a non-empty tuple")
    if type(mapper_type) is not type or not callable(mapper_factory):
        raise TypeError("mapper TCK requires an exact mapper type and factory")
    vectors: tuple[MapperTCKAcceptedV1 | MapperTCKRejectedV1, ...] = (*accepted, *rejected)
    if any(type(vector) not in {MapperTCKAcceptedV1, MapperTCKRejectedV1} for vector in vectors):
        raise TypeError("mapper TCK vectors require exact V1 classes")
    vector_ids = tuple(vector.vector_id for vector in vectors)
    if len(vector_ids) != len(set(vector_ids)):
        raise ValueError("mapper TCK vector IDs must be unique")

    retained_mappers: list[object] = []
    for accepted_vector in accepted:
        with native_im_mapper_zero_effect_fence_v1():
            mapper = mapper_factory()
        if type(mapper) is not mapper_type or not isinstance(mapper, NativeIMInboundMapperPort):
            _fail("native_im_mapper_tck_mapper_type_invalid", accepted_vector.vector_id)
        retained_mappers.append(mapper)
        first = _assert_accepted(
            mapper,
            accepted_vector,
            mapper_contract_id=mapper_contract_id,
            mapper_contract_digest=mapper_contract_digest,
        )
        second = _assert_accepted(
            mapper,
            accepted_vector,
            mapper_contract_id=mapper_contract_id,
            mapper_contract_digest=mapper_contract_digest,
        )
        if first != second:
            _fail(
                "native_im_mapper_tck_same_instance_nondeterministic",
                accepted_vector.vector_id,
            )
        with native_im_mapper_zero_effect_fence_v1():
            fresh = mapper_factory()
        if type(fresh) is not mapper_type or not isinstance(fresh, NativeIMInboundMapperPort):
            _fail("native_im_mapper_tck_mapper_type_invalid", accepted_vector.vector_id)
        if fresh is mapper:
            _fail("native_im_mapper_tck_factory_reused_instance", accepted_vector.vector_id)
        retained_mappers.append(fresh)
        if (
            _assert_accepted(
                fresh,
                accepted_vector,
                mapper_contract_id=mapper_contract_id,
                mapper_contract_digest=mapper_contract_digest,
            )
            != first
        ):
            _fail(
                "native_im_mapper_tck_fresh_instance_nondeterministic",
                accepted_vector.vector_id,
            )

    for rejected_vector in rejected:
        with native_im_mapper_zero_effect_fence_v1():
            mapper = mapper_factory()
        if type(mapper) is not mapper_type or not isinstance(mapper, NativeIMInboundMapperPort):
            _fail("native_im_mapper_tck_mapper_type_invalid", rejected_vector.vector_id)
        retained_mappers.append(mapper)
        _assert_rejected(mapper, rejected_vector)
        _assert_rejected(mapper, rejected_vector)

    if len({id(mapper) for mapper in retained_mappers}) != len(retained_mappers):
        _fail("native_im_mapper_tck_factory_reused_instance", vector_ids[0])
    return MapperTCKReportV1(
        mapper_contract_id=mapper_contract_id,
        mapper_contract_digest=mapper_contract_digest,
        accepted_vector_ids=tuple(vector.vector_id for vector in accepted),
        rejected_vector_ids=tuple(vector.vector_id for vector in rejected),
        suite_digest=_suite_digest(
            mapper_contract_id,
            mapper_contract_digest,
            accepted,
            rejected,
        ),
    )


__all__ = [
    "MapperTCKAcceptedV1",
    "MapperTCKContextV1",
    "MapperTCKRejectedV1",
    "MapperTCKReportV1",
    "NativeIMMapperTCKFailure",
    "assert_native_im_mapper_tck_v1",
    "native_im_mapper_zero_effect_fence_v1",
]
