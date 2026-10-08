"""The chat's own read-only tools, for looking things up before it answers.

They are the shared ones Suggestion composes (files, memory, memory_sql, web
and Zanei), over the user's registered folders and read access. A result too
large to show inline goes to the turn's own folder in the app's storage, which
the turn removes when it ends. The tools write nothing and ask for nothing:
work that changes things is a task the chat starts.
"""

from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path
from typing import Final

from pantaray_agents.local_runtime.context import store as context_store
from pantaray_agents.local_runtime.context.source_control import context_source_control
from pantaray_agents.local_runtime.context.source_gate import SourceInvalidated
from pantaray_agents.local_runtime.context.source_reader import SourceReader
from pantaray_agents.local_runtime.memory_catalog.connection import (
    open_memory_catalog_connection,
)
from pantaray_agents.local_runtime.tooling.action_session_temp_paths import (
    resolve_local_runtime_storage_base,
    validate_action_storage_component,
)
from pantaray_agents.local_runtime.tooling.outside_workspace_grant import (
    app_owned_roots,
)
from pantaray_agents.local_runtime.tooling.repository.workspace_settings import (
    list_workspace_settings,
)
from pantaray_agents.local_runtime.tooling.suggestion_research.zanei import (
    InsightActivityStart,
    SuggestionZaneiSession,
)
from pantaray_agents.tools.contract import (
    ReactToolCall,
    ReactToolDefinition,
    ReactToolResult,
    tool_error_response,
)
from pantaray_agents.tools.files.read_only_tools import build_read_only_file_tools
from pantaray_agents.tools.memory.retrieval import (
    MemoryContextSession,
    MemoryRetrievalPolicy,
    MemoryRetrievalSession,
)
from pantaray_agents.tools.memory.sql_tool import MemorySqlSession
from pantaray_agents.tools.web.session import WebResearchToolSession

CHAT_TOOL_RESULTS_DIRNAME: Final[str] = "chat_tool_results"
# Suggestion's research bounds: memory is read through these tools only.
_MEMORY_SEARCH_MAX_RESULTS: Final[int] = 8
_MEMORY_SEARCH_CONTENT_MAX_CHARS: Final[int] = 6_000
_MEMORY_REFERENCE_CONTENT_MAX_CHARS: Final[int] = 4_000


def chat_tool_results_root(*, db_path: Path, run_id: str) -> Path:
    validate_action_storage_component(field_name="run_id", value=run_id)
    return (
        resolve_local_runtime_storage_base(db_path=db_path)
        / CHAT_TOOL_RESULTS_DIRNAME
        / run_id
    )


def discard_chat_tool_results(*, db_path: Path, run_id: str) -> None:
    # Design limit: a turn the app quits in leaves its folder behind; sweep this
    # root at startup if leftovers are seen to grow.
    shutil.rmtree(chat_tool_results_root(db_path=db_path, run_id=run_id), True)


async def chat_research_tools(
    *, db_path: Path, busy_timeout_ms: int, user_id: str, run_id: str
) -> tuple[ReactToolDefinition, ...]:
    """The read-only tools of one turn ``run_id``; blocking, run it off the loop
    the server answers on."""

    settings = list_workspace_settings(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms, user_id=user_id
    )
    memory = MemoryRetrievalSession(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        context=MemoryContextSession(user_id=user_id, run_id=run_id),
        policy=MemoryRetrievalPolicy(
            allowed_focuses=("stable_knowledge", "activity", "agent_work", "all"),
            default_focus=None,
            max_results=_MEMORY_SEARCH_MAX_RESULTS,
            default_limit=None,
            call_limit=None,
            pinned_revisions=None,
            search_content_max_chars=_MEMORY_SEARCH_CONTENT_MAX_CHARS,
            reference_content_max_chars=_MEMORY_REFERENCE_CONTENT_MAX_CHARS,
            enqueue_repair_on_reference_failure=False,
        ),
    )
    return (
        *memory.definitions(),
        MemorySqlSession(
            db_path=db_path, busy_timeout_ms=busy_timeout_ms, user_id=user_id
        ).definition(),
        *build_read_only_file_tools(
            db_path=db_path,
            folders=tuple(
                Path(folder.canonical_real_path) for folder in settings.folders
            ),
            read_access_scope=settings.read_access_scope,
            app_storage_roots=app_owned_roots(db_path),
            spill_root=chat_tool_results_root(db_path=db_path, run_id=run_id),
        ),
        *WebResearchToolSession(user_id=user_id, speaks_to_user=True).definitions(),
        *(
            _recording_may_stop(tool)
            for tool in SuggestionZaneiSession(
                reader=SourceReader(context_source_control.gate),
                start=await _recent_activity(db_path, busy_timeout_ms, user_id),
            ).definitions()
        ),
    )


async def _recent_activity(
    db_path: Path, busy_timeout_ms: int, user_id: str
) -> InsightActivityStart | None:
    """What the latest short Insight has not summarized yet, when readable."""

    gate = context_source_control.gate
    async with gate.turn():
        source = gate.current(user_id)
    if source is None:
        return None
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms
    ) as connection:
        cursor = context_store.get_cursor(connection, source.binding)
    return (
        None if cursor is None else InsightActivityStart(source=source, cursor=cursor)
    )


def _recording_may_stop(tool: ReactToolDefinition) -> ReactToolDefinition:
    """``tool``, answering in words when the user stops recording mid-turn."""

    async def execute(call: ReactToolCall, step: int) -> ReactToolResult:
        try:
            return await tool.execute(call, step)
        except SourceInvalidated:
            return tool_error_response(
                tool_name=call.tool_name,
                error_code="RECORDING_UNAVAILABLE",
                message="Computer activity cannot be read right now.",
            )

    return replace(tool, execute=execute)


__all__ = ["chat_research_tools", "discard_chat_tool_results"]
