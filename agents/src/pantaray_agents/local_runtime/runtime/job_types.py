from __future__ import annotations

from typing import Final

LOCAL_ACTION_JOB_TYPE: Final[str] = "execute_action"
LOCAL_ACTION_SUBAGENT_JOB_TYPE: Final[str] = "execute_action_subagent"
LOCAL_SUGGESTION_JOB_TYPE: Final[str] = "generate_suggestion"
LOCAL_ACTIVITY_SUMMARY_JOB_TYPE: Final[str] = "summarize_activity"
LOCAL_INSIGHT_JOB_TYPE: Final[str] = "generate_insight"
LOCAL_MEMORY_UPDATE_JOB_TYPE: Final[str] = "memory_update"

ACTION_PROCESS_KIND: Final[str] = "action"
ACTION_SUBAGENT_PROCESS_KIND: Final[str] = "action_subagent"
SUGGESTION_PROCESS_KIND: Final[str] = "suggestion"
ACTIVITY_SUMMARY_PROCESS_KIND: Final[str] = "activity_summary"
INSIGHT_PROCESS_KIND: Final[str] = "insight"
MEMORY_PROCESS_KIND: Final[str] = "memory"

PROCESS_KIND_BY_JOB_TYPE: Final[dict[str, str]] = {
    LOCAL_ACTION_JOB_TYPE: ACTION_PROCESS_KIND,
    LOCAL_ACTION_SUBAGENT_JOB_TYPE: ACTION_SUBAGENT_PROCESS_KIND,
    LOCAL_SUGGESTION_JOB_TYPE: SUGGESTION_PROCESS_KIND,
    LOCAL_ACTIVITY_SUMMARY_JOB_TYPE: ACTIVITY_SUMMARY_PROCESS_KIND,
    LOCAL_INSIGHT_JOB_TYPE: INSIGHT_PROCESS_KIND,
    LOCAL_MEMORY_UPDATE_JOB_TYPE: MEMORY_PROCESS_KIND,
}
