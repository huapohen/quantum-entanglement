"""Budgeted context compiler with explicit provenance and omissions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Mapping, Tuple


class ContextBudgetError(ValueError):
    """Required context does not fit and must not be silently truncated."""


@dataclass(frozen=True)
class ContextItem:
    item_id: str
    category: str
    content: str
    required: bool = False
    relevance: float = 0.5
    provenance: str = "runtime"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.item_id.strip() or not self.category.strip():
            raise ValueError("context item_id and category are required")
        if not 0.0 <= self.relevance <= 1.0:
            raise ValueError("context relevance must be between 0 and 1")

    @property
    def estimated_tokens(self) -> int:
        # Deterministic conservative estimate; providers can replace it with a tokenizer plugin.
        return max(1, (len(self.content.encode("utf-8")) + 3) // 4)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "itemId": self.item_id,
            "category": self.category,
            "content": self.content,
            "required": self.required,
            "relevance": self.relevance,
            "provenance": self.provenance,
            "estimatedTokens": self.estimated_tokens,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class ContextBundle:
    items: Tuple[ContextItem, ...]
    omitted_item_ids: Tuple[str, ...]
    token_budget: int
    estimated_tokens: int
    digest: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "items": [item.to_dict() for item in self.items],
            "omittedItemIds": list(self.omitted_item_ids),
            "tokenBudget": self.token_budget,
            "estimatedTokens": self.estimated_tokens,
            "digest": self.digest,
        }

    def render(self) -> str:
        sections = []
        for item in self.items:
            sections.append("## %s [%s]\n%s" % (item.category, item.item_id, item.content))
        if self.omitted_item_ids:
            sections.append("## omitted\n" + ", ".join(self.omitted_item_ids))
        return "\n\n".join(sections)


class ContextCompiler:
    """Select required and relevant inputs while making every omission inspectable."""

    CATEGORY_WEIGHT = {
        "policy": 100,
        "goal": 95,
        "handoff": 90,
        "artifact": 80,
        "decision": 75,
        "memory": 60,
        "chat": 50,
        "tool": 40,
    }

    def compile(self, items: Iterable[ContextItem], token_budget: int) -> ContextBundle:
        if token_budget <= 0:
            raise ValueError("token_budget must be positive")
        candidates = tuple(items)
        required_tokens = sum(item.estimated_tokens for item in candidates if item.required)
        if required_tokens > token_budget:
            raise ContextBudgetError(
                "required context needs %d tokens but budget is %d"
                % (required_tokens, token_budget)
            )
        ordered = sorted(
            candidates,
            key=lambda item: (
                not item.required,
                -self.CATEGORY_WEIGHT.get(item.category, 0),
                -item.relevance,
                item.item_id,
            ),
        )
        selected = []
        omitted = []
        used = 0
        for item in ordered:
            if used + item.estimated_tokens <= token_budget:
                selected.append(item)
                used += item.estimated_tokens
            else:
                omitted.append(item.item_id)
        serialized = json.dumps(
            [item.to_dict() for item in selected], ensure_ascii=False, sort_keys=True
        ).encode("utf-8")
        digest = "sha256:" + hashlib.sha256(serialized).hexdigest()
        return ContextBundle(tuple(selected), tuple(omitted), token_budget, used, digest)

