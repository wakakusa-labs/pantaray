"""ActionAgent で使用するツール定義を公開するモジュール。"""

from .action_plan_tools import (
    READ_ACTION_PLAN_TOOL,
    READ_ACTION_PLAN_TOOL_ID,
    WRITE_ACTION_PLAN_TOOL,
    WRITE_ACTION_PLAN_TOOL_ID,
)
from .apply_patch_tool import APPLY_PATCH_TOOL
from .base import ToolDefinition
from .bash_tool import BASH_TOOL
from .capture_screen_tool import CAPTURE_SCREEN_TOOL, CAPTURE_SCREEN_TOOL_ID
from .discovery_tools import GLOB_TOOL, GREP_TOOL, LIST_TOOL
from .draft_final_answer_tool import (
    DRAFT_FINAL_ANSWER_TOOL,
    DRAFT_FINAL_ANSWER_TOOL_ID,
)
from .history_fetch_tool import HISTORY_FETCH_TOOL
from .memory_link_tools import (
    GET_MEMORY_REFERENCE_TOOL,
    LINK_MEMORY_TOOL,
    UNLINK_MEMORY_TOOL,
)
from .memory_search_tool import MEMORY_SEARCH_TOOL
from .memory_sql_tool import MEMORY_SQL_TOOL
from .native_tool_use import (
    STEP_NOTE_ARG,
    STEP_NOTE_DESCRIPTION,
    STEP_NOTE_MAX_LENGTH,
    build_native_action_tools,
    split_step_note,
)
from .read_tool import READ_TOOL
from .remember_tool import REMEMBER_TOOL, REMEMBER_TOOL_ID
from .render_pdf_page_tool import (
    RENDER_PDF_PAGE_TOOL,
    RENDER_PDF_PAGE_TOOL_ID,
)
from .run_python_tool import RUN_PYTHON_TOOL
from .subagent_tool import (
    CANCEL_SUBAGENT_TOOL,
    CANCEL_SUBAGENT_TOOL_ID,
    SEND_MESSAGE_TO_SUBAGENT_TOOL,
    SEND_MESSAGE_TO_SUBAGENT_TOOL_ID,
    SPAWN_SUBAGENT_TOOL,
    SPAWN_SUBAGENT_TOOL_ID,
    SUBMIT_SUBAGENT_REPORT_TOOL_ID,
    WAIT_SUBAGENTS_TOOL,
    WAIT_SUBAGENTS_TOOL_ID,
)
from .submit_final_answer_tool import (
    SUBMIT_FINAL_ANSWER_TOOL,
    SUBMIT_FINAL_ANSWER_TOOL_ID,
)
from .thinking_tool import THINKING_TOOL
from .web_crawl_tool import WEB_CRAWL_TOOL
from .web_extract_tool import WEB_EXTRACT_TOOL
from .web_search_tool import WEB_SEARCH_TOOL
from .zanei_tools import (
    ZANEI_QUERY_TOOL,
    ZANEI_QUERY_TOOL_ID,
    ZANEI_TIMELINE_TOOL,
    ZANEI_TIMELINE_TOOL_ID,
)

__all__ = [
    "ToolDefinition",
    "READ_ACTION_PLAN_TOOL",
    "READ_ACTION_PLAN_TOOL_ID",
    "WRITE_ACTION_PLAN_TOOL",
    "WRITE_ACTION_PLAN_TOOL_ID",
    "READ_TOOL",
    "RENDER_PDF_PAGE_TOOL",
    "RENDER_PDF_PAGE_TOOL_ID",
    "LIST_TOOL",
    "GLOB_TOOL",
    "GREP_TOOL",
    "DRAFT_FINAL_ANSWER_TOOL",
    "DRAFT_FINAL_ANSWER_TOOL_ID",
    "SUBMIT_FINAL_ANSWER_TOOL",
    "SUBMIT_FINAL_ANSWER_TOOL_ID",
    "CANCEL_SUBAGENT_TOOL",
    "CANCEL_SUBAGENT_TOOL_ID",
    "SPAWN_SUBAGENT_TOOL",
    "SPAWN_SUBAGENT_TOOL_ID",
    "SUBMIT_SUBAGENT_REPORT_TOOL_ID",
    "SEND_MESSAGE_TO_SUBAGENT_TOOL",
    "SEND_MESSAGE_TO_SUBAGENT_TOOL_ID",
    "WAIT_SUBAGENTS_TOOL",
    "WAIT_SUBAGENTS_TOOL_ID",
    "APPLY_PATCH_TOOL",
    "BASH_TOOL",
    "CAPTURE_SCREEN_TOOL",
    "CAPTURE_SCREEN_TOOL_ID",
    "RUN_PYTHON_TOOL",
    "THINKING_TOOL",
    "MEMORY_SEARCH_TOOL",
    "ZANEI_QUERY_TOOL",
    "ZANEI_QUERY_TOOL_ID",
    "ZANEI_TIMELINE_TOOL",
    "ZANEI_TIMELINE_TOOL_ID",
    "MEMORY_SQL_TOOL",
    "GET_MEMORY_REFERENCE_TOOL",
    "LINK_MEMORY_TOOL",
    "UNLINK_MEMORY_TOOL",
    "REMEMBER_TOOL",
    "REMEMBER_TOOL_ID",
    "WEB_SEARCH_TOOL",
    "WEB_EXTRACT_TOOL",
    "WEB_CRAWL_TOOL",
    "HISTORY_FETCH_TOOL",
    "SUPERVISOR_SINGLE_REACT_TOOL_IDS",
    "SUPERVISOR_SINGLE_REACT_ACT_TOOL_IDS",
    "TOOL_REGISTRY",
    "select_tool_registry",
    "build_native_action_tools",
    "split_step_note",
    "STEP_NOTE_ARG",
    "STEP_NOTE_DESCRIPTION",
    "STEP_NOTE_MAX_LENGTH",
    "select_supervisor_act_tool_ids",
    "select_supervisor_act_tool_registry",
]

