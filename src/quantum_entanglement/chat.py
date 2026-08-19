"""Provider-neutral group-chat ingress and deterministic @Agent routing."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable, Dict, Mapping, Optional, Tuple

from .protocol import ActorKind, ActorRef, CoordinationEnvelope, EnvelopeKind


class ChatRoute(str, Enum):
    DIRECT = "direct"
    PLANNER = "planner"


@dataclass(frozen=True)
class InboundChatMessage:
    provider: str
    external_message_id: str
    session_id: str
    thread_id: str
    sender: ActorRef
    text: str
    mentioned_actor_ids: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        required = (
            self.provider,
            self.external_message_id,
            self.session_id,
            self.thread_id,
            self.text,
        )
        if not all(item.strip() for item in required):
            raise ValueError("chat provider, ids, and text are required")


@dataclass(frozen=True)
class RoutedChatMessage:
    route: ChatRoute
    envelope: CoordinationEnvelope
    direct_agents: Tuple[ActorRef, ...] = ()


class MentionRouter:
    """Direct mentions bypass semantic planning but never bypass event ingestion."""

    def __init__(self, roster: Mapping[str, ActorRef], planner: ActorRef) -> None:
        self.roster: Dict[str, ActorRef] = dict(roster)
        self.planner = planner
        if planner.kind not in (ActorKind.AGENT, ActorKind.SYSTEM):
            raise ValueError("planner must be an agent or system actor")

    def route(self, message: InboundChatMessage) -> RoutedChatMessage:
        unknown = set(message.mentioned_actor_ids) - set(self.roster)
        if unknown:
            raise KeyError("mentioned actors are not in the session roster: %s" % sorted(unknown))
        direct = tuple(
            self.roster[actor_id]
            for actor_id in message.mentioned_actor_ids
            if self.roster[actor_id].kind == ActorKind.AGENT
        )
        route = ChatRoute.DIRECT if direct else ChatRoute.PLANNER
        recipients = direct or (self.planner,)
        envelope = CoordinationEnvelope.create(
            session_id=message.session_id,
            thread_id=message.thread_id,
            sender=message.sender,
            recipients=recipients,
            kind=EnvelopeKind.CHAT,
            payload={
                "text": message.text,
                "route": route.value,
                "provider": message.provider,
                "externalMessageId": message.external_message_id,
                "mentionedActorIds": list(message.mentioned_actor_ids),
            },
            causation_id=message.external_message_id,
            idempotency_key="chat:%s:%s" % (message.provider, message.external_message_id),
        )
        return RoutedChatMessage(route, envelope, direct)

    async def dispatch(
        self,
        message: InboundChatMessage,
        direct_handler: Callable[[RoutedChatMessage], Awaitable[object]],
        planner_handler: Callable[[RoutedChatMessage], Awaitable[object]],
    ) -> object:
        routed = self.route(message)
        if routed.route == ChatRoute.DIRECT:
            return await direct_handler(routed)
        return await planner_handler(routed)
