"""Private immutable inputs for future owner-transaction result Artifact writes."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from itertools import islice
from typing import NoReturn

from .invocation_results import (
    ScopedInvocationResultArtifactCandidateV2,
    ScopedInvocationResultArtifactV2,
)

_MAX_RESULT_ARTIFACTS = 256
_MAX_RESULT_ARTIFACT_CONTENT_BYTES = 64 * 1024 * 1024
_MAX_RESULT_ARTIFACT_METADATA_BYTES = 1_048_576
_RESULT_ARTIFACT_TRANSACTION_TOKEN = object()


class _ResultArtifactTransactionHandle:
    """One non-transferable, exit-invalidated EventStore transaction capability."""

    __slots__ = (
        "__active",
        "__connection",
        "__generation",
        "__process_owner",
        "__store",
    )

    def __init__(
        self,
        *,
        store: object,
        connection: sqlite3.Connection,
        process_owner: object,
        generation: int,
        token: object,
    ) -> None:
        if type(self) is not _ResultArtifactTransactionHandle:
            raise TypeError("result Artifact transaction handle must use the exact private class")
        if token is not _RESULT_ARTIFACT_TRANSACTION_TOKEN:
            raise TypeError("result Artifact transaction handle constructor is private")
        if type(connection) is not sqlite3.Connection:
            raise TypeError(
                "result Artifact transaction handle requires an exact SQLite connection"
            )
        if type(generation) is not int or generation <= 0:
            raise ValueError("result Artifact transaction generation is invalid")
        self.__store = store
        self.__connection: sqlite3.Connection | None = connection
        self.__process_owner = process_owner
        self.__generation = generation
        self.__active = True

    def _validated_connection(
        self,
        *,
        store: object,
        process_owner: object,
        generation: int,
        token: object,
    ) -> sqlite3.Connection:
        if type(self) is not _ResultArtifactTransactionHandle:
            raise TypeError("result Artifact transaction handle must be exact")
        if token is not _RESULT_ARTIFACT_TRANSACTION_TOKEN:
            raise TypeError("result Artifact transaction validation is private")
        if type(self.__active) is not bool or not self.__active:
            raise RuntimeError("result Artifact transaction handle is no longer active")
        if self.__store is not store or self.__process_owner is not process_owner:
            raise RuntimeError("result Artifact transaction handle has a foreign owner")
        if type(generation) is not int or self.__generation != generation:
            raise RuntimeError("result Artifact transaction handle generation is not active")
        connection = self.__connection
        if type(connection) is not sqlite3.Connection:
            raise RuntimeError("result Artifact transaction handle connection changed")
        transaction_open = connection.in_transaction
        if type(transaction_open) is not bool or not transaction_open:
            raise RuntimeError("result Artifact transaction is not open")
        return connection

    def _invalidate(self, *, token: object) -> None:
        if token is not _RESULT_ARTIFACT_TRANSACTION_TOKEN:
            raise TypeError("result Artifact transaction invalidation is private")
        self.__active = False
        self.__store = None
        self.__connection = None
        self.__process_owner = None

    def __copy__(self) -> NoReturn:
        raise TypeError("result Artifact transaction handles cannot be copied")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("result Artifact transaction handles cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("result Artifact transaction handles cannot be serialized")


@dataclass(frozen=True)
class _PreparedResultArtifact:
    """One caller-detached candidate snapshot; content never enters its repr."""

    ordinal: int
    tenant_id: str
    workspace_id: str
    session_id: str
    task_id: str
    artifact_id: str
    name: str
    media_type: str
    content: bytes = field(repr=False)
    metadata_canonical_bytes: bytes = field(repr=False)
    metadata_json: str = field(repr=False)
    created_by: str
    idempotency_key: str
    expected_head_version: int
    descriptor: ScopedInvocationResultArtifactV2
    candidate_sha256: str

    def verify(self) -> None:
        if type(self) is not _PreparedResultArtifact:
            raise TypeError("prepared result Artifact must use the exact private class")
        if type(self.ordinal) is not int or not 0 <= self.ordinal < _MAX_RESULT_ARTIFACTS:
            raise ValueError("prepared result Artifact ordinal is invalid")
        if type(self.content) is not bytes or type(self.metadata_canonical_bytes) is not bytes:
            raise TypeError("prepared result Artifact bytes are not immutable")
        if type(self.metadata_json) is not str:
            raise TypeError("prepared result Artifact metadata JSON is not text")
        try:
            if self.metadata_canonical_bytes.decode("utf-8") != self.metadata_json:
                raise ValueError("prepared result Artifact metadata bytes changed")
        except UnicodeError as error:
            raise ValueError("prepared result Artifact metadata is not UTF-8") from error
        candidate = ScopedInvocationResultArtifactCandidateV2(
            tenant_id=self.tenant_id,
            workspace_id=self.workspace_id,
            session_id=self.session_id,
            task_id=self.task_id,
            artifact_id=self.artifact_id,
            name=self.name,
            media_type=self.media_type,
            content=self.content,
            metadata_canonical_bytes=self.metadata_canonical_bytes,
            created_by=self.created_by,
            idempotency_key=self.idempotency_key,
            expected_head_version=self.expected_head_version,
        )
        if type(self.descriptor) is not ScopedInvocationResultArtifactV2:
            raise TypeError("prepared result Artifact descriptor is not exact")
        if candidate.to_descriptor() != self.descriptor:
            raise ValueError("prepared result Artifact descriptor changed")
        if candidate.canonical_digest() != self.candidate_sha256:
            raise ValueError("prepared result Artifact candidate digest changed")


@dataclass(frozen=True)
class _PreparedResultArtifactBatch:
    """One exact ordered batch detached from caller-owned candidate objects."""

    items: tuple[_PreparedResultArtifact, ...]
    total_content_bytes: int
    total_metadata_bytes: int

    def verify(self) -> None:
        if type(self) is not _PreparedResultArtifactBatch:
            raise TypeError("prepared result Artifact batch must use the exact private class")
        if type(self.items) is not tuple:
            raise TypeError("prepared result Artifact batch items must be an exact tuple")
        if len(self.items) > _MAX_RESULT_ARTIFACTS:
            raise ValueError("prepared result Artifact batch exceeds its item limit")
        for ordinal, item in enumerate(self.items):
            if type(item) is not _PreparedResultArtifact:
                raise TypeError("prepared result Artifact batch contains a non-exact item")
            item.verify()
            if item.ordinal != ordinal:
                raise ValueError("prepared result Artifact batch order changed")
        content_bytes = sum(len(item.content) for item in self.items)
        metadata_bytes = sum(len(item.metadata_canonical_bytes) for item in self.items)
        if type(self.total_content_bytes) is not int or self.total_content_bytes != content_bytes:
            raise ValueError("prepared result Artifact content total changed")
        if (
            type(self.total_metadata_bytes) is not int
            or self.total_metadata_bytes != metadata_bytes
        ):
            raise ValueError("prepared result Artifact metadata total changed")
        if content_bytes > _MAX_RESULT_ARTIFACT_CONTENT_BYTES:
            raise ValueError("prepared result Artifact content exceeds its batch limit")
        if metadata_bytes > _MAX_RESULT_ARTIFACT_METADATA_BYTES:
            raise ValueError("prepared result Artifact metadata exceeds its batch limit")
        _validate_batch_identities(self.items)


def _validate_batch_identities(items: tuple[_PreparedResultArtifact, ...]) -> None:
    if not items:
        return
    expected_scope = (
        items[0].tenant_id,
        items[0].workspace_id,
        items[0].session_id,
        items[0].task_id,
    )
    artifact_ids: set[str] = set()
    idempotency_coordinates: set[tuple[str, str, str]] = set()
    head_coordinates: set[tuple[str, str, str, str]] = set()
    for item in items:
        scope = (item.tenant_id, item.workspace_id, item.session_id, item.task_id)
        if scope != expected_scope:
            raise ValueError("prepared result Artifact batch scope is not exact")
        if item.artifact_id in artifact_ids:
            raise ValueError("prepared result Artifact IDs must be unique")
        artifact_ids.add(item.artifact_id)
        idempotency_coordinate = (item.tenant_id, item.workspace_id, item.idempotency_key)
        if idempotency_coordinate in idempotency_coordinates:
            raise ValueError("prepared result Artifact idempotency keys must be unique")
        idempotency_coordinates.add(idempotency_coordinate)
        head_coordinate = (item.tenant_id, item.workspace_id, item.session_id, item.name)
        if head_coordinate in head_coordinates:
            raise ValueError("prepared result Artifact head coordinates must be unique")
        head_coordinates.add(head_coordinate)


def _prepare_result_artifact_batch(
    candidates: Iterable[ScopedInvocationResultArtifactCandidateV2],
) -> _PreparedResultArtifactBatch:
    try:
        iterator = iter(candidates)
    except TypeError as error:
        raise TypeError("result Artifact candidates must be iterable") from error
    snapshot = tuple(islice(iterator, _MAX_RESULT_ARTIFACTS + 1))
    if len(snapshot) > _MAX_RESULT_ARTIFACTS:
        raise ValueError("result Artifact candidates exceed the batch item limit")
    prepared: list[_PreparedResultArtifact] = []
    for ordinal, candidate in enumerate(snapshot):
        if type(candidate) is not ScopedInvocationResultArtifactCandidateV2:
            raise TypeError("result Artifact candidates must use the exact schema-2 class")
        ScopedInvocationResultArtifactCandidateV2.__post_init__(candidate)
        metadata_bytes = candidate.metadata_canonical_bytes
        item = _PreparedResultArtifact(
            ordinal=ordinal,
            tenant_id=candidate.tenant_id,
            workspace_id=candidate.workspace_id,
            session_id=candidate.session_id,
            task_id=candidate.task_id,
            artifact_id=candidate.artifact_id,
            name=candidate.name,
            media_type=candidate.media_type,
            content=candidate.content,
            metadata_canonical_bytes=metadata_bytes,
            metadata_json=metadata_bytes.decode("utf-8"),
            created_by=candidate.created_by,
            idempotency_key=candidate.idempotency_key,
            expected_head_version=candidate.expected_head_version,
            descriptor=candidate.to_descriptor(),
            candidate_sha256=candidate.canonical_digest(),
        )
        item.verify()
        prepared.append(item)
    batch = _PreparedResultArtifactBatch(
        items=tuple(prepared),
        total_content_bytes=sum(len(item.content) for item in prepared),
        total_metadata_bytes=sum(len(item.metadata_canonical_bytes) for item in prepared),
    )
    batch.verify()
    return batch
