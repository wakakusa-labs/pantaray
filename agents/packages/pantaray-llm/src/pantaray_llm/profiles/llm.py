from typing import Literal

type ReasoningEffort = Literal["none", "low", "medium", "high", "xhigh", "max"]

OPENAI_GPT_6_LUNA_MODEL = "gpt-6-luna"
OPENAI_GPT_56_SOL_MODEL = "gpt-5.6-sol"

SUGGESTION_PROFILE_ID = "suggestion"
ACTIVITY_SUMMARY_PROFILE_ID = "activity_summary"
INSIGHT_PROFILE_ID = "insight"
ACTION_PLANNING_PROFILE_ID = "action.planning"
ACTION_EXECUTING_PROFILE_ID = "action.executing"
TOOL_THINKING_PROFILE_ID = "action.tool_thinking"
MEMORY_UPDATE_PROFILE_ID = "memory_update"
CHAT_PROFILE_ID = "chat"

__all__ = [
    "ACTION_PLANNING_PROFILE_ID",
    "ACTION_EXECUTING_PROFILE_ID",
    "ACTIVITY_SUMMARY_PROFILE_ID",
    "CHAT_PROFILE_ID",
    "INSIGHT_PROFILE_ID",
    "MEMORY_UPDATE_PROFILE_ID",
    "OPENAI_GPT_6_LUNA_MODEL",
    "OPENAI_GPT_56_SOL_MODEL",
    "ReasoningEffort",
    "SUGGESTION_PROFILE_ID",
    "TOOL_THINKING_PROFILE_ID",
]
