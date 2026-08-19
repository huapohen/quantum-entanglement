# ruff: noqa: UP006, UP031, UP035, UP045
"""Append-only artifact version ledger backed by collaboration events."""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from typing import Dict, Mapping, Optional, Tuple
from urllib.parse import quote

from .events import DomainEvent
from .protocol import ArtifactOutput, ArtifactRef, new_id, utc_now
from .store import SQLiteEventStore

_REPLAY_PAGE_LIMIT = 1_000
_MAX_REPLAY_EVENTS = 1_000_000


class ArtifactReplayError(RuntimeError):
    """Raised when startup replay cannot advance safely within its hard bounds."""


@dataclass(frozen=True)
class ArtifactVersion:
    ref: ArtifactRef
    content: str
    metadata: Mapping[str, object]
    created_at: str
    trigger: str


class ArtifactLedger:
    """Owns current versions so worker agents can remain stateless."""

    EVENT_TYPE = "artifact.versioned"

    def __init__(self, event_store: SQLiteEventStore) -> None:
        self.event_store = event_store
        self._versions: Dict[Tuple[str, str], Tuple[ArtifactVersion, ...]] = {}
        self._lock = threading.RLock()
        self._rebuild()

    def _rebuild(self) -> None:
        after_position = 0
        replayed = 0
        while replayed < _MAX_REPLAY_EVENTS:
            page_limit = min(_REPLAY_PAGE_LIMIT, _MAX_REPLAY_EVENTS - replayed)
            page = self.event_store.read_all(
                after_position=after_position,
                limit=page_limit,
            )
            after_position = self._validate_replay_page(
                page,
                after_position=after_position,
                requested_limit=page_limit,
            )
            if not page:
                return
            for stored in page:
                if stored.event.event_type != self.EVENT_TYPE:
                    continue
                payload = stored.event.payload
                ref = ArtifactRef.from_dict(payload["ref"])
                item = ArtifactVersion(
                    ref=ref,
                    content=str(payload["content"]),
                    metadata=dict(payload.get("metadata", {})),
                    created_at=str(payload["createdAt"]),
                    trigger=str(payload.get("trigger", "create")),
                )
                key = (str(payload["sessionId"]), ref.name)
                self._versions[key] = self._versions.get(key, ()) + (item,)
            replayed += len(page)
            if len(page) < page_limit:
                return

        probe = self.event_store.read_all(after_position=after_position, limit=1)
        self._validate_replay_page(
            probe,
            after_position=after_position,
            requested_limit=1,
        )
        if probe:
            raise ArtifactReplayError(
                f"artifact replay exceeds the {_MAX_REPLAY_EVENTS}-event safety limit"
            )

    @staticmethod
    def _validate_replay_page(
        page: Tuple[object, ...],
        *,
        after_position: int,
        requested_limit: int,
    ) -> int:
        if len(page) > requested_limit:
            raise ArtifactReplayError("artifact replay source exceeded its requested page limit")
        previous_position = after_position
        for stored in page:
            position = getattr(stored, "global_position", None)
            if type(position) is not int:
                raise ArtifactReplayError(
                    "artifact replay source returned an invalid global position"
                )
            if position <= previous_position:
                raise ArtifactReplayError(
                    "artifact replay global positions are not strictly increasing"
                )
            previous_position = position
        return previous_position

    @staticmethod
    def _digest(content: str) -> str:
        return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()

    def record(
        self,
        session_id: str,
        task_id: str,
        agent_id: str,
        output: ArtifactOutput,
        correlation_id: Optional[str] = None,
        causation_id: Optional[str] = None,
        trigger: Optional[str] = None,
    ) -> ArtifactVersion:
        key = (session_id, output.name)
        with self._lock:
            history = self._versions.get(key, ())
            version = len(history) + 1
            actual_trigger = trigger or ("create" if version == 1 else "revise")
            ref = ArtifactRef(
                artifact_id=new_id("art"),
                name=output.name,
                version=version,
                media_type=output.media_type,
                uri="artifact://%s/%s/v%d"
                % (quote(session_id, safe=""), quote(output.name), version),
                digest=self._digest(output.content),
                created_by=agent_id,
                task_id=task_id,
                parent_version=(version - 1 if version > 1 else None),
            )
            created_at = utc_now()
            item = ArtifactVersion(
                ref=ref,
                content=output.content,
                metadata=dict(output.metadata),
                created_at=created_at,
                trigger=actual_trigger,
            )
            event = DomainEvent(
                stream_id="session:%s" % session_id,
                event_type=self.EVENT_TYPE,
                actor_id=agent_id,
                correlation_id=correlation_id,
                causation_id=causation_id,
                idempotency_key="artifact:%s:%s:%s" % (task_id, output.name, ref.digest),
                payload={
                    "sessionId": session_id,
                    "taskId": task_id,
                    "ref": ref.to_dict(),
                    "content": output.content,
                    "metadata": dict(output.metadata),
                    "createdAt": created_at,
                    "trigger": actual_trigger,
                },
            )
            stored = self.event_store.append(event)
            # Idempotent retries return the existing event; rebuild the exact existing result.
            if stored.event.event_id != event.event_id:
                existing_ref = ArtifactRef.from_dict(stored.event.payload["ref"])
                for existing in history:
                    if existing.ref.artifact_id == existing_ref.artifact_id:
                        return existing
                self._rebuild()
                return next(
                    existing
                    for existing in self._versions[key]
                    if existing.ref.artifact_id == existing_ref.artifact_id
                )
            self._versions[key] = history + (item,)
            return item

    def current(self, session_id: str, name: str) -> Optional[ArtifactVersion]:
        with self._lock:
            history = self._versions.get((session_id, name), ())
            return history[-1] if history else None

    def history(self, session_id: str, name: str) -> Tuple[ArtifactVersion, ...]:
        with self._lock:
            return self._versions.get((session_id, name), ())

    def current_all(self, session_id: str) -> Tuple[ArtifactVersion, ...]:
        with self._lock:
            return tuple(
                versions[-1]
                for (candidate_session, _), versions in sorted(self._versions.items())
                if candidate_session == session_id and versions
            )

    def by_task(self, session_id: str, task_id: str) -> Tuple[ArtifactVersion, ...]:
        """Return outputs attributed to one task, rebuilt from the append-only ledger."""

        with self._lock:
            items = [
                item
                for (candidate_session, _), versions in self._versions.items()
                if candidate_session == session_id
                for item in versions
                if item.ref.task_id == task_id
            ]
            items.sort(key=lambda item: (item.ref.name, item.ref.version))
            return tuple(items)

    def restore(
        self,
        session_id: str,
        name: str,
        target_version: int,
        task_id: str,
        actor_id: str,
    ) -> ArtifactVersion:
        history = self.history(session_id, name)
        if target_version <= 0 or target_version > len(history):
            raise KeyError("artifact version not found: %s v%d" % (name, target_version))
        source = history[target_version - 1]
        return self.record(
            session_id=session_id,
            task_id=task_id,
            agent_id=actor_id,
            output=ArtifactOutput(
                name=name,
                content=source.content,
                media_type=source.ref.media_type,
                metadata={"restoredFrom": target_version},
            ),
            trigger="rollback",
        )


__all__ = ["ArtifactLedger", "ArtifactReplayError", "ArtifactVersion"]
