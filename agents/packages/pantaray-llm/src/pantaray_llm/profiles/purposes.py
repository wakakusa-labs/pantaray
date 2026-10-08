"""Model-independent inference requirements shared by cloud and direct routing."""

from dataclasses import dataclass
from typing import Literal

from .llm import (
    ACTION_EXECUTING_PROFILE_ID,
    ACTION_PLANNING_PROFILE_ID,
    ACTIVITY_SUMMARY_PROFILE_ID,
    CHAT_PROFILE_ID,
    INSIGHT_PROFILE_ID,
    MEMORY_UPDATE_PROFILE_ID,
    SUGGESTION_PROFILE_ID,
    TOOL_THINKING_PROFILE_ID,
    ReasoningEffort,
)
from .subagent_models import SUBAGENT_MODEL_SETTINGS

type LlmCapability = Literal["tool_use", "image_input", "structured_output"]
DEFAULT_MAX_OUTPUT_TOKENS = 65536


@dataclass(frozen=True, slots=True)
class Purpose:
    id: str
    reasoning_effort: ReasoningEffort
    required_capabilities: frozenset[LlmCapability]
    image_detail: Literal["low", "high", "original", "auto"] | None = None


# Required capabilities describe the workflow. An image preference does not
# make images mandatory for a text-only task such as activity summary.
LLM_PURPOSES = {
    purpose.id: purpose
    for purpose in (
        Purpose(MEMORY_UPDATE_PROFILE_ID, "high", frozenset({"tool_use"})),
        Purpose(SUGGESTION_PROFILE_ID, "high", frozenset({"tool_use"}), "high"),
        Purpose(ACTIVITY_SUMMARY_PROFILE_ID, "medium", frozenset(), "high"),
        Purpose(INSIGHT_PROFILE_ID, "medium", frozenset({"tool_use"}), "high"),
        # The chat answers first and hands long work to an Action, so it stays quick.
        Purpose(CHAT_PROFILE_ID, "low", frozenset({"tool_use"}), "high"),
        *(
            Purpose(profile_id, "high", frozenset({"tool_use"}), "high")
            for profile_id in (
                ACTION_PLANNING_PROFILE_ID,
                ACTION_EXECUTING_PROFILE_ID,
                TOOL_THINKING_PROFILE_ID,
            )
        ),
        *(
            Purpose(
                settings.profile_id,
                settings.reasoning_effort,
                frozenset({"tool_use"}),
                "high",
            )
            for settings in SUBAGENT_MODEL_SETTINGS
        ),
    )
}