TOOL_REGISTRY: dict[str, ToolDefinition] = {
    tool.tool_id: tool
    for tool in (
        THINKING_TOOL,
        MEMORY_SEARCH_TOOL,
        MEMORY_SQL_TOOL,
        GET_MEMORY_REFERENCE_TOOL,
        LINK_MEMORY_TOOL,
        UNLINK_MEMORY_TOOL,
        REMEMBER_TOOL,
        WEB_SEARCH_TOOL,
        WEB_EXTRACT_TOOL,
        WEB_CRAWL_TOOL,
        READ_TOOL,
        RENDER_PDF_PAGE_TOOL,
        LIST_TOOL,
        GLOB_TOOL,
        GREP_TOOL,
        APPLY_PATCH_TOOL,
        BASH_TOOL,
        RUN_PYTHON_TOOL,
        CAPTURE_SCREEN_TOOL,
        READ_ACTION_PLAN_TOOL,
        WRITE_ACTION_PLAN_TOOL,
        DRAFT_FINAL_ANSWER_TOOL,
        SUBMIT_FINAL_ANSWER_TOOL,
        HISTORY_FETCH_TOOL,
        ZANEI_TIMELINE_TOOL,
        ZANEI_QUERY_TOOL,
        SPAWN_SUBAGENT_TOOL,
        SEND_MESSAGE_TO_SUBAGENT_TOOL,
        WAIT_SUBAGENTS_TOOL,
        CANCEL_SUBAGENT_TOOL,
    )
}

SUPERVISOR_SINGLE_REACT_TOOL_IDS: tuple[str, ...] = (
    THINKING_TOOL.tool_id,
    MEMORY_SEARCH_TOOL.tool_id,
    MEMORY_SQL_TOOL.tool_id,
    GET_MEMORY_REFERENCE_TOOL.tool_id,
    LINK_MEMORY_TOOL.tool_id,
    UNLINK_MEMORY_TOOL.tool_id,
    REMEMBER_TOOL.tool_id,
    WEB_SEARCH_TOOL.tool_id,
    WEB_EXTRACT_TOOL.tool_id,
    WEB_CRAWL_TOOL.tool_id,
    READ_TOOL.tool_id,
    RENDER_PDF_PAGE_TOOL.tool_id,
    LIST_TOOL.tool_id,
    GLOB_TOOL.tool_id,
    GREP_TOOL.tool_id,
    APPLY_PATCH_TOOL.tool_id,
    BASH_TOOL.tool_id,
    RUN_PYTHON_TOOL.tool_id,
    CAPTURE_SCREEN_TOOL.tool_id,
    READ_ACTION_PLAN_TOOL.tool_id,
    WRITE_ACTION_PLAN_TOOL.tool_id,
    SPAWN_SUBAGENT_TOOL.tool_id,
    SEND_MESSAGE_TO_SUBAGENT_TOOL.tool_id,
    WAIT_SUBAGENTS_TOOL.tool_id,
    CANCEL_SUBAGENT_TOOL.tool_id,
    DRAFT_FINAL_ANSWER_TOOL.tool_id,
    SUBMIT_FINAL_ANSWER_TOOL.tool_id,
    HISTORY_FETCH_TOOL.tool_id,
    ZANEI_TIMELINE_TOOL.tool_id,
    ZANEI_QUERY_TOOL.tool_id,
)

SUPERVISOR_SINGLE_REACT_ACT_TOOL_IDS: tuple[str, ...] = SUPERVISOR_SINGLE_REACT_TOOL_IDS


def select_tool_registry(tool_ids: tuple[str, ...]) -> dict[str, ToolDefinition]:
    return {tool_id: TOOL_REGISTRY[tool_id] for tool_id in tool_ids}


_SUPERVISOR_SINGLE_REACT_ACT_TOOL_REGISTRY = select_tool_registry(
    SUPERVISOR_SINGLE_REACT_ACT_TOOL_IDS
)


def select_supervisor_act_tool_ids() -> tuple[str, ...]:
    return SUPERVISOR_SINGLE_REACT_ACT_TOOL_IDS


def select_supervisor_act_tool_registry() -> dict[str, ToolDefinition]:
    return dict(_SUPERVISOR_SINGLE_REACT_ACT_TOOL_REGISTRY)
