"""Typed repository contract for ActionAgent initial memory context."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class InitialInsightBrief:
    insight_id: str
    insight_profile_brief: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class InitialFactsBrief:
    fact_id: str
    facts_profile_brief: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class InitialMemoryContext:
    insight: InitialInsightBrief | None
    facts: InitialFactsBrief | None


__all__ = [
    "InitialFactsBrief",
    "InitialInsightBrief",
    "InitialMemoryContext",
]
