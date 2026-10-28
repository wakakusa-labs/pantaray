from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .llm import (
    OPENAI_GPT_6_LUNA_MODEL,
    OPENAI_GPT_61_SOL_MODEL,
    ReasoningEffort,
)


@dataclass(frozen=True, slots=True)
class SubagentModelSettings:
    selector: str
    profile_id: str
    provider: Literal["openai"]
    model: str
    reasoning_effort: ReasoningEffort
    recommendation: str


SUBAGENT_MODEL_SETTINGS = (
    SubagentModelSettings(
        selector="gpt-6-luna",
        profile_id="action.subagent.luna",
        provider="openai",
        model=OPENAI_GPT_6_LUNA_MODEL,
        reasoning_effort="high",
        recommendation="Recommended for all subagent work outside the explicit Sol recovery cases.",
    ),
    SubagentModelSettings(
        selector="gpt-6.1-sol",
        profile_id="action.subagent.sol",
        provider="openai",
        model=OPENAI_GPT_61_SOL_MODEL,
        reasoning_effort="high",
        recommendation=(
            "Recommended only after observed looping or non-progress, or material "
            "goal divergence that parent correction cannot recover."
        ),
    ),
)


__all__ = [
    "SUBAGENT_MODEL_SETTINGS",
    "SubagentModelSettings",
]
