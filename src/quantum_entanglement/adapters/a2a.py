"""Small, dependency-free A2A boundary adapter.

The internal CoordinationEnvelope remains the source of governance metadata.
This module only maps it to A2A JSON-RPC messages and restores remote results.
It intentionally does not turn A2A task state into the platform's source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

from ..protocol import (
    ActorRef,
    CoordinationEnvelope,
    EnvelopeKind,
    new_id,
)


@dataclass(frozen=True)
class A2ASkill:
    skill_id: str
    name: str
    description: str
    tags: Tuple[str, ...] = ()
    examples: Tuple[str, ...] = ()
    input_modes: Tuple[str, ...] = ()
    output_modes: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.skill_id.strip() or not self.name.strip() or not self.description.strip():
            raise ValueError("A2A skill id, name, and description are required")

    def to_dict(self) -> Dict[str, Any]:
        value: Dict[str, Any] = {
            "id": self.skill_id,
            "name": self.name,
            "description": self.description,
            "tags": list(self.tags),
            "examples": list(self.examples),
        }
        if self.input_modes:
            value["inputModes"] = list(self.input_modes)
        if self.output_modes:
            value["outputModes"] = list(self.output_modes)
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "A2ASkill":
        return cls(
            skill_id=str(value["id"]),
            name=str(value["name"]),
            description=str(value["description"]),
            tags=tuple(str(item) for item in value.get("tags", ())),
            examples=tuple(str(item) for item in value.get("examples", ())),
            input_modes=tuple(str(item) for item in value.get("inputModes", ())),
            output_modes=tuple(str(item) for item in value.get("outputModes", ())),
        )


@dataclass(frozen=True)
class A2AAgentCard:
    """Stable subset of an A2A Agent Card plus lossless extension fields."""

    name: str
    description: str
    url: str
    version: str
    skills: Tuple[A2ASkill, ...]
    protocol_version: Optional[str] = None
    preferred_transport: Optional[str] = None
    capabilities: Mapping[str, Any] = field(default_factory=dict)
    default_input_modes: Tuple[str, ...] = ("text/plain",)
    default_output_modes: Tuple[str, ...] = ("text/plain",)
    security_schemes: Mapping[str, Any] = field(default_factory=dict)
    security: Tuple[Mapping[str, Any], ...] = ()
    provider: Optional[Mapping[str, Any]] = None
    documentation_url: Optional[str] = None
    icon_url: Optional[str] = None
    extensions: Mapping[str, Any] = field(default_factory=dict)

    KNOWN_FIELDS = frozenset(
        {
            "name", "description", "url", "version", "skills", "protocolVersion",
            "preferredTransport", "capabilities", "defaultInputModes", "defaultOutputModes",
            "securitySchemes", "security", "provider", "documentationUrl", "iconUrl",
        }
    )

    def __post_init__(self) -> None:
        if not all(item.strip() for item in (self.name, self.description, self.url, self.version)):
            raise ValueError("A2A card name, description, url, and version are required")
        if not self.skills:
            raise ValueError("A2A card needs at least one skill")
        if not (self.url.startswith("https://") or self.url.startswith("http://")):
            raise ValueError("A2A card url must be HTTP(S)")

    def to_dict(self) -> Dict[str, Any]:
        value: Dict[str, Any] = dict(self.extensions)
        value.update(
            {
                "name": self.name,
                "description": self.description,
                "url": self.url,
                "version": self.version,
                "capabilities": dict(self.capabilities),
                "defaultInputModes": list(self.default_input_modes),
                "defaultOutputModes": list(self.default_output_modes),
                "skills": [skill.to_dict() for skill in self.skills],
            }
        )
        optional = {
            "protocolVersion": self.protocol_version,
            "preferredTransport": self.preferred_transport,
            "provider": dict(self.provider) if self.provider else None,
            "documentationUrl": self.documentation_url,
            "iconUrl": self.icon_url,
        }
        value.update({key: item for key, item in optional.items() if item is not None})
        if self.security_schemes:
            value["securitySchemes"] = dict(self.security_schemes)
        if self.security:
            value["security"] = [dict(item) for item in self.security]
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "A2AAgentCard":
        extensions = {key: item for key, item in value.items() if key not in cls.KNOWN_FIELDS}
        return cls(
            name=str(value["name"]),
            description=str(value["description"]),
            url=str(value["url"]),
            version=str(value["version"]),
            skills=tuple(A2ASkill.from_dict(item) for item in value.get("skills", ())),
            protocol_version=(str(value["protocolVersion"]) if value.get("protocolVersion") else None),
            preferred_transport=(
                str(value["preferredTransport"]) if value.get("preferredTransport") else None
            ),
            capabilities=dict(value.get("capabilities", {})),
            default_input_modes=tuple(str(item) for item in value.get("defaultInputModes", ())),
            default_output_modes=tuple(str(item) for item in value.get("defaultOutputModes", ())),
            security_schemes=dict(value.get("securitySchemes", {})),
            security=tuple(dict(item) for item in value.get("security", ())),
            provider=(dict(value["provider"]) if value.get("provider") else None),
            documentation_url=(
                str(value["documentationUrl"]) if value.get("documentationUrl") else None
            ),
            icon_url=(str(value["iconUrl"]) if value.get("iconUrl") else None),
            extensions=extensions,
        )


class A2AJsonRpcAdapter:
    """Maps internal envelopes to A2A JSON-RPC without losing governance fields."""

    def __init__(self, local_actor: ActorRef) -> None:
        self.local_actor = local_actor

    def message_send_request(
        self,
        envelope: CoordinationEnvelope,
        request_id: Optional[str] = None,
        blocking: bool = False,
    ) -> Dict[str, Any]:
        if envelope.kind not in (EnvelopeKind.TASK_ASSIGN, EnvelopeKind.CHAT, EnvelopeKind.HANDOFF):
            raise ValueError("envelope kind cannot be sent as an A2A user message")
        return {
            "jsonrpc": "2.0",
            "id": request_id or envelope.message_id,
            "method": "message/send",
            "params": {
                "message": {
                    "kind": "message",
                    "messageId": envelope.message_id,
                    "role": "user",
                    "parts": [
                        {
                            "kind": "data",
                            "data": {
                                "wanworkEnvelope": envelope.to_dict(),
                            },
                        }
                    ],
                    "metadata": {
                        "wanworkCorrelationId": envelope.correlation_id,
                        "wanworkCausationId": envelope.causation_id,
                        "wanworkIdempotencyKey": envelope.idempotency_key,
                        "wanworkSchema": envelope.schema_version,
                    },
                },
                "configuration": {"blocking": blocking},
            },
        }

    @staticmethod
    def task_get_request(task_id: str, request_id: Optional[str] = None) -> Dict[str, Any]:
        if not task_id.strip():
            raise ValueError("A2A task id is required")
        return {
            "jsonrpc": "2.0",
            "id": request_id or new_id("rpc"),
            "method": "tasks/get",
            "params": {"id": task_id},
        }

    @staticmethod
    def task_cancel_request(task_id: str, request_id: Optional[str] = None) -> Dict[str, Any]:
        if not task_id.strip():
            raise ValueError("A2A task id is required")
        return {
            "jsonrpc": "2.0",
            "id": request_id or new_id("rpc"),
            "method": "tasks/cancel",
            "params": {"id": task_id},
        }

    def result_envelope(
        self,
        request: CoordinationEnvelope,
        remote_actor: ActorRef,
        response: Mapping[str, Any],
    ) -> CoordinationEnvelope:
        if response.get("error") is not None:
            kind = EnvelopeKind.ERROR
            payload: Mapping[str, Any] = {"a2aError": dict(response["error"])}
        else:
            result = response.get("result")
            if not isinstance(result, Mapping):
                raise ValueError("A2A response needs a result object or error")
            remote_kind = str(result.get("kind", ""))
            kind = (
                EnvelopeKind.TASK_PROGRESS
                if remote_kind == "status-update"
                else EnvelopeKind.TASK_RESULT
            )
            payload = {"a2aResult": dict(result)}
        return CoordinationEnvelope.create(
            session_id=request.session_id,
            thread_id=request.thread_id,
            sender=remote_actor,
            recipients=(request.sender,),
            kind=kind,
            payload=payload,
            correlation_id=request.correlation_id,
            causation_id=request.message_id,
            idempotency_key="a2a-response:%s:%s" % (request.message_id, response.get("id", "none")),
            authority=request.authority,
        )
